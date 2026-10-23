"""自动清理调度器。

- `automation.auto_clean_enabled` 打开后，按 `scan_interval_minutes` 周期执行。
- 只处理「已退群」且默认可清理的会话，不动已删好友/临时会话。
- 默认不删除 provider_stats；可在自动清理设置里单独打开。
- 执行后按 `notify` 配置发送摘要；通知失败不影响清理结果。
"""

from __future__ import annotations

import asyncio
import json
import os
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from astrbot.api import logger

from .config import cfg_bool, cfg_int


def _float(value: Any) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


class AutoCleaner:
    def __init__(self, plugin: Any, page_api: Any):
        self.plugin = plugin
        self.page_api = page_api
        self._task: asyncio.Task | None = None
        self._stop = asyncio.Event()
        self._running = False
        self._last_run: float | None = None
        self._next_run: float | None = None
        self._last_result: dict[str, Any] | None = None
        self._state_key: tuple[bool, int] | None = None
        data_dir = getattr(plugin, "data_dir", None)
        self._state_file: Path | None = (
            Path(data_dir) / "automation_state.json" if data_dir is not None else None
        )
        self._load_state()

    def _load_state(self) -> None:
        if self._state_file is None or not self._state_file.is_file():
            return
        try:
            data = json.loads(self._state_file.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return
        if not isinstance(data, dict):
            return
        last_run = _float(data.get("last_run"))
        if last_run is not None:
            self._last_run = last_run
        if isinstance(data.get("last_result"), dict):
            self._last_result = data["last_result"]

        config = getattr(self.plugin, "config", None) or {}
        enabled = cfg_bool(config, "automation.auto_clean_enabled", False)
        interval = cfg_int(config, "automation.scan_interval_minutes", 0)
        if enabled and interval > 0:
            # 恢复配置键，避免 _loop 第一次 tick 把持久化的 next_run
            # 重置成“现在 + 一个完整间隔”。
            self._state_key = (True, int(interval))
            next_run = _float(data.get("next_run"))
            if next_run is not None:
                self._next_run = next_run

    def _save_state(self) -> None:
        if self._state_file is None:
            return
        payload = {
            "last_run": self._last_run,
            "last_result": self._last_result,
            "next_run": self._next_run,
        }
        try:
            self._state_file.parent.mkdir(parents=True, exist_ok=True)
            tmp = self._state_file.with_suffix(".json.tmp")
            tmp.write_text(
                json.dumps(payload, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            os.replace(tmp, self._state_file)
        except OSError:
            pass

    def start(self) -> None:
        if self._task is not None and not self._task.done():
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            logger.warning("会话清道夫：当前没有事件循环，自动清理未启动")
            return
        self._stop.clear()
        self._task = loop.create_task(self._loop())
        logger.info("会话清道夫自动清理调度器已启动")

    async def stop(self) -> None:
        self._stop.set()
        task = self._task
        self._task = None
        if task is not None and not task.done():
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
        logger.info("会话清道夫自动清理调度器已停止")

    def refresh(self) -> None:
        """配置保存后立即重算下次运行时间，不必等 20 秒轮询。"""
        config = self.plugin.config or {}
        enabled = cfg_bool(config, "automation.auto_clean_enabled", False)
        interval = cfg_int(config, "automation.scan_interval_minutes", 0)
        if not enabled or interval <= 0:
            changed = self._next_run is not None or self._state_key is not None
            self._next_run = None
            self._state_key = None
            if changed:
                self._save_state()
            return
        key = (True, int(interval))
        if key != self._state_key or self._next_run is None:
            self._state_key = key
            self._next_run = time.time() + interval * 60
            self._save_state()

    def status(self) -> dict[str, Any]:
        config = self.plugin.config or {}
        enabled = cfg_bool(config, "automation.auto_clean_enabled", False)
        interval = cfg_int(config, "automation.scan_interval_minutes", 0)
        return {
            "enabled": bool(enabled),
            "interval_minutes": int(interval),
            "include_provider_stats": cfg_bool(
                config, "automation.include_provider_stats", False
            ),
            "running": self._running,
            "last_run": _iso(self._last_run),
            "next_run": _iso(self._next_run) if enabled and interval > 0 else None,
            "last_result": self._last_result,
        }

    async def run_once(self) -> dict[str, Any]:
        if self._running:
            return {"ok": False, "error": "自动清理正在执行，请稍后再试"}
        self._running = True
        try:
            result = await self.page_api.run_auto_clean()
        except Exception as exc:
            logger.exception("会话清道夫自动清理失败")
            result = {"ok": False, "error": f"自动清理失败：{exc}"}
        finally:
            self._running = False
        self._last_run = time.time()
        self._last_result = result
        self._save_state()
        return result

    async def _loop(self) -> None:
        while True:
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=20)
                return
            except asyncio.TimeoutError:
                pass
            except asyncio.CancelledError:
                raise

            try:
                config = self.plugin.config or {}
                enabled = cfg_bool(config, "automation.auto_clean_enabled", False)
                interval = cfg_int(config, "automation.scan_interval_minutes", 0)
                key = (bool(enabled), int(interval))
                now = time.time()

                if not enabled or interval <= 0:
                    changed = self._next_run is not None or self._state_key is not None
                    self._next_run = None
                    self._state_key = None
                    if changed:
                        self._save_state()
                    continue

                if key != self._state_key or self._next_run is None:
                    self._state_key = key
                    self._next_run = now + interval * 60
                    self._save_state()
                    logger.info(
                        "会话清道夫自动清理已启用：每 %d 分钟执行一次", interval
                    )
                    continue

                if now >= self._next_run:
                    logger.info("会话清道夫开始执行定时自动清理")
                    await self.run_once()
                    self._next_run = time.time() + interval * 60
                    self._save_state()
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("会话清道夫自动清理循环异常")


def _iso(timestamp: float | None) -> str | None:
    if not timestamp:
        return None
    return datetime.fromtimestamp(timestamp, tz=timezone.utc).isoformat()
