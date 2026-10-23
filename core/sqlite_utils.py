"""SQLite 连接字符串工具。"""

from __future__ import annotations

from pathlib import Path


def readonly_uri(path: str | Path) -> str:
    """返回可安全处理空格、中文和特殊字符的只读 URI。"""

    return Path(path).resolve().as_uri() + "?mode=ro"


def write_uri(path: str | Path) -> str:
    """返回只打开已存在文件的写 URI；不会像默认 connect 那样创建空库。"""

    return Path(path).resolve().as_uri() + "?mode=rw"
