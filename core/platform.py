"""通过 AstrBot 的 aiocqhttp 适配器读取当前群列表和好友列表。

安全规则（方案第二节）：列表拿不到、超时或返回空列表时立即停止扫描，
不产生任何候选。这里只调用只读的 OneBot ``get_group_list`` /
``get_friend_list``，不会修改任何平台状态。
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from typing import Any

from .models import PlatformSnapshot


class PlatformListError(RuntimeError):
    """平台列表不可用时抛出；调用方必须停止扫描。"""


def _meta_value(adapter: Any, key: str) -> str:
    try:
        meta = adapter.meta() if callable(getattr(adapter, "meta", None)) else getattr(adapter, "meta", None)
    except Exception:
        meta = None
    if isinstance(meta, dict):
        return str(meta.get(key) or "")
    return str(getattr(meta, key, "") or "")


def _iter_adapters(context: Any) -> list[Any]:
    adapters: list[Any] = []
    try:
        direct = context.get_platform_inst("")
        if direct is not None:
            adapters.append(direct)
    except Exception:
        pass

    manager = getattr(context, "platform_manager", None)
    if manager is not None:
        for method_name in ("get_insts", "platform_insts"):
            try:
                value = getattr(manager, method_name, None)
                if callable(value):
                    value = value()
                if value:
                    adapters.extend(list(value))
            except Exception:
                continue
    # 去重，保持原顺序
    seen: set[int] = set()
    unique: list[Any] = []
    for adapter in adapters:
        if id(adapter) in seen:
            continue
        seen.add(id(adapter))
        unique.append(adapter)
    return unique


def find_adapter(context: Any, platform_id: str) -> Any | None:
    target = str(platform_id or "").strip()

    if target:
        try:
            direct = context.get_platform_inst(target)
            if direct is not None:
                return direct
        except Exception:
            pass

    adapters = _iter_adapters(context)
    if target:
        for adapter in adapters:
            if _meta_value(adapter, "id") == target:
                return adapter
    for adapter in adapters:
        if _meta_value(adapter, "name") == "aiocqhttp":
            return adapter
    return adapters[0] if adapters else None


def _get_client(adapter: Any) -> Any:
    get_client = getattr(adapter, "get_client", None)
    if callable(get_client):
        try:
            client = get_client()
            if client is not None:
                return client
        except Exception:
            pass
    for attr in ("bot", "client"):
        client = getattr(adapter, attr, None)
        if client is not None and hasattr(client, "call_action"):
            return client
    raise PlatformListError("aiocqhttp 适配器未提供可用的 OneBot 客户端")


async def _call_action(client: Any, action: str, timeout: float) -> Any:
    try:
        return await asyncio.wait_for(client.call_action(action), timeout=timeout)
    except asyncio.TimeoutError as exc:
        raise PlatformListError(f"{action} 超时（{timeout:g} 秒），已停止扫描") from exc
    except Exception as exc:
        raise PlatformListError(f"{action} 调用失败：{exc}，已停止扫描") from exc


def _unwrap(payload: Any) -> Any:
    if isinstance(payload, dict):
        for key in ("data", "result"):
            value = payload.get(key)
            if isinstance(value, (list, dict)):
                return value
    return payload


def _normalize_groups(payload: Any) -> dict[str, str]:
    payload = _unwrap(payload)
    if isinstance(payload, dict):
        for key in ("group_list", "groups", "list"):
            if isinstance(payload.get(key), list):
                payload = payload[key]
                break
        else:
            payload = list(payload.values())

    groups: dict[str, str] = {}
    if not isinstance(payload, list):
        return groups
    for item in payload:
        if isinstance(item, dict):
            group_id = item.get("group_id") or item.get("group") or item.get("id")
            name = item.get("group_name") or item.get("name") or ""
        else:
            group_id, name = item, ""
        if group_id is None:
            continue
        groups[str(group_id)] = str(name or group_id)
    return groups


def _normalize_friends(payload: Any) -> dict[str, str]:
    payload = _unwrap(payload)
    if isinstance(payload, dict):
        for key in ("friend_list", "friends", "list"):
            if isinstance(payload.get(key), list):
                payload = payload[key]
                break
        else:
            payload = list(payload.values())

    friends: dict[str, str] = {}
    if not isinstance(payload, list):
        return friends
    for item in payload:
        if isinstance(item, dict):
            user_id = item.get("user_id") or item.get("uin") or item.get("id")
            name = item.get("remark") or item.get("nickname") or item.get("name") or ""
        else:
            user_id, name = item, ""
        if user_id is None:
            continue
        friends[str(user_id)] = str(name or user_id)
    return friends


async def fetch_platform_snapshot(
    context: Any,
    platform_id: str = "default",
    timeout: float = 10.0,
) -> PlatformSnapshot:
    """读取当前群/好友列表；任一列表为空即抛错，调用方必须停止扫描。"""

    adapter = find_adapter(context, platform_id)
    if adapter is None:
        raise PlatformListError("未找到 aiocqhttp 平台实例，已停止扫描")
    client = _get_client(adapter)

    raw_groups = await _call_action(client, "get_group_list", timeout)
    raw_friends = await _call_action(client, "get_friend_list", timeout)

    groups = _normalize_groups(raw_groups)
    friends = _normalize_friends(raw_friends)
    if not groups:
        raise PlatformListError("get_group_list 返回空列表，已停止扫描")
    if not friends:
        raise PlatformListError("get_friend_list 返回空列表，已停止扫描")

    meta_name = _meta_value(adapter, "name") or "aiocqhttp"
    meta_id = _meta_value(adapter, "id")
    return PlatformSnapshot(
        platform_id=str(platform_id or meta_id or "default"),
        groups=groups,
        friends=friends,
        captured_at=datetime.now(timezone.utc).isoformat(),
        source=meta_name,
    )
