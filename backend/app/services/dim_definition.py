"""维度定义服务：管理维度表定义、主键设计、SCD类型配置。

增强能力：
- 创建维度时通过 DataChannel 发布维度定义，供 dim_attr 和 scd_handler 消费
- 维度编码重复校验与标准化转换
- SCD类型验证与默认值设定
- 发布维度时触发下游模块同步处理
"""
from __future__ import annotations

import re
from typing import Any

from app.services.base import BaseModuleService
from app.store import store


class DimDefinitionService(BaseModuleService):
    MODULE = "dim_definition"
    ENTITY = "维度表"
    OPERATION_TERM = "校验"
    LIST_FIELDS = ["维度编码", "维度名称", "主键定义", "SCD类型", "粒度说明", "数据来源", "刷新频率"]
    REQUIRED_FIELDS = ["维度编码", "维度名称", "主键定义"]
    STATUS_ORDER = ["草稿", "已建模", "已发布", "已停用"]
    ACTION_RULES = {"提交建模": "草稿", "发布维度": "已建模", "停用维度": "已发布"}
    NEGATIVE_ACTIONS = ["停用维度"]

    VALID_SCD_TYPES = {"SCD类型0", "SCD类型1", "SCD类型2", "SCD类型3", "SCD类型4", "SCD类型6"}
    CODE_PATTERN = re.compile(r'^DIM_[A-Z0-9_]+$')

    def create_entry(self, values: dict[str, Any]) -> tuple[dict[str, Any] | None, list[str]]:
        """创建维度定义，校验编码格式和SCD类型合法性，发布到数据通道。"""
        # 标准化处理
        dim_code = str(values.get("维度编码", "")).strip().upper()
        if dim_code and not self.CODE_PATTERN.match(dim_code):
            dim_code = "DIM_" + re.sub(r'[^A-Z0-9_]', '', dim_code.upper())
            values["维度编码"] = dim_code

        scd_type = str(values.get("SCD类型", "SCD类型2")).strip()
        if scd_type not in self.VALID_SCD_TYPES:
            scd_type = "SCD类型2"
            values["SCD类型"] = scd_type

        # 编码唯一性检查
        existing = store.rows(self.MODULE)
        for row in existing:
            if row.get("维度编码") == dim_code:
                return None, [f"维度编码 {dim_code} 已存在"]

        entry, missing = super().create_entry(values)
        if entry is None:
            return None, missing

        # 发布维度定义到数据通道，让 dim_attr 和 scd_handler 消费
        store.channel.publish("dim.defined", {
            "dim_code": entry["维度编码"],
            "dim_name": entry["维度名称"],
            "scd_type": entry["SCD类型"],
            "primary_key": entry["主键定义"],
            "entry_id": entry["id"],
        })

        # 注册数据产物
        store.artifacts.register(
            "dim_definition", "dim_definition",
            f"dim_{entry['id']}",
            {"dim_code": entry["维度编码"], "dim_name": entry["维度名称"], "scd_type": entry["SCD类型"]},
            source_modules=["dim_definition"],
        )
        return entry, []

    def run_action(self, entry_id: int, action: str) -> tuple[dict[str, Any] | None, str]:
        entry, message = super().run_action(entry_id, action)
        if entry is None:
            return None, message

        if action == "发布维度":
            # 发布维度时，通知下游模块处理维度发布事件
            store.channel.publish("dim.published", {
                "dim_code": entry["维度编码"],
                "dim_name": entry["维度名称"],
                "scd_type": entry["SCD类型"],
                "primary_key": entry["主键定义"],
                "granularity": entry["粒度说明"],
                "source": entry["数据来源"],
                "refresh_frequency": entry["刷新频率"],
                "entry_id": entry["id"],
            })
            # 为维度属性建立关联消费标记
            _auto_associate_attributes(entry["维度编码"], entry_id)
            # 为SCD策略建立自动适配
            _auto_create_scd_strategy(entry)
        elif action == "停用维度":
            store.channel.publish("dim.deactivated", {
                "dim_code": entry["维度编码"],
                "entry_id": entry["id"],
            })
            # 过期相关 topic 上未消费的消息
            store.channel.expire_topic("dim.defined")
        return entry, message

    def domain_snapshot(self) -> dict[str, Any]:
        base = super().domain_snapshot()
        # 增强快照：附加已发布维度关联的属性信息
        published_dims = []
        for row in store.rows(self.MODULE):
            if row.get("status") == "已发布":
                published_dims.append({
                    "dim_code": row.get("维度编码"),
                    "dim_name": row.get("维度名称"),
                    "scd_type": row.get("SCD类型"),
                })
        base["published_dims"] = published_dims
        base["channel_stats"] = store.channel.topic_stats().get("dim.defined", {})
        return base

    def receive_scd_commit(self, commit: dict[str, Any]) -> dict[str, Any]:
        """接收 SCD 处理服务的提交结果，更新维度定义的当前版本指针。

        SCD2/6 已由落库引擎插入新版本行，这里只把维度当前版本元数据对齐到提交结果；
        SCD1/3 不产生新行，当前版本号保持不变，但刷新来源批次与适用区间。
        """
        dim_code = str(commit.get("dim_code", ""))
        current_row = None
        for row in store.rows(self.MODULE):
            if str(row.get("维度编码", "")) == dim_code and str(row.get("当前标记", "Y")) == "Y":
                current_row = row
                break
        if current_row is None:
            return {"dim_code": dim_code, "accepted": False, "reason": "维度当前版本不存在"}

        new_version = commit.get("new_version")
        current_row["当前版本号"] = new_version
        current_row["上一版本号"] = commit.get("old_version")
        current_row["版本生效时间"] = commit.get("effective_start", current_row.get("版本生效时间", ""))
        current_row["版本失效时间"] = commit.get("effective_end", current_row.get("版本失效时间", ""))
        current_row["生效时间"] = current_row["版本生效时间"]
        current_row["失效时间"] = current_row["版本失效时间"]
        current_row["当前标记"] = "Y"
        current_row["current_marker"] = "1"
        current_row["来源批次ID"] = commit.get("batch_id", "")
        current_row["来源策略"] = commit.get("strategy_name", "")
        attributes = commit.get("attributes") or {}
        if attributes:
            current_row["版本属性"] = "; ".join(f"{k}={v}" for k, v in attributes.items())

        store.channel.publish("dim.scd_version_updated", {
            "dim_code": dim_code,
            "old_version": commit.get("old_version"),
            "new_version": new_version,
            "batch_id": commit.get("batch_id"),
            "entry_id": current_row.get("id"),
        })
        return {
            "dim_code": dim_code,
            "accepted": True,
            "current_version": new_version,
            "current_row_id": current_row.get("id"),
        }

    def list_versions(self, dim_code: str) -> list[dict[str, Any]]:
        """返回某维度的全部版本记录，按版本号升序，供工作台逐条核对处理结果。

        尚未经历 SCD 提交的历史行没有版本字段，按 v1 当前版本补齐默认展示值，
        不修改原始数据。
        """
        rows = [
            row for row in store.rows(self.MODULE)
            if str(row.get("维度编码", "")) == dim_code
        ]
        normalized: list[dict[str, Any]] = []
        for row in rows:
            view = dict(row)
            view.setdefault("当前版本号", 1)
            view.setdefault("上一版本号", None)
            view.setdefault("当前标记", "Y")
            view.setdefault("current_marker", "1")
            view.setdefault("版本生效时间", view.get("生效时间", ""))
            view.setdefault("版本失效时间", view.get("失效时间", "9999-12-31 23:59:59"))
            view.setdefault("版本属性", "")
            view.setdefault("来源批次ID", "")
            view.setdefault("来源策略", "")
            normalized.append(view)
        normalized.sort(key=lambda r: int(r.get("当前版本号", 1) or 1))
        return normalized


