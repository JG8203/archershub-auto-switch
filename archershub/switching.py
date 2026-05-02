from __future__ import annotations

import time
from typing import Any

import requests

from .api import (
    first_dict,
    first_list,
    flatten_jquery_form,
    ids_equal,
    normalize_value,
    parse_number,
    post_form_json,
    post_json,
    string_id,
    value_to_string,
)
from .console import log
from .constants import AutoSwitchSubmitError, CHANGE_SECTION_TYPE_ID
from .sections import (
    extract_course_code,
    fetch_course_data,
    find_target_section,
    is_section_open,
    normalize_section_name,
)


def resolve_change_reason(
    data: dict[str, Any],
    *,
    reason_id: str | None = None,
    reason_text: str | None = None,
) -> str:
    rules = first_list(data, "change_section_rule", "change_course_rule")
    is_required = any(str(rule.get("is_mandatory", "0")) == "1" for rule in rules if isinstance(rule, dict))
    reasons = [
        reason
        for reason in first_list(data, "reason_drp", "reason_details")
        if isinstance(reason, dict) and str(reason.get("add_drop_id", "")) == "3"
    ]

    if reason_id:
        if any(ids_equal(reason.get("acd_add_drop_reason_id"), reason_id) for reason in reasons):
            return str(reason_id)
        if not reasons:
            return str(reason_id)
        raise RuntimeError(f"change-section reason id {reason_id} is not available")

    if reason_text:
        needle = reason_text.strip().lower()
        for reason in reasons:
            text = value_to_string(reason.get("reason")) or ""
            if needle in text.lower():
                return string_id(reason.get("acd_add_drop_reason_id"))
        raise RuntimeError(f"change-section reason matching {reason_text!r} was not found")

    if is_required:
        if not reasons:
            raise RuntimeError("change-section reason is mandatory, but no reasons were returned")
        selected = reasons[0]
        log(
            "change-section reason is mandatory; using first available reason "
            f"{selected.get('acd_add_drop_reason_id')}: {selected.get('reason')}"
        )
        return string_id(selected.get("acd_add_drop_reason_id"))

    return "0"


def resolve_add_drop_reason(
    data: dict[str, Any],
    add_drop_id: str,
    *,
    reason_id: str | None = None,
    reason_text: str | None = None,
    required: bool = False,
) -> str:
    reasons = [
        reason
        for reason in first_list(data, "reason_drp", "reason_details")
        if isinstance(reason, dict) and str(reason.get("add_drop_id", "")) == str(add_drop_id)
    ]

    if reason_id:
        if any(ids_equal(reason.get("acd_add_drop_reason_id"), reason_id) for reason in reasons):
            return str(reason_id)
        if not reasons:
            return str(reason_id)
        raise RuntimeError(f"reason id {reason_id} is not available for ADD_DROP_ID={add_drop_id}")

    if reason_text:
        needle = reason_text.strip().lower()
        for reason in reasons:
            text = value_to_string(reason.get("reason")) or ""
            if needle in text.lower():
                return string_id(reason.get("acd_add_drop_reason_id"))
        raise RuntimeError(f"reason matching {reason_text!r} was not found for ADD_DROP_ID={add_drop_id}")

    if required:
        if not reasons:
            raise RuntimeError(f"reason is mandatory for ADD_DROP_ID={add_drop_id}, but no reasons were returned")
        selected = reasons[0]
        log(
            f"reason is mandatory for ADD_DROP_ID={add_drop_id}; using first available reason "
            f"{selected.get('acd_add_drop_reason_id')}: {selected.get('reason')}"
        )
        return string_id(selected.get("acd_add_drop_reason_id"))

    return "0"


def get_add_drop_state(
    session: requests.Session,
    base_url: str,
    academic_session_id: str,
) -> dict[str, Any]:
    data = post_form_json(
        session,
        base_url,
        "/ApplyAddDrop/GetAllCourseForAddDrop/",
        {"academicSessionId": academic_session_id},
    )
    normalized = normalize_value(data)
    if not isinstance(normalized, dict):
        raise RuntimeError("add/drop response was not an object")
    return normalized


