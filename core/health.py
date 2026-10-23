"""数据库体检：只读统计主库大小、页占用和候选可回收空间。

体检只读取 `data_v4.db` 的文件大小与 SQLite 页统计，不执行 VACUUM、不删除
任何数据。删除行之后文件不会自动缩小，真正回收磁盘空间需要额外执行 VACUUM；
本插件不会自动做这件事，只会把估算值展示给用户。
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .db import JanitorDB
from .models import TABLE_KEYS
from .preview import TABLE_LABELS
from .sqlite_utils import readonly_uri


def human_bytes(value: int | float) -> str:
    """把字节数格式化成便于阅读的字符串。"""

    size = float(max(0, int(value or 0)))
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if size < 1024 or unit == "TB":
            if unit == "B":
                return f"{int(size)} B"
            return f"{size:.1f} {unit}"
        size /= 1024
    return f"{size:.1f} TB"


def _file_size(path: Path) -> int:
    try:
        return int(path.stat().st_size)
    except OSError:
        return 0


def _sqlite_stats(path: Path) -> dict[str, Any]:
    stats: dict[str, Any] = {
        "page_size": 0,
        "page_count": 0,
        "freelist_count": 0,
        "freelist_bytes": 0,
        "freelist_human": "0 B",
        "journal_mode": "",
    }
    if not path.is_file():
        return stats
    try:
        connection = sqlite3.connect(readonly_uri(path), uri=True, timeout=10)
    except sqlite3.Error:
        return stats
    try:
        for key in ("page_size", "page_count", "freelist_count"):
            try:
                row = connection.execute(f"pragma {key}").fetchone()
                if row is not None:
                    stats[key] = int(row[0] or 0)
            except (sqlite3.Error, TypeError, ValueError):
                continue
        try:
            row = connection.execute("pragma journal_mode").fetchone()
            if row is not None:
                stats["journal_mode"] = str(row[0] or "")
        except sqlite3.Error:
            pass
    finally:
        connection.close()
    stats["freelist_bytes"] = int(stats["page_size"]) * int(stats["freelist_count"])
    stats["freelist_human"] = human_bytes(stats["freelist_bytes"])
    return stats


def _scan_stats(scan: dict[str, Any] | None) -> dict[str, Any]:
    """从最近一次扫描结果里汇总候选行数，不重新读取数据库。"""

    stats: dict[str, Any] = {
        "available": bool(scan),
        "generated_at": (scan or {}).get("generated_at"),
        "candidates": 0,
        "default_targets": 0,
        "default_rows": 0,
        "optional_provider_rows": 0,
        "candidate_rows_total": 0,
        "status_counts": {
            "active": 0,
            "orphan_group": 0,
            "expired_temp": 0,
            "missing_friend": 0,
            "whitelisted": 0,
        },
        "rows_by_table": {key: 0 for key in TABLE_KEYS},
    }
    if not scan:
        return stats

    summary = scan.get("summary") or {}
    targets = [
        item for item in (scan.get("targets") or []) if item.get("selectable")
    ]
    stats["candidates"] = int(summary.get("candidates") or len(targets))
    for status in stats["status_counts"]:
        stats["status_counts"][status] = int(summary.get(status) or 0)

    for item in targets:
        counts = item.get("counts") or {}
        default_selected = bool(item.get("default_selected"))
        if default_selected:
            stats["default_targets"] += 1
        for table in TABLE_KEYS:
            rows = int(counts.get(table) or 0)
            if rows <= 0:
                continue
            stats["candidate_rows_total"] += rows
            if table == "provider_stats":
                stats["optional_provider_rows"] += rows
                continue
            stats["rows_by_table"][table] += rows
            if default_selected:
                stats["default_rows"] += rows
    return stats


def build_health(
    db_path: str | Path,
    scan: dict[str, Any] | None = None,
    inspect: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """生成一份只读数据库体检报告。"""

    path = Path(db_path)
    exists = path.is_file()
    size = _file_size(path)
    inspect = inspect if inspect is not None else JanitorDB(path).inspect()
    counts = inspect.get("counts") or {}

    tables: list[dict[str, Any]] = []
    core_rows = 0
    for key in TABLE_KEYS:
        rows = int(counts.get(key) or 0)
        if rows < 0:
            rows = 0
        core_rows += rows
        tables.append(
            {
                "table": key,
                "label": TABLE_LABELS.get(key, key),
                "rows": rows,
            }
        )

    sqlite_stats = _sqlite_stats(path)
    scan_stats = _scan_stats(scan)
    delete_rows = scan_stats["default_rows"] + scan_stats["optional_provider_rows"]

    def estimate(rows: int) -> int:
        if not size or not core_rows or rows <= 0:
            return 0
        ratio = min(1.0, float(rows) / float(core_rows))
        return int(size * ratio)

    estimated = estimate(delete_rows)
    estimated_default = estimate(scan_stats["default_rows"])

    warnings: list[str] = []
    if not exists:
        warnings.append("未找到 AstrBot 主库 data_v4.db，无法执行数据库体检")
    elif core_rows == 0:
        warnings.append("核心相关表当前没有数据")
    if not scan:
        warnings.append("尚未扫描；默认可清理行数、可选调用统计和可回收空间需要先扫描")
    elif scan_stats["candidates"] == 0:
        warnings.append("当前没有可清理候选")
    elif delete_rows == 0:
        warnings.append(
            f"候选 {scan_stats['candidates']} 个，但默认可清理 0 行："
            "它们只剩 provider_stats 调用统计；需要清理时勾选“包含 provider_stats”"
        )
    if exists:
        warnings.append(
            "SQLite 删除行后不会自动缩小文件；真正回收磁盘空间需要执行 VACUUM"
            "（插件不会自动执行）"
        )
    if sqlite_stats["freelist_bytes"]:
        warnings.append(
            f"数据库内部已有约 {sqlite_stats['freelist_human']} 空闲页，"
            "VACUUM 前会被新数据复用"
        )

    return {
        "ok": True,
        "readonly": True,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "file": {
            "path": str(path),
            "exists": exists,
            "size_bytes": size,
            "size_human": human_bytes(size),
            "wal_bytes": _file_size(Path(str(path) + "-wal")),
            "shm_bytes": _file_size(Path(str(path) + "-shm")),
        },
        "sqlite": sqlite_stats,
        "database": {
            "core_rows_total": core_rows,
            "tables": tables,
        },
        "scan": scan_stats,
        "estimate": {
            "delete_rows": delete_rows,
            "default_rows": scan_stats["default_rows"],
            "provider_rows": scan_stats["optional_provider_rows"],
            "estimated_reclaim_bytes": estimated,
            "estimated_reclaim_human": human_bytes(estimated),
            "estimated_reclaim_default_bytes": estimated_default,
            "estimated_reclaim_default_human": human_bytes(estimated_default),
            "method": "按候选行数占核心相关表总行数的比例粗略估算",
        },
        "vacuum": {
            "recommended": bool(delete_rows or sqlite_stats["freelist_bytes"]),
            "freelist_bytes": sqlite_stats["freelist_bytes"],
            "note": "删除行或清理备份后，可用 VACUUM 让 SQLite 文件真正缩小",
        },
        "warnings": warnings,
    }
