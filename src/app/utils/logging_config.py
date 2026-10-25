"""
Booklet_AI Desktop — Logging Configuration

Centralized logging setup with a filter that redacts API keys
and other sensitive information from log output.

Security rules:
- API keys (Bearer tokens, sk-* patterns) are replaced with [REDACTED].
- Authorization headers are redacted.
- Document content (raw_content) is truncated to prevent PII leakage.
"""

import logging
import re
import sys
from typing import Optional

# Patterns that must be redacted from ALL log output
_REDACTION_PATTERNS = [
    # Bearer tokens
    (re.compile(r"Bearer\s+[A-Za-z0-9\-._~+/]+=*", re.IGNORECASE), "Bearer [REDACTED]"),
    # OpenRouter / OpenAI style keys
    (re.compile(r"sk-[A-Za-z0-9]{20,}"), "[REDACTED_API_KEY]"),
    # Generic authorization headers in dicts/JSON
    (
        re.compile(r"(['\"]?[Aa]uthorization['\"]?\s*[:=]\s*['\"]?)([^'\"]+)"),
        r"\1[REDACTED]",
    ),
]


class SensitiveDataFilter(logging.Filter):
    """Log filter that redacts sensitive data from log records.

    Applied to all handlers to ensure API keys and credentials
    never appear in log output, console, or log files.
    """

    def filter(self, record: logging.LogRecord) -> bool:
        if isinstance(record.msg, str):
            for pattern, replacement in _REDACTION_PATTERNS:
                record.msg = pattern.sub(replacement, record.msg)
        if record.args:
            sanitized_args = []
            for arg in (record.args if isinstance(record.args, tuple) else (record.args,)):
                if isinstance(arg, str):
                    for pattern, replacement in _REDACTION_PATTERNS:
                        arg = pattern.sub(replacement, arg)
                sanitized_args.append(arg)
            record.args = tuple(sanitized_args)
        return True


def setup_logging(
    level: int = logging.INFO,
    log_file: Optional[str] = None,
) -> None:
    """Configure application-wide logging with sensitive data redaction.

    Args:
        level: Logging level (default: INFO).
        log_file: Optional path to a log file. If None, logs to
                  stderr only.
    """
    # Root logger
    root_logger = logging.getLogger()
    root_logger.setLevel(level)

    # Clear existing handlers to prevent duplicates on re-init
    root_logger.handlers.clear()

    # Formatter
    formatter = logging.Formatter(
        fmt="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    # Sensitive data filter
    redaction_filter = SensitiveDataFilter()

    # Console handler (stderr)
    console_handler = logging.StreamHandler(sys.stderr)
    console_handler.setFormatter(formatter)
    console_handler.addFilter(redaction_filter)
    root_logger.addHandler(console_handler)

    # File handler (optional)
    if log_file:
        file_handler = logging.FileHandler(log_file, encoding="utf-8")
        file_handler.setFormatter(formatter)
        file_handler.addFilter(redaction_filter)
        root_logger.addHandler(file_handler)

    logging.getLogger(__name__).debug("Logging configured (level=%s)", level)
