from __future__ import annotations

import json
from typing import Any

import requests

from .api import (
    canonicalize_value,
    normalize_value,
    parse_number,
    post_form_json,
    value_to_string,
)


def first_string_field(value: dict[str, Any], array_key: str, fields: list[str]) -> str | None:
    items = value.get(array_key)
    if not isinstance(items, list):
        return None
    for item in items:
        if not isinstance(item, dict):
            continue
        for field in fields:
            item_value = value_to_string(item.get(field))
            if item_value:
                return item_value
    return None


def current_session_id(value: dict[str, Any]) -> str | None:
    items = value.get("session_drp")
    if not isinstance(items, list):
        return None
    for item in items:
        if not isinstance(item, dict):
            continue
        is_current = item.get("is_current_session")
        if is_current is True or is_current == 1 or is_current == "1" or str(is_current).lower() == "true":
            return value_to_string(item.get("academic_session_id"))
    return None


def extract_course_code(item: dict[str, Any]) -> str | None:
    for field in ("course_code", "coursecode", "code"):
        value = value_to_string(item.get(field))
        if value:
            return value.strip()

    text = value_to_string(item.get("text")) or value_to_string(item.get("course_name"))
    if not text:
        return None
    candidate = text.split(" - ", 1)[0].split(None, 1)[0].strip()
    return candidate or None


def extract_course_creation_id(item: dict[str, Any]) -> str | None:
    for field in ("course_creation_id", "courseid", "value", "id"):
        value = value_to_string(item.get(field))
        if value:
            return value
    return None


def resolve_course_target(
    session: requests.Session,
    base_url: str,
    course_code: str,
    *,
    campus_id: str | None = None,
    academic_session_id: str | None = None,
) -> dict[str, str]:
    context = normalize_value(post_form_json(session, base_url, "/CourseFinder/GetAllDropDownList/", {}))
    if not isinstance(context, dict):
        raise RuntimeError("course finder dropdown response was not an object")

    campus_id = campus_id or first_string_field(context, "campus_drp", ["campusno", "campus_no", "value", "id"])
    if not campus_id:
        raise RuntimeError("unable to determine campus id; pass --campus-id")

    academic_session_id = academic_session_id or current_session_id(context)
    if not academic_session_id:
        raise RuntimeError("unable to determine academic session id; pass --academic-session-id")

    course_list = normalize_value(
        post_form_json(
            session,
            base_url,
            "/CourseFinder/GetCourseList/",
            {"Campusno": campus_id, "AcademicSession": academic_session_id},
        )
    )
    if not isinstance(course_list, dict) or not isinstance(course_list.get("course_drp"), list):
        raise RuntimeError("course list response did not contain CourseDrp")

    matches: list[dict[str, Any]] = []
    for item in course_list["course_drp"]:
        if not isinstance(item, dict):
            continue
        code = extract_course_code(item)
        course_creation_id = extract_course_creation_id(item)
        if code and course_creation_id and code.upper() == course_code.upper():
            matches.append(item)

    if not matches:
        raise RuntimeError(
            f"course code {course_code} was not found for campus_id={campus_id} "
            f"academic_session_id={academic_session_id}"
        )
    if len(matches) > 1:
        choices = ", ".join(
            f"{extract_course_creation_id(item)} ({item.get('text') or item.get('course_name') or 'unknown course'})"
            for item in matches
        )
        raise RuntimeError(f"course code {course_code} resolved to multiple courses: {choices}")

    selected = matches[0]
    return {
        "course_code": extract_course_code(selected) or course_code.upper(),
        "course_creation_id": extract_course_creation_id(selected) or "",
        "campus_id": campus_id,
        "academic_session_id": academic_session_id,
        "is_cross_offer": value_to_string(selected.get("is_cross_offer")) or "0",
        "grid_type": value_to_string(selected.get("grid_type")) or "0",
    }


def fetch_course_snapshot(session: requests.Session, base_url: str, target: dict[str, str]) -> str:
    data = fetch_course_data(session, base_url, target)
    canonical = canonicalize_value(data)
    return json.dumps(canonical, indent=2, ensure_ascii=False)


def fetch_course_data(
    session: requests.Session,
    base_url: str,
    target: dict[str, str],
) -> Any:
    return normalize_value(post_form_json(
        session,
        base_url,
        "/CourseFinder/GetCFData/",
        {
            "Campusno": target["campus_id"],
            "AcademicSession": target["academic_session_id"],
            "Courseid": target["course_creation_id"],
            "isCrossOffer": target["is_cross_offer"],
            "gridType": target["grid_type"],
        },
    ))


def effective_capacity(section: dict[str, Any]) -> float:
    updated_capacity = parse_number(section.get("updated_capacity"))
    if updated_capacity > 0:
        return updated_capacity
    return parse_number(section.get("capacity"))


def available_slots(section: dict[str, Any]) -> float:
    return effective_capacity(section) - parse_number(section.get("enlisted"))


def is_section_open(section: dict[str, Any]) -> bool:
    return available_slots(section) > 0


def normalize_section_name(value: Any) -> str:
    text = value_to_string(value) or ""
    return " ".join(text.upper().split())


def find_target_section(course_data: Any, target_section: str) -> dict[str, Any] | None:
    if isinstance(course_data, dict):
        for value in course_data.values():
            found = find_target_section(value, target_section)
            if found is not None:
                return found
        return None

    if not isinstance(course_data, list):
        return None

    normalized_target = normalize_section_name(target_section)
    for item in course_data:
        if not isinstance(item, dict):
            continue
        if normalize_section_name(item.get("section_name")) == normalized_target:
            return item
    return None
