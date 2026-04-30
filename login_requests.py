from __future__ import annotations

import argparse
import base64
import getpass
import json
import os
import re
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path
from random import uniform
from typing import Any
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup
from cryptography.hazmat.primitives import hashes, padding
from cryptography.hazmat.primitives.asymmetric import padding as asym_padding, rsa
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

UA = "Mozilla/5.0"
TIMEOUT = 30
DEFAULT_BASE_URL = "https://archershub.dlsu.edu.ph"
DEFAULT_LOGIN_PATH = "StudentLogin"
DEFAULT_INTERVAL_SECS = 30
DEFAULT_MAX_LOGIN_ATTEMPTS = 5
CHANGE_SECTION_TYPE_ID = "2"
SWITCH_STRATEGY_CHANGE_SECTION = "change-section"
SWITCH_STRATEGY_DROP_ADD = "drop-add"
CAPTCHA_LENGTH = 6
CAPTCHA_RE = re.compile(r"^[A-Z0-9]{6}$")


class AutoSwitchSubmitError(RuntimeError):
    """Raised after an enrollment-changing submit was attempted."""


def b64_to_int(value: str) -> int:
    return int.from_bytes(base64.b64decode(value), "big")


def rsa_encrypt(password: str, modulus_b64: str, exponent_b64: str, nonce: str) -> str:
    pub = rsa.RSAPublicNumbers(
        e=b64_to_int(exponent_b64),
        n=b64_to_int(modulus_b64),
    ).public_key()

    payload = json.dumps(
        {"password": password, "nonce": nonce},
        separators=(",", ":"),
    ).encode()

    ct = pub.encrypt(
        payload,
        asym_padding.OAEP(
            mgf=asym_padding.MGF1(algorithm=hashes.SHA256()),
            algorithm=hashes.SHA256(),
            label=None,
        ),
    )
    return base64.b64encode(ct).decode()


def aes_encrypt(password: str, key_text: str, iv_text: str) -> str:
    key = key_text.encode()
    iv = iv_text.encode()

    padder = padding.PKCS7(128).padder()
    padded = padder.update(password.encode()) + padder.finalize()

    cipher = Cipher(algorithms.AES(key), modes.CBC(iv))
    enc = cipher.encryptor()
    ct = enc.update(padded) + enc.finalize()

    return base64.b64encode(ct).decode()


def open_file(path: str) -> None:
    try:
        if sys.platform == "darwin":
            subprocess.Popen(["open", path])
        elif sys.platform.startswith("linux"):
            subprocess.Popen(["xdg-open", path])
        elif sys.platform.startswith("win"):
            import os

            os.startfile(path)  # type: ignore[attr-defined]
    except Exception:
        pass


def sanitize_captcha_text(value: str) -> str:
    return re.sub(r"[^A-Za-z0-9]", "", value).upper()


def looks_like_captcha(value: str) -> bool:
    return bool(CAPTCHA_RE.fullmatch(value))


def solve_captcha_with_tesseract(path: Path) -> str | None:
    try:
        from PIL import Image, ImageFilter, ImageOps
        import pytesseract
    except ImportError as exc:
        log(f"captcha OCR unavailable; install pillow and pytesseract: {exc}")
        return None

    try:
        image = Image.open(path)
        image = ImageOps.grayscale(image)
        image = image.resize((image.width * 3, image.height * 3))
        image = image.filter(ImageFilter.MedianFilter(size=3))
        image = image.point(lambda pixel: 255 if pixel > 150 else 0)

        config = (
            "--psm 8 --oem 3 "
            "-c tessedit_char_whitelist=ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789"
        )
        text = pytesseract.image_to_string(image, config=config)
        captcha = sanitize_captcha_text(text)
        if looks_like_captcha(captcha):
            return captcha

        log(f"captcha OCR returned invalid text {captcha!r}; falling back to manual entry")
    except Exception as exc:
        log(f"captcha OCR failed: {exc}; falling back to manual entry")
    return None


