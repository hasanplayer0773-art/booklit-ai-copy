"""
Booklet_AI Desktop — STEP-7.4e SVG1/Path Output & InDesign Frame Styling Tests

Verifies:
1. ziamath config.svg2 = False outputs plain <path> elements (no <symbol>, no <use>)
   for full InDesign SVG import compatibility.
2. inject_tables_and_math.jsx resets the default frame appearance (strokeWeight = 0,
   strokeColor = None, fillColor = None) immediately after rectangles.add().
"""

from __future__ import annotations

from pathlib import Path
import re
import pytest

from app.core.math_renderer import render_mathml_to_svg

PROJECT_ROOT = Path(__file__).resolve().parents[2]


def test_svg2_disabled_produces_paths_without_symbols_or_uses(tmp_path: Path):
    """(a) Rendered SVG contains plain <path> elements and no <symbol> or <use> tags."""
    mathml = (
        '<math xmlns="http://www.w3.org/1998/Math/MathML">'
        '<msup><mrow><mfenced separators="|"><mrow><mn>1</mn><mo>+</mo><mi>x</mi></mrow></mfenced></mrow>'
        '<mrow><mi>n</mi></mrow></msup><mo>=</mo><mn>1</mn><mo>+</mo>'
        '<mfrac><mrow><mi>n</mi><mi>x</mi></mrow><mrow><mn>1</mn><mo>!</mo></mrow></mfrac><mo>+</mo>'
        '<mfrac><mrow><mi>n</mi><mfenced separators="|"><mrow><mi>n</mi><mo>-</mo><mn>1</mn></mrow></mfenced>'
        '<msup><mrow><mi>x</mi></mrow><mrow><mn>2</mn></mrow></msup></mrow><mrow><mn>2</mn><mo>!</mo></mrow></mfrac>'
        '<mo>+</mo><mo>…</mo></math>'
    )
    out_svg = tmp_path / "binomial_svg1.svg"
    result = render_mathml_to_svg(mathml, out_svg, size_pt=12.0)

    assert result.exists()
    content = result.read_text(encoding="utf-8")
    assert "<path" in content
    assert "<symbol" not in content
    assert "<use" not in content


def test_jsx_removes_default_frame_stroke_and_fill():
    """(b) inject_tables_and_math.jsx sets strokeWeight=0 and None swatch right after rectangles.add()."""
    jsx_path = PROJECT_ROOT / "src" / "assets" / "scripts" / "inject_tables_and_math.jsx"
    jsx_content = jsx_path.read_text(encoding="utf-8")

    # Verify rectangles.add() is immediately followed by stroke and fill resets
    pattern = re.compile(
        r"rectangles\.add\(\);\s*"
        r"(?:\/\/[^\r\n]*\s*)*"
        r"rect\.strokeWeight\s*=\s*0;\s*"
        r"[\s\S]*?swatches\.itemByName\(\"None\"\);\s*"
        r"[\s\S]*?rect\.strokeColor\s*=\s*app\.activeDocument\.swatches\.itemByName\(\"None\"\);\s*"
        r"rect\.fillColor\s*=\s*app\.activeDocument\.swatches\.itemByName\(\"None\"\);"
    )
    assert pattern.search(jsx_content), (
        "Expected rect.strokeWeight = 0 and None swatches right after rectangles.add() in inject_tables_and_math.jsx"
    )
