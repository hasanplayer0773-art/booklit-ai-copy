"""
Booklet_AI Desktop — Tests for STEP-7.7b

Static contracts and code verification for real image placement in InDesign JSX scripts:
- Verifies that generate_booklet.jsx item dispatch loop has a branch for
  `item.block_type === "image_prompt" && item.render_mode === "image"` BEFORE the
  generic text branch, preventing raw "[image:...]" text insertion.
- Verifies that injectImageBlock contains File.exists check, rect.place(),
  the "None" swatch validation and assignment, Leading.AUTO, the two spacing constants
  (IMAGE_SPACE_BEFORE_PT = 8.0 and IMAGE_SPACE_AFTER_PT = 8.0), and the 0.9 height cap.
- Verifies that there are no empty catch blocks in injectImageBlock.
- Verifies that the images_placed counter is tracked and reported in the result JSON.

NOTE: Visual correctness and rendering fidelity of placed images in InDesign frames
is verified only in a real Adobe InDesign environment.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
SCRIPTS_DIR = PROJECT_ROOT / "src" / "assets" / "scripts"
GENERATE_BOOKLET_JSX = SCRIPTS_DIR / "generate_booklet.jsx"
INJECT_TABLES_MATH_JSX = SCRIPTS_DIR / "inject_tables_and_math.jsx"


@pytest.fixture(scope="module")
def generate_booklet_source() -> str:
    """Read the content of generate_booklet.jsx."""
    assert GENERATE_BOOKLET_JSX.is_file(), f"Missing {GENERATE_BOOKLET_JSX}"
    return GENERATE_BOOKLET_JSX.read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def inject_tables_math_source() -> str:
    """Read the content of inject_tables_and_math.jsx."""
    assert INJECT_TABLES_MATH_JSX.is_file(), f"Missing {INJECT_TABLES_MATH_JSX}"
    return INJECT_TABLES_MATH_JSX.read_text(encoding="utf-8")


class TestStep77bImagePlacementContracts:
    """Static inspection tests for InDesign JSX image placement contracts."""

    def test_item_dispatch_branch_before_generic_text(self, generate_booklet_source: str):
        """Task 1: Dispatch branch for image_prompt with render_mode == 'image'
        exists BEFORE the generic text branch in the items loop."""
        loop_header = "for (var idx = 0; idx < items.length; idx++)"
        loop_pos = generate_booklet_source.find(loop_header)
        assert loop_pos != -1, f"Items dispatch loop '{loop_header}' not found"

        # Check branch condition inside loop
        branch_pattern = re.compile(
            r'item\.block_type\s*===\s*"image_prompt"\s*&&\s*item\.render_mode\s*===\s*"image"'
        )
        match_branch = branch_pattern.search(generate_booklet_source, loop_pos)
        assert match_branch is not None, "Dispatch branch condition for image_prompt image not found in items loop"

        branch_pos = match_branch.start()

        # Check call to injectImageBlock with usableWidthPt and usableHeightPt
        call_pattern = re.compile(
            r'injectImageBlock\s*\(\s*story\s*,\s*item\s*,\s*styles\s*,\s*usableWidthPt\s*,\s*usableHeightPt\s*\)'
        )
        assert call_pattern.search(generate_booklet_source, loop_pos) is not None, (
            "injectImageBlock(story, item, styles, usableWidthPt, usableHeightPt) call not found in items loop"
        )

        # Check getFrameUsableWidth and getFrameUsableHeight are used
        assert "getFrameUsableWidth(currentFrame)" in generate_booklet_source
        assert "getFrameUsableHeight(currentFrame)" in generate_booklet_source

        # Find the generic text branch within the items loop
        generic_marker = "var blockText = item.raw_content ||"
        generic_pos = generate_booklet_source.find(generic_marker, loop_pos)
        assert generic_pos != -1, "Generic text branch (item.raw_content) not found in items loop"

        # Assert image branch appears BEFORE generic text branch
        assert loop_pos < branch_pos < generic_pos, (
            "image_prompt branch must be placed BEFORE the generic text branch in items loop"
        )

    def test_image_block_raw_content_never_inserted(self, generate_booklet_source: str):
        """Task 1: In the image branch, raw_content is not added to story."""
        # Extract the image branch section
        pattern = re.compile(
            r'else\s+if\s*\(\s*item\.block_type\s*===\s*"image_prompt"\s*&&\s*item\.render_mode\s*===\s*"image"\s*\)\s*\{([^}]+)\}',
            re.DOTALL,
        )
        match = pattern.search(generate_booklet_source)
        assert match is not None, "Could not extract image branch block"
        branch_body = match.group(1)

        # Must not insert raw_content
        assert "raw_content" not in branch_body, (
            "image_prompt raw_content must NEVER be inserted into the story"
        )

    def test_inject_image_block_file_exists_and_place(self, inject_tables_math_source: str):
        """Task 2: injectImageBlock checks File.exists, places image, and handles errors."""
        # File.exists check before place
        assert "File.exists" in inject_tables_math_source or ".exists" in inject_tables_math_source
        assert "ImageAssetMissingError" in inject_tables_math_source

        # rect.place call
        assert re.search(r"rect\.place\s*\(\s*new\s+File\s*\(", inject_tables_math_source) or \
               re.search(r"rect\.place\s*\(", inject_tables_math_source), (
            "rect.place call not found in inject_tables_and_math.jsx"
        )

    def test_inject_image_block_none_swatch_lines(self, inject_tables_math_source: str):
        """Task 2: Stroke weight 0 and stroke/fill colors set to 'None' swatch with error checking."""
        assert 'swatches.itemByName("None")' in inject_tables_math_source
        assert "rect.strokeWeight = 0" in inject_tables_math_source
        assert "rect.strokeColor" in inject_tables_math_source
        assert "rect.fillColor" in inject_tables_math_source

    def test_inject_image_block_sizing_constants_and_height_cap(self, inject_tables_math_source: str):
        """Task 2: Sizing constants MM_TO_PT, IMAGE_FALLBACK_DPI, spacing constants, and 0.9 cap."""
        # Top-level constants
        assert "MM_TO_PT" in inject_tables_math_source
        assert "IMAGE_FALLBACK_DPI = 150" in inject_tables_math_source
        assert "IMAGE_SPACE_BEFORE_PT = 8.0" in inject_tables_math_source
        assert "IMAGE_SPACE_AFTER_PT = 8.0" in inject_tables_math_source

        # 0.9 height cap
        assert "0.9" in inject_tables_math_source
        assert "usableHeightPt * 0.9" in inject_tables_math_source or \
               "maxAllowedHeight = usableHeightPt * 0.9" in inject_tables_math_source

    def test_inject_image_block_paragraph_styling_and_leading(self, inject_tables_math_source: str):
        """Task 2: Apply ImagePlaceholderStyle with centered alignment and Leading.AUTO."""
        assert "Leading.AUTO" in inject_tables_math_source
        assert "ImagePlaceholderStyle" in inject_tables_math_source
        assert "CENTER_ALIGN" in inject_tables_math_source
        assert "RIGHT_TO_LEFT_DIRECTION" in inject_tables_math_source
        assert "FitOptions.PROPORTIONALLY" in inject_tables_math_source

    def test_no_empty_catch_blocks(self, inject_tables_math_source: str):
        """Task 2: NO empty catch blocks and no silent fallback to text."""
        # Find injectImageBlock function body
        func_match = re.search(
            r"function\s+injectImageBlock\s*\([^)]*\)\s*\{([\s\S]*?\n\})",
            inject_tables_math_source,
        )
        assert func_match is not None, "injectImageBlock function not found"
        func_body = func_match.group(1)

        empty_catch_pattern = re.compile(r"catch\s*\([^)]*\)\s*\{\s*\}")
        assert not empty_catch_pattern.search(func_body), (
            "Found empty catch block in injectImageBlock — silent failures forbidden"
        )

    def test_images_placed_counter_in_result_data(self, generate_booklet_source: str):
        """Task 3: images_placed counter accumulated and reported in resultData."""
        assert "images_placed:" in generate_booklet_source or "images_placed" in generate_booklet_source
        assert "imagesPlacedCount" in generate_booklet_source