def find_registered_drop_course(
    state: dict[str, Any],
    course_code: str,
    course_creation_id: str,
) -> dict[str, Any]:
    matches: list[dict[str, Any]] = []
    for item in first_list(state, "drop_registered_course_list"):
        if not isinstance(item, dict):
            continue
        if ids_equal(item.get("course_creation_id"), course_creation_id):
            matches.append(item)
            continue
        item_code = extract_course_code(item)
        if item_code and item_code.upper() == course_code.upper():
            matches.append(item)

    if not matches:
        raise RuntimeError(f"{course_code} was not found in drop-eligible registered courses")
    if len(matches) > 1:
        raise RuntimeError(f"multiple drop rows found for {course_code}; refusing to guess")
    return matches[0]


def current_registered_section_name(
    state: dict[str, Any],
    course_code: str,
    course_creation_id: str,
) -> str | None:
    for item in first_list(state, "get_enlisted_subject"):
        if not isinstance(item, dict):
            continue
        item_code = extract_course_code(item)
        if ids_equal(item.get("course_creation_id"), course_creation_id) or (
            item_code and item_code.upper() == course_code.upper()
        ):
            return normalize_section_name(item.get("section_name"))
    return None


def drop_add_switch_reflected(
    session: requests.Session,
    base_url: str,
    target: dict[str, str],
    target_section_name: str,
) -> bool:
    state = add_academic_session_to_state(
        get_add_drop_state(session, base_url, target["academic_session_id"]),
        target["academic_session_id"],
    )
    current_section = current_registered_section_name(
        state,
        target["course_code"],
        target["course_creation_id"],
    )
    return current_section == normalize_section_name(target_section_name)


def validate_drop_add_course(row: dict[str, Any], course_code: str) -> None:
    blocked_reasons = []
    if str(row.get("pre_requisite_status", "0")) == "1":
        blocked_reasons.append("pre-requisite")
    if str(row.get("is_subject_withdraw", "0")) == "1":
        blocked_reasons.append("partial withdrawal")
    if str(row.get("is_mandatory", "0")) == "1":
        blocked_reasons.append("mandatory")
    if str(row.get("is_grade_publish", "0")) == "1":
        blocked_reasons.append("grade already published")
    if blocked_reasons:
        raise RuntimeError(f"{course_code} cannot be drop/add switched: {', '.join(blocked_reasons)}")


def build_course_selection_item(row: dict[str, Any], *, section_creation_id: str, active: int) -> dict[str, Any]:
    return {
        "STUDENT_ID": string_id(row.get("student_id") or "0"),
        "COURSE_CREATION_ID": string_id(row.get("course_creation_id")),
        "SECTION_CREATION_ID": section_creation_id,
        "ACTIVE": active,
        "ENROLLMENT_SEMESTER_ID": string_id(row.get("enrollment_semester_id")),
        "ENLISTMENT_TYPE": string_id(row.get("regular_restudy") or "0"),
        "CURRICULUM_CREATION_ID": string_id(row.get("curriculum_creation_id")),
        "COURSE_CATEGORY_ID": string_id(row.get("course_category_id")),
    }


def build_enlistment_additional_details(row: dict[str, Any], *, is_drop: bool) -> dict[str, Any]:
    return {
        "STUDENT_ID": string_id(row.get("student_id") or "0"),
        "IS_PRE_REQUISITE": "0" if is_drop else string_id(row.get("pre_requisite_status") or "0"),
        "IS_CO_REQUISITE": "0",
        "IS_ELECTIVE": "0",
        "IS_GLOBAL_ELECTIVE": "0",
        "IS_MANDATORY": string_id(row.get("is_mandatory") or "0"),
        "IS_EQUIVALENCE": "0",
    }


def current_enlisted_credits(state: dict[str, Any]) -> float:
    total = 0.0
    for item in first_list(state, "get_enlisted_subject"):
        if not isinstance(item, dict):
            continue
        if str(item.get("is_exclude", "0")) == "0":
            total += parse_number(item.get("credits"))
    return total


def with_student_id(row: dict[str, Any], student_id: Any) -> dict[str, Any]:
    output = dict(row)
    output["student_id"] = student_id
    return output


