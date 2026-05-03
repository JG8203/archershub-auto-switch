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

    def test_login_retry_fetches_fresh_page_and_captcha_each_attempt(self):
        class FakeResponse:
            def __init__(self, *, text="", url="https://example.com/StudentLogin", content=b"", data=None, status_code=200):
                self.text = text
                self.url = url
                self.content = content
                self._data = data or {}
                self.status_code = status_code

            def raise_for_status(self):
                pass

            def json(self):
                return self._data

        class FakeSession:
            def __init__(self):
                self.headers = {}
                self.cookies = {}
                self.get_urls = []

            def get(self, url, **_kwargs):
                self.get_urls.append(url)
                if "ShowCaptchaImage" in url:
                    return FakeResponse(content=f"captcha-{len(self.get_urls)}".encode())
                if "api.ipify.org" in url:
                    return FakeResponse(data={"ip": "127.0.0.1"})
                if "getEncryptPassword" in url:
                    return FakeResponse(data={"key": "1234567890123456", "iv": "6543210987654321"})
                return FakeResponse(
                    text='<form id="LoginForm" action="/StudentLogin/Login"><input type="hidden" name="__RequestVerificationToken" value="tok"></form>',
                    url="https://example.com/StudentLogin",
                )

            def post(self, url, **_kwargs):
                if "GetLoginConfigurationDetails" in url:
                    return FakeResponse(data={"IS_LOAD_TESTING": 1})
                return FakeResponse(text='<form id="LoginForm"></form>', url="https://example.com/StudentLogin")

        from archershub.auth import login_with_retry

        sessions = [FakeSession(), FakeSession()]
        with patch("archershub.auth.create_session", side_effect=sessions), patch("archershub.auth.read_captcha", return_value="ABC123"):
            with self.assertRaises(AutomatedCaptchaEscalation):
                login_with_retry(
                    "https://example.com",
                    "StudentLogin",
                    "user",
                    "pass",
                    max_attempts=2,
                    manual_captcha_fallback=False,
                    save_artifacts=False,
                )

        self.assertEqual(sum(any(url.endswith("/StudentLogin") for url in session.get_urls) for session in sessions), 2)
        self.assertEqual(sum(any("ShowCaptchaImage" in url for url in session.get_urls) for session in sessions), 2)


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
