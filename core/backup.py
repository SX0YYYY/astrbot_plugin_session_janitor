"""备份包：整库快照 + 被删行 JSON + 被改文件副本。

V0.2 的自动回滚以逐行 JSON 为准，整库快照只作为灾备兜底。
"""

from __future__ import annotations

import base64
import json
import os
import re
import shutil
import sqlite3
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .models import CORE_TABLES
from .sqlite_utils import readonly_uri

BACKUP_ID_RE = re.compile(r"^\d{8}_\d{6}_[0-9a-f]{6}$")


def _jsonable(value: Any) -> Any:
    if isinstance(value, bytes):
        return {"__bytes_b64__": base64.b64encode(value).decode("ascii")}
    return value


def _restore_value(value: Any) -> Any:
    if isinstance(value, dict) and "__bytes_b64__" in value:
        return base64.b64decode(value["__bytes_b64__"])
    return value


def _now_id() -> str:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    return f"{stamp}_{uuid.uuid4().hex[:6]}"


def safe_backup_id(value: object) -> str:
    """校验备份 ID；只允许本插件生成的“时间戳_随机串”格式。"""

    text = str(value or "").strip()
    if not BACKUP_ID_RE.fullmatch(text):
        raise ValueError("备份 ID 格式不正确")
    return text


@dataclass
class BackupBundle:
    backup_id: str
    root: Path
    manifest: dict[str, Any]
    rows: dict[str, dict[str, Any]] = field(default_factory=dict)
    files: list[dict[str, Any]] = field(default_factory=list)

    @property
    def files_dir(self) -> Path:
        return self.root / "files"

    def snapshot_db(self, db_path: str | Path) -> Path:
        """用 SQLite backup API 生成一致性快照；源库以只读方式打开。"""

        source_path = Path(db_path)
        target = self.root / "data_v4.db"
        source = sqlite3.connect(readonly_uri(source_path), uri=True, timeout=30)
        source.execute("PRAGMA busy_timeout=30000")
        try:
            target_conn = sqlite3.connect(target)
            try:
                source.backup(target_conn)
            finally:
                target_conn.close()
        finally:
            source.close()
        self.manifest["db_snapshot"] = target.name
        self.manifest["db_snapshot_bytes"] = target.stat().st_size
        return target

    def add_rows(
        self,
        table: str,
        columns: list[str],
        rows: list[dict[str, Any]],
        database: str = "core",
    ) -> None:
        if not rows:
            return
        entry = self.rows.setdefault(
            table,
            {"database": database, "columns": columns, "rows": []},
        )
        entry["rows"].extend({key: _jsonable(value) for key, value in row.items()} for row in rows)

    def add_file(self, source: str | Path, relative: str) -> None:
        source_path = Path(source)
        relative_path = Path(relative)
        if relative_path.is_absolute() or ".." in relative_path.parts:
            raise ValueError("备份文件相对路径不安全")
        destination = self.files_dir / relative_path
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source_path, destination)
        self.files.append(
            {
                "source": str(source_path),
                "relative": relative_path.as_posix(),
                "bytes": destination.stat().st_size,
            }
        )

    def save(self) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        self.manifest["rows"] = {
            table: len(entry.get("rows") or []) for table, entry in self.rows.items()
        }
        self.manifest["files"] = self.files
        self.manifest["backup_id"] = self.backup_id
        tmp_rows = self.root / "rows.json.tmp"
        with tmp_rows.open("w", encoding="utf-8") as handle:
            json.dump(self.rows, handle, ensure_ascii=False)
        os.replace(tmp_rows, self.root / "rows.json")
        tmp_manifest = self.root / "manifest.json.tmp"
        with tmp_manifest.open("w", encoding="utf-8") as handle:
            json.dump(self.manifest, handle, ensure_ascii=False, indent=2)
        os.replace(tmp_manifest, self.root / "manifest.json")

    @staticmethod
    def restore_rows(
        connection: sqlite3.Connection,
        rows: dict[str, dict[str, Any]],
        database: str = "core",
    ) -> dict[str, int]:
        restored: dict[str, int] = {}
        table_columns: dict[str, set[str]] = {}
        for table, entry in rows.items():
            if entry.get("database", "core") != database:
                continue
            table_name = str(table)
            values = entry.get("rows") or []
            columns = [str(column) for column in (entry.get("columns") or [])]
            if not values or not columns:
                continue
            if table_name not in CORE_TABLES:
                raise ValueError(f"备份包含非核心表，已拒绝回滚：{table_name}")
            if table_name not in table_columns:
                info = connection.execute(
                    f'PRAGMA table_info("{table_name}")'
                ).fetchall()
                table_columns[table_name] = {
                    str(row[1]) for row in info if len(row) > 1
                }
            known = table_columns[table_name]
            if not known:
                raise ValueError(f"当前数据库缺少表 {table_name}，无法回滚")
            unknown = [column for column in columns if column not in known]
            if unknown:
                raise ValueError(
                    "备份与当前表结构不兼容，未知列："
                    + "、".join(unknown[:10])
                )
            placeholders = ",".join("?" for _ in columns)
            column_sql = ",".join('"%s"' % column for column in columns)
            sql = f'INSERT OR REPLACE INTO "{table_name}" ({column_sql}) VALUES ({placeholders})'
            payload = []
            for row in values:
                payload.append(tuple(_restore_value(row.get(column)) for column in columns))
            connection.executemany(sql, payload)
            restored[table_name] = len(payload)
        return restored


