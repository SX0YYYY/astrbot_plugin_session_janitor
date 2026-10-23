"""扫描结果的数据模型。"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any

STATUS_LABELS = {
    "active": "活跃",
    "orphan_group": "已退群",
    "expired_temp": "过期临时会话",
    "missing_friend": "好友列表外（疑似已删好友）",
    "whitelisted": "白名单",
    "skipped": "已跳过",
}

CANDIDATE_STATUSES = {"orphan_group", "expired_temp", "missing_friend"}

TABLE_KEYS = (
    "umo_aliases",
    "conversations",
    "preferences",
    "provider_stats",
    "platform_message_history",
)

# 插件只允许清理/回滚这五张 AstrBot 核心表；任何备份 JSON 里的其他表都必须拒绝。
CORE_TABLES = TABLE_KEYS


@dataclass
class PlatformSnapshot:
    platform_id: str
    groups: dict[str, str]
    friends: dict[str, str]
    captured_at: str
    source: str = "aiocqhttp"
    warnings: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.platform_id,
            "groups": len(self.groups),
            "friends": len(self.friends),
            "captured_at": self.captured_at,
            "source": self.source,
            "warnings": list(self.warnings),
        }


@dataclass
class Target:
    umo: str
    platform_id: str
    message_type: str
    session_id: str
    kind: str
    status: str
    name: str
    last_active: str | None
    last_active_ts: float | None
    counts: dict[str, int] = field(default_factory=dict)
    evidence: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    selectable: bool = False
    default_selected: bool = False
    preference_row_ids: list[int] = field(default_factory=list)

    @property
    def total_rows(self) -> int:
        return sum(int(v or 0) for v in self.counts.values())

    @property
    def status_label(self) -> str:
        return STATUS_LABELS.get(self.status, self.status)

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["total_rows"] = self.total_rows
        data["status_label"] = self.status_label
        return data


@dataclass
class ScanResult:
    ok: bool
    generated_at: str
    platform_id: str
    platform: dict[str, Any]
    summary: dict[str, int]
    tables: dict[str, dict[str, Any]]
    targets: list[Target] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "generated_at": self.generated_at,
            "platform_id": self.platform_id,
            "platform": self.platform,
            "summary": self.summary,
            "tables": self.tables,
            "targets": [target.to_dict() for target in self.targets],
            "warnings": self.warnings,
            "errors": self.errors,
        }
