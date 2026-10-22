"""Dashboard 插件页面 API。"""

from __future__ import annotations

import asyncio
import re
import time
import uuid
from datetime import datetime, timezone
from typing import Any, Callable

from astrbot.api import logger

try:
    from astrbot.api.web import json_response as _json_response
    from astrbot.api.web import request as _request
except Exception:  # 兼容极旧版本；页面会提示 API 不可用
    _json_response = None
    _request = None

from .constants import PLUGIN_NAME
from .core.cleaner import CoreCleaner
from .core.config import cfg_bool, cfg_get, cfg_int, cfg_list
from .core.db import JanitorDB
from .core.detail import build_conversation_detail, build_session_detail
from .core.models import TABLE_KEYS
from .core.platform import PlatformListError, fetch_platform_snapshot
from .core.preview import build_preview
from .core.scanner import Scanner

ProgressCallback = Callable[[str, int, str], None]


def jsonify(payload: Any) -> Any:
    if _json_response is None:
        return payload
    return _json_response(payload)


def _query(name: str, default: str = "") -> str:
    if _request is None:
        return default
    try:
        value = _request.query.get(name, default)
    except Exception:
        return default
    return str(value if value is not None else default)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


class PageApi:
    def __init__(self, plugin: Any):
        self.plugin = plugin
        self.store = plugin.store
        self._scan_lock = asyncio.Lock()
        self._jobs: dict[str, dict[str, Any]] = {}

    def register(self) -> None:
        if _json_response is None or _request is None:
            logger.warning("当前 AstrBot 缺少 astrbot.api.web，会话清道夫页面 API 不可用")
            return
        register = getattr(self.plugin.context, "register_web_api", None)
        if not callable(register):
            logger.warning("当前 AstrBot 不支持 register_web_api，会话清道夫页面 API 不可用")
            return

        routes = (
            ("overview", self.overview, ["GET"], "扫描概览"),
            ("sessions", self.sessions, ["GET"], "会话列表"),
            ("session_detail", self.session_detail, ["GET"], "会话详情"),
            ("conversation_detail", self.conversation_detail, ["GET"], "核心库对话记录"),
            ("scan", self.scan, ["POST"], "只读扫描"),
            ("preview", self.preview, ["POST"], "清理预览（只读）"),
            ("clean_start", self.clean_start, ["POST"], "启动清理任务"),
            ("clean_status", self.clean_status, ["GET"], "清理任务进度"),
            ("clean", self.clean, ["POST"], "执行清理（同步）"),
            ("backups", self.backups, ["GET"], "备份列表"),
            ("rollback", self.rollback, ["POST"], "回滚备份"),
            ("logs", self.logs, ["GET"], "扫描日志"),
            ("settings", self.settings, ["GET"], "当前配置"),
            ("settings_save", self.settings_save, ["POST"], "保存配置"),
        )
        for route, handler, methods, desc in routes:
            register(f"/{PLUGIN_NAME}/{route}", handler, methods, desc)
        logger.info("会话清道夫页面 API 已注册：%d 个路由", len(routes))

    # ------------------------------------------------------------------
    # GET
    # ------------------------------------------------------------------

    async def overview(self) -> Any:
        scan = self.store.load()
        inspect = await asyncio.to_thread(JanitorDB(self.plugin.db_path).inspect)
        database = {
            "exists": bool(inspect.get("exists")),
            "tables": inspect.get("tables") or [],
            "counts": inspect.get("counts") or {},
        }
        if not scan:
            return jsonify(
                {
                    "ok": True,
                    "needs_scan": True,
                    "scan": None,
                    "database": database,
                    "config": self._config_view(),
                    "warnings": ["尚未扫描；点击“开始扫描”读取当前群/好友列表"],
                }
            )

        scan_view = {
            key: scan.get(key)
            for key in (
                "ok",
                "generated_at",
                "platform_id",
                "platform",
                "summary",
                "tables",
                "warnings",
            )
        }
        return jsonify(
            {
                "ok": True,
                "needs_scan": False,
                "scan": scan_view,
                "database": database,
                "config": self._config_view(),
                "warnings": scan.get("warnings") or [],
            }
        )

    async def sessions(self) -> Any:
        scan = self.store.load()
        if not scan:
            return jsonify({"ok": False, "error": "尚未扫描，请先执行只读扫描", "items": []})

        status = _query("status").strip()
        kind = _query("type").strip()
        query = _query("q").strip().lower()
        try:
            limit = max(1, min(2000, int(_query("limit", "500"))))
        except ValueError:
            limit = 500
        try:
            offset = max(0, int(_query("offset", "0")))
        except ValueError:
            offset = 0

        items = []
        for item in scan.get("targets") or []:
            if status and item.get("status") != status:
                continue
            if kind and item.get("kind") != kind:
                continue
            if query:
                haystack = " ".join(
                    str(item.get(key) or "")
                    for key in ("name", "umo", "session_id", "status_label")
                ).lower()
                if query not in haystack:
                    continue
            items.append({key: value for key, value in item.items() if key != "preference_row_ids"})

        total = len(items)
        return jsonify(
            {
                "ok": True,
                "generated_at": scan.get("generated_at"),
                "total": total,
                "offset": offset,
                "limit": limit,
                "items": items[offset : offset + limit],
            }
        )

    async def session_detail(self) -> Any:
        scan = self.store.load()
        if not scan:
            return jsonify({"ok": False, "error": "尚未扫描，请先执行只读扫描"})
        umo = _query("umo").strip()
        if not umo:
            return jsonify({"ok": False, "error": "缺少 umo 参数"})
        target = next(
            (item for item in (scan.get("targets") or []) if str(item.get("umo")) == umo),
            None,
        )
        if target is None:
            return jsonify({"ok": False, "error": "扫描结果中找不到这个会话，请重新扫描"})
        try:
            limit = max(1, min(100, int(_query("limit", "20"))))
        except ValueError:
            limit = 20
        try:
            detail = await asyncio.to_thread(
                build_session_detail,
                self.plugin.db_path,
                target,
                limit,
            )
        except Exception as exc:
            logger.exception("读取会话详情失败")
            return jsonify({"ok": False, "error": f"读取会话详情失败：{exc}"})
        return jsonify({"ok": True, "detail": detail})

    async def conversation_detail(self) -> Any:
        conversation_id = _query("conversation_id").strip()
        if not conversation_id:
            return jsonify({"ok": False, "error": "缺少 conversation_id 参数"})
        user_id = _query("umo").strip() or None
        try:
            limit = max(1, min(500, int(_query("limit", "100"))))
        except ValueError:
            limit = 100
        try:
            detail = await asyncio.to_thread(
                build_conversation_detail,
                self.plugin.db_path,
                conversation_id,
                user_id,
                limit,
            )
        except FileNotFoundError as exc:
            return jsonify({"ok": False, "error": str(exc)})
        except Exception as exc:
            logger.exception("读取核心库对话记录失败")
            return jsonify({"ok": False, "error": f"读取对话记录失败：{exc}"})
        return jsonify({"ok": True, "detail": detail})

    async def logs(self) -> Any:
        try:
            limit = max(1, min(500, int(_query("limit", "50"))))
        except ValueError:
            limit = 50
        return jsonify({"ok": True, "items": self.store.tail_logs(limit)})

    async def settings(self) -> Any:
        return jsonify(
            {
                "ok": True,
                "readonly": False,
                "config": self._config_view(),
                "notes": [
                    "本页保存后立即写入 AstrBot 插件配置，下次扫描/预览使用新配置。",
                    "会话清道夫只清理 AstrBot 核心库的数据，不读取也不修改任何插件数据。",
                    "已退群默认勾选；已删好友 / 过期临时会话默认不勾选，需要手动选择。",
                    "自动清理、自动扫描、执行后通知为后续版本预留，当前保存也不会执行。",
                ],
            }
        )

    async def settings_save(self) -> Any:
        body = await self._body()
        try:
            payload = self._normalize_settings(body)
        except ValueError as exc:
            return jsonify({"ok": False, "error": str(exc)})
        config = self.plugin.config
        try:
            save = getattr(config, "save_config_async", None)
            if callable(save):
                await save(replace_config=payload)
            elif isinstance(config, dict):
                config.update(payload)
            else:
                raise RuntimeError("当前 AstrBot 插件配置不支持保存")
        except Exception as exc:
            logger.exception("会话清道夫保存配置失败")
            return jsonify({"ok": False, "error": f"保存配置失败：{exc}"})
        self.store.append_log(
            {
                "event": "settings",
                "ok": True,
                "platform_id": payload["scan"]["platform_id"],
                "temp_session_days": payload["scan"]["temp_session_days"],
            }
        )
        return jsonify(
            {
                "ok": True,
                "message": "配置已保存",
                "config": self._config_view(),
            }
        )

    # ------------------------------------------------------------------
    # POST
    # ------------------------------------------------------------------

    async def scan(self) -> Any:
        async with self._scan_lock:
            return jsonify(await self._run_scan())

    async def preview(self) -> Any:
        scan = self.store.load()
        if not scan:
            return jsonify({"ok": False, "error": "尚未扫描，请先执行只读扫描"})

        body: dict[str, Any] = {}
        if _request is not None:
            value = await _request.json(default={})
            if isinstance(value, dict):
                body = value
        include_provider_stats = bool(
            body.get(
                "include_provider_stats",
                cfg_bool(self.plugin.config, "scan.include_provider_stats", False),
            )
        )
        return jsonify(
            {
                "ok": True,
                "preview": build_preview(
                    scan,
                    include_provider_stats=include_provider_stats,
                ),
            }
        )

    async def clean_start(self) -> Any:
        """创建后台清理任务并立即返回 job_id，避免前端长时间等待。"""
        body = await self._body()
        error = self._validate_clean_body(body)
        if error:
            return jsonify({"ok": False, "error": error})
        for job in self._jobs.values():
            if job.get("status") == "running":
                return jsonify(
                    {
                        "ok": False,
                        "error": "已有清理任务正在执行，请等待完成",
                        "job_id": job.get("job_id"),
                    }
                )
        self._prune_jobs()
        job_id = uuid.uuid4().hex[:12]
        now = time.time()
        self._jobs[job_id] = {
            "job_id": job_id,
            "status": "running",
            "stage": "排队中",
            "percent": 0,
            "message": "任务已创建",
            "started_at": now,
            "updated_at": now,
            "finished_at": None,
            "result": None,
            "error": None,
        }
        asyncio.create_task(self._run_clean_job(job_id, body))
        return jsonify({"ok": True, "job_id": job_id})

    async def clean_status(self) -> Any:
        job_id = _query("job_id").strip()
        job = self._jobs.get(job_id)
        if job is None:
            return jsonify({"ok": False, "error": "任务不存在或已过期"})
        return jsonify({"ok": True, "job": job})

    async def clean(self) -> Any:
        """同步清理入口，保留给脚本/旧页面使用。"""
        async with self._scan_lock:
            return jsonify(await self._execute_clean(await self._body()))

    async def backups(self) -> Any:
        try:
            limit = max(1, min(100, int(_query("limit", "20"))))
        except ValueError:
            limit = 20
        return jsonify({"ok": True, "items": self.plugin.backup_store.list(limit)})

    async def rollback(self) -> Any:
        async with self._scan_lock:
            body = await self._body()
            if str(body.get("confirm") or "").strip().upper() != "RESTORE":
                return jsonify({"ok": False, "error": "请输入 RESTORE 确认回滚"})
            backup_id = str(body.get("backup_id") or "").strip()
            if not backup_id:
                return jsonify({"ok": False, "error": "缺少 backup_id"})
            cleaner = CoreCleaner(self.plugin.db_path, self.plugin.backup_store)
            try:
                result = await asyncio.to_thread(cleaner.rollback, backup_id)
            except Exception as exc:
                message = f"回滚失败：{exc}"
                logger.exception(message)
                self.store.append_log({"event": "rollback", "ok": False, "error": message})
                return jsonify({"ok": False, "error": message})

            fresh: dict[str, Any] | None = None
            warning = None
            try:
                fresh = await self._perform_scan()
                self.store.save(fresh)
            except Exception as exc:
                warning = f"回滚已完成，但刷新扫描失败：{exc}"

            self.store.append_log(
                {
                    "event": "rollback",
                    "ok": True,
                    "backup_id": backup_id,
                    "restored": result.get("restored"),
                    "restored_files": result.get("restored_files"),
                }
            )
            return jsonify({"ok": True, "result": result, "scan": fresh, "warning": warning})

    # ------------------------------------------------------------------
    # 清理任务
    # ------------------------------------------------------------------

    def _validate_clean_body(self, body: dict[str, Any]) -> str | None:
        if str(body.get("confirm") or "").strip() != "确认删除":
            return "请输入“确认删除”四个字确认执行清理"
        targets = [str(item).strip() for item in (body.get("targets") or []) if str(item).strip()]
        if not targets:
            return "没有选择任何会话"
        return None

    async def _run_clean_job(self, job_id: str, body: dict[str, Any]) -> None:
        try:
            async with self._scan_lock:
                payload = await self._execute_clean(
                    body,
                    progress=lambda stage, percent, message: self._update_job(
                        job_id, stage, percent, message
                    ),
                )
            if payload.get("ok"):
                self._finish_job(job_id, "success", "清理完成", payload)
            else:
                self._finish_job(
                    job_id,
                    "error",
                    str(payload.get("error") or "清理失败"),
                    payload,
                )
        except Exception as exc:
            logger.exception("会话清道夫后台清理失败")
            self._finish_job(job_id, "error", f"清理失败：{exc}", {"ok": False, "error": str(exc)})

    async def _execute_clean(
        self,
        body: dict[str, Any],
        progress: ProgressCallback | None = None,
    ) -> dict[str, Any]:
        error = self._validate_clean_body(body)
        if error:
            return {"ok": False, "error": error}
        targets = [
            str(item).strip()
            for item in (body.get("targets") or [])
            if str(item).strip()
        ]
        include_provider_stats = bool(
            body.get(
                "include_provider_stats",
                cfg_bool(self.plugin.config, "scan.include_provider_stats", False),
            )
        )
        retain = cfg_int(self.plugin.config, "backup.retain", 5)

        self._progress(progress, "读取当前群/好友列表", 3, "正在读取平台列表")
        try:
            scan = await self._perform_scan()
        except Exception as exc:
            message = f"清理前扫描失败，已中止：{exc}"
            logger.warning(message)
            self.store.append_log({"event": "clean", "ok": False, "error": message})
            return {"ok": False, "error": message}
        self.store.save(scan)

        self._progress(progress, "扫描核心库", 12, "已读取群/好友列表，准备备份")
        cleaner = CoreCleaner(self.plugin.db_path, self.plugin.backup_store)

        def mapped_progress(stage: str, percent: int, message: str) -> None:
            self._progress(progress, stage, 15 + int(percent * 0.8), message)

        try:
            result = await asyncio.to_thread(
                cleaner.clean,
                scan,
                targets,
                include_provider_stats,
                retain,
                True,
                mapped_progress,
            )
        except Exception as exc:
            message = f"清理失败，事务已回滚：{exc}"
            logger.exception(message)
            self.store.append_log({"event": "clean", "ok": False, "error": message})
            return {"ok": False, "error": message}

        self._progress(progress, "刷新扫描结果", 96, "正在重新扫描")
        warning = None
        fresh: dict[str, Any] | None = None
        try:
            fresh = await self._perform_scan()
            self.store.save(fresh)
        except Exception as exc:
            warning = f"清理已完成，但刷新扫描失败：{exc}"

        self.store.append_log(
            {
                "event": "clean",
                "ok": True,
                "targets": len(targets),
                "backup_id": result.backup_id,
                "deleted": result.deleted,
                "counts": result.counts,
            }
        )
        self._progress(progress, "清理完成", 100, f"共删除 {result.deleted} 行")
        return {"ok": True, "result": result.to_dict(), "warning": warning}

    def _progress(
        self,
        progress: ProgressCallback | None,
        stage: str,
        percent: int,
        message: str,
    ) -> None:
        if progress is None:
            return
        try:
            progress(stage, max(0, min(100, int(percent))), message)
        except Exception:
            pass

    def _update_job(self, job_id: str, stage: str, percent: int, message: str) -> None:
        job = self._jobs.get(job_id)
        if job is None or job.get("status") != "running":
            return
        job["stage"] = stage
        job["percent"] = max(0, min(100, int(percent)))
        job["message"] = message
        job["updated_at"] = time.time()

    def _finish_job(
        self,
        job_id: str,
        status: str,
        message: str,
        result: dict[str, Any] | None,
    ) -> None:
        job = self._jobs.get(job_id)
        if job is None:
            return
        job["status"] = status
        job["stage"] = "完成" if status == "success" else "失败"
        job["percent"] = 100 if status == "success" else int(job.get("percent") or 0)
        job["message"] = message
        job["error"] = None if status == "success" else message
        job["result"] = result
        job["updated_at"] = time.time()
        job["finished_at"] = time.time()

    def _prune_jobs(self) -> None:
        now = time.time()
        finished = [
            (job_id, job)
            for job_id, job in self._jobs.items()
            if job.get("status") != "running"
        ]
        finished.sort(key=lambda pair: pair[1].get("finished_at") or 0)
        for job_id, job in finished[:-20]:
            self._jobs.pop(job_id, None)
        for job_id, job in list(self._jobs.items()):
            if job.get("status") != "running" and now - (job.get("finished_at") or 0) > 1800:
                self._jobs.pop(job_id, None)

    # ------------------------------------------------------------------
    # 扫描
    # ------------------------------------------------------------------

    async def _run_scan(self) -> dict[str, Any]:
        try:
            data = await self._perform_scan()
        except PlatformListError as exc:
            message = str(exc)
            logger.warning("会话清道夫扫描中止：%s", message)
            self.store.append_log(
                {"event": "scan", "ok": False, "stage": "platform", "error": message}
            )
            return {"ok": False, "stage": "platform", "error": message}
        except Exception as exc:
            message = f"读取平台列表失败：{exc}"
            logger.warning("会话清道夫扫描中止：%s", message)
            self.store.append_log(
                {"event": "scan", "ok": False, "stage": "platform", "error": message}
            )
            return {"ok": False, "stage": "platform", "error": message}

        self.store.save(data)
        self.store.append_log(
            {
                "event": "scan",
                "ok": True,
                "platform_id": data.get("platform_id"),
                "groups": (data.get("platform") or {}).get("groups"),
                "friends": (data.get("platform") or {}).get("friends"),
                "targets": (data.get("summary") or {}).get("total"),
                "candidates": (data.get("summary") or {}).get("candidates"),
            }
        )
        return {"ok": True, "scan": data}

    async def _perform_scan(self) -> dict[str, Any]:
        config = self.plugin.config or {}
        platform_id = str(cfg_get(config, "scan.platform_id", "default") or "default")
        timeout = max(1, cfg_int(config, "scan.scan_timeout_seconds", 10))
        temp_days = max(1, cfg_int(config, "scan.temp_session_days", 30))
        include_provider_stats = cfg_bool(
            config, "scan.include_provider_stats", False
        )
        whitelist = cfg_list(config, "scan.whitelist", [])

        snapshot = await fetch_platform_snapshot(
            self.plugin.context,
            platform_id=platform_id,
            timeout=float(timeout),
        )
        scanner = Scanner(
            db_path=self.plugin.db_path,
            platform_id=platform_id,
            temp_session_days=temp_days,
            include_provider_stats=include_provider_stats,
            whitelist=whitelist,
        )
        result = await asyncio.to_thread(scanner.scan, snapshot)
        return result.to_dict()

    async def _body(self) -> dict[str, Any]:
        if _request is None:
            return {}
        value = await _request.json(default={})
        return value if isinstance(value, dict) else {}

    # ------------------------------------------------------------------
    # helpers
    # ------------------------------------------------------------------

    def _normalize_settings(self, body: dict[str, Any]) -> dict[str, Any]:
        def as_int(key: str, default: int, low: int, high: int) -> int:
            try:
                value = int(body.get(key, default))
            except (TypeError, ValueError) as exc:
                raise ValueError(f"{key} 必须是整数") from exc
            return max(low, min(high, value))

        def as_bool(key: str, default: bool = False) -> bool:
            value = body.get(key, default)
            if isinstance(value, str):
                return value.strip().lower() in {"1", "true", "yes", "on", "开启"}
            return bool(value)

        def as_list(key: str) -> list[str]:
            value = body.get(key)
            if isinstance(value, str):
                parts = re.split(r"[\n,，]+", value)
            elif isinstance(value, list):
                parts = [str(item) for item in value]
            else:
                parts = []
            return [part.strip() for part in parts if part.strip()]

        platform_id = str(body.get("platform_id") or "default").strip() or "default"
        return {
            "scan": {
                "platform_id": platform_id,
                "temp_session_days": as_int("temp_session_days", 30, 1, 365),
                "include_provider_stats": as_bool("include_provider_stats", False),
                "whitelist": as_list("whitelist"),
                "scan_timeout_seconds": as_int("scan_timeout_seconds", 10, 3, 60),
            },
            "automation": {
                "auto_clean_enabled": as_bool("auto_clean_enabled", False),
                "scan_interval_minutes": as_int("scan_interval_minutes", 0, 0, 1440),
            },
            "notify": {
                "enabled": as_bool("notify_enabled", True),
                "targets": as_list("notify_targets"),
            },
            "backup": {
                "retain": as_int("backup_retain", 5, 1, 30),
            },
        }

    def _config_view(self) -> dict[str, Any]:
        config = self.plugin.config or {}
        return {
            "platform_id": str(cfg_get(config, "scan.platform_id", "default") or "default"),
            "temp_session_days": cfg_int(config, "scan.temp_session_days", 30),
            "include_provider_stats": cfg_bool(
                config, "scan.include_provider_stats", False
            ),
            "whitelist": cfg_list(config, "scan.whitelist", []),
            "scan_timeout_seconds": cfg_int(config, "scan.scan_timeout_seconds", 10),
            "auto_clean_enabled": cfg_bool(
                config, "automation.auto_clean_enabled", False
            ),
            "scan_interval_minutes": cfg_int(
                config, "automation.scan_interval_minutes", 0
            ),
            "notify_enabled": cfg_bool(config, "notify.enabled", True),
            "notify_targets": cfg_list(config, "notify.targets", []),
            "backup_retain": cfg_int(config, "backup.retain", 5),
            "tables": list(TABLE_KEYS),
        }
