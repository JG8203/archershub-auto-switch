from __future__ import annotations

from io import StringIO
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from archershub.admin import main as admin_main
from archershub.storage import SQLiteStorage


class AdminCliTests(unittest.TestCase):
    def run_admin(self, *args: str) -> str:
        stdout = StringIO()
        with patch("sys.argv", ["archershub-admin", *args]), patch("sys.stdout", stdout):
            admin_main()
        return stdout.getvalue()

    def test_list_revoke_and_reactivate_code(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = str(Path(tmp) / "bot.sqlite3")
            storage = SQLiteStorage(db)
            code = storage.generate_registration_code(ttl_hours=1)
            user = storage.redeem_registration_code(code, 123, "tester")

            output = self.run_admin("--db", db, "list-codes")
            self.assertIn(code, output)
            self.assertIn("status=used", output)

            output = self.run_admin("--db", db, "revoke-code", code, "--reason", "test")
            self.assertIn(f"revoked {code}", output)
            self.assertFalse(storage.get_user_by_telegram_id(123).is_active)

            output = self.run_admin("--db", db, "list-codes")
            self.assertIn("status=revoked", output)
            self.assertIn("reason=test", output)

            output = self.run_admin("--db", db, "reactivate-user", str(user.id))
            self.assertIn(f"reactivated user={user.id}", output)
            self.assertTrue(storage.get_user_by_telegram_id(123).is_active)

    def test_list_codes_marks_expired_codes(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = str(Path(tmp) / "bot.sqlite3")
            storage = SQLiteStorage(db)
            code = storage.generate_registration_code(ttl_hours=1)
            with storage.connect() as conn:
                conn.execute("UPDATE registration_codes SET expires_at = '2000-01-01T00:00:00+00:00' WHERE code = ?", (code,))

            output = self.run_admin("--db", db, "list-codes")

            self.assertIn(code, output)
            self.assertIn("status=expired", output)


if __name__ == "__main__":
    unittest.main()
