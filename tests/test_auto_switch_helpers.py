import unittest
from unittest.mock import patch

import requests

from archershub.auth import looks_like_captcha, sanitize_captcha_text
from archershub.constants import AutoSwitchSubmitError, CHANGE_SECTION_TYPE_ID
from archershub.sections import (
    available_slots,
    effective_capacity,
    find_target_section,
    is_section_open,
)
from archershub.switching import (
    build_add_courses_payload,
    build_change_section_payload,
    build_drop_add_payload,
    maybe_submit_drop_add_switch,
    resolve_add_drop_reason,
    resolve_change_reason,
)


class AutoSwitchHelperTests(unittest.TestCase):
    def test_sanitize_captcha_text(self):
        self.assertEqual(sanitize_captcha_text(" ahm-vbm\n"), "AHMVBM")
        self.assertTrue(looks_like_captcha("AHMVBM"))
        self.assertFalse(looks_like_captcha("AHMVB"))

    def test_section_open_uses_updated_capacity_when_present(self):
        section = {"capacity": 45, "updated_capacity": 50, "enlisted": 45}
        self.assertEqual(effective_capacity(section), 50)
        self.assertEqual(available_slots(section), 5)
        self.assertTrue(is_section_open(section))

    def test_section_open_falls_back_to_capacity(self):
        section = {"capacity": "45", "updated_capacity": 0, "enlisted": "45"}
        self.assertFalse(is_section_open(section))

    def test_find_target_section_matches_normalized_name(self):
        data = [
            {"section_name": "C01", "section_creation_id": 1},
            {"section_name": " y03 ", "section_creation_id": 2},
        ]
        self.assertEqual(find_target_section(data, "Y03")["section_creation_id"], 2)

    def test_resolve_change_reason_matches_text(self):
        data = {
            "change_section_rule": [{"is_mandatory": 1}],
            "reason_drp": [
                {"acd_add_drop_reason_id": 10, "add_drop_id": 2, "reason": "Drop"},
                {"acd_add_drop_reason_id": 11, "add_drop_id": 3, "reason": "Schedule conflict"},
            ],
        }
        self.assertEqual(resolve_change_reason(data, reason_text="schedule"), "11")

    def test_resolve_change_reason_uses_first_required_reason(self):
        data = {
            "change_section_rule": [{"is_mandatory": 1}],
            "reason_drp": [
                {"acd_add_drop_reason_id": 11, "add_drop_id": 3, "reason": "First"},
                {"acd_add_drop_reason_id": 12, "add_drop_id": 3, "reason": "Second"},
            ],
        }
        self.assertEqual(resolve_change_reason(data), "11")

    def test_build_change_section_payload(self):
        state = {"change_section_rule": [{"is_approval_applicable": 0, "is_add_fee": 0, "add_drop_rule_id": 77}]}
        current = {
            "academic_session_id": 15,
            "section_creation_id": 100,
            "course_creation_id": 230,
            "enrollment_semester_id": 4,
            "curriculum_creation_id": 5,
            "parent_campus_no": 1,
            "demandpg_id": 0,
        }
        target = {"section_creation_id": 200}
        payload = build_change_section_payload(
            state=state,
            current_course=current,
            target_section=target,
            reason_id="11",
        )
        self.assertEqual(payload["TYPE_ID"], CHANGE_SECTION_TYPE_ID)
        self.assertEqual(payload["REASON_ID"], "11")
        self.assertEqual(payload["ADD_DROP_RULE_ID"], "77")
        self.assertEqual(
            payload["SaveSectionDetails"],
            [
                {
                    "OLD_SECTION_CREATION_ID": "100",
                    "NEW_SECTION_CREATION_ID": "200",
                    "COURSE_CREATION_ID": "230",
                    "ENROLLMENT_SEMESTER_ID": "4",
                    "CURRICULUM_CREATION_ID": "5",
                }
            ],
        )

    def test_build_drop_add_payload(self):
        state = {
            "academic_session_id": 135,
            "student_id": 37777,
            "get_enlisted_subject": [
                {"credits": 3, "is_exclude": 0},
                {"credits": 3, "is_exclude": 0},
            ],
            "min_credit": 0,
            "max_credit": 20,
            "max_credit_can_enroll": 0,
            "is_approval": 0,
            "is_student_confirmation": 0,
        }
        drop_course = {
            "course_creation_id": 1924,
            "section_creation_id": 671,
            "enrollment_semester_id": 43,
            "regular_restudy": 0,
            "curriculum_creation_id": 485,
            "course_category_id": 1,
            "credits": 3,
            "is_exclude": 0,
            "is_mandatory": 0,
        }
        payload = build_drop_add_payload(
            state=state,
            drop_course=drop_course,
            target_section={"section_creation_id": 900},
            add_reason_id="5",
            drop_reason_id="3",
        )
        self.assertEqual(payload["COMMAND_TYPE"], "INSERT_UPDATE_STUDENT_ADD_DROP")
        self.assertEqual(payload["IS_ADD_REASON_ID"], "5")
        self.assertEqual(payload["IS_DROP_REASON_ID"], "3")
        self.assertEqual(payload["UNIT"], "6")
        self.assertEqual(
            [(row["SECTION_CREATION_ID"], row["ACTIVE"]) for row in payload["CourseSelectionList"]],
            [("671", 2), ("900", 1)],
        )

    def test_build_add_courses_payload_batches_multiple_adds(self):
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
        base_add_course = {
            "enrollment_semester_id": 43,
            "regular_restudy": 0,
            "curriculum_creation_id": 485,
            "course_category_id": 1,
            "credits": 3,
            "is_exclude": 0,
            "is_mandatory": 0,
            "pre_requisite_status": 0,
        }
        payload = build_add_courses_payload(
            state=state,
            additions=[
                ({**base_add_course, "course_creation_id": 1924}, {"section_creation_id": 900}),
                ({**base_add_course, "course_creation_id": 1925}, {"section_creation_id": 901}),
            ],
            add_reason_id="5",
        )
        self.assertEqual(payload["COMMAND_TYPE"], "INSERT_UPDATE_STUDENT_ADD_DROP")
        self.assertEqual(payload["IS_ADD_REASON_ID"], "5")
        self.assertEqual(payload["IS_DROP_REASON_ID"], "0")
        self.assertEqual(payload["UNIT"], "12")
        self.assertEqual(
            [row["SECTION_CREATION_ID"] for row in payload["CourseSelectionList"]],
            ["900", "901"],
        )
        self.assertEqual(len(payload["EnlistmentAdditionalDetails"]), 2)

    def test_drop_add_submit_is_not_retried_after_timeout(self):
        state = {
            "student_id": 37777,
            "campusno": "1",
            "drop_registered_course_list": [
                {
                    "course_creation_id": "1924",
                    "section_creation_id": "671",
                    "enrollment_semester_id": "43",
                    "regular_restudy": "0",
                    "curriculum_creation_id": "485",
                    "course_category_id": "1",
                    "credits": "3",
                    "is_exclude": "0",
                    "is_mandatory": "0",
                }
            ],
            "get_enlisted_subject": [{"course_creation_id": "1924", "section_creation_id": "671", "credits": 3, "is_exclude": 0}],
            "min_credit": 0,
            "max_credit": 20,
            "max_credit_can_enroll": 0,
            "is_approval": 0,
            "is_student_confirmation": 0,
            "is_mandatory": 0,
        }
        target = {
            "course_code": "LCFAITH",
            "course_creation_id": "1924",
            "academic_session_id": "135",
            "campus_id": "1",
            "is_cross_offer": "0",
            "grid_type": "0",
        }
        with patch("archershub.switching.get_add_drop_state", return_value=state), \
            patch("archershub.switching.get_course_wise_section_data", return_value={"section_details": [{"section_creation_id": "900"}]}), \
            patch("archershub.switching.post_form_json", return_value=[{"status": "1"}]), \
            patch("archershub.switching.drop_add_switch_reflected", return_value=False), \
            patch("archershub.switching.time.sleep"), \
            patch("archershub.switching.submit_add_drop", side_effect=requests.exceptions.Timeout("slow")) as submit:
            with self.assertRaises(AutoSwitchSubmitError):
                maybe_submit_drop_add_switch(
                    object(),
                    "https://example.test",
                    target,
                    "C02",
                    {"section_creation_id": "900"},
                )
        self.assertEqual(submit.call_count, 1)

    def test_resolve_add_drop_reason(self):
        data = {
            "reason_drp": [
                {"acd_add_drop_reason_id": 3, "add_drop_id": 2, "reason": "Change of Schedule"},
                {"acd_add_drop_reason_id": 5, "add_drop_id": 1, "reason": "Want to Study Course"},
            ]
        }
        self.assertEqual(resolve_add_drop_reason(data, "2", reason_text="schedule"), "3")
        self.assertEqual(resolve_add_drop_reason(data, "1", reason_text="study"), "5")

    def test_find_add_course_returns_first_match_on_multiple(self):
        from archershub.switching import find_add_course
        state = {
            "add_course_list": [
                {"course_code": "DSILYTC", "course_creation_id": "1", "text": "Match 1"},
                {"course_code": "DSILYTC", "course_creation_id": "2", "text": "Match 2"},
            ]
        }
        match = find_add_course(state, "DSILYTC", "non-existent")
        self.assertEqual(match["course_creation_id"], "1")
        self.assertEqual(match["text"], "Match 1")

    def test_find_current_enlisted_course_returns_first_match_on_multiple(self):
        from archershub.switching import find_current_enlisted_course
        state = {
            "bind_section": [
                {"course_code": "DSILYTC", "course_creation_id": "1", "text": "Enlisted 1"},
                {"course_code": "DSILYTC", "course_creation_id": "2", "text": "Enlisted 2"},
            ]
        }
        match = find_current_enlisted_course(state, "DSILYTC", "non-existent")
        self.assertEqual(match["course_creation_id"], "1")
        self.assertEqual(match["text"], "Enlisted 1")


if __name__ == "__main__":
    unittest.main()
