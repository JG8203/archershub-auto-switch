from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable

from .api import ids_equal, string_id
from .notifications import filter_sections
from .sections import is_section_open, normalize_section_name

ClashChecker = Callable[[dict[str, Any]], bool]


class PermanentJobError(RuntimeError):
    """A user-fixable job problem that should be reported once, not retried forever."""

    def __init__(self, message: str, *, complete_job: bool = True) -> None:
        super().__init__(message)
        self.complete_job = complete_job


@dataclass(frozen=True)
class AddClassDecision:
    selected_section: dict[str, Any] | None
    skipped_priority_clashes: list[dict[str, Any]]
    unavailable_priority_sections: list[str]
    fallback_used: bool
    reason: str


@dataclass(frozen=True)
class ChangeSectionDecision:
    should_submit: bool
    reason: str
    target_section: dict[str, Any] | None


@dataclass(frozen=True)
class AutomationCandidate:
    job_type: str
    course_code: str
    action: str
    reason: str
    target_section_name: str | None
    dedupe_key: str | None
    details: dict[str, Any]


def choose_add_class_section(
    course_data: Any,
    *,
    priority_sections: list[str] | None = None,
    clashes: ClashChecker | None = None,
) -> AddClassDecision:
    """Pick an add-class section without displacing any existing schedule.

    Priority sections are tried first. Open priority sections that clash are recorded
    and skipped. Fallback uses normalized section-name order.
    """
    all_sections = filter_sections(course_data)
    by_name = {normalize_section_name(row.get("section_name")): row for row in all_sections}
    priority = [normalize_section_name(name) for name in (priority_sections or []) if name]
    clashes = clashes or (lambda _section: False)
    skipped_clashes: list[dict[str, Any]] = []
    unavailable: list[str] = []
    tried = set()

    for name in priority:
        tried.add(name)
        section = by_name.get(name)
        if not section or not is_section_open(section):
            unavailable.append(name)
            continue
        if clashes(section):
            skipped_clashes.append(section)
            continue
        return AddClassDecision(section, skipped_clashes, unavailable, False, "selected priority section")

    for section in sorted(all_sections, key=lambda row: normalize_section_name(row.get("section_name"))):
        name = normalize_section_name(section.get("section_name"))
        if name in tried:
            continue
        if not is_section_open(section):
            continue
        if clashes(section):
            continue
        return AddClassDecision(section, skipped_clashes, unavailable, bool(priority), "selected fallback section")

    return AddClassDecision(None, skipped_clashes, unavailable, bool(priority), "no open non-clashing section found")


def plan_change_section(
    course_data: Any,
    *,
    current_section_id: str | None,
    target_section_name: str,
) -> ChangeSectionDecision:
    target_name = normalize_section_name(target_section_name)
    target = None
    for section in filter_sections(course_data, [target_name]):
        target = section
        break
    if target is None:
        return ChangeSectionDecision(False, "target section was not found", None)
    target_id = string_id(target.get("section_creation_id"))
    if current_section_id and ids_equal(current_section_id, target_id):
        return ChangeSectionDecision(False, "already in target section", target)
    if not is_section_open(target):
        return ChangeSectionDecision(False, "target section is not open", target)
    return ChangeSectionDecision(True, "target section is open", target)
