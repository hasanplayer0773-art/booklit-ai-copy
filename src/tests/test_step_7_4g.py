"""
Booklet_AI Desktop — STEP-7.4g Vertical Spacing Around Inline Equations Tests

Static contract tests for inject_tables_and_math.jsx.

NOTE: InDesign ExtendScript (JSX) script visual rendering and actual vertical layout
can only be truly verified by running the script inside Adobe InDesign. These tests
verify that the script defines top-level vertical spacing constants (MATH_SPACE_BEFORE_PT = 10.0,
MATH_SPACE_AFTER_PT = 8.0) and assigns spaceBefore and spaceAfter to math paragraph ranges
in both the image-render and placeholder branches of injectMathFormulaBlock.
"""

from __future__ import annotations

from pathlib import Path
import re
import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[2]
JSX_PATH = PROJECT_ROOT / "src" / "assets" / "scripts" / "inject_tables_and_math.jsx"


def test_jsx_file_exists():
    """Verify that inject_tables_and_math.jsx exists at the expected path."""
    assert JSX_PATH.exists(), f"JSX file not found at {JSX_PATH}"


def test_math_spacing_constants_defined():
    """Assert MATH_SPACE_BEFORE_PT and MATH_SPACE_AFTER_PT are defined with correct values."""
    content = JSX_PATH.read_text(encoding="utf-8")

    match_before = re.search(r"var\s+MATH_SPACE_BEFORE_PT\s*=\s*10(?:\.0)?;", content)
    assert match_before, "Expected 'var MATH_SPACE_BEFORE_PT = 10.0;' in inject_tables_and_math.jsx"

    match_after = re.search(r"var\s+MATH_SPACE_AFTER_PT\s*=\s*8(?:\.0)?;", content)
    assert match_after, "Expected 'var MATH_SPACE_AFTER_PT = 8.0;' in inject_tables_and_math.jsx"


def test_math_spacing_applied_in_both_branches():
    """Assert spaceBefore and spaceAfter are applied in both image and placeholder branches."""
    content = JSX_PATH.read_text(encoding="utf-8")

    # Locate injectMathFormulaBlock function body
    fn_match = re.search(
        r"function\s+injectMathFormulaBlock\s*\([^)]*\)\s*\{([\s\S]*?\n\})", content
    )
    assert fn_match, "injectMathFormulaBlock function not found in inject_tables_and_math.jsx"
    fn_body = fn_match.group(1)

    # Split between image-render branch and placeholder/fallback branch
    split_marker = "// Placeholder or legacy text mode"
    assert split_marker in fn_body, f"Expected '{split_marker}' in injectMathFormulaBlock"
    image_branch, else_branch = fn_body.split(split_marker, 1)

    # Assert image branch sets spaceBefore and spaceAfter
    assert re.search(
        r"targetRange\.paragraphs\.everyItem\(\)\.spaceBefore\s*=\s*MATH_SPACE_BEFORE_PT;",
        image_branch,
    ), "Image branch must assign spaceBefore = MATH_SPACE_BEFORE_PT"
    assert re.search(
        r"targetRange\.paragraphs\.everyItem\(\)\.spaceAfter\s*=\s*MATH_SPACE_AFTER_PT;",
        image_branch,
    ), "Image branch must assign spaceAfter = MATH_SPACE_AFTER_PT"

    # Assert placeholder branch sets spaceBefore and spaceAfter
    assert re.search(
        r"targetRange\.paragraphs\.everyItem\(\)\.spaceBefore\s*=\s*MATH_SPACE_BEFORE_PT;",
        else_branch,
    ), "Placeholder/else branch must assign spaceBefore = MATH_SPACE_BEFORE_PT"
    assert re.search(
        r"targetRange\.paragraphs\.everyItem\(\)\.spaceAfter\s*=\s*MATH_SPACE_AFTER_PT;",
        else_branch,
    ), "Placeholder/else branch must assign spaceAfter = MATH_SPACE_AFTER_PT"
