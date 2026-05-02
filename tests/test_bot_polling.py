from __future__ import annotations

import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import AsyncMock

from archershub.bot.polling import build_application, clear_webhook_before_polling, stop_background_scheduler
from archershub.env import load_project_env


class PollingBotTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.old_env = os.environ.copy()

    def tearDown(self) -> None:
        os.environ.clear()
        os.environ.update(self.old_env)

    def test_load_project_env_reads_dotenv_without_overriding_shell(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            old_cwd = Path.cwd()
            try:
                os.chdir(tmp)
                Path(".env").write_text(
                    "BOT_TOKEN=123:file-token\n"
                    "ARCHERSHUB_MASTER_KEY=file-secret\n"
                    "ARCHERSHUB_DB=file.sqlite3\n",
                    encoding="utf-8",
                )
                os.environ["BOT_TOKEN"] = "123:shell-token"

                load_project_env()

                self.assertEqual(os.environ["BOT_TOKEN"], "123:shell-token")
                self.assertEqual(os.environ["ARCHERSHUB_MASTER_KEY"], "file-secret")
                self.assertEqual(os.environ["ARCHERSHUB_DB"], "file.sqlite3")
            finally:
                os.chdir(old_cwd)

    def test_build_application_registers_scheduler_and_handlers(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            os.environ["BOT_TOKEN"] = "123456:test-token"
            os.environ["ARCHERSHUB_MASTER_KEY"] = "test-secret"
            os.environ["ARCHERSHUB_DB"] = str(Path(tmp) / "bot.sqlite3")

            app = build_application()

            self.assertIn("storage", app.bot_data)
            self.assertIn("scheduler", app.bot_data)
            self.assertTrue(app.handlers)

    async def test_clear_webhook_before_polling_starts_scheduler(self) -> None:
        class FakeScheduler:
            def __init__(self) -> None:
                self.stopped = False

            async def run_forever(self) -> None:
                while not self.stopped:
                    await self.event.wait()

            def stop(self) -> None:
                self.stopped = True
                self.event.set()

        scheduler = FakeScheduler()
        scheduler.event = __import__("asyncio").Event()
        app = type(
            "FakeApp",
            (),
            {
                "bot": type("FakeBot", (), {"delete_webhook": AsyncMock()})(),
                "bot_data": {"scheduler": scheduler, "scheduler_task": None},
            },
        )()

        os.environ["TELEGRAM_DROP_PENDING_UPDATES"] = "1"
        await clear_webhook_before_polling(app)

        app.bot.delete_webhook.assert_awaited_once_with(drop_pending_updates=True)
        self.assertIsNotNone(app.bot_data["scheduler_task"])

        await stop_background_scheduler(app)
        self.assertTrue(app.bot_data["scheduler_task"].done())


if __name__ == "__main__":
    unittest.main()
