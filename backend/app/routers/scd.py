"""SCD 专属接口：属性变更批次提交、批次结果回查、维度版本与变更日志核对。"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException

from app.schemas import ScdBatchPayload
from app.services.dim_definition import DimDefinitionService
from app.services.scd_handler import ScdHandlerService
from app.services.slow_change_log import SlowChangeLogService
from app.store import store

router = APIRouter(prefix="/api/scd", tags=["SCD变更落库"])


@router.post("/strategies/{entry_id}/batch")
def submit_batch(entry_id: int, payload: ScdBatchPayload) -> dict[str, Any]:
    """工作台一次提交一组维度属性变更，返回带切换范围与逐维度结果的处理结果。"""
    outcome, message = ScdHandlerService().submit_changes(
        entry_id, payload.changes, batch_id=payload.batch_id,
    )
    if outcome is None:
        raise HTTPException(status_code=404, detail=message)
    outcome["message"] = message
    return outcome


@router.get("/batches/{batch_id}")
def get_batch(batch_id: str) -> dict[str, Any]:
    """回查同一批次的处理结果（成功/冲突/失败逐维度，与变更日志共用一个批次ID）。"""
    index = store.scd_commits.get(batch_id)
    if not index:
        raise HTTPException(status_code=404, detail=f"批次 {batch_id} 不存在")
    results = list(index.values())
    return {
        "batch_id": batch_id,
        "total": len(results),
        "switched_dimensions": [r["dim_code"] for r in results if r.get("switched")],
        "conflict_dimensions": [
            r["dim_code"] for r in results
            if not r.get("ok") and "版本冲突" in str(r.get("reason", ""))
        ],
        "failed_dimensions": [
            r["dim_code"] for r in results
            if not r.get("ok") and "版本冲突" not in str(r.get("reason", ""))
        ],
        "results": results,
    }


@router.get("/dimensions/{dim_code}/versions")
def list_dim_versions(dim_code: str) -> dict[str, Any]:
    """返回维度定义的版本记录链，便于核对当前版本与适用区间。"""
    versions = DimDefinitionService().list_versions(dim_code)
    if not versions:
        raise HTTPException(status_code=404, detail=f"维度 {dim_code} 无版本记录")
    return {"dim_code": dim_code, "total": len(versions), "versions": versions}


@router.get("/dimensions/{dim_code}/logs")
def list_dim_logs(dim_code: str) -> dict[str, Any]:
    """按维度回查变更日志；定位失败时可从日志中的策略名称回查原策略。"""
    logs = SlowChangeLogService().find_by_dimension(dim_code)
    return {"dim_code": dim_code, "total": len(logs), "logs": logs}


@router.get("/batches/{batch_id}/logs")
def list_batch_logs(batch_id: str) -> dict[str, Any]:
    """变更日志入口：按批次ID逐条核对实际写入的前后值与生效日期。"""
    logs = SlowChangeLogService().find_by_batch(batch_id)
    return {"batch_id": batch_id, "total": len(logs), "logs": logs}
