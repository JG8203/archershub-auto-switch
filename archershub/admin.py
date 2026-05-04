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


def _value(row: dict, *keys: str) -> str:
    for key in keys:
        value = row.get(key)
        if value not in (None, ""):
            return str(value)
    return "-"


def _current_academic_session_id(sessions) -> str | None:
    if not isinstance(sessions, list):
        return None
    current = next((row for row in sessions if isinstance(row, dict) and row.get("is_current_session")), None)
    row = current or next((row for row in sessions if isinstance(row, dict)), None)
    if row is None:
        return None
    value = row.get("academic_session_id")
    return str(value) if value not in (None, "") else None


def _enlistment_rows(data) -> list[dict]:
    if isinstance(data, list):
        return [row for row in data if isinstance(row, dict)]
    if isinstance(data, dict):
        for key in ("data", "rows", "profile_enlistment_grid_list", "profile_enlistmentgrid_list"):
            rows = data.get(key)
            if isinstance(rows, list):
                return [row for row in rows if isinstance(row, dict)]
    return []


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

    schedule = sub.add_parser("list-schedule", help="Fetch and display the current schedule for a user.")
    schedule.add_argument("identifier", help="User ID or ArchersHub username/id number.")

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
    elif args.command == "list-schedule":
        from .client import ArchersHubClient
        secret_box = SecretBox.from_env()

        user_id = None
        # 1. Try finding by database ID
        if args.identifier.isdigit():
            user = storage.get_user(int(args.identifier))
            if user:
                user_id = user.id
        
        # 2. Try finding by Telegram username
        if user_id is None:
            target_username = args.identifier.lstrip("@").lower()
            for user in storage.list_users():
                if user.username and user.username.lower() == target_username:
                    user_id = user.id
                    break

        # 3. Try finding by decrypted ArchersHub username (ID number)
        if user_id is None:
            for user in storage.list_users():
                creds = storage.get_credentials(user.id)
                if creds:
                    try:
                        username = secret_box.decrypt_text(creds.username_encrypted)
                        if username == args.identifier:
                            user_id = user.id
                            break
                    except Exception:
                        continue
        
        if user_id is None:
            print(f"User not found for identifier: {args.identifier}")
            return

        creds = storage.get_credentials(user_id)
        if not creds:
            print(f"No credentials stored for user ID {user_id}")
            return

        username = secret_box.decrypt_text(creds.username_encrypted)
        password = secret_box.decrypt_text(creds.password_encrypted)

        client = ArchersHubClient(username=username, password=password)
        try:
            print(f"Logging in as {username}...")
            client.login()
            print("Fetching enlistment schedule...")
            academic_session_id = _current_academic_session_id(client.profile_enlistment.get_all_drop_down())
            data = client.profile_enlistment.get_profile_enlistmentgrid_list(
                params={"academicid": academic_session_id} if academic_session_id else {}
            )
            rows = _enlistment_rows(data)

            if not rows:
                print("No enrolled courses found in enlistment data.")
                return

            print(f"\nSchedule for {username}:")
            print("-" * 80)
            for row in rows:
                code = _value(row, "course_code", "COURSE_CODE")
                name = _value(row, "course_name", "COURSE_NAME")
                section = _value(row, "section_name", "SECTION_NAME")
                credits = _value(row, "credits", "CREDITS")
                status = _value(row, "status", "STATUS", "approval_status", "APPROVAL_STATUS")
                schedule = _value(row, "time_table_date", "TIME_TABLE_DATE", "schedule", "SCHEDULE")
                print(f"{code} - {name}")
                print(f"  Section: {section}")
                print(f"  Credits: {credits}")
                print(f"  Status: {status}")
                print(f"  Schedule: {schedule}")
                print("-" * 80)

        except Exception as exc:
            print(f"Failed to fetch schedule: {exc}")

    elif args.command == "init-db":
        print(f"initialized {storage.path}")


if __name__ == "__main__":
    main()
