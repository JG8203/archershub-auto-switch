from __future__ import annotations

import html
import json
import re
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Any
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

from .api import flatten_jquery_form, normalize_value, parse_number, post_form_json, string_id, value_to_string
from .constants import TIMEOUT
from .sections import (
    available_slots,
    current_session_id,
    effective_capacity,
    extract_course_code,
    extract_course_creation_id,
    first_string_field,
    normalize_section_name,
)


@dataclass(frozen=True)
class CourseSearchContext:
    campus_id: str
    academic_session_id: str


@dataclass(frozen=True)
class CourseSearchResult:
    course_code: str
    course_name: str
    course_creation_id: str
    campus_id: str
    academic_session_id: str
    is_cross_offer: str = "0"
    grid_type: str = "0"

    def as_target(self) -> dict[str, str]:
        return {
            "course_code": self.course_code,
            "course_creation_id": self.course_creation_id,
            "campus_id": self.campus_id,
            "academic_session_id": self.academic_session_id,
            "is_cross_offer": self.is_cross_offer,
            "grid_type": self.grid_type,
        }


def course_display_name(item: dict[str, Any]) -> str:
    for field in ("course_name", "text", "subject_name", "name"):
        value = value_to_string(item.get(field))
        if value:
            return " ".join(value.split())
    code = extract_course_code(item)
    return code or "Unknown course"


def course_search_context(session: requests.Session, base_url: str) -> CourseSearchContext:
    context = normalize_value(post_form_json(session, base_url, "/CourseFinder/GetAllDropDownList/", {}))
    if not isinstance(context, dict):
        raise RuntimeError("course finder dropdown response was not an object")
    campus_id = first_string_field(context, "campus_drp", ["campusno", "campus_no", "value", "id"])
    if not campus_id:
        raise RuntimeError("unable to determine campus id")
    academic_session_id = current_session_id(context)
    if not academic_session_id:
        raise RuntimeError("unable to determine current academic session id")
    return CourseSearchContext(campus_id=campus_id, academic_session_id=academic_session_id)


def fetch_course_options(session: requests.Session, base_url: str, context: CourseSearchContext) -> list[CourseSearchResult]:
    data = normalize_value(
        post_form_json(
            session,
            base_url,
            "/CourseFinder/GetCourseList/",
            {"Campusno": context.campus_id, "AcademicSession": context.academic_session_id},
        )
    )
    if not isinstance(data, dict) or not isinstance(data.get("course_drp"), list):
        raise RuntimeError("course list response did not contain course_drp")
    results: list[CourseSearchResult] = []
    seen: set[tuple[str, str]] = set()
    for item in data["course_drp"]:
        if not isinstance(item, dict):
            continue
        code = extract_course_code(item)
        creation_id = extract_course_creation_id(item)
        if not code or not creation_id:
            continue
        key = (code.upper(), str(creation_id))
        if key in seen:
            continue
        seen.add(key)
        results.append(
            CourseSearchResult(
                course_code=code.upper(),
                course_name=course_display_name(item),
                course_creation_id=str(creation_id),
                campus_id=context.campus_id,
                academic_session_id=context.academic_session_id,
                is_cross_offer=value_to_string(item.get("is_cross_offer")) or "0",
                grid_type=value_to_string(item.get("grid_type")) or "0",
            )
        )
    return results


def search_courses(courses: list[CourseSearchResult], query: str, *, limit: int = 50) -> list[CourseSearchResult]:
    needle = " ".join(query.upper().split())
    if not needle:
        return []

    def rank(course: CourseSearchResult) -> tuple[int, str, str]:
        code = course.course_code.upper()
        name = course.course_name.upper()
        haystack = f"{code} {name}"
        if code == needle:
            bucket = 0
        elif code.startswith(needle):
            bucket = 1
        elif needle in code:
            bucket = 2
        elif name.startswith(needle):
            bucket = 3
        elif needle in haystack:
            bucket = 4
        else:
            bucket = 99
        return (bucket, code, name)

    matched = [course for course in courses if rank(course)[0] < 99]
    return sorted(matched, key=rank)[:limit]


def compact_course(course: CourseSearchResult) -> dict[str, str]:
    return {
        "course_code": course.course_code,
        "course_name": course.course_name,
        "course_creation_id": course.course_creation_id,
        "campus_id": course.campus_id,
        "academic_session_id": course.academic_session_id,
        "is_cross_offer": course.is_cross_offer,
        "grid_type": course.grid_type,
    }


def course_from_dict(data: dict[str, Any]) -> CourseSearchResult:
    return CourseSearchResult(
        course_code=str(data.get("course_code") or "").upper(),
        course_name=str(data.get("course_name") or data.get("course_code") or "Unknown course"),
        course_creation_id=str(data.get("course_creation_id") or ""),
        campus_id=str(data.get("campus_id") or ""),
        academic_session_id=str(data.get("academic_session_id") or ""),
        is_cross_offer=str(data.get("is_cross_offer") or "0"),
        grid_type=str(data.get("grid_type") or "0"),
    )


