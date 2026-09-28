"""Best-effort redaction for command and browser output."""

from __future__ import annotations

import re

# Browser output may include credentials in URL user information or parameters.
_URL_QUERY_PARAMETER_RE = re.compile(r"([?&#;])([^=&#;\s\"']+)=([^&#;\s\"']*)")
_SENSITIVE_URL_PARAMETER_PARTS = (
    "token",
    "secret",
    "password",
    "passwd",
    "apikey",
    "accesskey",
    "credential",
    "signature",
    "authorization",
    "auth",
    "code",
    "state",
    "session",
    "cookie",
    "jwt",
)
_OUTPUT_SECRET_PATTERNS = (
    (r"(?i)\b(sk(?:-proj)?-[A-Za-z0-9_-]{20,})\b", "[REDACTED_OPENAI_KEY]"),
    (r"(?i)\b(AKIA[0-9A-Z.]{6,})\b", "[REDACTED_AWS_KEY]"),
    (r"(?i)\b(AKIA[0-9A-Z]{16})\b", "[REDACTED_AWS_KEY]"),
    (r"(?i)(\bBearer\s+)([A-Za-z0-9._\-]{16,})\b", r"\1[REDACTED]"),
    (
        r"(?i)(\b(?:token|secret|password|api[_-]?key|passwd)\s*[:=]\s*[\"']?)([^\s\"']{8,})",
        r"\1[REDACTED]",
    ),
)


def _redact_url_query_parameter(match: re.Match[str]) -> str:
    separator, name, _value = match.groups()
    normalized_name = re.sub(r"[^a-z0-9]", "", name.lower())
    if any(part in normalized_name for part in _SENSITIVE_URL_PARAMETER_PARTS):
        return f"{separator}{name}=[REDACTED]"
    return match.group(0)


def redact_output(text: str) -> str:
    """Mask common credentials and sensitive URL parameters in output."""
    if not text:
        return ""

    output = re.sub(r"(?i)(https?://)[^/\s?#@]+@", r"\1[REDACTED]@", text)
    output = _URL_QUERY_PARAMETER_RE.sub(_redact_url_query_parameter, output)
    for pattern, replacement in _OUTPUT_SECRET_PATTERNS:
        output = re.sub(pattern, replacement, output)
    return output
