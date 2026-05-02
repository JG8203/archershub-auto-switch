from __future__ import annotations

import json
from typing import Any
from urllib.parse import urljoin

import requests

from .constants import TIMEOUT


def to_snake_case(value: str) -> str:
    output: list[str] = []
    prev_is_lower_or_digit = False
    for char in value:
        if char.isalnum():
            if char.isupper():
                if prev_is_lower_or_digit and output and output[-1] != "_":
                    output.append("_")
                output.append(char.lower())
                prev_is_lower_or_digit = False
            else:
                output.append(char)
                prev_is_lower_or_digit = char.islower() or char.isdigit()
        elif output and output[-1] != "_":
            output.append("_")
            prev_is_lower_or_digit = False
    return "".join(output).strip("_")


def normalize_value(value: Any) -> Any:
    if isinstance(value, dict):
        return {to_snake_case(str(key)): normalize_value(item) for key, item in value.items()}
    if isinstance(value, list):
        return [normalize_value(item) for item in value]
    return value


def canonicalize_value(value: Any) -> Any:
    if isinstance(value, dict):
        return {key: canonicalize_value(value[key]) for key in sorted(value)}
    if isinstance(value, list):
        return [canonicalize_value(item) for item in value]
    return value


def value_to_string(value: Any) -> str | None:
    if isinstance(value, str):
        return value
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return str(value)
    return None


def post_form_json(session: requests.Session, base_url: str, path: str, data: dict[str, Any]) -> Any:
    response = session.post(
        urljoin(base_url.rstrip("/") + "/", path.lstrip("/")),
        data=data,
        headers={"X-Requested-With": "XMLHttpRequest"},
        timeout=TIMEOUT,
    )
    response.raise_for_status()
    content_type = response.headers.get("content-type", "").lower()
    if "json" not in content_type and response.text.lstrip().startswith("<"):
        raise RuntimeError(f"expected JSON from {path}, got HTML; session may be logged out")
    return response.json()


def post_json(session: requests.Session, base_url: str, path: str, data: dict[str, Any]) -> Any:
    response = session.post(
        urljoin(base_url.rstrip("/") + "/", path.lstrip("/")),
        data=json.dumps(data),
        headers={
            "X-Requested-With": "XMLHttpRequest",
            "Content-Type": "application/json;charset=utf-8",
        },
        timeout=TIMEOUT,
    )
    response.raise_for_status()
    if response.text.lstrip().startswith("<"):
        raise RuntimeError(f"expected non-HTML response from {path}; session may be logged out")

    content_type = response.headers.get("content-type", "").lower()
    if "json" in content_type:
        return response.json()

    text = response.text.strip()
    try:
        return response.json()
    except ValueError:
        return text


def flatten_jquery_form(data: dict[str, Any]) -> dict[str, Any]:
    flattened: dict[str, Any] = {}

    def add(prefix: str, value: Any) -> None:
        if isinstance(value, list):
            for index, item in enumerate(value):
                add(f"{prefix}[{index}]", item)
        elif isinstance(value, dict):
            for key, item in value.items():
                add(f"{prefix}[{key}]", item)
        else:
            flattened[prefix] = value

    for key, value in data.items():
        add(key, value)
    return flattened


def parse_number(value: Any, default: float = 0) -> float:
    if isinstance(value, bool):
        return default
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        try:
            return float(value.strip())
        except ValueError:
            return default
    return default


def string_id(value: Any) -> str:
    text = value_to_string(value)
    if text is None:
        return ""
    return text.strip()


def ids_equal(left: Any, right: Any) -> bool:
    return string_id(left) == string_id(right)


def first_list(data: dict[str, Any], *keys: str) -> list[Any]:
    for key in keys:
        value = data.get(key)
        if isinstance(value, list):
            return value
    return []


def first_dict(data: dict[str, Any], *keys: str) -> dict[str, Any] | None:
    for key in keys:
        value = data.get(key)
        if isinstance(value, list) and value and isinstance(value[0], dict):
            return value[0]
        if isinstance(value, dict):
            return value
    return None
