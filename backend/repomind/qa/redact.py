"""Remove obvious secrets from text before it leaves the machine (prompts to an external LLM).

This is a best-effort safety net, not a guarantee: it catches well-known token formats and
`key = value` assignments for secret-looking names. Users who cannot accept any risk should set
REPOMIND_ALLOW_EXTERNAL_LLM=0 so that nothing is ever sent out."""
from __future__ import annotations

import re

PLACEHOLDER = "[REDACTED]"

_PATTERNS = [
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?(-----END [A-Z ]*PRIVATE KEY-----|$)", re.S),
    re.compile(r"\bsk-(?:or-v1-|ant-|proj-)?[A-Za-z0-9_\-]{16,}"),            # OpenAI / OpenRouter / Anthropic
    re.compile(r"\bgh[pousr]_[A-Za-z0-9]{20,}"),                                # GitHub tokens
    re.compile(r"\bAKIA[0-9A-Z]{16}\b"),                                        # AWS access key id
    re.compile(r"\bAIza[0-9A-Za-z_\-]{30,}"),                                   # Google API key
    re.compile(r"\bxox[abprs]-[A-Za-z0-9\-]{10,}"),                             # Slack
    re.compile(r"\beyJ[A-Za-z0-9_\-]{8,}\.[A-Za-z0-9_\-]{8,}\.[A-Za-z0-9_\-]{8,}"),  # JWT
]
# NAME = "value" / NAME: value where NAME looks secret. Keeps the name, drops the value.
_ASSIGNMENT = re.compile(
    r"(?i)(\b[\w.-]*(?:api[_-]?key|secret|passwd|password|token|credential|private[_-]?key)[\w.-]*['\"]?\s*[:=]\s*)"
    r"(['\"]?)(?!\d+(?:-\d+)?(?:[\s\]),;]|$))([^\s'\",;]{4,})(\2)")   # a path:10-20 citation is not a secret


def redact(text: str, extra_secrets: tuple[str, ...] = ()) -> str:
    """Return `text` with secrets replaced by [REDACTED]. `extra_secrets` are exact strings
    (e.g. the configured API key) that must never appear."""
    for secret in extra_secrets:
        if secret and len(secret) >= 6:
            text = text.replace(secret, PLACEHOLDER)
    for pat in _PATTERNS:
        text = pat.sub(PLACEHOLDER, text)
    return _ASSIGNMENT.sub(lambda m: f"{m[1]}{m[2]}{PLACEHOLDER}{m[4]}", text)
