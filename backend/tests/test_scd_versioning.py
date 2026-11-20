"""SCD 属性变更落库测试：版本记录、适用区间、乐观锁、失败回滚与日志一致性。"""
from __future__ import annotations

from copy import deepcopy

import pytest

from app.store import Store
from app.store import store as global_store
from app.services.dim_definition import DimDefinitionService
from app.services.scd_handler import ScdHandlerService
from app.services.scd_versioning import (
    OPEN_END_TS,
    apply_scd_change,
    parse_detection_fields,
    submit_scd_batch,
)


def make_strategy(store: Store, name: str = "产品SCD2", dim: str = "DIM_PRODUCT",
                  scd_type: str = "SCD类型2") -> dict:
    strategy = {
        "id": 100, "策略名称": name, "目标维度": dim, "SCD类型": scd_type,
        "变化检测": "MD5(product_name, product_category)",
        "生效时间": "2026-01-01", "失效时间": "9999-12-31", "当前标记": "Y",
        "status": "草稿", "pending": True, "abnormal": False,
    }
    store.rows("scd_handler").append(strategy)
    return strategy


def reset_dim(store: Store, dim_code: str = "DIM_PRODUCT", scd_type: str = "SCD类型2") -> dict:
    """把种子维度重置为干净的 v1 当前版本。"""
    for row in store.rows("dim_definition"):
        if row.get("维度编码") == dim_code:
            row["当前版本号"] = 1
            row["上一版本号"] = None
            row["当前标记"] = "Y"
            row["current_marker"] = "1"
            row["版本生效时间"] = "2026-01-01 00:00:00"
            row["版本失效时间"] = OPEN_END_TS
            row["生效时间"] = "2026-01-01 00:00:00"
            row["失效时间"] = OPEN_END_TS
            row["SCD类型"] = scd_type
            row["product_name"] = "旧产品名"
            row["product_category"] = "食品"
            return row
    raise AssertionError(f"种子中未找到 {dim_code}")


@pytest.fixture
def store() -> Store:
    """领域服务绑定全局单例 store，测试前后快照恢复相关表与提交台账。"""
    saved_tables = {name: deepcopy(rows) for name, rows in global_store._tables.items()}
    saved_commits = deepcopy(global_store.scd_commits)
    yield global_store
    global_store._tables = saved_tables
    global_store.scd_commits = saved_commits


