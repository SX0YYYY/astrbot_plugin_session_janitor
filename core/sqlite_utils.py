"""SQLite 连接字符串工具。"""

from __future__ import annotations

from pathlib import Path


def readonly_uri(path: str | Path) -> str:
    """返回可安全处理空格、中文和特殊字符的只读 URI。"""

    return Path(path).resolve().as_uri() + "?mode=ro"