def build_drop_add_payload(
    *,
    state: dict[str, Any],
    drop_course: dict[str, Any],
    target_section: dict[str, Any],
    add_reason_id: str,
    drop_reason_id: str,
) -> dict[str, Any]:
    student_id = state.get("student_id") or drop_course.get("student_id") or "0"
    row = with_student_id(drop_course, student_id)
    old_section_id = string_id(row.get("section_creation_id"))
    new_section_id = string_id(target_section.get("section_creation_id"))
    if not old_section_id or not new_section_id:
        raise RuntimeError("old or target section id is missing")
    if old_section_id == new_section_id:
        raise RuntimeError("target section is the same as the current section")

    drop_item = build_course_selection_item(row, section_creation_id=old_section_id, active=2)
    add_item = build_course_selection_item(row, section_creation_id=new_section_id, active=1)
    missing = [
        name
        for name, value in {
            "course creation id": add_item["COURSE_CREATION_ID"],
            "enrollment semester id": add_item["ENROLLMENT_SEMESTER_ID"],
            "curriculum creation id": add_item["CURRICULUM_CREATION_ID"],
            "course category id": add_item["COURSE_CATEGORY_ID"],
        }.items()
        if not value
    ]
    if missing:
        raise RuntimeError(f"missing required add/drop fields: {', '.join(missing)}")

    enlisted_credits = current_enlisted_credits(state)
    credits = parse_number(row.get("credits"))
    unit = enlisted_credits
    if str(row.get("is_exclude", "0")) != "0":
        unit = enlisted_credits
    else:
        unit = (enlisted_credits - credits) + credits

    max_credit = parse_number(state.get("max_credit")) + parse_number(state.get("max_credit_can_enroll"))
    min_credit = parse_number(state.get("min_credit"))
    if max_credit and unit > max_credit:
        raise RuntimeError(f"drop/add unit total {unit:g} exceeds max credit {max_credit:g}")
    if unit < min_credit:
        raise RuntimeError(f"drop/add unit total {unit:g} is below min credit {min_credit:g}")

    return {
        "ACADEMIC_SESSION_ID": string_id(state.get("academic_session_id")),
        "ACTIVE": 1,
        "CourseSelectionList": [drop_item, add_item],
        "COMMAND_TYPE": "INSERT_UPDATE_STUDENT_ADD_DROP",
        "IS_ADD_REASON_ID": add_reason_id,
        "IS_DROP_REASON_ID": drop_reason_id,
        "IS_FINAL_CONFIRM": 0,
        "IS_APPROVAL": string_id(state.get("is_approval") or "0"),
        "UNIT": f"{unit:g}",
        "IS_STUDENT_CONFIRMATION": string_id(state.get("is_student_confirmation") or "0"),
        "EnlistmentAdditionalDetails": [
            build_enlistment_additional_details(row, is_drop=True),
            build_enlistment_additional_details(row, is_drop=False),
        ],
    }


def find_add_course(
    state: dict[str, Any],
    course_code: str,
    course_creation_id: str,
) -> dict[str, Any]:
    matches: list[dict[str, Any]] = []
    for item in first_list(state, "add_offer_course_list", "course_details", "add_course_list"):
        if not isinstance(item, dict):
            continue
        if ids_equal(item.get("course_creation_id"), course_creation_id):
            matches.append(item)
            continue
        item_code = extract_course_code(item)
        if item_code and item_code.upper() == course_code.upper():
            matches.append(item)

    if not matches:
        raise RuntimeError(f"{course_code} was not found in add-eligible courses")
    if len(matches) > 1:
        raise RuntimeError(f"multiple add rows found for {course_code}; refusing to guess")
    return matches[0]


