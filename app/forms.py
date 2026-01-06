from __future__ import annotations

import re
from typing import Optional

FORM_PREFIX_ORDER: tuple[str, ...] = (
    "FORM 3",
    "FORM 4",
    "SCHEDULE 13D",
    "FORM 13F",
    "FORM 8-K",
    "FORM 10-K",
    "CONGRESS",
)

FORM_LABELS: dict[str, str] = {
    "FORM 3": "Form 3",
    "FORM 4": "Form 4",
    "SCHEDULE 13D": "Schedule 13D",
    "FORM 13F": "Form 13F",
    "FORM 8-K": "Form 8-K",
    "FORM 10-K": "Form 10-K",
    "CONGRESS": "Congress",
}


def normalize_form(value: object) -> Optional[str]:
    if value is None:
        return None
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        value = str(int(value))
    if not isinstance(value, str):
        return None

    raw = value.strip()
    if not raw:
        return None

    t = " ".join(raw.lower().split())
    t = t.replace("–", "-").replace("—", "-")

    if "congress" in t:
        return "CONGRESS"

    amendment = ""
    if re.search(r"(?:^|\\b)(?:amend(?:ment)?|a)\\b", t) or "/a" in t or "-a" in t:
        amendment = "/A"

    if re.search(r"(?:^|\\b)13\\s*d\\b", t):
        return f"SCHEDULE 13D{amendment}"
    if re.search(r"(?:^|\\b)13\\s*f\\b", t):
        return f"FORM 13F{amendment}"
    if re.search(r"(?:^|\\b)8\\s*-?\\s*k\\b", t):
        return f"FORM 8-K{amendment}"
    if re.search(r"(?:^|\\b)10\\s*-?\\s*k\\b", t):
        return f"FORM 10-K{amendment}"
    if re.search(r"(?:^|\\b)3\\b", t):
        return f"FORM 3{amendment}"
    if re.search(r"(?:^|\\b)4\\b", t):
        return f"FORM 4{amendment}"

    if t.startswith("form ") or t.startswith("schedule "):
        return raw.upper()

    return raw.strip()


def form_prefix(form: Optional[str]) -> Optional[str]:
    if not form:
        return None
    text = form.strip().upper()
    for prefix in FORM_PREFIX_ORDER:
        if text.startswith(prefix):
            return prefix
    if text.startswith("FORM 8K"):
        return "FORM 8-K"
    if text.startswith("FORM 10K"):
        return "FORM 10-K"
    if text.startswith("FORM 13D"):
        return "SCHEDULE 13D"
    if text.startswith("FORM 13F"):
        return "FORM 13F"
    if text.startswith("FORM 3"):
        return "FORM 3"
    if text.startswith("FORM 4"):
        return "FORM 4"
    if text.startswith("SCHEDULE 13D"):
        return "SCHEDULE 13D"
    if text.startswith("CONGRESS"):
        return "CONGRESS"
    return None
