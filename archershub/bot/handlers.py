from __future__ import annotations

import logging
from typing import Any

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update, constants
from telegram.ext import CommandHandler, ContextTypes, MessageHandler, ConversationHandler, filters

from ..scheduler import WatchScheduler
from ..sections import normalize_section_name
from ..storage import JOB_MODE_AUTO, JOB_MODE_NOTIFY, JOB_TYPE_ADD_CLASS, JOB_TYPE_CHANGE_SECTION, JOB_TYPE_WATCH, SQLiteStorage
from .messages import delete_message_safely
from .parsing import MODE_VALUES, parse_addclass_specs
from .service import BotArchersHubService, TelegramCaptchaRequired

ASK_USERNAME, ASK_PASSWORD = range(2)


class TelegramControlPanel:
    def __init__(self, storage: SQLiteStorage, archershub: BotArchersHubService, scheduler: WatchScheduler | None = None) -> None:
        self.storage = storage
        self.archershub = archershub
        self.scheduler = scheduler

    def build_handlers(self):
        return [
            CommandHandler("start", self.start),
            CommandHandler("help", self.help),
            ConversationHandler(
                entry_points=[CommandHandler("connect", self.connect)],
                states={
                    ASK_USERNAME: [MessageHandler(filters.TEXT & ~filters.COMMAND, self.received_username)],
                    ASK_PASSWORD: [MessageHandler(filters.TEXT & ~filters.COMMAND, self.received_password)],
                },
                fallbacks=[CommandHandler("cancel", self.cancel)],
                name="connect_archershub",
                persistent=False,
            ),
            CommandHandler("watch", self.watch),
            CommandHandler("change", self.change_section_job),
            CommandHandler("addclass", self.add_class_job),
            CommandHandler("setmode", self.set_mode),
            CommandHandler("setpriorities", self.set_priorities),
            CommandHandler("retarget", self.retarget_job),
            CommandHandler("pause", self.pause_job),
            CommandHandler("resume", self.resume_job),
            CommandHandler("checknow", self.check_now),
            CommandHandler("summary", self.summary),
            CommandHandler("confirm", self.confirm_job),
            CommandHandler("reject", self.reject_job),
            CommandHandler("jobs", self.jobs),
            CommandHandler("remove", self.remove),
        ]

    async def start(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        chat = update.effective_chat
        if chat is None or update.effective_user is None:
            return
        existing = self.storage.get_user_by_telegram_id(chat.id)
        if existing:
            await update.effective_message.reply_text("You are registered. Use /connect to set ArchersHub credentials or /watch to add watchers.")
            return
        if not ctx.args:
            await update.effective_message.reply_text("Send /start <one-time-code> to register.")
            return
        try:
            user = self.storage.redeem_registration_code(ctx.args[0], chat.id, update.effective_user.username)
        except ValueError as exc:
            await update.effective_message.reply_text(f"Registration failed: {exc}")
            return
        await update.effective_message.reply_text(
            "Registration complete. Next: /connect to verify your ArchersHub account.",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("Connect ArchersHub", callback_data="connect")]]),
        )
        logging.info("registered telegram_id=%s as user_id=%s", chat.id, user.id)

    async def help(self, update: Update, _ctx: ContextTypes.DEFAULT_TYPE) -> None:
        await update.effective_message.reply_text(
            "Commands:\n"
            "/start <code> - register with a one-time code\n"
            "/connect - save and verify ArchersHub credentials\n"
            "/watch COURSE [SECTION ...] - watch all sections or named sections\n"
            "/change COURSE TARGET_SECTION [notify|confirm|auto] - create a future change-section automation job\n"
            "/addclass COURSE[:SEC1,SEC2] [COURSE2[:SEC1,SEC2] ...] [notify|confirm|auto] - create one or more add-class automation jobs\n"
            "/setmode JOB_ID notify|confirm|auto - edit a job mode\n"
            "/setpriorities JOB_ID SEC1 [SEC2 ...] - edit add-class priorities\n"
            "/retarget JOB_ID SECTION - edit change-section target\n"
            "/pause JOB_ID - pause a job without removing it\n"
            "/resume JOB_ID - resume a paused job\n"
            "/checknow [JOB_ID] - run immediate checks for all your active jobs or one job\n"
            "/summary - show a compact status summary\n"
            "/confirm JOB_ID - execute a pending confirmation job now\n"
            "/reject JOB_ID - clear a pending confirmation request\n"
            "/jobs - list your jobs\n"
            "/remove JOB_ID - disable a job\n"
            "/cancel - cancel setup"
        )

    async def connect(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
        if not self._registered(update):
            await update.effective_message.reply_text("Register first with /start <code>.")
            return ConversationHandler.END
        ctx.user_data.clear()
        await update.effective_message.reply_text("Send your ArchersHub username/email. Use /cancel to stop.")
        return ASK_USERNAME

    async def received_username(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
        ctx.user_data["archershub_username"] = update.effective_message.text.strip()
        await delete_message_safely(update.effective_message)
        await update.effective_chat.send_message("Now send your ArchersHub password. I will try to delete the message after processing.")
        return ASK_PASSWORD

    async def received_password(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
        chat = update.effective_chat
        user = self.storage.get_user_by_telegram_id(chat.id)
        username = ctx.user_data.get("archershub_username", "")
        password = update.effective_message.text
        await delete_message_safely(update.effective_message)
        status = await chat.send_message("Verifying ArchersHub login with automated captcha solving.")
        try:
            await self.archershub.verify_and_store_credentials(user_id=user.id, username=username, password=password)
        except TelegramCaptchaRequired as exc:
            await status.edit_text(str(exc))
            return ConversationHandler.END
        except Exception as exc:
            await status.edit_text(f"Login failed: {exc}")
            return ConversationHandler.END
        finally:
            ctx.user_data.clear()
        await status.edit_text("ArchersHub credentials verified and stored encrypted.")
        return ConversationHandler.END

    async def cancel(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
        ctx.user_data.clear()
        await update.effective_message.reply_text("Cancelled.")
        return ConversationHandler.END

    async def watch(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        user = self._registered(update)
        if not user:
            await update.effective_message.reply_text("Register first with /start <code>.")
            return
        if not ctx.args:
            await update.effective_message.reply_text("Usage: /watch COURSE [SECTION ...]")
            return
        course = ctx.args[0].upper()
        sections = [normalize_section_name(arg) for arg in ctx.args[1:]]
        job = self.storage.add_job(
            user_id=user.id,
            job_type=JOB_TYPE_WATCH,
            mode=JOB_MODE_NOTIFY,
            course_code=course,
            section_filters=sections,
        )
        target = "all sections" if not sections else ", ".join(sections)
        await update.effective_message.reply_text(f"Watcher #{job.id} added for {course}: {target}.")

    async def change_section_job(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        user = self._registered(update)
        if not user:
            await update.effective_message.reply_text("Register first with /start <code>.")
            return
        if len(ctx.args) < 2:
            await update.effective_message.reply_text("Usage: /change COURSE TARGET_SECTION [notify|confirm|auto]")
            return
        mode = self._mode_from_args(ctx.args[2:])
        job = self.storage.add_job(
            user_id=user.id,
            job_type=JOB_TYPE_CHANGE_SECTION,
            mode=mode,
            course_code=ctx.args[0].upper(),
            target_section=normalize_section_name(ctx.args[1]),
        )
        await update.effective_message.reply_text(
            f"Change-section automation job #{job.id} saved in {mode} mode. "
            "Mutation execution is handled by the automation phase worker."
        )

    async def add_class_job(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        user = self._registered(update)
        if not user:
            await update.effective_message.reply_text("Register first with /start <code>.")
            return
        if not ctx.args:
            await update.effective_message.reply_text("Usage: /addclass COURSE[:SEC1,SEC2] [COURSE2[:SEC1,SEC2] ...] [notify|confirm|auto]")
            return
        mode = self._mode_from_args(ctx.args)
        try:
            specs = parse_addclass_specs(ctx.args)
        except ValueError as exc:
            await update.effective_message.reply_text(f"Invalid addclass request: {exc}")
            return
        jobs = []
        for course_code, priorities in specs:
            jobs.append(
                self.storage.add_job(
                    user_id=user.id,
                    job_type=JOB_TYPE_ADD_CLASS,
                    mode=mode,
                    course_code=course_code,
                    priority_sections=priorities,
                )
            )
        lines = []
        for job in jobs:
            target = "section-name fallback order" if not job.priority_sections else ", ".join(job.priority_sections)
            lines.append(f"#{job.id} {job.course_code} priorities={target}")
        await update.effective_message.reply_text(
            "Saved add-class automation jobs in "
            f"{mode} mode:\n" + "\n".join(lines) + "\nNo job will displace existing classes by default."
        )

    async def set_mode(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        job = await self._owned_job_or_reply(update, ctx, usage="Usage: /setmode JOB_ID notify|confirm|auto")
        if job is None:
            return
        if len(ctx.args) < 2 or ctx.args[1].lower() not in MODE_VALUES:
            await update.effective_message.reply_text("Usage: /setmode JOB_ID notify|confirm|auto")
            return
        self.storage.update_job_mode(job.id, ctx.args[1].lower())
        await update.effective_message.reply_text(f"Updated job #{job.id} mode to {ctx.args[1].lower()}.")

    async def set_priorities(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        job = await self._owned_job_or_reply(update, ctx, usage="Usage: /setpriorities JOB_ID SEC1 [SEC2 ...]")
        if job is None:
            return
        if job.job_type != JOB_TYPE_ADD_CLASS:
            await update.effective_message.reply_text("Priority editing is only available for add-class jobs.")
            return
        priorities = [normalize_section_name(arg) for arg in ctx.args[1:]]
        self.storage.update_job_priority_sections(job.id, priorities)
        await update.effective_message.reply_text(
            f"Updated priorities for job #{job.id}: "
            f"{', '.join(priorities) if priorities else 'fallback order only'}."
        )

    async def retarget_job(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        job = await self._owned_job_or_reply(update, ctx, usage="Usage: /retarget JOB_ID SECTION")
        if job is None:
            return
        if job.job_type != JOB_TYPE_CHANGE_SECTION or len(ctx.args) < 2:
            await update.effective_message.reply_text("Usage: /retarget JOB_ID SECTION")
            return
        target = normalize_section_name(ctx.args[1])
        self.storage.update_job_target_section(job.id, target)
        await update.effective_message.reply_text(f"Updated job #{job.id} target to {target}.")

    async def pause_job(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        job = await self._owned_job_or_reply(update, ctx, usage="Usage: /pause JOB_ID")
        if job is None:
            return
        self.storage.pause_job(job.id)
        await update.effective_message.reply_text(f"Paused job #{job.id}.")

    async def resume_job(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        job = await self._owned_job_or_reply(update, ctx, usage="Usage: /resume JOB_ID")
        if job is None:
            return
        self.storage.resume_job(job.id)
        await update.effective_message.reply_text(f"Resumed job #{job.id}.")

    async def check_now(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        user = self._registered(update)
        if not user:
            await update.effective_message.reply_text("Register first with /start <code>.")
            return
        if self.scheduler is None:
            await update.effective_message.reply_text("Manual checks are unavailable right now.")
            return
        job_ids = None
        if ctx.args:
            if not ctx.args[0].isdigit():
                await update.effective_message.reply_text("Usage: /checknow [JOB_ID]")
                return
            job_ids = {int(ctx.args[0])}
        status = await update.effective_message.reply_text("Running an immediate check...")
        result = await self.scheduler.run_selected(user_id=user.id, job_ids=job_ids)
        await status.edit_text(
            f"Check complete. processed={result.checked_jobs} notified={result.notifications_sent} "
            f"errors={len(result.errors)}"
        )

    async def summary(self, update: Update, _ctx: ContextTypes.DEFAULT_TYPE) -> None:
        user = self._registered(update)
        if not user:
            await update.effective_message.reply_text("Register first with /start <code>.")
            return
        jobs = self.storage.list_jobs(user_id=user.id)
        pending = self.storage.list_pending_actions(user_id=user.id)
        runtime_by_job = {row.job_id: row for row in self.storage.list_job_runtime()}
        active = sum(1 for job in jobs if job.enabled and job.completed_at is None and job.paused_at is None)
        paused = sum(1 for job in jobs if job.paused_at)
        completed = sum(1 for job in jobs if job.completed_at)
        failing = sum(1 for job in jobs if (runtime_by_job.get(job.id) and runtime_by_job[job.id].failure_count > 0))
        user_runtime = self.storage.get_user_runtime(user.id)
        captcha = "yes" if user_runtime and user_runtime.needs_captcha else "no"
        await update.effective_message.reply_text(
            "Summary:\n"
            f"jobs={len(jobs)} active={active} paused={paused} completed={completed}\n"
            f"pending_confirmations={len(pending)} failing_jobs={failing}\n"
            f"captcha_needed={captcha}"
        )

    async def jobs(self, update: Update, _ctx: ContextTypes.DEFAULT_TYPE) -> None:
        user = self._registered(update)
        if not user:
            await update.effective_message.reply_text("Register first with /start <code>.")
            return
        jobs = self.storage.list_jobs(user_id=user.id)
        pending_by_job = {item.job_id: item for item in self.storage.list_pending_actions(user_id=user.id)}
        if not jobs:
            await update.effective_message.reply_text("No jobs yet. Use /watch COURSE [SECTION ...].")
            return
        lines = []
        for job in jobs:
            status = "completed" if job.completed_at else ("paused" if job.paused_at else ("enabled" if job.enabled else "disabled"))
            if job.id in pending_by_job:
                status = f"{status}, pending-confirm:{pending_by_job[job.id].target_section or '-'}"
            runtime = self.storage.get_job_runtime(job.id)
            if runtime and runtime.failure_count > 0:
                status = f"{status}, failures={runtime.failure_count}"
            sections = "all" if not job.section_filters else ",".join(job.section_filters)
            target = f" target={job.target_section}" if job.target_section else ""
            priorities = f" priorities={','.join(job.priority_sections)}" if job.priority_sections else ""
            lines.append(f"#{job.id} {job.job_type} mode={job.mode} {job.course_code} [{sections}]{target}{priorities} {status}")
        await update.effective_message.reply_text("\n".join(lines))

    async def confirm_job(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        user = self._registered(update)
        if not user:
            await update.effective_message.reply_text("Register first with /start <code>.")
            return
        if not ctx.args or not ctx.args[0].isdigit():
            await update.effective_message.reply_text("Usage: /confirm JOB_ID")
            return
        job_id = int(ctx.args[0])
        job = self.storage.get_job(job_id)
        pending = self.storage.get_pending_action(job_id)
        if job is None or job.user_id != user.id:
            await update.effective_message.reply_text("That job was not found.")
            return
        if pending is None:
            await update.effective_message.reply_text("That job does not have a pending confirmation request.")
            return
        status = await update.effective_message.reply_text("Rechecking availability and submitting now...")
        try:
            message = await self.archershub.execute_automation_job(job)
        except Exception as exc:
            self.storage.clear_pending_action(job_id)
            await status.edit_text(f"Confirmation failed: {exc}")
            return
        self.storage.complete_job(job_id)
        await status.edit_text(message)

    async def reject_job(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        user = self._registered(update)
        if not user:
            await update.effective_message.reply_text("Register first with /start <code>.")
            return
        if not ctx.args or not ctx.args[0].isdigit():
            await update.effective_message.reply_text("Usage: /reject JOB_ID")
            return
        job_id = int(ctx.args[0])
        job = self.storage.get_job(job_id)
        if job is None or job.user_id != user.id:
            await update.effective_message.reply_text("That job was not found.")
            return
        self.storage.clear_pending_action(job_id)
        await update.effective_message.reply_text(f"Cleared pending confirmation for job #{job_id}.")

    async def remove(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        if not self._registered(update):
            await update.effective_message.reply_text("Register first with /start <code>.")
            return
        if not ctx.args or not ctx.args[0].isdigit():
            await update.effective_message.reply_text("Usage: /remove JOB_ID")
            return
        self.storage.disable_job(int(ctx.args[0]))
        await update.effective_message.reply_text(f"Disabled job #{ctx.args[0]}.")

    def _registered(self, update: Update):
        chat = update.effective_chat
        return self.storage.get_user_by_telegram_id(chat.id) if chat else None

    async def _owned_job_or_reply(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE, *, usage: str):
        user = self._registered(update)
        if not user:
            await update.effective_message.reply_text("Register first with /start <code>.")
            return None
        if not ctx.args or not ctx.args[0].isdigit():
            await update.effective_message.reply_text(usage)
            return None
        job = self.storage.get_job(int(ctx.args[0]))
        if job is None or job.user_id != user.id:
            await update.effective_message.reply_text("That job was not found.")
            return None
        return job

    @staticmethod
    def _mode_from_args(args: list[str]) -> str:
        for arg in args:
            lowered = arg.lower()
            if lowered in MODE_VALUES:
                return lowered
        return JOB_MODE_AUTO
