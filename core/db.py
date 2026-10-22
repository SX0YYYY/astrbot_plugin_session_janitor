"""AstrBot 主库（data_v4.db）只读访问层。

V0.1 只做只读扫描：连接串固定 ``mode=ro``，不会执行任何 INSERT/UPDATE/DELETE。
表不存在、字段缺失或某个查询失败都不会让整次扫描崩溃，只会记入 warnings。
"""

from __future__ import annotations

import json
import re
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .umo import (
    GROUP_MESSAGE,
    find_umos_in_text,
    make_umo,
    parse_analysis_key,
    parse_umo,
)

RELEVANT_TABLES = (
    "umo_aliases",
    "conversations",
    "preferences",
    "provider_stats",
    "platform_message_history",
    "platform_sessions",
)


@dataclass
class PreferenceRow:
    row_id: int
    scope: str
    scope_id: str
    key: str
    value_text: str
    last_active: str | None
    last_active_ts: float | None
    umos: set[str] = field(default_factory=set)
    group_ids: set[str] = field(default_factory=set)


def preference_target(row: PreferenceRow, platform_id: str) -> str | None:
    """给一条偏好记录选一个主归属，避免同一行被多个会话重复计数。"""

    if row.group_ids:
        group_id = sorted(row.group_ids)[0]
        return make_umo(platform_id, GROUP_MESSAGE, group_id)

    parsed_scope = parse_umo(row.scope_id)
    if (
        parsed_scope is not None
        and not parsed_scope.is_webchat
        and parsed_scope.platform_id == platform_id
    ):
        return parsed_scope.raw

    for umo in sorted(row.umos):
        parsed = parse_umo(umo)
        if (
            parsed is not None
            and not parsed.is_webchat
            and parsed.platform_id == platform_id
        ):
            return parsed.raw
    return None


@dataclass
class DbSnapshot:
    table_counts: dict[str, int]
    columns: dict[str, list[str]]
    umo_aliases: dict[str, dict[str, Any]]
    conversations: dict[str, dict[str, Any]]
    provider_stats: dict[str, dict[str, Any]]
    preferences: list[PreferenceRow]
    message_history: dict[str, dict[str, Any]]
    warnings: list[str]


def _as_iso_and_ts(value: Any) -> tuple[str | None, float | None]:
    """把 SQLite 里的日期/时间戳统一成 ISO 字符串和 epoch 秒。"""

    if value is None:
        return None, None
    if isinstance(value, (int, float)):
        stamp = float(value)
        if stamp > 1e12:  # 毫秒时间戳
            stamp /= 1000.0
        return (
            datetime.fromtimestamp(stamp, tz=timezone.utc).isoformat(),
            stamp,
        )

    text = str(value).strip()
    if not text:
        return None, None
    if re.fullmatch(r"\d+(?:\.\d+)?", text):
        try:
            return _as_iso_and_ts(float(text))
        except ValueError:
            return text, None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return text, None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.isoformat(), parsed.timestamp()


def _max_ts(*values: Any) -> tuple[str | None, float | None]:
    best_iso: str | None = None
    best_ts: float | None = None
    for value in values:
        iso, stamp = _as_iso_and_ts(value)
        if stamp is not None and (best_ts is None or stamp > best_ts):
            best_iso, best_ts = iso, stamp
    return best_iso, best_ts


