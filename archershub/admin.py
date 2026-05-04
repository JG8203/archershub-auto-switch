from __future__ import annotations

import argparse
import asyncio
import os

from .bot.service import BotArchersHubService
from .crypto import SecretBox
from .env import load_project_env
from .scheduler import WatchScheduler
from .storage import SQLiteStorage, text_to_dt, utcnow


def storage_from_args(args) -> SQLiteStorage:
    return SQLiteStorage(args.db or os.getenv("ARCHERSHUB_DB", "archershub_bot.sqlite3"))


def main() -> None:
    load_project_env()
    parser = argparse.ArgumentParser(description="Admin CLI for ArchersHub Telegram service")
    parser.add_argument("--db", help="SQLite database path")
    sub = parser.add_subparsers(dest="command", required=True)

    gen = sub.add_parser("generate-code", help="Generate a one-time registration code")
    gen.add_argument("--ttl-hours", type=int, default=24)

    sub.add_parser("list-codes")
    revoke = sub.add_parser("revoke-code", help="Revoke a registration code and deactivate its user if already used")
    revoke.add_argument("code")
    revoke.add_argument("--reason")
    reactivate = sub.add_parser("reactivate-user", help="Reactivate a user by user id or Telegram id")
    reactivate.add_argument("identifier", type=int)
    sub.add_parser("list-users")
    sub.add_parser("list-jobs")
    sub.add_parser("list-failures")
    sub.add_parser("list-captcha-users")
    sub.add_parser("list-login-errors")
    sub.add_parser("list-pending")
    interval = sub.add_parser("set-interval")
    interval.add_argument("seconds", type=int)
    sub.add_parser("init-db")
    recheck = sub.add_parser("recheck", help="Force immediate recheck of all active jobs, ignoring backoff.")
    recheck.add_argument("job_ids", nargs="*", type=int, help="Optional job IDs to recheck. If empty, all active jobs are checked.")
    recheck.add_argument("--verbose", action="store_true", help="Enable verbose logging.")

    set_cid = sub.add_parser("set-cid", help="Update the course creation ID (cid) for a job.")
    set_cid.add_argument("job_id", type=int)
    set_cid.add_argument("cid")

    args = parser.parse_args()
    storage = storage_from_args(args)

    if args.command == "generate-code":
        print(storage.generate_registration_code(ttl_hours=args.ttl_hours))
    elif args.command == "list-codes":
        for code in storage.list_registration_codes():
            expires_at = text_to_dt(code.expires_at)
            expired = bool(expires_at and expires_at < utcnow())
            status = "revoked" if code.revoked_at else ("used" if code.used_at else ("expired" if expired else "unused"))
            print(
                f"{code.code}\tstatus={status}\texpires={code.expires_at or '-'}\t"
                f"used_at={code.used_at or '-'}\tused_by_telegram={code.used_by_telegram_id or '-'}\t"
                f"revoked_at={code.revoked_at or '-'}\treason={code.revoked_reason or '-'}"
            )
    elif args.command == "revoke-code":
        code = storage.revoke_registration_code(args.code, reason=args.reason)
        used_by = code.used_by_telegram_id or "-"
        print(f"revoked {code.code}\tused_by_telegram={used_by}")
    elif args.command == "reactivate-user":
        user = storage.reactivate_user(args.identifier)
        print(f"reactivated user={user.id}\ttelegram={user.telegram_id}\tactive={user.is_active}")
    elif args.command == "list-users":
        for user in storage.list_users():
            print(f"{user.id}\ttelegram={user.telegram_id}\t@{user.username or '-'}\tactive={user.is_active}")
    elif args.command == "list-jobs":
        for job in storage.list_jobs():
            paused = job.paused_at or "-"
            print(
                f"{job.id}\tuser={job.user_id}\t{job.job_type}\tmode={job.mode}\t{job.course_code}\t"
                f"cid={job.course_creation_id or '-'}\t"
                f"enabled={job.enabled}\tpaused={paused}\tcompleted={job.completed_at or '-'}"
            )
    elif args.command == "list-failures":
        for row in storage.list_job_runtime():
            if row.failure_count > 0:
                print(
                    f"job={row.job_id}\tfailures={row.failure_count}\t"
                    f"next_retry_at={row.next_retry_at or '-'}\tlast_error={row.last_error or '-'}"
                )
    elif args.command == "list-captcha-users":
        for row in storage.list_user_runtime():
            if row.needs_captcha:
                print(
                    f"user={row.user_id}\tlast_captcha_at={row.last_captcha_at or '-'}\t"
                    f"note={row.last_captcha_note or '-'}"
                )
    elif args.command == "list-login-errors":
        for row in storage.list_user_runtime():
            if row.last_login_error:
                print(
                    f"user={row.user_id}\tlast_login_error_at={row.last_login_error_at or '-'}\t"
                    f"error={row.last_login_error}"
                )
    elif args.command == "list-pending":
        for row in storage.list_pending_actions():
            print(
                f"job={row.job_id}\tuser={row.user_id}\taction={row.action_type}\t"
                f"target={row.target_section or '-'}\tcreated_at={row.created_at}"
            )
    elif args.command == "set-interval":
        storage.set_interval_secs(args.seconds)
        print(f"interval_secs={storage.get_interval_secs()}")
    elif args.command == "recheck":
        import logging
        level = logging.DEBUG if args.verbose else logging.INFO
        logging.basicConfig(level=level, format="%(asctime)s [%(levelname)s] %(message)s")

        secret_box = SecretBox.from_env()

        async def send_captcha_mock(chat_id, image_bytes, caption):
            print(f"CAPTCHA for {chat_id}: {caption}")

        async def send_message_mock(chat_id, text):
            print(f"TELEGRAM to {chat_id}: {text}")

        ah_service = BotArchersHubService(
            storage,
            secret_box,
            send_captcha_image=send_captcha_mock,
        )
        scheduler = WatchScheduler(
            storage,
            fetch_course=ah_service.fetch_course_for_job,
            send_message=send_message_mock,
            inspect_automation=ah_service.inspect_automation_job,
            execute_automation=ah_service.execute_automation_job,
            execute_automation_batch=ah_service.execute_automation_batch,
        )

        async def run():
            job_ids = set(args.job_ids) if args.job_ids else None
            if job_ids:
                print(f"Force rechecking job(s) {', '.join(map(str, job_ids))}...")
            else:
                print("Force rechecking all active jobs...")
            
            result = await scheduler.run_selected(job_ids=job_ids)
            print(f"Done. checked={result.checked_jobs} notifications={result.notifications_sent} errors={len(result.errors)}")
            for err in result.errors:
                print(f"ERROR: {err}")

        asyncio.run(run())
    elif args.command == "set-cid":
        storage.update_job_course_creation_id(args.job_id, args.cid)
        print(f"Updated job #{args.job_id} cid={args.cid}")
    elif args.command == "init-db":
        print(f"initialized {storage.path}")


if __name__ == "__main__":
    main()
