from __future__ import annotations

import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock

from telegram.ext import CommandHandler, ConversationHandler

from archershub.bot.handlers import TelegramControlPanel
from archershub.scheduler import SchedulerCycleResult
from archershub.storage import JOB_MODE_AUTO, JOB_TYPE_ADD_CLASS, JOB_TYPE_CHANGE_SECTION, SQLiteStorage


class FakeMessage:
    def __init__(self, text: str = "") -> None:
        self.text = text
        self.replies: list[tuple[str, object | None, FakeMessage]] = []
        self.edits: list[str] = []
        self.deleted = False

    async def reply_text(self, text: str, reply_markup=None):
        message = FakeMessage()
        self.replies.append((text, reply_markup, message))
        return message

    async def edit_text(self, text: str):
        self.edits.append(text)

    async def delete(self):
        self.deleted = True


class FakeChat:
    def __init__(self, chat_id: int) -> None:
        self.id = chat_id
        self.sent: list[tuple[str, object | None, FakeMessage]] = []

    async def send_message(self, text: str, reply_markup=None):
        message = FakeMessage()
        self.sent.append((text, reply_markup, message))
        return message


def fake_update(chat_id: int, text: str = ""):
    chat = FakeChat(chat_id)
    message = FakeMessage(text)
    return SimpleNamespace(effective_chat=chat, effective_message=message, callback_query=None)


def command_names(handlers) -> set[str]:
    names: set[str] = set()
    for handler in handlers:
        if isinstance(handler, CommandHandler):
            names.update(handler.commands)
        elif isinstance(handler, ConversationHandler):
            for entry in handler.entry_points:
                if isinstance(entry, CommandHandler):
                    names.update(entry.commands)
    return names


class TelegramHandlerUxTests(unittest.IsolatedAsyncioTestCase):
    def test_help_explains_add_vs_change_and_watch_is_registered(self):
        with tempfile.TemporaryDirectory() as tmp:
            panel = TelegramControlPanel(SQLiteStorage(f"{tmp}/bot.sqlite3"), AsyncMock())
            commands = command_names(panel.build_handlers())
            self.assertIn("watch", commands)
            self.assertNotIn("summary", commands)
            self.assertNotIn("checknow", commands)
            self.assertIn("cancel", commands)
            self.assertIn("recheck", commands)
            text = panel.help_text()
            self.assertIn("Add class never drops/changes", text)
            self.assertIn("change-section feature, never drop-add", text)
            self.assertIn("/recheck", text)
            self.assertIn("/watch", text)

    async def test_successful_login_continues_to_onboarding_menu(self):
        with tempfile.TemporaryDirectory() as tmp:
            storage = SQLiteStorage(f"{tmp}/bot.sqlite3")
            user = storage.redeem_registration_code(storage.generate_registration_code(), 123, "tester")
            service = SimpleNamespace(verify_and_store_credentials=AsyncMock())
            panel = TelegramControlPanel(storage, service)
            update = fake_update(user.telegram_id, "secret")
            ctx = SimpleNamespace(user_data={"archershub_username": "student"})

            await panel.received_password(update, ctx)

            self.assertTrue(update.effective_message.deleted)
            self.assertIn("ArchersHub credentials verified", update.effective_chat.sent[0][2].edits[0])
            self.assertIn("What do you want to do next?", update.effective_chat.sent[1][0])
            self.assertIsNotNone(update.effective_chat.sent[1][1])

    async def test_add_and_change_wizard_create_auto_jobs(self):
        with tempfile.TemporaryDirectory() as tmp:
            storage = SQLiteStorage(f"{tmp}/bot.sqlite3")
            user = storage.redeem_registration_code(storage.generate_registration_code(), 456, "tester")
            panel = TelegramControlPanel(storage, AsyncMock())
            ctx = SimpleNamespace(user_data={})

            await panel.received_add_course(fake_update(user.telegram_id, "LCFAITH"), ctx)
            await panel.received_add_priorities(fake_update(user.telegram_id, "Z18, Z19"), ctx)

            ctx = SimpleNamespace(user_data={})
            await panel.received_change_course(fake_update(user.telegram_id, "GETEAMS"), ctx)
            await panel.received_change_section(fake_update(user.telegram_id, "S11"), ctx)

            jobs = storage.list_jobs(user_id=user.id)
            self.assertEqual(jobs[0].job_type, JOB_TYPE_ADD_CLASS)
            self.assertEqual(jobs[0].mode, JOB_MODE_AUTO)
            self.assertEqual(jobs[0].priority_sections, ["Z18", "Z19"])
            self.assertEqual(jobs[1].job_type, JOB_TYPE_CHANGE_SECTION)
            self.assertEqual(jobs[1].target_section, "S11")

    async def test_watch_command_creates_notification_job(self):
        with tempfile.TemporaryDirectory() as tmp:
            storage = SQLiteStorage(f"{tmp}/bot.sqlite3")
            user = storage.redeem_registration_code(storage.generate_registration_code(), 654, "tester")
            panel = TelegramControlPanel(storage, AsyncMock())
            update = fake_update(user.telegram_id)
            ctx = SimpleNamespace(args=["LCFAITH", "Z18", "Z19"])

            await panel.watch(update, ctx)

            job = storage.list_jobs(user_id=user.id)[0]
            self.assertEqual(job.job_type, "watch")
            self.assertEqual(job.section_filters, ["Z18", "Z19"])
            self.assertIn("Saved watch job", update.effective_message.replies[0][0])

    async def test_recheck_runs_selected_user_jobs(self):
        with tempfile.TemporaryDirectory() as tmp:
            storage = SQLiteStorage(f"{tmp}/bot.sqlite3")
            user = storage.redeem_registration_code(storage.generate_registration_code(), 789, "tester")
            job = storage.add_job(user_id=user.id, job_type=JOB_TYPE_ADD_CLASS, mode=JOB_MODE_AUTO, course_code="LCFAITH")
            scheduler = SimpleNamespace(run_selected=AsyncMock(return_value=SchedulerCycleResult(checked_jobs=1, notifications_sent=0, errors=[])))
            panel = TelegramControlPanel(storage, AsyncMock(), scheduler)
            update = fake_update(user.telegram_id)
            ctx = SimpleNamespace(args=[str(job.id)])

            await panel.recheck(update, ctx)

            scheduler.run_selected.assert_awaited_once_with(user_id=user.id, job_ids={job.id})
            self.assertIn("Rechecking now", update.effective_message.replies[0][0])
            self.assertIn("checked=1", update.effective_message.replies[0][2].edits[0])


if __name__ == "__main__":
    unittest.main()
