"""核心库清理与回滚（V0.2）。

清理前先生成整库快照和逐行 JSON 备份；删除在单事务内完成。
自动回滚使用逐行 JSON，不直接替换运行中的 data_v4.db。
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from .backup import BackupBundle, BackupStore

ProgressCallback = Callable[[str, int, str], None]

CORE_TABLES = (
    "umo_aliases",
    "conversations",
    "preferences",
    "provider_stats",
    "platform_message_history",
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
        targets = self._validate_targets(scan, selected_umos)
        bundle = self.backup_store.create("core", list(targets))
        warnings: list[str] = []
        counts: dict[str, int] = {table: 0 for table in CORE_TABLES}

        _emit(progress, "生成整库快照", 5, "正在复制 data_v4.db")
        try:
            bundle.snapshot_db(self.db_path)
        except Exception as exc:
            warnings.append(f"整库快照失败：{exc}")
        _emit(progress, "备份并删除数据", 15, f"共 {len(targets)} 个会话")

        connection = self._connect(write=True)
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

            # 先落盘备份，再提交删除；即使进程在提交前退出，也有可回滚数据。
            _emit(progress, "写入备份清单", 92, "正在保存逐行备份")
            bundle.manifest["status"] = "pending"
            bundle.manifest["counts"] = counts
            bundle.save()
            connection.commit()
        except Exception:
            connection.rollback()
            bundle.manifest["status"] = "failed"
            bundle.save()
            raise
        finally:
            connection.close()

        bundle.manifest["status"] = "completed"
        bundle.manifest["counts"] = counts
        bundle.save()
        if finalize:
            self.backup_store.prune(retain)
        _emit(progress, "清理完成", 100, f"共删除 {sum(counts.values())} 行")
        return CleanResult(
            backup_id=bundle.backup_id,
            targets=list(targets),
            counts={key: value for key, value in counts.items() if value},
            warnings=warnings,
            bundle=bundle,
        )

    def rollback(self, backup_id: str) -> dict[str, Any]:
        manifest, rows = self.backup_store.load(backup_id)
        connection = self._connect(write=True)
        try:
            connection.execute("BEGIN IMMEDIATE")
            restored = BackupBundle.restore_rows(connection, rows)
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

        bundle = BackupBundle(
            backup_id=backup_id,
            root=self.backup_store.root / backup_id,
            manifest=manifest,
            rows=rows,
            files=list(manifest.get("files") or []),
        )
        restored_files = bundle.restore_files()
        self.backup_store.mark_restored(manifest, restored)
        return {
            "backup_id": backup_id,
            "restored": restored,
            "restored_files": restored_files,
        }

    def _validate_targets(
        self,
        scan: dict[str, Any],
        selected_umos: list[str],
    ) -> dict[str, dict[str, Any]]:
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
        if invalid:
            raise ValueError("以下会话不是可清理候选：" + "、".join(invalid[:10]))
        return selected

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

    def _connect(self, write: bool = False) -> sqlite3.Connection:
        if write:
            connection = sqlite3.connect(self.db_path, timeout=30)
            connection.execute("PRAGMA busy_timeout=30000")
            return connection
        uri = f"file:{self.db_path.as_posix()}?mode=ro"
        return sqlite3.connect(uri, uri=True, timeout=30)


def _chunks(values: list[int], size: int) -> list[list[int]]:
    return [values[index : index + size] for index in range(0, len(values), size)]


def _emit(progress: ProgressCallback | None, stage: str, percent: int, message: str) -> None:
    if progress is None:
        return
    try:
        progress(stage, max(0, min(100, int(percent))), message)
    except Exception:
        pass
