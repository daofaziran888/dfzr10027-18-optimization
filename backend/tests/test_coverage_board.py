"""切片覆盖看板测试。

覆盖需求主线：
1. 登记两条共 350 行的可用切片，发布清单并打开覆盖看板与同批明细；
2. 对账会话钉选版本后，把 50 行切片标记过期（到期更正），对账中总览仍是 350 行；
3. 结束对账后新一次查看采用更正版，可用量为 300 行；
4. 多次查看命中物化缓存，registry_scan_count 不增长，证明不再每次全量遍历；
5. 仅「可用且行数合法」的切片计入，已过期/待切分/行数非法单列，小计与总览同版同口径。
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.services.coverage_board import (
    STATUS_AVAILABLE,
    STATUS_EXPIRED,
    CoverageBoardService,
    coverage_board_service,
    parse_row_count,
)
from app.services.segmentation import SegmentationService
from app.store import store

MODULE = "segmentation"


@pytest.fixture(autouse=True)
def empty_segmentation_table():
    """覆盖看板用例只关心本用例登记的切片，先清掉种子数据里的示例切片。

    conftest 的 fresh_store 先重置 store 与看板，这里再清空切片登记表。
    """
    store.rows(MODULE).clear()
    coverage_board_service.reset()
    yield


def _register_available_slice(code: str, source: str, row_count: str) -> dict:
    """登记一条切片并直接流转到可用。"""
    service = SegmentationService()
    entry, missing = service.create_entry({
        "切片编号": code, "源数据集": source, "切分维度": "dt",
        "切片粒度": "日", "行数统计": row_count,
    })
    assert not missing
    service.run_action(entry["id"], "启动切分")
    entry, _ = service.run_action(entry["id"], "标记可用")
    assert entry["status"] == STATUS_AVAILABLE
    return entry


def _register_pending_slice(code: str, source: str, row_count: str) -> dict:
    service = SegmentationService()
    entry, missing = service.create_entry({
        "切片编号": code, "源数据集": source, "切分维度": "dt", "行数统计": row_count,
    })
    assert not missing
    return entry


class TestRowCountParsing:
    """行数合法性口径。"""

    @pytest.mark.parametrize(
        "raw,expected",
        [("350", 350), (300, 300), (0, 0), ("0", 0), ("", None), (None, None),
         ("-1", None), ("12.5", None), ("abc", None), (True, None), (" 50 ", 50)],
    )
    def test_parse_row_count(self, raw, expected):
        assert parse_row_count(raw) == expected


class TestManifestPublishAndCoverageRule:
    """清单发布、覆盖口径与同批清单小计。"""

    def test_two_available_slices_total_350(self):
        order_slice = _register_available_slice("SEGM-300", "order_events", "300")
        pv_slice = _register_available_slice("SEGM-50", "page_views", "50")
        # 再来一条待切分和一条行数非法的可用切片，验证单列与剔除
        _register_pending_slice("SEGM-PEND", "order_events", "999")
        invalid = _register_available_slice("SEGM-BAD", "experiment_results", "not-a-number")

        manifest = coverage_board_service.publish_manifest(remark="首次发布")

        assert manifest["manifest_version"] == 1
        assert manifest["total_available_rows"] == 350
        assert manifest["included_slice_count"] == 2
        included_ids = {item["entry_id"] for item in manifest["included_slices"]}
        assert included_ids == {order_slice["id"], pv_slice["id"]}

        # 各源数据集可用行数在同一视图展示，且带来源切片可追溯
        coverage = {item["源数据集"]: item for item in manifest["source_coverage"]}
        assert coverage["order_events"]["可用行数"] == 300
        assert coverage["page_views"]["可用行数"] == 50
        assert coverage["order_events"]["来源切片"][0]["切片编号"] == "SEGM-300"

        # 已过期/待切分/行数非法全部单列，不进可用量
        reasons = {item["entry_id"]: item["排除原因"] for item in manifest["excluded_slices"]}
        assert reasons  # 非空
        pending_entry = store.find(MODULE, [r for r in store.rows(MODULE) if r["切片编号"] == "SEGM-PEND"][0]["id"])
        assert reasons[pending_entry["id"]] == "待切分"
        assert reasons[invalid["id"]] == "行数非法"

        # 清单小计 == 总览总量，同一版本同一覆盖口径
        subtotal = sum(item["可用行数"] for item in manifest["source_coverage"])
        assert subtotal == manifest["total_available_rows"] == 350

    def test_expired_slices_listed_separately(self):
        _register_available_slice("SEGM-EXP-1", "order_events", "100")
        expired = _register_available_slice("SEGM-EXP-2", "page_views", "80")
        SegmentationService().run_action(expired["id"], "标记过期")

        manifest = coverage_board_service.publish_manifest()
        assert manifest["total_available_rows"] == 100
        excluded = {item["entry_id"]: item["排除原因"] for item in manifest["excluded_slices"]}
        assert excluded[expired["id"]] == STATUS_EXPIRED

    def test_dashboard_carries_manifest_trace_fields(self):
        _register_available_slice("SEGM-1", "order_events", "300")
        _register_available_slice("SEGM-2", "page_views", "50")
        dashboard = coverage_board_service.get_dashboard()
        assert dashboard["total_available_rows"] == 350
        assert dashboard["manifest_version"] == 1
        assert dashboard["coverage_rule"].startswith("仅纳入")
        assert dashboard["excluded_summary"]["待切分"] == 0
        # 看板里的源数据集计数与清单同源
        assert sum(item["可用行数"] for item in dashboard["source_coverage"]) == 350


class TestCachedDashboardAvoidsRescan:
    """物化看板：多次查看不重新遍历登记。"""

    def test_repeated_views_hit_cache(self):
        _register_available_slice("SEGM-1", "order_events", "300")
        _register_available_slice("SEGM-2", "page_views", "50")

        first = coverage_board_service.get_dashboard()
        assert first["served_from_cache"] is False  # 首次查看需要扫描一次
        scans_after_first = coverage_board_service._scan_count
        assert scans_after_first == 1

        for _ in range(5):
            cached = coverage_board_service.get_dashboard()
            assert cached["served_from_cache"] is True
            assert cached["total_available_rows"] == 350
        # 连续 5 次查看，扫描次数完全不增长
        assert coverage_board_service._scan_count == scans_after_first

    def test_explicit_old_version_never_rescans(self):
        _register_available_slice("SEGM-1", "order_events", "350")
        coverage_board_service.publish_manifest()  # v1
        SegmentationService().run_action(
            next(r for r in store.rows(MODULE) if r["切片编号"] == "SEGM-1")["id"], "标记过期")
        coverage_board_service.get_dashboard()  # 触发 v2
        scans = coverage_board_service._scan_count

        old = coverage_board_service.get_dashboard(version=1)
        assert old["manifest_version"] == 1
        assert old["total_available_rows"] == 350  # 旧版计数仍可追溯
        assert old["served_from_cache"] is True
        assert coverage_board_service._scan_count == scans  # 追溯旧版零扫描

    def test_change_then_view_scans_once(self):
        _register_available_slice("SEGM-1", "order_events", "350")
        coverage_board_service.get_dashboard()
        assert coverage_board_service._scan_count == 1

        # 与登记无关的重复查看不扫描
        coverage_board_service.get_dashboard()
        assert coverage_board_service._scan_count == 1

        target = next(r for r in store.rows(MODULE) if r["切片编号"] == "SEGM-1")
        SegmentationService().run_action(target["id"], "标记过期")
        refreshed = coverage_board_service.get_dashboard()  # 水位变化，重新扫描一次
        assert refreshed["served_from_cache"] is False
        assert coverage_board_service._scan_count == 2
        # 再看又回到缓存
        assert coverage_board_service.get_dashboard()["served_from_cache"] is True
        assert coverage_board_service._scan_count == 2


class TestReconciliationVersionPinning:
    """对账期间的到期更正：钉选版本保留完整营业日报数，新版采用更正版。"""

    def test_reconciliation_keeps_pinned_version_then_adopts_correction(self):
        big = _register_available_slice("SEGM-300", "order_events", "300")
        small = _register_available_slice("SEGM-50", "page_views", "50")

        # 打开覆盖看板与同批清单：基线 350 行
        baseline = coverage_board_service.get_dashboard()
        assert baseline["total_available_rows"] == 350
        baseline_manifest = coverage_board_service.get_manifest(baseline["manifest_version"])
        assert baseline_manifest["total_available_rows"] == 350

        # 对账开始，钉选 v1
        session = coverage_board_service.open_reconciliation(business_ref="DAILY-REPORT-20261120")
        pinned = session["pinned_version"]
        assert pinned == 1

        # 对账中收到一笔到期更正：50 行切片改为已过期
        SegmentationService().run_action(small["id"], "标记过期")
        # 到期更正作为变更事件留痕，可追溯
        changes = coverage_board_service.list_changes()
        assert any(event["entry_id"] == small["id"] and "标记过期" in event["reason"] for event in changes)

        # 正在对账的总览仍采用钉选的 v1：完整营业日报数 350 行
        recon_dashboard = coverage_board_service.get_session_dashboard(session["session_id"])
        assert recon_dashboard["pinned_version"] == 1
        assert recon_dashboard["manifest_version"] == 1
        assert recon_dashboard["total_available_rows"] == 350
        # 新版清单懒发布，此时尚未生成 v2，但水位已领先，会话明确感知到更正存在
        assert recon_dashboard["version_stale"] is True
        assert recon_dashboard["latest_version"] == 1
        assert recon_dashboard["registry_watermark"] > recon_dashboard["pinned_watermark"]

        # 对账内同批清单与总览同版同口径，小计仍是 350
        recon_manifest = coverage_board_service.get_session_manifest(session["session_id"])
        assert recon_manifest["manifest_version"] == 1
        assert sum(item["可用行数"] for item in recon_manifest["source_coverage"]) == 350

        # 同时，新一次查看（会话外）立即采用更正版：可用量 300 行
        latest = coverage_board_service.get_dashboard()
        assert latest["manifest_version"] == 2
        assert latest["total_available_rows"] == 300
        assert {item["源数据集"]: item["可用行数"] for item in latest["source_coverage"]} == {"order_events": 300}
        # 旧版清单仍能逐源追溯计数来源
        old = coverage_board_service.get_manifest(1)
        old_sources = {item["源数据集"]: item["可用行数"] for item in old["source_coverage"]}
        assert old_sources == {"order_events": 300, "page_views": 50}

        # 结束对账后会话视图确认释放钉选
        closed = coverage_board_service.close_reconciliation(session["session_id"])
        assert closed["closed_at"] is not None
        assert coverage_board_service.get_dashboard()["total_available_rows"] == 300

    def test_closed_session_cannot_be_used(self):
        _register_available_slice("SEGM-1", "order_events", "10")
        session = coverage_board_service.open_reconciliation()
        coverage_board_service.close_reconciliation(session["session_id"])
        with pytest.raises(KeyError):
            coverage_board_service.get_session_dashboard(session["session_id"])

    def test_unknown_manifest_version_raises(self):
        with pytest.raises(KeyError):
            coverage_board_service.get_manifest(99)


class TestCoverageBoardAPI:
    """接口层冒烟：确认路由注册与端到端数据流。"""

    def test_full_flow_via_http(self):
        client = TestClient(app)
        seg = SegmentationService()
        for code, source, rows in [("SEGM-300", "order_events", "300"), ("SEGM-50", "page_views", "50")]:
            entry, _ = seg.create_entry(
                {"切片编号": code, "源数据集": source, "切分维度": "dt", "行数统计": rows})
            seg.run_action(entry["id"], "启动切分")
            seg.run_action(entry["id"], "标记可用")

        # 看板
        resp = client.get("/api/coverage-board/dashboard")
        assert resp.status_code == 200
        assert resp.json()["total_available_rows"] == 350

        # 同批清单明细
        detail = client.get("/api/coverage-board/manifests/1")
        assert detail.status_code == 200
        assert len(detail.json()["included_slices"]) == 2

        # 打开对账并钉选
        opened = client.post("/api/coverage-board/reconciliations?business_ref=DAILY-1")
        session_id = opened.json()["entry"]["session_id"]

        small_id = next(r["id"] for r in store.rows(MODULE) if r["切片编号"] == "SEGM-50")
        client.post(f"/api/segmentation/{small_id}/actions", json={"action": "标记过期"})

        # 对账总览仍是 350
        recon = client.get(f"/api/coverage-board/reconciliations/{session_id}/dashboard")
        assert recon.json()["total_available_rows"] == 350
        assert recon.json()["version_stale"] is True
        # 会话外新看板为 300，且为缓存命中后不再增加扫描
        before_scan = client.get("/api/coverage-board/dashboard").json()["registry_scan_count"]
        again = client.get("/api/coverage-board/dashboard")
        assert again.json()["total_available_rows"] == 300
        assert again.json()["registry_scan_count"] == before_scan
        assert again.json()["served_from_cache"] is True

        # 版本历史与变更留痕
        history = client.get("/api/coverage-board/manifests").json()
        assert [item["manifest_version"] for item in history["items"]] == [1, 2]
        assert client.get("/api/coverage-board/changes").json()["total"] >= 1

        # 404 行为
        assert client.get("/api/coverage-board/manifests/99").status_code == 404
