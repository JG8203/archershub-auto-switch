import asyncio
import tempfile
import unittest

from archershub.bot.parsing import parse_addclass_specs
from archershub.bot.service import BotArchersHubService
from archershub.crypto import SecretBox
from archershub.jobs import AutomationBatchResult, AutomationCandidate, PermanentJobError, choose_add_class_section, plan_change_section
from archershub.notifications import compact_sections, diff_sections, filter_sections, has_changes
from archershub.scheduler import WatchScheduler
from archershub.storage import (
    JOB_MODE_AUTO,
    JOB_MODE_CONFIRM,
    JOB_MODE_NOTIFY,
    JOB_TYPE_ADD_CLASS,
    JOB_TYPE_CHANGE_SECTION,
    JOB_TYPE_WATCH,
    SQLiteStorage,
)
from archershub.switching import build_add_course_payload


COURSE_DATA = [
    {"section_name": "C02", "section_creation_id": 2, "capacity": 10, "updated_capacity": 0, "enlisted": 9, "schedule": "T 1", "main_teacher": "B"},
    {"section_name": "C01", "section_creation_id": 1, "capacity": 10, "updated_capacity": 0, "enlisted": 10, "schedule": "M 1", "main_teacher": "A"},
    {"section_name": "C03", "section_creation_id": 3, "capacity": 10, "updated_capacity": 0, "enlisted": 8, "schedule": "W 1", "main_teacher": "C"},
]


class StorageCryptoTests(unittest.TestCase):
    def test_registration_credentials_jobs_and_interval(self):
        with tempfile.TemporaryDirectory() as tmp:
            storage = SQLiteStorage(f"{tmp}/bot.sqlite3")
            code = storage.generate_registration_code(ttl_hours=1)
            user = storage.redeem_registration_code(code, 123, "tester")
            self.assertEqual(user.telegram_id, 123)
            with self.assertRaises(ValueError):
                storage.redeem_registration_code(code, 456)

            box = SecretBox.from_secret("dev-secret")
            encrypted_user = box.encrypt_text("student")
            encrypted_pass = box.encrypt_text("password")
            storage.save_credentials(user.id, encrypted_user, encrypted_pass, box.encrypt_text('{"a":"b"}'))
            creds = storage.get_credentials(user.id)
            self.assertEqual(box.decrypt_text(creds.username_encrypted), "student")
            self.assertEqual(box.decrypt_text(creds.password_encrypted), "password")

            job = storage.add_job(user_id=user.id, job_type=JOB_TYPE_ADD_CLASS, mode=JOB_MODE_NOTIFY, course_code="lcfaith", priority_sections=["C01"])
            self.assertEqual(job.course_code, "LCFAITH")
            self.assertEqual(storage.list_jobs(user_id=user.id)[0].priority_sections, ["C01"])
            storage.set_pending_action(job_id=job.id, user_id=user.id, action_type="add_class", target_section="C02", details={"dedupe_key": "add:2"})
            pending = storage.get_pending_action(job.id)
            self.assertEqual(pending.target_section, "C02")
            self.assertEqual(pending.details["dedupe_key"], "add:2")
            storage.clear_pending_action(job.id)
            self.assertIsNone(storage.get_pending_action(job.id))
            storage.pause_job(job.id)
            self.assertIsNotNone(storage.get_job(job.id).paused_at)
            storage.resume_job(job.id)
            self.assertIsNone(storage.get_job(job.id).paused_at)
            storage.set_interval_secs(45)
            self.assertEqual(storage.get_interval_secs(), 45)

    def test_registration_code_revocation(self):
        with tempfile.TemporaryDirectory() as tmp:
            storage = SQLiteStorage(f"{tmp}/bot.sqlite3")
            unused_code = storage.generate_registration_code(ttl_hours=1)
            revoked = storage.revoke_registration_code(unused_code, reason="test revoke")
            self.assertIsNotNone(revoked.revoked_at)
            self.assertEqual(revoked.revoked_reason, "test revoke")
            with self.assertRaisesRegex(ValueError, "revoked"):
                storage.redeem_registration_code(unused_code, 456, "tester")

            used_code = storage.generate_registration_code(ttl_hours=1)
            user = storage.redeem_registration_code(used_code, 789, "tester")
            self.assertTrue(user.is_active)
            storage.revoke_registration_code(used_code, reason="left")
            self.assertFalse(storage.get_user_by_telegram_id(789).is_active)

            with self.assertRaisesRegex(ValueError, "not found"):
                storage.revoke_registration_code("missing")

            listed = {row.code: row for row in storage.list_registration_codes()}
            self.assertEqual(listed[unused_code].revoked_reason, "test revoke")
            self.assertEqual(listed[used_code].used_by_telegram_id, 789)

    def test_reactivate_user_by_id_or_telegram_id(self):
        with tempfile.TemporaryDirectory() as tmp:
            storage = SQLiteStorage(f"{tmp}/bot.sqlite3")
            code = storage.generate_registration_code(ttl_hours=1)
            user = storage.redeem_registration_code(code, 123456, "tester")
            storage.revoke_registration_code(code, reason="temporary")
            self.assertFalse(storage.get_user_by_telegram_id(123456).is_active)

            reactivated = storage.reactivate_user(user.id)
            self.assertTrue(reactivated.is_active)
            storage.revoke_registration_code(code, reason="temporary")
            reactivated = storage.reactivate_user(123456)
            self.assertTrue(reactivated.is_active)


