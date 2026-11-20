"""变更日志服务：记录维度属性每次变更的详细信息。

增强能力：
- 消费 attr.confirmed / dim.published 通道自动记录变更
- 变更追溯：生成变更前后对比报告
- 变更审计：按时间线生成变更链
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from app.services.base import BaseModuleService
from app.store import store


class SlowChangeLogService(BaseModuleService):
    MODULE = "slow_change_log"
    ENTITY = "维度变更"
    OPERATION_TERM = "归档"
    LIST_FIELDS = ["变更ID", "目标维度", "变更字段", "旧值", "新值", "变更时间", "变更原因"]
    REQUIRED_FIELDS = ["变更ID", "目标维度", "变更字段"]
    STATUS_ORDER = ["已记录", "已追溯", "已归档"]
    ACTION_RULES = {"追溯变更": "已记录", "归档记录": "已追溯"}
    NEGATIVE_ACTIONS = []

    def create_entry(self, values: dict[str, Any]) -> tuple[dict[str, Any] | None, list[str]]:
        # 变更日志只登记实际写入的前后值；SCD 提交的日志由 scd_versioning 引擎
        # 在确认落库成功后写入，这里不再从通道消息臆造旧值/新值。
        if not values.get("变更时间"):
            values["变更时间"] = datetime.now(timezone.utc).strftime("%Y-%m-%d")

        entry, missing = super().create_entry(values)
        if entry is None:
            return None, missing

        store.channel.publish("change_log.recorded", {
            "change_id": entry["变更ID"],
            "dim_code": entry.get("目标维度"),
            "field": entry["变更字段"],
            "entry_id": entry["id"],
        })

        store.artifacts.register(
            "slow_change_log", "change_record",
            f"change_{entry['id']}",
            {"change_id": entry["变更ID"], "dim_code": entry.get("目标维度")},
            ["dim_definition", "scd_handler", "slow_change_log"],
        )
        return entry, []

    def find_by_batch(self, batch_id: str) -> list[dict[str, Any]]:
        """按 SCD 批次ID回查该批实际写入的全部变更日志。"""
        return [
            row for row in store.rows(self.MODULE)
            if str(row.get("批次ID", "")) == batch_id
        ]

    def find_by_dimension(self, dim_code: str) -> list[dict[str, Any]]:
        """按维度回查变更链，定位失败时可据此找到原策略名再回查策略。"""
        return [
            row for row in store.rows(self.MODULE)
            if str(row.get("目标维度", "")) == dim_code
        ]

    def run_action(self, entry_id: int, action: str) -> tuple[dict[str, Any] | None, str]:
        entry, message = super().run_action(entry_id, action)
        if entry is None:
            return None, message

        if action == "追溯变更":
            trace = _trace_change_history(entry)
            store.channel.publish("change_log.traced", {
                "change_id": entry["变更ID"],
                "history": trace,
                "entry_id": entry_id,
            })
        elif action == "归档记录":
            store.channel.publish("change_log.archived", {
                "change_id": entry["变更ID"],
                "entry_id": entry_id,
            })
        return entry, message

    def domain_snapshot(self) -> dict[str, Any]:
        base = super().domain_snapshot()
        by_dim: dict[str, int] = {}
        for row in store.rows(self.MODULE):
            dim = row.get("目标维度", "未知")
            by_dim[dim] = by_dim.get(dim, 0) + 1
        base["changes_by_dimension"] = by_dim
        base["consumed_scd_results"] = len(store.channel.consume("scd.processing_result", "slow_change_log"))
        return base


def _trace_change_history(entry: dict[str, Any]) -> list[dict[str, Any]]:
    target_dim = entry.get("目标维度", "")
    related = []
    for row in store.rows("slow_change_log"):
        if row.get("目标维度") == target_dim and row.get("变更ID") != entry["变更ID"]:
            related.append({"change_id": row.get("变更ID"), "field": row.get("变更字段"),
                          "old_val": row.get("旧值"), "new_val": row.get("新值"),
                          "time": row.get("变更时间")})
    return related