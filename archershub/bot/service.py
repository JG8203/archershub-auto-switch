from __future__ import annotations

import asyncio
from typing import Any, Awaitable, Callable

from ..auth import AutomatedCaptchaEscalation, apply_session_cookies_json, create_session, login_with_retry, session_cookies_json
from ..client import ArchersHubClient
from ..constants import DEFAULT_BASE_URL, DEFAULT_LOGIN_PATH, DEFAULT_MAX_LOGIN_ATTEMPTS
from ..crypto import SecretBox
from ..jobs import AutomationCandidate, choose_add_class_section, plan_change_section
from ..sections import fetch_course_data, resolve_course_target
from ..storage import JOB_TYPE_ADD_CLASS, JOB_TYPE_CHANGE_SECTION, CredentialRecord, JobRecord, SQLiteStorage
from ..switching import (
    build_add_course_payload,
    find_add_course,
    find_current_enlisted_course,
    get_add_drop_state,
    get_change_section_state,
    get_course_wise_section_data,
    has_course_clash,
    maybe_submit_target_switch,
    resolve_add_drop_reason,
)


class TelegramCaptchaRequired(RuntimeError):
    pass


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
                save_artifacts=False,
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
            save_login_artifacts=False,
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
            target = resolve_course_target(session, self.base_url, job.course_code)
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
                save_artifacts=False,
                manual_captcha_fallback=False,
            )
            self.storage.clear_user_captcha_needed(job.user_id)
            self.storage.save_credentials(
                job.user_id,
                credentials.username_encrypted,
                credentials.password_encrypted,
                self.secret_box.encrypt_text(session_cookies_json(session)),
            )
            target = resolve_course_target(session, self.base_url, job.course_code)
            course_data = fetch_course_data(session, self.base_url, target)
            return session, target, course_data

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
        session, target, course_data = self._load_course_bundle(job)
        if job.job_type == JOB_TYPE_CHANGE_SECTION:
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
            add_state = get_add_drop_state(session, self.base_url, target["academic_session_id"])
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
            reason_required = str(add_state.get("is_mandatory", "0")) == "1"
            add_reason_id = resolve_add_drop_reason(add_state, "1", required=reason_required)
            payload = build_add_course_payload(
                state={**add_state, "academic_session_id": target["academic_session_id"]},
                add_course=add_course,
                target_section=target_section,
                add_reason_id=add_reason_id,
            )
            from ..switching import submit_add_drop

            submit_add_drop(session, self.base_url, payload)
            return f"Added {job.course_code} section {target_section.get('section_name')}."

        raise RuntimeError(f"unsupported automation job type: {job.job_type}")

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
