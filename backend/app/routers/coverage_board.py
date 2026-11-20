"""切片覆盖看板接口。

总览（dashboard）关联数据分层维护的可用切片清单汇总覆盖量；同批清单（manifest）
提供逐切片明细与每个源数据集的计数来源，支持打开核对与旧版本追溯。
对账会话在打开时钉选清单版本，会话内总览与清单小计始终同版本同口径。
"""
from __future__ import annotations

from fastapi import APIRouter, HTTPException, Query

from app.schemas import ActionResult
from app.services.coverage_board import coverage_board_service

router = APIRouter(prefix="/api/coverage-board", tags=["切片覆盖看板"])


@router.get("/dashboard")
def get_dashboard(
    version: int | None = Query(default=None, description="清单版本号；不传取最新，传旧版本用于追溯"),
) -> dict[str, object]:
    """覆盖看板总览：各源数据集可用行数、排除项与当前清单版本。

    登记未变更时命中物化缓存，不重新扫描登记；``served_from_cache`` 与
    ``registry_scan_count`` 可直接证明复用情况。
    """
    try:
        return coverage_board_service.get_dashboard(version)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.post("/manifests/publish", response_model=ActionResult)
def publish_manifest(
    remark: str | None = Query(default=None, description="本版清单备注，如「月初对账基线」"),
) -> ActionResult:
    """全量扫描一次登记并发布新版可用切片清单。"""
    manifest = coverage_board_service.publish_manifest(remark=remark)
    return ActionResult(
        ok=True,
        message=f"可用切片清单 v{manifest['manifest_version']} 已发布，可用量 {manifest['total_available_rows']} 行",
        entry={"manifest_version": manifest["manifest_version"],
               "total_available_rows": manifest["total_available_rows"]},
    )


@router.get("/manifests")
def list_manifests() -> dict[str, object]:
    """清单版本历史：对账后可在这里找到到期更正前后的各版摘要。"""
    items = coverage_board_service.list_manifests()
    return {"items": items, "total": len(items)}


@router.get("/manifests/{version}")
def get_manifest(version: int) -> dict[str, object]:
    """打开某一版同批清单核对：可用切片明细、排除项与源数据集计数来源。"""
    try:
        return coverage_board_service.get_manifest(version)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.get("/changes")
def list_changes() -> dict[str, object]:
    """登记变更留痕：到期更正等事件按时间顺序可追溯。"""
    items = coverage_board_service.list_changes()
    return {"items": items, "total": len(items)}


@router.post("/reconciliations", response_model=ActionResult)
def open_reconciliation(
    business_ref: str | None = Query(default=None, description="对账业务单号，如营业日报编号"),
) -> ActionResult:
    """打开对账会话并钉选当前清单版本；会话内总览不被期间的到期更正影响。"""
    view = coverage_board_service.open_reconciliation(business_ref=business_ref)
    return ActionResult(ok=True, message=f"对账会话已打开，钉选清单 v{view['pinned_version']}", entry=view)


@router.get("/reconciliations/{session_id}")
def get_reconciliation(session_id: str) -> dict[str, object]:
    """查询对账会话状态，含钉选版本与最新版本是否已经偏离。"""
    try:
        return coverage_board_service.get_session(session_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.get("/reconciliations/{session_id}/dashboard")
def get_reconciliation_dashboard(session_id: str) -> dict[str, object]:
    """对账中的总览：采用钉选版本的物化看板，保留更正前的完整营业日报数。"""
    try:
        return coverage_board_service.get_session_dashboard(session_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.get("/reconciliations/{session_id}/manifest")
def get_reconciliation_manifest(session_id: str) -> dict[str, object]:
    """对账中的同批清单：与总览同一版本、同一覆盖口径，小计与总量一致。"""
    try:
        return coverage_board_service.get_session_manifest(session_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.post("/reconciliations/{session_id}/close", response_model=ActionResult)
def close_reconciliation(session_id: str) -> ActionResult:
    """结束对账并释放版本钉选，之后查看立即采用更正版清单。"""
    try:
        view = coverage_board_service.close_reconciliation(session_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return ActionResult(ok=True, message="对账会话已结束，后续查看采用最新清单版本", entry=view)