def build_add_course_payload(
    *,
    state: dict[str, Any],
    add_course: dict[str, Any],
    target_section: dict[str, Any],
    add_reason_id: str,
) -> dict[str, Any]:
    student_id = state.get("student_id") or add_course.get("student_id") or "0"
    row = with_student_id(add_course, student_id)
    new_section_id = string_id(target_section.get("section_creation_id"))
    if not new_section_id:
        raise RuntimeError("target section id is missing")

    add_item = build_course_selection_item(row, section_creation_id=new_section_id, active=1)
    missing = [
        name
        for name, value in {
            "course creation id": add_item["COURSE_CREATION_ID"],
            "enrollment semester id": add_item["ENROLLMENT_SEMESTER_ID"],
            "curriculum creation id": add_item["CURRICULUM_CREATION_ID"],
            "course category id": add_item["COURSE_CATEGORY_ID"],
        }.items()
        if not value
    ]
    if missing:
        raise RuntimeError(f"missing required add-course fields: {', '.join(missing)}")

    enlisted_credits = current_enlisted_credits(state)
    credits = parse_number(row.get("credits"))
    unit = enlisted_credits if str(row.get("is_exclude", "0")) != "0" else enlisted_credits + credits

    max_credit = parse_number(state.get("max_credit")) + parse_number(state.get("max_credit_can_enroll"))
    if max_credit and unit > max_credit:
        raise RuntimeError(f"add-course unit total {unit:g} exceeds max credit {max_credit:g}")

    return {
        "ACADEMIC_SESSION_ID": string_id(state.get("academic_session_id")),
        "ACTIVE": 1,
        "CourseSelectionList": [add_item],
        "COMMAND_TYPE": "INSERT_UPDATE_STUDENT_ADD_DROP",
        "IS_ADD_REASON_ID": add_reason_id,
        "IS_DROP_REASON_ID": "0",
        "IS_FINAL_CONFIRM": 0,
        "IS_APPROVAL": string_id(state.get("is_approval") or "0"),
        "UNIT": f"{unit:g}",
        "IS_STUDENT_CONFIRMATION": string_id(state.get("is_student_confirmation") or "0"),
        "EnlistmentAdditionalDetails": [build_enlistment_additional_details(row, is_drop=False)],
    }


def has_course_clash(
    session: requests.Session,
    base_url: str,
    academic_session_id: str,
    course_creation_id: str,
    section_creation_id: str,
    campus_no: str,
) -> bool:
    try:
        check_course_clash(
            session,
            base_url,
            academic_session_id,
            course_creation_id,
            section_creation_id,
            campus_no,
        )
        return False
    except RuntimeError:
        return True


def add_academic_session_to_state(state: dict[str, Any], academic_session_id: str) -> dict[str, Any]:
    output = dict(state)
    output["academic_session_id"] = academic_session_id
    return output


def get_change_section_state(
    session: requests.Session,
    base_url: str,
    academic_session_id: str,
) -> dict[str, Any]:
    data = post_form_json(
        session,
        base_url,
        "/ApplyAddDrop/GetAddDropCChangeOfSection/",
        {"typeId": CHANGE_SECTION_TYPE_ID, "AcademicSessionId": academic_session_id},
    )
    normalized = normalize_value(data)
    if not isinstance(normalized, dict):
        raise RuntimeError("change-section response was not an object")
    return normalized


def get_course_wise_section_data(
    session: requests.Session,
    base_url: str,
    academic_session_id: str,
    course_creation_id: str,
    is_cross_offer: str,
    grid_type: str,
) -> dict[str, Any]:
    course_list = [
        {
            "COURSE_CREATION_ID": course_creation_id,
            "CROSS_OFFER": is_cross_offer,
            "GRID_TYPE": grid_type,
        }
    ]
    data = post_form_json(
        session,
        base_url,
        "/Enlistment/GetCourseWiseSectionData/",
        flatten_jquery_form({"CourseList": course_list, "academicSessionId": academic_session_id}),
    )
    normalized = normalize_value(data)
    if not isinstance(normalized, dict):
        raise RuntimeError("course-wise section response was not an object")
    return normalized


def find_current_enlisted_course(
    state: dict[str, Any],
    course_code: str,
    course_creation_id: str,
) -> dict[str, Any]:
    matches: list[dict[str, Any]] = []
    for item in first_list(state, "bind_section"):
        if not isinstance(item, dict):
            continue
        if ids_equal(item.get("course_creation_id"), course_creation_id):
            matches.append(item)
            continue
        item_code = extract_course_code(item)
        if item_code and item_code.upper() == course_code.upper():
            matches.append(item)

    if not matches:
        raise RuntimeError(f"student is not currently enlisted in {course_code}")
    if len(matches) > 1:
        raise RuntimeError(f"multiple current enlisted rows found for {course_code}; refusing to guess")
    return matches[0]


