"""
Booklet_AI Desktop — STEP-7.4f Ministry-Question Callout Border & Padding Tests

Static contract tests for apply_question_callout.jsx.

NOTE: InDesign ExtendScript (JSX) scripts can only be truly verified by running
them inside Adobe InDesign. These tests assert that the script defines the
intended paragraph border line weights (right=3.0, top/bottom/left=0) and
border offsets (top/bottom=4.0, left/right=6.0) to ensure a right-side-only bar
with proper text padding without an enclosing box.
"""

from __future__ import annotations

from pathlib import Path
import re
import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[2]
JSX_PATH = PROJECT_ROOT / "src" / "assets" / "scripts" / "apply_question_callout.jsx"


def test_jsx_file_exists():
    """Verify that apply_question_callout.jsx exists at the expected path."""
    assert JSX_PATH.exists(), f"JSX file not found at {JSX_PATH}"


def test_callout_border_line_weights():
    """Assert top, bottom, and left border line weights are 0, and right is 3.0."""
    content = JSX_PATH.read_text(encoding="utf-8")

    # Assert paragraphBorderOn is enabled
    assert re.search(r"paragraphBorderOn\s*=\s*true;", content), (
        "Expected p.paragraphBorderOn = true; in apply_question_callout.jsx"
    )

    # Assert 0 weight on top, bottom, and left
    assert re.search(r"paragraphBorderTopLineWeight\s*=\s*0(?:\.0)?;", content), (
        "Expected p.paragraphBorderTopLineWeight = 0; in apply_question_callout.jsx"
    )
    assert re.search(r"paragraphBorderBottomLineWeight\s*=\s*0(?:\.0)?;", content), (
        "Expected p.paragraphBorderBottomLineWeight = 0; in apply_question_callout.jsx"
    )
    assert re.search(r"paragraphBorderLeftLineWeight\s*=\s*0(?:\.0)?;", content), (
        "Expected p.paragraphBorderLeftLineWeight = 0; in apply_question_callout.jsx"
    )

    # Assert 3.0 weight on right
    assert re.search(r"paragraphBorderRightLineWeight\s*=\s*3\.0;", content), (
        "Expected p.paragraphBorderRightLineWeight = 3.0; in apply_question_callout.jsx"
    )


def test_callout_border_and_shading_offsets():
    """Assert that the 4 border offsets and shading offsets are configured with padding values."""
    content = JSX_PATH.read_text(encoding="utf-8")

    # Verify offset constants define top/bottom=4.0 and left/right=6.0
    assert re.search(r"TOP_OFFSET\s*=\s*4\.0;", content), (
        "Expected TOP_OFFSET = 4.0; in apply_question_callout.jsx"
    )
    assert re.search(r"BOTTOM_OFFSET\s*=\s*4\.0;", content), (
        "Expected BOTTOM_OFFSET = 4.0; in apply_question_callout.jsx"
    )
    assert re.search(r"LEFT_OFFSET\s*=\s*6\.0;", content), (
        "Expected LEFT_OFFSET = 6.0; in apply_question_callout.jsx"
    )
    assert re.search(r"RIGHT_OFFSET\s*=\s*6\.0;", content), (
        "Expected RIGHT_OFFSET = 6.0; in apply_question_callout.jsx"
    )

    # Verify all 4 border offsets are set
    assert re.search(r"paragraphBorderTopOffset\s*=\s*(?:TOP_OFFSET|4\.0);", content), (
        "Expected paragraphBorderTopOffset in apply_question_callout.jsx"
    )
    assert re.search(r"paragraphBorderBottomOffset\s*=\s*(?:BOTTOM_OFFSET|4\.0);", content), (
        "Expected paragraphBorderBottomOffset in apply_question_callout.jsx"
    )
    assert re.search(r"paragraphBorderLeftOffset\s*=\s*(?:LEFT_OFFSET|6\.0);", content), (
        "Expected paragraphBorderLeftOffset in apply_question_callout.jsx"
    )
    assert re.search(r"paragraphBorderRightOffset\s*=\s*(?:RIGHT_OFFSET|6\.0);", content), (
        "Expected paragraphBorderRightOffset in apply_question_callout.jsx"
    )

    # Verify all 4 shading offsets are set
    assert re.search(r"paragraphShadingTopOffset\s*=\s*(?:TOP_OFFSET|4\.0);", content), (
        "Expected paragraphShadingTopOffset in apply_question_callout.jsx"
    )
    assert re.search(r"paragraphShadingBottomOffset\s*=\s*(?:BOTTOM_OFFSET|4\.0);", content), (
        "Expected paragraphShadingBottomOffset in apply_question_callout.jsx"
    )
    assert re.search(r"paragraphShadingLeftOffset\s*=\s*(?:LEFT_OFFSET|6\.0);", content), (
        "Expected paragraphShadingLeftOffset in apply_question_callout.jsx"
    )
    assert re.search(r"paragraphShadingRightOffset\s*=\s*(?:RIGHT_OFFSET|6\.0);", content), (
        "Expected paragraphShadingRightOffset in apply_question_callout.jsx"
    )
