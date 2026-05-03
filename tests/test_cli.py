from __future__ import annotations

import unittest
from unittest.mock import patch

from archershub.cli import parse_args
from archershub.constants import SWITCH_STRATEGY_CHANGE_SECTION


class CliTests(unittest.TestCase):
    def test_verbose_flag_is_accepted(self):
        with patch("sys.argv", ["archershub", "--course-code", "LCFAITH", "--verbose"]):
            args = parse_args()
        self.assertTrue(args.verbose)
        self.assertEqual(args.course_code, "LCFAITH")

    def test_auto_switch_defaults_to_change_section(self):
        with patch("sys.argv", ["archershub"]):
            args = parse_args()
        self.assertEqual(args.switch_strategy, SWITCH_STRATEGY_CHANGE_SECTION)


if __name__ == "__main__":
    unittest.main()
