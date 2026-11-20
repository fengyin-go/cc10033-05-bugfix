"""SCD 属性变更落库引擎：把一次属性变更作为带乐观锁的事务提交到维度定义。

设计要点（对应缺陷修复）：
- 当次处理的属性值持久化到维度定义的新版本记录（SCD2/6 插行，SCD1/3 就地更新）。
- 切换时旧版本「版本失效时间」与新版本「版本生效时间」严格衔接（同一时刻派生）。
- 旧版本号不匹配判定为版本冲突：不插新版本行、不写变更日志，其余维度不受影响。
- 写入步骤失败时回滚全部改动，绝不留下额外的已生效记录，也不重复记日志。
- 批次幂等：同一 (批次ID, 目标维度) 重复提交直接回放首次结果，不二次落库。
- 变更日志只为真正提交成功的维度登记实际写入的前后值与生效日期，并附策略名便于回查。
"""
from __future__ import annotations

import re
import uuid
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from typing import Any

from app.store import store

OPEN_END_TS = "9999-12-31 23:59:59"
DEFAULT_EFF_DATE = "2026-01-01"

# 维度定义版本记录在通用字段之外扩展的版本管理字段
VERSION_FIELDS = [
    "当前版本号",
    "上一版本号",
    "版本生效时间",
    "版本失效时间",
    "当前标记",
    "版本属性",
    "来源批次ID",
    "来源策略",
]

_DIM_LIST_FIELDS = [
    "维度编码", "维度名称", "主键定义", "SCD类型", "粒度说明", "数据来源", "刷新频率",
]


def parse_detection_fields(rule: str) -> list[str]:
    """从变化检测规则（如 MD5(a, b) / 字段级比较: a -- ...）解析受监控属性名。"""
    if not rule:
        return []
    match = re.search(r"[（(]([^()（）]+)[)）]", rule)
    body = match.group(1) if match else rule
    fields: list[str] = []
    for part in re.split(r"[,，]", body):
        name = re.sub(r"^字段级比较\s*[:：]?\s*", "", part).strip()
        name = name.split("--")[0].strip()
        if name and re.match(r"^[A-Za-z_][A-Za-z0-9_]*$", name) and name not in fields:
            fields.append(name)
    return fields


def _new_batch_id() -> str:
    return f"BATCH-{datetime.now(timezone.utc).strftime('%Y%m%d%H%M%S')}-{uuid.uuid4().hex[:6]}"


def _effective_window(effective_date: str | None) -> tuple[str, str, str]:
    """由同一个生效日期派生新版本生效时间与旧版本失效时间，保证区间衔接。

    返回 (生效日期, 新版本生效时间, 旧版本失效时间)。
    未给生效日期时取当前时刻：旧版本截止到切换前一秒，新版本从该秒起生效。
    """
    effective_date = (effective_date or "").strip()
    if effective_date:
        start = datetime.strptime(effective_date, "%Y-%m-%d")
        start_ts = start.strftime("%Y-%m-%d %H:%M:%S")
        end_ts = (start - timedelta(seconds=1)).strftime("%Y-%m-%d %H:%M:%S")
        return effective_date, start_ts, end_ts
    start = datetime.now(timezone.utc).replace(microsecond=0)
    return (
        start.strftime("%Y-%m-%d"),
        start.strftime("%Y-%m-%d %H:%M:%S"),
        (start - timedelta(seconds=1)).strftime("%Y-%m-%d %H:%M:%S"),
    )


def _find_dim_row(dim_code: str) -> dict[str, Any] | None:
    for row in store.rows("dim_definition"):
        if str(row.get("维度编码", "")) == dim_code:
            return row
    return None


def _current_version_row(dim_code: str) -> dict[str, Any] | None:
    """取维度当前生效版本行；历史数据没有版本标记时，原始行即当前版本。"""
    candidates = [r for r in store.rows("dim_definition") if str(r.get("维度编码", "")) == dim_code]
    if not candidates:
        return None
    marked = [r for r in candidates if str(r.get("当前标记", "Y")) == "Y"]
    if marked:
        return marked[0]
    return candidates[0]


