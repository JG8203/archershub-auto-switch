from __future__ import annotations

import os
import unittest

from archershub.client import ArchersHubClient, ArchersHubResponseError, UnsafeEndpointError


REQUIRED_ENV = ("ARCHERSHUB_USERNAME", "ARCHERSHUB_PASSWORD")


class LiveReadOnlyEndpointTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        missing = [name for name in REQUIRED_ENV if not os.getenv(name)]
        if missing:
            raise unittest.SkipTest(f"missing env vars: {', '.join(missing)}")
        cls.client = ArchersHubClient.from_env()
        try:
            cls.client.login()
        except Exception as exc:  # captcha/OCR/site availability can make live login impossible.
            raise unittest.SkipTest(f"live ArchersHub login failed: {exc}") from exc

    def assert_endpoint_data(self, endpoint: str, **kwargs):
        try:
            data = self.client.call(endpoint, **kwargs)
        except ArchersHubResponseError:
            raise
        self.assertIsNotNone(data)
        if isinstance(data, str):
            self.assertNotIn("loginform", data.lower())
        return data

    def test_dashboard_read_endpoints(self):
        for endpoint in [
            "StudentDashboard/GetEnrollmentJourneyDashboard",
            "StudentDashboard/StudentDashboardConfiguration",
            "StudentDashboard/GetDataForStudentScheduleForToday",
            "StudentDashboard/GetDataForEnrolledCourseForStudent",
            "StudentDashboard/GetImportantDate",
            "Common/GetStudentPhoto",
        ]:
            with self.subTest(endpoint=endpoint):
                self.assert_endpoint_data(endpoint)

    def test_profile_and_enrollment_read_endpoints(self):
        profile_dropdown = self.assert_endpoint_data("ProfileEnlistment/GetAllDropDown")
        current_session_id = None
        if isinstance(profile_dropdown, list):
            current = next((row for row in profile_dropdown if isinstance(row, dict) and row.get("is_current_session")), None)
            row = current or (profile_dropdown[0] if profile_dropdown and isinstance(profile_dropdown[0], dict) else None)
            current_session_id = row.get("academic_session_id") if row else None

        calls = [
            ("ProfileDetails/GetStudentProfileDropDown", {}),
            ("ProfileDetails/GetStudentPersonalDetails", {"params": {"pagetabid": 1}}),
            ("ProfileDetails/GetStudentAddressDetails", {"params": {"pagetabid": 2}}),
            ("ProfileEnlistment/GetProfileEnlistmentgridList", {"params": {"academicid": current_session_id}}),
            ("Schedule/GetScheduleData", {}),
            ("STDStudentHold/GetDropDown", {}),
        ]
        for endpoint, kwargs in calls:
            with self.subTest(endpoint=endpoint):
                self.assert_endpoint_data(endpoint, **kwargs)

    def test_course_finder_dropdown_is_readable(self):
        data = self.assert_endpoint_data("CourseFinder/GetAllDropDownList")
        self.assertTrue(isinstance(data, (dict, list, str)))

    def test_mutating_endpoint_is_blocked_in_live_default(self):
        with self.assertRaises(UnsafeEndpointError):
            self.client.call("ApplyWithdrawal/DeleteWithdrawalById", params={"applyWithdrawalId": "0"})


if __name__ == "__main__":
    unittest.main()