def build_change_section_payload(
    *,
    state: dict[str, Any],
    current_course: dict[str, Any],
    target_section: dict[str, Any],
    reason_id: str,
) -> dict[str, Any]:
    rule = first_dict(state, "change_section_rule")
    if not rule:
        raise RuntimeError("change-section rule was not returned; change section may be unavailable")

    old_section_id = string_id(current_course.get("section_creation_id"))
    new_section_id = string_id(target_section.get("section_creation_id"))
    course_creation_id = string_id(current_course.get("course_creation_id"))
    enrollment_semester_id = string_id(current_course.get("enrollment_semester_id"))
    curriculum_creation_id = string_id(current_course.get("curriculum_creation_id"))
    campus_no = string_id(current_course.get("parent_campus_no"))
    is_approval = string_id(rule.get("is_approval_applicable") or current_course.get("is_approval_applicable") or "0")

    missing = [
        name
        for name, value in {
            "old section id": old_section_id,
            "new section id": new_section_id,
            "course creation id": course_creation_id,
            "enrollment semester id": enrollment_semester_id,
            "curriculum creation id": curriculum_creation_id,
            "campus no": campus_no,
        }.items()
        if not value
    ]
    if missing:
        raise RuntimeError(f"missing required change-section fields: {', '.join(missing)}")

    return {
        "ACADEMIC_SESSION_ID": string_id(current_course.get("academic_session_id") or rule.get("academic_session_id")),
        "CAMPUSNO": campus_no,
        "CURRICULUM_CREATION_ID": curriculum_creation_id,
        "DEMANDPG_ID": string_id(current_course.get("demandpg_id") or "0"),
        "REASON_ID": reason_id,
        "TYPE_ID": CHANGE_SECTION_TYPE_ID,
        "ENROLLMENT_SEMESTER_ID": enrollment_semester_id,
        "IS_APPROVAL_APPLICABLE": is_approval,
        "IS_FEE_APPLICABLE": string_id(rule.get("is_add_fee") or "0"),
        "ADD_DROP_RULE_ID": string_id(rule.get("add_drop_rule_id") or "0"),
        "SaveSectionDetails": [
            {
                "OLD_SECTION_CREATION_ID": old_section_id,
                "NEW_SECTION_CREATION_ID": new_section_id,
                "COURSE_CREATION_ID": course_creation_id,
                "ENROLLMENT_SEMESTER_ID": enrollment_semester_id,
                "CURRICULUM_CREATION_ID": curriculum_creation_id,
            }
        ],
    }


def ensure_academic_session(payload: dict[str, Any], academic_session_id: str) -> None:
    if not payload.get("ACADEMIC_SESSION_ID"):
        payload["ACADEMIC_SESSION_ID"] = academic_session_id


def check_course_clash(
    session: requests.Session,
    base_url: str,
    academic_session_id: str,
    course_creation_id: str,
    section_creation_id: str,
    campus_no: str,
) -> None:
    course_list = [
        {
            "COURSE_CREATION_ID": course_creation_id,
            "SECTION_CREATION_ID": section_creation_id,
            "CAMPUSNO": campus_no,
        }
    ]
    data = post_form_json(
        session,
        base_url,
        "/Enlistment/GetCourseClashDetails/",
        flatten_jquery_form({"academicSessionId": academic_session_id, "CourseList": course_list}),
    )
    normalized = normalize_value(data)
    if not isinstance(normalized, list) or not normalized or not isinstance(normalized[0], dict):
        raise RuntimeError("course clash response had an unexpected shape")

    status = normalized[0].get("status")
    if str(status) != "1":
        raise RuntimeError(f"target section has a schedule clash or server warning: {status}")


