"""
Booklet_AI Desktop — STEP-7.4 Unit and Contract Tests

Validates:
1. Pure Python MathML to SVG rendering via ziamath & ziafont (MIT License).
2. MathRenderError typed exception on invalid or empty MathML.
3. SVG vector generation containing <path> elements for real binomial and trigonometric equations.
4. Acceptance of namespace-prefixed MathML (<mml:math>) and <mfenced>.
5. Payload construction: render_mode="image", absolute svg_path, plain alt_text.
6. Failure fallback: render_mode="placeholder", visible Arabic placeholder text, MATH_RENDER_FAILED warning.
7. Warning reconciliation: MATHML_NOT_RENDERABLE removed on render success; retained on failure.
8. Source of truth invariance: raw_content and content_hash are never mutated.
9. Static JSX contracts: rect.place(), FitOptions.FRAME_TO_CONTENT, MathPlaceError, centered LTR MathFormulaStyle.
10. Strict no-empty-catch policy across all JSX scripts.
11. Pinned requirements.txt and PyInstaller spec data file bundling.
"""

from __future__ import annotations

import json
from pathlib import Path
import re
import pytest

from app.automation.generation_pipeline import build_payload
from app.automation.geometry import MarginConfig, PageGeometry, PageNumberingStyle
from app.automation.styles_schema import TypographyStylesConfig
from app.core.exceptions import BookletError, MathPlaceError, MathRenderError
from app.core.math_renderer import extract_alt_text, normalize_mathml, render_mathml_to_svg
from app.core.models import BookletDocument, ContentBlock, PageData
from app.core.text_integrity import compute_canonical_hash, verify_text_integrity

PROJECT_ROOT = Path(__file__).resolve().parents[2]


# ─── Fixtures ──────────────────────────────────────────────────────────


@pytest.fixture
def sample_binomial_mathml() -> str:
    """Real binomial expansion equation: (1+x)^n = 1 + nx/1! + n(n-1)x^2/2! + ..."""
    return """<math xmlns="http://www.w3.org/1998/Math/MathML">
  <msup>
    <mrow>
      <mo>(</mo>
      <mn>1</mn>
      <mo>+</mo>
      <mi>x</mi>
      <mo>)</mo>
    </mrow>
    <mi>n</mi>
  </msup>
  <mo>=</mo>
  <mn>1</mn>
  <mo>+</mo>
  <mfrac>
    <mrow>
      <mi>n</mi>
      <mi>x</mi>
    </mrow>
    <mrow>
      <mn>1</mn>
      <mo>!</mo>
    </mrow>
  </mfrac>
  <mo>+</mo>
  <mfrac>
    <mrow>
      <mi>n</mi>
      <mo>(</mo>
      <mi>n</mi>
      <mo>−</mo>
      <mn>1</mn>
      <mo>)</mo>
      <msup>
        <mi>x</mi>
        <mn>2</mn>
      </msup>
    </mrow>
    <mrow>
      <mn>2</mn>
      <mo>!</mo>
    </mrow>
  </mfrac>
  <mo>+</mo>
  <mo>…</mo>
</math>"""


@pytest.fixture
def sample_greek_mathml() -> str:
    """Euler / trigonometric formula with Greek letters: cos θ + i sin θ."""
    return """<math xmlns="http://www.w3.org/1998/Math/MathML">
  <mrow>
    <mi>cos</mi>
    <mi>θ</mi>
    <mo>+</mo>
    <mi>i</mi>
    <mi>sin</mi>
    <mi>θ</mi>
  </mrow>
</math>"""


@pytest.fixture
def dummy_geometry() -> PageGeometry:
    return PageGeometry(
        page_size="A4",
        width_pt=595.28,
        height_pt=841.89,
        facing_pages=True,
        margins=MarginConfig(
            top_mm=20.0,
            bottom_mm=20.0,
            inside_spine_mm=22.0,
            outside_mm=15.0,
        ),
        numbering_style=PageNumberingStyle.ARABIC_INDIC,
    )


@pytest.fixture
def dummy_styles() -> TypographyStylesConfig:
    return TypographyStylesConfig(
        body_font="Tajawal",
        math_font="Cambria Math",
        math_size_pt=12.0,
    )


# ─── 1. MathML to SVG Renderer Unit Tests ─────────────────────────────