class NotificationDiffTests(unittest.TestCase):
    def test_filter_and_diff_availability_changes(self):
        previous = compact_sections(filter_sections(COURSE_DATA, ["C01", "C02"]))
        updated_data = [dict(row) for row in COURSE_DATA]
        updated_data[0]["enlisted"] = 10
        updated_data[1]["enlisted"] = 9
        current = compact_sections(filter_sections(updated_data, ["C01", "C02"]))
        changes = diff_sections(previous, current)
        self.assertTrue(has_changes(changes))
        self.assertEqual(len(changes["availability"]), 2)


class JobPlanningTests(unittest.TestCase):
    def test_addclass_parser_supports_multiple_course_specs(self):
        specs = parse_addclass_specs(["LCFAITH:Z18,Z19", "GETEAMS:S11", "notify"])
        self.assertEqual(specs, [("LCFAITH", ["Z18", "Z19"]), ("GETEAMS", ["S11"])])

    def test_addclass_parser_keeps_legacy_single_course_priority_form(self):
        specs = parse_addclass_specs(["LCFAITH", "Z18", "Z19", "auto"])
        self.assertEqual(specs, [("LCFAITH", ["Z18", "Z19"])])

    def test_add_class_priority_clash_then_section_name_fallback(self):
        decision = choose_add_class_section(
            COURSE_DATA,
            priority_sections=["C03"],
            clashes=lambda section: section["section_name"] == "C03",
        )
        self.assertEqual(decision.selected_section["section_name"], "C02")
        self.assertTrue(decision.fallback_used)
        self.assertEqual([row["section_name"] for row in decision.skipped_priority_clashes], ["C03"])

    def test_add_class_never_selects_clashing_sections(self):
        decision = choose_add_class_section(COURSE_DATA, clashes=lambda section: True)
        self.assertIsNone(decision.selected_section)

    def test_change_section_plan_is_open_target_only(self):
        decision = plan_change_section(COURSE_DATA, current_section_id="1", target_section_name="C02")
        self.assertTrue(decision.should_submit)
        self.assertEqual(decision.target_section["section_creation_id"], 2)
        same = plan_change_section(COURSE_DATA, current_section_id="2", target_section_name="C02")
        self.assertFalse(same.should_submit)

    def test_build_add_course_payload(self):
        state = {
            "academic_session_id": 135,
            "student_id": 37777,
            "get_enlisted_subject": [
                {"credits": 3, "is_exclude": 0},
                {"credits": 3, "is_exclude": 0},
            ],
            "max_credit": 20,
            "max_credit_can_enroll": 0,
            "is_approval": 0,
            "is_student_confirmation": 0,
        }
        add_course = {
            "course_creation_id": 1924,
            "enrollment_semester_id": 43,
            "regular_restudy": 0,
            "curriculum_creation_id": 485,
            "course_category_id": 1,
            "credits": 3,
            "is_exclude": 0,
            "is_mandatory": 0,
            "pre_requisite_status": 0,
        }
        payload = build_add_course_payload(
            state=state,
            add_course=add_course,
            target_section={"section_creation_id": 900},
            add_reason_id="5",
        )
        self.assertEqual(payload["COMMAND_TYPE"], "INSERT_UPDATE_STUDENT_ADD_DROP")
        self.assertEqual(payload["IS_ADD_REASON_ID"], "5")
        self.assertEqual(payload["IS_DROP_REASON_ID"], "0")
        self.assertEqual(payload["UNIT"], "9")
        self.assertEqual(payload["CourseSelectionList"][0]["SECTION_CREATION_ID"], "900")

    def test_add_class_already_enlisted_with_priority_converts_to_change(self):
        with tempfile.TemporaryDirectory() as tmp:
            storage = SQLiteStorage(f"{tmp}/bot.sqlite3")
            user = storage.redeem_registration_code(storage.generate_registration_code(), 123)
            job = storage.add_job(user_id=user.id, job_type=JOB_TYPE_ADD_CLASS, mode=JOB_MODE_AUTO, course_code="LCFAITH", priority_sections=["C02"])
            service = BotArchersHubService(storage, SecretBox.from_secret("dev-secret"))
            target = {"course_code": "LCFAITH", "course_creation_id": "123", "academic_session_id": "1", "campus_id": "1"}
            add_state = {"bind_section": [{"course_creation_id": "123", "section_creation_id": "1"}]}

            from unittest.mock import patch

            with patch.object(service, "_load_course_bundle", return_value=(object(), target, COURSE_DATA)), patch("archershub.bot.service.get_add_drop_state", return_value=add_state):
                candidate = service._inspect_automation_job_sync(job)

            self.assertEqual(candidate.action, "converted_to_change")
            updated = storage.get_job(job.id)
            self.assertEqual(updated.job_type, JOB_TYPE_CHANGE_SECTION)
            self.assertEqual(updated.target_section, "C02")

    def test_add_class_not_add_eligible_warns_without_disabling(self):
        with tempfile.TemporaryDirectory() as tmp:
            storage = SQLiteStorage(f"{tmp}/bot.sqlite3")
            user = storage.redeem_registration_code(storage.generate_registration_code(), 123)
            job = storage.add_job(user_id=user.id, job_type=JOB_TYPE_ADD_CLASS, mode=JOB_MODE_AUTO, course_code="LCFAITH", priority_sections=["C02"])
            service = BotArchersHubService(storage, SecretBox.from_secret("dev-secret"))
            target = {"course_code": "LCFAITH", "course_creation_id": "123", "academic_session_id": "1", "campus_id": "1"}
            add_state = {"bind_section": [], "add_offer_course_list": []}

            from unittest.mock import patch

            with patch.object(service, "_load_course_bundle", return_value=(object(), target, COURSE_DATA)), patch("archershub.bot.service.get_add_drop_state", return_value=add_state):
                candidate = service._inspect_automation_job_sync(job)

            self.assertEqual(candidate.action, "add_not_eligible")
            self.assertTrue(candidate.details["warning_only"])
            self.assertTrue(storage.get_job(job.id).enabled)

    def test_change_section_not_eligible_warns_without_disabling(self):
        with tempfile.TemporaryDirectory() as tmp:
            storage = SQLiteStorage(f"{tmp}/bot.sqlite3")
            user = storage.redeem_registration_code(storage.generate_registration_code(), 123)
            job = storage.add_job(user_id=user.id, job_type=JOB_TYPE_CHANGE_SECTION, mode=JOB_MODE_AUTO, course_code="LCFAITH", target_section="C02")
            service = BotArchersHubService(storage, SecretBox.from_secret("dev-secret"))
            target = {"course_code": "LCFAITH", "course_creation_id": "123", "academic_session_id": "1", "campus_id": "1"}

            from unittest.mock import patch

            with patch.object(service, "_load_course_bundle", return_value=(object(), target, COURSE_DATA)), patch("archershub.bot.service.get_change_section_state", return_value={"bind_section": []}):
                candidate = service._inspect_automation_job_sync(job)

            self.assertEqual(candidate.action, "change_not_eligible")
            self.assertTrue(candidate.details["warning_only"])
            self.assertTrue(storage.get_job(job.id).enabled)


