"""备份包：整库快照 + 被删行 JSON + 被改文件副本。

V0.2 的自动回滚以逐行 JSON 为准，整库快照只作为灾备兜底。
"""

from __future__ import annotations

import base64
import json
import os
import shutil
import sqlite3
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


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
        source = sqlite3.connect(f"file:{source_path.as_posix()}?mode=ro", uri=True)
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

    def restore_files(self) -> int:
        restored = 0
        for item in self.files:
            source = self.files_dir / item["relative"]
            target = Path(item["source"])
            if not source.is_file():
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, target)
            restored += 1
        return restored

    @staticmethod
    def restore_rows(
        connection: sqlite3.Connection,
        rows: dict[str, dict[str, Any]],
        database: str = "core",
    ) -> dict[str, int]:
        restored: dict[str, int] = {}
        for table, entry in rows.items():
            if entry.get("database", "core") != database:
                continue
            values = entry.get("rows") or []
            columns = entry.get("columns") or []
            if not values or not columns:
                continue
            placeholders = ",".join("?" for _ in columns)
            sql = 'INSERT OR REPLACE INTO "%s" (%s) VALUES (%s)' % (
                table,
                ",".join('"%s"' % column for column in columns),
                placeholders,
            )
            payload = []
            for row in values:
                payload.append(tuple(_restore_value(row.get(column)) for column in columns))
            connection.executemany(sql, payload)
            restored[table] = len(payload)
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

    def list(self, limit: int = 50) -> list[dict[str, Any]]:
        items: list[dict[str, Any]] = []
        for path in sorted(self.root.iterdir(), reverse=True):
            if not path.is_dir():
                continue
            manifest_path = path / "manifest.json"
            if not manifest_path.is_file():
                continue
            try:
                data = json.loads(manifest_path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            if isinstance(data, dict):
                items.append(data)
            if len(items) >= limit:
                break
        return items

    def load(self, backup_id: str) -> tuple[dict[str, Any], dict[str, Any]]:
        root = self.root / backup_id
        manifest_path = root / "manifest.json"
        rows_path = root / "rows.json"
        if not manifest_path.is_file():
            raise FileNotFoundError("备份不存在")
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        rows = {}
        if rows_path.is_file():
            rows = json.loads(rows_path.read_text(encoding="utf-8"))
        return manifest, rows

    def mark_restored(self, manifest: dict[str, Any], restored: dict[str, Any]) -> None:
        root = self.root / manifest["backup_id"]
        manifest["status"] = "restored"
        manifest["restored_at"] = datetime.now(timezone.utc).isoformat()
        manifest["restored"] = restored
        tmp = root / "manifest.json.tmp"
        with tmp.open("w", encoding="utf-8") as handle:
            json.dump(manifest, handle, ensure_ascii=False, indent=2)
        os.replace(tmp, root / "manifest.json")

    def prune(self, retain: int) -> None:
        retain = max(1, int(retain))
        dirs = [path for path in sorted(self.root.iterdir(), reverse=True) if path.is_dir()]
        for path in dirs[retain:]:
            try:
                shutil.rmtree(path)
            except OSError:
                pass
