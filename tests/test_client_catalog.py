import unittest
from unittest.mock import patch

from archershub.catalog import ENDPOINTS, get_endpoint
from archershub.client import ArchersHubClient, UnsafeEndpointError


class ClientCatalogTests(unittest.TestCase):
    def test_catalog_contains_mirror_discovered_endpoints(self):
        self.assertGreaterEqual(len(ENDPOINTS), 200)
        self.assertEqual(get_endpoint("StudentDashboard/GetImportantDate").controller, "StudentDashboard")
        self.assertEqual(get_endpoint("/ProfileDetails/GetStudentPersonalDetails").action, "GetStudentPersonalDetails")

    def test_catalog_marks_known_dangerous_endpoint(self):
        self.assertEqual(get_endpoint("ApplyWithdrawal/DeleteWithdrawalById").safety, "mutation")
        self.assertEqual(get_endpoint("MyPayments/SaveStudentPaymentFromWallet").safety, "mutation")

    def test_mutation_endpoint_requires_opt_in_and_exact_confirmation(self):
        client = ArchersHubClient(session=object(), allow_mutation=False)
        with self.assertRaises(UnsafeEndpointError):
            client.call("ApplyWithdrawal/DeleteWithdrawalById", data={"applyWithdrawalId": "1"})

        client = ArchersHubClient(session=object(), allow_mutation=True)
        with self.assertRaises(UnsafeEndpointError):
            client.call("ApplyWithdrawal/DeleteWithdrawalById", data={"applyWithdrawalId": "1"}, confirm_mutation="wrong")

    def test_from_env_reads_login_retry_options(self):
        env = {
            "ARCHERSHUB_USERNAME": "student",
            "ARCHERSHUB_PASSWORD": "secret",
            "ARCHERSHUB_MAX_LOGIN_ATTEMPTS": "7",
            "ARCHERSHUB_NO_CAPTCHA_OCR": "1",
            "ARCHERSHUB_SAVE_LOGIN_ARTIFACTS": "1",
        }
        with patch.dict("os.environ", env, clear=True):
            client = ArchersHubClient.from_env()

        self.assertEqual(client.username, "student")
        self.assertEqual(client.password, "secret")
        self.assertEqual(client.max_login_attempts, 7)
        self.assertFalse(client.captcha_ocr)
        self.assertTrue(client.save_login_artifacts)


if __name__ == "__main__":
    unittest.main()