def _version_no(row: dict[str, Any]) -> int:
    try:
        return int(row.get("当前版本号", 1))
    except (TypeError, ValueError):
        return 1


def _normalize_attrs(raw: Any) -> dict[str, str]:
    """兼容 dict 与 'a=1,b=2' 文本两种属性提交形式。"""
    if isinstance(raw, dict):
        return {str(k).strip(): str(v) for k, v in raw.items() if str(k).strip()}
    attrs: dict[str, str] = {}
    for line in str(raw or "").replace("，", ",").replace("；", ";").split(";"):
        for pair in line.split(","):
            if "=" in pair:
                key, value = pair.split("=", 1)
                key = key.strip()
                if key:
                    attrs[key] = value.strip()
    return attrs


def _log_change(
    *, dim_code: str, field: str, old_value: str, new_value: str,
    effective_date: str, strategy_name: str, batch_id: str,
    new_version_no: int,
) -> dict[str, Any]:
    """只为确实提交的维度写一条变更日志，登记实际前后值与生效日期。"""
    table = store.rows("slow_change_log")
    log_id = max((int(r.get("id", 0)) for r in table), default=0) + 1
    entry = {
        "id": log_id,
        "status": "已记录",
        "pending": True,
        "abnormal": False,
        "变更ID": f"SCL_{dim_code}_{batch_id.replace('BATCH-', '')}_{field}",
        "目标维度": dim_code,
        "变更字段": field,
        "旧值": old_value,
        "新值": new_value,
        "变更时间": effective_date,
        "变更原因": f"SCD策略[{strategy_name}]提交，批次{batch_id}，生效版本v{new_version_no}",
        "批次ID": batch_id,
        "策略名称": strategy_name,
        "生效版本号": new_version_no,
    }
    table.append(entry)
    return entry


def _failed(dim_code: str, reason: str, *, expected_version: Any = None,
            current_version: Any = None, batch_id: str = "") -> dict[str, Any]:
    return {
        "dim_code": dim_code,
        "ok": False,
        "switched": False,
        "reason": reason,
        "expected_version": expected_version,
        "current_version": current_version,
        "batch_id": batch_id,
        "logs": [],
    }


