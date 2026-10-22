"""安全读取 AstrBot 插件配置。"""

from __future__ import annotations

from typing import Any


def cfg_get(source: Any, key: str, default: Any = None) -> Any:
    current = source
    for part in str(key).split("."):
        if isinstance(current, dict) and part in current:
            current = current[part]
        else:
            return default
    return current


def cfg_int(source: Any, key: str, default: int) -> int:
    try:
        return int(cfg_get(source, key, default))
    except (TypeError, ValueError):
        return default


def cfg_bool(source: Any, key: str, default: bool) -> bool:
    value = cfg_get(source, key, default)
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "on", "是", "开"}
    return bool(value)


def cfg_list(source: Any, key: str, default: list[Any] | None = None) -> list[Any]:
    value = cfg_get(source, key, default if default is not None else [])
    if isinstance(value, list):
        return value
    if isinstance(value, str):
        return [item.strip() for item in value.replace("，", ",").split(",") if item.strip()]
    return []
