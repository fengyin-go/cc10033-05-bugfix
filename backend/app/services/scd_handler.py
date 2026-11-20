"""SCD处理器服务：管理缓慢变化维的处理策略，支持SCD类型1/2/3真实处理逻辑。

增强能力：
- SCD类型1：直接覆盖旧值（只保留最新）
- SCD类型2：新增行保留历史，通过生效/失效时间 + 当前标记追踪
- SCD类型3：保留原值和新值两列，支持当前值与上一值并存
- 消费 dim.defined 和 attr.confirmed 通道消息
- 变化检测基于MD5哈希，自动生成suggested策略
- 启用策略时可携带属性变更批次，经 scd_versioning 引擎带乐观锁落库
"""
from __future__ import annotations

import hashlib
from datetime import datetime, timezone
from typing import Any

from app.services.base import BaseModuleService
from app.services.scd_versioning import submit_scd_batch
from app.store import store


class ScdHandlerService(BaseModuleService):
    MODULE = "scd_handler"
    ENTITY = "SCD策略"
    OPERATION_TERM = "校验"
    LIST_FIELDS = ["策略名称", "目标维度", "SCD类型", "变化检测", "生效时间", "失效时间", "当前标记"]
    REQUIRED_FIELDS = ["策略名称", "目标维度", "SCD类型"]
    STATUS_ORDER = ["草稿", "已启用", "已停用"]
    ACTION_RULES = {"启用策略": "草稿", "停用策略": "已启用"}
    NEGATIVE_ACTIONS = ["停用策略"]

    VALID_SCD_TYPES = {"SCD类型0", "SCD类型1", "SCD类型2", "SCD类型3"}

    def create_entry(self, values: dict[str, Any]) -> tuple[dict[str, Any] | None, list[str]]:
        """创建SCD策略：消费维度定义和属性确认通道，校验SCD类型并生成变化检测规则。"""
        scd_type = str(values.get("SCD类型", "SCD类型2")).strip()
        if scd_type not in self.VALID_SCD_TYPES:
            scd_type = "SCD类型2"
            values["SCD类型"] = scd_type

        dim_code = str(values.get("目标维度", "")).strip()

        # 消费维度定义通道
        dim_msg = store.channel.consume_latest("dim.defined", "scd_handler")
        if dim_msg and dim_msg["payload"].get("dim_code") == dim_code:
            # 维度已定义，自动继承SCD类型
            inherited_scd = dim_msg["payload"].get("scd_type", scd_type)
            if values.get("SCD类型") != inherited_scd:
                values["SCD类型"] = inherited_scd
                scd_type = inherited_scd

        # 消费属性确认通道，构建变化检测规则
        attr_msgs = store.channel.consume("attr.confirmed", "scd_handler")
        detection_fields = []
        for m in attr_msgs:
            if m["payload"].get("dim_code") == dim_code:
                detection_fields.append(m["payload"].get("attr_name", ""))

        # 生成变化检测规则
        if detection_fields:
            detection_rule = _build_detection_rule(scd_type, detection_fields)
        else:
            detection_rule = "MD5(record)"

        values["变化检测"] = detection_rule

        # 设置生效/失效时间默认值
        if not values.get("生效时间"):
            values["生效时间"] = "2026-01-01"
        if not values.get("失效时间"):
            values["失效时间"] = "9999-12-31"
        if not values.get("当前标记"):
            values["当前标记"] = "Y"

        entry, missing = super().create_entry(values)
        if entry is None:
            return None, missing

        # 发布SCD策略到数据通道
        store.channel.publish("scd.strategy_created", {
            "strategy_name": entry["策略名称"],
            "dim_code": dim_code,
            "scd_type": entry["SCD类型"],
            "detection_rule": detection_rule,
            "entry_id": entry["id"],
        })

        # 注册SCD策略产物
        store.artifacts.register(
            "scd_handler", "scd_strategy",
            f"scd_{entry['id']}",
            {"strategy_name": entry["策略名称"], "dim_code": dim_code, "scd_type": entry["SCD类型"]},
            source_modules=["dim_definition", "dim_attr", "scd_handler"],
        )
        return entry, []

    def submit_changes(self, entry_id: int, changes: list[dict[str, Any]],
                       batch_id: str | None = None) -> tuple[dict[str, Any] | None, str]:
        """提交属性变更批次：逐维度带乐观锁落库，返回同一份处理结果。"""
        entry = store.find(self.MODULE, entry_id)
        if entry is None:
            return None, f"{self.ENTITY} {entry_id} 不存在或已归档"
        if not changes:
            return None, "变更批次为空，无可提交的属性变更"
        outcome = submit_scd_batch(entry, changes, batch_id=batch_id)
        store.channel.publish("scd.processing_result", {
            "strategy_name": entry["策略名称"],
            "dim_code": entry.get("目标维度", ""),
            "scd_type": entry.get("SCD类型", ""),
            "result": outcome,
            "entry_id": entry_id,
            "batch_id": outcome["batch_id"],
        })
        return outcome, "SCD变更批次已处理"

    def run_action(self, entry_id: int, action: str,
                   payload: dict[str, Any] | None = None) -> tuple[dict[str, Any] | None, str]:
        entry, message = super().run_action(entry_id, action)
        if entry is None:
            return None, message

        payload = payload or {}
        scd_type = entry.get("SCD类型", "")
        dim_code = entry.get("目标维度", "")

        if action == "启用策略":
            changes = payload.get("changes")
            if changes:
                # 启用即提交：把当次属性变更真正落到维度版本记录
                outcome = submit_scd_batch(entry, changes, batch_id=payload.get("batch_id"))
                store.channel.publish("scd.processing_result", {
                    "strategy_name": entry["策略名称"],
                    "dim_code": dim_code,
                    "scd_type": scd_type,
                    "result": outcome,
                    "entry_id": entry_id,
                    "batch_id": outcome["batch_id"],
                })
                entry["最近处理批次"] = outcome["batch_id"]
                entry["最近切换维度"] = ",".join(outcome["switched_dimensions"])
            else:
                # 无变更数据时仅发布策略级处理模拟，不写维度版本、不写变更日志
                result = _simulate_scd_processing(entry)
                store.channel.publish("scd.processing_result", {
                    "strategy_name": entry["策略名称"],
                    "dim_code": dim_code,
                    "scd_type": scd_type,
                    "result": result,
                    "entry_id": entry_id,
                })

        elif action == "停用策略":
            store.channel.publish("scd.strategy_deactivated", {
                "strategy_name": entry["策略名称"],
                "dim_code": dim_code,
                "entry_id": entry_id,
            })

        return entry, message

    def domain_snapshot(self) -> dict[str, Any]:
        base = super().domain_snapshot()
        # SCD类型分布
        scd_dist: dict[str, int] = {}
        for row in store.rows(self.MODULE):
            t = row.get("SCD类型", "未指定")
            scd_dist[t] = scd_dist.get(t, 0) + 1
        base["scd_type_distribution"] = scd_dist

        # 消费的维度和属性消息统计
        base["consumed_dims"] = len(store.channel.consume("dim.defined", "scd_handler"))
        base["consumed_attrs"] = len(store.channel.consume("attr.confirmed", "scd_handler"))

        # 处理结果汇总
        base["processing_results"] = len(store.channel.consume("scd.processing_result", "scd_handler"))
        return base


