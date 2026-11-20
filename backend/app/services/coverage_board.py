"""切片覆盖看板：把切片登记聚合为带版本的可用切片清单与物化看板。

设计要点
--------
1. 覆盖口径只有一个出口 :func:`is_coverage_eligible`：状态为「可用」且行数合法
   （非负整数）的切片才计入可用量；已过期、待切分/切分中、行数非法的数据单列。
2. 每次发布清单（manifest）只全量扫描一次登记，聚合结果连同每个源数据集的计数
   来源（逐切片快照）一起固化为不可变版本；旧版本永久保留，供对账追溯。
3. 看板是清单的物化视图：同一版本重复查看直接命中缓存，不再遍历登记，
   ``registry_scan_count`` 与 ``served_from_cache`` 用来证明这一点。
4. 对账会话打开时钉选当前清单版本。期间登记发生到期更正只会产生新版本，
   会话内总览与清单小计始终使用钉选版本；新一次查看（或关闭会话）才采用新版。
"""
from __future__ import annotations

from datetime import datetime
from typing import Any

from app.services.segmentation import MODULE, STATUS_AVAILABLE
from app.store import store

# 不计入可用量、需要在清单中单列的状态。
STATUS_PENDING = "待切分"
STATUS_RUNNING = "切分中"
STATUS_EXPIRED = "已过期"
NON_COVERED_STATUSES = (STATUS_PENDING, STATUS_RUNNING, STATUS_EXPIRED)


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def parse_row_count(raw: Any) -> int | None:
    """把登记里的「行数统计」解析成合法行数；无法解析或为负数时返回 ``None``。

    登记字段在页面上是文本（如 ``"125000"``），这里统一收口校验，
    空串、``None``、小数、带杂字符的内容都视为行数非法。
    """
    if isinstance(raw, bool):  # bool 是 int 的子类，显式排除，避免 True 被当成 1
        return None
    if isinstance(raw, int):
        return raw if raw >= 0 else None
    text = str(raw or "").strip()
    if not text:
        return None
    if not text.isdigit():  # 只接受非负整数字面量，拒绝 "-1"、"1.5"、"abc"
        return None
    return int(text)


def is_coverage_eligible(entry: dict[str, Any]) -> bool:
    """唯一的覆盖口径：可用状态且行数合法（非负整数）的切片才纳入可用量。"""
    return entry.get("status") == STATUS_AVAILABLE and parse_row_count(entry.get("行数统计")) is not None


