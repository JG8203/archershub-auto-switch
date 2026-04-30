import unittest

import login_requests as lr


class AutoSwitchHelperTests(unittest.TestCase):
    def test_section_open_uses_updated_capacity_when_present(self):
        section = {"capacity": 45, "updated_capacity": 50, "enlisted": 45}
        self.assertEqual(lr.effective_capacity(section), 50)
        self.assertEqual(lr.available_slots(section), 5)
        self.assertTrue(lr.is_section_open(section))

    def test_section_open_falls_back_to_capacity(self):
        section = {"capacity": "45", "updated_capacity": 0, "enlisted": "45"}
        self.assertFalse(lr.is_section_open(section))

    def test_find_target_section_matches_normalized_name(self):
        data = [
            {"section_name": "C01", "section_creation_id": 1},
            {"section_name": " y03 ", "section_creation_id": 2},
        ]
        self.assertEqual(lr.find_target_section(data, "Y03")["section_creation_id"], 2)

    def test_resolve_change_reason_matches_text(self):
        data = {
            "change_section_rule": [{"is_mandatory": 1}],
            "reason_drp": [
                {"acd_add_drop_reason_id": 10, "add_drop_id": 2, "reason": "Drop"},
                {"acd_add_drop_reason_id": 11, "add_drop_id": 3, "reason": "Schedule conflict"},
            ],
        }
        self.assertEqual(lr.resolve_change_reason(data, reason_text="schedule"), "11")

    def test_resolve_change_reason_uses_first_required_reason(self):
        data = {
            "change_section_rule": [{"is_mandatory": 1}],
            "reason_drp": [
                {"acd_add_drop_reason_id": 11, "add_drop_id": 3, "reason": "First"},
                {"acd_add_drop_reason_id": 12, "add_drop_id": 3, "reason": "Second"},
            ],
        }
        self.assertEqual(lr.resolve_change_reason(data), "11")

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
        payload = lr.build_change_section_payload(
            state=state,
            current_course=current,
            target_section=target,
            reason_id="11",
        )
        self.assertEqual(payload["TYPE_ID"], lr.CHANGE_SECTION_TYPE_ID)
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
        payload = lr.build_drop_add_payload(
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

    def test_resolve_add_drop_reason(self):
        data = {
            "reason_drp": [
                {"acd_add_drop_reason_id": 3, "add_drop_id": 2, "reason": "Change of Schedule"},
                {"acd_add_drop_reason_id": 5, "add_drop_id": 1, "reason": "Want to Study Course"},
            ]
        }
        self.assertEqual(lr.resolve_add_drop_reason(data, "2", reason_text="schedule"), "3")
        self.assertEqual(lr.resolve_add_drop_reason(data, "1", reason_text="study"), "5")


if __name__ == "__main__":
    unittest.main()
