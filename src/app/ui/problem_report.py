"""
Booklet_AI Desktop — Problem Report Builder (STEP-6.2)

Assembles a self-contained, shareable problem report artifact requiring
zero technical input from the operator — triggered by a single button
press. The operator's only action is choosing where to send/save the
resulting file; no manual description or log-copying is required.

Security guarantee:
- Every string field is passed through redact() before being placed on
  the model — this is the single centralized redaction boundary.
- No caller is trusted to have pre-sanitized its input.
- API keys, file contents, and any other sensitive/PII values are
  never included as a matter of policy — enforced structurally by
  redact(), not merely by convention elsewhere in the codebase.
"""

import json
import logging
from datetime import datetime, timezone
from typing import List

from pydantic import BaseModel, ConfigDict

from app.ui.report_redaction import redact

logger = logging.getLogger(__name__)


class ProblemReport(BaseModel):
    """Immutable artifact capturing everything needed for diagnosis.

    All string fields are REDACTED before assignment — see
    build_problem_report() below. Constructing this model by hand
    from unredacted sources is forbidden by contract.
    """

    model_config = ConfigDict(frozen=True)

    generated_at: str
    app_version: str
    correlation_id: str
    original_exception_type: str
    original_exception_message: str
    recent_log_lines: List[str]


def build_problem_report(
    exc: Exception,
    app_version: str,
    correlation_id: str,
    recent_log_lines: List[str],
) -> ProblemReport:
    """Assemble a self-contained, shareable problem report.

    Every string field is passed through redact() before being placed
    on the model — this is the single centralized redaction boundary;
    no caller is trusted to have pre-sanitized its input, per the
    confirmed defect that "the rest of the system doesn't log secrets"
    is not an adequate guarantee on its own.

    Args:
        exc: The original (untranslated) exception that triggered the
             report. If exc is a UserFacingError, its .original
             attribute is used to get the technical exception.
        app_version: Current application version string.
        correlation_id: Ties this report to a specific job.
        recent_log_lines: Last N lines from the application log file.

    Returns:
        Frozen ProblemReport with all fields redacted.
    """
    # If the exception is a UserFacingError wrapper, unwrap to get
    # the original technical exception for the report.
    from app.ui.error_translation import UserFacingError

    actual_exc = exc
    if isinstance(exc, UserFacingError):
        actual_exc = exc.original

    return ProblemReport(
        generated_at=datetime.now(timezone.utc).isoformat(),
        app_version=redact(app_version),
        correlation_id=redact(correlation_id),
        original_exception_type=type(actual_exc).__name__,
        original_exception_message=redact(str(actual_exc)),
        recent_log_lines=[redact(line) for line in recent_log_lines],
    )


def export_problem_report_json(report: ProblemReport) -> str:
    """Serialize a ProblemReport to a JSON string for saving/sharing.

    Args:
        report: The frozen ProblemReport to serialize.

    Returns:
        Pretty-printed JSON string (UTF-8 safe, no ASCII escaping).
    """
    return json.dumps(
        report.model_dump(),
        ensure_ascii=False,
        indent=2,
    )