class TestMathMLRenderer:
    def test_renderer_produces_svg_with_path_for_binomial(self, sample_binomial_mathml: str, tmp_path: Path):
        """Renderer produces an SVG file containing <path> vector elements for the real equation."""
        out_svg = tmp_path / "binomial.svg"
        result_path = render_mathml_to_svg(sample_binomial_mathml, out_svg, size_pt=14.0)

        assert result_path.exists()
        assert result_path == out_svg.resolve()
        svg_content = result_path.read_text(encoding="utf-8")
        assert "<svg" in svg_content
        assert "</svg>" in svg_content
        assert "<path" in svg_content
        assert len(svg_content) > 1000

    def test_renderer_produces_svg_for_greek_characters(self, sample_greek_mathml: str, tmp_path: Path):
        """Renderer converts Greek symbols (cos θ + i sin θ) to vector paths without font issues."""
        out_svg = tmp_path / "greek.svg"
        result_path = render_mathml_to_svg(sample_greek_mathml, out_svg, size_pt=12.0)

        assert result_path.exists()
        svg_content = result_path.read_text(encoding="utf-8")
        assert "<path" in svg_content

    def test_renderer_accepts_namespace_prefixed_mathml(self, tmp_path: Path):
        """Renderer accepts <mml:math ...> and namespace-prefixed tags."""
        ns_mathml = (
            '<mml:math xmlns:mml="http://www.w3.org/1998/Math/MathML">'
            "<mml:mrow><mml:mi>x</mml:mi><mml:mo>+</mml:mo><mml:mi>y</mml:mi></mml:mrow>"
            "</mml:math>"
        )
        out_svg = tmp_path / "ns_math.svg"
        result_path = render_mathml_to_svg(ns_mathml, out_svg, size_pt=12.0)
        assert result_path.exists()
        assert "<path" in result_path.read_text(encoding="utf-8")

    def test_renderer_accepts_mfenced(self, tmp_path: Path):
        """Renderer accepts <mfenced> container elements."""
        mfenced_mathml = (
            '<math xmlns="http://www.w3.org/1998/Math/MathML">'
            '<mfenced open="(" close=")"><mrow><mi>a</mi><mo>+</mo><mi>b</mi></mrow></mfenced>'
            "</math>"
        )
        out_svg = tmp_path / "mfenced.svg"
        result_path = render_mathml_to_svg(mfenced_mathml, out_svg, size_pt=12.0)
        assert result_path.exists()
        assert "<path" in result_path.read_text(encoding="utf-8")

    def test_invalid_mathml_raises_math_render_error(self, tmp_path: Path):
        """Invalid, broken, or non-math XML raises typed MathRenderError."""
        out_svg = tmp_path / "invalid.svg"

        # Non-XML string "hello"
        with pytest.raises(MathRenderError):
            render_mathml_to_svg("hello", out_svg)

        # XML with root other than 'math' ("<a/>")
        with pytest.raises(MathRenderError):
            render_mathml_to_svg("<a/>", out_svg)

        # Empty string
        with pytest.raises(MathRenderError):
            render_mathml_to_svg("", out_svg)

        # Whitespace only
        with pytest.raises(MathRenderError):
            render_mathml_to_svg("   \n\t  ", out_svg)

        # Malformed XML (mismatched tags)
        with pytest.raises(MathRenderError):
            render_mathml_to_svg("<math><mi>x</mo></math>", out_svg)

        # Non-XML unparseable garbage
        with pytest.raises(MathRenderError):
            render_mathml_to_svg("<math><unclosed_tag", out_svg)

    def test_math_render_error_inherits_from_booklet_error(self):
        """MathRenderError must derive from BookletError."""
        assert issubclass(MathRenderError, BookletError)

    def test_extract_alt_text_behavior(self, sample_binomial_mathml: str, sample_greek_mathml: str):
        """extract_alt_text strips tags, unescapes entities, and falls back to 'equation'."""
        alt1 = extract_alt_text(sample_binomial_mathml)
        assert "1 + x" in alt1
        assert "<" not in alt1
        assert ">" not in alt1

        alt2 = extract_alt_text(sample_greek_mathml)
        assert "cos" in alt2
        assert "θ" in alt2
        assert "sin" in alt2

        # Empty / non-text fallback
        assert extract_alt_text("<math></math>") == "equation"
        assert extract_alt_text("") == "equation"


