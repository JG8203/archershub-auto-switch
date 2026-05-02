import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from archershub.auth import AutomatedCaptchaEscalation, LoginAttemptError, read_captcha
from archershub.bot.service import BotArchersHubService, TelegramCaptchaRequired
from archershub.crypto import SecretBox
from archershub.storage import SQLiteStorage


class CaptchaHelperTests(unittest.TestCase):
    def test_read_captcha_can_disable_manual_fallback(self):
        with tempfile.TemporaryDirectory() as tmp:
            with patch("archershub.auth.solve_captcha_with_tesseract", return_value=None):
                with self.assertRaisesRegex(RuntimeError, "captcha OCR could not solve"):
                    read_captcha(path=Path(tmp) / "captcha.png", use_ocr=True, allow_manual_fallback=False)

    def test_login_retry_raises_captcha_escalation_after_automated_exhaustion(self):
        last_error = LoginAttemptError("login failed", captcha_image_bytes=b"image-data", captcha_text="ABC123")
        with patch("archershub.auth.login_once", side_effect=last_error):
            from archershub.auth import login_with_retry

            with self.assertRaises(AutomatedCaptchaEscalation) as ctx:
                login_with_retry(
                    "https://example.com",
                    "StudentLogin",
                    "user",
                    "pass",
                    max_attempts=5,
                    manual_captcha_fallback=False,
                    save_artifacts=False,
                )
        self.assertEqual(ctx.exception.attempts, 5)
        self.assertEqual(ctx.exception.image_bytes, b"image-data")


class BotCaptchaEscalationTests(unittest.IsolatedAsyncioTestCase):
    async def test_verify_credentials_sends_captcha_image_after_automated_failures(self):
        with tempfile.TemporaryDirectory() as tmp:
            storage = SQLiteStorage(f"{tmp}/bot.sqlite3")
            user = storage.redeem_registration_code(storage.generate_registration_code(), 555, "tester")
            sent = []

            async def send_captcha_image(chat_id: int, image_bytes: bytes, caption: str) -> None:
                sent.append((chat_id, image_bytes, caption))

            service = BotArchersHubService(
                storage,
                SecretBox.from_secret("dev-secret"),
                send_captcha_image=send_captcha_image,
            )

            with patch(
                "archershub.bot.service.login_with_retry",
                side_effect=AutomatedCaptchaEscalation(5, b"captcha-bytes", RuntimeError("ocr failed")),
            ):
                with self.assertRaises(TelegramCaptchaRequired):
                    await service.verify_and_store_credentials(
                        user_id=user.id,
                        username="student",
                        password="secret",
                    )

            self.assertEqual(len(sent), 1)
            self.assertEqual(sent[0][0], 555)
            self.assertEqual(sent[0][1], b"captcha-bytes")
            self.assertIn("failed after 5 attempts", sent[0][2])


if __name__ == "__main__":
    unittest.main()
