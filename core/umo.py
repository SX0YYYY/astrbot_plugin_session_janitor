"""UMO（统一消息来源）解析工具。

AstrBot 的 UMO 形如 ``default:GroupMessage:583715943``。这里只处理群聊和好友
私聊；webchat、其他平台和系统会话一律跳过，避免把仍在使用的数据当残留。
"""

from __future__ import annotations

import re
from dataclasses import dataclass

UMO_RE = re.compile(
    r"^(?P<platform>[^:]+):(?P<message_type>GroupMessage|FriendMessage):(?P<session_id>.+)$"
)
# qq_group_daily_analysis 的偏好键：analysis_<群号>_<日期>
ANALYSIS_KEY_RE = re.compile(r"analysis_(?P<group_id>\d{3,12})_")
# 从 JSON 文本里找 UMO，例如 default:GroupMessage:123456
UMO_IN_TEXT_RE = re.compile(
    r"(?P<umo>[A-Za-z0-9_.\-]+:(?:GroupMessage|FriendMessage):[^\"'\s,}\]]+)"
)

GROUP_MESSAGE = "GroupMessage"
FRIEND_MESSAGE = "FriendMessage"
SUPPORTED_TYPES = (GROUP_MESSAGE, FRIEND_MESSAGE)


@dataclass(frozen=True)
class Umo:
    raw: str
    platform_id: str
    message_type: str
    session_id: str

    @property
    def is_group(self) -> bool:
        return self.message_type == GROUP_MESSAGE

    @property
    def is_friend(self) -> bool:
        return self.message_type == FRIEND_MESSAGE

    @property
    def kind(self) -> str:
        return "group" if self.is_group else "friend"

    @property
    def is_webchat(self) -> bool:
        return self.platform_id.lower() == "webchat"


def parse_umo(value: object) -> Umo | None:
    """把字符串解析成 Umo；不是群聊/好友私聊则返回 None。"""

    text = str(value or "").strip()
    if not text:
        return None
    match = UMO_RE.match(text)
    if not match:
        return None
    return Umo(
        raw=text,
        platform_id=match.group("platform"),
        message_type=match.group("message_type"),
        session_id=match.group("session_id"),
    )


def make_umo(platform_id: str, message_type: str, session_id: object) -> str:
    return f"{platform_id}:{message_type}:{session_id}"


def parse_analysis_key(key: object) -> str | None:
    """从 analysis_<群号>_<日期> 里取出群号。"""

    match = ANALYSIS_KEY_RE.search(str(key or ""))
    if not match:
        return None
    return match.group("group_id")


def find_umos_in_text(value: object) -> set[str]:
    """从偏好值等 JSON 文本里找出 UMO；只返回可解析的群聊/好友 UMO。"""

    found: set[str] = set()
    for match in UMO_IN_TEXT_RE.finditer(str(value or "")):
        candidate = match.group("umo").rstrip("\\")
        if parse_umo(candidate) is not None:
            found.add(candidate)
    return found


def is_webchat_umo(value: object) -> bool:
    parsed = parse_umo(value)
    return bool(parsed and parsed.is_webchat)
