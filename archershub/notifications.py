from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .sections import available_slots, effective_capacity, normalize_section_name


@dataclass(frozen=True)
class SectionSummary:
    section_name: str
    section_creation_id: str
    capacity: float
    enlisted: float
    available: float
    schedule: str
    teacher: str

    @property
    def is_open(self) -> bool:
        return self.available > 0


def section_key(section: dict[str, Any]) -> str:
    return str(section.get("section_creation_id") or normalize_section_name(section.get("section_name")))


def summarize_section(section: dict[str, Any]) -> SectionSummary:
    capacity = effective_capacity(section)
    enlisted = float(section.get("enlisted") or 0)
    return SectionSummary(
        section_name=normalize_section_name(section.get("section_name")),
        section_creation_id=str(section.get("section_creation_id") or ""),
        capacity=capacity,
        enlisted=enlisted,
        available=available_slots(section),
        schedule=str(section.get("schedule") or ""),
        teacher=str(section.get("main_teacher") or section.get("teacher") or "-"),
    )


def filter_sections(course_data: Any, section_names: list[str] | None = None) -> list[dict[str, Any]]:
    wanted = {normalize_section_name(name) for name in (section_names or []) if name}
    found: list[dict[str, Any]] = []

    def visit(value: Any) -> None:
        if isinstance(value, dict):
            if "section_name" in value:
                name = normalize_section_name(value.get("section_name"))
                if not wanted or name in wanted:
                    found.append(value)
                return
            for item in value.values():
                visit(item)
        elif isinstance(value, list):
            for item in value:
                visit(item)

    visit(course_data)
    return sorted(found, key=lambda row: normalize_section_name(row.get("section_name")))


def compact_sections(sections: list[dict[str, Any]]) -> list[dict[str, Any]]:
    compact: list[dict[str, Any]] = []
    for section in sections:
        summary = summarize_section(section)
        compact.append(
            {
                "key": section_key(section),
                "section_name": summary.section_name,
                "section_creation_id": summary.section_creation_id,
                "capacity": summary.capacity,
                "enlisted": summary.enlisted,
                "available": summary.available,
                "is_open": summary.is_open,
                "schedule": summary.schedule,
                "teacher": summary.teacher,
            }
        )
    return compact


def diff_sections(previous: list[dict[str, Any]] | None, current: list[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    previous = previous or []
    old_by_key = {str(row.get("key")): row for row in previous}
    new_by_key = {str(row.get("key")): row for row in current}

    added = [row for key, row in new_by_key.items() if key not in old_by_key]
    removed = [row for key, row in old_by_key.items() if key not in new_by_key]
    availability: list[dict[str, Any]] = []
    enrollment: list[dict[str, Any]] = []

    for key, new in new_by_key.items():
        old = old_by_key.get(key)
        if not old:
            continue
        if bool(old.get("is_open")) != bool(new.get("is_open")):
            availability.append({"old": old, "new": new})
        elif old.get("available") != new.get("available") or old.get("capacity") != new.get("capacity") or old.get("enlisted") != new.get("enlisted"):
            enrollment.append({"old": old, "new": new})

    return {"added": added, "removed": removed, "availability": availability, "enrollment": enrollment}


def has_changes(changes: dict[str, list[dict[str, Any]]]) -> bool:
    return any(changes.get(key) for key in ("added", "removed", "availability", "enrollment"))


def format_section_line(section: dict[str, Any]) -> str:
    status = "OPEN" if section.get("is_open") else "FULL"
    return (
        f"{section.get('section_name', '?')}: {status} "
        f"({section.get('available', '?'):g} slots, "
        f"{section.get('enlisted', '?'):g}/{section.get('capacity', '?'):g})\n"
        f"Schedule: {section.get('schedule') or '-'}\n"
        f"Teacher: {section.get('teacher') or '-'}"
    )


def format_changes(course_code: str, changes: dict[str, list[dict[str, Any]]]) -> str:
    lines = [f"Updates for {course_code}"]
    if changes.get("added"):
        lines.append("\nNew sections:")
        lines.extend(format_section_line(row) for row in changes["added"])
    if changes.get("removed"):
        lines.append("\nRemoved sections:")
        lines.extend(format_section_line(row) for row in changes["removed"])
    if changes.get("availability"):
        lines.append("\nAvailability changes:")
        for change in changes["availability"]:
            old = change["old"]
            new = change["new"]
            lines.append(
                f"{new.get('section_name')}: "
                f"{'OPEN' if old.get('is_open') else 'FULL'} -> {'OPEN' if new.get('is_open') else 'FULL'} "
                f"({old.get('available'):g} -> {new.get('available'):g} slots)"
            )
    if changes.get("enrollment"):
        lines.append("\nEnrollment/capacity changes:")
        for change in changes["enrollment"]:
            old = change["old"]
            new = change["new"]
            lines.append(
                f"{new.get('section_name')}: "
                f"{old.get('enlisted'):g}/{old.get('capacity'):g} -> {new.get('enlisted'):g}/{new.get('capacity'):g} "
                f"({old.get('available'):g} -> {new.get('available'):g} slots)"
            )
    return "\n".join(lines)
