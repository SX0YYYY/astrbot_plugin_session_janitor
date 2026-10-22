from __future__ import annotations

import asyncio
import logging
import sqlite3
import sys
import tempfile
import types
import unittest
from pathlib import Path

PLUGIN_DIR = Path(__file__).resolve().parents[1]
PROJECT_DIR = PLUGIN_DIR.parent
sys.path.insert(0, str(PROJECT_DIR))

# 在导入 page_api 之前注入最小的 astrbot 模块，避免测试环境安装整套 AstrBot。
astrbot_module = types.ModuleType("astrbot")
api_module = types.ModuleType("astrbot.api")
api_module.logger = logging.getLogger("test")
star_module = types.ModuleType("astrbot.api.star")
star_module.Context = object


class _FakeStar:
    def __init__(self, context):
        self.context = context


star_module.Star = _FakeStar
star_module.StarTools = types.SimpleNamespace(get_data_dir=lambda name: Path(tempfile.gettempdir()) / name)
star_module.register = lambda *args, **kwargs: (lambda cls: cls)
web_module = types.ModuleType("astrbot.api.web")
web_module.json_response = lambda payload, **kwargs: payload


class _FakeRequest:
    def __init__(self):
        self.query = {}
        self._json = {}

    async def json(self, default=None):
        return self._json


fake_request = _FakeRequest()
web_module.request = fake_request
sys.modules.setdefault("astrbot", astrbot_module)
sys.modules.setdefault("astrbot.api", api_module)
sys.modules.setdefault("astrbot.api.star", star_module)
sys.modules.setdefault("astrbot.api.web", web_module)

from astrbot_plugin_session_janitor import page_api as page_api_module  # noqa: E402
from astrbot_plugin_session_janitor.core.backup import BackupStore  # noqa: E402
from astrbot_plugin_session_janitor.core.store import ScanStore  # noqa: E402
from astrbot_plugin_session_janitor.main import SessionJanitorPlugin  # noqa: E402
from astrbot_plugin_session_janitor.tests.test_core import build_db, snapshot  # noqa: E402


class _FakeContext:
    pass


class _FakePlugin:
    def __init__(self, db_path: Path, store: ScanStore):
        self.config = {
            "scan": {
                "platform_id": "default",
                "temp_session_days": 30,
                "include_provider_stats": False,
                "scan_timeout_seconds": 5,
                "whitelist": [],
            },
            "backup": {"retain": 5},
        }
        self.db_path = db_path
        self.store = store
        self.data_dir = store.data_dir
        self.backup_store = BackupStore(store.data_dir)
        self.context = _FakeContext()


class PageApiTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        root = Path(self.temp.name)
        self.db = root / "data_v4.db"
        build_db(self.db)
        self.plugin = _FakePlugin(self.db, ScanStore(root / "plugin_data"))
        self.api = page_api_module.PageApi(self.plugin)
        fake_request.query = {}
        fake_request._json = {}

    def tearDown(self):
        self.temp.cleanup()

    async def test_scan_and_read_views(self):
        async def fake_fetch(context, platform_id="default", timeout=10.0):
            return snapshot()

        page_api_module.fetch_platform_snapshot = fake_fetch
        result = await self.api._run_scan()
        self.assertTrue(result["ok"])
        self.assertEqual(result["scan"]["summary"]["candidates"], 3)

        overview = await self.api.overview()
        self.assertFalse(overview["needs_scan"])
        self.assertEqual(overview["scan"]["summary"]["orphan_group"], 1)

        sessions = await self.api.sessions()
        self.assertTrue(sessions["ok"])
        self.assertEqual(sessions["total"], 5)

        fake_request.query = {"status": "orphan_group"}
        filtered = await self.api.sessions()
        self.assertEqual(filtered["total"], 1)

        preview = await self.api.preview()
        self.assertTrue(preview["preview"]["readonly"])
        self.assertEqual(preview["preview"]["summary"]["targets"], 3)

        logs = await self.api.logs()
        self.assertTrue(logs["items"])

        settings = await self.api.settings()
        self.assertFalse(settings["readonly"])
        self.assertEqual(settings["config"]["temp_session_days"], 30)

    async def test_session_detail(self):
        async def fake_fetch(context, platform_id="default", timeout=10.0):
            return snapshot()

        page_api_module.fetch_platform_snapshot = fake_fetch
        await self.api._run_scan()
        fake_request.query = {"umo": "default:GroupMessage:222"}
        result = await self.api.session_detail()
        self.assertTrue(result["ok"])
        detail = result["detail"]
        self.assertEqual(detail["session_id"], "222")
        self.assertEqual(detail["kind"], "group")
        self.assertTrue(detail["aliases"])
        self.assertTrue(detail["conversations"])
        self.assertTrue(detail["preferences"])
        self.assertTrue(detail["core_messages"])
        self.assertNotIn("livingmemory", detail)
        self.assertNotIn("recent_messages", detail)
        self.assertEqual(detail["provider_stats"]["count"], 4)

    async def test_conversation_detail(self):
        async def fake_fetch(context, platform_id="default", timeout=10.0):
            return snapshot()

        page_api_module.fetch_platform_snapshot = fake_fetch
        await self.api._run_scan()
        fake_request.query = {
            "conversation_id": "c2",
            "umo": "default:GroupMessage:222",
            "limit": "50",
        }
        result = await self.api.conversation_detail()
        self.assertTrue(result["ok"])
        detail = result["detail"]
        self.assertEqual(detail["message_count"], 2)
        self.assertEqual(detail["messages"][0]["role_label"], "用户")
        self.assertEqual(detail["messages"][0]["texts"], ["你好"])
        self.assertEqual(detail["messages"][1]["texts"], ["你好呀"])
        self.assertEqual(detail["messages"][1]["thinks"], ["先打招呼"])

    async def test_auto_clean_only_orphan_groups(self):
        async def fake_fetch(context, platform_id="default", timeout=10.0):
            return snapshot()

        page_api_module.fetch_platform_snapshot = fake_fetch
        result = await self.api.run_auto_clean()
        self.assertTrue(result["ok"])
        self.assertFalse(result["skipped"])
        self.assertEqual(result["targets"], 1)
        self.assertEqual(result["result"]["counts"]["umo_aliases"], 1)
        self.assertEqual(result["result"]["counts"]["conversations"], 3)
        self.assertEqual(result["result"]["counts"]["preferences"], 1)
        self.assertNotIn("provider_stats", result["result"]["counts"])
        # 已删好友/临时会话不在自动清理范围内
        self.assertEqual(result["result"]["targets"], ["default:GroupMessage:222"])
        logs = await self.api.logs()
        self.assertEqual(logs["items"][0]["event"], "auto_clean")

    async def test_settings_save(self):
        fake_request._json = {
            "platform_id": "default",
            "temp_session_days": 45,
            "scan_timeout_seconds": 12,
            "backup_retain": 7,
            "include_provider_stats": True,
            "whitelist": ["222", "333"],
            "auto_clean_enabled": False,
            "scan_interval_minutes": 0,
            "notify_enabled": True,
            "notify_targets": [],
        }
        result = await self.api.settings_save()
        self.assertTrue(result["ok"])
        self.assertEqual(result["config"]["temp_session_days"], 45)
        self.assertEqual(result["config"]["backup_retain"], 7)
        self.assertTrue(result["config"]["include_provider_stats"])
        self.assertEqual(result["config"]["whitelist"], ["222", "333"])

    async def test_notify_auto_clean(self):
        sent = []

        class _NotifyContext:
            async def send_message(self, umo, chain):
                sent.append(umo)
                return True

        self.plugin.context = _NotifyContext()
        self.plugin.config["notify"] = {
            "enabled": True,
            "targets": ["default:GroupMessage:123"],
        }
        old_chain = page_api_module.MessageChain
        old_plain = page_api_module.Plain
        page_api_module.MessageChain = lambda chain: chain
        page_api_module.Plain = lambda text: text
        try:
            result = await self.api._notify_auto_clean(
                {
                    "deleted": 2,
                    "targets": ["default:GroupMessage:222"],
                    "counts": {"umo_aliases": 1, "preferences": 1},
                    "backup_id": "backup-test",
                }
            )
        finally:
            page_api_module.MessageChain = old_chain
            page_api_module.Plain = old_plain
        self.assertEqual(sent, ["default:GroupMessage:123"])
        self.assertEqual(result["sent"], 1)
        self.assertEqual(result["failed"], 0)

    async def test_settings_auto_clean_requires_interval(self):
        fake_request._json = {
            "auto_clean_enabled": True,
            "scan_interval_minutes": 0,
        }
        result = await self.api.settings_save()
        self.assertFalse(result["ok"])
        self.assertIn("扫描间隔", result["error"])

    async def test_clean_job_progress(self):
        async def fake_fetch(context, platform_id="default", timeout=10.0):
            return snapshot()

        page_api_module.fetch_platform_snapshot = fake_fetch
        fake_request._json = {
            "targets": ["default:GroupMessage:222"],
            "confirm": "确认删除",
            "include_provider_stats": False,
        }
        started = await self.api.clean_start()
        self.assertTrue(started["ok"])
        job_id = started["job_id"]
        fake_request.query = {"job_id": job_id}
        job = None
        for _ in range(200):
            await asyncio.sleep(0.01)
            status = await self.api.clean_status()
            self.assertTrue(status["ok"])
            job = status["job"]
            if job["status"] != "running":
                break
        self.assertIsNotNone(job)
        self.assertEqual(job["status"], "success")
        self.assertTrue(job["result"]["ok"])
        self.assertEqual(job["result"]["result"]["counts"]["umo_aliases"], 1)
        self.assertGreaterEqual(job["percent"], 99)

    def test_plugin_init(self):
        plugin = SessionJanitorPlugin(_FakeContext(), {})
        self.assertTrue(plugin.data_dir.exists())
        self.assertIsNotNone(plugin.store)
        self.assertIsNotNone(plugin.page_api)

    async def test_clean_and_rollback(self):
        async def fake_fetch(context, platform_id="default", timeout=10.0):
            return snapshot()

        page_api_module.fetch_platform_snapshot = fake_fetch
        fake_request._json = {
            "targets": ["default:GroupMessage:222"],
            "confirm": "确认删除",
            "include_provider_stats": True,
        }
        cleaned = await self.api.clean()
        self.assertTrue(cleaned["ok"])
        self.assertEqual(cleaned["result"]["counts"]["umo_aliases"], 1)
        self.assertEqual(cleaned["result"]["counts"]["conversations"], 3)
        self.assertTrue(cleaned["result"]["backup_id"])

        overview = await self.api.overview()
        self.assertEqual(overview["scan"]["summary"]["total"], 4)

        backups = await self.api.backups()
        self.assertTrue(backups["items"])
        backup_id = cleaned["result"]["backup_id"]

        fake_request._json = {"backup_id": backup_id, "confirm": "确认回滚"}
        rolled = await self.api.rollback()
        self.assertTrue(rolled["ok"])
        self.assertTrue(rolled["result"]["safety_backup_id"])
        overview = await self.api.overview()
        self.assertEqual(overview["scan"]["summary"]["total"], 5)

        fake_request._json = {"backup_id": backup_id, "confirm": "确认回滚"}
        again = await self.api.rollback()
        self.assertFalse(again["ok"])
        self.assertIn("已经回滚过", again["error"])

    async def test_clean_requires_confirmation(self):
        async def fake_fetch(context, platform_id="default", timeout=10.0):
            return snapshot()

        page_api_module.fetch_platform_snapshot = fake_fetch
        fake_request._json = {"targets": ["default:GroupMessage:222"], "confirm": ""}
        result = await self.api.clean()
        self.assertFalse(result["ok"])

    async def test_scan_platform_failure(self):
        async def fake_fetch(context, platform_id="default", timeout=10.0):
            raise page_api_module.PlatformListError("列表为空")

        page_api_module.fetch_platform_snapshot = fake_fetch
        result = await self.api._run_scan()
        self.assertFalse(result["ok"])
        self.assertEqual(result["stage"], "platform")

    async def test_include_provider_stats_string_false_is_false(self):
        async def fake_fetch(context, platform_id="default", timeout=10.0):
            return snapshot()

        page_api_module.fetch_platform_snapshot = fake_fetch
        fake_request._json = {
            "targets": ["default:GroupMessage:222"],
            "confirm": "确认删除",
            "include_provider_stats": "false",
        }
        result = await self.api.clean()
        self.assertTrue(result["ok"])
        self.assertNotIn("provider_stats", result["result"]["counts"])
        con = sqlite3.connect(self.db)
        try:
            count = con.execute(
                "select count(*) from provider_stats where umo = ?",
                ("default:GroupMessage:222",),
            ).fetchone()[0]
        finally:
            con.close()
        self.assertEqual(count, 4)

    async def test_settings_out_of_range_rejected(self):
        fake_request._json = {"temp_session_days": 0}
        result = await self.api.settings_save()
        self.assertFalse(result["ok"])
        self.assertIn("必须在", result["error"])

    async def test_admins_id_string_is_not_split(self):
        class _Ctx:
            def get_config(self):
                return {"admins_id": "123456"}

        self.plugin.context = _Ctx()
        self.assertEqual(self.api._default_admin_targets(), ["123456"])


if __name__ == "__main__":
    unittest.main()
