"""
Booklet_AI Desktop — STEP-7.4d Regression Tests

Guards against greedy regex bugs in ziamath (unescape) causing mismatched tags
or silent equation content loss when encountering ASCII minus '-' characters.
"""

from __future__ import annotations

from pathlib import Path
import pytest

from app.core.exceptions import MathRenderError
from app.core.math_renderer import render_mathml_to_svg


def test_real_regression_fixture_binomial_expansion(tmp_path: Path):
    """Real regression fixture with ASCII '-' inside <mo> must render cleanly."""
    mathml = (
        '<math xmlns="http://www.w3.org/1998/Math/MathML">'
        '<msup><mrow><mfenced separators="|"><mrow><mn>1</mn><mo>+</mo><mi>x</mi></mrow></mfenced></mrow>'
        '<mrow><mi>n</mi></mrow></msup><mo>=</mo><mn>1</mn><mo>+</mo>'
        '<mfrac><mrow><mi>n</mi><mi>x</mi></mrow><mrow><mn>1</mn><mo>!</mo></mrow></mfrac><mo>+</mo>'
        '<mfrac><mrow><mi>n</mi><mfenced separators="|"><mrow><mi>n</mi><mo>-</mo><mn>1</mn></mrow></mfenced>'
        '<msup><mrow><mi>x</mi></mrow><mrow><mn>2</mn></mrow></msup></mrow><mrow><mn>2</mn><mo>!</mo></mrow></mfrac>'
        '<mo>+</mo><mo>…</mo></math>'
    )
    out_svg = tmp_path / "real_binomial.svg"
    result = render_mathml_to_svg(mathml, out_svg, size_pt=12.0)

    assert result.exists()
    content = result.read_text(encoding="utf-8")
    assert "<svg" in content
    assert "</svg>" in content
    assert "<path" in content


def test_silent_loss_guard_preserves_equation_content(tmp_path: Path):
    """Rendering a=b-c with ASCII '-' must equal explicit U+2212 and differ from a-c."""
    ascii_svg_path = tmp_path / "ascii_minus.svg"
    unicode_svg_path = tmp_path / "unicode_minus.svg"
    short_svg_path = tmp_path / "short_dropped.svg"

    render_mathml_to_svg("<math><mi>a</mi><mo>=</mo><mi>b</mi><mo>-</mo><mi>c</mi></math>", ascii_svg_path)
    render_mathml_to_svg("<math><mi>a</mi><mo>=</mo><mi>b</mi><mo>\u2212</mo><mi>c</mi></math>", unicode_svg_path)
    render_mathml_to_svg("<math><mi>a</mi><mo>-</mo><mi>c</mi></math>", short_svg_path)

    ascii_bytes = ascii_svg_path.read_bytes()
    unicode_bytes = unicode_svg_path.read_bytes()
    short_bytes = short_svg_path.read_bytes()

    # The two outputs must be byte-identical
    assert ascii_bytes == unicode_bytes
    # Must differ from a-c (proves '= b' is NOT dropped)
    assert ascii_bytes != short_bytes


def test_leading_minus_number(tmp_path: Path):
    """A leading-minus number: <math><mn>5</mn><mo>=</mo><mn>-3</mn></math> renders and matches U+2212."""
    ascii_num_path = tmp_path / "ascii_neg.svg"
    unicode_num_path = tmp_path / "unicode_neg.svg"

    render_mathml_to_svg("<math><mn>5</mn><mo>=</mo><mn>-3</mn></math>", ascii_num_path)
    render_mathml_to_svg("<math><mn>5</mn><mo>=</mo><mn>\u22123</mn></math>", unicode_num_path)

    assert ascii_num_path.read_bytes() == unicode_num_path.read_bytes()


def test_invalid_input_still_raises_math_render_error(tmp_path: Path):
    """Non-XML 'hello' and non-math '<a/>' must continue to raise MathRenderError."""
    dummy_path = tmp_path / "fail.svg"

    with pytest.raises(MathRenderError):
        render_mathml_to_svg("hello", dummy_path)

    with pytest.raises(MathRenderError):
        render_mathml_to_svg("<a/>", dummy_path)
