"""Structured JSON logging that never writes secrets or PII.

Redaction happens in the formatter, on the final message, extra fields and tracebacks,
so it covers every log call without callers having to remember anything.
"""

import json
import logging
import re
import sys
from datetime import UTC, datetime
from typing import Any

from pydantic import SecretStr

REDACTED = "[redacted]"

# Field names whose values are never logged.
_SENSITIVE_KEY = re.compile(
    r"(pass(word)?|secret|token|api[_-]?key|authorization|cookie|jwt|account|iban|ifsc|card)",
    re.IGNORECASE,
)
_EMAIL = re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+")
# Phone and account numbers: 9+ digits, optionally separated by spaces or dashes.
_LONG_NUMBER = re.compile(r"(?<!\d)\+?\d(?:[\s-]?\d){8,}(?!\d)")
# "Bearer abc.def", "api_key=abc", "password: abc" inside free text.
_CREDENTIAL_IN_TEXT = re.compile(
    r"(?i)\b(bearer|password|passwd|secret|token|api[_-]?key)\b(\s*[:=]?\s*)([^\s,;\"']+)"
)

# Attributes every LogRecord has; anything else was passed via `extra=`.
_STANDARD_ATTRS = set(vars(logging.makeLogRecord({}))) | {"message", "asctime", "taskName"}
_HANDLER_MARK = "_refund_matrix_handler"


def redact_text(text: str) -> str:
    text = _CREDENTIAL_IN_TEXT.sub(lambda m: f"{m.group(1)}{m.group(2)}{REDACTED}", text)
    text = _EMAIL.sub("[email]", text)
    return _LONG_NUMBER.sub("[number]", text)


def redact_value(key: str, value: Any) -> Any:
    if _SENSITIVE_KEY.search(key) or isinstance(value, SecretStr):
        return REDACTED
    if isinstance(value, str):
        return redact_text(value)
    if isinstance(value, dict):
        return {str(k): redact_value(str(k), v) for k, v in value.items()}
    if isinstance(value, list | tuple | set):
        return [redact_value(key, v) for v in value]
    if value is None or isinstance(value, bool | int | float):
        return value
    return redact_text(str(value))


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "ts": datetime.fromtimestamp(record.created, tz=UTC).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": redact_text(record.getMessage()),
        }
        for key, value in record.__dict__.items():
            if key not in _STANDARD_ATTRS and not key.startswith("_"):
                payload[key] = redact_value(key, value)
        if record.exc_info:
            payload["exception"] = redact_text(self.formatException(record.exc_info))
        return json.dumps(payload, default=str, ensure_ascii=False)


def configure_logging(level: str = "INFO") -> None:
    """Install one JSON handler on the root logger. Safe to call more than once."""
    root = logging.getLogger()
    for handler in list(root.handlers):
        if getattr(handler, _HANDLER_MARK, False):
            root.removeHandler(handler)
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(JsonFormatter())
    setattr(handler, _HANDLER_MARK, True)
    root.addHandler(handler)
    root.setLevel(level.upper())
