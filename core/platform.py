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


def _require_aiocqhttp(adapter: Any) -> Any:
    name = _meta_value(adapter, "name").strip().lower()
    if name != "aiocqhttp":
        shown = name or type(adapter).__name__
        raise PlatformListError(
            f"当前平台实例是 {shown}，不是 aiocqhttp（OneBot/NapCat），无法读取群列表"
        )
    return adapter


def find_adapter(context: Any, platform_id: str) -> Any | None:
    target = str(platform_id or "").strip()

    if target:
        try:
            direct = context.get_platform_inst(target)
            if direct is not None:
                return _require_aiocqhttp(direct)
        except PlatformListError:
            raise
        except Exception:
            pass

    adapters = _iter_adapters(context)
    if target:
        for adapter in adapters:
            if _meta_value(adapter, "id") == target:
                return _require_aiocqhttp(adapter)

    candidates = [
        adapter
        for adapter in adapters
        if _meta_value(adapter, "name").strip().lower() == "aiocqhttp"
    ]
    if len(candidates) == 1:
        return candidates[0]
    if len(candidates) > 1:
        raise PlatformListError(
            "检测到多个 aiocqhttp 平台实例，请在设置中明确指定平台 ID"
        )
    raise PlatformListError("未找到 aiocqhttp（OneBot/NapCat）平台实例，已停止扫描")


def list_platforms(context: Any) -> list[dict[str, Any]]:
    """列出当前 AstrBot 已加载的平台实例，供设置页下拉选择。

    只读取适配器自身的 meta 信息，不调用任何平台接口，也不修改平台状态。
    """

    items: list[dict[str, Any]] = []
    seen: set[str] = set()
    for adapter in _iter_adapters(context):
        platform_id = _meta_value(adapter, "id").strip()
        name = _meta_value(adapter, "name").strip()
        if not platform_id and not name:
            name = type(adapter).__name__
        key = platform_id or name
        if not key or key in seen:
            continue
        seen.add(key)
        items.append(
            {
                "id": platform_id or name,
                "name": name or platform_id,
                "source": "adapter",
                "supported": name.lower() == "aiocqhttp",
            }
        )
    return items


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
            # 只接受明确的列表字段；错误包（status/retcode/message）不能
            # 被当成群列表，否则会把失败响应当成一堆伪群号。
            return {}

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
            return {}

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

    warnings: list[str] = []
    if not friends:
        # 好友列表为空可能是合法的（新 bot 没有好友），也可能只是接口异常。
        # 群聊清理不依赖好友列表；私聊会按“好友列表外”处理且默认不勾选，
        # 因此允许继续，但必须在界面和日志里明确提示。
        warnings.append(
            "get_friend_list 返回空列表：私聊会话将全部按好友列表外处理，"
            "默认不勾选；如确认好友列表不应为空，请先检查平台接口"
        )

    meta_name = _meta_value(adapter, "name") or "aiocqhttp"
    meta_id = _meta_value(adapter, "id")
    resolved_id = str(meta_id or platform_id or "default")
    if platform_id and meta_id and str(platform_id) != meta_id:
        warnings.append(
            f"配置的平台 ID 是 {platform_id}，实际适配器 ID 是 {meta_id}；"
            "已按实际适配器 ID 扫描"
        )
    return PlatformSnapshot(
        platform_id=resolved_id,
        groups=groups,
        friends=friends,
        captured_at=datetime.now(timezone.utc).isoformat(),
        source=meta_name,
        warnings=warnings,
    )