def read_captcha(path: Path, *, use_ocr: bool = True) -> str:
    if use_ocr:
        captcha = solve_captcha_with_tesseract(path)
        if captcha:
            print(f"Captcha OCR: {captcha}")
            return captcha

    open_file(str(path))
    while True:
        captcha = sanitize_captcha_text(input("Captcha: "))
        if looks_like_captcha(captcha):
            return captcha
        print(f"Captcha should be {CAPTCHA_LENGTH} letters/digits. Try again.")


def get_login_form(html: str, page_url: str):
    soup = BeautifulSoup(html, "html.parser")
    form = soup.find("form", id="LoginForm") or soup.find("form")
    if form is None:
        raise RuntimeError("login form not found")

    token_input = form.find("input", {"name": "__RequestVerificationToken"})
    if token_input is None or not token_input.get("value"):
        raise RuntimeError("__RequestVerificationToken not found")

    hidden = {
        input_tag.get("name"): input_tag.get("value", "")
        for input_tag in form.find_all("input", {"type": "hidden"})
        if input_tag.get("name")
    }

    submit_url = urljoin(page_url, form.get("action") or page_url)
    return token_input.get("value"), submit_url, hidden


def normalize_config(data: Any) -> dict[str, Any]:
    if isinstance(data, list):
        return data[0] if data and isinstance(data[0], dict) else {}
    if isinstance(data, dict):
        return data
    return {}


def create_session() -> requests.Session:
    session = requests.Session()
    session.headers.update({"User-Agent": UA})
    return session


def login_once(
    base_url: str,
    login_path: str,
    username: str,
    password: str,
    *,
    save_artifacts: bool = True,
    captcha_ocr: bool = True,
) -> requests.Session:
    session = create_session()
    base = base_url.rstrip("/") + "/"

    login_response = session.get(urljoin(base, login_path), timeout=TIMEOUT)
    login_response.raise_for_status()
    token, submit_url, hidden = get_login_form(login_response.text, login_response.url)

    captcha_response = session.get(
        urljoin(base, "StudentLogin/ShowCaptchaImage"),
        timeout=TIMEOUT,
    )
    captcha_response.raise_for_status()

    captcha_path = Path("captcha.png")
    captcha_path.write_bytes(captcha_response.content)
    print(f"Captcha saved to: {captcha_path}")
    captcha = read_captcha(captcha_path, use_ocr=captcha_ocr)

    cfg_response = session.post(
        urljoin(base, "StudentLogin/GetLoginConfigurationDetails/"),
        timeout=TIMEOUT,
    )
    cfg_response.raise_for_status()
    data = normalize_config(cfg_response.json())

    mode = int(data.get("IS_LOAD_TESTING", 0))
    print(f"Login mode from server: IS_LOAD_TESTING={mode}")

    if mode == 1:
        encrypt_response = session.get(
            urljoin(base, "StudentLogin/getEncryptPassword"),
            timeout=TIMEOUT,
        )
        encrypt_response.raise_for_status()
        encrypt_data = encrypt_response.json()
        encrypted = aes_encrypt(password, encrypt_data["key"], encrypt_data["iv"])
    else:
        rsa_response = session.get(
            urljoin(base, "StudentLogin/GetRsaPublicKey"),
            timeout=TIMEOUT,
        )
        rsa_response.raise_for_status()
        rsa_data = rsa_response.json()
        encrypted = rsa_encrypt(
            password,
            rsa_data["Modulus"],
            rsa_data["Exponent"],
            rsa_data["Nonce"],
        )

    ip = ""
    try:
        ip = session.get("https://api.ipify.org?format=json", timeout=10).json().get("ip", "")
    except Exception:
        pass

    payload = hidden.copy()
    payload.update(
        {
            "__RequestVerificationToken": token,
            "USER_LOGIN": username,
            "LOGIN_OTP": encrypted,
            "JsonIpAddress": ip,
            "CURRENT_DATE": datetime.now().strftime("%d/%m/%Y %H:%M:%S"),
            "IS_LOAD_TESTING": str(mode),
            "CaptchaText": captcha,
        }
    )
    payload.pop("txtpassword", None)

    response = session.post(
        submit_url,
        data=payload,
        headers={"Referer": login_response.url, "Origin": base.rstrip("/")},
        allow_redirects=True,
        timeout=TIMEOUT,
    )

    print(f"HTTP status: {response.status_code}")
    print(f"Final URL: {response.url}")

    if save_artifacts:
        Path("login_result.html").write_text(response.text, encoding="utf-8")
        Path("cookies.json").write_text(
            json.dumps(
                [
                    {
                        "name": cookie.name,
                        "value": cookie.value,
                        "domain": cookie.domain,
                        "path": cookie.path,
                        "secure": cookie.secure,
                        "expires": cookie.expires,
                    }
                    for cookie in session.cookies
                ],
                indent=2,
            ),
            encoding="utf-8",
        )

    looks_logged_in = (
        "loginform" not in response.text.lower()
        and "studentdashboard" in response.url.lower()
    )
    print("Looks logged in:" if looks_logged_in else "Login may have failed:", looks_logged_in)
    if save_artifacts:
        print("Saved: login_result.html, cookies.json")

    if not looks_logged_in:
        raise RuntimeError("login failed; inspect login_result.html")

    return session


