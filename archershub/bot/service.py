from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass
from typing import Any, Awaitable, Callable

import requests

from ..api import first_list, flatten_jquery_form, normalize_value, post_form_json, string_id
from ..auth import AutomatedCaptchaEscalation, apply_session_cookies_json, create_session, login_with_retry, session_cookies_json
from ..client import ArchersHubClient
from ..constants import AutoSwitchSubmitError, DEFAULT_BASE_URL, DEFAULT_LOGIN_PATH, DEFAULT_MAX_LOGIN_ATTEMPTS
from ..course_search import CourseSearchResult, compact_course, course_display_name, course_from_dict, course_search_context, fetch_course_options, reveal_teachers_with_schedule_data, search_courses, section_summary
from ..crypto import SecretBox
from ..jobs import AutomationBatchResult, AutomationCandidate, choose_add_class_section, plan_change_section
from ..sections import extract_course_code, extract_course_creation_id, fetch_course_data, resolve_course_target, resolve_course_target_by_id
from ..storage import JOB_TYPE_ADD_CLASS, JOB_TYPE_CHANGE_SECTION, CredentialRecord, JobRecord, SQLiteStorage
from ..switching import (
    add_academic_session_to_state,
    build_add_courses_payload,
    find_add_course,
    find_current_enlisted_course,
    get_add_drop_state,
    get_change_section_state,
    get_course_wise_section_data,
    has_course_clash,
    maybe_submit_target_switch,
    resolve_add_drop_reason,
    submit_add_drop,
)


class TelegramCaptchaRequired(RuntimeError):
    pass


@dataclass(frozen=True)
class PreparedAddClass:
    job: JobRecord
    session: Any
    target: dict[str, str]
    add_state: dict[str, Any]
    add_course: dict[str, Any]
    target_section: dict[str, Any]
    target_section_name: str