class TestVersionPersistence:
    def test_scd2_persists_attributes_into_new_version_row(self, store: Store) -> None:
        reset_dim(store)
        strategy = make_strategy(store)
        outcome = submit_scd_batch(strategy, [{
            "目标维度": "DIM_PRODUCT",
            "旧版本号": 1,
            "属性变更": {"product_name": "新产品名", "product_category": "饮料"},
            "生效日期": "2026-11-20",
        }])

        assert outcome["applied_count"] == 1
        versions = DimDefinitionService().list_versions("DIM_PRODUCT")
        current = [v for v in versions if v["当前标记"] == "Y"]
        assert len(current) == 1
        assert current[0]["当前版本号"] == 2
        assert current[0]["product_name"] == "新产品名"
        assert current[0]["product_category"] == "饮料"
        assert current[0]["版本属性"] == "product_name=新产品名; product_category=饮料"

    def test_old_and_new_windows_meet(self, store: Store) -> None:
        reset_dim(store)
        strategy = make_strategy(store)
        submit_scd_batch(strategy, [{
            "目标维度": "DIM_PRODUCT", "旧版本号": 1,
            "属性变更": {"product_name": "新产品名"}, "生效日期": "2026-11-20",
        }])
        versions = DimDefinitionService().list_versions("DIM_PRODUCT")
        old = next(v for v in versions if v["当前版本号"] == 1)
        new = next(v for v in versions if v["当前版本号"] == 2)
        assert old["版本失效时间"] == "2026-11-19 23:59:59"
        assert new["版本生效时间"] == "2026-11-20 00:00:00"
        assert new["版本失效时间"] == OPEN_END_TS
        assert old["当前标记"] == "N"
        assert new["当前标记"] == "Y"

    def test_dim_definition_receives_commit_and_updates_current(self, store: Store) -> None:
        reset_dim(store)
        strategy = make_strategy(store)
        outcome = submit_scd_batch(strategy, [{
            "目标维度": "DIM_PRODUCT", "旧版本号": 1,
            "属性变更": {"product_name": "新产品名"},
        }])
        received = DimDefinitionService().receive_scd_commit({
            "dim_code": "DIM_PRODUCT", "scd_type": "SCD类型2",
            "old_version": 1, "new_version": 2,
            "effective_start": "2026-11-20 00:00:00", "effective_end": OPEN_END_TS,
            "batch_id": outcome["batch_id"], "strategy_name": strategy["策略名称"],
            "attributes": {"product_name": "新产品名"},
            "current_row_id": 999,
        })
        assert received["accepted"] is True
        current = [v for v in DimDefinitionService().list_versions("DIM_PRODUCT")
                   if v["当前标记"] == "Y"][0]
        assert current["当前版本号"] == 2
        assert current["来源批次ID"] == outcome["batch_id"]

    def test_scd1_overwrites_without_new_row(self, store: Store) -> None:
        reset_dim(store, scd_type="SCD类型1")
        strategy = make_strategy(store, scd_type="SCD类型1")
        before = len(store.rows("dim_definition"))
        outcome = submit_scd_batch(strategy, [{
            "目标维度": "DIM_PRODUCT", "旧版本号": 1,
            "属性变更": {"product_name": "覆盖名"},
        }])
        assert outcome["applied_count"] == 1
        assert len(store.rows("dim_definition")) == before
        current = [v for v in DimDefinitionService().list_versions("DIM_PRODUCT")
                   if v["当前标记"] == "Y"][0]
        assert current["product_name"] == "覆盖名"
        assert current["当前版本号"] == 1

    def test_scd3_keeps_previous_value_column(self, store: Store) -> None:
        reset_dim(store, scd_type="SCD类型3")
        strategy = make_strategy(store, scd_type="SCD类型3")
        submit_scd_batch(strategy, [{
            "目标维度": "DIM_PRODUCT", "旧版本号": 1,
            "属性变更": {"product_name": "第三版名"},
        }])
        current = [v for v in DimDefinitionService().list_versions("DIM_PRODUCT")
                   if v["当前标记"] == "Y"][0]
        assert current["product_name"] == "第三版名"
        assert current["prev_product_name"] == "旧产品名"


class TestOptimisticLockAndPartialFailure:
    def test_stale_version_conflicts_without_any_write(self, store: Store) -> None:
        reset_dim(store)
        strategy = make_strategy(store)
        log_before = len(store.rows("slow_change_log"))
        dim_before = len(store.rows("dim_definition"))
        outcome = apply_scd_change(strategy, {
            "目标维度": "DIM_PRODUCT", "旧版本号": 7,
            "属性变更": {"product_name": "冲突名"},
        }, "BATCH-X")
        assert outcome["ok"] is False
        assert "版本冲突" in outcome["reason"]
        assert len(store.rows("dim_definition")) == dim_before
        assert len(store.rows("slow_change_log")) == log_before
        # 旧版本依然保持当前生效
        current = [v for v in DimDefinitionService().list_versions("DIM_PRODUCT")
                   if v["当前标记"] == "Y"][0]
        assert current["当前版本号"] == 1

    def test_batch_partial_failure_keeps_other_dimensions(self, store: Store) -> None:
        reset_dim(store, dim_code="DIM_PRODUCT")
        reset_dim(store, dim_code="DIM_SUPPLIER")
        strategy = make_strategy(store, dim="DIM_PRODUCT")
        outcome = submit_scd_batch(strategy, [
            {"目标维度": "DIM_PRODUCT", "旧版本号": 1,
             "属性变更": {"product_name": "正常切换"}, "生效日期": "2026-11-20"},
            {"目标维度": "DIM_SUPPLIER", "旧版本号": 9,
             "属性变更": {"supplier_grade": "A"}, "生效日期": "2026-11-20"},
        ])
        assert outcome["applied_count"] == 1
        assert outcome["conflict_count"] == 1
        assert outcome["switched_dimensions"] == ["DIM_PRODUCT"]
        assert outcome["conflict_dimensions"] == ["DIM_SUPPLIER"]
        # 冲突维度未被改动
        supplier = [v for v in DimDefinitionService().list_versions("DIM_SUPPLIER")
                    if v["当前标记"] == "Y"][0]
        assert supplier["当前版本号"] == 1
        assert supplier.get("supplier_grade") != "A"

    def test_missing_dimension_is_reported_not_raised(self, store: Store) -> None:
        strategy = make_strategy(store)
        outcome = apply_scd_change(strategy, {
            "目标维度": "DIM_NOT_EXIST", "旧版本号": 1,
            "属性变更": {"x": "y"},
        }, "BATCH-Y")
        assert outcome["ok"] is False
        assert "不存在" in outcome["reason"]