class BackupStore:
    def __init__(self, data_dir: str | Path):
        self.root = Path(data_dir) / "backups"
        self.root.mkdir(parents=True, exist_ok=True)

    def create(self, mode: str, targets: list[str]) -> BackupBundle:
        backup_id = _now_id()
        root = self.root / backup_id
        root.mkdir(parents=True, exist_ok=True)
        manifest = {
            "backup_id": backup_id,
            "created_at": datetime.now(timezone.utc).isoformat(),
            "mode": mode,
            "targets": targets,
            "status": "created",
            "restored_at": None,
        }
        return BackupBundle(backup_id=backup_id, root=root, manifest=manifest)

    def _path(self, backup_id: object) -> Path:
        text = safe_backup_id(backup_id)
        root = self.root.resolve()
        target = (self.root / text).resolve()
        try:
            target.relative_to(root)
        except ValueError as exc:
            raise ValueError("备份路径不安全") from exc
        if target == root or target.parent != root:
            raise ValueError("备份路径不安全")
        return target

    def list(self, limit: int = 50) -> list[dict[str, Any]]:
        items: list[dict[str, Any]] = []
        for path in sorted(self.root.iterdir(), reverse=True):
            if not path.is_dir() or not BACKUP_ID_RE.fullmatch(path.name):
                continue
            manifest_path = path / "manifest.json"
            if not manifest_path.is_file():
                continue
            try:
                data = json.loads(manifest_path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            if isinstance(data, dict) and str(data.get("backup_id") or "") == path.name:
                items.append(data)
            if len(items) >= limit:
                break
        return items

    def load(self, backup_id: str) -> tuple[dict[str, Any], dict[str, Any]]:
        text = safe_backup_id(backup_id)
        root = self._path(text)
        manifest_path = root / "manifest.json"
        rows_path = root / "rows.json"
        if not manifest_path.is_file():
            raise FileNotFoundError("备份不存在")
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        except ValueError as exc:
            raise ValueError("备份清单格式不正确") from exc
        if not isinstance(manifest, dict):
            raise ValueError("备份清单格式不正确")
        if str(manifest.get("backup_id") or "") != text:
            raise ValueError("备份清单与备份 ID 不一致")
        if not rows_path.is_file():
            if manifest.get("mode") == "safety":
                return manifest, {}
            raise FileNotFoundError("备份缺少逐行数据 rows.json，无法回滚")
        try:
            loaded = json.loads(rows_path.read_text(encoding="utf-8"))
        except ValueError as exc:
            raise ValueError("备份数据格式不正确") from exc
        if not isinstance(loaded, dict):
            raise ValueError("备份数据格式不正确")
        return manifest, loaded

    def mark_restored(
        self,
        manifest: dict[str, Any],
        restored: dict[str, Any],
        skipped_files: int = 0,
        safety_backup_id: str | None = None,
        skipped_conflicts: dict[str, int] | None = None,
        overwritten: dict[str, int] | None = None,
    ) -> None:
        root = self._path(manifest.get("backup_id"))
        manifest["status"] = "restored"
        manifest["restored_at"] = datetime.now(timezone.utc).isoformat()
        manifest["restored"] = restored
        manifest["skipped_files"] = int(skipped_files or 0)
        manifest["safety_backup_id"] = safety_backup_id
        if skipped_conflicts is not None:
            manifest["skipped_conflicts"] = skipped_conflicts
        if overwritten is not None:
            manifest["overwritten"] = overwritten
        tmp = root / "manifest.json.tmp"
        with tmp.open("w", encoding="utf-8") as handle:
            json.dump(manifest, handle, ensure_ascii=False, indent=2)
        os.replace(tmp, root / "manifest.json")

    def prune(self, retain: int, keep: set[str] | None = None) -> None:
        retain = max(1, int(retain))
        protected = set(keep or ())
        dirs = [
            path
            for path in sorted(self.root.iterdir(), reverse=True)
            if path.is_dir() and BACKUP_ID_RE.fullmatch(path.name)
        ]
        restored_paths: list[str] = []
        for path in dirs:
            manifest_path = path / "manifest.json"
            try:
                data = json.loads(manifest_path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            if not isinstance(data, dict):
                continue
            if data.get("status") == "pending":
                protected.add(path.name)
            elif data.get("status") == "restored":
                restored_paths.append(path.name)
        # 已回滚备份也按保留份数封顶，避免整库快照无限增长。
        protected.update(restored_paths[:retain])
        deletable = [path for path in dirs if path.name not in protected]
        for path in deletable[retain:]:
            try:
                shutil.rmtree(path)
            except OSError:
                pass
