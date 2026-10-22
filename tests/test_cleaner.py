from __future__ import annotations

import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

PLUGIN_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PLUGIN_DIR))

from core.backup import BackupStore  # noqa: E402
from core.cleaner import CoreCleaner  # noqa: E402
from core.scanner import Scanner  # noqa: E402
from tests.test_core import NOW_TS, build_db, snapshot  # noqa: E402


def count_rows(db: Path, table: str, where: str, params=()) -> int:
    con = sqlite3.connect("file:" + db.as_posix() + "?mode=ro", uri=True)
    try:
        return int(con.execute(f'select count(*) from "{table}" where {where}', params).fetchone()[0])
    finally:
        con.close()


class CleanerTests(unittest.TestCase):
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

    def test_clean_and_rollback_core(self):
        result = self.cleaner.clean(
            self.scan,
            ["default:GroupMessage:222"],
            include_provider_stats=False,
        )
        self.assertEqual(result.counts["umo_aliases"], 1)
        self.assertEqual(result.counts["conversations"], 3)
        self.assertEqual(result.counts["preferences"], 1)
        self.assertEqual(result.counts["platform_message_history"], 2)
        self.assertNotIn("provider_stats", result.counts)
        self.assertEqual(count_rows(self.db, "umo_aliases", "umo = ?", ("default:GroupMessage:222",)), 0)
        self.assertEqual(count_rows(self.db, "conversations", "user_id = ?", ("default:GroupMessage:222",)), 0)
        self.assertEqual(count_rows(self.db, "provider_stats", "umo = ?", ("default:GroupMessage:222",)), 4)

        manifest, rows = self.store.load(result.backup_id)
        self.assertEqual(manifest["status"], "completed")
        self.assertTrue((self.store.root / result.backup_id / "data_v4.db").is_file())
        self.assertIn("umo_aliases", rows)

        rollback = self.cleaner.rollback(result.backup_id)
        self.assertEqual(rollback["backup_id"], result.backup_id)
        self.assertEqual(count_rows(self.db, "umo_aliases", "umo = ?", ("default:GroupMessage:222",)), 1)
        self.assertEqual(count_rows(self.db, "conversations", "user_id = ?", ("default:GroupMessage:222",)), 3)
        manifest, _ = self.store.load(result.backup_id)
        self.assertEqual(manifest["status"], "restored")

    def test_provider_stats_optional(self):
        result = self.cleaner.clean(
            self.scan,
            ["default:GroupMessage:222"],
            include_provider_stats=True,
        )
        self.assertEqual(result.counts["provider_stats"], 4)
        self.assertEqual(count_rows(self.db, "provider_stats", "umo = ?", ("default:GroupMessage:222",)), 0)
        self.cleaner.rollback(result.backup_id)
        self.assertEqual(count_rows(self.db, "provider_stats", "umo = ?", ("default:GroupMessage:222",)), 4)

    def test_reject_active_target(self):
        with self.assertRaises(ValueError):
            self.cleaner.clean(self.scan, ["default:GroupMessage:111"])

    def test_reject_empty_selection(self):
        with self.assertRaises(ValueError):
            self.cleaner.clean(self.scan, [])

    def test_rollback_restores_files_from_manifest(self):
        sample = Path(self.temp.name) / "sample.txt"
        sample.write_text("before", encoding="utf-8")
        bundle = self.store.create("files", [])
        bundle.add_file(sample, "sample.txt")
        bundle.save()
        sample.unlink()
        self.cleaner.rollback(bundle.backup_id)
        self.assertEqual(sample.read_text(encoding="utf-8"), "before")


if __name__ == "__main__":
    unittest.main()