class JanitorDB:
    """只读数据库快照读取器。"""

    def __init__(self, path: str | Path):
        self.path = Path(path)

    def _connect(self) -> sqlite3.Connection:
        uri = f"file:{self.path.as_posix()}?mode=ro"
        connection = sqlite3.connect(uri, uri=True, timeout=10)
        connection.row_factory = sqlite3.Row
        return connection

    def _table_names(self, connection: sqlite3.Connection) -> list[str]:
        rows = connection.execute(
            "select name from sqlite_master where type='table' order by name"
        ).fetchall()
        return [str(row["name"]) for row in rows]

    def _columns(self, connection: sqlite3.Connection, table: str) -> list[str]:
        try:
            rows = connection.execute(f'pragma table_info("{table}")').fetchall()
        except sqlite3.Error:
            return []
        return [str(row["name"]) for row in rows]

    def inspect(self) -> dict[str, Any]:
        if not self.path.is_file():
            return {
                "exists": False,
                "tables": [],
                "counts": {},
                "columns": {},
            }
        connection = self._connect()
        try:
            tables = self._table_names(connection)
            counts: dict[str, int] = {}
            columns: dict[str, list[str]] = {}
            for table in tables:
                columns[table] = self._columns(connection, table)
                try:
                    counts[table] = int(
                        connection.execute(f'select count(*) from "{table}"').fetchone()[0]
                    )
                except sqlite3.Error:
                    counts[table] = -1
        finally:
            connection.close()
        return {"exists": True, "tables": tables, "counts": counts, "columns": columns}

    def snapshot(self) -> DbSnapshot:
        warnings: list[str] = []
        if not self.path.is_file():
            return DbSnapshot(
                table_counts={},
                columns={},
                umo_aliases={},
                conversations={},
                provider_stats={},
                preferences=[],
                message_history={},
                warnings=["未找到 AstrBot 主库 data_v4.db"],
            )

        connection = self._connect()
        try:
            tables = set(self._table_names(connection))
            counts: dict[str, int] = {}
            columns: dict[str, list[str]] = {}
            for table in RELEVANT_TABLES:
                if table not in tables:
                    counts[table] = 0
                    columns[table] = []
                    warnings.append(f"表 {table} 不存在，已跳过")
                    continue
                columns[table] = self._columns(connection, table)
                try:
                    counts[table] = int(
                        connection.execute(f'select count(*) from "{table}"').fetchone()[0]
                    )
                except sqlite3.Error as exc:
                    counts[table] = 0
                    warnings.append(f"统计表 {table} 失败：{exc}")

            umo_aliases = self._read_umo_aliases(connection, columns, warnings)
            conversations = self._read_conversations(connection, columns, warnings)
            provider_stats = self._read_provider_stats(connection, columns, warnings)
            preferences = self._read_preferences(connection, columns, warnings)
            message_history = self._read_message_history(connection, columns, warnings)
        finally:
            connection.close()

        return DbSnapshot(
            table_counts=counts,
            columns=columns,
            umo_aliases=umo_aliases,
            conversations=conversations,
            provider_stats=provider_stats,
            preferences=preferences,
            message_history=message_history,
            warnings=warnings,
        )

    # ------------------------------------------------------------------
    # 各表读取
    # ------------------------------------------------------------------

    def _read_umo_aliases(
        self,
        connection: sqlite3.Connection,
        columns: dict[str, list[str]],
        warnings: list[str],
    ) -> dict[str, dict[str, Any]]:
        values: dict[str, dict[str, Any]] = {}
        if not columns.get("umo_aliases"):
            return values
        try:
            rows = connection.execute(
                "select umo, creator_sender_id, auto_name, user_alias, "
                "created_at, updated_at from umo_aliases"
            ).fetchall()
        except sqlite3.Error as exc:
            warnings.append(f"读取 umo_aliases 失败：{exc}")
            return values
        for row in rows:
            iso, stamp = _max_ts(row["created_at"], row["updated_at"])
            values[str(row["umo"])] = {
                "creator_sender_id": str(row["creator_sender_id"] or ""),
                "auto_name": str(row["auto_name"] or ""),
                "user_alias": str(row["user_alias"] or ""),
                "last_active": iso,
                "last_active_ts": stamp,
            }
        return values

    def _read_conversations(
        self,
        connection: sqlite3.Connection,
        columns: dict[str, list[str]],
        warnings: list[str],
    ) -> dict[str, dict[str, Any]]:
        values: dict[str, dict[str, Any]] = {}
        if not columns.get("conversations"):
            return values
        try:
            rows = connection.execute(
                "select user_id, count(*) as row_count, max(updated_at) as last_at "
                "from conversations group by user_id"
            ).fetchall()
        except sqlite3.Error as exc:
            warnings.append(f"读取 conversations 失败：{exc}")
            return values
        for row in rows:
            iso, stamp = _as_iso_and_ts(row["last_at"])
            values[str(row["user_id"])] = {
                "count": int(row["row_count"] or 0),
                "last_active": iso,
                "last_active_ts": stamp,
            }
        return values

    def _read_provider_stats(
        self,
        connection: sqlite3.Connection,
        columns: dict[str, list[str]],
        warnings: list[str],
    ) -> dict[str, dict[str, Any]]:
        values: dict[str, dict[str, Any]] = {}
        if not columns.get("provider_stats"):
            return values
        try:
            rows = connection.execute(
                "select umo, count(*) as row_count, max(end_time) as last_at "
                "from provider_stats group by umo"
            ).fetchall()
        except sqlite3.Error as exc:
            warnings.append(f"读取 provider_stats 失败：{exc}")
            return values
        for row in rows:
            iso, stamp = _as_iso_and_ts(row["last_at"])
            values[str(row["umo"])] = {
                "count": int(row["row_count"] or 0),
                "last_active": iso,
                "last_active_ts": stamp,
            }
        return values

    def _read_preferences(
        self,
        connection: sqlite3.Connection,
        columns: dict[str, list[str]],
        warnings: list[str],
    ) -> list[PreferenceRow]:
        if not columns.get("preferences"):
            return []
        try:
            rows = connection.execute(
                "select id, scope, scope_id, key, value, created_at, updated_at "
                "from preferences"
            ).fetchall()
        except sqlite3.Error as exc:
            warnings.append(f"读取 preferences 失败：{exc}")
            return []

        result: list[PreferenceRow] = []
        for row in rows:
            value = row["value"]
            if not isinstance(value, str):
                try:
                    value = json.dumps(value, ensure_ascii=False)
                except (TypeError, ValueError):
                    value = str(value or "")
            key = str(row["key"] or "")
            scope_id = str(row["scope_id"] or "")
            iso, stamp = _max_ts(row["created_at"], row["updated_at"])

            umos = find_umos_in_text(value)
            if parse_umo(scope_id) is not None:
                umos.add(scope_id)
            group_id = parse_analysis_key(key)
            group_ids = {group_id} if group_id else set()

            result.append(
                PreferenceRow(
                    row_id=int(row["id"]),
                    scope=str(row["scope"] or ""),
                    scope_id=scope_id,
                    key=key,
                    value_text=value,
                    last_active=iso,
                    last_active_ts=stamp,
                    umos=umos,
                    group_ids=group_ids,
                )
            )
        return result

    def _read_message_history(
        self,
        connection: sqlite3.Connection,
        columns: dict[str, list[str]],
        warnings: list[str],
    ) -> dict[str, dict[str, Any]]:
        values: dict[str, dict[str, Any]] = {}
        if not columns.get("platform_message_history"):
            return values
        try:
            rows = connection.execute(
                "select platform_id, user_id, count(*) as row_count, "
                "max(created_at) as last_at "
                "from platform_message_history group by platform_id, user_id"
            ).fetchall()
        except sqlite3.Error as exc:
            warnings.append(f"读取 platform_message_history 失败：{exc}")
            return values
        for row in rows:
            iso, stamp = _as_iso_and_ts(row["last_at"])
            key = f"{row['platform_id']}:{row['user_id']}"
            values[key] = {
                "platform_id": str(row["platform_id"] or ""),
                "user_id": str(row["user_id"] or ""),
                "count": int(row["row_count"] or 0),
                "last_active": iso,
                "last_active_ts": stamp,
            }
        return values
