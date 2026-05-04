from __future__ import annotations

import logging
from enum import IntEnum
import secrets
import time

from telegram import Chat, InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import CallbackQueryHandler, CommandHandler, ContextTypes, ConversationHandler, MessageHandler, filters

from ..constants import AutoSwitchSubmitError
from ..scheduler import WatchScheduler
from ..sections import MultipleCoursesFound, normalize_section_name
from ..storage import JOB_MODE_AUTO, JOB_MODE_NOTIFY, JOB_TYPE_ADD_CLASS, JOB_TYPE_CHANGE_SECTION, JOB_TYPE_WATCH, SQLiteStorage, UserRecord
from .messages import delete_message_safely
from .parsing import MODE_VALUES, parse_addclass_specs
from .service import BotArchersHubService, TelegramCaptchaRequired

class ConversationState(IntEnum):
    ASK_REGISTRATION_CODE = 0
    ASK_USERNAME = 1
    ASK_PASSWORD = 2
    ASK_ADD_COURSE = 3
    ASK_ADD_PRIORITIES = 4
    ASK_CHANGE_COURSE = 5
    ASK_CHANGE_SECTION = 6
    ASK_WATCH_COURSE = 7
    ASK_WATCH_SECTIONS = 8
    ASK_SEARCH_QUERY = 9


