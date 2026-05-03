from __future__ import annotations

import asyncio
import argparse
from io import BytesIO
import logging
import os

from telegram.ext import Application

from ..crypto import SecretBox
from ..env import load_project_env
from ..scheduler import WatchScheduler
from ..storage import SQLiteStorage
from .handlers import TelegramControlPanel
from .service import BotArchersHubService


def build_application() -> Application:
    load_project_env()
    token = os.environ["BOT_TOKEN"]
    storage = SQLiteStorage(os.getenv("ARCHERSHUB_DB", "archershub_bot.sqlite3"))
    duplicate_groups = storage.list_duplicate_active_jobs()
    for jobs in duplicate_groups:
        ids = ", ".join(f"#{job.id}" for job in jobs)
        sample = jobs[0]
        logging.warning(
            "duplicate active Telegram bot jobs found on startup: user_id=%s type=%s course=%s jobs=%s",
            sample.user_id,
            sample.job_type,
            sample.course_code,
            ids,
        )
    tg_app = Application.builder().token(token).build()

    async def send_captcha_image(chat_id: int, image_bytes: bytes, caption: str) -> None:
        image = BytesIO(image_bytes)
        image.name = "captcha.png"
        await tg_app.bot.send_photo(chat_id=chat_id, photo=image, caption=caption)

    secret_box = SecretBox.from_env()
    ah_service = BotArchersHubService(storage, secret_box, send_captcha_image=send_captcha_image)
    scheduler = WatchScheduler(
        storage,
        fetch_course=ah_service.fetch_course_for_job,
        send_message=lambda chat_id, text: tg_app.bot.send_message(chat_id=chat_id, text=text),
        inspect_automation=ah_service.inspect_automation_job,
        execute_automation=ah_service.execute_automation_job,
        execute_automation_batch=ah_service.execute_automation_batch,
    )
    tg_app.add_handlers(TelegramControlPanel(storage, ah_service, scheduler).build_handlers())
    tg_app.bot_data["storage"] = storage
    tg_app.bot_data["archershub_service"] = ah_service
    tg_app.bot_data["scheduler"] = scheduler
    tg_app.bot_data["scheduler_task"] = None
    return tg_app


async def start_background_scheduler(app: Application) -> None:
    scheduler = app.bot_data["scheduler"]
    app.bot_data["scheduler_task"] = asyncio.create_task(scheduler.run_forever())


async def stop_background_scheduler(app: Application) -> None:
    scheduler = app.bot_data.get("scheduler")
    if scheduler is None:
        return
    scheduler.stop()
    task = app.bot_data.get("scheduler_task")
    if task is not None:
        await task


async def clear_webhook_before_polling(app: Application) -> None:
    drop_updates = os.getenv("TELEGRAM_DROP_PENDING_UPDATES", "").lower() in {"1", "true", "yes"}
    await app.bot.delete_webhook(drop_pending_updates=drop_updates)
    ah_service = app.bot_data.get("archershub_service")
    if ah_service is not None:
        await ah_service.port_legacy_jobs(lambda chat_id, text: app.bot.send_message(chat_id=chat_id, text=text))
    await start_background_scheduler(app)


def main() -> None:
    parser = argparse.ArgumentParser(description="Run the ArchersHub Telegram bot with long polling")
    parser.parse_args()
    app = build_application()
    app.post_init = clear_webhook_before_polling
    app.post_stop = stop_background_scheduler
    app.run_polling(allowed_updates=None)


if __name__ == "__main__":
    main()
