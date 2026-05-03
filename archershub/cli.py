from __future__ import annotations

import argparse
import getpass
import time
from pathlib import Path
from random import uniform

from .auth import login_once, login_with_retry
from .console import log
from .constants import (
    AutoSwitchSubmitError,
    DEFAULT_BASE_URL,
    DEFAULT_INTERVAL_SECS,
    DEFAULT_LOGIN_PATH,
    DEFAULT_MAX_LOGIN_ATTEMPTS,
    SWITCH_STRATEGY_CHANGE_SECTION,
    SWITCH_STRATEGY_DROP_ADD,
)
from .env import load_project_env
from .sections import (
    available_slots,
    effective_capacity,
    fetch_course_data,
    fetch_course_snapshot,
    find_target_section,
    is_section_open,
    normalize_section_name,
    resolve_course_target,
)
from .switching import maybe_submit_drop_add_switch, maybe_submit_target_switch


def persist_snapshot(snapshot: str, snapshot_file: str | None) -> None:
    if snapshot_file:
        Path(snapshot_file).write_text(snapshot, encoding="utf-8")
    print(snapshot)


def authenticated_session(args: argparse.Namespace, password: str):
    return login_with_retry(
        args.base_url,
        args.login_path,
        args.username,
        password,
        max_attempts=args.max_login_attempts,
        captcha_ocr=not args.no_captcha_ocr,
    )


def selected_course_target(session, args: argparse.Namespace) -> dict[str, str]:
    return resolve_course_target(
        session,
        args.base_url,
        args.course_code,
        campus_id=args.campus_id,
        academic_session_id=args.academic_session_id,
    )


def verbose_log(args: argparse.Namespace, message: str) -> None:
    if getattr(args, "verbose", False):
        log(f"verbose: {message}")


def run_course_watch(args: argparse.Namespace, password: str) -> None:
    session = authenticated_session(args, password)
    target = selected_course_target(session, args)
    log(
        "watching "
        f"course_code={target['course_code']} "
        f"course_creation_id={target['course_creation_id']} "
        f"campus_id={target['campus_id']} "
        f"academic_session_id={target['academic_session_id']}"
    )

    previous_snapshot: str | None = None
    while True:
        try:
            verbose_log(args, "fetching course snapshot")
            snapshot = fetch_course_snapshot(session, args.base_url, target)
        except Exception as exc:
            log(f"poll failed: {exc}; re-authenticating")
            verbose_log(args, "session may be expired; logging in again")
            session = authenticated_session(args, password)
            target = selected_course_target(session, args)
            continue

        if previous_snapshot is None:
            log("received initial snapshot")
            persist_snapshot(snapshot, args.snapshot_file)
        elif previous_snapshot != snapshot:
            log("change detected")
            persist_snapshot(snapshot, args.snapshot_file)
        else:
            log("no change")

        previous_snapshot = snapshot
        if args.once:
            break
        time.sleep(args.interval_secs)


