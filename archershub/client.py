from __future__ import annotations

import json
import os
from dataclasses import dataclass
from typing import Any, BinaryIO
from urllib.parse import urljoin

import requests

from .api import normalize_value
from .auth import login_with_retry
from .catalog import ENDPOINTS, EndpointSpec, get_endpoint
from .constants import DEFAULT_BASE_URL, DEFAULT_LOGIN_PATH, DEFAULT_MAX_LOGIN_ATTEMPTS, TIMEOUT

_MUTATING_SAFETY = {"mutation", "payment"}


class UnsafeEndpointError(RuntimeError):
    """Raised when a mutating/payment endpoint is called without confirmation."""


class ArchersHubResponseError(RuntimeError):
    """Raised when ArchersHub returns an unexpected response for an endpoint call."""


@dataclass(frozen=True)
class EndpointResult:
    spec: EndpointSpec
    status_code: int
    content_type: str
    data: Any
    response: requests.Response


class EndpointNamespace:
    """Convenience accessor for all endpoints under a controller."""

    def __init__(self, client: "ArchersHubClient", controller: str):
        self.client = client
        self.controller = controller

    def call(self, action: str, **kwargs: Any) -> Any:
        return self.client.call(f"{self.controller}/{action}", **kwargs)

    def __getattr__(self, name: str):
        action = "".join(part.capitalize() for part in name.split("_"))

        def invoke(**kwargs: Any) -> Any:
            return self.call(action, **kwargs)

        return invoke