class TestWriteFailureRollback:
    def test_write_failure_leaves_no_effective_record_or_log(self, store: Store) -> None:
        reset_dim(store)
        strategy = make_strategy(store)
        dim_before = len(store.rows("dim_definition"))
        log_before = len(store.rows("slow_change_log"))
        outcome = apply_scd_change(strategy, {
            "目标维度": "DIM_PRODUCT", "旧版本号": 1,
            "属性变更": {"product_name": "回滚名"},
            "模拟写入失败": True,
        }, "BATCH-FAIL")
        assert outcome["ok"] is False
        assert "回滚" in outcome["reason"]
        assert len(store.rows("dim_definition")) == dim_before
        assert len(store.rows("slow_change_log")) == log_before
        current = [v for v in DimDefinitionService().list_versions("DIM_PRODUCT")
                   if v["当前标记"] == "Y"][0]
        assert current["product_name"] == "旧产品名"
        assert current["当前版本号"] == 1


class TestIdempotency:
    def test_duplicate_batch_replays_without_double_switch(self, store: Store) -> None:
        reset_dim(store)
        strategy = make_strategy(store)
        changes = [{"目标维度": "DIM_PRODUCT", "旧版本号": 1,
                    "属性变更": {"product_name": "第一次"}, "生效日期": "2026-11-20"}]
        first = submit_scd_batch(strategy, changes, batch_id="BATCH-DUP")
        # 第二次用同一批次ID提交，即便此时旧版本号已变成2也应回放而非冲突
        second = submit_scd_batch(strategy, changes, batch_id="BATCH-DUP")
        assert first["batch_id"] == second["batch_id"]
        versions = DimDefinitionService().list_versions("DIM_PRODUCT")
        assert len([v for v in versions if v["当前标记"] == "Y"]) == 1
        assert max(int(v["当前版本号"]) for v in versions) == 2
        logs = [r for r in store.rows("slow_change_log") if r.get("批次ID") == "BATCH-DUP"]
        assert len(logs) == 1
        assert logs[0]["新值"] == "第一次"


class TestChangeLog:
    def test_log_records_actual_before_after_and_effective_date(self, store: Store) -> None:
        reset_dim(store)
        strategy = make_strategy(store)
        submit_scd_batch(strategy, [{
            "目标维度": "DIM_PRODUCT", "旧版本号": 1,
            "属性变更": {"product_name": "新产品名", "product_category": "饮料"},
            "生效日期": "2026-11-20",
        }])
        logs = SlowChangeLogService_find(store, "DIM_PRODUCT")
        batch_logs = [r for r in logs if r.get("批次ID")]
        assert len(batch_logs) == 2
        name_log = next(r for r in batch_logs if r["变更字段"] == "product_name")
        assert name_log["旧值"] == "旧产品名"
        assert name_log["新值"] == "新产品名"
        assert name_log["变更时间"] == "2026-11-20"
        assert name_log["策略名称"] == strategy["策略名称"]

    def test_unchanged_attribute_writes_no_log_no_version(self, store: Store) -> None:
        reset_dim(store)
        strategy = make_strategy(store)
        log_before = len(store.rows("slow_change_log"))
        outcome = submit_scd_batch(strategy, [{
            "目标维度": "DIM_PRODUCT", "旧版本号": 1,
            "属性变更": {"product_name": "旧产品名"},
        }])
        assert outcome["applied_count"] == 0
        assert outcome["unchanged_count"] == 1
        assert len(store.rows("slow_change_log")) == log_before

    def test_failed_commit_writes_no_log(self, store: Store) -> None:
        reset_dim(store)
        strategy = make_strategy(store)
        log_before = len(store.rows("slow_change_log"))
        submit_scd_batch(strategy, [{
            "目标维度": "DIM_PRODUCT", "旧版本号": 3,
            "属性变更": {"product_name": "不该出现"},
        }])
        assert len(store.rows("slow_change_log")) == log_before


