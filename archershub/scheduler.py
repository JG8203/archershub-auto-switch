from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from datetime import timedelta
from typing import Any, Awaitable, Callable

from .jobs import AutomationCandidate
from .notifications import compact_sections, diff_sections, filter_sections, format_changes, has_changes
from .storage import JOB_MODE_AUTO, JOB_MODE_CONFIRM, JOB_MODE_NOTIFY, JOB_TYPE_WATCH, JobRecord, SQLiteStorage, dt_to_text, text_to_dt, utcnow

FetchCourse = Callable[[JobRecord], Awaitable[Any]]
SendMessage = Callable[[int, str], Awaitable[None]]
InspectAutomation = Callable[[JobRecord], Awaitable[AutomationCandidate | None]]
ExecuteAutomation = Callable[[JobRecord], Awaitable[str]]


@dataclass(frozen=True)
class SchedulerCycleResult:
    checked_jobs: int
    notifications_sent: int
    errors: list[str]


class WatchScheduler:
    """Global checker that processes all active watcher jobs at the admin interval."""

    def __init__(
        self,
        storage: SQLiteStorage,
        fetch_course: FetchCourse,
        send_message: SendMessage,
        inspect_automation: InspectAutomation | None = None,
        execute_automation: ExecuteAutomation | None = None,
    ) -> None:
        self.storage = storage
        self.fetch_course = fetch_course
        self.send_message = send_message
        self.inspect_automation = inspect_automation
        self.execute_automation = execute_automation
        self._stopped = asyncio.Event()

    def stop(self) -> None:
        self._stopped.set()

    async def run_forever(self) -> None:
        while not self._stopped.is_set():
            await self.run_once()
            try:
                await asyncio.wait_for(self._stopped.wait(), timeout=self.storage.get_interval_secs())
            except asyncio.TimeoutError:
                pass

    async def run_once(self) -> SchedulerCycleResult:
        jobs = self.storage.list_jobs(active_only=True)
        return await self._run_jobs(jobs, ignore_backoff=False)

    async def run_selected(self, *, user_id: int | None = None, job_ids: set[int] | None = None) -> SchedulerCycleResult:
        jobs = self.storage.list_jobs(user_id=user_id)
        selected = [job for job in jobs if job.enabled and job.completed_at is None and job.paused_at is None]
        if job_ids is not None:
            selected = [job for job in selected if job.id in job_ids]
        return await self._run_jobs(selected, ignore_backoff=True)

    async def _run_jobs(self, jobs: list[JobRecord], *, ignore_backoff: bool) -> SchedulerCycleResult:
        checked = 0
        sent = 0
        errors: list[str] = []
        cycle_cache: dict[tuple[int, str], Any] = {}

        for job in jobs:
            if not ignore_backoff and self._is_backing_off(job):
                continue
            checked += 1
            try:
                self.storage.record_job_checked(job.id)
                if job.job_type == JOB_TYPE_WATCH:
                    cache_key = (job.user_id, job.course_code)
                    if cache_key not in cycle_cache:
                        cycle_cache[cache_key] = await self.fetch_course(job)
                    course_data = cycle_cache[cache_key]
                    sections = compact_sections(filter_sections(course_data, job.section_filters))
                    snapshot_key = f"job:{job.id}:watch"
                    previous = self.storage.get_snapshot(snapshot_key)
                    changes = diff_sections(previous, sections)
                    self.storage.set_snapshot(snapshot_key, sections)
                    if previous is not None and has_changes(changes):
                        user = self._user_for_job(job)
                        if user:
                            await self.send_message(user.telegram_id, format_changes(job.course_code, changes))
                            sent += 1
                    self.storage.record_job_success(job.id, action="watch", message="watch cycle completed")
                else:
                    sent += await self._process_automation_job(job)
            except Exception as exc:  # keep one bad user/job from stopping the cycle
                message = f"job {job.id}: {exc}"
                logging.exception("watch scheduler failed for %s", message)
                errors.append(message)
                self._record_backoff(job, str(exc))

        return SchedulerCycleResult(checked, sent, errors)

    def _user_for_job(self, job: JobRecord):
        for user in self.storage.list_users():
            if user.id == job.user_id and user.is_active:
                return user
        return None

    async def _process_automation_job(self, job: JobRecord) -> int:
        if self.inspect_automation is None:
            return 0
        candidate = await self.inspect_automation(job)
        alert_snapshot_key = f"job:{job.id}:automation-alert"
        if candidate is None:
            self.storage.delete_snapshot(alert_snapshot_key)
            return 0
        user = self._user_for_job(job)
        if user is None:
            return 0

        if job.mode == JOB_MODE_AUTO:
            if self.execute_automation is None:
                return 0
            result = await self.execute_automation(job)
            self.storage.complete_job(job.id)
            self.storage.record_job_success(job.id, action=candidate.action, message=result)
            self.storage.delete_snapshot(alert_snapshot_key)
            self.storage.clear_pending_action(job.id)
            await self.send_message(user.telegram_id, result)
            return 1

        if job.mode == JOB_MODE_CONFIRM:
            pending = self.storage.get_pending_action(job.id)
            if pending and pending.target_section == candidate.target_section_name:
                return 0
            self.storage.set_pending_action(
                job_id=job.id,
                user_id=user.id,
                action_type=candidate.action,
                target_section=candidate.target_section_name,
                details={"dedupe_key": candidate.dedupe_key, **candidate.details},
            )
            await self.send_message(
                user.telegram_id,
                self._format_confirmation_prompt(job, candidate),
            )
            self.storage.record_job_success(job.id, action="confirm_pending", message=f"awaiting confirmation for {candidate.target_section_name or '-'}")
            return 1

        previous_alert = self.storage.get_snapshot(alert_snapshot_key)
        if previous_alert == candidate.dedupe_key:
            return 0
        self.storage.set_snapshot(alert_snapshot_key, candidate.dedupe_key)
        await self.send_message(user.telegram_id, self._format_notify_message(job, candidate))
        self.storage.record_job_success(job.id, action="notify", message=f"notified for {candidate.target_section_name or '-'}")
        return 1

    @staticmethod
    def _format_notify_message(job: JobRecord, candidate: AutomationCandidate) -> str:
        target = candidate.target_section_name or "unknown section"
        lines = [
            f"{job.course_code} is actionable for {candidate.action.replace('_', ' ')}.\n"
            f"Target: {target}",
            f"Reason: {candidate.reason}",
        ]
        if candidate.details.get("target_schedule"):
            lines.append(f"Schedule: {candidate.details['target_schedule']}")
        if candidate.details.get("fallback_used"):
            lines.append("Selection: used fallback section order")
        skipped = candidate.details.get("skipped_priority_clashes") or []
        if skipped:
            lines.append(f"Skipped priority clashes: {', '.join(skipped)}")
        unavailable = candidate.details.get("unavailable_priority_sections") or []
        if unavailable:
            lines.append(f"Unavailable priorities: {', '.join(unavailable)}")
        return "\n".join(lines)

    @staticmethod
    def _format_confirmation_prompt(job: JobRecord, candidate: AutomationCandidate) -> str:
        target = candidate.target_section_name or "unknown section"
        lines = [
            f"{job.course_code} is ready for {candidate.action.replace('_', ' ')}.",
            f"Target: {target}",
            f"Reason: {candidate.reason}",
        ]
        if candidate.details.get("target_schedule"):
            lines.append(f"Schedule: {candidate.details['target_schedule']}")
        if candidate.details.get("fallback_used"):
            lines.append("Selection: used fallback section order")
        skipped = candidate.details.get("skipped_priority_clashes") or []
        if skipped:
            lines.append(f"Skipped priority clashes: {', '.join(skipped)}")
        lines.append(f"Reply with /confirm {job.id} to execute or /reject {job.id} to clear this request.")
        return "\n".join(lines)

    def _is_backing_off(self, job: JobRecord) -> bool:
        runtime = self.storage.get_job_runtime(job.id)
        if runtime is None or not runtime.next_retry_at:
            return False
        next_retry_at = text_to_dt(runtime.next_retry_at)
        return bool(next_retry_at and next_retry_at > utcnow())

    def _record_backoff(self, job: JobRecord, error: str) -> None:
        runtime = self.storage.get_job_runtime(job.id)
        failure_count = (runtime.failure_count if runtime else 0) + 1
        delay_seconds = min(600, 5 * (2 ** min(failure_count - 1, 6)))
        next_retry_at = utcnow() + timedelta(seconds=delay_seconds)
        self.storage.record_job_failure(
            job.id,
            error=error,
            next_retry_at=dt_to_text(next_retry_at),
        )
