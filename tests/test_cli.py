from __future__ import annotations

import unittest
from unittest.mock import patch

from archershub.cli import parse_args


class CliTests(unittest.TestCase):
    def test_verbose_flag_is_accepted(self):
        with patch("sys.argv", ["archershub", "--course-code", "LCFAITH", "--verbose"]):
            args = parse_args()
        self.assertTrue(args.verbose)
        self.assertEqual(args.course_code, "LCFAITH")


if __name__ == "__main__":
    unittest.main()