def apply_scd_change(strategy_entry: dict[str, Any], change: dict[str, Any],
                     batch_id: str) -> dict[str, Any]:
    """提交单个维度的 SCD 属性变更，返回该维度的处理结果。

    整个过程是一次乐观锁事务：校验全部通过后才动手写库，
    写入异常会回滚旧行修改并摘除新行，且不产生任何变更日志。
    """
    dim_code = str(change.get("目标维度", "")).strip()
    strategy_name = str(strategy_entry.get("策略名称", ""))
    scd_type = str(strategy_entry.get("SCD类型", "SCD类型2"))
    attr_values = _normalize_attrs(change.get("属性变更", change.get("属性值", {})))
    effective_date = str(change.get("生效日期", "")).strip() or None
    fault_injection = bool(change.get("模拟写入失败", False))

    if not dim_code:
        return _failed("(空)", "缺少目标维度", batch_id=batch_id)
    if not attr_values:
        return _failed(dim_code, "缺少变更属性", batch_id=batch_id)

    current = _current_version_row(dim_code)
    if current is None:
        return _failed(dim_code, f"维度 {dim_code} 不存在，无法定位当前版本", batch_id=batch_id)

    current_no = _version_no(current)
    raw_expected = change.get("旧版本号", current_no)
    try:
        expected_no = int(raw_expected)
    except (TypeError, ValueError):
        return _failed(dim_code, f"旧版本号格式非法：{raw_expected!r}",
                       expected_version=raw_expected, current_version=current_no,
                       batch_id=batch_id)

    # 乐观锁：旧版本不匹配即冲突，不做任何写入
    if expected_no != current_no:
        return _failed(
            dim_code,
            f"版本冲突：提交基于v{expected_no}，维度当前已是v{current_no}",
            expected_version=expected_no, current_version=current_no, batch_id=batch_id,
        )

    eff_date, start_ts, end_ts = _effective_window(effective_date)

    # 仅对真正发生变化的属性登记日志；属性值照样全部持久化到版本记录
    changed: dict[str, str] = {}
    for key, new_value in attr_values.items():
        old_value = str(current.get(key, ""))
        if old_value != new_value:
            changed[key] = old_value

    if not changed:
        return {
            "dim_code": dim_code,
            "ok": True,
            "switched": False,
            "reason": "属性值与当前版本一致，无实际变化，未生成新版本",
            "expected_version": expected_no,
            "current_version": current_no,
            "new_version": current_no,
            "effective_date": eff_date,
            "batch_id": batch_id,
            "logs": [],
        }

    # ---- 以下为写入区：先备份，异常即回滚，保证不留半成品已生效记录 ----
    backup = deepcopy(current)
    new_row: dict[str, Any] | None = None
    written_logs: list[dict[str, Any]] = []
    log_table_len_before = len(store.rows("slow_change_log"))
    dim_table = store.rows("dim_definition")

    try:
        if fault_injection:
            raise RuntimeError("模拟写入失败：维度版本落库异常")

        if scd_type in ("SCD类型2", "SCD类型6"):
            new_no = current_no + 1
            # 1) 关闭旧版本：截止时间与新版本生效时间衔接
            current["当前标记"] = "N"
            current["版本失效时间"] = end_ts
            current["失效时间"] = end_ts
            # 2) 组装并插入新版本行，持久化当次全部属性值
            new_row = {field: current.get(field, "") for field in _DIM_LIST_FIELDS}
            new_row["维度编码"] = dim_code
            new_row["id"] = max((int(r.get("id", 0)) for r in dim_table), default=0) + 1
            new_row["status"] = "已发布"
            new_row["pending"] = False
            new_row["abnormal"] = False
            new_row["current_marker"] = "1"
            new_row["当前标记"] = "Y"
            new_row["当前版本号"] = new_no
            new_row["上一版本号"] = current_no
            new_row["版本生效时间"] = start_ts
            new_row["版本失效时间"] = OPEN_END_TS
            new_row["生效时间"] = start_ts
            new_row["失效时间"] = OPEN_END_TS
            new_row["来源批次ID"] = batch_id
            new_row["来源策略"] = strategy_name
            for key, value in attr_values.items():
                new_row[key] = value
            new_row["版本属性"] = "; ".join(f"{k}={v}" for k, v in attr_values.items())
            dim_table.append(new_row)
            committed_version = new_no
        elif scd_type == "SCD类型1":
            # 直接覆盖当前行，不保留历史版本
            for key, value in attr_values.items():
                current[key] = value
            current["版本属性"] = "; ".join(f"{k}={v}" for k, v in attr_values.items())
            current["来源批次ID"] = batch_id
            current["来源策略"] = strategy_name
            committed_version = current_no
        elif scd_type == "SCD类型3":
            # 当前值落入 prev_ 列，新值写入当前列
            for key, value in attr_values.items():
                current[f"prev_{key}"] = current.get(key, "")
                current[key] = value
            current["来源批次ID"] = batch_id
            current["来源策略"] = strategy_name
            committed_version = current_no
        else:  # SCD类型0 及其它不可变策略
            raise RuntimeError(f"{scd_type}为不可变维度，属性变更不允许落库")

        # 3) 写入成功后才登记变更日志（每个变化属性一条，只记实际前后值）
        for key, old_value in changed.items():
            written_logs.append(_log_change(
                dim_code=dim_code, field=key, old_value=old_value,
                new_value=attr_values[key], effective_date=eff_date,
                strategy_name=strategy_name, batch_id=batch_id,
                new_version_no=committed_version,
            ))

    except Exception as exc:  # 回滚：恢复旧行、摘除新行、撤销日志
        for key, value in backup.items():
            current[key] = value
        if new_row is not None and new_row in dim_table:
            dim_table.remove(new_row)
        del store.rows("slow_change_log")[log_table_len_before:]
        return _failed(dim_code, f"写入失败已回滚：{exc}",
                       expected_version=expected_no, current_version=current_no,
                       batch_id=batch_id)

    # 维度定义接收 SCD 提交结果，更新当前版本指针
    from app.services.dim_definition import DimDefinitionService
    DimDefinitionService().receive_scd_commit({
        "dim_code": dim_code,
        "scd_type": scd_type,
        "old_version": current_no,
        "new_version": committed_version,
        "effective_start": start_ts,
        "effective_end": OPEN_END_TS,
        "batch_id": batch_id,
        "strategy_name": strategy_name,
        "attributes": dict(attr_values),
        "current_row_id": (new_row or current)["id"],
    })

    return {
        "dim_code": dim_code,
        "ok": True,
        "switched": True,
        "reason": "提交成功",
        "scd_type": scd_type,
        "expected_version": expected_no,
        "current_version": current_no,
        "new_version": committed_version,
        "effective_date": eff_date,
        "effective_start": start_ts,
        "old_version_end": end_ts if scd_type in ("SCD类型2", "SCD类型6") else None,
        "current_row_id": (new_row or current)["id"],
        "batch_id": batch_id,
        "changed_fields": list(changed.keys()),
        "logs": [log["变更ID"] for log in written_logs],
    }


