from __future__ import annotations

import logging

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import CallbackQueryHandler, CommandHandler, ContextTypes, ConversationHandler, MessageHandler, filters

from ..scheduler import WatchScheduler
from ..sections import normalize_section_name
from ..storage import JOB_MODE_AUTO, JOB_MODE_NOTIFY, JOB_TYPE_ADD_CLASS, JOB_TYPE_CHANGE_SECTION, JOB_TYPE_WATCH, SQLiteStorage
from .messages import delete_message_safely
from .parsing import MODE_VALUES, parse_addclass_specs
from .service import BotArchersHubService, TelegramCaptchaRequired

ASK_USERNAME, ASK_PASSWORD, ASK_ADD_COURSE, ASK_ADD_PRIORITIES, ASK_CHANGE_COURSE, ASK_CHANGE_SECTION = range(6)


class TelegramControlPanel:
    def __init__(self, storage: SQLiteStorage, archershub: BotArchersHubService, scheduler: WatchScheduler | None = None) -> None:
        self.storage = storage
        self.archershub = archershub
        self.scheduler = scheduler

    def build_handlers(self):
        return [
            CommandHandler("start", self.start),
            ConversationHandler(
                entry_points=[CommandHandler("connect", self.connect), CallbackQueryHandler(self.connect, pattern="^connect$")],
                states={
                    ASK_USERNAME: [MessageHandler(filters.TEXT & ~filters.COMMAND, self.received_username)],
                    ASK_PASSWORD: [MessageHandler(filters.TEXT & ~filters.COMMAND, self.received_password)],
                },
                fallbacks=[CommandHandler("cancel", self.cancel)],
                name="connect_archershub",
                persistent=False,
            ),
            ConversationHandler(
                entry_points=[CallbackQueryHandler(self.begin_add_wizard, pattern="^menu:add$")],
                states={
                    ASK_ADD_COURSE: [MessageHandler(filters.TEXT & ~filters.COMMAND, self.received_add_course)],
                    ASK_ADD_PRIORITIES: [MessageHandler(filters.TEXT & ~filters.COMMAND, self.received_add_priorities)],
                },
                fallbacks=[CommandHandler("cancel", self.cancel)],
                name="add_class_wizard",
                persistent=False,
            ),
            ConversationHandler(
                entry_points=[CallbackQueryHandler(self.begin_change_wizard, pattern="^menu:change$")],
                states={
                    ASK_CHANGE_COURSE: [MessageHandler(filters.TEXT & ~filters.COMMAND, self.received_change_course)],
                    ASK_CHANGE_SECTION: [MessageHandler(filters.TEXT & ~filters.COMMAND, self.received_change_section)],
                },
                fallbacks=[CommandHandler("cancel", self.cancel)],
                name="change_section_wizard",
                persistent=False,
            ),
            CommandHandler("help", self.help),
            CommandHandler("watch", self.watch),
            CommandHandler("change", self.change_section_job),
            CommandHandler("addclass", self.add_class_job),
            CommandHandler("setmode", self.set_mode),
            CommandHandler("setpriorities", self.set_priorities),
            CommandHandler("retarget", self.retarget_job),
            CommandHandler("confirm", self.confirm_job),
            CommandHandler("reject", self.reject_job),
            CommandHandler("jobs", self.jobs),
            CommandHandler("recheck", self.recheck),
            CommandHandler("remove", self.remove),
            CommandHandler("cancel", self.cancel),
            CallbackQueryHandler(self.menu_callback, pattern="^menu:(jobs|help)$"),
        ]

    @staticmethod
    def main_menu_markup() -> InlineKeyboardMarkup:
        return InlineKeyboardMarkup(
            [
                [InlineKeyboardButton("➕ Add a class", callback_data="menu:add")],
                [InlineKeyboardButton("🔁 Change section", callback_data="menu:change")],
                [InlineKeyboardButton("📋 My jobs", callback_data="menu:jobs"), InlineKeyboardButton("❓ Help", callback_data="menu:help")],
            ]
        )

    @staticmethod
    def onboarding_text() -> str:
        return (
            "What do you want to do next?\n\n"
            "➕ Add a class: use this for a course you are not enlisted in yet. "
            "I will try open sections in your priority order, then safe fallback sections. "
            "I will not drop or change existing classes to make room.\n\n"
            "🔁 Change section: use this when you already have the class and only want a different section. "
            "This uses ArchersHub's change-section function only, never drop-add."
        )

    async def start(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        chat = update.effective_chat
        if chat is None or update.effective_user is None:
            return
        existing = self.storage.get_user_by_telegram_id(chat.id)
        if existing:
            await update.effective_message.reply_text(
                "You are registered. Use /connect to update your ArchersHub login, or choose what to do next.",
                reply_markup=self.main_menu_markup(),
            )
            return
        if not ctx.args:
            await update.effective_message.reply_text(
                "Welcome! Register first with your one-time code:\n\n"
                "/start YOUR_CODE\n\n"
                "Ask the service admin for a code if you do not have one yet."
            )
            return
        try:
            user = self.storage.redeem_registration_code(ctx.args[0], chat.id, update.effective_user.username)
        except ValueError as exc:
            await update.effective_message.reply_text(f"Registration failed: {exc}")
            return
        await update.effective_message.reply_text(
            "Registration complete. Next, connect your ArchersHub account so I can check sections for you.",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("Connect ArchersHub", callback_data="connect")]]),
        )
        logging.info("registered telegram_id=%s as user_id=%s", chat.id, user.id)

    async def help(self, update: Update, _ctx: ContextTypes.DEFAULT_TYPE) -> None:
        await update.effective_message.reply_text(self.help_text(), reply_markup=self.main_menu_markup())

    @staticmethod
    def help_text() -> str:
        return (
            "ArchersHub Bot Help\n\n"
            "Create jobs:\n"
            "• /watch LCFAITH Z18 Z19 — notify when matching sections get slots.\n"
            "• /addclass LCFAITH:Z18,Z19 — add a class you do not have yet. Priorities are optional.\n"
            "• /addclass LCFAITH:Z18,Z19 GETEAMS:S11 confirm — add multiple classes and ask before submitting.\n"
            "• /change LCFAITH Z18 — change an existing class to section Z18.\n\n"
            "Difference:\n"
            "• Add class never drops/changes your current classes to solve conflicts.\n"
            "• Change section only uses ArchersHub's change-section feature, never drop-add.\n\n"
            "Manage jobs:\n"
            "• /jobs — list saved jobs.\n"
            "• /recheck — force-check all active jobs now.\n"
            "• /recheck 12 — force-check only job #12 now.\n"
            "• /remove 12 — disable job #12.\n"
            "• /setmode 12 confirm — change mode to notify, confirm, or auto.\n"
            "• /setpriorities 12 Z18 Z19 — edit add-class priority sections.\n"
            "• /retarget 13 Z20 — edit a change-section target.\n\n"
            "Confirm mode:\n"
            "• /confirm 12 — recheck and submit a pending request.\n"
            "• /reject 12 — clear a pending request.\n\n"
            "Modes: auto submits when safe, confirm asks first, notify only alerts."
        )

    async def menu_callback(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        if update.callback_query:
            await update.callback_query.answer()
        action = update.callback_query.data if update.callback_query else ""
        if action == "menu:jobs":
            await self.jobs(update, ctx)
        elif action == "menu:help":
            await self.help(update, ctx)

    async def connect(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
        if update.callback_query:
            await update.callback_query.answer()
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
        status = await chat.send_message("Verifying ArchersHub login with a fresh captcha on every automated attempt.")
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
        await chat.send_message(self.onboarding_text(), reply_markup=self.main_menu_markup())
        return ConversationHandler.END

    async def cancel(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
        ctx.user_data.clear()
        await update.effective_message.reply_text("Cancelled. Use /help or the menu below when you are ready.", reply_markup=self.main_menu_markup())
        return ConversationHandler.END

    async def begin_add_wizard(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
        await update.callback_query.answer()
        if not self._registered(update):
            await update.effective_message.reply_text("Register first with /start <code>.")
            return ConversationHandler.END
        ctx.user_data.clear()
        await update.effective_message.reply_text(
            "Add a class\n\n"
            "Use this for a course you are not enlisted in yet. I will not drop or change existing classes.\n\n"
            "Send the course code, e.g. LCFAITH."
        )
        return ASK_ADD_COURSE

    async def received_add_course(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
        ctx.user_data["add_course_code"] = update.effective_message.text.strip().upper()
        await update.effective_message.reply_text(
            "Optional: send priority sections separated by spaces or commas, e.g. Z18 Z19.\n"
            "Send '-' to skip priorities and use the first safe open section."
        )
        return ASK_ADD_PRIORITIES

    async def received_add_priorities(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
        user = self._registered(update)
        if not user:
            await update.effective_message.reply_text("Register first with /start <code>.")
            return ConversationHandler.END
        text = update.effective_message.text.strip()
        priorities = [] if text in {"-", "skip", "SKIP"} else [normalize_section_name(part) for part in text.replace(",", " ").split()]
        job = self.storage.add_job(
            user_id=user.id,
            job_type=JOB_TYPE_ADD_CLASS,
            mode=JOB_MODE_AUTO,
            course_code=ctx.user_data.get("add_course_code", ""),
            priority_sections=priorities,
        )
        ctx.user_data.clear()
        await update.effective_message.reply_text(self._add_job_confirmation(job), reply_markup=self.main_menu_markup())
        return ConversationHandler.END

    async def begin_change_wizard(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
        await update.callback_query.answer()
        if not self._registered(update):
            await update.effective_message.reply_text("Register first with /start <code>.")
            return ConversationHandler.END
        ctx.user_data.clear()
        await update.effective_message.reply_text(
            "Change section\n\n"
            "Use this when you already have the class and want a different section. "
            "This uses ArchersHub's change-section function only; it never drop-adds.\n\n"
            "Send the course code, e.g. LCFAITH."
        )
        return ASK_CHANGE_COURSE

    async def received_change_course(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
        ctx.user_data["change_course_code"] = update.effective_message.text.strip().upper()
        await update.effective_message.reply_text("Send the target section, e.g. Z18.")
        return ASK_CHANGE_SECTION

    async def received_change_section(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
        user = self._registered(update)
        if not user:
            await update.effective_message.reply_text("Register first with /start <code>.")
            return ConversationHandler.END
        target = normalize_section_name(update.effective_message.text.strip())
        job = self.storage.add_job(
            user_id=user.id,
            job_type=JOB_TYPE_CHANGE_SECTION,
            mode=JOB_MODE_AUTO,
            course_code=ctx.user_data.get("change_course_code", ""),
            target_section=target,
        )
        ctx.user_data.clear()
        await update.effective_message.reply_text(self._change_job_confirmation(job), reply_markup=self.main_menu_markup())
        return ConversationHandler.END

    async def change_section_job(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        user = self._registered(update)
        if not user:
            await update.effective_message.reply_text("Register first with /start <code>.")
            return
        if len(ctx.args) < 2:
            await update.effective_message.reply_text(
                "Usage: /change COURSE TARGET_SECTION [notify|confirm|auto]\n"
                "Example: /change LCFAITH Z18\n\n"
                "Use this only for a class you already have. It uses ArchersHub change-section, never drop-add."
            )
            return
        mode = self._mode_from_args(ctx.args[2:])
        job = self.storage.add_job(
            user_id=user.id,
            job_type=JOB_TYPE_CHANGE_SECTION,
            mode=mode,
            course_code=ctx.args[0].upper(),
            target_section=normalize_section_name(ctx.args[1]),
        )
        await update.effective_message.reply_text(self._change_job_confirmation(job))

    async def add_class_job(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        user = self._registered(update)
        if not user:
            await update.effective_message.reply_text("Register first with /start <code>.")
            return
        if not ctx.args:
            await update.effective_message.reply_text(
                "Usage: /addclass COURSE[:SEC1,SEC2] [COURSE2[:SEC1,SEC2] ...] [notify|confirm|auto]\n"
                "Example: /addclass LCFAITH:Z18,Z19\n"
                "Example: /addclass LCFAITH:Z18,Z19 GETEAMS:S11 confirm\n\n"
                "Use this for classes you do not have yet. I will not drop/change existing classes."
            )
            return
        mode = self._mode_from_args(ctx.args)
        try:
            specs = parse_addclass_specs(ctx.args)
        except ValueError as exc:
            await update.effective_message.reply_text(f"Invalid add-class request: {exc}\nExample: /addclass LCFAITH:Z18,Z19")
            return
        jobs = [
            self.storage.add_job(
                user_id=user.id,
                job_type=JOB_TYPE_ADD_CLASS,
                mode=mode,
                course_code=course_code,
                priority_sections=priorities,
            )
            for course_code, priorities in specs
        ]
        await update.effective_message.reply_text("\n\n".join(self._add_job_confirmation(job) for job in jobs))

    async def watch(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        user = self._registered(update)
        if not user:
            await update.effective_message.reply_text("Register first with /start <code>.")
            return
        if not ctx.args:
            await update.effective_message.reply_text(
                "Usage: /watch COURSE [SECTION ...]\n"
                "Example: /watch LCFAITH\n"
                "Example: /watch LCFAITH Z18 Z19\n\n"
                "This only notifies you when matching sections have available slots. It never submits changes."
            )
            return
        sections = [normalize_section_name(arg) for arg in ctx.args[1:]]
        job = self.storage.add_job(
            user_id=user.id,
            job_type=JOB_TYPE_WATCH,
            mode=JOB_MODE_NOTIFY,
            course_code=ctx.args[0].upper(),
            section_filters=sections,
        )
        target = "all sections" if not sections else ", ".join(sections)
        await update.effective_message.reply_text(
            f"Saved watch job #{job.id} for {job.course_code}: {target}.\n"
            "I will only notify when matching sections gain available slots."
        )

    async def set_mode(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        job = await self._owned_job_or_reply(update, ctx, usage="Usage: /setmode JOB_ID notify|confirm|auto\nExample: /setmode 12 confirm")
        if job is None:
            return
        if len(ctx.args) < 2 or ctx.args[1].lower() not in MODE_VALUES:
            await update.effective_message.reply_text("Usage: /setmode JOB_ID notify|confirm|auto\nExample: /setmode 12 confirm")
            return
        self.storage.update_job_mode(job.id, ctx.args[1].lower())
        await update.effective_message.reply_text(f"Updated job #{job.id} mode to {ctx.args[1].lower()}.")

    async def set_priorities(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        job = await self._owned_job_or_reply(update, ctx, usage="Usage: /setpriorities JOB_ID SEC1 [SEC2 ...]\nExample: /setpriorities 12 Z18 Z19")
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
        job = await self._owned_job_or_reply(update, ctx, usage="Usage: /retarget JOB_ID SECTION\nExample: /retarget 13 Z20")
        if job is None:
            return
        if job.job_type != JOB_TYPE_CHANGE_SECTION or len(ctx.args) < 2:
            await update.effective_message.reply_text("Usage: /retarget JOB_ID SECTION\nExample: /retarget 13 Z20")
            return
        target = normalize_section_name(ctx.args[1])
        self.storage.update_job_target_section(job.id, target)
        await update.effective_message.reply_text(f"Updated job #{job.id} target to {target}.")

    async def jobs(self, update: Update, _ctx: ContextTypes.DEFAULT_TYPE) -> None:
        user = self._registered(update)
        if not user:
            await update.effective_message.reply_text("Register first with /start <code>.")
            return
        jobs = [job for job in self.storage.list_jobs(user_id=user.id) if job.job_type in {JOB_TYPE_WATCH, JOB_TYPE_ADD_CLASS, JOB_TYPE_CHANGE_SECTION}]
        pending_by_job = {item.job_id: item for item in self.storage.list_pending_actions(user_id=user.id)}
        if not jobs:
            await update.effective_message.reply_text(
                "No jobs yet. Choose an option below or use:\n"
                "/watch LCFAITH Z18\n"
                "/addclass LCFAITH:Z18,Z19\n"
                "/change LCFAITH Z18",
                reply_markup=self.main_menu_markup(),
            )
            return
        lines = ["Your jobs:"]
        for job in jobs:
            status = "completed" if job.completed_at else ("paused" if job.paused_at else ("enabled" if job.enabled else "disabled"))
            if job.id in pending_by_job:
                status = f"{status}, pending-confirm:{pending_by_job[job.id].target_section or '-'}"
            runtime = self.storage.get_job_runtime(job.id)
            if runtime and runtime.failure_count > 0:
                status = f"{status}, failures={runtime.failure_count}"
            if job.job_type == JOB_TYPE_ADD_CLASS:
                priorities = ",".join(job.priority_sections) if job.priority_sections else "fallback"
                lines.append(f"#{job.id} add {job.course_code} priorities={priorities} mode={job.mode} {status}")
            elif job.job_type == JOB_TYPE_CHANGE_SECTION:
                lines.append(f"#{job.id} change {job.course_code} target={job.target_section} mode={job.mode} {status}")
            else:
                sections = ",".join(job.section_filters) if job.section_filters else "all"
                lines.append(f"#{job.id} watch {job.course_code} sections={sections} {status}")
        await update.effective_message.reply_text("\n".join(lines), reply_markup=self.main_menu_markup())

    async def recheck(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        user = self._registered(update)
        if not user:
            await update.effective_message.reply_text("Register first with /start <code>.")
            return
        if self.scheduler is None:
            await update.effective_message.reply_text("Recheck is unavailable because the background scheduler is not running.")
            return
        job_ids = None
        if ctx.args:
            if len(ctx.args) > 1 or not ctx.args[0].isdigit():
                await update.effective_message.reply_text("Usage: /recheck [JOB_ID]\nExample: /recheck\nExample: /recheck 12")
                return
            job = self.storage.get_job(int(ctx.args[0]))
            if job is None or job.user_id != user.id or job.job_type not in {JOB_TYPE_WATCH, JOB_TYPE_ADD_CLASS, JOB_TYPE_CHANGE_SECTION}:
                await update.effective_message.reply_text("That job was not found.")
                return
            job_ids = {job.id}
        status = await update.effective_message.reply_text("Rechecking now. I will relogin automatically if the saved session expired.")
        result = await self.scheduler.run_selected(user_id=user.id, job_ids=job_ids)
        message = f"Recheck complete. checked={result.checked_jobs} notifications={result.notifications_sent}"
        if result.errors:
            message += f" errors={len(result.errors)}"
        await status.edit_text(message)

    async def confirm_job(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        user = self._registered(update)
        if not user:
            await update.effective_message.reply_text("Register first with /start <code>.")
            return
        if not ctx.args or not ctx.args[0].isdigit():
            await update.effective_message.reply_text("Usage: /confirm JOB_ID\nExample: /confirm 12")
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
            await update.effective_message.reply_text("Usage: /reject JOB_ID\nExample: /reject 12")
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
            await update.effective_message.reply_text("Usage: /remove JOB_ID\nExample: /remove 12")
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

    @staticmethod
    def _add_job_confirmation(job) -> str:
        target = ", ".join(job.priority_sections) if job.priority_sections else "first safe open section"
        action = "auto-submit" if job.mode == JOB_MODE_AUTO else job.mode
        return (
            f"Saved add-class job #{job.id} for {job.course_code}.\n"
            f"Priority: {target}.\n"
            f"Mode: {job.mode} ({action}).\n"
            "I will never drop or change existing classes to add this class."
        )

    @staticmethod
    def _change_job_confirmation(job) -> str:
        action = "auto-submit" if job.mode == JOB_MODE_AUTO else job.mode
        return (
            f"Saved change-section job #{job.id} for {job.course_code} → {job.target_section}.\n"
            f"Mode: {job.mode} ({action}).\n"
            "This will use ArchersHub change-section only, never drop-add."
        )