def submit_change_section(session: requests.Session, base_url: str, payload: dict[str, Any]) -> Any:
    response = post_json(session, base_url, "/ApplyAddDrop/SaveChangeOfCourse/", payload)
    if str(response).strip() != "1":
        raise RuntimeError(f"change-section submit was not accepted: {response!r}")
    return response


def submit_add_drop(session: requests.Session, base_url: str, payload: dict[str, Any]) -> Any:
    response = post_json(session, base_url, "/ApplyAddDrop/SaveEnlistmentData/", payload)
    if str(response).strip() not in {"0", "1"}:
        raise RuntimeError(f"add/drop submit was not accepted: {response!r}")
    return response


def maybe_submit_target_switch(
    session: requests.Session,
    base_url: str,
    target: dict[str, str],
    target_section_name: str,
    target_section: dict[str, Any],
    *,
    change_reason_id: str | None = None,
    change_reason_text: str | None = None,
) -> bool:
    state = get_change_section_state(session, base_url, target["academic_session_id"])
    current_course = find_current_enlisted_course(state, target["course_code"], target["course_creation_id"])
    current_section_id = string_id(current_course.get("section_creation_id"))
    target_section_id = string_id(target_section.get("section_creation_id"))

    if current_section_id == target_section_id:
        log(f"already enlisted in {target['course_code']} {target_section_name}; nothing to do")
        return True

    section_data = get_course_wise_section_data(
        session,
        base_url,
        target["academic_session_id"],
        target["course_creation_id"],
        target["is_cross_offer"],
        target["grid_type"],
    )
    section_details = first_list(section_data, "section_details")
    if section_details and not any(
        isinstance(section, dict) and ids_equal(section.get("section_creation_id"), target_section_id)
        for section in section_details
    ):
        raise RuntimeError(f"target section id {target_section_id} is not available for change-section")

    reason_id = resolve_change_reason(
        state,
        reason_id=change_reason_id,
        reason_text=change_reason_text,
    )
    payload = build_change_section_payload(
        state=state,
        current_course=current_course,
        target_section=target_section,
        reason_id=reason_id,
    )
    ensure_academic_session(payload, target["academic_session_id"])
    detail = payload["SaveSectionDetails"][0]

    check_course_clash(
        session,
        base_url,
        target["academic_session_id"],
        detail["COURSE_CREATION_ID"],
        detail["NEW_SECTION_CREATION_ID"],
        payload["CAMPUSNO"],
    )

    log(
        "submitting automatic change-section request "
        f"{target['course_code']} {current_section_id} -> {target_section_name} ({target_section_id})"
    )
    try:
        submit_change_section(session, base_url, payload)
    except Exception as exc:
        raise AutoSwitchSubmitError(
            "change-section submit was attempted but did not return a clean success; "
            "stopping to avoid duplicate submissions"
        ) from exc
    log("change-section request accepted")
    return True


