from __future__ import annotations

from io import StringIO
from pathlib import Path
from types import SimpleNamespace
import tempfile
import unittest
from unittest.mock import patch

from archershub.admin import main as admin_main
from archershub.crypto import SecretBox
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

    def test_list_schedule_uses_profile_enlistment_current_session(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = str(Path(tmp) / "bot.sqlite3")
            storage = SQLiteStorage(db)
            code = storage.generate_registration_code(ttl_hours=1)
            user = storage.redeem_registration_code(code, 123, "tester")
            box = SecretBox.from_secret("dev-secret")
            storage.save_credentials(user.id, box.encrypt_text("11922334"), box.encrypt_text("password"), None)

            profile_enlistment = SimpleNamespace()
            profile_enlistment.get_all_drop_down = unittest.mock.Mock(
                return_value=[
                    {"academic_session_id": 10, "is_current_session": False},
                    {"academic_session_id": 20, "is_current_session": True},
                ]
            )
            profile_enlistment.get_profile_enlistmentgrid_list = unittest.mock.Mock(
                return_value=[
                    {
                        "course_code": "LCFAITH",
                        "course_name": "Faith",
                        "section_name": "Z18",
                        "credits": 3,
                        "status": "Enlisted",
                        "time_table_date": "MON 09:00-10:30",
                    }
                ]
            )
            client = SimpleNamespace(
                login=unittest.mock.Mock(),
                profile_enlistment=profile_enlistment,
            )

            with patch.dict("os.environ", {"ARCHERSHUB_MASTER_KEY": "dev-secret"}), patch(
                "archershub.client.ArchersHubClient", return_value=client
            ):
                output = self.run_admin("--db", db, "list-schedule", "tester")

            profile_enlistment.get_profile_enlistmentgrid_list.assert_called_once_with(params={"academicid": "20"})
            self.assertIn("LCFAITH - Faith", output)
            self.assertIn("Section: Z18", output)
            self.assertIn("Schedule: MON 09:00-10:30", output)


if __name__ == "__main__":
    unittest.main()