def SlowChangeLogService_find(store: Store, dim_code: str) -> list:
    from app.services.slow_change_log import SlowChangeLogService
    return SlowChangeLogService().find_by_dimension(dim_code)


class TestServiceIntegration:
    def test_scd_service_submit_returns_same_result_traceable_everywhere(self, store: Store) -> None:
        reset_dim(store)
        reset_dim(store, dim_code="DIM_SUPPLIER")
        strategy = make_strategy(store)
        # 工作台一次提交两组变更：第一组旧版本匹配，另一组冲突
        outcome, message = ScdHandlerService().submit_changes(strategy["id"], [
            {"目标维度": "DIM_PRODUCT", "旧版本号": 1,
             "属性变更": {"product_name": "工作台切换"}, "生效日期": "2026-11-20"},
            {"目标维度": "DIM_SUPPLIER", "旧版本号": 5,
             "属性变更": {"supplier_grade": "S"}, "生效日期": "2026-11-20"},
        ])
        assert message == "SCD变更批次已处理"
        assert outcome["switched_dimensions"] == ["DIM_PRODUCT"]
        assert outcome["conflict_dimensions"] == ["DIM_SUPPLIER"]

        # 变更日志入口按批次核对：只登记实际写入的那一组
        from app.services.slow_change_log import SlowChangeLogService
        batch_logs = SlowChangeLogService().find_by_batch(outcome["batch_id"])
        assert len(batch_logs) == 1
        assert batch_logs[0]["目标维度"] == "DIM_PRODUCT"

        # 维度版本与处理结果指向同一个新版本
        versions = DimDefinitionService().list_versions("DIM_PRODUCT")
        current = [v for v in versions if v["当前标记"] == "Y"][0]
        assert current["当前版本号"] == 2
        result_row = next(r for r in outcome["results"] if r["dim_code"] == "DIM_PRODUCT")
        assert result_row["new_version"] == current["当前版本号"]
        assert result_row["effective_start"] == current["版本生效时间"]

    def test_run_action_activation_with_changes_commits_version(self, store: Store) -> None:
        reset_dim(store)
        strategy = make_strategy(store)
        entry, message = ScdHandlerService().run_action(strategy["id"], "启用策略", {
            "changes": [{
                "目标维度": "DIM_PRODUCT", "旧版本号": 1,
                "属性变更": {"product_name": "启用即落库"},
            }],
        })
        assert entry is not None
        assert entry["id"] == strategy["id"]
        current = [v for v in DimDefinitionService().list_versions("DIM_PRODUCT")
                   if v["当前标记"] == "Y"][0]
        assert current["product_name"] == "启用即落库"
        assert current["当前版本号"] == 2

    def test_activation_without_changes_does_not_fabricate_log(self, store: Store) -> None:
        reset_dim(store)
        strategy = make_strategy(store)
        log_before = len(store.rows("slow_change_log"))
        ScdHandlerService().run_action(strategy["id"], "启用策略")
        assert len(store.rows("slow_change_log")) == log_before


class TestDetectionParsing:
    def test_parse_detection_fields(self) -> None:
        assert parse_detection_fields("MD5(product_name, product_category)") == [
            "product_name", "product_category"]
        assert parse_detection_fields("字段级比较: customer_level -- 保留旧值列") == [
            "customer_level"]
        assert parse_detection_fields("") == []
