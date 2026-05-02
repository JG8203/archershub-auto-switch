from __future__ import annotations

import json
import secrets
import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterator

DEFAULT_INTERVAL_SECS = 30
DEFAULT_ADD_CONFLICT_POLICY = "never_displace"
JOB_TYPE_WATCH = "watch"
JOB_TYPE_CHANGE_SECTION = "change_section"
JOB_TYPE_ADD_CLASS = "add_class"
JOB_MODE_NOTIFY = "notify"
JOB_MODE_CONFIRM = "confirm"
JOB_MODE_AUTO = "auto"


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def dt_to_text(value: datetime | None) -> str | None:
    return value.isoformat() if value else None


def text_to_dt(value: str | None) -> datetime | None:
    return datetime.fromisoformat(value) if value else None


@dataclass(frozen=True)
class UserRecord:
    id: int
    telegram_id: int
    username: str | None
    registered_at: str
    default_add_conflict_policy: str = DEFAULT_ADD_CONFLICT_POLICY
    is_active: bool = True


@dataclass(frozen=True)
class RegistrationCode:
    code: str
    expires_at: str | None
    used_at: str | None
    used_by_telegram_id: int | None


@dataclass(frozen=True)
class CredentialRecord:
    user_id: int
    username_encrypted: str
    password_encrypted: str
    cookies_encrypted: str | None
    updated_at: str


@dataclass(frozen=True)
class JobRecord:
    id: int
    user_id: int
    job_type: str
    mode: str
    course_code: str
    section_filters: list[str]
    priority_sections: list[str]
    target_section: str | None
    enabled: bool
    paused_at: str | None
    completed_at: str | None
    created_at: str
    updated_at: str


@dataclass(frozen=True)
class PendingActionRecord:
    job_id: int
    user_id: int
    action_type: str
    target_section: str | None
    details_json: str
    created_at: str

    @property
    def details(self) -> dict[str, Any]:
        data = json.loads(self.details_json or "{}")
        return data if isinstance(data, dict) else {}


@dataclass(frozen=True)
class JobRuntimeRecord:
    job_id: int
    failure_count: int
    last_error: str | None
    last_error_at: str | None
    next_retry_at: str | None
    last_success_at: str | None
    last_checked_at: str | None
    last_action: str | None
    last_message: str | None


@dataclass(frozen=True)
class UserRuntimeRecord:
    user_id: int
    needs_captcha: bool
    last_captcha_at: str | None
    last_captcha_note: str | None
    last_login_error: str | None
    last_login_error_at: str | None