# ─── 2. Payload Construction & Warning Reconciliation Tests ───────────


class TestBuildPayloadMathRendering:
    def test_payload_items_carry_svg_path_and_render_mode_image(
        self,
        sample_binomial_mathml: str,
        dummy_geometry: PageGeometry,
        dummy_styles: TypographyStylesConfig,
        tmp_path: Path,
    ):
        """Successfully rendered math formula gets render_mode='image', svg_path, and alt_text."""
        block = ContentBlock(
            block_id="blk_m001",
            block_type="math_formula",
            raw_content=sample_binomial_mathml,
            content_hash=compute_canonical_hash(sample_binomial_mathml),
            source_position={"body_index": 0, "sub_index": 0},
        )
        doc = BookletDocument(
            doc_id="test_math_doc",
            file_path="C:/test/sample.docx",
            file_type="docx",
            pages=[PageData(page_number=1, width=595.28, height=841.89, content_blocks=[block])],
            warnings=["MATHML_NOT_RENDERABLE"],
        )

        payload_path = build_payload(doc, dummy_geometry, dummy_styles, tmp_path)
        assert payload_path.exists()

        with open(payload_path, "r", encoding="utf-8") as f:
            payload = json.load(f)

        item = payload["items"][0]
        assert item["block_id"] == "blk_m001"
        assert item["block_type"] == "math_formula"
        assert item["render_mode"] == "image"
        assert "svg_path" in item
        assert Path(item["svg_path"]).exists()
        assert Path(item["svg_path"]).is_absolute()
        assert item["alt_text"] != ""
        assert item["alt_text"] != "equation"

        # MathML source of truth remains unchanged
        assert item["raw_content"] == sample_binomial_mathml
        assert item["content_hash"] == block.content_hash
        verify_text_integrity(item["raw_content"], item["content_hash"])

        # Warning reconciliation (STEP-7.4 Requirement 5):
        # MATHML_NOT_RENDERABLE is removed when rendering succeeds
        assert "MATHML_NOT_RENDERABLE" not in doc.warnings
        assert "MATHML_NOT_RENDERABLE" not in payload.get("warnings", [])

        # math_stats reporting
        stats = payload.get("math_stats", {})
        assert stats.get("rendered") == 1
        assert stats.get("failed") == 0

    def test_payload_fallback_to_placeholder_on_render_failure(
        self,
        dummy_geometry: PageGeometry,
        dummy_styles: TypographyStylesConfig,
        tmp_path: Path,
    ):
        """Failed math rendering gets render_mode='placeholder', Arabic text, and warning."""
        invalid_mathml = "<math><mi>mismatched</mo></math>"
        block = ContentBlock(
            block_id="blk_m_fail",
            block_type="math_formula",
            raw_content=invalid_mathml,
            content_hash=compute_canonical_hash(invalid_mathml),
            source_position={"body_index": 0, "sub_index": 0},
        )
        doc = BookletDocument(
            doc_id="test_fail_doc",
            file_path="C:/test/sample.docx",
            file_type="docx",
            pages=[PageData(page_number=1, width=595.28, height=841.89, content_blocks=[block])],
            warnings=["MATHML_NOT_RENDERABLE"],
        )

        payload_path = build_payload(doc, dummy_geometry, dummy_styles, tmp_path)
        with open(payload_path, "r", encoding="utf-8") as f:
            payload = json.load(f)

        item = payload["items"][0]
        assert item["render_mode"] == "placeholder"
        assert item["placeholder_text"] == "[معادلة تحتاج مراجعة]"
        # Raw XML is NEVER injected into InDesign output
        assert "<math" not in item["placeholder_text"]

        # Warnings: MATH_RENDER_FAILED emitted, and MATHML_NOT_RENDERABLE kept
        assert "MATH_RENDER_FAILED:blk_m_fail" in doc.warnings
        assert "MATHML_NOT_RENDERABLE" in doc.warnings

        stats = payload.get("math_stats", {})
        assert stats.get("rendered") == 0
        assert stats.get("failed") == 1


# ─── 3. Static JSX Contracts & Script Verification Tests ──────────────