class CoverageBoardService:
    """维护版本化可用切片清单、物化看板与对账会话。"""

    def __init__(self) -> None:
        self.reset()

    # ------------------------------------------------------------------ 状态
    def reset(self) -> None:
        """清空清单历史、看板缓存与对账会话；与 store 重置联动，供测试隔离。"""
        self._manifests: list[dict[str, Any]] = []
        self._dashboards: dict[int, dict[str, Any]] = {}
        self._sessions: dict[str, dict[str, Any]] = {}
        self._scan_count = 0          # 全量遍历切片登记的累计次数
        self._registry_watermark = 0  # 登记变更水位：变更一次加一
        self._changes: list[dict[str, Any]] = []
        self._session_seq = 0

    # ------------------------------------------------------------ 登记变更事件
    def notify_registry_changed(self, entry: dict[str, Any] | None = None, *, reason: str = "登记变更") -> None:
        """切片登记发生状态/行数变化时调用：抬高水位使旧看板缓存失效并留痕。

        清单本身是不可变快照，不会被原地改写；这里只让“最新看板”的缓存失效，
        下一次查看会基于当前登记发布新版本，旧版本仍可被对账与追溯读取。
        """
        self._registry_watermark += 1
        event = {
            "seq": self._registry_watermark,
            "reason": reason,
            "changed_at": _now(),
            "entry_id": None if entry is None else entry.get("id"),
            "切片编号": None if entry is None else entry.get("切片编号"),
            "status": None if entry is None else entry.get("status"),
            "行数统计": None if entry is None else entry.get("行数统计"),
        }
        self._changes.append(event)

    def list_changes(self) -> list[dict[str, Any]]:
        return [dict(event) for event in self._changes]

    # ------------------------------------------------------------ 清单发布（扫描）
    def publish_manifest(self, *, remark: str | None = None) -> dict[str, Any]:
        """全量扫描一次切片登记，发布一版不可变的可用切片清单并物化看板。"""
        entries = store.rows(MODULE)
        self._scan_count += 1  # 一次发布 = 一次全量遍历
        scanned_at = _now()

        included: list[dict[str, Any]] = []
        excluded: list[dict[str, Any]] = []
        # 源数据集 -> 计数来源，保证同视图能展示各源数据集的可用行数
        per_source: dict[str, dict[str, Any]] = {}

        for entry in entries:
            row_count = parse_row_count(entry.get("行数统计"))
            status = entry.get("status")
            snapshot = {
                "entry_id": entry.get("id"),
                "切片编号": entry.get("切片编号"),
                "源数据集": entry.get("源数据集"),
                "切分维度": entry.get("切分维度"),
                "切片粒度": entry.get("切片粒度"),
                "status": status,
                "行数统计": entry.get("行数统计"),
                "row_count": row_count,
            }
            if is_coverage_eligible(entry):
                included.append(snapshot)
                source = str(entry.get("源数据集") or "未命名源数据集")
                bucket = per_source.setdefault(
                    source, {"源数据集": source, "可用行数": 0, "切片数": 0, "来源切片": []})
                bucket["可用行数"] += row_count or 0
                bucket["切片数"] += 1
                bucket["来源切片"].append({
                    "entry_id": entry.get("id"),
                    "切片编号": entry.get("切片编号"),
                    "row_count": row_count,
                })
            else:
                if status == STATUS_AVAILABLE:
                    reason = "行数非法"  # 可用但行数不合法，按口径剔除并单列
                elif status == STATUS_EXPIRED:
                    reason = "已过期"
                elif status in (STATUS_PENDING, STATUS_RUNNING):
                    reason = "待切分"
                else:
                    reason = f"状态非可用（{status}）"
                snapshot = {**snapshot, "排除原因": reason}
                excluded.append(snapshot)

        included.sort(key=lambda item: (str(item["源数据集"]), int(item["entry_id"] or 0)))
        excluded.sort(key=lambda item: (item["排除原因"], int(item["entry_id"] or 0)))
        source_cov = sorted(per_source.values(), key=lambda item: item["源数据集"])

        version = len(self._manifests) + 1
        total_available_rows = sum(item["可用行数"] for item in source_cov)
        manifest = {
            "manifest_version": version,
            "published_at": scanned_at,
            "remark": remark,
            "coverage_rule": "仅纳入状态=可用且行数统计为非负整数的切片",
            "registry_watermark": self._registry_watermark,
            "total_available_rows": total_available_rows,
            "included_slice_count": len(included),
            "excluded_slice_count": len(excluded),
            "included_slices": included,          # 同批清单：可用切片明细
            "excluded_slices": excluded,          # 已过期/待切分/行数非法单列
            "source_coverage": source_cov,        # 每个源数据集的计数来源
        }
        # 小计必须等于总览：源数据集小计求和即清单总量，同一版本同一口径
        assert sum(item["可用行数"] for item in source_cov) == total_available_rows
        self._manifests.append(manifest)
        self._dashboards[version] = self._build_dashboard(manifest, scanned=False)
        return manifest

    def _build_dashboard(self, manifest: dict[str, Any], *, scanned: bool) -> dict[str, Any]:
        """把清单物化成看板视图；看板不重算，只搬运清单已经固化的数字。"""
        expired = [item for item in manifest["excluded_slices"] if item["排除原因"] == "已过期"]
        pending = [item for item in manifest["excluded_slices"] if item["排除原因"] == "待切分"]
        invalid = [item for item in manifest["excluded_slices"] if item["排除原因"] == "行数非法"]
        return {
            "manifest_version": manifest["manifest_version"],
            "published_at": manifest["published_at"],
            "remark": manifest.get("remark"),
            "coverage_rule": manifest["coverage_rule"],
            "total_available_rows": manifest["total_available_rows"],
            "included_slice_count": manifest["included_slice_count"],
            "source_coverage": manifest["source_coverage"],
            "excluded_summary": {
                "已过期": len(expired),
                "待切分": len(pending),
                "行数非法": len(invalid),
            },
            "excluded_slices": manifest["excluded_slices"],
            "registry_watermark": manifest["registry_watermark"],
            "served_from_cache": not scanned,
            "registry_scan_count": self._scan_count,
        }

    # ------------------------------------------------------------ 看板读取（缓存）
    def get_dashboard(self, version: int | None = None) -> dict[str, Any]:
        """查看覆盖看板。

        - 不传版本：返回最新看板；登记有变更才发布新版本（触发一次扫描），
          否则直接返回物化缓存，不遍历登记。
        - 指定版本：只读取该版本缓存，任何情况下都不重新扫描。
        """
        if version is not None:
            manifest = self.get_manifest(version)
            cached = self._dashboards[version]
            # 显式追溯旧版本：复制一份并标注为缓存命中，计数保持不变
            return {**cached, "served_from_cache": True, "registry_scan_count": self._scan_count}

        if self._manifests and self._manifests[-1]["registry_watermark"] == self._registry_watermark:
            latest_version = self._manifests[-1]["manifest_version"]
            return {**self._dashboards[latest_version],
                    "served_from_cache": True,
                    "registry_scan_count": self._scan_count}
        # 水位领先最新清单：登记发生过变更，需要发布新版（此处发生一次全量扫描）
        manifest = self.publish_manifest()
        return {**self._dashboards[manifest["manifest_version"]],
                "served_from_cache": False,
                "registry_scan_count": self._scan_count}

    def get_manifest(self, version: int) -> dict[str, Any]:
        """读取指定版本的完整清单（含逐切片计数来源）；版本不存在要明确报错。"""
        for manifest in self._manifests:
            if manifest["manifest_version"] == version:
                return manifest
        raise KeyError(f"清单版本 v{version} 不存在")

    def list_manifests(self) -> list[dict[str, Any]]:
        """清单版本历史：只返回摘要，明细通过 get_manifest 追溯。"""
        return [{
            "manifest_version": item["manifest_version"],
            "published_at": item["published_at"],
            "remark": item.get("remark"),
            "registry_watermark": item["registry_watermark"],
            "total_available_rows": item["total_available_rows"],
            "included_slice_count": item["included_slice_count"],
            "excluded_slice_count": item["excluded_slice_count"],
        } for item in self._manifests]

    # ------------------------------------------------------------ 对账会话（版本钉选）
    def open_reconciliation(self, *, business_ref: str | None = None) -> dict[str, Any]:
        """打开对账会话：钉选发布时刻的最新清单版本，会话内一直沿用该版本。"""
        if not self._manifests or self._manifests[-1]["registry_watermark"] != self._registry_watermark:
            self.publish_manifest(remark="对账开始时发布的基线清单")
        pinned = self._manifests[-1]["manifest_version"]
        self._session_seq += 1
        session_id = f"RECON-{self._session_seq:04d}"
        session = {
            "session_id": session_id,
            "business_ref": business_ref,
            "pinned_version": pinned,
            "opened_at": _now(),
            "closed_at": None,
        }
        self._sessions[session_id] = session
        return self._session_view(session_id)

    def _require_session(self, session_id: str) -> dict[str, Any]:
        session = self._sessions.get(session_id)
        if session is None:
            raise KeyError(f"对账会话 {session_id} 不存在")
        if session.get("closed_at"):
            raise KeyError(f"对账会话 {session_id} 已结束")
        return session

    def _session_view(self, session_id: str) -> dict[str, Any]:
        session = self._sessions[session_id]
        pinned = session["pinned_version"]
        latest = self._manifests[-1]["manifest_version"] if self._manifests else None
        # 新版清单是懒发布的：即使 v2 还没生成，只要登记水位领先钉选清单，
        # 就说明期间发生过到期更正，会话钉选版本已经偏离当前登记。
        pinned_watermark = self.get_manifest(pinned)["registry_watermark"]
        watermark_stale = pinned_watermark != self._registry_watermark
        return {
            **session,
            "latest_version": latest,
            "registry_watermark": self._registry_watermark,
            "pinned_watermark": pinned_watermark,
            "version_stale": latest != pinned or watermark_stale,
        }

    def get_session(self, session_id: str) -> dict[str, Any]:
        self._require_session(session_id)
        return self._session_view(session_id)

    def get_session_dashboard(self, session_id: str) -> dict[str, Any]:
        """对账中的总览：始终读钉选版本缓存，不扫描、不被到期更正影响。"""
        session = self._require_session(session_id)
        view = self._session_view(session_id)
        dashboard = self.get_dashboard(session["pinned_version"])
        return {
            **dashboard,
            "reconciliation_session_id": session_id,
            "pinned_version": session["pinned_version"],
            "latest_version": view["latest_version"],
            "registry_watermark": view["registry_watermark"],
            "pinned_watermark": view["pinned_watermark"],
            "version_stale": view["version_stale"],
        }

    def get_session_manifest(self, session_id: str) -> dict[str, Any]:
        """对账中的同批清单：与总览同版本同口径，小计必然对得上总览总量。"""
        session = self._require_session(session_id)
        return self.get_manifest(session["pinned_version"])

    def close_reconciliation(self, session_id: str) -> dict[str, Any]:
        """结束对账：释放钉选，之后查看自动采用最新更正版清单。"""
        session = self._require_session(session_id)
        session["closed_at"] = _now()
        return self._session_view(session_id)

    def list_sessions(self) -> list[dict[str, Any]]:
        return [self._session_view(session_id) for session_id in sorted(self._sessions)]


coverage_board_service = CoverageBoardService()