class TelegramControlPanel:
    def __init__(self, storage: SQLiteStorage, archershub: BotArchersHubService, scheduler: WatchScheduler | None = None) -> None:
        self.storage = storage
        self.archershub = archershub
        self.scheduler = scheduler

    def _registration_handler(self) -> ConversationHandler:
        return ConversationHandler(
            entry_points=[CommandHandler("start", self.start)],
            states={
                ConversationState.ASK_REGISTRATION_CODE: [MessageHandler(filters.TEXT & ~filters.COMMAND, self.received_registration_code)],
            },
            fallbacks=[CommandHandler("cancel", self.cancel)],
            name="register_telegram",
            persistent=False,
        )

    def _connect_handler(self) -> ConversationHandler:
        return ConversationHandler(
            entry_points=[CommandHandler(["connect", "login"], self.connect), CallbackQueryHandler(self.connect, pattern="^connect$")],
            states={
                ConversationState.ASK_USERNAME: [MessageHandler(filters.TEXT & ~filters.COMMAND, self.received_username)],
                ConversationState.ASK_PASSWORD: [MessageHandler(filters.TEXT & ~filters.COMMAND, self.received_password)],
            },
            fallbacks=[CommandHandler("cancel", self.cancel)],
            name="connect_archershub",
            persistent=False,
        )

    def _add_class_handler(self) -> ConversationHandler:
        return ConversationHandler(
            entry_points=[CallbackQueryHandler(self.begin_add_wizard, pattern="^menu:add$")],
            states={
                ConversationState.ASK_ADD_COURSE: [MessageHandler(filters.TEXT & ~filters.COMMAND, self.received_add_course)],
                ConversationState.ASK_ADD_PRIORITIES: [MessageHandler(filters.TEXT & ~filters.COMMAND, self.received_add_priorities)],
            },
            fallbacks=[CommandHandler("cancel", self.cancel)],
            name="add_class_wizard",
            persistent=False,
        )

    def _change_section_handler(self) -> ConversationHandler:
        return ConversationHandler(
            entry_points=[CallbackQueryHandler(self.begin_change_wizard, pattern="^menu:change$")],
            states={
                ConversationState.ASK_CHANGE_COURSE: [MessageHandler(filters.TEXT & ~filters.COMMAND, self.received_change_course)],
                ConversationState.ASK_CHANGE_SECTION: [MessageHandler(filters.TEXT & ~filters.COMMAND, self.received_change_section)],
            },
            fallbacks=[CommandHandler("cancel", self.cancel)],
            name="change_section_wizard",
            persistent=False,
        )

    def _watch_handler(self) -> ConversationHandler:
        return ConversationHandler(
            entry_points=[CallbackQueryHandler(self.begin_watch_wizard, pattern="^menu:watch$")],
            states={
                ConversationState.ASK_WATCH_COURSE: [MessageHandler(filters.TEXT & ~filters.COMMAND, self.received_watch_course)],
                ConversationState.ASK_WATCH_SECTIONS: [MessageHandler(filters.TEXT & ~filters.COMMAND, self.received_watch_sections)],
            },
            fallbacks=[CommandHandler("cancel", self.cancel)],
            name="watch_only_wizard",
            persistent=False,
        )

    def _search_handler(self) -> ConversationHandler:
        return ConversationHandler(
            entry_points=[CommandHandler("search", self.search), CallbackQueryHandler(self.begin_search_wizard, pattern="^menu:search$")],
            states={
                ConversationState.ASK_SEARCH_QUERY: [MessageHandler(filters.TEXT & ~filters.COMMAND, self.received_search_query)],
            },
            fallbacks=[CommandHandler("cancel", self.cancel)],
            name="course_search",
            persistent=False,
        )

    def _command_handlers(self) -> list:
        return [
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
            CallbackQueryHandler(self.course_search_callback, pattern="^cs:"),
            CallbackQueryHandler(self.menu_callback, pattern="^menu:(jobs|help)$"),
            MessageHandler(filters.COMMAND, self.unknown_command),
        ]

    def build_handlers(self) -> list:
        return [
            self._registration_handler(),
            self._connect_handler(),
            self._add_class_handler(),
            self._change_section_handler(),
            self._watch_handler(),
            self._search_handler(),
            *self._command_handlers(),
        ]

    @staticmethod
    def main_menu_markup() -> InlineKeyboardMarkup:
        return InlineKeyboardMarkup(
            [
                [InlineKeyboardButton("🔎 Search courses", callback_data="menu:search")],
                [InlineKeyboardButton("➕ Add a class", callback_data="menu:add")],
                [InlineKeyboardButton("🔁 Change section", callback_data="menu:change")],
                [InlineKeyboardButton("👀 Watch only", callback_data="menu:watch")],
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
            "This uses ArchersHub's change-section function only, never drop-add.\n\n"
            "👀 Watch only: get notified when sections open without submitting anything.\n\n"
            "🔎 Course Search: search Course Finder, reveal teachers when available, and create jobs from results."
        )

    @staticmethod
    def connect_markup() -> InlineKeyboardMarkup:
        return InlineKeyboardMarkup([[InlineKeyboardButton("Connect ArchersHub", callback_data="connect")]])

    async def start(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
        chat = update.effective_chat
        if chat is None or update.effective_user is None:
            return ConversationHandler.END
        existing = self.storage.get_user_by_telegram_id(chat.id)
        if existing:
            await self._reply_registered_home(update, existing)
            return ConversationHandler.END
        if not ctx.args:
            await update.effective_message.reply_text(
                "Welcome! Please send your one-time registration code.\n\n"
                "Ask the service admin for a code if you do not have one yet."
            )
            return ConversationState.ASK_REGISTRATION_CODE
        await self._redeem_registration_code(update, ctx.args[0])
        return ConversationHandler.END

    async def received_registration_code(self, update: Update, _ctx: ContextTypes.DEFAULT_TYPE) -> int:
        registered = await self._redeem_registration_code(update, update.effective_message.text.strip())
        return ConversationHandler.END if registered else ConversationState.ASK_REGISTRATION_CODE

    async def _redeem_registration_code(self, update: Update, code: str) -> bool:
        chat = update.effective_chat
        if chat is None or update.effective_user is None:
            return False
        try:
            user = self.storage.redeem_registration_code(code, chat.id, update.effective_user.username)
        except ValueError as exc:
            await update.effective_message.reply_text(f"Registration failed: {exc}\n\nSend another code, or use /cancel to stop.")
            return False
        await update.effective_message.reply_text(
            "Registration complete. Next, connect your ArchersHub account so I can check sections for you.",
            reply_markup=self.connect_markup(),
        )
        logging.info("registered telegram_id=%s as user_id=%s", chat.id, user.id)
        return True

    async def help(self, update: Update, _ctx: ContextTypes.DEFAULT_TYPE) -> None:
        await update.effective_message.reply_text(self.help_text(), reply_markup=self.main_menu_markup())

    @staticmethod
    def help_text() -> str:
        return (
            "ArchersHub Bot Help\n\n"
            "Create jobs:\n"
            "• /watch LCFAITH Z18 Z19 — notify when matching sections get slots.\n"
            "• /search LCFAITH — search Course Finder and choose sections/actions.\n"
            "• /addclass LCFAITH:Z18,Z19 — add a class you do not have yet. Priorities are optional.\n"
            "• /addclass LCFAITH:Z18,Z19 GETEAMS:S11 confirm — add multiple classes and submit pending adds as one add/drop batch.\n"
            "• /change LCFAITH Z18 — change an existing class to section Z18.\n\n"
            "Difference:\n"
            "• Add class never drops/changes your current classes to solve conflicts.\n"
            "• Change section only uses ArchersHub's change-section feature, never drop-add.\n\n"
            "Manage jobs:\n"
            "• /jobs — list saved jobs.\n"
            "• /login — connect or update your saved ArchersHub login.\n"
            "• /recheck — force-check all active jobs now.\n"
            "• /recheck 12 — force-check only job #12 now.\n"
            "• /remove 12 — disable job #12.\n"
            "• /setmode 12 confirm — change mode to notify, confirm, or auto.\n"
            "• /setpriorities 12 Z18 Z19 — edit add-class priority sections.\n"
            "• /retarget 13 Z20 — edit a change-section target.\n\n"
            "Confirm mode:\n"
            "• /confirm 12 — recheck and submit a pending request. Pending add-class requests are batched together.\n"
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
            await self._reply_access_required(update)
            return ConversationHandler.END
        ctx.user_data.clear()
        await update.effective_message.reply_text(
            "Send your ArchersHub username/email. Use /cancel to stop. This will replace any saved ArchersHub login.\n\n"
            "I will store your login encrypted and use it only to check or submit your jobs."
        )
        return ConversationState.ASK_USERNAME

    async def received_username(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
        ctx.user_data["archershub_username"] = update.effective_message.text.strip()
        await delete_message_safely(update.effective_message)
        await update.effective_chat.send_message("Now send your ArchersHub password. I will try to delete the message after processing.")
        return ConversationState.ASK_PASSWORD

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
        user = self._registered(update)
        if not user:
            await self._reply_access_required(update)
            return ConversationHandler.END
        if not self._has_credentials(user.id):
            await update.effective_message.reply_text("Connect your ArchersHub account first.", reply_markup=self.connect_markup())
            return ConversationHandler.END
        ctx.user_data.clear()
        await update.effective_message.reply_text(
            "Add a class\n\n"
            "Use this for a course you are not enlisted in yet. I will not drop or change existing classes.\n\n"
            "Send the course code, e.g. LCFAITH."
        )
        return ConversationState.ASK_ADD_COURSE

    async def received_add_course(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
        ctx.user_data["add_course_code"] = update.effective_message.text.strip().upper()
        await update.effective_message.reply_text(
            "Optional: send priority sections separated by spaces or commas, e.g. Z18 Z19.\n"
            "Send '-' to skip priorities and use the first safe open section."
        )
        return ConversationState.ASK_ADD_PRIORITIES

    async def received_add_priorities(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
        user = self._registered(update)
        if not user:
            await self._reply_access_required(update)
            return ConversationHandler.END
        text = update.effective_message.text.strip()
        priorities = [] if text in {"-", "skip", "SKIP"} else [normalize_section_name(part) for part in text.replace(",", " ").split()]
        target = await self._resolve_course_or_reply(
            update,
            user.id,
            ctx.user_data.get("add_course_code", ""),
            intent={"job_type": JOB_TYPE_ADD_CLASS, "mode": JOB_MODE_AUTO, "priority_sections": priorities}
        )
        if target is None:
            return ConversationHandler.END
        job = self.storage.add_job(
            user_id=user.id,
            job_type=JOB_TYPE_ADD_CLASS,
            mode=JOB_MODE_AUTO,
            course_code=target["course_code"],
            course_creation_id=target["course_creation_id"],
            priority_sections=priorities,
        )
        ctx.user_data.clear()
        await update.effective_message.reply_text(self._add_job_confirmation(job), reply_markup=self.main_menu_markup())
        return ConversationHandler.END

    async def begin_change_wizard(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
        await update.callback_query.answer()
        user = self._registered(update)
        if not user:
            await self._reply_access_required(update)
            return ConversationHandler.END
        if not self._has_credentials(user.id):
            await update.effective_message.reply_text("Connect your ArchersHub account first.", reply_markup=self.connect_markup())
            return ConversationHandler.END
        ctx.user_data.clear()
        await update.effective_message.reply_text(
            "Change section\n\n"
            "Use this when you already have the class and want a different section. "
            "This uses ArchersHub's change-section function only; it never drop-adds.\n\n"
            "Send the course code, e.g. LCFAITH."
        )
        return ConversationState.ASK_CHANGE_COURSE

    async def received_change_course(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
        ctx.user_data["change_course_code"] = update.effective_message.text.strip().upper()
        await update.effective_message.reply_text("Send the target section, e.g. Z18.")
        return ConversationState.ASK_CHANGE_SECTION

    async def received_change_section(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
        user = self._registered(update)
        if not user:
            await self._reply_access_required(update)
            return ConversationHandler.END
        target_section = normalize_section_name(update.effective_message.text.strip())
        target_course = await self._resolve_course_or_reply(
            update,
            user.id,
            ctx.user_data.get("change_course_code", ""),
            intent={"job_type": JOB_TYPE_CHANGE_SECTION, "mode": JOB_MODE_AUTO, "target_section": target_section}
        )
        if target_course is None:
            return ConversationHandler.END
        job = self.storage.add_job(
            user_id=user.id,
            job_type=JOB_TYPE_CHANGE_SECTION,
            mode=JOB_MODE_AUTO,
            course_code=target_course["course_code"],
            course_creation_id=target_course["course_creation_id"],
            target_section=target_section,
        )
        ctx.user_data.clear()
        await update.effective_message.reply_text(self._change_job_confirmation(job), reply_markup=self.main_menu_markup())
        return ConversationHandler.END

    async def change_section_job(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        user = self._registered(update)
        if not user:
            await self._reply_access_required(update)
            return
        if not self._has_credentials(user.id):
            await update.effective_message.reply_text("Connect your ArchersHub account first.", reply_markup=self.connect_markup())
            return
        if len(ctx.args) < 2:
            await update.effective_message.reply_text(
                "Usage: /change COURSE TARGET_SECTION [notify|confirm|auto]\n"
                "Example: /change LCFAITH Z18\n\n"
                "Use this only for a class you already have. It uses ArchersHub change-section, never drop-add."
            )
            return
        mode = self._mode_from_args(ctx.args[2:])
        target_section = normalize_section_name(ctx.args[1])
        target_course = await self._resolve_course_or_reply(
            update,
            user.id,
            ctx.args[0],
            intent={"job_type": JOB_TYPE_CHANGE_SECTION, "mode": mode, "target_section": target_section}
        )
        if target_course is None:
            return
        job = self.storage.add_job(
            user_id=user.id,
            job_type=JOB_TYPE_CHANGE_SECTION,
            mode=mode,
            course_code=target_course["course_code"],
            course_creation_id=target_course["course_creation_id"],
            target_section=normalize_section_name(ctx.args[1]),
        )
        await update.effective_message.reply_text(self._change_job_confirmation(job))

    async def add_class_job(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        user = self._registered(update)
        if not user:
            await self._reply_access_required(update)
            return
        if not self._has_credentials(user.id):
            await update.effective_message.reply_text("Connect your ArchersHub account first.", reply_markup=self.connect_markup())
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
        jobs = []
        for course_code, priorities in specs:
            target_course = await self._resolve_course_or_reply(
                update,
                user.id,
                course_code,
                intent={"job_type": JOB_TYPE_ADD_CLASS, "mode": mode, "priority_sections": priorities}
            )
            if target_course is None:
                return
            jobs.append(
                self.storage.add_job(
                user_id=user.id,
                job_type=JOB_TYPE_ADD_CLASS,
                mode=mode,
                course_code=target_course["course_code"],
                course_creation_id=target_course["course_creation_id"],
                priority_sections=priorities,
            )
            )
        await update.effective_message.reply_text("\n\n".join(self._add_job_confirmation(job) for job in jobs))

    async def watch(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        user = self._registered(update)
        if not user:
            await self._reply_access_required(update)
            return
        if not self._has_credentials(user.id):
            await update.effective_message.reply_text("Connect your ArchersHub account first.", reply_markup=self.connect_markup())
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
        target_course = await self._resolve_course_or_reply(
            update,
            user.id,
            ctx.args[0],
            intent={"job_type": JOB_TYPE_WATCH, "mode": JOB_MODE_NOTIFY, "section_filters": sections}
        )
        if target_course is None:
            return
        job = self.storage.add_job(
            user_id=user.id,
            job_type=JOB_TYPE_WATCH,
            mode=JOB_MODE_NOTIFY,
            course_code=target_course["course_code"],
            course_creation_id=target_course["course_creation_id"],
            section_filters=sections,
        )
        target = "all sections" if not sections else ", ".join(sections)
        await update.effective_message.reply_text(
            f"Saved watch job #{job.id} for {job.course_code}: {target}.\n"
            "I will only notify when matching sections gain available slots.",
            reply_markup=self.main_menu_markup(),
        )

    async def begin_watch_wizard(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
        await update.callback_query.answer()
        user = self._registered(update)
        if not user:
            await self._reply_access_required(update)
            return ConversationHandler.END
        if not self._has_credentials(user.id):
            await update.effective_message.reply_text("Connect your ArchersHub account first.", reply_markup=self.connect_markup())
            return ConversationHandler.END
        ctx.user_data.clear()
        await update.effective_message.reply_text(
            "Watch only\n\n"
            "I will notify you when matching sections get slots. I will not submit anything.\n\n"
            "Send the course code, e.g. LCFAITH."
        )
        return ConversationState.ASK_WATCH_COURSE

    async def received_watch_course(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
        ctx.user_data["watch_course_code"] = update.effective_message.text.strip().upper()
        await update.effective_message.reply_text(
            "Optional: send sections to watch separated by spaces or commas, e.g. Z18 Z19.\n"
            "Send '-' to watch all sections."
        )
        return ConversationState.ASK_WATCH_SECTIONS

    async def received_watch_sections(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
        user = self._registered(update)
        if not user:
            await self._reply_access_required(update)
            return ConversationHandler.END
        text = update.effective_message.text.strip()
        sections = [] if text in {"-", "skip", "SKIP"} else [normalize_section_name(part) for part in text.replace(",", " ").split()]
        target_course = await self._resolve_course_or_reply(
            update,
            user.id,
            ctx.user_data.get("watch_course_code", ""),
            intent={"job_type": JOB_TYPE_WATCH, "mode": JOB_MODE_NOTIFY, "section_filters": sections}
        )
        if target_course is None:
            return ConversationHandler.END
        job = self.storage.add_job(
            user_id=user.id,
            job_type=JOB_TYPE_WATCH,
            mode=JOB_MODE_NOTIFY,
            course_code=target_course["course_code"],
            course_creation_id=target_course["course_creation_id"],
            section_filters=sections,
        )
        ctx.user_data.clear()
        target = "all sections" if not sections else ", ".join(sections)
        await update.effective_message.reply_text(
            f"Saved watch job #{job.id} for {job.course_code}: {target}.\n"
            "I will only notify when matching sections gain available slots.",
            reply_markup=self.main_menu_markup(),
        )
        return ConversationHandler.END

    async def begin_search_wizard(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
        if update.callback_query:
            await update.callback_query.answer()
        user = self._registered(update)
        if not user:
            await self._reply_access_required(update)
            return ConversationHandler.END
        if not self._has_credentials(user.id):
            await update.effective_message.reply_text("Connect your ArchersHub account first.", reply_markup=self.connect_markup())
            return ConversationHandler.END
        ctx.user_data.clear()
        await update.effective_message.reply_text("Course Search\n\nSend a subject code or keyword, e.g. LCFAITH or accounting.")
        return ConversationState.ASK_SEARCH_QUERY

    async def search(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
        user = self._registered(update)
        if not user:
            await self._reply_access_required(update)
            return ConversationHandler.END
        if not self._has_credentials(user.id):
            await update.effective_message.reply_text("Connect your ArchersHub account first.", reply_markup=self.connect_markup())
            return ConversationHandler.END
        if not ctx.args:
            await update.effective_message.reply_text("Course Search\n\nSend a subject code or keyword, e.g. LCFAITH or accounting.")
            return ConversationState.ASK_SEARCH_QUERY
        await self._run_course_search(update, user, " ".join(ctx.args))
        return ConversationHandler.END

    async def received_search_query(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
        user = self._registered(update)
        if not user:
            await self._reply_access_required(update)
            return ConversationHandler.END
        await self._run_course_search(update, user, update.effective_message.text.strip())
        ctx.user_data.clear()
        return ConversationHandler.END

    async def _run_course_search(self, update: Update, user, query: str) -> None:
        query = query.strip()
        if not query:
            await update.effective_message.reply_text("Send a subject code or keyword, e.g. LCFAITH or accounting.")
            return
        status = await update.effective_message.reply_text(f"Searching Course Finder for {query!r}...")
        try:
            courses = await self.archershub.search_courses_for_user(user.id, query)
        except TelegramCaptchaRequired as exc:
            await status.edit_text(str(exc))
            return
        except Exception as exc:
            await status.edit_text(f"Course search failed: {exc}")
            return
        if not courses:
            await status.edit_text(f"No Course Finder matches for {query!r}.")
            return
        token = secrets.token_hex(4)
        self._save_search_state(user.id, {"token": token, "created_at": time.time(), "query": query, "courses": courses, "sections": {}, "intent": {}})
        await status.edit_text("Search complete.")
        text, markup = self._course_results_message(token, {"query": query, "courses": courses}, 0)
        await update.effective_message.reply_text(text, reply_markup=markup)

    async def course_search_callback(self, update: Update, _ctx: ContextTypes.DEFAULT_TYPE) -> None:
        if update.callback_query:
            await update.callback_query.answer()
        user = self._registered(update)
        if not user:
            await self._reply_access_required(update)
            return
        data = update.callback_query.data if update.callback_query else ""
        parts = data.split(":")
        if len(parts) < 3:
            await update.effective_message.reply_text("That search action was invalid. Use /search to start again.")
            return
        state = self._load_search_state(user.id, parts[2])
        if state is None:
            await update.effective_message.reply_text("That search expired. Use /search to start again.")
            return
        kind = parts[1]
        if kind == "r" and len(parts) >= 4:
            text, markup = self._course_results_message(parts[2], state, int(parts[3]))
            await update.effective_message.reply_text(text, reply_markup=markup)
            return
        if kind == "c" and len(parts) >= 4:
            await self._show_search_course_sections(update, user, state, parts[2], int(parts[3]), 0)
            return
        if kind == "sp" and len(parts) >= 5:
            await self._show_search_course_sections(update, user, state, parts[2], int(parts[3]), int(parts[4]))
            return
        if kind == "s" and len(parts) >= 5:
            text, markup = self._section_actions_message(parts[2], state, int(parts[3]), int(parts[4]))
            await update.effective_message.reply_text(text, reply_markup=markup)
            return
        if kind == "addall" and len(parts) >= 4:
            await self._create_search_job(update, user, state, int(parts[3]), None, "addall")
            return
        if kind == "a" and len(parts) >= 6:
            await self._create_search_job(update, user, state, int(parts[4]), int(parts[5]), parts[3])
            return
        await update.effective_message.reply_text("That search action was invalid. Use /search to start again.")

    async def _show_search_course_sections(self, update: Update, user, state: dict, token: str, course_index: int, page: int) -> None:
        courses = state.get("courses") or []
        if course_index < 0 or course_index >= len(courses):
            await update.effective_message.reply_text("That course result was not found. Use /search to start again.")
            return
        section_cache = state.setdefault("sections", {})
        cache_key = str(course_index)
        if cache_key not in section_cache:
            status = await update.effective_message.reply_text("Loading sections and revealing teachers when available...")
            try:
                section_cache[cache_key] = await self.archershub.fetch_search_course_sections(user.id, courses[course_index])
                self._save_search_state(user.id, state)
            except TelegramCaptchaRequired as exc:
                await status.edit_text(str(exc))
                return
            except Exception as exc:
                await status.edit_text(f"Could not load sections: {exc}")
                return
            await status.edit_text("Sections loaded.")
        text, markup = self._section_results_message(token, state, course_index, page)
        await update.effective_message.reply_text(text, reply_markup=markup)

    async def _create_search_job(self, update: Update, user, state: dict, course_index: int, section_index: int | None, action: str) -> None:
        courses = state.get("courses") or []
        if course_index < 0 or course_index >= len(courses):
            await update.effective_message.reply_text("That course result was not found. Use /search to start again.")
            return
        course = courses[course_index]
        course_code = str(course.get("course_code") or "").upper()
        course_creation_id = str(course.get("course_creation_id") or "")
        intent = state.get("intent")
        section = None
        if section_index is not None:
            section = self._section_at(state, course_index, section_index)
            if section is None:
                await update.effective_message.reply_text("That section result was not found. Open the course from search again.")
                return
        if action == "watch":
            job = self.storage.add_job(user_id=user.id, job_type=JOB_TYPE_WATCH, mode=JOB_MODE_NOTIFY, course_code=course_code, course_creation_id=course_creation_id, section_filters=[section["section_name"]])
            await update.effective_message.reply_text(f"Saved watch job #{job.id} for {course_code} {section['section_name']}.", reply_markup=self.main_menu_markup())
        elif action == "add":
            job = self.storage.add_job(user_id=user.id, job_type=JOB_TYPE_ADD_CLASS, mode=JOB_MODE_AUTO, course_code=course_code, course_creation_id=course_creation_id, priority_sections=[section["section_name"]])
            await update.effective_message.reply_text(self._add_job_confirmation(job), reply_markup=self.main_menu_markup())
        elif action == "change":
            job = self.storage.add_job(user_id=user.id, job_type=JOB_TYPE_CHANGE_SECTION, mode=JOB_MODE_AUTO, course_code=course_code, course_creation_id=course_creation_id, target_section=section["section_name"])
            await update.effective_message.reply_text(self._change_job_confirmation(job), reply_markup=self.main_menu_markup())
        elif action == "addall":
            job_type = intent.get("job_type") if intent else JOB_TYPE_ADD_CLASS
            mode = intent.get("mode") if intent else JOB_MODE_AUTO
            # If we had specific filters in intent, maybe we should respect them?
            # But "addall" in UI usually means "everything".
            # However, if it was /watch COURSE Z18 and they click "Watch entire subject", 
            # we should probably watch Z18 if intent has it.
            # But let's stick to the simplest fix: just respect job_type and mode.
            
            section_filters = intent.get("section_filters") if intent and job_type == JOB_TYPE_WATCH else []
            priority_sections = intent.get("priority_sections") if intent and job_type == JOB_TYPE_ADD_CLASS else []
            target_section = intent.get("target_section") if intent and job_type == JOB_TYPE_CHANGE_SECTION else None

            job = self.storage.add_job(
                user_id=user.id,
                job_type=job_type,
                mode=mode,
                course_code=course_code,
                course_creation_id=course_creation_id,
                section_filters=section_filters,
                priority_sections=priority_sections,
                target_section=target_section,
            )
            if job_type == JOB_TYPE_WATCH:
                target = "all sections" if not section_filters else ", ".join(section_filters)
                await update.effective_message.reply_text(
                    f"Saved watch job #{job.id} for {job.course_code}: {target}.\n"
                    "I will only notify when matching sections gain available slots.",
                    reply_markup=self.main_menu_markup(),
                )
            elif job_type == JOB_TYPE_CHANGE_SECTION:
                await update.effective_message.reply_text(self._change_job_confirmation(job), reply_markup=self.main_menu_markup())
            else:
                await update.effective_message.reply_text(self._add_job_confirmation(job), reply_markup=self.main_menu_markup())
        else:
            await update.effective_message.reply_text("That search action was invalid. Use /search to start again.")

    @staticmethod
    def _course_results_message(token: str, state: dict, page: int) -> tuple[str, InlineKeyboardMarkup]:
        courses = state.get("courses") or []
        page_size = 5
        max_page = max(0, (len(courses) - 1) // page_size)
        page = max(0, min(page, max_page))
        start = page * page_size
        shown = courses[start:start + page_size]
        lines = [f"Course Search: {state.get('query', '')}", f"Results {start + 1}-{start + len(shown)} of {len(courses)}"]
        rows = []
        for offset, course in enumerate(shown):
            idx = start + offset
            label = f"{course.get('course_code')} — {course.get('course_name')}"
            lines.append(f"{idx + 1}. {label}")
            rows.append([InlineKeyboardButton(label[:60], callback_data=f"cs:c:{token}:{idx}")])
        nav = []
        if page > 0:
            nav.append(InlineKeyboardButton("<<", callback_data=f"cs:r:{token}:{page - 1}"))
        if page < max_page:
            nav.append(InlineKeyboardButton(">>", callback_data=f"cs:r:{token}:{page + 1}"))
        if nav:
            rows.append(nav)
        return "\n".join(lines), InlineKeyboardMarkup(rows)

    @staticmethod
    def _section_results_message(token: str, state: dict, course_index: int, page: int) -> tuple[str, InlineKeyboardMarkup]:
        courses = state.get("courses") or []
        course = courses[course_index]
        sections = ((state.get("sections") or {}).get(str(course_index))) or []
        intent = state.get("intent")
        all_label = "Add entire subject"
        if intent and intent.get("job_type") == JOB_TYPE_WATCH:
            all_label = "Watch entire subject"
        elif intent and intent.get("job_type") == JOB_TYPE_CHANGE_SECTION:
            all_label = "Change entire subject"

        page_size = 5
        if not sections:
            return f"No sections found for {course.get('course_code')}.", InlineKeyboardMarkup([[InlineKeyboardButton(all_label, callback_data=f"cs:addall:{token}:{course_index}")]])
        max_page = max(0, (len(sections) - 1) // page_size)
        page = max(0, min(page, max_page))
        start = page * page_size
        shown = sections[start:start + page_size]
        lines = [f"{course.get('course_code')} sections", f"{course.get('course_name')}", f"Results {start + 1}-{start + len(shown)} of {len(sections)}"]
        rows = []
        for offset, section in enumerate(shown):
            idx = start + offset
            status = "OPEN" if float(section.get("available") or 0) > 0 else "FULL"
            lines.append(
                f"{idx + 1}. {section.get('section_name')}: {status} "
                f"({float(section.get('available') or 0):g} slots, {float(section.get('enlisted') or 0):g}/{float(section.get('capacity') or 0):g})\n"
                f"Teacher: {section.get('teacher') or '-'}\n"
                f"Schedule: {section.get('schedule') or '-'}"
            )
            rows.append([InlineKeyboardButton(f"{section.get('section_name')} actions", callback_data=f"cs:s:{token}:{course_index}:{idx}")])
        rows.append([InlineKeyboardButton(all_label, callback_data=f"cs:addall:{token}:{course_index}")])
        nav = []
        if page > 0:
            nav.append(InlineKeyboardButton("<<", callback_data=f"cs:sp:{token}:{course_index}:{page - 1}"))
        if page < max_page:
            nav.append(InlineKeyboardButton(">>", callback_data=f"cs:sp:{token}:{course_index}:{page + 1}"))
        if nav:
            rows.append(nav)
        return "\n\n".join(lines), InlineKeyboardMarkup(rows)

    def _section_actions_message(self, token: str, state: dict, course_index: int, section_index: int) -> tuple[str, InlineKeyboardMarkup]:
        courses = state.get("courses") or []
        course = courses[course_index]
        section = self._section_at(state, course_index, section_index)
        if section is None:
            return "That section result was not found. Open the course from search again.", InlineKeyboardMarkup([])
        text = (
            f"{course.get('course_code')} {section.get('section_name')}\n"
            f"Teacher: {section.get('teacher') or '-'}\n"
            f"Schedule: {section.get('schedule') or '-'}\n"
            f"Available: {float(section.get('available') or 0):g} slots"
        )
        return text, InlineKeyboardMarkup(
            [
                [InlineKeyboardButton("👀 Watch section", callback_data=f"cs:a:{token}:watch:{course_index}:{section_index}")],
                [InlineKeyboardButton("➕ Add with priority", callback_data=f"cs:a:{token}:add:{course_index}:{section_index}")],
                [InlineKeyboardButton("🔁 Change to section", callback_data=f"cs:a:{token}:change:{course_index}:{section_index}")],
            ]
        )

    def _section_at(self, state: dict, course_index: int, section_index: int) -> dict | None:
        sections = ((state.get("sections") or {}).get(str(course_index))) or []
        if section_index < 0 or section_index >= len(sections):
            return None
        return sections[section_index]

    def _search_snapshot_key(self, user_id: int) -> str:
        return f"telegram:course-search:{user_id}"

    def _save_search_state(self, user_id: int, state: dict) -> None:
        self.storage.set_snapshot(self._search_snapshot_key(user_id), state)

    def _load_search_state(self, user_id: int, token: str) -> dict | None:
        state = self.storage.get_snapshot(self._search_snapshot_key(user_id))
        if not isinstance(state, dict) or state.get("token") != token:
            return None
        if time.time() - float(state.get("created_at") or 0) > 1800:
            self.storage.delete_snapshot(self._search_snapshot_key(user_id))
            return None
        return state

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
        active = await self._get_active_user(update)
        if active is None:
            return
        user, _chat = active
        jobs = [
            job
            for job in self.storage.list_jobs(user_id=user.id)
            if job.enabled and job.job_type in {JOB_TYPE_WATCH, JOB_TYPE_ADD_CLASS, JOB_TYPE_CHANGE_SECTION}
        ]
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
        active = await self._get_active_user(update)
        if active is None:
            return
        user, _chat = active
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
            await self._reply_access_required(update)
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
        batch_jobs = [job]
        try:
            if job.job_type == JOB_TYPE_ADD_CLASS and pending.action_type == "add_class":
                batch_jobs = []
                for candidate in self.storage.list_jobs(user_id=user.id):
                    if (
                        candidate.job_type != JOB_TYPE_ADD_CLASS
                        or not candidate.enabled
                        or candidate.completed_at is not None
                        or candidate.paused_at is not None
                    ):
                        continue
                    candidate_pending = self.storage.get_pending_action(candidate.id)
                    if candidate_pending and candidate_pending.action_type == "add_class":
                        batch_jobs.append(candidate)
                result = await self.archershub.execute_automation_batch(batch_jobs)
                submitted = set(result.submitted_job_ids)
                for batch_job in batch_jobs:
                    if batch_job.id in submitted:
                        self.storage.complete_job(batch_job.id)
                message = result.message
            else:
                message = await self.archershub.execute_automation_job(job)
                self.storage.complete_job(job_id)
        except AutoSwitchSubmitError as exc:
            for batch_job in batch_jobs:
                self.storage.complete_job(batch_job.id)
            await status.edit_text(
                "Submit was attempted, but the result was unclear. "
                "I stopped the job to avoid a duplicate add/drop or change-section submission.\n\n"
                f"{exc}"
            )
            return
        except Exception as exc:
            if job.job_type == JOB_TYPE_ADD_CLASS:
                for pending_job in batch_jobs:
                    self.storage.clear_pending_action(pending_job.id)
            else:
                self.storage.clear_pending_action(job_id)
            await status.edit_text(f"Confirmation failed: {exc}")
            return
        await status.edit_text(message)

    async def reject_job(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        user = self._registered(update)
        if not user:
            await self._reply_access_required(update)
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
        active = await self._get_active_user(update)
        if active is None:
            return
        if not ctx.args or not ctx.args[0].isdigit():
            await update.effective_message.reply_text("Usage: /remove JOB_ID\nExample: /remove 12")
            return
        self.storage.disable_job(int(ctx.args[0]))
        await update.effective_message.reply_text(f"Removed job #{ctx.args[0]} from your active list.")

    def _registered(self, update: Update):
        chat = update.effective_chat
        user = self.storage.get_user_by_telegram_id(chat.id) if chat else None
        return user if user and user.is_active else None

    async def _get_active_user(self, update: Update) -> tuple[UserRecord, Chat] | None:
        """Return (user, chat) for active registered users, or reject the update."""
        chat = update.effective_chat
        if chat is None:
            return None
        user = self.storage.get_user_by_telegram_id(chat.id)
        if user and user.is_active:
            return user, chat
        await self._reply_access_required(update)
        return None

    def _inactive_user(self, update: Update):
        chat = update.effective_chat
        user = self.storage.get_user_by_telegram_id(chat.id) if chat else None
        return user if user and not user.is_active else None

    def _has_credentials(self, user_id: int) -> bool:
        return self.storage.get_credentials(user_id) is not None

    async def _reply_registered_home(self, update: Update, user) -> None:
        if not user.is_active:
            await update.effective_message.reply_text("Your access has been revoked. Ask the service admin if you need access again.")
            return
        if self._has_credentials(user.id):
            await update.effective_message.reply_text(self.onboarding_text(), reply_markup=self.main_menu_markup())
            return
        await update.effective_message.reply_text(
            "You are registered. Next, connect your ArchersHub account so I can check sections for you.",
            reply_markup=self.connect_markup(),
        )

    async def _reply_access_required(self, update: Update) -> None:
        if self._inactive_user(update):
            await update.effective_message.reply_text("Your access has been revoked. Ask the service admin if you need access again.")
            return
        await update.effective_message.reply_text("Register first with /start.")

    async def unknown_command(self, update: Update, _ctx: ContextTypes.DEFAULT_TYPE) -> None:
        user = self._registered(update)
        if user and self._has_credentials(user.id):
            await update.effective_message.reply_text(
                "command not recognized. Use /help or choose an option below.",
                reply_markup=self.main_menu_markup(),
            )
        elif user:
            await update.effective_message.reply_text(
                "command not recognized. Connect your ArchersHub account first.",
                reply_markup=self.connect_markup(),
            )
        elif self._inactive_user(update):
            await update.effective_message.reply_text("Your access has been revoked. Ask the service admin if you need access again.")
        else:
            await update.effective_message.reply_text("command not recognized. Use /start to register.")

    async def _owned_job_or_reply(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE, *, usage: str):
        user = self._registered(update)
        if not user:
            await self._reply_access_required(update)
            return None
        if not ctx.args or not ctx.args[0].isdigit():
            await update.effective_message.reply_text(usage)
            return None
        job = self.storage.get_job(int(ctx.args[0]))
        if job is None or job.user_id != user.id:
            await update.effective_message.reply_text("That job was not found.")
            return None
        return job

    async def _resolve_course_or_reply(self, update: Update, user_id: int, course_code: str, intent: dict | None = None) -> dict[str, str] | None:
        course_code = (course_code or "").strip().upper()
        if not course_code:
            await update.effective_message.reply_text("Course code is required. Use 🔎 Search courses if you are unsure.")
            return None
        try:
            return await self.archershub.resolve_course_for_user(user_id, course_code)
        except TelegramCaptchaRequired as exc:
            await update.effective_message.reply_text(str(exc))
            return None
        except MultipleCoursesFound as exc:
            courses = self.archershub.format_course_matches(exc.matches, exc.campus_id, exc.academic_session_id)
            token = secrets.token_hex(4)
            self._save_search_state(user_id, {"token": token, "created_at": time.time(), "query": course_code, "courses": courses, "sections": {}, "intent": intent})
            _, markup = self._course_results_message(token, {"query": course_code, "courses": courses}, 0)
            await update.effective_message.reply_text(
                f"{course_code} has multiple Course Finder matches. Please choose the exact offering below:",
                reply_markup=markup
            )
            return None
        except Exception as exc:
            message = str(exc)
            if "was not found" in message:
                await update.effective_message.reply_text(f"{course_code} was not found in Course Finder.")
            else:
                await update.effective_message.reply_text(f"Could not resolve {course_code}: {exc}")
            return None

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
