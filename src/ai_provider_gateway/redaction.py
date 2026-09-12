"""Recursive log redaction for credential and identity-bearing values."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
import re
from typing import Any


REDACTED = "[REDACTED]"
EMAIL_REDACTED = "[EMAIL_REDACTED]"

_SENSITIVE_KEYS = re.compile(
    r"^(?:authorization|cookie|set-cookie|password|passwd|secret|token|"
    r"client[_-]?secret|oauth[_-]?token|access[_-]?token|refresh[_-]?token|"
    r"api[_-]?key|x-api-key|management[_-]?key|gateway[_-]?api[_-]?key|"
    r"credential|session[_-]?id|oauth[_-]?state|recovery[_-]?challenge|prompt|response|"
    r"messages|tool[_-]?arguments)$",
    re.IGNORECASE,
)
_BEARER = re.compile(r"(?i)\bBearer\s+[A-Za-z0-9._~+\-/=]+")
_SECRET_HEADER = re.compile(
    r"(?im)\b(authorization|cookie|set-cookie)(\s*[:=]\s*)([^\r\n]+)"
)
_EMAIL = re.compile(r"(?i)\b[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}\b")
_WINDOWS_USER_PATH = re.compile(
    r"(?i)\b[A-Z]:\\Users\\[^\\\s\"']+(?P<rest>(?:\\[^\s\"']*)?)"
)
_NAMED_SECRET = re.compile(
    r"(?i)\b(password|passwd|(?:client[_-]?)?secret|(?:oauth[_-]?)?token|"
    r"api[_-]?key|management[_-]?key|session[_-]?id|recovery[_-]?challenge)"
    r"(\s*[:=]\s*)([^\s,;]+)"
)


def redact_text(value: str) -> str:
    """Redact common secrets, email addresses, and Windows user roots."""

    value = _BEARER.sub(f"Bearer {REDACTED}", value)
    value = _SECRET_HEADER.sub(
        lambda match: f"{match.group(1)}{match.group(2)}{REDACTED}", value
    )
    value = _NAMED_SECRET.sub(lambda match: f"{match.group(1)}{match.group(2)}{REDACTED}", value)
    value = _EMAIL.sub(EMAIL_REDACTED, value)
    return _WINDOWS_USER_PATH.sub(
        lambda match: f"%USERPROFILE%{match.group('rest') or ''}", value
    )


def redact(value: Any, *, key: str | None = None) -> Any:
    """Return a redacted copy of nested logging data without mutating input."""

    if key is not None and _SENSITIVE_KEYS.fullmatch(key):
        return REDACTED
    if isinstance(value, str):
        return redact_text(value)
    if isinstance(value, Mapping):
        return {str(item_key): redact(item_value, key=str(item_key)) for item_key, item_value in value.items()}
    if isinstance(value, tuple):
        return tuple(redact(item) for item in value)
    if isinstance(value, Sequence) and not isinstance(value, (bytes, bytearray)):
        return [redact(item) for item in value]
    return value
