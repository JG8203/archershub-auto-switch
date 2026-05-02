from __future__ import annotations

import base64
import json
import re
import subprocess
import sys
from datetime import datetime
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urljoin

import requests
from requests.utils import cookiejar_from_dict, dict_from_cookiejar
from bs4 import BeautifulSoup
from cryptography.hazmat.primitives import hashes, padding
from cryptography.hazmat.primitives.asymmetric import padding as asym_padding, rsa
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

from .console import log
from .constants import CAPTCHA_LENGTH, CAPTCHA_RE, TIMEOUT, UA


class LoginAttemptError(RuntimeError):
    """Raised when a single login attempt fails after a captcha was fetched."""

    def __init__(self, message: str, *, captcha_image_bytes: bytes | None = None, captcha_text: str | None = None) -> None:
        super().__init__(message)
        self.captcha_image_bytes = captcha_image_bytes
        self.captcha_text = captcha_text


class AutomatedCaptchaEscalation(RuntimeError):
    """Raised when automated captcha solving is exhausted and the image should be shown to the user."""

    def __init__(self, attempts: int, image_bytes: bytes, last_error: Exception) -> None:
        super().__init__(f"automated captcha solving failed after {attempts} attempts: {last_error}")
        self.attempts = attempts
        self.image_bytes = image_bytes
        self.last_error = last_error


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


def read_captcha(path: Path, *, use_ocr: bool = True, allow_manual_fallback: bool = True) -> str:
    if use_ocr:
        captcha = solve_captcha_with_tesseract(path)
        if captcha:
            print(f"Captcha OCR: {captcha}")
            return captcha
        if not allow_manual_fallback:
            raise RuntimeError("captcha OCR could not solve the image automatically")

    if not allow_manual_fallback:
        raise RuntimeError("manual captcha entry is disabled")
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


CaptchaReader = Callable[[Path], str]


def session_cookies_json(session: requests.Session) -> str:
    return json.dumps(dict_from_cookiejar(session.cookies), sort_keys=True)


def apply_session_cookies_json(session: requests.Session, cookies_json: str | None) -> None:
    if not cookies_json:
        return
    data = json.loads(cookies_json)
    if not isinstance(data, dict):
        raise RuntimeError("stored cookies must be a JSON object")
    session.cookies.update(cookiejar_from_dict({str(key): str(value) for key, value in data.items()}))


def login_once(
    base_url: str,
    login_path: str,
    username: str,
    password: str,
    *,
    save_artifacts: bool = True,
    captcha_ocr: bool = True,
    captcha_reader: CaptchaReader | None = None,
    manual_captcha_fallback: bool = True,
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

    captcha_bytes = captcha_response.content
    captcha_path = Path("captcha.png")
    captcha_path.write_bytes(captcha_bytes)
    print(f"Captcha saved to: {captcha_path}")
    try:
        captcha = (
            captcha_reader(captcha_path)
            if captcha_reader
            else read_captcha(
                captcha_path,
                use_ocr=captcha_ocr,
                allow_manual_fallback=manual_captcha_fallback,
            )
        )
    except Exception as exc:
        raise LoginAttemptError(
            f"captcha could not be prepared automatically: {exc}",
            captcha_image_bytes=captcha_bytes,
        ) from exc

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
        raise LoginAttemptError(
            "login failed; inspect login_result.html",
            captcha_image_bytes=captcha_bytes,
            captcha_text=captcha,
        )

    return session


def login_with_retry(
    base_url: str,
    login_path: str,
    username: str,
    password: str,
    *,
    max_attempts: int,
    captcha_ocr: bool = True,
    save_artifacts: bool = True,
    captcha_reader: CaptchaReader | None = None,
    manual_captcha_fallback: bool = True,
) -> requests.Session:
    last_error: Exception | None = None
    for attempt in range(1, max_attempts + 1):
        log(f"login attempt {attempt}/{max_attempts}")
        try:
            session = login_once(
                base_url,
                login_path,
                username,
                password,
                captcha_ocr=captcha_ocr,
                save_artifacts=save_artifacts,
                captcha_reader=captcha_reader,
                manual_captcha_fallback=manual_captcha_fallback,
            )
            log("login succeeded")
            return session
        except Exception as exc:
            last_error = exc
            log(f"login attempt failed: {exc}")
            if attempt == max_attempts:
                break
    if (
        not manual_captcha_fallback
        and isinstance(last_error, LoginAttemptError)
        and last_error.captcha_image_bytes
    ):
        raise AutomatedCaptchaEscalation(max_attempts, last_error.captcha_image_bytes, last_error) from last_error
    raise RuntimeError(f"exhausted login attempts: {last_error}")