class BotArchersHubService:
    """Bridge between Telegram users, encrypted storage, and ArchersHub helpers."""

    def __init__(
        self,
        storage: SQLiteStorage,
        secret_box: SecretBox,
        *,
        base_url: str = DEFAULT_BASE_URL,
        send_captcha_image: Callable[[int, bytes, str], Awaitable[None]] | None = None,
    ) -> None:
        self.storage = storage
        self.secret_box = secret_box
        self.base_url = base_url.rstrip("/")
        self.send_captcha_image = send_captcha_image

    async def _notify_captcha_escalation(self, user_id: int, exc: AutomatedCaptchaEscalation) -> None:
        self.storage.mark_user_captcha_needed(user_id, note=str(exc))
        user = next((row for row in self.storage.list_users() if row.id == user_id), None)
        if user is None or self.send_captcha_image is None:
            return
        caption = (
            f"Automated captcha solving failed after {exc.attempts} attempts.\n"
            "Please review the latest captcha image."
        )
        await self.send_captcha_image(user.telegram_id, exc.image_bytes, caption)

    async def verify_and_store_credentials(
        self,
        *,
        user_id: int,
        username: str,
        password: str,
        captcha_reader=None,
    ) -> None:
        def do_login():
            return login_with_retry(
                self.base_url,
                DEFAULT_LOGIN_PATH,
                username,
                password,
                max_attempts=DEFAULT_MAX_LOGIN_ATTEMPTS,
                captcha_ocr=True,
                save_artifacts=True,
                captcha_reader=captcha_reader,
                manual_captcha_fallback=False,
            )

        try:
            session = await asyncio.to_thread(do_login)
        except AutomatedCaptchaEscalation as exc:
            await self._notify_captcha_escalation(user_id, exc)
            raise TelegramCaptchaRequired(
                f"Automatic captcha solving failed after {exc.attempts} attempts. "
                "I sent the latest captcha image here."
            ) from exc
        except Exception as exc:
            self.storage.record_user_login_error(user_id, str(exc))
            raise
        self.storage.clear_user_captcha_needed(user_id)
        self.storage.save_credentials(
            user_id,
            self.secret_box.encrypt_text(username) or "",
            self.secret_box.encrypt_text(password) or "",
            self.secret_box.encrypt_text(session_cookies_json(session)),
        )

    def client_for_credentials(self, credentials: CredentialRecord) -> ArchersHubClient:
        username = self.secret_box.decrypt_text(credentials.username_encrypted)
        password = self.secret_box.decrypt_text(credentials.password_encrypted)
        session = create_session()
        cookies_json = self.secret_box.decrypt_text(credentials.cookies_encrypted)
        apply_session_cookies_json(session, cookies_json)
        return ArchersHubClient(
            base_url=self.base_url,
            username=username,
            password=password,
            session=session,
            allow_mutation=False,
            save_login_artifacts=True,
        )

    def _load_course_bundle(self, job: JobRecord) -> tuple[Any, dict[str, str], Any]:
        credentials = self.storage.get_credentials(job.user_id)
        if credentials is None:
            raise RuntimeError("user has no ArchersHub credentials")
        username = self.secret_box.decrypt_text(credentials.username_encrypted)
        password = self.secret_box.decrypt_text(credentials.password_encrypted)

        session = create_session()
        apply_session_cookies_json(session, self.secret_box.decrypt_text(credentials.cookies_encrypted))
        try:
            target = self._resolve_job_target(session, job)
            course_data = fetch_course_data(session, self.base_url, target)
            return session, target, course_data
        except Exception:
            session = login_with_retry(
                self.base_url,
                DEFAULT_LOGIN_PATH,
                username or "",
                password or "",
                max_attempts=DEFAULT_MAX_LOGIN_ATTEMPTS,
                captcha_ocr=True,
                save_artifacts=True,
                manual_captcha_fallback=False,
            )
            self.storage.clear_user_captcha_needed(job.user_id)
            self.storage.save_credentials(
                job.user_id,
                credentials.username_encrypted,
                credentials.password_encrypted,
                self.secret_box.encrypt_text(session_cookies_json(session)),
            )
            target = self._resolve_job_target(session, job)
            course_data = fetch_course_data(session, self.base_url, target)
            return session, target, course_data

    def _resolve_job_target(self, session: Any, job: JobRecord) -> dict[str, str]:
        if not job.course_creation_id:
            raise RuntimeError(
                f"job #{job.id} for {job.course_code} is missing course_creation_id; "
                "recreate it through Course Search"
            )
        return resolve_course_target_by_id(
            session,
            self.base_url,
            job.course_creation_id,
            course_code=job.course_code,
        )


    def _credentials_session(self, user_id: int) -> tuple[Any, CredentialRecord, str | None, str | None]:
        credentials = self.storage.get_credentials(user_id)
        if credentials is None:
            raise RuntimeError("user has no ArchersHub credentials")
        username = self.secret_box.decrypt_text(credentials.username_encrypted)
        password = self.secret_box.decrypt_text(credentials.password_encrypted)
        session = create_session()
        apply_session_cookies_json(session, self.secret_box.decrypt_text(credentials.cookies_encrypted))
        return session, credentials, username, password

    def _relogin_user(self, user_id: int, credentials: CredentialRecord, username: str | None, password: str | None) -> Any:
        session = login_with_retry(
            self.base_url,
            DEFAULT_LOGIN_PATH,
            username or "",
            password or "",
            max_attempts=DEFAULT_MAX_LOGIN_ATTEMPTS,
            captcha_ocr=True,
            save_artifacts=True,
            manual_captcha_fallback=False,
        )
        self.storage.clear_user_captcha_needed(user_id)
        self.storage.save_credentials(
            user_id,
            credentials.username_encrypted,
            credentials.password_encrypted,
            self.secret_box.encrypt_text(session_cookies_json(session)),
        )
        return session

    def _with_user_session_retry(self, user_id: int, operation):
        session, credentials, username, password = self._credentials_session(user_id)
        try:
            return operation(session)
        except Exception:
            session = self._relogin_user(user_id, credentials, username, password)
            return operation(session)

    async def search_courses_for_user(self, user_id: int, query: str) -> list[dict[str, str]]:
        try:
            def load(session):
                context = course_search_context(session, self.base_url)
                return [compact_course(course) for course in search_courses(fetch_course_options(session, self.base_url, context), query)]
            return await asyncio.to_thread(lambda: self._with_user_session_retry(user_id, load))
        except AutomatedCaptchaEscalation as exc:
            await self._notify_captcha_escalation(user_id, exc)
            raise TelegramCaptchaRequired(
                f"Automatic captcha solving failed after {exc.attempts} attempts while searching courses. "
                "I sent the latest captcha image to the user."
            ) from exc

    async def resolve_course_for_user(self, user_id: int, course_code: str) -> dict[str, str]:
        try:
            def load(session):
                return resolve_course_target(session, self.base_url, course_code)
            return await asyncio.to_thread(lambda: self._with_user_session_retry(user_id, load))
        except AutomatedCaptchaEscalation as exc:
            await self._notify_captcha_escalation(user_id, exc)
            raise TelegramCaptchaRequired(
                f"Automatic captcha solving failed after {exc.attempts} attempts while resolving {course_code}. "
                "I sent the latest captcha image to the user."
            ) from exc

    def format_course_matches(self, matches: list[dict[str, Any]], campus_id: str, academic_session_id: str) -> list[dict[str, str]]:
        return [
            compact_course(
                CourseSearchResult(
                    course_code=extract_course_code(item) or "",
                    course_name=course_display_name(item),
                    course_creation_id=str(extract_course_creation_id(item)),
                    campus_id=campus_id,
                    academic_session_id=academic_session_id,
                    is_cross_offer=normalize_value(item.get("is_cross_offer")) or "0",
                    grid_type=normalize_value(item.get("grid_type")) or "0",
                )
            )
            for item in matches
        ]

    async def port_legacy_jobs(self, send_message: Callable[[int, str], Awaitable[None]] | None = None) -> None:
        for job in self.storage.list_jobs(active_only=True):
            if job.course_creation_id:
                continue
            user = next((row for row in self.storage.list_users() if row.id == job.user_id and row.is_active), None)
            try:
                target = await self.resolve_course_for_user(job.user_id, job.course_code)
            except Exception as exc:
                self.storage.pause_job(job.id)
                if user and send_message:
                    await send_message(
                        user.telegram_id,
                        f"Paused legacy job #{job.id} for {job.course_code}: {exc}\n\n"
                        "Please recreate it through 🔎 Search courses so the exact Course Finder offering is selected.",
                    )
                continue
            duplicate = self.storage.find_duplicate_active_job(
                user_id=job.user_id,
                job_type=job.job_type,
                course_code=target["course_code"],
                course_creation_id=target["course_creation_id"],
                section_filters=job.section_filters,
                priority_sections=job.priority_sections,
                target_section=job.target_section,
            )
            if duplicate is not None and duplicate.id != job.id:
                self.storage.pause_job(job.id)
                if user and send_message:
                    await send_message(
                        user.telegram_id,
                        f"Paused duplicate legacy job #{job.id} for {job.course_code}; existing job #{duplicate.id} already targets this offering."
                    )
                continue
            self.storage.update_job_course_creation_id(job.id, target["course_creation_id"])

    async def fetch_search_course_sections(self, user_id: int, course_data: dict[str, Any]) -> list[dict[str, Any]]:
        try:
            def load(session):
                course = course_from_dict(course_data)
                sections = fetch_course_data(session, self.base_url, course.as_target())
                if not isinstance(sections, list):
                    sections = []
                try:
                    sections = reveal_teachers_with_schedule_data(session, self.base_url, course, sections)
                except Exception:
                    pass
                return [section_summary(section) for section in sections if isinstance(section, dict)]
            return await asyncio.to_thread(lambda: self._with_user_session_retry(user_id, load))
        except AutomatedCaptchaEscalation as exc:
            await self._notify_captcha_escalation(user_id, exc)
            raise TelegramCaptchaRequired(
                f"Automatic captcha solving failed after {exc.attempts} attempts while loading sections. "
                "I sent the latest captcha image to the user."
            ) from exc

    async def fetch_course_for_job(self, job: JobRecord) -> Any:
        try:
            return await asyncio.to_thread(lambda: self._load_course_bundle(job)[2])
        except AutomatedCaptchaEscalation as exc:
            await self._notify_captcha_escalation(job.user_id, exc)
            raise TelegramCaptchaRequired(
                f"Automatic captcha solving failed after {exc.attempts} attempts for {job.course_code}. "
                "I sent the latest captcha image to the user."
            ) from exc

    async def inspect_automation_job(self, job: JobRecord) -> AutomationCandidate | None:
        try:
            return await asyncio.to_thread(self._inspect_automation_job_sync, job)
        except AutomatedCaptchaEscalation as exc:
            await self._notify_captcha_escalation(job.user_id, exc)
            raise TelegramCaptchaRequired(
                f"Automatic captcha solving failed after {exc.attempts} attempts for {job.course_code}. "
                "I sent the latest captcha image to the user."
            ) from exc

    def _inspect_automation_job_sync(self, job: JobRecord) -> AutomationCandidate | None:
        session, target, course_data = self._load_course_bundle(job)
        if job.job_type == JOB_TYPE_CHANGE_SECTION:
            if not job.target_section:
                raise RuntimeError("change-section job is missing target section")
            state = get_change_section_state(session, self.base_url, target["academic_session_id"])
            try:
                current_course = find_current_enlisted_course(state, target["course_code"], target["course_creation_id"])
            except RuntimeError:
                return self._warning_candidate(
                    job,
                    action="change_not_eligible",
                    reason=(
                        f"{job.course_code} is not change-section eligible for your account right now. "
                        "I will keep checking and notify you if it becomes available."
                    ),
                    dedupe_key=f"change-not-eligible:{job.id}",
                )
            decision = plan_change_section(
                course_data,
                current_section_id=str(current_course.get("section_creation_id") or ""),
                target_section_name=job.target_section,
            )
            if decision.target_section is None:
                return self._warning_candidate(
                    job,
                    action="change_target_missing",
                    reason=(
                        f"Target section {job.target_section} was not found for {job.course_code}. "
                        "I will keep checking in case it appears later."
                    ),
                    dedupe_key=f"change-target-missing:{job.id}:{job.target_section}",
                )
            if not decision.should_submit or decision.target_section is None:
                return None
            section = decision.target_section
            return AutomationCandidate(
                job_type=job.job_type,
                course_code=job.course_code,
                action="change_section",
                reason=decision.reason,
                target_section_name=str(section.get("section_name") or job.target_section),
                dedupe_key=f"change:{section.get('section_creation_id')}",
                details={
                    "current_section_id": str(current_course.get("section_creation_id") or ""),
                    "target_section_id": str(section.get("section_creation_id") or ""),
                    "target_schedule": str(section.get("schedule") or ""),
                },
            )

        if job.job_type == JOB_TYPE_ADD_CLASS:
            add_state = get_add_drop_state(session, self.base_url, target["academic_session_id"])
            try:
                find_current_enlisted_course(add_state, target["course_code"], target["course_creation_id"])
            except RuntimeError:
                pass
            else:
                if job.priority_sections:
                    target_section = job.priority_sections[0]
                    self.storage.convert_job_to_change_section(job.id, target_section)
                    return self._warning_candidate(
                        job,
                        action="converted_to_change",
                        reason=(
                            f"You already have {job.course_code}, so I converted job #{job.id} "
                            f"to change-section targeting {target_section}."
                        ),
                        dedupe_key=f"converted-to-change:{job.id}:{target_section}",
                        target_section_name=target_section,
                    )
                return self._warning_candidate(
                    job,
                    action="already_enlisted_needs_target",
                    reason=(
                        f"You already have {job.course_code}. Add-class is only for classes you do not have yet. "
                        f"Use /change {job.course_code} SECTION to choose a target section."
                    ),
                    dedupe_key=f"already-enlisted-no-target:{job.id}",
                )
            if self._find_add_course(add_state, target["course_code"], target["course_creation_id"]) is None:
                return self._warning_candidate(
                    job,
                    action="add_not_eligible",
                    reason=(
                        f"{job.course_code} is visible in Course Finder but is not add-eligible for your account right now. "
                        "This can happen because of curriculum/campus rules, enrollment appointment timing, max units, "
                        "or add/drop rules. I will keep checking and notify you if it becomes available."
                    ),
                    dedupe_key=f"add-not-eligible:{job.id}",
                )
            campus_no = str(add_state.get("campusno") or target["campus_id"])
            decision = choose_add_class_section(
                course_data,
                priority_sections=job.priority_sections,
                clashes=lambda section: has_course_clash(
                    session,
                    self.base_url,
                    target["academic_session_id"],
                    target["course_creation_id"],
                    str(section.get("section_creation_id") or ""),
                    campus_no,
                ),
            )
            if decision.selected_section is None:
                return None
            section = decision.selected_section
            return AutomationCandidate(
                job_type=job.job_type,
                course_code=job.course_code,
                action="add_class",
                reason=decision.reason,
                target_section_name=str(section.get("section_name") or ""),
                dedupe_key=f"add:{section.get('section_creation_id')}",
                details={
                    "academic_session_id": str(target.get("academic_session_id") or ""),
                    "course_creation_id": str(target.get("course_creation_id") or ""),
                    "target_section_id": str(section.get("section_creation_id") or ""),
                    "target_schedule": str(section.get("schedule") or ""),
                    "fallback_used": decision.fallback_used,
                    "skipped_priority_clashes": [str(row.get("section_name") or "") for row in decision.skipped_priority_clashes],
                    "unavailable_priority_sections": decision.unavailable_priority_sections,
                },
            )

        return None

    async def execute_automation_job(self, job: JobRecord) -> str:
        try:
            return await asyncio.to_thread(self._execute_automation_job_sync, job)
        except AutomatedCaptchaEscalation as exc:
            await self._notify_captcha_escalation(job.user_id, exc)
            raise TelegramCaptchaRequired(
                f"Automatic captcha solving failed after {exc.attempts} attempts for {job.course_code}. "
                "I sent the latest captcha image to the user."
            ) from exc

    def _execute_automation_job_sync(self, job: JobRecord) -> str:
        if job.job_type == JOB_TYPE_CHANGE_SECTION:
            session, target, course_data = self._load_course_bundle(job)
            if not job.target_section:
                raise RuntimeError("change-section job is missing target section")
            decision = plan_change_section(
                course_data,
                current_section_id=None,
                target_section_name=job.target_section,
            )
            if not decision.should_submit or decision.target_section is None:
                raise RuntimeError(decision.reason)
            maybe_submit_target_switch(
                session,
                self.base_url,
                target,
                job.target_section,
                decision.target_section,
            )
            return f"Changed {job.course_code} to section {job.target_section}."

        if job.job_type == JOB_TYPE_ADD_CLASS:
            return self._execute_add_class_batch_sync([job]).message

        raise RuntimeError(f"unsupported automation job type: {job.job_type}")

    async def execute_automation_batch(self, jobs: list[JobRecord]) -> AutomationBatchResult:
        try:
            return await asyncio.to_thread(self._execute_add_class_batch_sync, jobs)
        except AutomatedCaptchaEscalation as exc:
            user_id = jobs[0].user_id if jobs else 0
            await self._notify_captcha_escalation(user_id, exc)
            raise TelegramCaptchaRequired(
                f"Automatic captcha solving failed after {exc.attempts} attempts for an add/drop batch. "
                "I sent the latest captcha image to the user."
            ) from exc

    def _execute_add_class_batch_sync(self, jobs: list[JobRecord]) -> AutomationBatchResult:
        if not jobs:
            raise RuntimeError("add/drop batch is empty")
        user_ids = {job.user_id for job in jobs}
        if len(user_ids) != 1:
            raise RuntimeError("add/drop batch can only include one user")
        if any(job.job_type != JOB_TYPE_ADD_CLASS for job in jobs):
            raise RuntimeError("add/drop batch can only include add-class jobs")

        prepared = [self._prepare_add_class_job(job) for job in jobs]
        academic_session_ids = {item.target["academic_session_id"] for item in prepared}
        if len(academic_session_ids) != 1:
            raise RuntimeError("add/drop batch can only include one academic session")

        academic_session_id = prepared[0].target["academic_session_id"]
        add_state = add_academic_session_to_state(prepared[0].add_state, academic_session_id)
        reason_required = str(add_state.get("is_mandatory", "0")) == "1"
        add_reason_id = resolve_add_drop_reason(add_state, "1", required=reason_required)
        payload = build_add_courses_payload(
            state=add_state,
            additions=[(item.add_course, item.target_section) for item in prepared],
            add_reason_id=add_reason_id,
        )
        self._check_add_class_batch_clashes(prepared)
        lines = ["Added through one add/drop submission:"]
        lines.extend(f"• {item.job.course_code} section {item.target_section_name}" for item in prepared)
        message = "\n".join(lines)
        try:
            submit_add_drop(prepared[0].session, self.base_url, payload)
        except (requests.exceptions.Timeout, requests.exceptions.ConnectionError) as exc:
            time.sleep(10)
            if self._add_class_batch_reflected(prepared):
                return AutomationBatchResult(
                    submitted_job_ids=[item.job.id for item in prepared],
                    message=message,
                )
            raise AutoSwitchSubmitError(
                "add/drop batch submit was attempted but could not be verified; "
                "stopping to avoid a second add/drop submission"
            ) from exc
        except Exception as exc:
            time.sleep(10)
            if self._add_class_batch_reflected(prepared):
                return AutomationBatchResult(
                    submitted_job_ids=[item.job.id for item in prepared],
                    message=message,
                )
            raise AutoSwitchSubmitError(
                "add/drop batch submit was attempted but did not return a clean success; "
                "stopping to avoid a second add/drop submission"
            ) from exc

        return AutomationBatchResult(
            submitted_job_ids=[item.job.id for item in prepared],
            message=message,
        )

    def _prepare_add_class_job(self, job: JobRecord) -> PreparedAddClass:
        session, target, course_data = self._load_course_bundle(job)
        add_state = get_add_drop_state(session, self.base_url, target["academic_session_id"])
        try:
            find_current_enlisted_course(add_state, target["course_code"], target["course_creation_id"])
        except RuntimeError:
            pass
        else:
            raise RuntimeError(f"{job.course_code} is already enlisted; use change-section instead of add/drop")

        campus_no = str(add_state.get("campusno") or target["campus_id"])
        decision = choose_add_class_section(
            course_data,
            priority_sections=job.priority_sections,
            clashes=lambda section: has_course_clash(
                session,
                self.base_url,
                target["academic_session_id"],
                target["course_creation_id"],
                str(section.get("section_creation_id") or ""),
                campus_no,
            ),
        )
        if decision.selected_section is None:
            raise RuntimeError(decision.reason)
        target_section = decision.selected_section
        add_course = self._find_add_course(add_state, target["course_code"], target["course_creation_id"])
        if add_course is None:
            raise RuntimeError(f"{job.course_code} is not add-eligible right now")
        section_data = get_course_wise_section_data(
            session,
            self.base_url,
            target["academic_session_id"],
            target["course_creation_id"],
            target["is_cross_offer"],
            target["grid_type"],
        )
        available_ids = {
            str(row.get("section_creation_id"))
            for row in section_data.get("section_details", [])
            if isinstance(row, dict)
        }
        target_section_id = str(target_section.get("section_creation_id") or "")
        if available_ids and target_section_id not in available_ids:
            raise RuntimeError("selected section is no longer available for add-course")
        if has_course_clash(
            session,
            self.base_url,
            target["academic_session_id"],
            target["course_creation_id"],
            target_section_id,
            campus_no,
        ):
            raise RuntimeError(f"selected section {target_section.get('section_name')} has a schedule clash")
        return PreparedAddClass(
            job=job,
            session=session,
            target=target,
            add_state=add_state,
            add_course=add_course,
            target_section=target_section,
            target_section_name=str(target_section.get("section_name") or ""),
        )

    def _check_add_class_batch_clashes(self, prepared: list[PreparedAddClass]) -> None:
        if not prepared:
            return
        state = prepared[0].add_state
        academic_session_id = prepared[0].target["academic_session_id"]
        campus_no = string_id(state.get("campusno") or prepared[0].target["campus_id"])
        course_list: list[dict[str, str]] = []
        for row in first_list(state, "get_enlisted_subject"):
            if not isinstance(row, dict):
                continue
            course_creation_id = string_id(row.get("course_creation_id"))
            section_creation_id = string_id(row.get("section_creation_id"))
            if not course_creation_id or not section_creation_id:
                continue
            course_list.append(
                {
                    "COURSE_CREATION_ID": course_creation_id,
                    "SECTION_CREATION_ID": section_creation_id,
                    "CAMPUSNO": string_id(row.get("parent_campus_no") or campus_no),
                }
            )
        for item in prepared:
            course_list.append(
                {
                    "COURSE_CREATION_ID": item.target["course_creation_id"],
                    "SECTION_CREATION_ID": string_id(item.target_section.get("section_creation_id")),
                    "CAMPUSNO": string_id(item.add_state.get("campusno") or item.target["campus_id"]),
                }
            )
        data = post_form_json(
            prepared[0].session,
            self.base_url,
            "/Enlistment/GetCourseClashDetails/",
            flatten_jquery_form({"academicSessionId": academic_session_id, "CourseList": course_list}),
        )
        normalized = normalize_value(data)
        if not isinstance(normalized, list) or not normalized or not isinstance(normalized[0], dict):
            raise RuntimeError("batch course clash response had an unexpected shape")
        status = normalized[0].get("status")
        if str(status) != "1":
            raise RuntimeError(f"add/drop batch has a schedule clash or server warning: {status}")

    def _add_class_batch_reflected(self, prepared: list[PreparedAddClass]) -> bool:
        if not prepared:
            return False
        try:
            state = get_add_drop_state(prepared[0].session, self.base_url, prepared[0].target["academic_session_id"])
            for item in prepared:
                current = find_current_enlisted_course(state, item.target["course_code"], item.target["course_creation_id"])
                if string_id(current.get("section_creation_id")) != string_id(item.target_section.get("section_creation_id")):
                    return False
            return True
        except Exception:
            return False

    @staticmethod
    def _find_add_course(add_state: dict[str, Any], course_code: str, course_creation_id: str) -> dict[str, Any] | None:
        try:
            return find_add_course(add_state, course_code, course_creation_id)
        except RuntimeError as exc:
            if "was not found in add-eligible courses" not in str(exc):
                raise
            return None

    @staticmethod
    def _warning_candidate(
        job: JobRecord,
        *,
        action: str,
        reason: str,
        dedupe_key: str,
        target_section_name: str | None = None,
    ) -> AutomationCandidate:
        return AutomationCandidate(
            job_type=job.job_type,
            course_code=job.course_code,
            action=action,
            reason=reason,
            target_section_name=target_section_name,
            dedupe_key=dedupe_key,
            details={"warning_only": True},
        )
