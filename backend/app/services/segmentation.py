"""数据分层业务规则：状态流转、字段校验与筛选口径都收在这里。"""
from __future__ import annotations

from typing import Any

from app.store import store

MODULE = "segmentation"
STATUS_AVAILABLE = "可用"
LIST_FIELDS = ["切片编号", "源数据集", "切分维度", "切片粒度", "保留天数", "存储格式", "行数统计", "最后切分"]
REQUIRED_FIELDS = ["切片编号", "源数据集", "切分维度"]
STATUS_ORDER = ["待切分", "切分中", "可用", "已过期"]
ACTION_RULES = {"启动切分": "切分中", "标记可用": "可用", "标记过期": "已过期"}
NEGATIVE_ACTIONS = []


def _notify_coverage_board(entry: dict[str, Any], *, reason: str) -> None:
    """登记变更后通知覆盖看板抬高水位（延迟导入避免模块循环依赖）。"""
    from app.services.coverage_board import coverage_board_service

    coverage_board_service.notify_registry_changed(entry, reason=reason)


class SegmentationService:
    def list_entries(
        self,
        *,
        filters: dict[str, str] | None = None,
        keyword: str | None = None,
        status: str | None = None,
        page: int = 1,
        size: int = 20,
    ) -> tuple[list[dict[str, Any]], int]:
        rows = store.rows(MODULE)
        for field, expected in (filters or {}).items():
            rows = [row for row in rows if expected.casefold() in str(row.get(field, "")).casefold()]
        if keyword:
            rows = [row for row in rows if keyword.casefold() in str(row.get("切片编号", "")).casefold()]
        if status:
            rows = [row for row in rows if row.get("status") == status]
        total = len(rows)
        start = (page - 1) * size
        return rows[start:start + size], total

    def get_entry(self, entry_id: int) -> dict[str, Any] | None:
        return store.find(MODULE, entry_id)

    def create_entry(self, values: dict[str, Any]) -> tuple[dict[str, Any] | None, list[str]]:
        missing = [field for field in REQUIRED_FIELDS if not str(values.get(field) or "").strip()]
        if missing:
            return None, missing
        rows = store.rows(MODULE)
        entry = {"id": max((int(row.get("id", 0)) for row in rows), default=0) + 1}
        entry.update({field: values.get(field) for field in LIST_FIELDS})
        entry["status"] = STATUS_ORDER[0]
        entry["pending"] = True
        entry["abnormal"] = False
        rows.append(entry)
        _notify_coverage_board(entry, reason="登记新切片")
        return entry, []

    def run_action(self, entry_id: int, action: str) -> tuple[dict[str, Any] | None, str]:
        entry = store.find(MODULE, entry_id)
        if entry is None:
            return None, f"数据切片 {entry_id} 不存在或已归档"
        if action not in ACTION_RULES:
            return None, f"动作「{action}」不属于数据分层可执行范围"
        target = ACTION_RULES[action]
        if target not in STATUS_ORDER:
            return None, f"目标状态「{target}」不在允许的状态序列里"
        entry["status"] = target
        entry["pending"] = target != STATUS_ORDER[-1]
        entry["abnormal"] = action in NEGATIVE_ACTIONS
        _notify_coverage_board(entry, reason=f"切片动作「{action}」")
        return entry, f"数据切片已{action}"
