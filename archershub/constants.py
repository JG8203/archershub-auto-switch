from __future__ import annotations

import re

UA = "Mozilla/5.0"
TIMEOUT = 30
DEFAULT_BASE_URL = "https://archershub.dlsu.edu.ph"
DEFAULT_LOGIN_PATH = "StudentLogin"
DEFAULT_INTERVAL_SECS = 30
DEFAULT_MAX_LOGIN_ATTEMPTS = 5
CHANGE_SECTION_TYPE_ID = "2"
SWITCH_STRATEGY_CHANGE_SECTION = "change-section"
SWITCH_STRATEGY_DROP_ADD = "drop-add"
CAPTCHA_LENGTH = 6
CAPTCHA_RE = re.compile(r"^[A-Z0-9]{6}$")


class AutoSwitchSubmitError(RuntimeError):
    """Raised after an enrollment-changing submit was attempted."""