class TestStaticJSXMathContracts:
    @pytest.fixture(autouse=True)
    def _load_scripts(self):
        scripts_dir = PROJECT_ROOT / "src" / "assets" / "scripts"
        self.itm_path = scripts_dir / "inject_tables_and_math.jsx"
        self.itm_content = self.itm_path.read_text(encoding="utf-8")

        self.gb_path = scripts_dir / "generate_booklet.jsx"
        self.gb_content = self.gb_path.read_text(encoding="utf-8")

    def test_rect_place_and_frame_to_content(self):
        """inject_tables_and_math.jsx places SVG and fits frame to content."""
        assert ".rectangles.add(" in self.itm_content
        assert "rect.place(" in self.itm_content or ".place(" in self.itm_content
        assert "FitOptions.FRAME_TO_CONTENT" in self.itm_content

    def test_inline_anchoring_configured(self):
        """Inline anchoring is explicitly configured on the anchored rectangle."""
        assert "AnchorPosition.INLINE_POSITION" in self.itm_content
        assert "anchoredPosition" in self.itm_content

    def test_math_place_error_thrown_on_failure(self):
        """MathPlaceError is thrown if placing the SVG fails."""
        assert "MathPlaceError" in self.itm_content
        assert re.search(r'throw\s+new\s+Error\(\s*"MathPlaceError', self.itm_content)

    def test_math_place_error_class_in_python(self):
        """MathPlaceError is defined in Python exceptions and mapped in com_supervisor."""
        assert issubclass(MathPlaceError, BookletError)

    def test_centered_ltr_math_formula_style(self):
        """Math styling sets MathFormulaStyle, LTR direction, and centered alignment."""
        assert "styles.mathFormulaStyle" in self.itm_content
        assert "ParagraphDirectionOptions.LEFT_TO_RIGHT_DIRECTION" in self.itm_content
        assert "Justification.CENTER_ALIGN" in self.itm_content
        assert "Defense in Depth" in self.itm_content

    def test_placeholder_text_in_jsx(self):
        """Placeholder branch injects Arabic placeholder text, never raw XML."""
        assert "[معادلة تحتاج مراجعة]" in self.itm_content

    def test_generate_booklet_dispatches_math_item(self):
        """generate_booklet.jsx passes the item object to injectMathFormulaBlock."""
        assert "injectMathFormulaBlock(story, item, styles)" in self.gb_content
        assert "ensureFrameCapacity" in self.gb_content

    def test_no_empty_catch_blocks_in_all_jsx_scripts(self):
        """Strict contract: zero empty catch blocks across all JSX scripts."""
        scripts_dir = PROJECT_ROOT / "src" / "assets" / "scripts"
        jsx_files = list(scripts_dir.glob("*.jsx"))
        assert len(jsx_files) > 0

        empty_catch_pattern = re.compile(r"catch\s*\([^\)]*\)\s*\{([^}]*)\}")
        violations = []

        for jf in jsx_files:
            content = jf.read_text(encoding="utf-8")
            for match in empty_catch_pattern.finditer(content):
                body = match.group(1).strip()
                # Strip single-line and multi-line comments
                body_clean = re.sub(r"//[^\r\n]*", "", body)
                body_clean = re.sub(r"/\*[\s\S]*?\*/", "", body_clean).strip()
                if not body_clean:
                    violations.append(f"{jf.name}: {match.group(0)}")

        assert not violations, f"Found empty catch block(s) in JSX files: {violations}"


# ─── 4. Dependencies & PyInstaller Packaging Contracts ────────────────


class TestPackagingAndDependencies:
    def test_requirements_contains_pinned_ziamath_and_ziafont(self):
        """requirements.txt must contain pinned ziamath and ziafont."""
        req_file = PROJECT_ROOT / "src" / "requirements.txt"
        content = req_file.read_text(encoding="utf-8")
        assert re.search(r"^ziamath==\d+\.\d+", content, re.MULTILINE)
        assert re.search(r"^ziafont==\d+\.\d+", content, re.MULTILINE)

    def test_spec_bundles_ziamath_and_ziafont_datas(self):
        """booklet_ai.spec must collect data files for ziamath and ziafont."""
        spec_file = PROJECT_ROOT / "src" / "booklet_ai.spec"
        content = spec_file.read_text(encoding="utf-8")
        assert 'collect_data_files("ziamath")' in content
        assert 'collect_data_files("ziafont")' in content
        assert '"app.core.math_renderer"' in content
