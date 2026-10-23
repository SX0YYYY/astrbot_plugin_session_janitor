"""V0.7.1 回归测试：回滚边界、冲突、缺文件、quick_check 与目标跳过。"""

from __future__ import annotations

import json
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

PLUGIN_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PLUGIN_DIR))

from core.backup import BackupStore  # noqa: E402
from core.cleaner import CoreCleaner, RollbackConflictError  # noqa: E402
from core.scanner import Scanner  # noqa: E402
from tests.test_core import NOW_TS, build_db, snapshot  # noqa: E402


def count_rows(db: Path, table: str, where: str, params=()) -> int:
    con = sqlite3.connect("file:" + db.as_posix() + "?mode=ro", uri=True)
    try:
        return int(
            con.execute(
                f'select count(*) from "{table}" where {where}', params
            ).fetchone()[0]
        )
    finally:
        con.close()


class RollbackSafetyTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        root = Path(self.temp.name)
        self.db = root / "data_v4.db"
        build_db(self.db)
        self.scan = Scanner(
            self.db,
            platform_id="default",
            temp_session_days=30,
            now=NOW_TS,
        ).scan(snapshot()).to_dict()
        self.store = BackupStore(root / "plugin_data")
        self.cleaner = CoreCleaner(self.db, self.store)

    def tearDown(self):
        self.temp.cleanup()

    def _clean_with_provider(self):
        return self.cleaner.clean(
            self.scan,
            ["default:GroupMessage:222"],
            include_provider_stats=True,
        )

    def test_rollback_conflict_requires_choice(self):
        result = self._clean_with_provider()
        con = sqlite3.connect(self.db)
        con.execute(
            "insert or replace into provider_stats "
            "(id, umo, provider_model) values (2, 'default:GroupMessage:111', 'NEW')"
        )
        con.commit()
        con.close()

        with self.assertRaises(RollbackConflictError) as ctx:
            self.cleaner.rollback(result.backup_id)
        self.assertEqual(ctx.exception.conflicts, {"provider_stats": 1})
        manifest, _ = self.store.load(result.backup_id)
        self.assertEqual(manifest["status"], "completed")

    def test_rollback_skip_conflict_keeps_new_row(self):
        result = self._clean_with_provider()
        con = sqlite3.connect(self.db)
        con.execute(
            "insert or replace into provider_stats "
            "(id, umo, provider_model) values (2, 'default:GroupMessage:111', 'NEW')"
        )
        con.commit()
        con.close()

        out = self.cleaner.rollback(result.backup_id, "skip")
        self.assertEqual(out["skipped_conflicts"], {"provider_stats": 1})
        con = sqlite3.connect("file:" + self.db.as_posix() + "?mode=ro", uri=True)
        try:
            row = con.execute(
                "select umo, provider_model from provider_stats where id = 2"
            ).fetchone()
            self.assertEqual(row, ("default:GroupMessage:111", "NEW"))
            self.assertEqual(
                count_rows(
                    self.db,
                    "provider_stats",
                    "umo = ?",
                    ("default:GroupMessage:222",),
                ),
                3,  # id=2 跳过，其余 3 行恢复
            )
        finally:
            con.close()

    def test_rollback_overwrite_replaces_new_row(self):
        result = self._clean_with_provider()
        con = sqlite3.connect(self.db)
        con.execute(
            "insert or replace into provider_stats "
            "(id, umo, provider_model) values (2, 'default:GroupMessage:111', 'NEW')"
        )
        con.commit()
        con.close()

        out = self.cleaner.rollback(result.backup_id, "overwrite")
        self.assertEqual(out["overwritten"], {"provider_stats": 1})
        con = sqlite3.connect("file:" + self.db.as_posix() + "?mode=ro", uri=True)
        try:
            row = con.execute(
                "select umo, provider_model from provider_stats where id = 2"
            ).fetchone()
            self.assertEqual(row, ("default:GroupMessage:222", "m"))
        finally:
            con.close()

    def test_missing_rows_json_aborts(self):
        result = self.cleaner.clean(
            self.scan,
            ["default:GroupMessage:222"],
            include_provider_stats=False,
        )
        rows_file = self.store.root / result.backup_id / "rows.json"
        rows_file.unlink()

        with self.assertRaises(FileNotFoundError):
            self.cleaner.rollback(result.backup_id)
        manifest = json.loads(
            (self.store.root / result.backup_id / "manifest.json").read_text(
                encoding="utf-8"
            )
        )
        self.assertEqual(manifest["status"], "completed")
        self.assertEqual(
            count_rows(
                self.db,
                "umo_aliases",
                "umo = ?",
                ("default:GroupMessage:222",),
            ),
            0,
        )

    def test_missing_database_does_not_create_empty_file(self):
        result = self.cleaner.clean(
            self.scan,
            ["default:GroupMessage:222"],
            include_provider_stats=False,
        )
        self.db.unlink()

        with self.assertRaises(FileNotFoundError):
            self.cleaner.rollback(result.backup_id)
        self.assertFalse(self.db.exists())

    def test_non_core_table_rejected(self):
        # 手工构造一个“核心行 + 非核心表”备份，验证回滚不会越界写表。
        bundle = self.store.create("core", [])
        bundle.add_rows(
            "umo_aliases",
            [
                "created_at",
                "updated_at",
                "id",
                "umo",
                "creator_sender_id",
                "auto_name",
                "user_alias",
            ],
            [
                {
                    "created_at": "2026-10-22 00:00:00",
                    "updated_at": "2026-10-22 00:00:00",
                    "id": 999,
                    "umo": "default:GroupMessage:999",
                    "creator_sender_id": "999",
                    "auto_name": "x",
                    "user_alias": None,
                }
            ],
        )
        bundle.manifest["status"] = "completed"
        bundle.save()
        rows_path = self.store.root / bundle.backup_id / "rows.json"
        rows = json.loads(rows_path.read_text(encoding="utf-8"))
        rows["victim"] = {
            "database": "core",
            "columns": ["id", "note"],
            "rows": [{"id": 1, "note": "injected"}],
        }
        rows_path.write_text(json.dumps(rows), encoding="utf-8")

        con = sqlite3.connect(self.db)
        con.execute("create table victim (id integer primary key, note text)")
        con.commit()
        con.close()

        with self.assertRaises(ValueError):
            self.cleaner.rollback(bundle.backup_id)
        self.assertEqual(count_rows(self.db, "victim", "1 = 1"), 0)

    def test_safety_snapshot_failure_aborts(self):
        result = self.cleaner.clean(
            self.scan,
            ["default:GroupMessage:222"],
            include_provider_stats=False,
        )
        with patch(
            "core.backup.BackupBundle.snapshot_db",
            side_effect=OSError("磁盘已满"),
        ):
            with self.assertRaises(RuntimeError):
                self.cleaner.rollback(result.backup_id)
        self.assertEqual(
            count_rows(
                self.db,
                "umo_aliases",
                "umo = ?",
                ("default:GroupMessage:222",),
            ),
            0,
        )
        manifest, _ = self.store.load(result.backup_id)
        self.assertEqual(manifest["status"], "completed")

    def test_quick_check_success_allows_commit(self):
        with patch("core.cleaner.QUICK_CHECK_THRESHOLD", 1):
            result = self.cleaner.clean(
                self.scan,
                ["default:GroupMessage:222"],
                include_provider_stats=False,
            )
        self.assertGreater(result.deleted, 0)
        manifest, _ = self.store.load(result.backup_id)
        self.assertEqual(manifest["status"], "completed")
        self.assertEqual(manifest.get("quick_check"), "ok")

    def test_quick_check_failure_rolls_back(self):
        with patch("core.cleaner.QUICK_CHECK_THRESHOLD", 1), patch.object(
            CoreCleaner,
            "_quick_check",
            return_value="malformed",
        ):
            with self.assertRaises(RuntimeError):
                self.cleaner.clean(
                    self.scan,
                    ["default:GroupMessage:222"],
                    include_provider_stats=False,
                )
        self.assertEqual(
            count_rows(
                self.db,
                "umo_aliases",
                "umo = ?",
                ("default:GroupMessage:222",),
            ),
            1,
        )
        backups = self.store.list(10)
        self.assertTrue(backups)
        self.assertEqual(backups[0]["status"], "failed")

    def test_invalid_targets_are_skipped(self):
        result = self.cleaner.clean(
            self.scan,
            ["default:GroupMessage:111", "default:GroupMessage:222"],
            include_provider_stats=False,
        )
        self.assertNotIn("default:GroupMessage:111", result.targets)
        self.assertTrue(
            any("default:GroupMessage:111" in item for item in result.warnings)
        )
        self.assertEqual(
            count_rows(
                self.db,
                "umo_aliases",
                "umo = ?",
                ("default:GroupMessage:111",),
            ),
            1,
        )
        self.assertEqual(
            count_rows(
                self.db,
                "umo_aliases",
                "umo = ?",
                ("default:GroupMessage:222",),
            ),
            0,
        )

    def test_created_backup_rejected(self):
        bundle = self.store.create("core", [])
        bundle.save()
        with self.assertRaises(ValueError):
            self.cleaner.rollback(bundle.backup_id)


if __name__ == "__main__":
    unittest.main()