def _build_detection_rule(scd_type: str, fields: list[str]) -> str:
    """根据SCD类型和属性字段构建变化检测规则。"""
    if not fields:
        return "MD5(record)"
    field_list = ", ".join(fields)
    if scd_type == "SCD类型1":
        return f"MD5({field_list}) -- 直接覆盖"
    elif scd_type == "SCD类型2":
        return f"MD5({field_list}) -- 新增行保留历史"
    elif scd_type == "SCD类型3":
        return f"字段级比较: {field_list} -- 保留旧值列"
    return f"MD5({field_list})"


def _simulate_scd_processing(entry: dict[str, Any]) -> dict[str, Any]:
    """模拟SCD处理过程，展示不同类型的变化处理结果。"""
    scd_type = entry.get("SCD类型", "SCD类型2")
    detection_rule = entry.get("变化检测", "")
    dim_code = entry.get("目标维度", "")

    # 计算变化检测哈希
    rule_hash = hashlib.md5(detection_rule.encode()).hexdigest()[:12]

    if scd_type == "SCD类型1":
        return {
            "action": "overwrite", "algorithm": "scd1_direct_overwrite",
            "operation": f"直接更新{dim_code}表当前行，不保留历史",
            "affected_fields": [f.strip() for f in detection_rule.replace("MD5(", "").replace(")", "").split(",") if f.strip()],
            "rule_hash": rule_hash,
            "history_preserved": False,
            "new_rows_created": 0,
        }
    elif scd_type == "SCD类型2":
        return {
            "action": "insert_new_row",
            "algorithm": "scd2_versioning",
            "operation": f"为{dim_code}插入新版本行，设置旧行eff_end_dt=当前时间-1秒, is_current='N'",
            "affected_fields": [f.strip() for f in detection_rule.replace("MD5(", "").replace(")", "").split(",") if f.strip()],
            "rule_hash": rule_hash,
            "history_preserved": True,
            "new_rows_created": 1,
            "old_row_updated": True,
            "version_columns": {"eff_start_dt": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S"),
                               "eff_end_dt": "9999-12-31", "is_current": "Y"},
        }
    elif scd_type == "SCD类型3":
        return {
            "action": "update_with_history",
            "algorithm": "scd3_previous_value",
            "operation": f"更新{dim_code}表，将当前值移入prev_列，写入新值到当前列",
            "affected_fields": [f.strip() for f in detection_rule.replace("MD5(", "").replace(")", "").split(",") if f.strip()],
            "rule_hash": rule_hash,
            "history_preserved": True,
            "new_rows_created": 0,
            "prev_columns": [f"prev_{f.strip()}" for f in detection_rule.replace("MD5(", "").replace(")", "").split(",") if f.strip()],
        }
    else:
        return {
            "action": "no_change",
            "algorithm": "scd0_immutable",
            "operation": f"{dim_code}为不可变维度，跳过更新",
            "affected_fields": [],
            "rule_hash": rule_hash,
            "history_preserved": False,
            "new_rows_created": 0,
        }