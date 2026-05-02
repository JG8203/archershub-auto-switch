from __future__ import annotations

import asyncio
from io import BytesIO
import os

from fastapi import FastAPI, Header, HTTPException, Request
from telegram import Update
from telegram.ext import Application

from ..crypto import SecretBox
from ..scheduler import WatchScheduler
from ..storage import SQLiteStorage
from .handlers import TelegramControlPanel
from .service import BotArchersHubService


def create_app() -> FastAPI:
    token = os.environ["BOT_TOKEN"]
    webhook_secret = os.getenv("TELEGRAM_WEBHOOK_SECRET", "")
    storage = SQLiteStorage(os.getenv("ARCHERSHUB_DB", "archershub_bot.sqlite3"))
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
    )
    tg_app.add_handlers(TelegramControlPanel(storage, ah_service, scheduler).build_handlers())

    app = FastAPI(title="ArchersHub Telegram Service")
    app.state.storage = storage
    app.state.telegram = tg_app
    app.state.scheduler = scheduler
    app.state.scheduler_task = None

    @app.on_event("startup")
    async def startup() -> None:
        await tg_app.initialize()
        await tg_app.start()
        app.state.scheduler_task = asyncio.create_task(app.state.scheduler.run_forever())

    @app.on_event("shutdown")
    async def shutdown() -> None:
        app.state.scheduler.stop()
        if app.state.scheduler_task:
            await app.state.scheduler_task
        await tg_app.stop()
        await tg_app.shutdown()

    @app.get("/healthz")
    async def healthz():
        jobs = storage.list_jobs()
        job_runtime = storage.list_job_runtime()
        user_runtime = storage.list_user_runtime()
        return {
            "ok": True,
            "scheduler": storage.get_scheduler_status(),
            "jobs_total": len(jobs),
            "jobs_active": sum(1 for job in jobs if job.enabled and job.completed_at is None and job.paused_at is None),
            "jobs_paused": sum(1 for job in jobs if job.paused_at),
            "jobs_completed": sum(1 for job in jobs if job.completed_at),
            "jobs_failing": sum(1 for row in job_runtime if row.failure_count > 0),
            "pending_confirmations": len(storage.list_pending_actions()),
            "users_needing_captcha": sum(1 for row in user_runtime if row.needs_captcha),
            "users_with_login_errors": sum(1 for row in user_runtime if row.last_login_error),
        }

    @app.post("/telegram/webhook")
    async def telegram_webhook(request: Request, x_telegram_bot_api_secret_token: str | None = Header(default=None)):
        if webhook_secret and x_telegram_bot_api_secret_token != webhook_secret:
            raise HTTPException(status_code=403, detail="bad webhook secret")
        payload = await request.json()
        update = Update.de_json(payload, tg_app.bot)
        await tg_app.process_update(update)
        return {"ok": True}

    return app


app = create_app()
