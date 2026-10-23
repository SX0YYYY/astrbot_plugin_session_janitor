"""核心库清理与回滚（V0.2，V0.7.1 加固）。

清理前先生成整库快照和逐行 JSON 备份；删除在单事务内完成。
自动回滚使用逐行 JSON，不直接替换运行中的 data_v4.db。

V0.7.1 加固：
- 回滚前检测同主键冲突，默认不覆盖；支持 skip / overwrite。
- 缺 rows.json、库文件缺失、备份状态异常时中止。
- 回滚前安全快照失败即中止。
- 删除量达到阈值时在提交前执行 quick_check。
- 只允许回滚五张核心表，并校验列名。
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from .backup import BackupBundle, BackupStore
from .models import CORE_TABLES
from .sqlite_utils import readonly_uri, write_uri

ProgressCallback = Callable[[str, int, str], None]

# 删除量达到该阈值时，在 commit 前执行 quick_check；小清理跳过，兼顾安全与体感。
QUICK_CHECK_THRESHOLD = 500
CONFLICT_MODES = {"skip", "overwrite"}


class RollbackConflictError(ValueError):
    """回滚会覆盖同主键新数据；调用方需要让用户选择 skip / overwrite。"""

    def __init__(self, conflicts: dict[str, int], message: str | None = None):
        self.conflicts = dict(conflicts or {})
        super().__init__(
            message
            or "回滚会覆盖同主键的新数据，请选择跳过冲突或强制覆盖"
        )


@dataclass
class CleanResult:
    backup_id: str
    targets: list[str]
    counts: dict[str, int] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)
    bundle: BackupBundle | None = field(default=None, repr=False)

    @property
    def deleted(self) -> int:
        return sum(self.counts.values())

    def to_dict(self) -> dict[str, Any]:
        return {
            "backup_id": self.backup_id,
            "targets": self.targets,
            "counts": self.counts,
            "deleted": self.deleted,
            "warnings": self.warnings,
        }


class CoreCleaner:
    def __init__(self, db_path: str | Path, backup_store: BackupStore):
        self.db_path = Path(db_path)
        self.backup_store = backup_store

    def clean(
        self,
        scan: dict[str, Any],
        selected_umos: list[str],
        include_provider_stats: bool = False,
        retain: int = 5,
        finalize: bool = True,
        progress: ProgressCallback | None = None,
    ) -> CleanResult:
        if not self.db_path.is_file():
            raise RuntimeError("未找到 AstrBot 主库 data_v4.db，已中止清理")
        targets, invalid = self._validate_targets(scan, selected_umos)
        warnings: list[str] = []
        if invalid:
            warnings.append(
                "以下会话已失效或不再可清理，已跳过："
                + "、".join(invalid[:10])
                + (" 等" if len(invalid) > 10 else "")
            )
        bundle = self.backup_store.create("core", list(targets))
        counts: dict[str, int] = {table: 0 for table in CORE_TABLES}

        _emit(progress, "生成整库快照", 5, "正在复制 data_v4.db")
        try:
            bundle.snapshot_db(self.db_path)
        except Exception as exc:
            message = f"整库快照失败，已中止清理：{exc}"
            bundle.manifest["status"] = "failed"
            bundle.manifest["error"] = message
            try:
                bundle.save()
            except Exception:
                pass
            raise RuntimeError(message) from exc
        _emit(progress, "备份并删除数据", 15, f"共 {len(targets)} 个会话")

        try:
            connection = self._connect(write=True)
        except Exception as exc:
            message = f"打开数据库失败，已中止清理：{exc}"
            bundle.manifest["status"] = "failed"
            bundle.manifest["error"] = message
            try:
                bundle.save()
            except Exception:
                pass
            raise RuntimeError(message) from exc

        try:
            connection.execute("BEGIN IMMEDIATE")
            operations = self._operations(list(targets.values()), include_provider_stats)
            total = max(1, len(operations))
            step = max(1, total // 20)
            for index, (table, where, params) in enumerate(operations, 1):
                columns, rows = self._select(connection, table, where, params)
                if rows:
                    bundle.add_rows(table, columns, rows)
                    deleted = self._delete(connection, table, where, params)
                    counts[table] = counts.get(table, 0) + deleted
                if index == 1 or index % step == 0 or index == total:
                    _emit(
                        progress,
                        "备份并删除数据",
                        15 + int(75 * index / total),
                        f"正在处理 {table}（{index}/{total}）",
                    )

            # 先落盘备份，再校验，最后提交删除；即使进程在提交前退出，也有可回滚数据。
            _emit(progress, "写入备份清单", 92, "正在保存逐行备份")
            bundle.manifest["status"] = "pending"
            bundle.manifest["counts"] = counts

            deleted_total = sum(counts.values())
            if deleted_total >= QUICK_CHECK_THRESHOLD:
                _emit(progress, "校验数据库完整性", 94, "正在执行 quick_check")
                check = self._quick_check(connection)
                bundle.manifest["quick_check"] = check or "unknown"
                if str(check).strip().lower() != "ok":
                    raise RuntimeError(
                        "数据库 quick_check 未通过，已回滚，未删除任何数据："
                        + str(check or "unknown")[:200]
                    )
            else:
                bundle.manifest["quick_check"] = "skipped"

            bundle.save()
            connection.commit()
        except Exception as exc:
            connection.rollback()
            bundle.manifest["status"] = "failed"
            bundle.manifest["error"] = str(exc)
            try:
                bundle.save()
            except Exception:
                pass
            raise
        finally:
            connection.close()

        bundle.manifest["status"] = "completed"
        bundle.manifest["counts"] = counts
        bundle.save()
        if finalize:
            self.backup_store.prune(retain, keep={bundle.backup_id})
        _emit(progress, "清理完成", 100, f"共删除 {sum(counts.values())} 行")
        return CleanResult(
            backup_id=bundle.backup_id,
            targets=list(targets),
            counts={key: value for key, value in counts.items() if value},
            warnings=warnings,
            bundle=bundle,
        )

    def rollback(
        self,
        backup_id: str,
        on_conflict: str | None = None,
    ) -> dict[str, Any]:
        if on_conflict is not None and on_conflict not in CONFLICT_MODES:
            raise ValueError("冲突处理方式只能是 skip 或 overwrite")
        if not self.db_path.is_file():
            raise FileNotFoundError("AstrBot 主库 data_v4.db 不存在，已中止回滚")

        manifest, rows = self.backup_store.load(backup_id)
        if manifest.get("mode") == "safety":
            raise ValueError("这是回滚前的整库快照，不能通过逐行回滚恢复")
        status = manifest.get("status")
        if status == "restored":
            raise ValueError("该备份已经回滚过，不能重复回滚")
        if status == "failed":
            raise ValueError("该备份生成失败，不能用于回滚")
        if status not in {"completed", "pending"}:
            raise ValueError("该备份当前状态不可回滚")

        self._validate_backup_rows(manifest, rows)
        conflicts, conflict_keys, pk_columns = self._find_conflicts(rows)
        if conflicts and on_conflict is None:
            raise RollbackConflictError(conflicts)

        filtered_rows = rows
        skipped_conflicts: dict[str, int] = {}
        overwritten: dict[str, int] = {}
        if conflicts and on_conflict == "skip":
            filtered_rows, skipped_conflicts = self._filter_conflict_rows(
                rows, conflict_keys, pk_columns
            )
            if not self._count_core_rows(filtered_rows):
                raise ValueError(
                    "所有待回滚行都与当前数据冲突；已按“跳过冲突”处理，未修改数据库"
                )
        elif conflicts and on_conflict == "overwrite":
            overwritten = dict(conflicts)

        safety_backup_id: str | None = None
        safety = self.backup_store.create("safety", [])
        try:
            safety.snapshot_db(self.db_path)
            safety.manifest["status"] = "completed"
            safety.manifest["reason"] = "rollback_pre_snapshot"
            safety.save()
            safety_backup_id = safety.backup_id
        except Exception as exc:
            raise RuntimeError(
                f"回滚前安全快照失败，已中止回滚：{exc}"
            ) from exc

        try:
            connection = self._connect(write=True)
        except Exception as exc:
            raise RuntimeError(f"打开数据库失败，已中止回滚：{exc}") from exc
        try:
            connection.execute("BEGIN IMMEDIATE")
            restored = BackupBundle.restore_rows(connection, filtered_rows)
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

        skipped_files = len(manifest.get("files") or [])
        self.backup_store.mark_restored(
            manifest,
            restored,
            skipped_files=skipped_files,
            safety_backup_id=safety_backup_id,
            skipped_conflicts=skipped_conflicts,
            overwritten=overwritten,
        )
        return {
            "backup_id": backup_id,
            "restored": restored,
            "restored_files": 0,
            "skipped_files": skipped_files,
            "safety_backup_id": safety_backup_id,
            "safety_warning": None,
            "conflicts": conflicts,
            "skipped_conflicts": skipped_conflicts,
            "overwritten": overwritten,
        }

    def _validate_backup_rows(
        self,
        manifest: dict[str, Any],
        rows: dict[str, Any],
    ) -> None:
        expected = sum(
            int(value or 0)
            for table, value in (manifest.get("rows") or {}).items()
            if str(table) in CORE_TABLES
        )
        actual = self._count_core_rows(rows)
        if expected != actual:
            raise ValueError(
                f"备份行数与清单不一致（清单 {expected} 行，实际 {actual} 行），"
                "可能已损坏，已中止回滚"
            )
        if actual == 0:
            raise ValueError("备份没有可回滚的核心库数据，已中止回滚")

    @staticmethod
    def _count_core_rows(rows: dict[str, Any]) -> int:
        total = 0
        for entry in rows.values():
            if not isinstance(entry, dict):
                continue
            if entry.get("database", "core") != "core":
                continue
            total += len(entry.get("rows") or [])
        return total

    def _find_conflicts(
        self,
        rows: dict[str, Any],
    ) -> tuple[dict[str, int], dict[str, set[tuple[Any, ...]]], dict[str, list[str]]]:
        conflicts: dict[str, int] = {}
        conflict_keys: dict[str, set[tuple[Any, ...]]] = {}
        pk_columns: dict[str, list[str]] = {}
        connection = self._connect(write=False)
        try:
            for table, entry in rows.items():
                if not isinstance(entry, dict):
                    continue
                if entry.get("database", "core") != "core":
                    continue
                table_name = str(table)
                if table_name not in CORE_TABLES:
                    raise ValueError(f"备份包含非核心表，已拒绝回滚：{table_name}")
                values = entry.get("rows") or []
                columns = [str(column) for column in (entry.get("columns") or [])]
                if not values or not columns:
                    continue
                keys = self._primary_key_columns(connection, table_name)
                pk_columns[table_name] = keys
                if not keys:
                    continue
                existing = self._existing_keys(
                    connection, table_name, keys, values
                )
                if existing:
                    conflicts[table_name] = len(existing)
                    conflict_keys[table_name] = existing
        finally:
            connection.close()
        return conflicts, conflict_keys, pk_columns

    @staticmethod
    def _primary_key_columns(
        connection: sqlite3.Connection,
        table: str,
    ) -> list[str]:
        rows = connection.execute(f'PRAGMA table_info("{table}")').fetchall()
        ordered = [
            (int(row[5] or 0), str(row[1]))
            for row in rows
            if len(row) > 5 and int(row[5] or 0) > 0
        ]
        ordered.sort(key=lambda item: item[0])
        return [name for _, name in ordered]

    def _existing_keys(
        self,
        connection: sqlite3.Connection,
        table: str,
        pk_columns: list[str],
        values: list[dict[str, Any]],
    ) -> set[tuple[Any, ...]]:
        found: set[tuple[Any, ...]] = set()
        select_sql = ",".join('"%s"' % column for column in pk_columns)
        for chunk in _chunks(values, 200):
            conditions: list[str] = []
            params: list[Any] = []
            for row in chunk:
                key = self._row_key(row, pk_columns)
                if key is None:
                    continue
                conditions.append(
                    "("
                    + " AND ".join('"%s" = ?' % column for column in pk_columns)
                    + ")"
                )
                params.extend(key)
            if not conditions:
                continue
            sql = (
                f'SELECT {select_sql} FROM "{table}" WHERE '
                + " OR ".join(conditions)
            )
            for existing in connection.execute(sql, params):
                found.add(tuple(existing))
        return found

    @staticmethod
    def _row_key(
        row: dict[str, Any],
        pk_columns: list[str],
    ) -> tuple[Any, ...] | None:
        key: list[Any] = []
        for column in pk_columns:
            if column not in row:
                return None
            value = row[column]
            if value is None:
                return None
            key.append(value)
        return tuple(key)

    def _filter_conflict_rows(
        self,
        rows: dict[str, Any],
        conflict_keys: dict[str, set[tuple[Any, ...]]],
        pk_columns: dict[str, list[str]],
    ) -> tuple[dict[str, Any], dict[str, int]]:
        filtered: dict[str, Any] = {}
        skipped: dict[str, int] = {}
        for table, entry in rows.items():
            if not isinstance(entry, dict):
                continue
            table_name = str(table)
            if entry.get("database", "core") != "core":
                filtered[table_name] = entry
                continue
            keys = conflict_keys.get(table_name)
            pk = pk_columns.get(table_name) or []
            values = entry.get("rows") or []
            if not keys or not pk:
                filtered[table_name] = entry
                continue
            kept: list[dict[str, Any]] = []
            skipped_count = 0
            for row in values:
                key = self._row_key(row, pk)
                if key is not None and key in keys:
                    skipped_count += 1
                    continue
                kept.append(row)
            if skipped_count:
                skipped[table_name] = skipped_count
            if kept:
                filtered[table_name] = {**entry, "rows": kept}
        return filtered, skipped

    def _validate_targets(
        self,
        scan: dict[str, Any],
        selected_umos: list[str],
    ) -> tuple[dict[str, dict[str, Any]], list[str]]:
        if not selected_umos:
            raise ValueError("没有选择任何会话")
        scan_targets = {
            str(item.get("umo")): item
            for item in (scan.get("targets") or [])
            if item.get("umo")
        }
        selected: dict[str, dict[str, Any]] = {}
        invalid: list[str] = []
        for umo in selected_umos:
            item = scan_targets.get(str(umo))
            if not item or not item.get("selectable"):
                invalid.append(str(umo))
                continue
            selected[str(umo)] = item
        if not selected:
            raise ValueError(
                "没有可清理的有效目标；以下会话已失效："
                + "、".join(invalid[:10])
            )
        return selected, invalid

    def _operations(
        self,
        targets: list[dict[str, Any]],
        include_provider_stats: bool,
    ) -> list[tuple[str, str, list[Any]]]:
        operations: list[tuple[str, str, list[Any]]] = []
        for target in targets:
            umo = str(target.get("umo"))
            platform_id = str(target.get("platform_id") or "")
            operations.append(("umo_aliases", "umo = ?", [umo]))
            operations.append(("conversations", "user_id = ?", [umo]))
            if include_provider_stats:
                operations.append(("provider_stats", "umo = ?", [umo]))
            operations.append(
                (
                    "platform_message_history",
                    "platform_id = ? AND user_id = ?",
                    [platform_id, umo],
                )
            )
            ids = [int(value) for value in (target.get("preference_row_ids") or [])]
            for chunk in _chunks(ids, 400):
                marks = ",".join("?" for _ in chunk)
                operations.append(("preferences", f"id IN ({marks})", chunk))
        return operations

    def _select(
        self,
        connection: sqlite3.Connection,
        table: str,
        where: str,
        params: list[Any],
    ) -> tuple[list[str], list[dict[str, Any]]]:
        cursor = connection.execute(f'SELECT * FROM "{table}" WHERE {where}', params)
        columns = [description[0] for description in cursor.description or []]
        rows = [dict(zip(columns, row)) for row in cursor.fetchall()]
        return columns, rows

    @staticmethod
    def _delete(
        connection: sqlite3.Connection,
        table: str,
        where: str,
        params: list[Any],
    ) -> int:
        cursor = connection.execute(f'DELETE FROM "{table}" WHERE {where}', params)
        return int(cursor.rowcount or 0)

    @staticmethod
    def _quick_check(connection: sqlite3.Connection) -> str:
        row = connection.execute("PRAGMA quick_check").fetchone()
        if not row:
            return ""
        return str(row[0] or "")

    def _connect(self, write: bool = False) -> sqlite3.Connection:
        if write:
            if not self.db_path.is_file():
                raise FileNotFoundError("AstrBot 主库 data_v4.db 不存在")
            connection = sqlite3.connect(
                write_uri(self.db_path), uri=True, timeout=30
            )
            connection.execute("PRAGMA busy_timeout=30000")
            return connection
        return sqlite3.connect(readonly_uri(self.db_path), uri=True, timeout=30)


def _chunks(values: list[Any], size: int) -> list[list[Any]]:
    return [values[index : index + size] for index in range(0, len(values), size)]


def _emit(progress: ProgressCallback | None, stage: str, percent: int, message: str) -> None:
    if progress is None:
        return
    try:
        progress(stage, max(0, min(100, int(percent))), message)
    except Exception:
        pass
