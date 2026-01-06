from __future__ import annotations

import re
from typing import Optional

SOURCE_ORDER: tuple[str, ...] = (
    "form3",
    "form4",
    "schedule13d",
    "form13f",
    "form8k",
    "form10k",
    "congress",
)

CANONICAL_SOURCES = SOURCE_ORDER

SOURCE_LABELS: dict[str, str] = {
    "congress": "Congress",
    "form3": "Form 3",
    "form4": "Form 4",
    "schedule13d": "Schedule 13D",
    "form13f": "Form 13F",
    "form8k": "Form 8-K",
    "form10k": "Form 10-K",
}

SOURCE_ALIASES: dict[str, str] = {
    "congress": "congress",
    "insider": "form4",
    "form3": "form3",
    "form 3": "form3",
    "3": "form3",
    "form4": "form4",
    "form 4": "form4",
    "schedule13d": "schedule13d",
    "schedule 13d": "schedule13d",
    "13d": "schedule13d",
    "form13f": "form13f",
    "form 13f": "form13f",
    "13f": "form13f",
    "form8k": "form8k",
    "form 8-k": "form8k",
    "8k": "form8k",
    "8-k": "form8k",
    "form10k": "form10k",
    "form 10-k": "form10k",
    "10k": "form10k",
    "10-k": "form10k",
}


def normalize_source(value: object) -> Optional[str]:
    if value is None:
        return None
    if isinstance(value, bool):
        return None
    if not isinstance(value, str):
        return None

    candidate = " ".join(value.strip().lower().split())
    if not candidate:
        return None

    mapped = SOURCE_ALIASES.get(candidate, candidate)
    if mapped in SOURCE_LABELS:
        return mapped
    return None


def infer_source_from_form(value: object) -> Optional[str]:
    if value is None:
        return None
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        value = str(int(value))
    if not isinstance(value, str):
        return None

    text = value.strip()
    if not text:
        return None

    direct = normalize_source(text)
    if direct and direct != "congress":
        return direct

    t = " ".join(text.lower().split())
    t = t.replace("–", "-").replace("—", "-")

    if re.search(r"(?:^|\\b)13\\s*d\\b", t):
        return "schedule13d"
    if re.search(r"(?:^|\\b)13\\s*f\\b", t):
        return "form13f"
    if re.search(r"(?:^|\\b)8\\s*-?\\s*k\\b", t):
        return "form8k"
    if re.search(r"(?:^|\\b)10\\s*-?\\s*k\\b", t):
        return "form10k"
    if re.search(r"(?:^|\\b)3(?:\\s*[/\\-]\\s*a)?\\b", t):
        return "form3"
    if re.search(r"(?:^|\\b)4(?:\\s*[/\\-]\\s*a)?\\b", t):
        return "form4"
    return None
