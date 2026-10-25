"""
Booklet_AI Desktop — Post-Generation Preflight (STEP-6.4)

Checks that run INSIDE the COMSupervisor child process after InDesign
composition completes, before the child process exits.  These checks
require the actual InDesign document and cannot run before generation.

Checks performed:
  - Text-frame overset detection (reuses .overflows — same property
    STEP-1.1's ensureFrameCapacity() uses, AC-6.4.4).
  - Missing-font detection against the required-font list derived
    from TypographyStylesConfig (AC-6.4.4c).
  - Unintended blank pages.

The child process writes the resulting PreflightReport as JSON to the
preflight_output_path specified in COMJobRequest.  The supervisor reads
it after the child process joins (AC-6.4.4b).

This module is imported ONLY inside the child process — it IS allowed
to import win32com/pythoncom (they are available in the child process
after CoInitialize).
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any, List

logger = logging.getLogger(__name__)


def run_post_generation_checks(
    indesign_app: Any,
    indesign_doc: Any,
    required_fonts: List[str],
    preflight_output_path: str,
) -> None:
    """Run post-generation preflight checks and write results to disk.

    Called inside the COM child process after composition completes,
    before the child exits.  Writes a JSON-serialized PreflightReport
    to preflight_output_path for the supervisor to read.

    Args:
        indesign_app: Live InDesign.Application COM object.
        indesign_doc: Live InDesign.Document COM object (the composed booklet).
        required_fonts: List of font names that must be present (derived
                        from TypographyStylesConfig, AC-6.4.4c).
        preflight_output_path: File path to write the JSON PreflightReport.
    """
    issues: List[dict] = []

    if indesign_doc is not None:
        # ── 1. Text-frame overset detection (AC-6.4.4) ────────────────
        # Reuses the same .overflows property that STEP-1.1's
        # ensureFrameCapacity() checks, not a separate overflow check.
        try:
            for story_idx in range(indesign_doc.Stories.Count):
                story = indesign_doc.Stories.Item(story_idx + 1)
                for frame_idx in range(story.TextContainers.Count):
                    frame = story.TextContainers.Item(frame_idx + 1)
                    if getattr(frame, "overflows", False):
                        issues.append({
                            "severity": "blocker",
                            "code": "text_frame_overset",
                            "message": (
                                "يوجد إطار نصي يتجاوز حدوده — قد يكون هناك "
                                "نص مفقود لا يظهر في المخرجات."
                            ),
                        })
        except Exception as exc:
            logger.warning("Overset check encountered an error: %s", exc)
            issues.append({
                "severity": "warning",
                "code": "overset_check_error",
                "message": f"تعذّر فحص تجاوز الإطارات النصية: {exc}",
            })

        # ── 2. Missing-font detection (AC-6.4.4c) ─────────────────────
        # Check document fonts against the required list.
        try:
            doc_font_names: set[str] = set()
            for font_idx in range(indesign_doc.Fonts.Count):
                font = indesign_doc.Fonts.Item(font_idx + 1)
                font_name = getattr(font, "Name", "")
                font_family = getattr(font, "FontFamily", "")
                doc_font_names.add(font_name)
                doc_font_names.add(font_family)

            for req_font in required_fonts:
                if req_font not in doc_font_names:
                    issues.append({
                        "severity": "blocker",
                        "code": "missing_required_font",
                        "message": (
                            f"الخط المطلوب '{req_font}' غير متوفر في "
                            f"مستند InDesign — قد يُستبدل تلقائياً "
                            f"بخط غير مناسب."
                        ),
                    })
        except Exception as exc:
            logger.warning("Font check encountered an error: %s", exc)
            issues.append({
                "severity": "warning",
                "code": "font_check_error",
                "message": f"تعذّر فحص الخطوط المطلوبة: {exc}",
            })

        # ── 3. Unintended blank pages ─────────────────────────────────
        try:
            for page_idx in range(indesign_doc.Pages.Count):
                page = indesign_doc.Pages.Item(page_idx + 1)
                page_items = page.AllPageItems
                if page_items.Count == 0:
                    issues.append({
                        "severity": "warning",
                        "code": "blank_page_detected",
                        "message": (
                            f"الصفحة {page_idx + 1} فارغة تماماً — "
                            f"تحقق مما إذا كانت مقصودة."
                        ),
                    })
        except Exception as exc:
            logger.warning("Blank page check encountered an error: %s", exc)
    else:
        logger.info(
            "No active InDesign document provided to run_post_generation_checks; "
            "skipping document checks."
        )

    # ── Write report to disk ──────────────────────────────────────
    report = {
        "issues": issues,
        "stage": "post_generation",
    }
    output_path = Path(preflight_output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with open(output_path, "w", encoding="utf-8") as f:
        json.dump(report, f, ensure_ascii=False, indent=2)

    logger.info(
        "Post-generation preflight wrote %d issue(s) to %s",
        len(issues),
        preflight_output_path,
    )


def read_post_generation_report(
    preflight_output_path: str,
) -> dict:
    """Read a post-generation PreflightReport from the file written by
    the child process.

    Called by the supervisor (parent process) after the child joins.

    Args:
        preflight_output_path: Path to the JSON file written by
                                run_post_generation_checks().

    Returns:
        A dict with 'issues' and 'stage' keys, suitable for constructing
        a PreflightReport.

    Raises:
        FileNotFoundError: If the report file doesn't exist.
        ValueError: If the file is malformed.
    """
    path = Path(preflight_output_path)
    if not path.exists():
        raise FileNotFoundError(
            f"Post-generation preflight report not found: "
            f"{preflight_output_path}. The COM child process may have "
            f"crashed before writing preflight results."
        )

    with open(path, "r", encoding="utf-8") as f:
        data = json.load(f)

    if not isinstance(data, dict) or "issues" not in data:
        raise ValueError(
            f"Malformed post-generation preflight report at "
            f"{preflight_output_path}."
        )

    return data
