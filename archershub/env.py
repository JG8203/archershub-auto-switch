from __future__ import annotations

from pathlib import Path

from dotenv import load_dotenv


def load_project_env() -> None:
    """Load local environment variables for Poetry entrypoints.

    `load_dotenv` is intentionally non-overriding so real shell/systemd
    environment variables keep precedence over values in `.env`.
    """

    load_dotenv(Path.cwd() / ".env", override=False)
