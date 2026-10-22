"""扫描结果与操作日志的本地存储。

V0.1 只保存只读扫描结果，不保存消息正文。文件位于
``data/plugin_data/astrbot_plugin_session_janitor/``。
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


class ScanStore:
    MAX_LOG_LINES = 1000

    def __init__(self, data_dir: str | Path):
        self.data_dir = Path(data_dir)
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.result_file = self.data_dir / "scan_result.json"
        self.log_file = self.data_dir / "scan_log.jsonl"

    def save(self, result: dict[str, Any]) -> None:
        tmp = self.result_file.with_suffix(".json.tmp")
        with tmp.open("w", encoding="utf-8") as handle:
            json.dump(result, handle, ensure_ascii=False, indent=2)
        os.replace(tmp, self.result_file)

    def load(self) -> dict[str, Any] | None:
        if not self.result_file.is_file():
            return None
        try:
            with self.result_file.open(encoding="utf-8") as handle:
                data = json.load(handle)
        except (OSError, ValueError):
            return None
        return data if isinstance(data, dict) else None

    def append_log(self, event: dict[str, Any]) -> None:
        payload = dict(event)
        payload.setdefault("ts", datetime.now(timezone.utc).isoformat())
        try:
            with self.log_file.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(payload, ensure_ascii=False) + "\n")
            self._trim_log()
        except OSError:
            pass

    def tail_logs(self, limit: int = 50) -> list[dict[str, Any]]:
        if not self.log_file.is_file():
            return []
        try:
            lines = self.log_file.read_text(encoding="utf-8").splitlines()
        except OSError:
            return []
        items: list[dict[str, Any]] = []
        for line in lines[-max(1, int(limit)):]:
            try:
                value = json.loads(line)
            except ValueError:
                continue
            if isinstance(value, dict):
                items.append(value)
        return list(reversed(items))

    def _trim_log(self) -> None:
        try:
            lines = self.log_file.read_text(encoding="utf-8").splitlines()
        except OSError:
            return
        if len(lines) <= self.MAX_LOG_LINES:
            return
        keep = lines[-self.MAX_LOG_LINES:]
        tmp = self.log_file.with_suffix(".jsonl.tmp")
        try:
            tmp.write_text("\n".join(keep) + "\n", encoding="utf-8")
            os.replace(tmp, self.log_file)
        except OSError:
            pass
