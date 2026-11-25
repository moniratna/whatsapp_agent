from __future__ import annotations

import re
from typing import Any

_PHONE = re.compile(r"(?<!\w)\+?[0-9][0-9 ()-]{7,}[0-9](?!\w)")
_EMAIL = re.compile(r"\b[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}\b")
_URL = re.compile(r"https?://\S+", re.IGNORECASE)
_INTRODUCED_NAME = re.compile(
    r"(?i)(\b(?:my name is|this is|i am|i'm|name is)\s+)"
    r"[A-Z][a-z'-]+(?:\s+[A-Z][a-z'-]+){0,2}"
)
_TITLED_NAME = re.compile(r"\b(?:Mr|Mrs|Ms|Miss|Dr)\.?\s+[A-Z][a-z'-]+(?:\s+[A-Z][a-z'-]+)?\b")
_ADDRESS = re.compile(
    r"(?i)\b(?:flat|apartment|apt|villa|building|tower|street|road|unit|plot|house)\s+"
    r"(?:no\.?\s*)?[A-Za-z0-9-]+(?:\s+[A-Za-z0-9-]+){0,4}"
)


def deidentify_text(value: str | None) -> str | None:
    """Apply the same deterministic redactions used by transcript exports."""
    if value is None:
        return None
    value = _EMAIL.sub("[EMAIL]", value)
    value = _URL.sub("[URL]", value)
    value = _PHONE.sub("[PHONE]", value)
    value = _INTRODUCED_NAME.sub(r"\1[NAME]", value)
    value = _TITLED_NAME.sub("[NAME]", value)
    return _ADDRESS.sub("[ADDRESS]", value)


def deidentify_value(value: Any) -> Any:
    if isinstance(value, str):
        return deidentify_text(value) or ""
    if isinstance(value, list):
        return [deidentify_value(item) for item in value]
    if isinstance(value, dict):
        return {str(key): deidentify_value(item) for key, item in value.items()}
    return value
