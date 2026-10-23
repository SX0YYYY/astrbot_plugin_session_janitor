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

try:
    from astrbot.core.message.components import Plain
    from astrbot.core.message.message_event_result import MessageChain
except Exception:  # 通知是可选项，缺少消息组件时只写日志
    Plain = None
    MessageChain = None

from .constants import PLUGIN_NAME, PLUGIN_VERSION
from .core.cleaner import CoreCleaner
from .core.config import cfg_bool, cfg_get, cfg_int, cfg_list, strict_bool
from .core.db import JanitorDB
from .core.detail import build_conversation_detail, build_session_detail
from .core.health import build_health
from .core.models import TABLE_KEYS
from .core.platform import PlatformListError, fetch_platform_snapshot, list_platforms
from .core.preview import TABLE_LABELS, build_preview
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
        self._job_tasks: dict[str, asyncio.Task] = {}

    async def shutdown(self) -> None:
        """插件卸载/重载时取消尚未完成的清理任务。"""

        tasks = list(self._job_tasks.values())
        self._job_tasks.clear()
        for task in tasks:
            if not task.done():
                task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        for job in self._jobs.values():
            if job.get("status") == "running":
                job["status"] = "error"
                job["stage"] = "已取消"
                job["message"] = "插件卸载，清理任务已取消"
                job["error"] = job["message"]
                job["finished_at"] = time.time()

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
            ("health_report", self.health_report, ["GET"], "数据库体检"),
            ("platforms", self.platforms, ["GET"], "可用平台实例"),
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
            ("automation_status", self.automation_status, ["GET"], "自动清理状态"),
            ("auto_clean_run", self.auto_clean_run, ["POST"], "立即运行一次自动清理"),
            ("notify_test", self.notify_test, ["POST"], "发送测试通知"),
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
        try:
            health = await asyncio.to_thread(
                build_health, self.plugin.db_path, scan, inspect
            )
        except Exception as exc:
            logger.exception("生成数据库体检失败")
            health = {"ok": False, "error": f"生成数据库体检失败：{exc}"}
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
                    "health": health,
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
                "changes",
            )
        }
        return jsonify(
            {
                "ok": True,
                "needs_scan": False,
                "scan": scan_view,
                "database": database,
                "health": health,
                "config": self._config_view(),
                "warnings": scan.get("warnings") or [],
            }
        )

    async def health_report(self) -> Any:
        scan = self.store.load()
        try:
            report = await asyncio.to_thread(
                build_health, self.plugin.db_path, scan
            )
        except Exception as exc:
            logger.exception("生成数据库体检失败")
            return jsonify({"ok": False, "error": f"生成数据库体检失败：{exc}"})
        return jsonify({"ok": True, "health": report})

    async def platforms(self) -> Any:
        """返回当前 AstrBot 已加载的平台实例，供设置页下拉选择。"""

        try:
            items = list_platforms(self.plugin.context)
        except Exception as exc:
            logger.warning("读取平台实例列表失败：%s", exc)
            items = []
        current = str(
            cfg_get(self.plugin.config, "scan.platform_id", "default") or "default"
        )
        if current and all(str(item.get("id")) != current for item in items):
            items.insert(
                0,
                {
                    "id": current,
                    "name": f"{current}（当前配置）",
                    "source": "config",
                },
            )
        recommended = next(
            (
                str(item.get("id"))
                for item in items
                if str(item.get("name")) == "aiocqhttp"
            ),
            current or "default",
        )
        return jsonify(
            {
                "ok": True,
                "items": items,
                "current": current,
                "recommended": recommended,
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
                    "自动清理只处理「已退群」的默认可清理会话；已删好友/临时会话不会自动清。",
                    "自动清理默认关闭；开启后按间隔执行，执行结果会写入扫描日志并按通知配置发送。",
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
        automation = getattr(self.plugin, "automation", None)
        refresh = getattr(automation, "refresh", None)
        if callable(refresh):
            refresh()
            self.store.append_log(
                {
                    "event": "settings",
                    "ok": True,
                    "platform_id": payload["scan"]["platform_id"],
                    "temp_session_days": payload["scan"]["temp_session_days"],
                    "operator": self._operator(),
                }
            )
        return jsonify(
            {
                "ok": True,
                "message": "配置已保存",
                "config": self._config_view(),
            }
        )

    async def automation_status(self) -> Any:
        automation = getattr(self.plugin, "automation", None)
        status = automation.status() if automation is not None else {
            "enabled": False,
            "interval_minutes": 0,
            "include_provider_stats": False,
            "running": False,
            "last_run": None,
            "next_run": None,
            "last_result": None,
        }
        return jsonify({"ok": True, "status": status})

    async def auto_clean_run(self) -> Any:
        body = await self._body()
        if str(body.get("confirm") or "").strip() != "确认运行":
            return jsonify({"ok": False, "error": "请输入“确认运行”四个字"})
        automation = getattr(self.plugin, "automation", None)
        if automation is None or not callable(getattr(automation, "run_once", None)):
            return jsonify({"ok": False, "error": "自动清理调度器未启动"})
        result = await automation.run_once()
        if result.get("ok"):
            return jsonify({"ok": True, "result": result})
        return jsonify(
            {
                "ok": False,
                "error": result.get("error") or "自动清理失败",
                "result": result,
            }
        )

    async def notify_test(self) -> Any:
        body = await self._body()
        if str(body.get("confirm") or "").strip() != "确认发送":
            return jsonify({"ok": False, "error": "请输入“确认发送”四个字"})
        result = await self._notify_auto_clean(
            {
                "deleted": 1,
                "targets": ["测试通知"],
                "counts": {"preferences": 1},
                "backup_id": "测试通知",
            },
            test=True,
        )
        if int(result.get("sent") or 0) > 0:
            return jsonify({"ok": True, "result": result})
        return jsonify(
            {
                "ok": False,
                "error": result.get("reason") or "测试通知发送失败",
                "result": result,
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
        default_include = cfg_bool(
            self.plugin.config, "scan.include_provider_stats", False
        )
        include_provider_stats = strict_bool(
            body.get("include_provider_stats", default_include),
            default_include,
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
        task = asyncio.create_task(self._run_clean_job(job_id, body))
        self._job_tasks[job_id] = task
        task.add_done_callback(lambda _task: self._job_tasks.pop(job_id, None))
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
            confirm = str(body.get("confirm") or "").strip()
            if confirm not in {"确认回滚", "RESTORE"}:
                return jsonify(
                    {"ok": False, "error": "请输入“确认回滚”四个字确认回滚"}
                )
            backup_id = str(body.get("backup_id") or "").strip()
            if not backup_id:
                return jsonify({"ok": False, "error": "缺少 backup_id"})
            cleaner = CoreCleaner(self.plugin.db_path, self.plugin.backup_store)
            try:
                result = await asyncio.to_thread(cleaner.rollback, backup_id)
            except ValueError as exc:
                message = f"回滚失败：{exc}"
                logger.warning(message)
                self.store.append_log(
                    {
                        "event": "rollback",
                        "ok": False,
                        "error": message,
                        "operator": self._operator(),
                    }
                )
                return jsonify({"ok": False, "error": message})
            except Exception as exc:
                message = f"回滚失败：{exc}"
                logger.exception(message)
                self.store.append_log(
                    {
                        "event": "rollback",
                        "ok": False,
                        "error": message,
                        "operator": self._operator(),
                    }
                )
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
                    "skipped_files": result.get("skipped_files"),
                    "safety_backup_id": result.get("safety_backup_id"),
                    "operator": self._operator(),
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

    async def run_auto_clean(self) -> dict[str, Any]:
        """按自动清理规则执行一次：只处理已退群会话。"""
        config = self.plugin.config or {}
        include_provider_stats = cfg_bool(
            config, "automation.include_provider_stats", False
        )
        async with self._scan_lock:
            try:
                scan = await self._perform_scan()
            except Exception as exc:
                message = f"自动清理扫描失败：{exc}"
                logger.warning(message)
                self.store.append_log(
                    {"event": "auto_clean", "ok": False, "error": message}
                )
                return {"ok": False, "error": message}
            self.store.save(scan)

            selected: list[str] = []
            for item in scan.get("targets") or []:
                if not item.get("selectable"):
                    continue
                if item.get("status") != "orphan_group":
                    continue
                counts = item.get("counts") or {}
                provider_rows = int(counts.get("provider_stats") or 0)
                core_rows = sum(
                    int(value or 0)
                    for table, value in counts.items()
                    if table != "provider_stats"
                )
                if core_rows > 0 or (include_provider_stats and provider_rows > 0):
                    selected.append(str(item.get("umo")))

            if not selected:
                result = {
                    "ok": True,
                    "skipped": True,
                    "reason": "没有可自动清理的已退群会话",
                    "candidates": int((scan.get("summary") or {}).get("candidates") or 0),
                    "deleted": 0,
                }
                self.store.append_log(
                    {
                        "event": "auto_clean",
                        "ok": True,
                        "skipped": True,
                        "candidates": result["candidates"],
                        "deleted": 0,
                    }
                )
                return result

            payload = await self._execute_clean(
                {
                    "targets": selected,
                    "confirm": "确认删除",
                    "include_provider_stats": include_provider_stats,
                },
                prefetched_scan=scan,
            )
            if not payload.get("ok"):
                message = str(payload.get("error") or "自动清理失败")
                self.store.append_log(
                    {"event": "auto_clean", "ok": False, "error": message}
                )
                return {"ok": False, "error": message}

            result = payload.get("result") or {}
            notify = await self._notify_auto_clean(result)
            self.store.append_log(
                {
                    "event": "auto_clean",
                    "ok": True,
                    "targets": len(selected),
                    "backup_id": result.get("backup_id"),
                    "deleted": result.get("deleted"),
                    "counts": result.get("counts"),
                    "notify": notify,
                }
            )
            return {
                "ok": True,
                "skipped": False,
                "targets": len(selected),
                "result": result,
                "notify": notify,
                "warning": payload.get("warning"),
            }

    async def _notify_auto_clean(
        self, result: dict[str, Any], test: bool = False
    ) -> dict[str, Any]:
        if not int(result.get("deleted") or 0):
            return {"sent": 0, "failed": 0, "reason": "没有删除数据"}
        config = self.plugin.config or {}
        if not cfg_bool(config, "notify.enabled", True):
            return {"sent": 0, "failed": 0, "reason": "通知已关闭"}
        if MessageChain is None or Plain is None:
            return {"sent": 0, "failed": 0, "reason": "当前 AstrBot 不支持消息链"}
        platform_id = str(cfg_get(config, "scan.platform_id", "default") or "default")
        targets = cfg_list(config, "notify.targets", [])
        if not targets:
            targets = self._default_admin_targets()
        if not targets:
            return {
                "sent": 0,
                "failed": 0,
                "reason": "没有通知目标（未配置 notify.targets，也没有数字管理员 QQ）",
            }
        text = self._format_auto_clean_message(result, test=test)
        sent = 0
        failed = 0
        errors: list[str] = []
        for target in targets:
            umo = self._normalize_notify_target(str(target), platform_id)
            if not umo:
                continue
            try:
                found = await self.plugin.context.send_message(
                    umo, MessageChain([Plain(text)])
                )
                if found is False:
                    failed += 1
                    errors.append(f"{umo}：未找到平台")
                else:
                    sent += 1
            except Exception as exc:
                failed += 1
                errors.append(f"{umo}：{exc}")
        if errors:
            logger.warning("会话清道夫自动清理通知失败：%s", "；".join(errors[:5]))
        return {
            "sent": sent,
            "failed": failed,
            "targets": targets,
            "errors": errors[:5],
        }

    def _default_admin_targets(self) -> list[str]:
        try:
            config = self.plugin.context.get_config()
            admins = config.get("admins_id", []) if config is not None else []
        except Exception:
            return []
        if isinstance(admins, str):
            admins = [admins]
        elif not isinstance(admins, (list, tuple, set)):
            return []
        result: list[str] = []
        for item in admins:
            text = str(item).strip()
            if text.isdigit() and text not in result:
                result.append(text)
        return result

    @staticmethod
    def _normalize_notify_target(target: str, platform_id: str) -> str | None:
        target = target.strip()
        if not target:
            return None
        if target.startswith(("GroupMessage:", "FriendMessage:")):
            return f"{platform_id}:{target}"
        if ":" in target:
            return target
        if target.isdigit():
            return f"{platform_id}:FriendMessage:{target}"
        return None

    @staticmethod
    def _format_auto_clean_message(
        result: dict[str, Any], test: bool = False
    ) -> str:
        counts = result.get("counts") or {}
        detail = " / ".join(
            f"{TABLE_LABELS.get(key, key)} {value}"
            for key, value in counts.items()
            if int(value or 0)
        )
        targets = result.get("targets") or []
        title = "【会话清道夫·测试通知】" if test else "【会话清道夫·自动清理】"
        return "\n".join(
            [
                title,
                "时间：" + datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                "删除：" + str(int(result.get("deleted") or 0)) + " 行",
                "会话：" + str(len(targets)) + " 个（已退群）",
                "明细：" + (detail or "无"),
                "备份：" + str(result.get("backup_id") or "--"),
                "可在 Dashboard → 插件 → 会话清道夫 → 执行清理 中回滚。",
            ]
        )

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
        prefetched_scan: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        error = self._validate_clean_body(body)
        if error:
            return {"ok": False, "error": error}
        targets = [
            str(item).strip()
            for item in (body.get("targets") or [])
            if str(item).strip()
        ]
        default_include = cfg_bool(
            self.plugin.config, "scan.include_provider_stats", False
        )
        include_provider_stats = strict_bool(
            body.get("include_provider_stats", default_include),
            default_include,
        )
        retain = cfg_int(self.plugin.config, "backup.retain", 5)

        if prefetched_scan is None:
            self._progress(progress, "读取当前群/好友列表", 3, "正在读取平台列表")
            try:
                scan = await self._perform_scan()
            except Exception as exc:
                message = f"清理前扫描失败，已中止：{exc}"
                logger.warning(message)
                self.store.append_log({"event": "clean", "ok": False, "error": message})
                return {"ok": False, "error": message}
        else:
            scan = prefetched_scan
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
        except RuntimeError as exc:
            message = str(exc)
            logger.warning(message)
            self.store.append_log(
                {
                    "event": "clean",
                    "ok": False,
                    "error": message,
                    "operator": self._operator(),
                }
            )
            return {"ok": False, "error": message}
        except Exception as exc:
            message = f"清理失败，事务已回滚：{exc}"
            logger.exception(message)
            self.store.append_log(
                {
                    "event": "clean",
                    "ok": False,
                    "error": message,
                    "operator": self._operator(),
                }
            )
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
                "operator": self._operator(),
            }
        )
        self._progress(progress, "清理完成", 100, f"共删除 {result.deleted} 行")
        return {"ok": True, "result": result.to_dict(), "warning": warning}

    def _operator(self) -> str:
        if _request is None:
            return ""
        try:
            state = getattr(_request, "state", None)
            dashboard = getattr(state, "dashboard_g", None)
            return str(getattr(dashboard, "username", "") or "")
        except Exception:
            return ""

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
        previous = self.store.load()
        try:
            data = await self._perform_scan()
        except PlatformListError as exc:
            message = str(exc)
            logger.warning("会话清道夫扫描中止：%s", message)
            self.store.append_log(
                {
                    "event": "scan",
                    "ok": False,
                    "stage": "platform",
                    "error": message,
                    "operator": self._operator(),
                }
            )
            return {"ok": False, "stage": "platform", "error": message}
        except Exception as exc:
            message = f"读取平台列表失败：{exc}"
            logger.warning("会话清道夫扫描中止：%s", message)
            self.store.append_log(
                {
                    "event": "scan",
                    "ok": False,
                    "stage": "platform",
                    "error": message,
                    "operator": self._operator(),
                }
            )
            return {"ok": False, "stage": "platform", "error": message}

        data["changes"] = self._diff_scans(previous, data)
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
                "operator": self._operator(),
            }
        )
        return {"ok": True, "scan": data}

    @staticmethod
    def _diff_scans(
        previous: dict[str, Any] | None,
        current: dict[str, Any],
    ) -> dict[str, Any] | None:
        if not previous:
            return None
        old_targets = {
            str(item.get("umo")): item
            for item in (previous.get("targets") or [])
            if item.get("umo")
        }
        new_targets = {
            str(item.get("umo")): item
            for item in (current.get("targets") or [])
            if item.get("umo")
        }

        def brief(item: dict[str, Any]) -> dict[str, Any]:
            return {
                "umo": item.get("umo"),
                "name": item.get("name"),
                "status": item.get("status"),
                "status_label": item.get("status_label"),
            }

        new_candidates = [
            brief(item)
            for umo, item in new_targets.items()
            if item.get("selectable")
            and (umo not in old_targets or not old_targets[umo].get("selectable"))
        ]
        resolved_candidates = [
            brief(item)
            for umo, item in old_targets.items()
            if item.get("selectable")
            and (umo not in new_targets or not new_targets[umo].get("selectable"))
        ]
        changed = []
        for umo, item in new_targets.items():
            old_item = old_targets.get(umo)
            if old_item and old_item.get("status") != item.get("status"):
                row = brief(item)
                row["previous_status"] = old_item.get("status")
                row["previous_status_label"] = old_item.get("status_label")
                changed.append(row)
        return {
            "previous_generated_at": previous.get("generated_at"),
            "new_count": len(new_candidates),
            "resolved_count": len(resolved_candidates),
            "changed_count": len(changed),
            "new_candidates": new_candidates[:20],
            "resolved_candidates": resolved_candidates[:20],
            "changed": changed[:20],
        }

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
        def as_int(
            key: str,
            default: int,
            low: int,
            high: int,
            label: str | None = None,
        ) -> int:
            try:
                value = int(body.get(key, default))
            except (TypeError, ValueError) as exc:
                raise ValueError(f"{label or key}必须是整数") from exc
            if value < low or value > high:
                raise ValueError(
                    f"{label or key}必须在 {low}–{high} 之间，当前值 {value}"
                )
            return value

        def as_bool(key: str, default: bool = False) -> bool:
            return strict_bool(body.get(key, default), default)

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
        auto_enabled = as_bool("auto_clean_enabled", False)
        auto_interval = as_int(
            "scan_interval_minutes", 0, 0, 1440, "自动扫描间隔"
        )
        if auto_enabled and auto_interval <= 0:
            raise ValueError("开启自动清理时，扫描间隔必须大于 0 分钟")
        return {
            "scan": {
                "platform_id": platform_id,
                "temp_session_days": as_int(
                    "temp_session_days", 30, 1, 365, "临时会话阈值"
                ),
                "include_provider_stats": as_bool("include_provider_stats", False),
                "whitelist": as_list("whitelist"),
                "scan_timeout_seconds": as_int(
                    "scan_timeout_seconds", 10, 3, 60, "平台列表超时"
                ),
            },
            "automation": {
                "auto_clean_enabled": auto_enabled,
                "scan_interval_minutes": auto_interval,
                "include_provider_stats": as_bool(
                    "auto_clean_include_provider_stats", False
                ),
            },
            "notify": {
                "enabled": as_bool("notify_enabled", True),
                "targets": as_list("notify_targets"),
            },
            "backup": {
                "retain": as_int("backup_retain", 5, 1, 30, "备份保留份数"),
            },
        }

    def _config_view(self) -> dict[str, Any]:
        config = self.plugin.config or {}
        return {
            "version": PLUGIN_VERSION,
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
            "auto_clean_include_provider_stats": cfg_bool(
                config, "automation.include_provider_stats", False
            ),
            "notify_enabled": cfg_bool(config, "notify.enabled", True),
            "notify_targets": cfg_list(config, "notify.targets", []),
            "backup_retain": cfg_int(config, "backup.retain", 5),
            "tables": list(TABLE_KEYS),
        }
