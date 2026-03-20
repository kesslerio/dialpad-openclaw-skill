from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

import sys

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

import sms_sqlite


class SmsSqliteCacheCleanupTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)

        self.original_db_path = sms_sqlite.DB_PATH
        sms_sqlite.DB_PATH = Path(self.temp_dir.name) / "sms.db"
        self.addCleanup(self._restore_db_path)

        self.conn = sms_sqlite.init_db()
        self.addCleanup(self.conn.close)

    def _restore_db_path(self):
        sms_sqlite.DB_PATH = self.original_db_path

    def _store(self, dialpad_id: int, phone: str, name: str, ts: int) -> None:
        payload = {
            "id": dialpad_id,
            "created_date": ts,
            "direction": "inbound",
            "from_number": phone,
            "to_number": ["+14155201316"],
            "text": f"msg-{dialpad_id}",
            "contact": {"name": name},
        }
        sms_sqlite.store_message(self.conn, payload, is_new=False)

    def test_contact_summary_prefers_latest_contact_name(self):
        number = "+13053354499"
        self._store(1, number, "Zed Person", 1000)
        self._store(2, number, "Adhara", 2000)

        row = self.conn.execute(
            "SELECT name FROM contacts WHERE phone_number = ?",
            (number,),
        ).fetchone()

        self.assertIsNotNone(row)
        self.assertEqual(row["name"], "Adhara")

    def test_cleanup_reconciles_stale_name_and_removes_orphan(self):
        number = "+14155550123"
        self._store(10, number, "Correct Name", 2000)

        self.conn.execute(
            "UPDATE contacts SET name = ? WHERE phone_number = ?",
            ("Stale Name", number),
        )
        self.conn.execute(
            "INSERT INTO contacts (phone_number, name) VALUES (?, ?)",
            ("+19999999999", "Orphan"),
        )
        self.conn.commit()

        result = sms_sqlite.cleanup_stale_contacts(self.conn)

        refreshed = self.conn.execute(
            "SELECT name FROM contacts WHERE phone_number = ?",
            (number,),
        ).fetchone()
        orphan = self.conn.execute(
            "SELECT 1 FROM contacts WHERE phone_number = ?",
            ("+19999999999",),
        ).fetchone()

        self.assertIsNotNone(refreshed)
        self.assertEqual(refreshed["name"], "Correct Name")
        self.assertIsNone(orphan)
        self.assertEqual(result["updated"], 1)
        self.assertEqual(result["removed"], 1)

    def test_latest_contact_name_skips_whitespace_only_newer_values(self):
        number = "+14155550001"
        self._store(20, number, "Adhara", 1000)
        self._store(21, number, "\n\t  ", 2000)

        sms_sqlite._update_contact_summary(self.conn, number)

        row = self.conn.execute(
            "SELECT name FROM contacts WHERE phone_number = ?",
            (number,),
        ).fetchone()

        self.assertIsNotNone(row)
        self.assertEqual(row["name"], "Adhara")


if __name__ == "__main__":
    unittest.main()