def section_key(section: dict[str, Any]) -> tuple[str, str, str]:
    return (
        string_id(section.get("course_creation_id")),
        string_id(section.get("section_creation_id")),
        string_id(section.get("batch_creation_id") or "0"),
    )


def teacher_from_row(row: dict[str, Any]) -> str:
    for field in ("main_teacher", "teacher", "teacher_name"):
        text = value_to_string(row.get(field))
        if text and text.strip() and text.strip() != "-":
            return " ".join(text.split())
    return ""


def clean_teacher_text(value: str) -> str:
    text = html.unescape(value)
    text = re.sub(r"<[^>]+>", " ", text)
    text = re.sub(r"[\r\n\t]+", " ", text)
    text = re.sub(r"\s+", " ", text).strip(" :-–—|")
    if not text or text == "-":
        return ""
    return text


def plain_html_text(value: str) -> str:
    soup = BeautifulSoup(value, "html.parser")
    for tag in soup(["script", "style", "noscript"]):
        tag.decompose()
    return clean_teacher_text(soup.get_text("\n"))


def html_text_lines(value: str) -> list[str]:
    soup = BeautifulSoup(value, "html.parser")
    for tag in soup(["script", "style", "noscript"]):
        tag.decompose()
    return [clean_teacher_text(line) for line in soup.get_text("\n").splitlines() if clean_teacher_text(line)]


TEACHER_LABEL_RE = re.compile(
    r"(?:main\s*)?(?:teacher|faculty|instructor)(?:\s*name)?\s*[:\-–—]\s*([^\n\r|<>{}\\[\\]]+)",
    re.IGNORECASE,
)
TEACHER_ICON_RE = re.compile(r"(?:👤|&#128100;|&[#a-zA-Z0-9]+;)\s*([A-Z][^\n\r|<>{}\\[\\]]{2,80})")
SECTION_LABEL_RE = re.compile(r"section\s*[:\-–—]\s*([^\n\r|<>{}\\[\\]]+)", re.IGNORECASE)


def extract_labeled_value(value: str, label: str) -> str:
    label_re = re.compile(rf"^{re.escape(label)}\s*[:\-–—]\s*(.+)$", re.IGNORECASE)
    for line in html_text_lines(value):
        match = label_re.search(line)
        if match:
            return clean_teacher_text(match.group(1))
    return ""


def extract_teacher_from_text(value: str) -> str:
    teacher = extract_labeled_value(value, "Teacher") or extract_labeled_value(value, "Main Teacher")
    if teacher:
        return teacher
    text = plain_html_text(value)
    for regex in (TEACHER_LABEL_RE, TEACHER_ICON_RE):
        match = regex.search(text)
        if match:
            teacher = clean_teacher_text(match.group(1))
            if teacher:
                return teacher
    return ""


def extract_section_from_text(value: str) -> str:
    section = extract_labeled_value(value, "Section")
    if section:
        return normalize_section_name(section)
    text = plain_html_text(value)
    match = SECTION_LABEL_RE.search(text)
    return normalize_section_name(match.group(1)) if match else ""


