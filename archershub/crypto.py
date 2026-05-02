from __future__ import annotations

import base64
import hashlib
import os
from dataclasses import dataclass

from cryptography.fernet import Fernet, InvalidToken


class SecretError(RuntimeError):
    """Raised when encrypted application secrets cannot be processed."""


def fernet_key_from_secret(secret: str) -> bytes:
    """Derive a stable Fernet key from an arbitrary deployment secret."""
    if not secret:
        raise SecretError("ARCHERSHUB_MASTER_KEY is required for encrypted storage")
    try:
        raw = base64.urlsafe_b64decode(secret.encode())
        if len(raw) == 32:
            return secret.encode()
    except Exception:
        pass
    digest = hashlib.sha256(secret.encode("utf-8")).digest()
    return base64.urlsafe_b64encode(digest)


@dataclass(frozen=True)
class SecretBox:
    """Small wrapper around Fernet for app-level encrypted SQLite fields."""

    fernet: Fernet

    @classmethod
    def from_secret(cls, secret: str) -> "SecretBox":
        return cls(Fernet(fernet_key_from_secret(secret)))

    @classmethod
    def from_env(cls, env_name: str = "ARCHERSHUB_MASTER_KEY") -> "SecretBox":
        return cls.from_secret(os.getenv(env_name, ""))

    def encrypt_text(self, value: str | None) -> str | None:
        if value is None:
            return None
        return self.fernet.encrypt(value.encode("utf-8")).decode("ascii")

    def decrypt_text(self, token: str | None) -> str | None:
        if token is None:
            return None
        try:
            return self.fernet.decrypt(token.encode("ascii")).decode("utf-8")
        except InvalidToken as exc:
            raise SecretError("encrypted value could not be decrypted with this master key") from exc