def submit_scd_batch(strategy_entry: dict[str, Any],
                     changes: list[dict[str, Any]],
                     batch_id: str | None = None) -> dict[str, Any]:
    """批量提交多个维度的属性变更。

    逐维度独立乐观锁提交：一个维度因旧版本不匹配失败，不影响其余具备写入条件的维度。
    批次按 (批次ID, 维度) 幂等，重复提交回放首次结果，不会二次切换、二次记日志。
    """
    batch_id = batch_id or _new_batch_id()
    index = store.scd_commits.setdefault(batch_id, {})

    results: list[dict[str, Any]] = []
    replayed = 0
    for change in changes:
        dim_code = str(change.get("目标维度", "")).strip() or "(空)"
        if dim_code in index:
            results.append(deepcopy(index[dim_code]))
            replayed += 1
            continue
        outcome = apply_scd_change(strategy_entry, change, batch_id)
        index[dim_code] = deepcopy(outcome)
        results.append(outcome)

    applied = [r for r in results if r.get("switched")]
    conflicts = [r for r in results if not r.get("ok") and "版本冲突" in str(r.get("reason", ""))]
    failures = [r for r in results if not r.get("ok") and "版本冲突" not in str(r.get("reason", ""))]
    unchanged = [r for r in results if r.get("ok") and not r.get("switched")]

    # 仅统计本次真正落库的维度；幂等回放的结果不计入切换数，避免把一次成功变更记两遍
    fresh = results[replayed:]
    fresh_applied = [r for r in fresh if r.get("switched")]
    fresh_conflicts = [r for r in fresh if not r.get("ok") and "版本冲突" in str(r.get("reason", ""))]
    fresh_failures = [r for r in fresh if not r.get("ok") and "版本冲突" not in str(r.get("reason", ""))]
    fresh_unchanged = [r for r in fresh if r.get("ok") and not r.get("switched")]

    return {
        "batch_id": batch_id,
        "strategy_name": strategy_entry.get("策略名称", ""),
        "scd_type": strategy_entry.get("SCD类型", ""),
        "total": len(results),
        "replayed_count": replayed,
        "applied_count": len(fresh_applied),
        "conflict_count": len(fresh_conflicts),
        "failure_count": len(fresh_failures),
        "unchanged_count": len(fresh_unchanged),
        "switched_dimensions": [r["dim_code"] for r in fresh_applied],
        "conflict_dimensions": [r["dim_code"] for r in fresh_conflicts],
        "failed_dimensions": [r["dim_code"] for r in fresh_failures],
        "results": results,
    }
