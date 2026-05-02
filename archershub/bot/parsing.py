from __future__ import annotations

import re

from ..sections import normalize_section_name

MODE_VALUES = {"notify", "confirm", "auto"}
SECTION_TOKEN_RE = re.compile(r"^[A-Z]{1,4}\d{1,3}[A-Z]?$")


def parse_addclass_specs(args: list[str]) -> list[tuple[str, list[str]]]:
    payload = [arg for arg in args if arg.lower() not in MODE_VALUES]
    if not payload:
        raise ValueError("at least one course is required")
    if len(payload) > 1 and ":" not in payload[0] and all(SECTION_TOKEN_RE.fullmatch(token.upper()) for token in payload[1:]):
        return [(payload[0].upper(), [normalize_section_name(token) for token in payload[1:]])]

    specs: list[tuple[str, list[str]]] = []
    for item in payload:
        course_part, _, priority_part = item.partition(":")
        course_code = course_part.strip().upper()
        if not course_code:
            raise ValueError(f"invalid addclass spec: {item}")
        priorities = [
            normalize_section_name(token)
            for token in priority_part.split(",")
            if token.strip()
        ]
        specs.append((course_code, priorities))
    return specs