class SQLiteStorage:
    """SQLite repository for Telegram users, jobs, snapshots, and scheduler state."""

    def __init__(self, path: str | Path = "archershub_bot.sqlite3") -> None:
        self.path = Path(path)
        if self.path.parent != Path(""):
            self.path.parent.mkdir(parents=True, exist_ok=True)
        self.migrate()

    @contextmanager
    def connect(self) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(self.path)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        try:
            yield conn
            conn.commit()
        finally:
            conn.close()

    def migrate(self) -> None:
        with sqlite3.connect(self.path) as conn:
            conn.execute("PRAGMA foreign_keys = ON")
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS users (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    telegram_id INTEGER NOT NULL UNIQUE,
                    username TEXT,
                    registered_at TEXT NOT NULL,
                    default_add_conflict_policy TEXT NOT NULL DEFAULT 'never_displace',
                    is_active INTEGER NOT NULL DEFAULT 1
                );

                CREATE TABLE IF NOT EXISTS registration_codes (
                    code TEXT PRIMARY KEY,
                    created_at TEXT NOT NULL,
                    expires_at TEXT,
                    used_at TEXT,
                    used_by_telegram_id INTEGER
                );

                CREATE TABLE IF NOT EXISTS credentials (
                    user_id INTEGER PRIMARY KEY REFERENCES users(id) ON DELETE CASCADE,
                    username_encrypted TEXT NOT NULL,
                    password_encrypted TEXT NOT NULL,
                    cookies_encrypted TEXT,
                    updated_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS jobs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                    job_type TEXT NOT NULL,
                    mode TEXT NOT NULL,
                    course_code TEXT NOT NULL,
                    section_filters_json TEXT NOT NULL DEFAULT '[]',
                    priority_sections_json TEXT NOT NULL DEFAULT '[]',
                    target_section TEXT,
                    enabled INTEGER NOT NULL DEFAULT 1,
                    paused_at TEXT,
                    completed_at TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );

                CREATE INDEX IF NOT EXISTS idx_jobs_enabled ON jobs(enabled, completed_at);
                CREATE INDEX IF NOT EXISTS idx_jobs_user ON jobs(user_id);

                CREATE TABLE IF NOT EXISTS snapshots (
                    key TEXT PRIMARY KEY,
                    data_json TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS scheduler_state (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS pending_actions (
                    job_id INTEGER PRIMARY KEY REFERENCES jobs(id) ON DELETE CASCADE,
                    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                    action_type TEXT NOT NULL,
                    target_section TEXT,
                    details_json TEXT NOT NULL DEFAULT '{}',
                    created_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS job_runtime (
                    job_id INTEGER PRIMARY KEY REFERENCES jobs(id) ON DELETE CASCADE,
                    failure_count INTEGER NOT NULL DEFAULT 0,
                    last_error TEXT,
                    last_error_at TEXT,
                    next_retry_at TEXT,
                    last_success_at TEXT,
                    last_checked_at TEXT,
                    last_action TEXT,
                    last_message TEXT
                );

                CREATE TABLE IF NOT EXISTS user_runtime (
                    user_id INTEGER PRIMARY KEY REFERENCES users(id) ON DELETE CASCADE,
                    needs_captcha INTEGER NOT NULL DEFAULT 0,
                    last_captcha_at TEXT,
                    last_captcha_note TEXT,
                    last_login_error TEXT,
                    last_login_error_at TEXT
                );
                """
            )
            columns = {row[1] for row in conn.execute("PRAGMA table_info(jobs)").fetchall()}
            if "paused_at" not in columns:
                conn.execute("ALTER TABLE jobs ADD COLUMN paused_at TEXT")
            now = dt_to_text(utcnow())
            conn.execute(
                "INSERT OR IGNORE INTO scheduler_state(key, value, updated_at) VALUES('interval_secs', ?, ?)",
                (str(DEFAULT_INTERVAL_SECS), now),
            )

    def generate_registration_code(self, *, ttl_hours: int | None = 24) -> str:
        code = secrets.token_urlsafe(9)
        now = utcnow()
        expires = now + timedelta(hours=ttl_hours) if ttl_hours else None
        with self.connect() as conn:
            conn.execute(
                "INSERT INTO registration_codes(code, created_at, expires_at) VALUES(?, ?, ?)",
                (code, dt_to_text(now), dt_to_text(expires)),
            )
        return code

    def redeem_registration_code(self, code: str, telegram_id: int, username: str | None = None) -> UserRecord:
        now = utcnow()
        with self.connect() as conn:
            row = conn.execute("SELECT * FROM registration_codes WHERE code = ?", (code,)).fetchone()
            if row is None:
                raise ValueError("registration code was not found")
            if row["used_at"]:
                raise ValueError("registration code was already used")
            expires_at = text_to_dt(row["expires_at"])
            if expires_at and expires_at < now:
                raise ValueError("registration code has expired")

            conn.execute(
                """
                INSERT INTO users(telegram_id, username, registered_at, is_active)
                VALUES(?, ?, ?, 1)
                ON CONFLICT(telegram_id) DO UPDATE SET username=excluded.username, is_active=1
                """,
                (telegram_id, username, dt_to_text(now)),
            )
            conn.execute(
                "UPDATE registration_codes SET used_at = ?, used_by_telegram_id = ? WHERE code = ?",
                (dt_to_text(now), telegram_id, code),
            )
            user_row = conn.execute("SELECT * FROM users WHERE telegram_id = ?", (telegram_id,)).fetchone()
        return self._user_from_row(user_row)

    def get_user_by_telegram_id(self, telegram_id: int) -> UserRecord | None:
        with self.connect() as conn:
            row = conn.execute("SELECT * FROM users WHERE telegram_id = ?", (telegram_id,)).fetchone()
        return self._user_from_row(row) if row else None

    def list_users(self) -> list[UserRecord]:
        with self.connect() as conn:
            rows = conn.execute("SELECT * FROM users ORDER BY registered_at").fetchall()
        return [self._user_from_row(row) for row in rows]

    def save_credentials(self, user_id: int, username_encrypted: str, password_encrypted: str, cookies_encrypted: str | None = None) -> None:
        now = dt_to_text(utcnow())
        with self.connect() as conn:
            conn.execute(
                """
                INSERT INTO credentials(user_id, username_encrypted, password_encrypted, cookies_encrypted, updated_at)
                VALUES(?, ?, ?, ?, ?)
                ON CONFLICT(user_id) DO UPDATE SET
                  username_encrypted=excluded.username_encrypted,
                  password_encrypted=excluded.password_encrypted,
                  cookies_encrypted=excluded.cookies_encrypted,
                  updated_at=excluded.updated_at
                """,
                (user_id, username_encrypted, password_encrypted, cookies_encrypted, now),
            )

    def get_credentials(self, user_id: int) -> CredentialRecord | None:
        with self.connect() as conn:
            row = conn.execute("SELECT * FROM credentials WHERE user_id = ?", (user_id,)).fetchone()
        return CredentialRecord(**dict(row)) if row else None

    def add_job(
        self,
        *,
        user_id: int,
        job_type: str,
        course_code: str,
        mode: str = JOB_MODE_NOTIFY,
        section_filters: list[str] | None = None,
        priority_sections: list[str] | None = None,
        target_section: str | None = None,
    ) -> JobRecord:
        now = dt_to_text(utcnow())
        with self.connect() as conn:
            cur = conn.execute(
                """
                INSERT INTO jobs(user_id, job_type, mode, course_code, section_filters_json,
                                 priority_sections_json, target_section, enabled, created_at, updated_at)
                VALUES(?, ?, ?, ?, ?, ?, ?, 1, ?, ?)
                """,
                (
                    user_id,
                    job_type,
                    mode,
                    course_code.upper(),
                    json.dumps(section_filters or []),
                    json.dumps(priority_sections or []),
                    target_section.upper() if target_section else None,
                    now,
                    now,
                ),
            )
            row = conn.execute("SELECT * FROM jobs WHERE id = ?", (cur.lastrowid,)).fetchone()
        return self._job_from_row(row)

    def list_jobs(self, *, user_id: int | None = None, active_only: bool = False) -> list[JobRecord]:
        query = "SELECT * FROM jobs"
        clauses: list[str] = []
        params: list[Any] = []
        if user_id is not None:
            clauses.append("user_id = ?")
            params.append(user_id)
        if active_only:
            clauses.append("enabled = 1 AND completed_at IS NULL AND paused_at IS NULL")
        if clauses:
            query += " WHERE " + " AND ".join(clauses)
        query += " ORDER BY id"
        with self.connect() as conn:
            rows = conn.execute(query, params).fetchall()
        return [self._job_from_row(row) for row in rows]

    def get_job(self, job_id: int) -> JobRecord | None:
        with self.connect() as conn:
            row = conn.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
        return self._job_from_row(row) if row else None

    def disable_job(self, job_id: int) -> None:
        with self.connect() as conn:
            conn.execute("UPDATE jobs SET enabled = 0, updated_at = ? WHERE id = ?", (dt_to_text(utcnow()), job_id))
            conn.execute("DELETE FROM pending_actions WHERE job_id = ?", (job_id,))

    def complete_job(self, job_id: int) -> None:
        now = dt_to_text(utcnow())
        with self.connect() as conn:
            conn.execute("UPDATE jobs SET completed_at = ?, enabled = 0, updated_at = ? WHERE id = ?", (now, now, job_id))
            conn.execute("DELETE FROM pending_actions WHERE job_id = ?", (job_id,))

    def pause_job(self, job_id: int) -> None:
        now = dt_to_text(utcnow())
        with self.connect() as conn:
            conn.execute("UPDATE jobs SET paused_at = ?, updated_at = ? WHERE id = ?", (now, now, job_id))

    def resume_job(self, job_id: int) -> None:
        with self.connect() as conn:
            conn.execute("UPDATE jobs SET paused_at = NULL, updated_at = ? WHERE id = ?", (dt_to_text(utcnow()), job_id))

    def update_job_mode(self, job_id: int, mode: str) -> None:
        with self.connect() as conn:
            conn.execute("UPDATE jobs SET mode = ?, updated_at = ? WHERE id = ?", (mode, dt_to_text(utcnow()), job_id))

    def update_job_priority_sections(self, job_id: int, priority_sections: list[str]) -> None:
        with self.connect() as conn:
            conn.execute(
                "UPDATE jobs SET priority_sections_json = ?, updated_at = ? WHERE id = ?",
                (json.dumps(priority_sections), dt_to_text(utcnow()), job_id),
            )

    def update_job_target_section(self, job_id: int, target_section: str | None) -> None:
        with self.connect() as conn:
            conn.execute(
                "UPDATE jobs SET target_section = ?, updated_at = ? WHERE id = ?",
                (target_section.upper() if target_section else None, dt_to_text(utcnow()), job_id),
            )

    def get_snapshot(self, key: str) -> Any | None:
        with self.connect() as conn:
            row = conn.execute("SELECT data_json FROM snapshots WHERE key = ?", (key,)).fetchone()
        return json.loads(row["data_json"]) if row else None

    def set_snapshot(self, key: str, data: Any) -> None:
        now = dt_to_text(utcnow())
        with self.connect() as conn:
            conn.execute(
                """
                INSERT INTO snapshots(key, data_json, updated_at) VALUES(?, ?, ?)
                ON CONFLICT(key) DO UPDATE SET data_json=excluded.data_json, updated_at=excluded.updated_at
                """,
                (key, json.dumps(data, sort_keys=True), now),
            )

    def delete_snapshot(self, key: str) -> None:
        with self.connect() as conn:
            conn.execute("DELETE FROM snapshots WHERE key = ?", (key,))

    def set_pending_action(
        self,
        *,
        job_id: int,
        user_id: int,
        action_type: str,
        target_section: str | None,
        details: dict[str, Any] | None = None,
    ) -> None:
        with self.connect() as conn:
            conn.execute(
                """
                INSERT INTO pending_actions(job_id, user_id, action_type, target_section, details_json, created_at)
                VALUES(?, ?, ?, ?, ?, ?)
                ON CONFLICT(job_id) DO UPDATE SET
                  user_id=excluded.user_id,
                  action_type=excluded.action_type,
                  target_section=excluded.target_section,
                  details_json=excluded.details_json,
                  created_at=excluded.created_at
                """,
                (
                    job_id,
                    user_id,
                    action_type,
                    target_section,
                    json.dumps(details or {}, sort_keys=True),
                    dt_to_text(utcnow()),
                ),
            )

    def get_pending_action(self, job_id: int) -> PendingActionRecord | None:
        with self.connect() as conn:
            row = conn.execute("SELECT * FROM pending_actions WHERE job_id = ?", (job_id,)).fetchone()
        return PendingActionRecord(**dict(row)) if row else None

    def list_pending_actions(self, *, user_id: int | None = None) -> list[PendingActionRecord]:
        query = "SELECT * FROM pending_actions"
        params: list[Any] = []
        if user_id is not None:
            query += " WHERE user_id = ?"
            params.append(user_id)
        query += " ORDER BY created_at"
        with self.connect() as conn:
            rows = conn.execute(query, params).fetchall()
        return [PendingActionRecord(**dict(row)) for row in rows]

    def clear_pending_action(self, job_id: int) -> None:
        with self.connect() as conn:
            conn.execute("DELETE FROM pending_actions WHERE job_id = ?", (job_id,))

    def get_job_runtime(self, job_id: int) -> JobRuntimeRecord | None:
        with self.connect() as conn:
            row = conn.execute("SELECT * FROM job_runtime WHERE job_id = ?", (job_id,)).fetchone()
        return JobRuntimeRecord(**dict(row)) if row else None

    def list_job_runtime(self) -> list[JobRuntimeRecord]:
        with self.connect() as conn:
            rows = conn.execute("SELECT * FROM job_runtime ORDER BY COALESCE(last_error_at, last_checked_at, last_success_at, '') DESC").fetchall()
        return [JobRuntimeRecord(**dict(row)) for row in rows]

    def record_job_success(self, job_id: int, *, action: str | None = None, message: str | None = None) -> None:
        now = dt_to_text(utcnow())
        with self.connect() as conn:
            conn.execute(
                """
                INSERT INTO job_runtime(job_id, failure_count, last_error, last_error_at, next_retry_at, last_success_at, last_checked_at, last_action, last_message)
                VALUES(?, 0, NULL, NULL, NULL, ?, ?, ?, ?)
                ON CONFLICT(job_id) DO UPDATE SET
                  failure_count=0,
                  last_error=NULL,
                  last_error_at=NULL,
                  next_retry_at=NULL,
                  last_success_at=excluded.last_success_at,
                  last_checked_at=excluded.last_checked_at,
                  last_action=excluded.last_action,
                  last_message=excluded.last_message
                """,
                (job_id, now, now, action, message),
            )

    def record_job_failure(self, job_id: int, *, error: str, next_retry_at: str | None) -> JobRuntimeRecord:
        now = dt_to_text(utcnow())
        current = self.get_job_runtime(job_id)
        failure_count = (current.failure_count if current else 0) + 1
        with self.connect() as conn:
            conn.execute(
                """
                INSERT INTO job_runtime(job_id, failure_count, last_error, last_error_at, next_retry_at, last_checked_at)
                VALUES(?, ?, ?, ?, ?, ?)
                ON CONFLICT(job_id) DO UPDATE SET
                  failure_count=excluded.failure_count,
                  last_error=excluded.last_error,
                  last_error_at=excluded.last_error_at,
                  next_retry_at=excluded.next_retry_at,
                  last_checked_at=excluded.last_checked_at
                """,
                (job_id, failure_count, error, now, next_retry_at, now),
            )
        return self.get_job_runtime(job_id)  # type: ignore[return-value]

    def record_job_checked(self, job_id: int) -> None:
        now = dt_to_text(utcnow())
        with self.connect() as conn:
            conn.execute(
                """
                INSERT INTO job_runtime(job_id, last_checked_at) VALUES(?, ?)
                ON CONFLICT(job_id) DO UPDATE SET last_checked_at=excluded.last_checked_at
                """,
                (job_id, now),
            )

    def get_user_runtime(self, user_id: int) -> UserRuntimeRecord | None:
        with self.connect() as conn:
            row = conn.execute("SELECT * FROM user_runtime WHERE user_id = ?", (user_id,)).fetchone()
        if not row:
            return None
        data = dict(row)
        data["needs_captcha"] = bool(data["needs_captcha"])
        return UserRuntimeRecord(**data)

    def list_user_runtime(self) -> list[UserRuntimeRecord]:
        with self.connect() as conn:
            rows = conn.execute("SELECT * FROM user_runtime ORDER BY COALESCE(last_captcha_at, last_login_error_at, '') DESC").fetchall()
        records: list[UserRuntimeRecord] = []
        for row in rows:
            data = dict(row)
            data["needs_captcha"] = bool(data["needs_captcha"])
            records.append(UserRuntimeRecord(**data))
        return records

    def mark_user_captcha_needed(self, user_id: int, note: str | None = None) -> None:
        now = dt_to_text(utcnow())
        with self.connect() as conn:
            conn.execute(
                """
                INSERT INTO user_runtime(user_id, needs_captcha, last_captcha_at, last_captcha_note)
                VALUES(?, 1, ?, ?)
                ON CONFLICT(user_id) DO UPDATE SET
                  needs_captcha=1,
                  last_captcha_at=excluded.last_captcha_at,
                  last_captcha_note=excluded.last_captcha_note
                """,
                (user_id, now, note),
            )

    def clear_user_captcha_needed(self, user_id: int) -> None:
        with self.connect() as conn:
            conn.execute(
                """
                INSERT INTO user_runtime(user_id, needs_captcha) VALUES(?, 0)
                ON CONFLICT(user_id) DO UPDATE SET needs_captcha=0
                """,
                (user_id,),
            )

    def record_user_login_error(self, user_id: int, error: str) -> None:
        now = dt_to_text(utcnow())
        with self.connect() as conn:
            conn.execute(
                """
                INSERT INTO user_runtime(user_id, last_login_error, last_login_error_at)
                VALUES(?, ?, ?)
                ON CONFLICT(user_id) DO UPDATE SET
                  last_login_error=excluded.last_login_error,
                  last_login_error_at=excluded.last_login_error_at
                """,
                (user_id, error, now),
            )

    def get_scheduler_status(self) -> dict[str, str | None]:
        with self.connect() as conn:
            rows = conn.execute("SELECT key, value FROM scheduler_state").fetchall()
        return {str(row["key"]): str(row["value"]) for row in rows}

    def get_interval_secs(self) -> int:
        with self.connect() as conn:
            row = conn.execute("SELECT value FROM scheduler_state WHERE key = 'interval_secs'").fetchone()
        return int(row["value"]) if row else DEFAULT_INTERVAL_SECS

    def set_interval_secs(self, value: int) -> None:
        if value < 5:
            raise ValueError("interval must be at least 5 seconds")
        with self.connect() as conn:
            conn.execute(
                """
                INSERT INTO scheduler_state(key, value, updated_at) VALUES('interval_secs', ?, ?)
                ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at
                """,
                (str(value), dt_to_text(utcnow())),
            )

    def set_scheduler_status(
        self,
        *,
        last_error: str | None = None,
        checked_jobs: int | None = None,
        notifications_sent: int | None = None,
    ) -> None:
        now = dt_to_text(utcnow())
        with self.connect() as conn:
            conn.execute(
                """
                INSERT INTO scheduler_state(key, value, updated_at) VALUES('last_run_at', ?, ?)
                ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at
                """,
                (now, now),
            )
            if last_error is not None:
                conn.execute(
                    """
                    INSERT INTO scheduler_state(key, value, updated_at) VALUES('last_error', ?, ?)
                    ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at
                    """,
                    (last_error, now),
                )
            if checked_jobs is not None:
                conn.execute(
                    """
                    INSERT INTO scheduler_state(key, value, updated_at) VALUES('last_checked_jobs', ?, ?)
                    ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at
                    """,
                    (str(checked_jobs), now),
                )
            if notifications_sent is not None:
                conn.execute(
                    """
                    INSERT INTO scheduler_state(key, value, updated_at) VALUES('last_notifications_sent', ?, ?)
                    ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at
                    """,
                    (str(notifications_sent), now),
                )

    @staticmethod
    def _user_from_row(row: sqlite3.Row) -> UserRecord:
        data = dict(row)
        data["is_active"] = bool(data["is_active"])
        return UserRecord(**data)

    @staticmethod
    def _job_from_row(row: sqlite3.Row) -> JobRecord:
        data = dict(row)
        data["section_filters"] = json.loads(data.pop("section_filters_json") or "[]")
        data["priority_sections"] = json.loads(data.pop("priority_sections_json") or "[]")
        data["enabled"] = bool(data["enabled"])
        return JobRecord(**data)