def run_auto_switch_section(args: argparse.Namespace, password: str) -> None:
    if not args.target_section:
        raise RuntimeError("--target-section is required with --auto-switch-section")

    session = authenticated_session(args, password)
    target = selected_course_target(session, args)
    log(
        "auto-switch watching "
        f"course_code={target['course_code']} "
        f"target_section={args.target_section} "
        f"course_creation_id={target['course_creation_id']} "
        f"campus_id={target['campus_id']} "
        f"academic_session_id={target['academic_session_id']}"
    )

    while True:
        try:
            verbose_log(args, "fetching latest course data")
            course_data = fetch_course_data(session, args.base_url, target)
            section = find_target_section(course_data, args.target_section)
            if section is None:
                raise RuntimeError(f"target section {args.target_section} was not found")

            slots = available_slots(section)
            log(
                f"{target['course_code']} {args.target_section}: "
                f"enlisted={section.get('enlisted')} "
                f"capacity={effective_capacity(section):g} "
                f"available={slots:g}"
            )

            if is_section_open(section):
                log("target section appears open; rechecking before submit")
                verbose_log(args, "target appears open; re-fetching before mutation")
                course_data = fetch_course_data(session, args.base_url, target)
                section = find_target_section(course_data, args.target_section)
                if section is None:
                    raise RuntimeError(f"target section {args.target_section} disappeared on recheck")
                if not is_section_open(section):
                    log("target section closed during recheck; continuing watch")
                elif args.switch_strategy == SWITCH_STRATEGY_CHANGE_SECTION:
                    if maybe_submit_target_switch(
                        session,
                        args.base_url,
                        target,
                        args.target_section,
                        section,
                        change_reason_id=args.change_reason_id,
                        change_reason_text=args.change_reason_text,
                    ):
                        return
                elif args.switch_strategy == SWITCH_STRATEGY_DROP_ADD:
                    if maybe_submit_drop_add_switch(
                        session,
                        args.base_url,
                        target,
                        args.target_section,
                        section,
                        add_reason_id=args.add_reason_id,
                        add_reason_text=args.add_reason_text,
                        drop_reason_id=args.drop_reason_id,
                        drop_reason_text=args.drop_reason_text,
                    ):
                        return
                else:
                    raise RuntimeError(f"unknown switch strategy: {args.switch_strategy}")
        except AutoSwitchSubmitError:
            raise
        except Exception as exc:
            log(f"auto-switch poll/submit failed: {exc}")
            if args.once:
                raise
            log("re-authenticating before continuing")
            verbose_log(args, "session may be expired; logging in again")
            session = authenticated_session(args, password)
            target = selected_course_target(session, args)

        if args.once:
            break

        jitter = uniform(0, min(3, max(0, args.interval_secs * 0.1)))
        time.sleep(args.interval_secs + jitter)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Login to ArchersHub and optionally watch Course Finder offerings."
    )
    parser.add_argument("base_url", nargs="?", default=DEFAULT_BASE_URL)
    parser.add_argument("--login-path", default=DEFAULT_LOGIN_PATH)
    parser.add_argument("--username")
    parser.add_argument("--password")
    parser.add_argument("--no-captcha-ocr", action="store_true", help="Disable Tesseract OCR and always ask for captcha manually.")
    parser.add_argument("--course-code", help="Enable course-watch mode for this course code.")
    parser.add_argument("--auto-switch-section", action="store_true", help="Automatically change to --target-section when it opens.")
    parser.add_argument(
        "--switch-strategy",
        choices=[SWITCH_STRATEGY_DROP_ADD, SWITCH_STRATEGY_CHANGE_SECTION],
        default=SWITCH_STRATEGY_CHANGE_SECTION,
        help="How --auto-switch-section submits the switch. Defaults to change-section.",
    )
    parser.add_argument("--target-section", help="Target section name for --auto-switch-section, e.g. Y03.")
    parser.add_argument("--change-reason-id", help="Reason id to use if change-section requires a reason.")
    parser.add_argument("--change-reason-text", help="Text to match against change-section reasons.")
    parser.add_argument("--add-reason-id", help="Add reason id to use for drop-add.")
    parser.add_argument("--add-reason-text", help="Text to match against add reasons for drop-add.")
    parser.add_argument("--drop-reason-id", help="Drop reason id to use for drop-add.")
    parser.add_argument("--drop-reason-text", help="Text to match against drop reasons for drop-add.")
    parser.add_argument("--campus-id")
    parser.add_argument("--academic-session-id")
    parser.add_argument("--interval-secs", type=int, default=DEFAULT_INTERVAL_SECS)
    parser.add_argument("--max-login-attempts", type=int, default=DEFAULT_MAX_LOGIN_ATTEMPTS)
    parser.add_argument("--snapshot-file")
    parser.add_argument("--once", action="store_true")
    parser.add_argument("--verbose", action="store_true", help="Print extra CLI flow logs.")
    return parser.parse_args()


def main() -> None:
    load_project_env()
    args = parse_args()
    args.base_url = args.base_url.rstrip("/")
    args.username = args.username or input("Username / Email: ").strip()
    password = args.password or getpass.getpass("Password: ")

    if args.course_code:
        args.course_code = args.course_code.upper()
        if args.auto_switch_section:
            args.target_section = normalize_section_name(args.target_section)
            run_auto_switch_section(args, password)
        else:
            run_course_watch(args, password)
    else:
        if args.auto_switch_section:
            raise RuntimeError("--course-code is required with --auto-switch-section")
        login_once(
            args.base_url,
            args.login_path,
            args.username,
            password,
            captcha_ocr=not args.no_captcha_ocr,
        )
