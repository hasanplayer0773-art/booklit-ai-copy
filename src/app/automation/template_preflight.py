"""
Booklet_AI Desktop — Template Preflight Checker (STEP-2.3)

Validates that all nine BLOCK_STYLE_MAP style names exist in the base
.idml template's paragraph styles panel BEFORE generation begins —
detected at document-load time, not discovered mid-injection (AC-2.3.4).

This module provides two interfaces:
- validate_template_styles(): Python-side check that builds the JSX
  preflight script invocation data.
- build_preflight_payload(): Produces the JSON payload consumed by
  the JSX-side preflight function.

The actual InDesign style existence check runs in the JSX layer
(preflight_styles.jsx) via the COM bridge, since paragraph style
objects are only accessible through the InDesign DOM.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import List, Optional

from app.automation.style_dispatch import get_all_required_style_names
from app.core.exceptions import StyleMappingError


def build_preflight_payload(output_path: Optional[Path] = None) -> dict:
    """Build a JSON-serializable payload for the JSX preflight script.

    The payload contains the complete list of paragraph style names
    that must exist in the InDesign template. The JSX script iterates
    this list and reports any missing styles.

    Args:
        output_path: Optional path to write the payload as a JSON file
            (for file-based IPC with ExtendScript). If None, returns
            the dict without writing to disk.

    Returns:
        A dict with key 'required_styles' containing a sorted list
        of all style names from BLOCK_STYLE_MAP.
    """
    required = sorted(get_all_required_style_names())
    payload = {"required_styles": required}

    if output_path is not None:
        output_path.parent.mkdir(parents=True, exist_ok=True)
        with open(output_path, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False, indent=2)

    return payload


def check_preflight_result(result_path: Path) -> None:
    """Read and validate the JSX preflight result file.

    The JSX preflight script writes a JSON result file with:
    - 'status': 'ok' if all styles exist, 'error' otherwise.
    - 'missing_styles': list of style names not found in the template.

    Args:
        result_path: Path to the JSON result file written by
            preflight_styles.jsx.

    Raises:
        StyleMappingError: If any required paragraph style is missing
            from the InDesign template.
        FileNotFoundError: If the result file does not exist.
        ValueError: If the result file is malformed.
    """
    if not result_path.exists():
        raise FileNotFoundError(
            f"Preflight result file not found: {result_path}. "
            f"The JSX preflight script may not have executed."
        )

    with open(result_path, "r", encoding="utf-8") as f:
        result = json.load(f)

    if not isinstance(result, dict):
        raise ValueError(
            f"Preflight result file is malformed — expected a JSON "
            f"object, got {type(result).__name__}."
        )

    status = result.get("status")
    if status == "ok":
        return  # All styles exist — precondition satisfied

    missing: List[str] = result.get("missing_styles", [])
    if not missing:
        raise ValueError(
            f"Preflight result has status '{status}' but no "
            f"'missing_styles' list — malformed JSX output."
        )

    raise StyleMappingError(
        f"Template precondition check failed (AC-2.3.4): "
        f"{len(missing)} paragraph style(s) missing from the "
        f".idml template: {missing}. These styles must exist in "
        f"the template's paragraph styles panel before generation. "
        f"Add them to the template or fix BLOCK_STYLE_MAP if the "
        f"names are mismatched."
    )
