from __future__ import annotations

import json
import sqlite3
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core.config import cfg_bool, cfg_get, cfg_int, cfg_list
from core.db import JanitorDB, preference_targets
from core.models import PlatformSnapshot
from core.preview import build_preview
from core.scanner import Scanner
from core.store import ScanStore
from core.umo import find_umos_in_text, parse_analysis_key, parse_umo


NOW = datetime(2026, 10, 22, 12, 0, tzinfo=timezone.utc)
NOW_TS = NOW.timestamp()


def ago(days: float) -> str:
    return (NOW - timedelta(days=days)).strftime("%Y-%m-%d %H:%M:%S.%f")


def epoch_ago(days: float) -> float:
    return NOW_TS - days * 86400


SCHEMA = """
create table umo_aliases (
  created_at text, updated_at text, id integer primary key,
  umo text, creator_sender_id text, auto_name text, user_alias text
);
create table conversations (
  created_at text, updated_at text, inner_conversation_id integer primary key,
  conversation_id text, platform_id text, user_id text, content text,
  title text, persona_id text, token_usage integer
);
create table preferences (
  created_at text, updated_at text, id integer primary key,
  scope text, scope_id text, key text, value text
);
create table provider_stats (
  created_at text, updated_at text, id integer primary key,
  agent_type text, status text, umo text, conversation_id text,
  provider_id text, provider_model text, token_input_other integer,
  token_input_cached integer, token_output integer, start_time real,
  end_time real, time_to_first_token real
);
create table platform_message_history (
  created_at text, updated_at text, id integer primary key,
  platform_id text, user_id text, sender_id text, sender_name text,
  content text, llm_checkpoint_id text
);
create table platform_sessions (
  created_at text, updated_at text, inner_id integer primary key,
  session_id text, platform_id text, creator text, display_name text,
  is_group integer
);
"""


def build_db(path: Path) -> None:
    con = sqlite3.connect(path)
    con.executescript(SCHEMA)
    con.executemany(
        "insert into umo_aliases values (?,?,?,?,?,?,?)",
        [
            (ago(30), ago(10), 1, "default:GroupMessage:111", "1", "Active Group", None),
            (ago(30), ago(10), 2, "default:GroupMessage:222", "2", "Orphan Group", None),
            (ago(30), ago(10), 3, "default:FriendMessage:333", "333", "Active Friend", None),
            (ago(30), ago(1), 4, "default:FriendMessage:555", "555", "Recent Stranger", None),
            (ago(30), ago(1), 5, "webchat:FriendMessage:webchat!u!abc", "u", "WebChat", None),
            (ago(30), ago(1), 6, "other:GroupMessage:999", "999", "Other Platform", None),
        ],
    )
    conversation_content = json.dumps(
        [
            {"role": "user", "content": [{"type": "text", "text": "你好"}]},
            {
                "role": "assistant",
                "content": [
                    {"type": "think", "think": "先打招呼"},
                    {"type": "text", "text": "你好呀"},
                ],
            },
        ],
        ensure_ascii=False,
    )
    con.executemany(
        "insert into conversations values (?,?,?,?,?,?,?,?,?,?)",
        [
            (ago(30), ago(2), 1, "c1", "default", "default:GroupMessage:111", "[]", None, None, 0),
            (ago(30), ago(3), 2, "c2", "default", "default:GroupMessage:222", conversation_content, None, None, 0),
            (ago(30), ago(4), 3, "c3", "default", "default:GroupMessage:222", "[]", None, None, 0),
            (ago(30), ago(5), 4, "c4", "default", "default:GroupMessage:222", "[]", None, None, 0),
            (ago(30), ago(2), 5, "c5", "default", "default:FriendMessage:333", "[]", None, None, 0),
            (ago(45), ago(40), 6, "c6", "default", "default:FriendMessage:444", "[]", None, None, 0),
            (ago(30), ago(1), 7, "c7", "webchat", "webchat:FriendMessage:webchat!u!abc", "[]", None, None, 0),
            (ago(30), ago(1), 8, "c8", "other", "other:GroupMessage:999", "[]", None, None, 0),
        ],
    )
    con.executemany(
        "insert into preferences values (?,?,?,?,?,?,?)",
        [
            (ago(30), ago(3), 1, "plugin", "daily-analysis", "analysis_222_2026-10-22_20-00", '{"val": {}}'),
            (ago(45), ago(40), 2, "umo", "default:FriendMessage:444", "session_service_config", '{"val": {}}'),
            (ago(30), ago(1), 3, "umo", "default:GroupMessage:111", "session_service_config", '{"val": {}}'),
        ],
    )
    con.executemany(
        "insert into provider_stats values (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        [
            (ago(30), ago(2), 1, "internal", "completed", "default:GroupMessage:111", "c1", "p", "m", 1, 0, 1, 0, epoch_ago(2), 0),
            (ago(30), ago(3), 2, "internal", "completed", "default:GroupMessage:222", "c2", "p", "m", 1, 0, 1, 0, epoch_ago(3), 0),
            (ago(30), ago(4), 3, "internal", "completed", "default:GroupMessage:222", "c3", "p", "m", 1, 0, 1, 0, epoch_ago(4), 0),
            (ago(30), ago(5), 4, "internal", "completed", "default:GroupMessage:222", "c4", "p", "m", 1, 0, 1, 0, epoch_ago(5), 0),
            (ago(30), ago(6), 5, "internal", "completed", "default:GroupMessage:222", "c5", "p", "m", 1, 0, 1, 0, epoch_ago(6), 0),
        ],
    )
    con.executemany(
        "insert into platform_message_history values (?,?,?,?,?,?,?,?,?)",
        [
            (ago(3), ago(3), 1, "default", "default:GroupMessage:222", "1", "u", "{}", None),
            (ago(2), ago(2), 2, "default", "default:GroupMessage:222", "1", "u", "{}", None),
            (ago(1), ago(1), 3, "webchat", "webchat!u!abc", "u", "u", "{}", None),
        ],
    )
    con.execute(
        "insert into platform_sessions values (?,?,?,?,?,?,?,?)",
        (ago(1), ago(1), 1, "webchat!u!abc", "webchat", "u", None, 0),
    )
    con.commit()
    con.close()