def login_with_retry(args: argparse.Namespace, password: str) -> requests.Session:
    last_error: Exception | None = None
    for attempt in range(1, args.max_login_attempts + 1):
        log(f"login attempt {attempt}/{args.max_login_attempts}")
        try:
            session = login_once(
                args.base_url,
                args.login_path,
                args.username,
                password,
                captcha_ocr=not args.no_captcha_ocr,
            )
            log("login succeeded")
            return session
        except Exception as exc:
            last_error = exc
            log(f"login attempt failed: {exc}")
            if attempt == args.max_login_attempts:
                break
    raise RuntimeError(f"exhausted login attempts: {last_error}")


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


def resolve_course_target(session: requests.Session, args: argparse.Namespace) -> dict[str, str]:
    context = normalize_value(post_form_json(session, args.base_url, "/CourseFinder/GetAllDropDownList/", {}))
    if not isinstance(context, dict):
        raise RuntimeError("course finder dropdown response was not an object")

    campus_id = args.campus_id or first_string_field(context, "campus_drp", ["campusno", "campus_no", "value", "id"])
    if not campus_id:
        raise RuntimeError("unable to determine campus id; pass --campus-id")

    academic_session_id = args.academic_session_id or current_session_id(context)
    if not academic_session_id:
        raise RuntimeError("unable to determine academic session id; pass --academic-session-id")

    course_list = normalize_value(
        post_form_json(
            session,
            args.base_url,
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
        if code and course_creation_id and code.upper() == args.course_code.upper():
            matches.append(item)

    if not matches:
        raise RuntimeError(
            f"course code {args.course_code} was not found for campus_id={campus_id} "
            f"academic_session_id={academic_session_id}"
        )
    if len(matches) > 1:
        choices = ", ".join(
            f"{extract_course_creation_id(item)} ({item.get('text') or item.get('course_name') or 'unknown course'})"
            for item in matches
        )
        raise RuntimeError(f"course code {args.course_code} resolved to multiple courses: {choices}")

    selected = matches[0]
    return {
        "course_code": extract_course_code(selected) or args.course_code.upper(),
        "course_creation_id": extract_course_creation_id(selected) or "",
        "campus_id": campus_id,
        "academic_session_id": academic_session_id,
        "is_cross_offer": value_to_string(selected.get("is_cross_offer")) or "0",
        "grid_type": value_to_string(selected.get("grid_type")) or "0",
    }


def fetch_course_snapshot(session: requests.Session, args: argparse.Namespace, target: dict[str, str]) -> str:
    data = fetch_course_data(session, args, target)
    canonical = canonicalize_value(data)
    return json.dumps(canonical, indent=2, ensure_ascii=False)


def fetch_course_data(
    session: requests.Session,
    args: argparse.Namespace,
    target: dict[str, str],
) -> Any:
    return normalize_value(post_form_json(
        session,
        args.base_url,
        "/CourseFinder/GetCFData/",
        {
            "Campusno": target["campus_id"],
            "AcademicSession": target["academic_session_id"],
            "Courseid": target["course_creation_id"],
            "isCrossOffer": target["is_cross_offer"],
            "gridType": target["grid_type"],
        },
    ))


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
    args: argparse.Namespace,
    academic_session_id: str,
) -> dict[str, Any]:
    data = post_form_json(
        session,
        args.base_url,
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
    args: argparse.Namespace,
    target: dict[str, str],
) -> bool:
    state = add_academic_session_to_state(
        get_add_drop_state(session, args, target["academic_session_id"]),
        target["academic_session_id"],
    )
    current_section = current_registered_section_name(
        state,
        target["course_code"],
        target["course_creation_id"],
    )
    return current_section == normalize_section_name(args.target_section)


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


def add_academic_session_to_state(state: dict[str, Any], academic_session_id: str) -> dict[str, Any]:
    output = dict(state)
    output["academic_session_id"] = academic_session_id
    return output


def get_change_section_state(
    session: requests.Session,
    args: argparse.Namespace,
    academic_session_id: str,
) -> dict[str, Any]:
    data = post_form_json(
        session,
        args.base_url,
        "/ApplyAddDrop/GetAddDropCChangeOfSection/",
        {"typeId": CHANGE_SECTION_TYPE_ID, "AcademicSessionId": academic_session_id},
    )
    normalized = normalize_value(data)
    if not isinstance(normalized, dict):
        raise RuntimeError("change-section response was not an object")
    return normalized


def get_course_wise_section_data(
    session: requests.Session,
    args: argparse.Namespace,
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
        args.base_url,
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
    args: argparse.Namespace,
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
        args.base_url,
        "/Enlistment/GetCourseClashDetails/",
        flatten_jquery_form({"academicSessionId": academic_session_id, "CourseList": course_list}),
    )
    normalized = normalize_value(data)
    if not isinstance(normalized, list) or not normalized or not isinstance(normalized[0], dict):
        raise RuntimeError("course clash response had an unexpected shape")

    status = normalized[0].get("status")
    if str(status) != "1":
        raise RuntimeError(f"target section has a schedule clash or server warning: {status}")


def submit_change_section(session: requests.Session, args: argparse.Namespace, payload: dict[str, Any]) -> Any:
    response = post_json(session, args.base_url, "/ApplyAddDrop/SaveChangeOfCourse/", payload)
    if str(response).strip() != "1":
        raise RuntimeError(f"change-section submit was not accepted: {response!r}")
    return response


def submit_add_drop(session: requests.Session, args: argparse.Namespace, payload: dict[str, Any]) -> Any:
    response = post_json(session, args.base_url, "/ApplyAddDrop/SaveEnlistmentData/", payload)
    if str(response).strip() not in {"0", "1"}:
        raise RuntimeError(f"add/drop submit was not accepted: {response!r}")
    return response


def maybe_submit_target_switch(
    session: requests.Session,
    args: argparse.Namespace,
    target: dict[str, str],
    target_section: dict[str, Any],
) -> bool:
    state = get_change_section_state(session, args, target["academic_session_id"])
    current_course = find_current_enlisted_course(state, target["course_code"], target["course_creation_id"])
    current_section_id = string_id(current_course.get("section_creation_id"))
    target_section_id = string_id(target_section.get("section_creation_id"))

    if current_section_id == target_section_id:
        log(f"already enlisted in {target['course_code']} {args.target_section}; nothing to do")
        return True

    section_data = get_course_wise_section_data(
        session,
        args,
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
        reason_id=args.change_reason_id,
        reason_text=args.change_reason_text,
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
        args,
        target["academic_session_id"],
        detail["COURSE_CREATION_ID"],
        detail["NEW_SECTION_CREATION_ID"],
        payload["CAMPUSNO"],
    )

    log(
        "submitting automatic change-section request "
        f"{target['course_code']} {current_section_id} -> {args.target_section} ({target_section_id})"
    )
    try:
        submit_change_section(session, args, payload)
    except Exception as exc:
        raise AutoSwitchSubmitError(
            "change-section submit was attempted but did not return a clean success; "
            "stopping to avoid duplicate submissions"
        ) from exc
    log("change-section request accepted")
    return True


def maybe_submit_drop_add_switch(
    session: requests.Session,
    args: argparse.Namespace,
    target: dict[str, str],
    target_section: dict[str, Any],
) -> bool:
    add_drop_state = add_academic_session_to_state(
        get_add_drop_state(session, args, target["academic_session_id"]),
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
        log(f"already enlisted in {target['course_code']} {args.target_section}; nothing to do")
        return True

    section_data = get_course_wise_section_data(
        session,
        args,
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
        reason_id=args.add_reason_id,
        reason_text=args.add_reason_text,
        required=reason_required,
    )
    drop_reason_id = resolve_add_drop_reason(
        add_drop_state,
        "2",
        reason_id=args.drop_reason_id,
        reason_text=args.drop_reason_text,
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
        args.base_url,
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
        f"{target['course_code']} {current_section_id} -> {args.target_section} ({target_section_id})"
    )
    while True:
        try:
            submit_add_drop(session, args, payload)
            log("drop/add request accepted")
            return True
        except (requests.exceptions.Timeout, requests.exceptions.ConnectionError) as exc:
            log(
                "drop/add submit did not return a clean response "
                f"({exc}); waiting 10 seconds before checking server state"
            )
            time.sleep(10)

            try:
                if drop_add_switch_reflected(session, args, target):
                    log(
                        "drop/add submit appears successful after timeout; "
                        f"{target['course_code']} is now in {args.target_section}"
                    )
                    return True
            except Exception as verify_exc:
                log(f"could not verify post-submit state yet: {verify_exc}")

            try:
                course_data = fetch_course_data(session, args, target)
                refreshed_section = find_target_section(course_data, args.target_section)
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
                if drop_add_switch_reflected(session, args, target):
                    log(
                        "drop/add switch is reflected despite the error response; "
                        f"{target['course_code']} is now in {args.target_section}"
                    )
                    return True
            except Exception as verify_exc:
                log(f"could not verify post-error state: {verify_exc}")
            raise


def persist_snapshot(snapshot: str, snapshot_file: str | None) -> None:
    if snapshot_file:
        Path(snapshot_file).write_text(snapshot, encoding="utf-8")
    print(snapshot)


def log(message: str) -> None:
    print(f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] {message}", file=sys.stderr)


def run_course_watch(args: argparse.Namespace, password: str) -> None:
    session = login_with_retry(args, password)
    target = resolve_course_target(session, args)
    log(
        "watching "
        f"course_code={target['course_code']} "
        f"course_creation_id={target['course_creation_id']} "
        f"campus_id={target['campus_id']} "
        f"academic_session_id={target['academic_session_id']}"
    )

    previous_snapshot: str | None = None
    while True:
        try:
            snapshot = fetch_course_snapshot(session, args, target)
        except Exception as exc:
            log(f"poll failed: {exc}; re-authenticating")
            session = login_with_retry(args, password)
            target = resolve_course_target(session, args)
            continue

        if previous_snapshot is None:
            log("received initial snapshot")
            persist_snapshot(snapshot, args.snapshot_file)
        elif previous_snapshot != snapshot:
            log("change detected")
            persist_snapshot(snapshot, args.snapshot_file)
        else:
            log("no change")

        previous_snapshot = snapshot
        if args.once:
            break
        time.sleep(args.interval_secs)


def run_auto_switch_section(args: argparse.Namespace, password: str) -> None:
    if not args.target_section:
        raise RuntimeError("--target-section is required with --auto-switch-section")

    session = login_with_retry(args, password)
    target = resolve_course_target(session, args)
    log(
        "auto-switch watching "
        f"course_code={target['course_code']} "
        f"target_section={args.target_section} "
        f"course_creation_id={target['course_creation_id']} "
        f"campus_id={target['campus_id']} "
        f"academic_session_id={target['academic_session_id']}"
    )

    while True:
        try:
            course_data = fetch_course_data(session, args, target)
            section = find_target_section(course_data, args.target_section)
            if section is None:
                raise RuntimeError(f"target section {args.target_section} was not found")

            slots = available_slots(section)
            log(
                f"{target['course_code']} {args.target_section}: "
                f"enlisted={section.get('enlisted')} "
                f"capacity={effective_capacity(section):g} "
                f"available={slots:g}"
            )

            if is_section_open(section):
                log("target section appears open; rechecking before submit")
                course_data = fetch_course_data(session, args, target)
                section = find_target_section(course_data, args.target_section)
                if section is None:
                    raise RuntimeError(f"target section {args.target_section} disappeared on recheck")
                if not is_section_open(section):
                    log("target section closed during recheck; continuing watch")
                elif args.switch_strategy == SWITCH_STRATEGY_CHANGE_SECTION:
                    if maybe_submit_target_switch(session, args, target, section):
                        return
                elif args.switch_strategy == SWITCH_STRATEGY_DROP_ADD:
                    if maybe_submit_drop_add_switch(session, args, target, section):
                        return
                else:
                    raise RuntimeError(f"unknown switch strategy: {args.switch_strategy}")
        except AutoSwitchSubmitError:
            raise
        except Exception as exc:
            log(f"auto-switch poll/submit failed: {exc}")
            if args.once:
                raise
            log("re-authenticating before continuing")
            session = login_with_retry(args, password)
            target = resolve_course_target(session, args)

        if args.once:
            break

        jitter = uniform(0, min(3, max(0, args.interval_secs * 0.1)))
        time.sleep(args.interval_secs + jitter)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Login to ArchersHub and optionally watch Course Finder offerings."
    )
    parser.add_argument("base_url", nargs="?", default=DEFAULT_BASE_URL)
    parser.add_argument("--login-path", default=DEFAULT_LOGIN_PATH)
    parser.add_argument("--username")
    parser.add_argument("--password")
    parser.add_argument("--no-captcha-ocr", action="store_true", help="Disable Tesseract OCR and always ask for captcha manually.")
    parser.add_argument("--course-code", help="Enable course-watch mode for this course code.")
    parser.add_argument("--auto-switch-section", action="store_true", help="Automatically change to --target-section when it opens.")
    parser.add_argument(
        "--switch-strategy",
        choices=[SWITCH_STRATEGY_DROP_ADD, SWITCH_STRATEGY_CHANGE_SECTION],
        default=SWITCH_STRATEGY_DROP_ADD,
        help="How --auto-switch-section submits the switch. Defaults to drop-add.",
    )
    parser.add_argument("--target-section", help="Target section name for --auto-switch-section, e.g. Y03.")
    parser.add_argument("--change-reason-id", help="Reason id to use if change-section requires a reason.")
    parser.add_argument("--change-reason-text", help="Text to match against change-section reasons.")
    parser.add_argument("--add-reason-id", help="Add reason id to use for drop-add.")
    parser.add_argument("--add-reason-text", help="Text to match against add reasons for drop-add.")
    parser.add_argument("--drop-reason-id", help="Drop reason id to use for drop-add.")
    parser.add_argument("--drop-reason-text", help="Text to match against drop reasons for drop-add.")
    parser.add_argument("--campus-id")
    parser.add_argument("--academic-session-id")
    parser.add_argument("--interval-secs", type=int, default=DEFAULT_INTERVAL_SECS)
    parser.add_argument("--max-login-attempts", type=int, default=DEFAULT_MAX_LOGIN_ATTEMPTS)
    parser.add_argument("--snapshot-file")
    parser.add_argument("--once", action="store_true")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    args.base_url = args.base_url.rstrip("/")
    args.username = args.username or input("Username / Email: ").strip()
    password = args.password or getpass.getpass("Password: ")

    if args.course_code:
        args.course_code = args.course_code.upper()
        if args.auto_switch_section:
            args.target_section = normalize_section_name(args.target_section)
            run_auto_switch_section(args, password)
        else:
            run_course_watch(args, password)
    else:
        if args.auto_switch_section:
            raise RuntimeError("--course-code is required with --auto-switch-section")
        login_once(
            args.base_url,
            args.login_path,
            args.username,
            password,
            captcha_ocr=not args.no_captcha_ocr,
        )


if __name__ == "__main__":
    main()
