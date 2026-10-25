"""
Booklet_AI Desktop — Report Redaction (STEP-6.2)

Centralized, mandatory redaction — the Problem Report NEVER relies on
"the rest of the codebase doesn't log secrets" as its safety guarantee.
Every value that reaches ProblemReport passes through redact() first,
regardless of source (exception message, log line, etc.).

This is a structural guarantee, not a convention:
- build_problem_report() calls redact() on every field.
- No caller may construct a ProblemReport by hand-populating fields
  from unredacted sources.
"""

import re
from typing import List, Tuple


# ═══════════════════════════════════════════════════════════════════════
# Redaction patterns — extended whenever a new secret-shaped value is
# introduced anywhere in the codebase. This is a standing requirement.
# ═══════════════════════════════════════════════════════════════════════

_REDACTION_PATTERNS: List[Tuple[re.Pattern, str]] = [
    # Bearer tokens (OAuth / API)
    (
        re.compile(r"Bearer\s+[A-Za-z0-9\-._~+/]+=*", re.IGNORECASE),
        "Bearer [REDACTED]",
    ),
    # OpenRouter / OpenAI style API keys (sk-or-v1-..., sk-..., or-...)
    (
        re.compile(r"(?:sk|or)-[A-Za-z0-9._-]{14,}"),
        "[REDACTED_API_KEY]",
    ),
    # Authorization header values in dict/JSON representations
    (
        re.compile(r'["\']?[Aa]uthorization["\']?\s*[:=]\s*["\'][^"\']*["\']'),
        '"Authorization": "[REDACTED]"',
    ),
    # Generic long hex/base64 tokens (48+ chars) that look like secrets
    (
        re.compile(r"(?<![A-Za-z0-9])[A-Za-z0-9+/]{48,}={0,2}(?![A-Za-z0-9])"),
        "[REDACTED_TOKEN]",
    ),
]


def redact(text: str) -> str:
    """Apply all redaction patterns to the given text.

    Returns a copy of the text with all detected secrets replaced
    by safe placeholder strings. Patterns are applied in order;
    each pattern operates on the result of the previous one.

    This function is the single centralized redaction boundary.
    No caller is trusted to have pre-sanitized its input.

    Args:
        text: The raw text that may contain secrets.

    Returns:
        Redacted text safe for inclusion in problem reports.
    """
    for pattern, replacement in _REDACTION_PATTERNS:
        text = pattern.sub(replacement, text)
    return text