def snapshot() -> PlatformSnapshot:
    return PlatformSnapshot(
        platform_id="default",
        groups={"111": "Active Group"},
        friends={"333": "Active Friend"},
        captured_at=NOW.isoformat(),
    )


class UmoTests(unittest.TestCase):
    def test_parse_umo(self):
        parsed = parse_umo("default:GroupMessage:583715943")
        self.assertIsNotNone(parsed)
        self.assertEqual(parsed.platform_id, "default")
        self.assertEqual(parsed.session_id, "583715943")
        self.assertTrue(parsed.is_group)
        self.assertIsNone(parse_umo("webchat!u!abc"))
        self.assertTrue(parse_umo("webchat:FriendMessage:webchat!u!abc").is_webchat)

    def test_analysis_key_and_text(self):
        self.assertEqual(parse_analysis_key("analysis_583715943_2026-10-22_20-00"), "583715943")
        self.assertIsNone(parse_analysis_key("session_service_config"))
        found = find_umos_in_text('{"umo": "default:GroupMessage:123456"}')
        self.assertEqual(found, {"default:GroupMessage:123456"})


class ScannerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.db = Path(self.temp.name) / "data_v4.db"
        build_db(self.db)
        self.scan = Scanner(
            self.db,
            platform_id="default",
            temp_session_days=30,
            include_provider_stats=False,
            now=NOW_TS,
        ).scan(snapshot())
        self.targets = {item.umo: item for item in self.scan.targets}

    def tearDown(self):
        self.temp.cleanup()

    def test_classification(self):
        self.assertEqual(self.targets["default:GroupMessage:111"].status, "active")
        self.assertEqual(self.targets["default:GroupMessage:222"].status, "orphan_group")
        self.assertEqual(self.targets["default:FriendMessage:333"].status, "active")
        self.assertEqual(self.targets["default:FriendMessage:444"].status, "expired_temp")
        self.assertEqual(self.targets["default:FriendMessage:555"].status, "missing_friend")
        self.assertNotIn("webchat:FriendMessage:webchat!u!abc", self.targets)
        self.assertNotIn("other:GroupMessage:999", self.targets)

    def test_counts_and_tables(self):
        orphan = self.targets["default:GroupMessage:222"]
        self.assertEqual(orphan.counts["umo_aliases"], 1)
        self.assertEqual(orphan.counts["conversations"], 3)
        self.assertEqual(orphan.counts["preferences"], 1)
        self.assertEqual(orphan.counts["provider_stats"], 4)
        self.assertEqual(orphan.counts["platform_message_history"], 2)
        self.assertEqual(self.scan.summary["candidates"], 3)
        self.assertEqual(self.scan.tables["conversations"]["orphan"], 4)
        self.assertEqual(self.scan.tables["preferences"]["orphan"], 2)
        self.assertEqual(self.scan.tables["provider_stats"]["orphan"], 4)
        self.assertEqual(self.scan.tables["platform_message_history"]["orphan"], 2)

    def test_default_selected(self):
        self.assertTrue(self.targets["default:GroupMessage:222"].default_selected)
        self.assertFalse(self.targets["default:FriendMessage:444"].default_selected)
        self.assertFalse(self.targets["default:FriendMessage:555"].default_selected)

    def test_preview_provider_stats_switch(self):
        data = self.scan.to_dict()
        without = build_preview(data, include_provider_stats=False)
        with_stats = build_preview(data, include_provider_stats=True)
        self.assertEqual(without["summary"]["rows"]["provider_stats"], 0)
        self.assertEqual(with_stats["summary"]["rows"]["provider_stats"], 4)
        self.assertGreater(with_stats["summary"]["total_rows"], without["summary"]["total_rows"])
        self.assertTrue(without["readonly"])

    def test_whitelist(self):
        result = Scanner(
            self.db,
            platform_id="default",
            temp_session_days=30,
            whitelist=["222"],
            now=NOW_TS,
        ).scan(snapshot())
        targets = {item.umo: item for item in result.targets}
        self.assertEqual(targets["default:GroupMessage:222"].status, "whitelisted")
        self.assertFalse(targets["default:GroupMessage:222"].selectable)
        self.assertEqual(result.summary["whitelisted"], 1)
        self.assertEqual(result.summary["candidates"], 2)

    def test_shared_preference_row_is_skipped(self):
        con = sqlite3.connect(self.db)
        con.execute(
            "insert into preferences values (?,?,?,?,?,?,?)",
            (
                ago(1),
                ago(1),
                99,
                "umo",
                "",
                "shared_key",
                json.dumps(
                    {
                        "a": "default:GroupMessage:111",
                        "b": "default:GroupMessage:222",
                    }
                ),
            ),
        )
        con.commit()
        con.close()

        scan = Scanner(
            self.db,
            platform_id="default",
            temp_session_days=30,
            now=NOW_TS,
        ).scan(snapshot())
        orphan = next(
            item for item in scan.targets if item.umo == "default:GroupMessage:222"
        )
        active = next(
            item for item in scan.targets if item.umo == "default:GroupMessage:111"
        )
        self.assertNotIn(99, orphan.preference_row_ids)
        self.assertNotIn(99, active.preference_row_ids)
        self.assertTrue(
            any("同时关联多个会话" in warning for warning in scan.warnings)
        )

        row = next(
            item for item in JanitorDB(self.db).preferences() if item.row_id == 99
        )
        self.assertEqual(
            preference_targets(row, "default"),
            {
                "default:GroupMessage:111",
                "default:GroupMessage:222",
            },
        )


class StoreTests(unittest.TestCase):
    def test_roundtrip(self):
        with tempfile.TemporaryDirectory() as temp:
            store = ScanStore(temp)
            self.assertIsNone(store.load())
            store.save({"ok": True, "generated_at": "x", "targets": []})
            self.assertEqual(store.load()["ok"], True)
            store.append_log({"event": "scan", "ok": True})
            logs = store.tail_logs(10)
            self.assertEqual(logs[0]["event"], "scan")


class ConfigTests(unittest.TestCase):
    def test_nested(self):
        config = {"scan": {"platform_id": "default", "temp_session_days": "30", "flag": "true", "list": "a,b"}}
        self.assertEqual(cfg_get(config, "scan.platform_id"), "default")
        self.assertEqual(cfg_int(config, "scan.temp_session_days", 10), 30)
        self.assertTrue(cfg_bool(config, "scan.flag", False))
        self.assertEqual(cfg_list(config, "scan.list"), ["a", "b"])
        self.assertEqual(cfg_get(config, "missing.key", "fallback"), "fallback")


if __name__ == "__main__":
    unittest.main()
