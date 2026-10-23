"""安全读取 AstrBot 插件配置。"""

from __future__ import annotations

from typing import Any

# 自动清理间隔的默认值。0 不再表示“关闭”，关闭请用 auto_clean_enabled 开关。
DEFAULT_SCAN_INTERVAL_MINUTES = 60


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


def effective_scan_interval(source: Any) -> int:
    """自动清理间隔；旧配置里的 0/负数按默认 60 处理。

    0.7.3 起 0 不再表示“关闭”，但 0.7.2 及更早的配置可能仍存着 0。
    调度、状态展示和配置迁移统一走这个函数，避免出现“显示每 60 分钟、
    实际永不执行”的旧配置。
    """

    interval = cfg_int(
        source,
        "automation.scan_interval_minutes",
        DEFAULT_SCAN_INTERVAL_MINUTES,
    )
    return interval if interval > 0 else DEFAULT_SCAN_INTERVAL_MINUTES


def strict_bool(value: Any, default: bool = False) -> bool:
    """严格解析布尔值；字符串 "false" 不会被误判成 True。"""

    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value != 0
    if isinstance(value, str):
        return value.strip().lower() in {
            "1",
            "true",
            "yes",
            "on",
            "是",
            "开",
            "开启",
        }
    return default


def cfg_bool(source: Any, key: str, default: bool) -> bool:
    return strict_bool(cfg_get(source, key, default), default)


def cfg_list(source: Any, key: str, default: list[Any] | None = None) -> list[Any]:
    value = cfg_get(source, key, default if default is not None else [])
    if isinstance(value, list):
        return value
    if isinstance(value, str):
        return [item.strip() for item in value.replace("，", ",").split(",") if item.strip()]
    return []