def _auto_associate_attributes(dim_code: str, dim_entry_id: int) -> None:
    """自动关联维度属性：为已存在的属性匹配到新发布的维度。"""
    attr_rows = store.rows("dim_attr")
    for attr in attr_rows:
        if attr.get("所属维度") == dim_code:
            # 发送属性关联事件
            store.channel.publish("attr.linked", {
                "dim_code": dim_code,
                "attr_name": attr.get("属性名称"),
                "data_type": attr.get("数据类型"),
                "attr_id": attr.get("id"),
                "source_dim_id": dim_entry_id,
            })


def _auto_create_scd_strategy(dim_def: dict[str, Any]) -> None:
    """根据维度定义的SCD类型自动生成SCD处理策略建议。"""
    scd_type = dim_def.get("SCD类型", "SCD类型2")
    dim_code = dim_def.get("维度编码", "")
    strategy_name = f"{dim_code}_{scd_type.replace('类型', '')}_auto"

    # 检查是否已有同名策略
    existing = store.rows("scd_handler")
    for row in existing:
        if row.get("策略名称") == strategy_name:
            return

    # 发布SCD策略建议到通道
    store.channel.publish("scd.strategy_suggested", {
        "dim_code": dim_code,
        "scd_type": scd_type,
        "strategy_name": strategy_name,
        "dim_name": dim_def.get("维度名称"),
        "primary_key": dim_def.get("主键定义"),
        "source_dim_id": dim_def.get("id"),
    })