def schedule_rows_from_html(value: str, sections: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Extract teacher names from the HTML form of CourseFinder/GetScheduleData.

    The endpoint has been observed returning markup instead of JSON. Treat all
    markup as untrusted display text: strip active content, only keep text, and
    match teacher labels/icons near the selected section's identifiers/name.
    """

    soup = BeautifulSoup(value, "html.parser")
    for tag in soup(["script", "style", "noscript"]):
        tag.decompose()

    rows: list[dict[str, Any]] = []
    full_text = soup.get_text("\n")
    for section in sections:
        course_id, section_id, batch_id = section_key(section)
        section_name = normalize_section_name(section.get("section_name"))
        candidates: list[str] = []

        for attr_value in (section_id, section_name):
            if not attr_value:
                continue
            found = soup.find_all(string=re.compile(re.escape(attr_value), re.IGNORECASE))
            for node in found:
                parent = getattr(node, "parent", None)
                for _ in range(4):
                    if parent is None:
                        break
                    text = parent.get_text("\n", strip=True)
                    if text:
                        candidates.append(text)
                    parent = parent.parent

        for marker in (section_id, section_name):
            if not marker:
                continue
            for match in re.finditer(re.escape(marker), full_text, re.IGNORECASE):
                start = max(0, match.start() - 600)
                end = min(len(full_text), match.end() + 900)
                candidates.append(full_text[start:end])

        teacher = ""
        for candidate in candidates:
            teacher = extract_teacher_from_text(candidate)
            if teacher:
                break
        if teacher:
            rows.append(
                {
                    "course_creation_id": course_id,
                    "section_creation_id": section_id,
                    "batch_creation_id": batch_id,
                    "main_teacher": teacher,
                }
            )
    return rows


def normalize_schedule_response(value: Any, sections: list[dict[str, Any]]) -> Any:
    if isinstance(value, (list, dict)):
        return normalize_value(value)
    if isinstance(value, bytes):
        value = value.decode("utf-8", errors="replace")
    if not isinstance(value, str):
        return value
    text = value.strip()
    if not text:
        return []
    try:
        return normalize_value(json.loads(text))
    except ValueError:
        pass
    return schedule_rows_from_html(text, sections)


def schedule_row_teacher(row: dict[str, Any]) -> str:
    return teacher_from_row(row) or extract_teacher_from_text(str(row.get("course_name") or ""))


def schedule_row_section_name(row: dict[str, Any]) -> str:
    return normalize_section_name(row.get("section_name")) or extract_section_from_text(str(row.get("course_name") or ""))


def merge_revealed_teachers(sections: list[dict[str, Any]], schedule_rows: Any) -> list[dict[str, Any]]:
    schedule_rows = normalize_schedule_response(schedule_rows, sections)
    if not isinstance(schedule_rows, list):
        return sections
    teachers: dict[tuple[str, str, str], str] = {}
    teachers_by_section_name: dict[str, str] = {}
    for row in schedule_rows:
        if not isinstance(row, dict):
            continue
        normalized = normalize_value(row)
        if not isinstance(normalized, dict):
            continue
        teacher = schedule_row_teacher(normalized)
        if teacher:
            teachers.setdefault(section_key(normalized), teacher)
            section_name = schedule_row_section_name(normalized)
            if section_name:
                teachers_by_section_name.setdefault(section_name, teacher)
    if not teachers:
        if not teachers_by_section_name:
            return sections
    if not teachers and not teachers_by_section_name:
        return sections
    merged: list[dict[str, Any]] = []
    for section in sections:
        updated = dict(section)
        teacher = teachers.get(section_key(updated)) or teachers_by_section_name.get(normalize_section_name(updated.get("section_name")))
        current = teacher_from_row(updated)
        if teacher and not current:
            updated["main_teacher"] = teacher
        merged.append(updated)
    return merged


def _parse_date(value: Any) -> date | None:
    text = value_to_string(value)
    if not text:
        return None
    text = text.strip()
    for fmt in ("%m/%d/%Y", "%d/%m/%Y", "%Y-%m-%d", "%Y/%m/%d", "%m-%d-%Y", "%d-%m-%Y"):
        try:
            return datetime.strptime(text, fmt).date()
        except ValueError:
            pass
    return None


def schedule_date_range(sections: list[dict[str, Any]]) -> tuple[str, str]:
    starts = [_parse_date(section.get("start_date")) for section in sections]
    ends = [_parse_date(section.get("end_date")) for section in sections]
    valid_starts = [item for item in starts if item is not None]
    valid_ends = [item for item in ends if item is not None]
    if valid_starts and valid_ends:
        return min(valid_starts).isoformat(), max(valid_ends).isoformat()
    today = date.today()
    start = today - timedelta(days=today.weekday())
    end = start + timedelta(days=6)
    return start.isoformat(), end.isoformat()


def reveal_teachers_with_schedule_data(
    session: requests.Session,
    base_url: str,
    course: CourseSearchResult,
    sections: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    if not sections:
        return sections
    start_date, end_date = schedule_date_range(sections)
    enlistment_schedule = []
    for section in sections:
        course_creation_id, section_creation_id, batch_creation_id = section_key(section)
        if not course_creation_id or not section_creation_id:
            continue
        enlistment_schedule.append(
            {
                "COURSE_CREATION_ID": int(course_creation_id) if course_creation_id.isdigit() else course_creation_id,
                "SECTION_CREATION_ID": int(section_creation_id) if section_creation_id.isdigit() else section_creation_id,
                "BATCH_CREATION_ID": int(batch_creation_id) if batch_creation_id.isdigit() else batch_creation_id,
                "CAMPUSNO": course.campus_id,
            }
        )
    if not enlistment_schedule:
        return sections
    response = session.post(
        urljoin(base_url.rstrip("/") + "/", "CourseFinder/GetScheduleData/"),
        data=flatten_jquery_form(
            {
                "STARTDATE": start_date,
                "ENDDATE": end_date,
                "ACADEMICSESSIONID": int(course.academic_session_id) if course.academic_session_id.isdigit() else course.academic_session_id,
                "enlistmentSchedule": enlistment_schedule,
            }
        ),
        headers={"X-Requested-With": "XMLHttpRequest"},
        timeout=TIMEOUT,
    )
    response.raise_for_status()
    content_type = response.headers.get("content-type", "").lower()
    data: Any
    if "json" in content_type:
        data = response.json()
    else:
        data = response.text
    return merge_revealed_teachers(sections, data)


def section_summary(section: dict[str, Any]) -> dict[str, Any]:
    teacher = teacher_from_row(section) or "-"
    return {
        "section_name": normalize_section_name(section.get("section_name")),
        "section_creation_id": string_id(section.get("section_creation_id")),
        "batch_creation_id": string_id(section.get("batch_creation_id") or "0"),
        "capacity": effective_capacity(section),
        "enlisted": parse_number(section.get("enlisted")),
        "available": available_slots(section),
        "schedule": str(section.get("schedule") or "-"),
        "teacher": teacher,
    }
