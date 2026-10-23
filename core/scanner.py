"""只读扫描器：把主库里的 QQ 群/好友会话与当前平台列表对比。

扫描阶段只产出清单和候选状态，不执行删除。分类原则：
- GroupMessage 不在 get_group_list -> 已退群（默认勾选）
- FriendMessage 不在 get_friend_list -> 好友列表外；最后活跃超过阈值时
  标为过期临时会话，否则标为“好友列表外”（两者默认都不勾选）
- webchat、系统会话、其他平台 -> 跳过
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .db import DbSnapshot, JanitorDB, PreferenceRow, preference_targets
from .models import TABLE_KEYS, PlatformSnapshot, ScanResult, Target
from .umo import Umo, parse_umo


@dataclass
class _Builder:
    umo: Umo
    counts: dict[str, int] = field(default_factory=lambda: {key: 0 for key in TABLE_KEYS})
    last_iso: str | None = None
    last_ts: float | None = None
    names: list[str] = field(default_factory=list)
    notes: set[str] = field(default_factory=set)
    preference_row_ids: set[int] = field(default_factory=set)

    def add(self, table: str, count: int = 1) -> None:
        if table in self.counts:
            self.counts[table] += max(0, int(count))

    def add_preference(self, row: PreferenceRow) -> None:
        self.preference_row_ids.add(row.row_id)
        self.counts["preferences"] = len(self.preference_row_ids)
        self.touch(row.last_active, row.last_active_ts)

    def touch(self, iso: str | None, stamp: float | None) -> None:
        if stamp is not None and (self.last_ts is None or stamp > self.last_ts):
            self.last_iso = iso
            self.last_ts = stamp


class Scanner:
    def __init__(
        self,
        db_path: str | Path,
        platform_id: str = "default",
        temp_session_days: int = 30,
        include_provider_stats: bool = False,
        whitelist: list[Any] | None = None,
        now: float | None = None,
    ):
        self.db_path = Path(db_path)
        self.platform_id = str(platform_id or "default")
        self.temp_session_days = max(1, int(temp_session_days))
        self.include_provider_stats = bool(include_provider_stats)
        self.whitelist = [str(item).strip() for item in (whitelist or []) if str(item).strip()]
        self.now = float(now) if now is not None else time.time()

    def scan(self, snapshot: PlatformSnapshot) -> ScanResult:
        raw = JanitorDB(self.db_path).snapshot()
        builders: dict[str, _Builder] = {}

        def get_builder(raw_umo: object) -> _Builder | None:
            parsed = parse_umo(raw_umo)
            if parsed is None or parsed.is_webchat:
                return None
            if parsed.platform_id != self.platform_id:
                return None
            if parsed.raw not in builders:
                builders[parsed.raw] = _Builder(parsed)
            return builders[parsed.raw]

        # umo_aliases
        for umo, info in raw.umo_aliases.items():
            builder = get_builder(umo)
            if builder is None:
                continue
            builder.add("umo_aliases")
            builder.touch(info.get("last_active"), info.get("last_active_ts"))
            for name in (info.get("user_alias"), info.get("auto_name")):
                if name:
                    builder.names.append(str(name))

        # conversations
        for umo, info in raw.conversations.items():
            builder = get_builder(umo)
            if builder is None:
                continue
            builder.add("conversations", int(info.get("count") or 0))
            builder.touch(info.get("last_active"), info.get("last_active_ts"))

        # provider_stats
        for umo, info in raw.provider_stats.items():
            builder = get_builder(umo)
            if builder is None:
                continue
            builder.add("provider_stats", int(info.get("count") or 0))
            builder.touch(info.get("last_active"), info.get("last_active_ts"))

        # platform_message_history（QQ 的 user_id 是 UMO；webchat 的是 UUID）
        skipped_message_history = 0
        for info in raw.message_history.values():
            if str(info.get("platform_id") or "").lower() == "webchat":
                skipped_message_history += int(info.get("count") or 0)
                continue
            builder = get_builder(info.get("user_id"))
            if builder is None:
                continue
            builder.add("platform_message_history", int(info.get("count") or 0))
            builder.touch(info.get("last_active"), info.get("last_active_ts"))

        # preferences：只有唯一归属时才挂到会话；跨会话共享的行整行删除会误伤，
        # 因此跳过并记录 warning。
        shared_preference_rows = 0
        for row in raw.preferences:
            targets_for_row = preference_targets(row, self.platform_id)
            if not targets_for_row:
                continue
            if len(targets_for_row) > 1:
                shared_preference_rows += 1
                continue
            builder = get_builder(next(iter(targets_for_row)))
            if builder is None:
                continue
            builder.add_preference(row)

        targets = [
            self._build_target(builder, snapshot, raw)
            for builder in builders.values()
        ]
        targets.sort(
            key=lambda item: (
                0 if item.selectable else 1,
                item.status,
                item.kind,
                item.session_id,
            )
        )

        summary = {status: 0 for status in (
            "active",
            "orphan_group",
            "expired_temp",
            "missing_friend",
            "whitelisted",
        )}
        for target in targets:
            summary[target.status] = summary.get(target.status, 0) + 1
        summary["total"] = len(targets)
        summary["candidates"] = sum(1 for target in targets if target.selectable)

        tables = self._build_tables(raw, targets)
        warnings = list(raw.warnings) + list(snapshot.warnings)
        if not self.include_provider_stats:
            warnings.append("调用统计默认不计入清理预览（面板可单独勾选）")
        if raw.table_counts.get("platform_sessions", 0):
            warnings.append("platform_sessions 已跳过（插件不处理平台会话表）")
        if skipped_message_history:
            warnings.append(
                f"platform_message_history 中有 {skipped_message_history} 条 webchat 记录，已跳过"
            )
        if shared_preference_rows:
            warnings.append(
                f"{shared_preference_rows} 条偏好同时关联多个会话，已跳过（整行删除会误伤其他会话）"
            )
        if not targets:
            warnings.append("未发现 QQ 群/好友会话数据")

        return ScanResult(
            ok=True,
            generated_at=datetime.now(timezone.utc).isoformat(),
            platform_id=self.platform_id,
            platform=snapshot.to_dict(),
            summary=summary,
            tables=tables,
            targets=targets,
            warnings=warnings,
        )

    def _is_whitelisted(self, umo: Umo) -> bool:
        tokens = {
            umo.raw,
            umo.session_id,
            f"{umo.message_type}:{umo.session_id}",
        }
        return any(item in tokens for item in self.whitelist)

    def _build_target(
        self,
        builder: _Builder,
        snapshot: PlatformSnapshot,
        raw: DbSnapshot,
    ) -> Target:
        umo = builder.umo
        session_id = umo.session_id
        kind = umo.kind

        if umo.is_group:
            platform_name = snapshot.groups.get(session_id)
        else:
            platform_name = snapshot.friends.get(session_id)

        name = platform_name or ""
        if not name:
            for candidate in reversed(builder.names):
                if candidate:
                    name = candidate
                    break
        if not name:
            name = session_id

        whitelisted = self._is_whitelisted(umo)
        age_days: float | None = None
        if builder.last_ts is not None:
            age_days = max(0.0, (self.now - builder.last_ts) / 86400.0)
        cleanable_rows = sum(
            int(value or 0)
            for table, value in builder.counts.items()
            if table != "provider_stats"
        )

        notes: set[str] = set(builder.notes)
        if whitelisted:
            status = "whitelisted"
            notes.add("命中白名单，永不清理")
            selectable = False
            default_selected = False
        elif umo.is_group:
            if session_id in snapshot.groups:
                status = "active"
                notes.add("当前仍在群列表中")
            else:
                status = "orphan_group"
                notes.add("不在当前群列表中（已退群）")
            selectable = status == "orphan_group"
            default_selected = status == "orphan_group" and (
                cleanable_rows > 0 or self.include_provider_stats
            )
        else:
            if session_id in snapshot.friends:
                status = "active"
                notes.add("当前仍在好友列表中")
            elif age_days is not None and age_days > self.temp_session_days:
                status = "expired_temp"
                notes.add(
                    f"不在好友列表，最后活跃约 {age_days:.0f} 天前，"
                    f"超过 {self.temp_session_days} 天阈值"
                )
            else:
                status = "missing_friend"
                notes.add("不在好友列表，未超过临时会话阈值（统一按好友列表外处理）")
            selectable = status in {"expired_temp", "missing_friend"}
            default_selected = False
            notes.add("插件无法区分删好友和换号；好友列表外/临时会话默认不勾选")

        if builder.counts.get("provider_stats"):
            if cleanable_rows == 0 and not self.include_provider_stats:
                notes.add(
                    "只剩调用统计；默认不清理，"
                    "需要时在预览/执行清理页勾选“包含调用统计”"
                )
            else:
                notes.add("调用统计默认不清理，可在预览中单独勾选")
        if builder.counts.get("preferences"):
            notes.add("preferences 命中 analysis_* 或会话配置键")
        if builder.counts.get("platform_message_history"):
            notes.add("platform_message_history 命中 QQ 会话历史")

        return Target(
            umo=umo.raw,
            platform_id=umo.platform_id,
            message_type=umo.message_type,
            session_id=session_id,
            kind=kind,
            status=status,
            name=name,
            last_active=builder.last_iso,
            last_active_ts=builder.last_ts,
            counts=dict(builder.counts),
            evidence=[key for key in TABLE_KEYS if builder.counts.get(key)],
            notes=sorted(notes),
            selectable=selectable,
            default_selected=default_selected,
            preference_row_ids=sorted(builder.preference_row_ids),
        )

    def _build_tables(
        self,
        raw: DbSnapshot,
        targets: list[Target],
    ) -> dict[str, dict[str, Any]]:
        candidate_targets = [target for target in targets if target.selectable]
        preference_ids: set[int] = set()
        for target in candidate_targets:
            preference_ids.update(target.preference_row_ids)

        tables: dict[str, dict[str, Any]] = {}
        for table in TABLE_KEYS:
            if table == "preferences":
                orphan = len(preference_ids)
            elif table == "umo_aliases":
                orphan = sum(
                    1 for target in candidate_targets if target.counts.get(table, 0)
                )
            else:
                orphan = sum(
                    int(target.counts.get(table, 0)) for target in candidate_targets
                )
            tables[table] = {
                "total": int(raw.table_counts.get(table, 0)),
                "orphan": orphan,
                "optional": table == "provider_stats",
            }

        tables["platform_sessions"] = {
            "total": int(raw.table_counts.get("platform_sessions", 0)),
            "orphan": 0,
            "optional": False,
            "skipped": True,
        }
        return tables