class ArchersHubClient:
    """Authenticated Python client for mirror-discovered ArchersHub endpoints.

    The client is intentionally conservative: endpoints classified as mutation or
    payment are blocked unless `allow_mutation=True` and the exact endpoint key is
    supplied through `confirm_mutation`.
    """

    def __init__(
        self,
        *,
        base_url: str = DEFAULT_BASE_URL,
        login_path: str = DEFAULT_LOGIN_PATH,
        username: str | None = None,
        password: str | None = None,
        session: requests.Session | None = None,
        allow_mutation: bool = False,
        captcha_ocr: bool = True,
        max_login_attempts: int = DEFAULT_MAX_LOGIN_ATTEMPTS,
        save_login_artifacts: bool = False,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.login_path = login_path
        self.username = username
        self.password = password
        self.session = session
        self.allow_mutation = allow_mutation
        self.captcha_ocr = captcha_ocr
        self.max_login_attempts = max_login_attempts
        self.save_login_artifacts = save_login_artifacts

    @classmethod
    def from_env(cls, *, prefix: str = "ARCHERSHUB_") -> "ArchersHubClient":
        return cls(
            base_url=os.getenv(prefix + "BASE_URL", DEFAULT_BASE_URL),
            login_path=os.getenv(prefix + "LOGIN_PATH", DEFAULT_LOGIN_PATH),
            username=os.getenv(prefix + "USERNAME"),
            password=os.getenv(prefix + "PASSWORD"),
            allow_mutation=os.getenv(prefix + "ALLOW_MUTATION", "").lower() in {"1", "true", "yes"},
            captcha_ocr=os.getenv(prefix + "NO_CAPTCHA_OCR", "").lower() not in {"1", "true", "yes"},
            max_login_attempts=int(os.getenv(prefix + "MAX_LOGIN_ATTEMPTS", DEFAULT_MAX_LOGIN_ATTEMPTS)),
            save_login_artifacts=os.getenv(prefix + "SAVE_LOGIN_ARTIFACTS", "").lower() in {"1", "true", "yes"},
        )

    def login(self) -> requests.Session:
        if self.session is not None:
            return self.session
        if not self.username or not self.password:
            raise RuntimeError("username and password are required to login")
        self.session = login_with_retry(
            self.base_url,
            self.login_path,
            self.username,
            self.password,
            max_attempts=self.max_login_attempts,
            captcha_ocr=self.captcha_ocr,
            save_artifacts=self.save_login_artifacts,
        )
        return self.session

    @property
    def endpoints(self) -> tuple[EndpointSpec, ...]:
        return ENDPOINTS

    def endpoint(self, name: str) -> EndpointSpec:
        return get_endpoint(name)

    def namespace(self, controller: str) -> EndpointNamespace:
        return EndpointNamespace(self, controller)

    def __getattr__(self, name: str) -> EndpointNamespace:
        controller = "".join(part.capitalize() for part in name.split("_"))
        if any(spec.controller.lower() == controller.lower() for spec in ENDPOINTS):
            # Preserve catalog casing.
            actual = next(spec.controller for spec in ENDPOINTS if spec.controller.lower() == controller.lower())
            return self.namespace(actual)
        raise AttributeError(name)

    def call(
        self,
        endpoint: str,
        *,
        data: dict[str, Any] | None = None,
        params: dict[str, Any] | None = None,
        files: dict[str, Any] | None = None,
        confirm_mutation: str | bool | None = None,
        normalize: bool = True,
        return_result: bool = False,
        timeout: int = TIMEOUT,
    ) -> Any:
        spec = get_endpoint(endpoint)
        self._ensure_safe(spec, confirm_mutation)
        response = self._send(spec, data=data or {}, params=params or {}, files=files, timeout=timeout)
        parsed = self._parse_response(spec, response)
        if normalize:
            parsed = normalize_value(parsed)
        result = EndpointResult(spec, response.status_code, response.headers.get("content-type", ""), parsed, response)
        return result if return_result else result.data

    def _ensure_safe(self, spec: EndpointSpec, confirm_mutation: str | bool | None) -> None:
        if spec.safety not in _MUTATING_SAFETY:
            return
        expected = spec.key
        confirmed = confirm_mutation is True or confirm_mutation == expected or confirm_mutation == spec.path
        if not (self.allow_mutation and confirmed):
            raise UnsafeEndpointError(
                f"{spec.key} is classified as {spec.safety}; pass allow_mutation=True and "
                f"confirm_mutation={expected!r} to call it"
            )

    def _send(
        self,
        spec: EndpointSpec,
        *,
        data: dict[str, Any],
        params: dict[str, Any],
        files: dict[str, Any] | None,
        timeout: int,
    ) -> requests.Response:
        session = self.login()
        url = urljoin(self.base_url + "/", spec.path.lstrip("/"))
        headers = {"X-Requested-With": "XMLHttpRequest"}

        if spec.method == "GET":
            response = session.get(url, params={**data, **params}, headers=headers, timeout=timeout)
        elif spec.body_type == "json":
            response = session.post(
                url,
                params=params,
                data=json.dumps(data),
                headers={**headers, "Content-Type": "application/json;charset=utf-8"},
                timeout=timeout,
            )
        elif spec.body_type == "multipart":
            multipart_files = files or {}
            response = session.post(url, params=params, data=data, files=multipart_files, headers=headers, timeout=timeout)
        else:
            response = session.post(url, params=params, data=data, headers=headers, timeout=timeout)

        response.raise_for_status()
        if response.text.lstrip().lower().startswith("<") and "studentlogin" in response.url.lower():
            raise ArchersHubResponseError(f"{spec.key} returned login HTML; session may be expired")
        return response

    def _parse_response(self, spec: EndpointSpec, response: requests.Response) -> Any:
        content_type = response.headers.get("content-type", "").lower()
        if spec.safety == "download" or "application/pdf" in content_type or "image/" in content_type:
            return response.content
        if "json" in content_type:
            return response.json()
        text = response.text.strip()
        if not text:
            return ""
        if text.startswith("<"):
            if spec.method == "GET":
                return text
            raise ArchersHubResponseError(f"expected data from {spec.key}, got HTML")
        try:
            return response.json()
        except ValueError:
            return text

    def download(
        self,
        endpoint: str,
        *,
        data: dict[str, Any] | None = None,
        params: dict[str, Any] | None = None,
        confirm_mutation: str | bool | None = None,
    ) -> bytes:
        result = self.call(
            endpoint,
            data=data,
            params=params,
            confirm_mutation=confirm_mutation,
            normalize=False,
            return_result=True,
        )
        if isinstance(result.data, bytes):
            return result.data
        if isinstance(result.data, str):
            return result.data.encode()
        return json.dumps(result.data).encode()


# Readable controller aliases for IDE completion/imports.
CONTROLLERS = tuple(sorted({spec.controller for spec in ENDPOINTS}))
