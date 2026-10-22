"""会话详情：只读查询 AstrBot 核心库（data_v4.db）。

不读取、不展示、不修改任何插件数据。QQ 群/好友的聊天正文不在核心库中，
因此详情页只展示核心库实际保存的对话记录、偏好记录和平台消息历史。
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any

from .db import JanitorDB, _as_iso_and_ts, preference_targets
from .sqlite_utils import readonly_uri

PREVIEW_CHARS = 400
MAX_MESSAGES = 20


def _preview(value: Any, limit: int = PREVIEW_CHARS) -> str:
    if value is None:
        return ""
    if isinstance(value, (dict, list)):
        try:
            text = json.dumps(value, ensure_ascii=False)
        except (TypeError, ValueError):
            text = str(value)
    else:
        text = str(value)
    text = text.replace("\r\n", "\n").strip()
    if len(text) > limit:
        return text[:limit] + "…"
    return text


def _connect(path: Path) -> sqlite3.Connection:
    connection = sqlite3.connect(readonly_uri(path), uri=True, timeout=10)
    connection.row_factory = sqlite3.Row
    return connection


def _table_exists(connection: sqlite3.Connection, table: str) -> bool:
    row = connection.execute(
        "select 1 from sqlite_master where type='table' and name=? limit 1",
        (table,),
    ).fetchone()
    return row is not None


def build_session_detail(
    db_path: str | Path,
    target: dict[str, Any],
    limit: int = MAX_MESSAGES,
) -> dict[str, Any]:
    db = Path(db_path)
    limit = max(1, min(100, int(limit)))
    umo = str(target.get("umo") or "")
    platform_id = str(target.get("platform_id") or "")
    detail: dict[str, Any] = {
        "aliases": [],
        "conversations": [],
        "preferences": [],
        "provider_stats": {},
        "core_messages": [],
        "warnings": [],
    }

    if db.is_file():
        connection = _connect(db)
        try:
            if _table_exists(connection, "umo_aliases"):
                try:
                    rows = connection.execute(
                        "select umo, creator_sender_id, auto_name, user_alias, created_at, updated_at "
                        "from umo_aliases where umo = ?",
                        (umo,),
                    ).fetchall()
                    for row in rows:
                        detail["aliases"].append(
                            {
                                "umo": row["umo"],
                                "creator_sender_id": row["creator_sender_id"],
                                "auto_name": row["auto_name"],
                                "user_alias": row["user_alias"],
                                "created_at": row["created_at"],
                                "updated_at": row["updated_at"],
                            }
                        )
                except sqlite3.Error as exc:
                    detail["warnings"].append(f"读取会话别名失败：{exc}")

            if _table_exists(connection, "conversations"):
                try:
                    rows = connection.execute(
                        "select conversation_id, title, length(content) as content_length, "
                        "substr(content, 1, ?) as content_preview, token_usage, created_at, updated_at "
                        "from conversations where user_id = ? order by updated_at desc",
                        (PREVIEW_CHARS, umo),
                    ).fetchall()
                    for row in rows:
                        detail["conversations"].append(
                            {
                                "conversation_id": row["conversation_id"],
                                "title": row["title"],
                                "content_length": int(row["content_length"] or 0),
                                "content_preview": _preview(row["content_preview"]),
                                "token_usage": row["token_usage"],
                                "created_at": row["created_at"],
                                "updated_at": row["updated_at"],
                            }
                        )
                except sqlite3.Error as exc:
                    detail["warnings"].append(f"读取对话记录失败：{exc}")

            if _table_exists(connection, "provider_stats"):
                try:
                    row = connection.execute(
                        "select count(*) as count, min(end_time) as first_ts, max(end_time) as last_ts "
                        "from provider_stats where umo = ?",
                        (umo,),
                    ).fetchone()
                    first_iso, _ = _as_iso_and_ts(row["first_ts"])
                    last_iso, _ = _as_iso_and_ts(row["last_ts"])
                    detail["provider_stats"] = {
                        "count": int(row["count"] or 0),
                        "first_at": first_iso,
                        "last_at": last_iso,
                    }
                except sqlite3.Error as exc:
                    detail["warnings"].append(f"读取调用统计失败：{exc}")

            if _table_exists(connection, "platform_message_history"):
                try:
                    rows = connection.execute(
                        "select sender_id, sender_name, content, created_at "
                        "from platform_message_history "
                        "where platform_id = ? and user_id = ? "
                        "order by created_at desc limit ?",
                        (platform_id, umo, limit),
                    ).fetchall()
                    for row in rows:
                        detail["core_messages"].append(
                            {
                                "sender_id": row["sender_id"],
                                "sender_name": row["sender_name"],
                                "content": _preview(row["content"]),
                                "at": row["created_at"],
                            }
                        )
                except sqlite3.Error as exc:
                    detail["warnings"].append(f"读取平台消息历史失败：{exc}")
        finally:
            connection.close()
    else:
        detail["warnings"].append("未找到 AstrBot 主库 data_v4.db")

    try:
        for row in JanitorDB(db).preferences():
            target_umos = preference_targets(row, platform_id)
            if umo not in target_umos:
                continue
            detail["preferences"].append(
                {
                    "row_id": row.row_id,
                    "scope": row.scope,
                    "scope_id": row.scope_id,
                    "key": row.key,
                    "value_length": len(row.value_text or ""),
                    "value_preview": _preview(row.value_text),
                    "last_active": row.last_active,
                    "shared": len(target_umos) > 1,
                }
            )
    except Exception as exc:  # 详情是辅助视图，读取失败不影响主流程
        detail["warnings"].append(f"读取偏好记录失败：{exc}")

    alias_names: list[str] = []
    for item in detail["aliases"]:
        for key in ("auto_name", "user_alias"):
            value = str(item.get(key) or "").strip()
            if value and value not in alias_names:
                alias_names.append(value)

    messages = detail.pop("core_messages")
    return {
        "umo": target.get("umo"),
        "platform_id": target.get("platform_id"),
        "message_type": target.get("message_type"),
        "session_id": target.get("session_id"),
        "kind": target.get("kind"),
        "status": target.get("status"),
        "status_label": target.get("status_label"),
        "name": target.get("name"),
        "last_active": target.get("last_active"),
        "selectable": bool(target.get("selectable")),
        "default_selected": bool(target.get("default_selected")),
        "notes": target.get("notes") or [],
        "counts": target.get("counts") or {},
        "alias_names": alias_names,
        "aliases": detail["aliases"],
        "conversations": detail["conversations"],
        "preferences": detail["preferences"],
        "provider_stats": detail["provider_stats"],
        "core_messages": messages,
        "warnings": detail["warnings"],
    }


ROLE_LABELS = {
    "user": "用户",
    "assistant": "助手",
    "system": "系统",
    "tool": "工具",
}


def _parse_message_blocks(value: Any) -> dict[str, Any]:
    """把 conversations.content 里的单个消息内容整理成可读块。"""

    texts: list[str] = []
    thinks: list[str] = []
    tools: list[str] = []
    images = 0

    if isinstance(value, str):
        try:
            blocks: Any = json.loads(value)
        except ValueError:
            blocks = [{"type": "text", "text": value}]
    else:
        blocks = value

    if not isinstance(blocks, list):
        blocks = [blocks]

    for block in blocks:
        if isinstance(block, str):
            texts.append(block)
            continue
        if not isinstance(block, dict):
            texts.append(str(block))
            continue
        kind = str(block.get("type") or "")
        if kind == "text":
            text = block.get("text")
            if text is not None:
                texts.append(str(text))
        elif kind == "think":
            think = block.get("think") or block.get("text")
            if think:
                thinks.append(str(think))
        elif kind in {"image", "image_url"}:
            images += 1
        elif "tool" in kind or kind in {"function_call", "function_result"}:
            tools.append(json.dumps(block, ensure_ascii=False, indent=2))
        else:
            text = block.get("text") or block.get("content")
            if text:
                texts.append(str(text))
            else:
                tools.append(json.dumps(block, ensure_ascii=False, indent=2))

    return {
        "texts": texts,
        "thinks": thinks,
        "tools": tools,
        "images": images,
    }


def build_conversation_detail(
    db_path: str | Path,
    conversation_id: str,
    user_id: str | None = None,
    limit: int = 100,
) -> dict[str, Any]:
    """读取核心库 conversations.content，解析成可读的聊天记录。"""

    db = Path(db_path)
    limit = max(1, min(500, int(limit)))
    if not db.is_file():
        raise FileNotFoundError("未找到 AstrBot 主库 data_v4.db")
    connection = _connect(db)
    try:
        if user_id:
            row = connection.execute(
                "select conversation_id, title, platform_id, user_id, content, "
                "token_usage, created_at, updated_at from conversations "
                "where conversation_id = ? and user_id = ? "
                "order by updated_at desc limit 1",
                (conversation_id, user_id),
            ).fetchone()
        else:
            row = connection.execute(
                "select conversation_id, title, platform_id, user_id, content, "
                "token_usage, created_at, updated_at from conversations "
                "where conversation_id = ? order by updated_at desc limit 1",
                (conversation_id,),
            ).fetchone()
    finally:
        connection.close()
    if row is None:
        raise FileNotFoundError("核心库里找不到这条对话记录")

    raw = row["content"]
    warnings: list[str] = []
    if isinstance(raw, str):
        try:
            parsed = json.loads(raw)
        except ValueError:
            parsed = []
            warnings.append("对话内容不是标准 JSON，下面显示原始文本的前 2000 字。")
    else:
        parsed = raw

    messages: list[dict[str, Any]] = []
    if isinstance(parsed, list):
        for item in parsed:
            if not isinstance(item, dict):
                continue
            role = str(item.get("role") or "")
            blocks = _parse_message_blocks(item.get("content"))
            tool_calls = item.get("tool_calls")
            if isinstance(tool_calls, list):
                for call in tool_calls:
                    blocks["tools"].append(json.dumps(call, ensure_ascii=False, indent=2))
            messages.append(
                {
                    "role": role,
                    "role_label": ROLE_LABELS.get(role, role or "未知"),
                    **blocks,
                }
            )
    elif isinstance(raw, str) and raw and not warnings:
        messages.append(
            {
                "role": "unknown",
                "role_label": "原始内容",
                "texts": [_preview(raw, 2000)],
                "thinks": [],
                "tools": [],
                "images": 0,
            }
        )

    total = len(messages)
    shown = messages[-limit:]
    return {
        "conversation_id": row["conversation_id"],
        "title": row["title"],
        "platform_id": row["platform_id"],
        "user_id": row["user_id"],
        "token_usage": row["token_usage"],
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
        "message_count": total,
        "shown_messages": len(shown),
        "truncated": total > len(shown),
        "messages": shown,
        "warnings": warnings,
    }