class SchedulerTests(unittest.TestCase):
    def test_scheduler_watch_jobs_notify_when_slots_open(self):
        async def scenario():
            with tempfile.TemporaryDirectory() as tmp:
                storage = SQLiteStorage(f"{tmp}/bot.sqlite3")
                user = storage.redeem_registration_code(storage.generate_registration_code(), 999)
                storage.add_job(user_id=user.id, job_type=JOB_TYPE_WATCH, mode=JOB_MODE_NOTIFY, course_code="ABC", section_filters=["C01"])
                sent = []
                calls = []
                data = [dict(row) for row in COURSE_DATA]

                async def fetch(job):
                    calls.append(job.id)
                    return data

                async def send(chat_id, text):
                    sent.append((chat_id, text))

                scheduler = WatchScheduler(storage, fetch, send)
                first = await scheduler.run_once()
                self.assertEqual(first.checked_jobs, 1)
                self.assertEqual(sent, [])
                data[1] = dict(data[1], enlisted=9)
                second = await scheduler.run_once()
                self.assertEqual(second.notifications_sent, 1)
                self.assertEqual(sent[0][0], 999)
                self.assertIn("C01", sent[0][1])
                self.assertEqual(calls, [1, 1])

        asyncio.run(scenario())

    def test_scheduler_handles_confirm_and_auto_automation_jobs(self):
        async def scenario():
            with tempfile.TemporaryDirectory() as tmp:
                storage = SQLiteStorage(f"{tmp}/bot.sqlite3")
                user = storage.redeem_registration_code(storage.generate_registration_code(), 111)
                confirm_job = storage.add_job(
                    user_id=user.id,
                    job_type=JOB_TYPE_ADD_CLASS,
                    mode=JOB_MODE_CONFIRM,
                    course_code="ABC",
                    priority_sections=["C02"],
                )
                auto_job = storage.add_job(
                    user_id=user.id,
                    job_type=JOB_TYPE_ADD_CLASS,
                    mode=JOB_MODE_AUTO,
                    course_code="DEF",
                    priority_sections=["C03"],
                )
                sent = []
                executed = []

                async def fetch(_job):
                    return COURSE_DATA

                async def send(chat_id, text):
                    sent.append((chat_id, text))

                async def inspect(job):
                    target = "C02" if job.id == confirm_job.id else "C03"
                    return AutomationCandidate(
                        job_type=job.job_type,
                        course_code=job.course_code,
                        action="add_class",
                        reason="selected section",
                        target_section_name=target,
                        dedupe_key=f"add:{target}",
                        details={},
                    )

                async def execute(job):
                    executed.append(job.id)
                    return f"submitted {job.course_code}"

                scheduler = WatchScheduler(
                    storage,
                    fetch,
                    send,
                    inspect_automation=inspect,
                    execute_automation=execute,
                )
                result = await scheduler.run_once()
                self.assertEqual(result.notifications_sent, 2)
                self.assertEqual(executed, [auto_job.id])
                self.assertIsNotNone(storage.get_pending_action(confirm_job.id))
                self.assertIsNone(storage.get_pending_action(auto_job.id))
                self.assertIsNotNone(storage.get_job(confirm_job.id))
                self.assertIsNotNone(storage.get_job(auto_job.id).completed_at)
                self.assertEqual(len(sent), 2)

        asyncio.run(scenario())

    def test_scheduler_batches_auto_addclass_jobs_when_batch_callback_exists(self):
        async def scenario():
            with tempfile.TemporaryDirectory() as tmp:
                storage = SQLiteStorage(f"{tmp}/bot.sqlite3")
                user = storage.redeem_registration_code(storage.generate_registration_code(), 112)
                job_one = storage.add_job(
                    user_id=user.id,
                    job_type=JOB_TYPE_ADD_CLASS,
                    mode=JOB_MODE_AUTO,
                    course_code="ABC",
                    priority_sections=["C02"],
                )
                job_two = storage.add_job(
                    user_id=user.id,
                    job_type=JOB_TYPE_ADD_CLASS,
                    mode=JOB_MODE_AUTO,
                    course_code="DEF",
                    priority_sections=["C03"],
                )
                sent = []
                individual_executed = []
                batch_calls = []

                async def fetch(_job):
                    return COURSE_DATA

                async def send(chat_id, text):
                    sent.append((chat_id, text))

                async def inspect(job):
                    target = "C02" if job.id == job_one.id else "C03"
                    return AutomationCandidate(
                        job_type=job.job_type,
                        course_code=job.course_code,
                        action="add_class",
                        reason="selected section",
                        target_section_name=target,
                        dedupe_key=f"add:{target}",
                        details={"academic_session_id": "1"},
                    )

                async def execute(job):
                    individual_executed.append(job.id)
                    return "should not run"

                async def execute_batch(jobs):
                    batch_calls.append([job.id for job in jobs])
                    return AutomationBatchResult(
                        submitted_job_ids=[job.id for job in jobs],
                        message="submitted add/drop batch",
                    )

                scheduler = WatchScheduler(
                    storage,
                    fetch,
                    send,
                    inspect_automation=inspect,
                    execute_automation=execute,
                    execute_automation_batch=execute_batch,
                )
                result = await scheduler.run_once()

                self.assertEqual(result.checked_jobs, 2)
                self.assertEqual(result.notifications_sent, 1)
                self.assertEqual(batch_calls, [[job_one.id, job_two.id]])
                self.assertEqual(individual_executed, [])
                self.assertIn("submitted add/drop batch", sent[0][1])
                self.assertIsNotNone(storage.get_job(job_one.id).completed_at)
                self.assertIsNotNone(storage.get_job(job_two.id).completed_at)

        asyncio.run(scenario())

    def test_scheduler_notify_mode_dedupes_same_candidate(self):
        async def scenario():
            with tempfile.TemporaryDirectory() as tmp:
                storage = SQLiteStorage(f"{tmp}/bot.sqlite3")
                user = storage.redeem_registration_code(storage.generate_registration_code(), 222)
                storage.add_job(
                    user_id=user.id,
                    job_type=JOB_TYPE_ADD_CLASS,
                    mode=JOB_MODE_NOTIFY,
                    course_code="ABC",
                    priority_sections=["C02"],
                )
                sent = []

                async def fetch(_job):
                    return COURSE_DATA

                async def send(chat_id, text):
                    sent.append((chat_id, text))

                async def inspect(_job):
                    return AutomationCandidate(
                        job_type=JOB_TYPE_ADD_CLASS,
                        course_code="ABC",
                        action="add_class",
                        reason="selected section",
                        target_section_name="C02",
                        dedupe_key="add:2",
                        details={},
                    )

                scheduler = WatchScheduler(storage, fetch, send, inspect_automation=inspect)
                await scheduler.run_once()
                await scheduler.run_once()
                self.assertEqual(len(sent), 1)

        asyncio.run(scenario())

    def test_scheduler_warning_only_candidates_are_sent_once_and_not_executed(self):
        async def scenario():
            with tempfile.TemporaryDirectory() as tmp:
                storage = SQLiteStorage(f"{tmp}/bot.sqlite3")
                user = storage.redeem_registration_code(storage.generate_registration_code(), 224)
                storage.add_job(user_id=user.id, job_type=JOB_TYPE_ADD_CLASS, mode=JOB_MODE_AUTO, course_code="ABC")
                sent = []
                executed = []

                async def fetch(_job):
                    return COURSE_DATA

                async def send(chat_id, text):
                    sent.append((chat_id, text))

                async def inspect(_job):
                    return AutomationCandidate(
                        job_type=JOB_TYPE_ADD_CLASS,
                        course_code="ABC",
                        action="add_not_eligible",
                        reason="ABC is not add-eligible yet",
                        target_section_name=None,
                        dedupe_key="warn:add:1",
                        details={"warning_only": True},
                    )

                async def execute(job):
                    executed.append(job.id)
                    return "should not run"

                scheduler = WatchScheduler(storage, fetch, send, inspect_automation=inspect, execute_automation=execute)
                first = await scheduler.run_once()
                second = await scheduler.run_once()
                self.assertEqual(first.notifications_sent, 1)
                self.assertEqual(second.notifications_sent, 0)
                self.assertEqual(executed, [])
                self.assertIn("not add-eligible", sent[0][1])

        asyncio.run(scenario())

    def test_scheduler_backoff_skips_immediate_retry_after_failure(self):
        async def scenario():
            with tempfile.TemporaryDirectory() as tmp:
                storage = SQLiteStorage(f"{tmp}/bot.sqlite3")
                user = storage.redeem_registration_code(storage.generate_registration_code(), 333)
                job = storage.add_job(
                    user_id=user.id,
                    job_type=JOB_TYPE_ADD_CLASS,
                    mode=JOB_MODE_NOTIFY,
                    course_code="ABC",
                )
                calls = []

                async def fetch(_job):
                    return COURSE_DATA

                async def send(_chat_id, _text):
                    pass

                async def inspect(_job):
                    calls.append(job.id)
                    raise RuntimeError("temporary failure")

                scheduler = WatchScheduler(storage, fetch, send, inspect_automation=inspect)
                first = await scheduler.run_once()
                second = await scheduler.run_once()
                self.assertEqual(first.checked_jobs, 1)
                self.assertEqual(second.checked_jobs, 0)
                runtime = storage.get_job_runtime(job.id)
                self.assertEqual(runtime.failure_count, 1)
                self.assertIsNotNone(runtime.next_retry_at)
                self.assertEqual(calls, [job.id])

        asyncio.run(scenario())

    def test_scheduler_stops_permanent_job_errors_and_notifies_user(self):
        async def scenario():
            with tempfile.TemporaryDirectory() as tmp:
                storage = SQLiteStorage(f"{tmp}/bot.sqlite3")
                user = storage.redeem_registration_code(storage.generate_registration_code(), 444)
                job = storage.add_job(
                    user_id=user.id,
                    job_type=JOB_TYPE_ADD_CLASS,
                    mode=JOB_MODE_AUTO,
                    course_code="LCFAITH",
                )
                sent = []

                async def fetch(_job):
                    return COURSE_DATA

                async def send(chat_id, text):
                    sent.append((chat_id, text))

                async def inspect(_job):
                    raise PermanentJobError("LCFAITH is not add-eligible")

                scheduler = WatchScheduler(storage, fetch, send, inspect_automation=inspect)
                result = await scheduler.run_once()
                self.assertEqual(result.checked_jobs, 1)
                self.assertEqual(result.notifications_sent, 1)
                self.assertIn("not add-eligible", sent[0][1])
                self.assertIsNotNone(storage.get_job(job.id).completed_at)
                runtime = storage.get_job_runtime(job.id)
                self.assertEqual(runtime.failure_count, 0)

        asyncio.run(scenario())


if __name__ == "__main__":
    unittest.main()