def maybe_submit_drop_add_switch(
    session: requests.Session,
    base_url: str,
    target: dict[str, str],
    target_section_name: str,
    target_section: dict[str, Any],
    *,
    add_reason_id: str | None = None,
    add_reason_text: str | None = None,
    drop_reason_id: str | None = None,
    drop_reason_text: str | None = None,
) -> bool:
    add_drop_state = add_academic_session_to_state(
        get_add_drop_state(session, base_url, target["academic_session_id"]),
        target["academic_session_id"],
    )
    drop_course = find_registered_drop_course(
        add_drop_state,
        target["course_code"],
        target["course_creation_id"],
    )
    validate_drop_add_course(drop_course, target["course_code"])

    current_section_id = string_id(drop_course.get("section_creation_id"))
    target_section_id = string_id(target_section.get("section_creation_id"))
    if current_section_id == target_section_id:
        log(f"already enlisted in {target['course_code']} {target_section_name}; nothing to do")
        return True

    section_data = get_course_wise_section_data(
        session,
        base_url,
        target["academic_session_id"],
        target["course_creation_id"],
        target["is_cross_offer"],
        target["grid_type"],
    )
    section_details = first_list(section_data, "section_details")
    if section_details and not any(
        isinstance(section, dict) and ids_equal(section.get("section_creation_id"), target_section_id)
        for section in section_details
    ):
        raise RuntimeError(f"target section id {target_section_id} is not available for add/drop")

    reason_required = str(add_drop_state.get("is_mandatory", "0")) == "1"
    add_reason_id = resolve_add_drop_reason(
        add_drop_state,
        "1",
        reason_id=add_reason_id,
        reason_text=add_reason_text,
        required=reason_required,
    )
    drop_reason_id = resolve_add_drop_reason(
        add_drop_state,
        "2",
        reason_id=drop_reason_id,
        reason_text=drop_reason_text,
        required=reason_required,
    )
    payload = build_drop_add_payload(
        state=add_drop_state,
        drop_course=drop_course,
        target_section=target_section,
        add_reason_id=add_reason_id,
        drop_reason_id=drop_reason_id,
    )

    clash_list = []
    for row in first_list(add_drop_state, "drop_registered_course_list"):
        if not isinstance(row, dict):
            continue
        section_id = target_section_id if ids_equal(row.get("course_creation_id"), target["course_creation_id"]) else string_id(row.get("section_creation_id"))
        clash_list.append(
            {
                "COURSE_CREATION_ID": string_id(row.get("course_creation_id")),
                "SECTION_CREATION_ID": section_id,
                "CAMPUSNO": string_id(add_drop_state.get("campusno") or row.get("parent_campus_no") or target["campus_id"]),
            }
        )
    if not clash_list:
        clash_list.append(
            {
                "COURSE_CREATION_ID": target["course_creation_id"],
                "SECTION_CREATION_ID": target_section_id,
                "CAMPUSNO": string_id(add_drop_state.get("campusno") or target["campus_id"]),
            }
        )

    clash_response = post_form_json(
        session,
        base_url,
        "/Enlistment/GetCourseClashDetails/",
        flatten_jquery_form({"academicSessionId": target["academic_session_id"], "CourseList": clash_list}),
    )
    normalized = normalize_value(clash_response)
    if not isinstance(normalized, list) or not normalized or not isinstance(normalized[0], dict):
        raise RuntimeError("course clash response had an unexpected shape")
    status = normalized[0].get("status")
    if str(status) != "1":
        raise RuntimeError(f"target drop/add has a schedule clash or server warning: {status}")

    log(
        "submitting automatic drop/add request "
        f"{target['course_code']} {current_section_id} -> {target_section_name} ({target_section_id})"
    )
    while True:
        try:
            submit_add_drop(session, base_url, payload)
            log("drop/add request accepted")
            return True
        except (requests.exceptions.Timeout, requests.exceptions.ConnectionError) as exc:
            log(
                "drop/add submit did not return a clean response "
                f"({exc}); waiting 10 seconds before checking server state"
            )
            time.sleep(10)

            try:
                if drop_add_switch_reflected(session, base_url, target, target_section_name):
                    log(
                        "drop/add submit appears successful after timeout; "
                        f"{target['course_code']} is now in {target_section_name}"
                    )
                    return True
            except Exception as verify_exc:
                log(f"could not verify post-submit state yet: {verify_exc}")

            try:
                course_data = fetch_course_data(session, base_url, target)
                refreshed_section = find_target_section(course_data, target_section_name)
                if refreshed_section is None:
                    log("target section disappeared after submit timeout; returning to normal polling")
                    return False
                if not is_section_open(refreshed_section):
                    log("target section is no longer open after submit timeout; returning to normal polling")
                    return False
            except Exception as availability_exc:
                log(f"could not recheck target availability after timeout: {availability_exc}")
                return False

            log("target section is still open and switch is not reflected yet; retrying submit")
        except Exception as exc:
            log(
                "drop/add submit returned an error response "
                f"({exc}); waiting 10 seconds before verifying server state"
            )
            time.sleep(10)
            try:
                if drop_add_switch_reflected(session, base_url, target, target_section_name):
                    log(
                        "drop/add switch is reflected despite the error response; "
                        f"{target['course_code']} is now in {target_section_name}"
                    )
                    return True
            except Exception as verify_exc:
                log(f"could not verify post-error state: {verify_exc}")
            raise
