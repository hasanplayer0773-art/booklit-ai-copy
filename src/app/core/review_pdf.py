"""
Booklet_AI Desktop — Quick PDF Export for Review (STEP-6.5)

Provides a lightweight, InDesign-independent export path that generates
a reviewable PDF directly from classified ContentBlocks.

Architectural guarantees (STEP-6.5):
  - Zero COM / Zero InDesign: Never invokes COMSupervisor, win32com, or
    pythoncom (AC-6.5.1).
  - Review Watermark / Marker: Every page carries a visible "معاينة / PREVIEW"
    marker and banner to prevent confusion with final print output (AC-6.5.2).
  - Standalone data source: Operates entirely on BookletDocument / DocumentStore
    data without requiring .idml templates or InDesign styles (AC-6.5.3).
  - Arabic Shaping & BiDi: Uses WeasyPrint (backed by Pango and HarfBuzz)
    to render shaped Arabic glyphs, diacritics, and RTL text order (AC-6.5.4).
  - Dedicated MathML Rendering: Pre-renders MathML to SVG via ziamath before
    HTML assembly, rendered strictly LTR (AC-6.5.5).
  - Legacy Equation Gap: Displays legacy_equation_placeholder as a clearly
    labeled callout box, never blank or omitted (AC-6.5.7).
  - Zero-Mutation: Verifies SHA-256 canonical hash of all blocks before rendering.
"""

from __future__ import annotations

import base64
import html
import logging
import os
import re
import sys
from pathlib import Path
from typing import Any, List, Optional, Union

import ziamath as zm

from app.core.models import BookletDocument, ContentBlock, PageData, TableData
from app.core.text_integrity import compute_canonical_hash, verify_text_integrity

logger = logging.getLogger("booklet_ai.review_pdf")

# ─── Constant Markers & Text ───────────────────────────────────────────
REVIEW_WATERMARK_TEXT = "معاينة / PREVIEW"
REVIEW_HEADER_TEXT = "معاينة فقط — غير مخصص للطباعة | PREVIEW ONLY — NOT FOR PRINT"
REVIEW_FOOTER_PREFIX = "معاينة سريعة — Booklet_AI Review Export | صفحة "
LEGACY_EQUATION_LABEL = "معادلة قديمة — تحتاج معالجة يدوية في InDesign"

# ─── Environment / DLL Setup for WeasyPrint on Windows ─────────────────
_DLL_DIRECTORIES_CONFIGURED = False


def setup_weasyprint_environment() -> bool:
    """Configure DLL search paths on Windows to resolve Pango, Cairo, and GLib.

    Searches known standard bundling and runtime paths so that WeasyPrint
    can load its native C dependencies under Windows without requiring a
    global system GTK installer.

    Returns:
        True if at least one candidate directory was located and registered.
    """
    global _DLL_DIRECTORIES_CONFIGURED
    if _DLL_DIRECTORIES_CONFIGURED:
        return True

    candidate_dirs: List[Path] = []

    # 0. Explicit environment variable override
    env_override = os.environ.get("BOOKLET_GTK_PATH")
    if env_override:
        candidate_dirs.append(Path(env_override))

    # 1. PyInstaller bundled directory (STEP-4.1 onedir build)
    meipass = getattr(sys, "_MEIPASS", None)
    if meipass:
        candidate_dirs.append(Path(meipass) / "assets" / "gtk_runtime")
        candidate_dirs.append(Path(meipass))

    # 2. Local project assets directory
    file_path = Path(__file__).resolve()
    # file_path.parents[2] is 'src' if file is in 'src/app/core'
    candidate_dirs.append(file_path.parents[2] / "assets" / "gtk_runtime")
    if len(file_path.parents) > 3:
        candidate_dirs.append(file_path.parents[3] / "src" / "assets" / "gtk_runtime")
        candidate_dirs.append(file_path.parents[3] / "assets" / "gtk_runtime")

    # 3. User local runtime cache
    local_app_data = os.environ.get("LOCALAPPDATA")
    if local_app_data:
        candidate_dirs.append(
            Path(local_app_data) / "weasyprint" / "onedir" / "weasyprint" / "_internal"
        )

    # 4. Standard GTK3 installation paths (fallback)
    candidate_dirs.append(Path(r"C:\Program Files\GTK3-Runtime Win64\bin"))
    candidate_dirs.append(Path(r"C:\msys64\mingw64\bin"))

    configured = False
    for c_dir in candidate_dirs:
        if c_dir.is_dir() and (c_dir / "libgobject-2.0-0.dll").exists():
            try:
                if hasattr(os, "add_dll_directory"):
                    os.add_dll_directory(str(c_dir))
                # Also prepend to PATH for cffi/ctypes fallback
                curr_path = os.environ.get("PATH", "")
                if str(c_dir) not in curr_path:
                    os.environ["PATH"] = str(c_dir) + os.pathsep + curr_path
                logger.info("Registered WeasyPrint DLL directory: %s", c_dir)
                configured = True
                break
            except Exception as exc:
                logger.warning("Failed to register DLL directory %s: %s", c_dir, exc)

    _DLL_DIRECTORIES_CONFIGURED = True
    return configured


# ─── MathML to SVG Renderer (AC-6.5.5) ─────────────────────────────────


def render_mathml_to_svg(mathml_string: str) -> str:
    """Convert MathML markup into a standalone SVG vector image string.

    Uses ziamath for font-embedded, standalone MathML rendering without
    requiring external TeX or browser runtimes. The resulting SVG is oriented
    strictly Left-to-Right (LTR).

    Args:
        mathml_string: MathML markup string (e.g. '<math>...</math>').

    Returns:
        Rendered SVG string.
    """
    raw = mathml_string.strip()
    if not raw:
        return '<svg xmlns="http://www.w3.org/2000/svg" width="10" height="10"></svg>'

    # Ensure MathML namespace wrapper if omitted
    if not raw.startswith("<math"):
        raw = f'<math xmlns="http://www.w3.org/1998/Math/MathML">{raw}</math>'
    elif "xmlns=" not in raw and "xmlns:" not in raw:
        raw = raw.replace("<math", '<math xmlns="http://www.w3.org/1998/Math/MathML"', 1)

    try:
        math_elem = zm.Math(raw)
        svg_output = math_elem.svg()
        if isinstance(svg_output, bytes):
            return svg_output.decode("utf-8")
        return str(svg_output)
    except Exception as exc:
        logger.warning("ziamath failed to parse MathML; producing text fallback SVG: %s", exc)
        # Safe fallback SVG displaying the equation text cleanly
        clean_text = re.sub(r"<[^>]+>", "", raw).strip() or "معادلة رياضية"
        escaped_text = html.escape(clean_text)
        return (
            f'<svg xmlns="http://www.w3.org/2000/svg" width="200" height="30" viewBox="0 0 200 30">'
            f'<rect width="200" height="30" fill="#f8f9fa" stroke="#ced4da" rx="4"/>'
            f'<text x="10" y="20" font-family="monospace" font-size="12" fill="#333" direction="ltr">'
            f'{escaped_text}</text></svg>'
        )


def mathml_to_data_uri(mathml_string: str) -> str:
    """Convert MathML markup directly to an SVG data: URI for HTML embedding."""
    svg_str = render_mathml_to_svg(mathml_string)
    b64_data = base64.b64encode(svg_str.encode("utf-8")).decode("ascii")
    return f"data:image/svg+xml;base64,{b64_data}"


# ─── Review HTML Template Builder ──────────────────────────────────────


class ReviewHTMLBuilder:
    """Builds the review-optimized HTML document from BookletDocument blocks."""

    def __init__(self, doc: BookletDocument):
        self._doc = doc

    def build_html(self) -> str:
        """Construct full HTML document with styles, watermark, and content."""
        pages_html: List[str] = []

        for page in self._doc.pages:
            page_content: List[str] = []

            # 1. Blocks on this page
            for block in page.content_blocks:
                block_html = self._render_block(block)
                if block_html:
                    page_content.append(block_html)

            # 2. Tables on this page
            for table in page.tables:
                page_content.append(self._render_table(table))

            pages_html.append(
                f'<div class="booklet-page">\n'
                f'  {"".join(page_content)}\n'
                f'</div>'
            )

        body_inner = "\n".join(pages_html)

        return f"""<!DOCTYPE html>
<html dir="rtl" lang="ar">
<head>
<meta charset="utf-8">
<title>معاينة مستند — Booklet_AI Review</title>
<style>
{self._get_css_styles()}
</style>
</head>
<body>
<div class="review-watermark-overlay" aria-hidden="true">{REVIEW_WATERMARK_TEXT}</div>
<div class="preview-banner">
    ⚠️ <strong>{REVIEW_WATERMARK_TEXT}</strong> — {REVIEW_HEADER_TEXT}
</div>
{body_inner}
</body>
</html>"""

    def _render_block(self, block: ContentBlock) -> str:
        btype = block.block_type
        content = block.raw_content

        if btype == "chapter_header":
            return f'<h1 class="chapter-header" dir="rtl">{html.escape(content)}</h1>'

        elif btype == "arabic_text":
            return f'<p class="arabic-text" dir="rtl">{html.escape(content)}</p>'

        elif btype == "english_text":
            return f'<p class="english-text" dir="ltr">{html.escape(content)}</p>'

        elif btype == "ministry_question":
            return (
                f'<div class="ministry-question" dir="rtl">'
                f'  <span class="q-badge">سؤال وزاري</span>'
                f'  <div class="q-content">{html.escape(content)}</div>'
                f'</div>'
            )

        elif btype == "math_formula":
            data_uri = mathml_to_data_uri(content)
            return (
                f'<div class="math-block" dir="ltr">'
                f'  <img class="math-svg" src="{data_uri}" alt="Mathematical Formula" />'
                f'</div>'
            )

        elif btype == "chemistry_formula":
            return f'<div class="chemistry-formula" dir="ltr">{html.escape(content)}</div>'

        elif btype == "list_item":
            return f'<div class="list-item" dir="rtl">• {html.escape(content)}</div>'

        elif btype == "legacy_equation_placeholder":
            return (
                f'<div class="legacy-equation-placeholder" dir="rtl">'
                f'  <div class="legacy-title">⚠️ {LEGACY_EQUATION_LABEL}</div>'
                f'  <div class="legacy-details">{html.escape(content)}</div>'
                f'</div>'
            )

        elif btype == "image_prompt":
            asset_info = block.source_position.get("image_asset")
            source_path = None
            if isinstance(asset_info, dict):
                source_path = asset_info.get("source_path")
            elif hasattr(asset_info, "source_path"):
                source_path = asset_info.source_path

            if source_path and Path(source_path).exists():
                file_url = Path(source_path).as_uri()
                return (
                    f'<div class="image-container" dir="ltr">'
                    f'  <img src="{file_url}" alt="Extracted graphic" class="extracted-img" />'
                    f'</div>'
                )
            return (
                f'<div class="image-placeholder" dir="rtl">'
                f'  🖼 [عنصر صورة: {html.escape(content)}]'
                f'</div>'
            )

        # Fallback for unexpected block types
        return f'<div class="generic-block" dir="rtl">{html.escape(content)}</div>'

    def _render_table(self, table: TableData) -> str:
        rows_html: List[str] = []

        if table.headers:
            th_cells = "".join(f"<th>{html.escape(h)}</th>" for h in table.headers)
            rows_html.append(f"<thead><tr>{th_cells}</tr></thead>")

        tbody_rows: List[str] = []
        for row in table.rows:
            td_cells = "".join(f"<td>{html.escape(cell)}</td>" for cell in row)
            tbody_rows.append(f"<tr>{td_cells}</tr>")

        if tbody_rows:
            rows_html.append(f"<tbody>{''.join(tbody_rows)}</tbody>")

        return (
            f'<div class="table-container" dir="rtl">'
            f'  <table class="review-table">'
            f'    {"".join(rows_html)}'
            f'  </table>'
            f'</div>'
        )

    def _get_css_styles(self) -> str:
        return f"""
        @page {{
            size: A4 portrait;
            margin: 22mm 18mm 22mm 18mm;
            @top-center {{
                content: "{REVIEW_HEADER_TEXT}";
                font-family: "Segoe UI", Arial, sans-serif;
                font-size: 8pt;
                font-weight: bold;
                color: #d32f2f;
                border-bottom: 1px dashed #ef9a9a;
                padding-bottom: 4px;
            }}
            @bottom-center {{
                content: "{REVIEW_FOOTER_PREFIX}" counter(page);
                font-family: "Segoe UI", Arial, sans-serif;
                font-size: 8pt;
                color: #757575;
            }}
        }}

        body {{
            font-family: "Traditional Arabic", "Segoe UI", "Tahoma", sans-serif;
            font-size: 11pt;
            line-height: 1.6;
            color: #212121;
            margin: 0;
            padding: 0;
            position: relative;
        }}

        .booklet-page {{
            page-break-after: always;
            break-after: page;
        }}

        .booklet-page:last-child {{
            page-break-after: auto;
            break-after: auto;
        }}

        /* Subtle diagonal watermark on background */
        .review-watermark-overlay {{
            position: fixed;
            top: 35%;
            left: 5%;
            width: 90%;
            text-align: center;
            font-size: 65pt;
            font-weight: 900;
            color: rgba(220, 53, 69, 0.08);
            transform: rotate(-30deg);
            pointer-events: none;
            z-index: -1;
            font-family: Arial, sans-serif;
        }}

        .preview-banner {{
            background-color: #ffebee;
            border: 1px solid #ef5350;
            border-radius: 4px;
            color: #b71c1c;
            padding: 6px 12px;
            font-size: 9pt;
            text-align: center;
            margin-bottom: 16px;
        }}

        .chapter-header {{
            font-size: 18pt;
            font-weight: bold;
            color: #0d47a1;
            border-bottom: 2px solid #1976d2;
            padding-bottom: 6px;
            margin-top: 20px;
            margin-bottom: 14px;
        }}

        .arabic-text {{
            text-align: justify;
            margin-bottom: 10px;
        }}

        .english-text {{
            font-family: "Segoe UI", Arial, sans-serif;
            text-align: left;
            margin-bottom: 10px;
            color: #263238;
        }}

        .ministry-question {{
            background-color: #f3e5f5;
            border-right: 4px solid #8e24aa;
            padding: 10px 14px;
            margin: 14px 0;
            border-radius: 2px 4px 4px 2px;
        }}

        .q-badge {{
            display: inline-block;
            background-color: #8e24aa;
            color: #ffffff;
            font-size: 8pt;
            font-weight: bold;
            padding: 2px 8px;
            border-radius: 3px;
            margin-bottom: 6px;
        }}

        .math-block {{
            margin: 14px 0;
            text-align: center;
            background-color: #fbfbfb;
            padding: 8px;
            border-radius: 4px;
        }}

        .math-svg {{
            max-width: 90%;
            height: auto;
            vertical-align: middle;
        }}

        .chemistry-formula {{
            font-family: "Courier New", monospace;
            background-color: #e8f5e9;
            border-left: 3px solid #43a047;
            padding: 6px 10px;
            margin: 8px 0;
        }}

        .list-item {{
            margin: 4px 0 4px 12px;
        }}

        /* AC-6.5.7: Clearly labeled gap for legacy equation placeholders */
        .legacy-equation-placeholder {{
            background-color: #fff8e1;
            border: 2px dashed #ffa000;
            border-radius: 6px;
            padding: 10px 14px;
            margin: 14px 0;
            color: #e65100;
        }}

        .legacy-title {{
            font-weight: bold;
            font-size: 10pt;
            margin-bottom: 4px;
        }}

        .legacy-details {{
            font-size: 9pt;
            color: #795548;
        }}

        .image-container {{
            text-align: center;
            margin: 12px 0;
        }}

        .extracted-img {{
            max-width: 85%;
            max-height: 120mm;
            border: 1px solid #e0e0e0;
            border-radius: 3px;
        }}

        .image-placeholder {{
            border: 1px dashed #9e9e9e;
            padding: 16px;
            text-align: center;
            color: #757575;
            background-color: #fafafa;
            margin: 12px 0;
        }}

        .review-table {{
            width: 100%;
            border-collapse: collapse;
            margin: 12px 0;
        }}

        .review-table th, .review-table td {{
            border: 1px solid #bdbdbd;
            padding: 6px 10px;
            font-size: 9.5pt;
        }}

        .review-table th {{
            background-color: #eeeeee;
            font-weight: bold;
        }}
        """


# ─── Public Review PDF Generation API ───────────────────────────────────


def generate_review_pdf(
    doc: BookletDocument,
    output_path: Union[str, Path],
    verify_integrity: bool = True,
) -> Path:
    """Generate a quick review PDF directly from a BookletDocument.

    Executes entirely in the local Python layer using WeasyPrint and ziamath.
    Never connects to InDesign or touches COM (AC-6.5.1).

    Args:
        doc: The BookletDocument instance to render.
        output_path: Destination path on disk for the review PDF.
        verify_integrity: Whether to enforce the Zero-Mutation canonical hash
                          guarantee on all blocks before rendering.

    Returns:
        Path object pointing to the created review PDF.

    Raises:
        TextIntegrityError: If any content block fails SHA-256 integrity.
        RuntimeError: If WeasyPrint cannot render the document.
    """
    # 1. Zero-Mutation integrity check
    if verify_integrity:
        for page in doc.pages:
            for block in page.content_blocks:
                verify_text_integrity(
                    block.raw_content,
                    block.content_hash,
                    context=f"Review PDF block {block.block_id}",
                )

    # 2. Setup environment so WeasyPrint can locate Windows DLLs
    setup_weasyprint_environment()

    # 3. Import WeasyPrint inside function to avoid module-level failures
    #    if DLLs are configured dynamically
    from weasyprint import HTML

    # 4. Assemble HTML with rendered MathML SVGs and watermarks
    builder = ReviewHTMLBuilder(doc)
    html_content = builder.build_html()

    # 5. Render to PDF
    out = Path(output_path).resolve()
    out.parent.mkdir(parents=True, exist_ok=True)

    try:
        HTML(string=html_content).write_pdf(target=str(out))
    except Exception as exc:
        logger.exception("WeasyPrint review PDF rendering failed")
        raise RuntimeError(f"Quick review PDF export failed: {exc}") from exc

    if not out.exists() or out.stat().st_size == 0:
        raise RuntimeError(f"Review PDF was not generated or is empty at: {out}")

    logger.info("Successfully generated review PDF at: %s (%d bytes)", out, out.stat().st_size)
    return out


def generate_review_pdf_from_store(
    store: Any,
    output_path: Union[str, Path],
    doc_id: Optional[str] = None,
    verify_integrity: bool = True,
) -> Path:
    """Generate a quick review PDF directly from a DocumentStore instance.

    Convenience wrapper that extracts the BookletDocument from the store
    and generates the review PDF.

    Args:
        store: DocumentStore instance containing loaded documents.
        output_path: Destination path on disk for the review PDF.
        doc_id: Optional document ID. If None, uses store's active document.
        verify_integrity: Whether to enforce Zero-Mutation hash verification.

    Returns:
        Path object pointing to the created review PDF.

    Raises:
        ValueError: If no document is found in the store.
        TextIntegrityError: If any content block fails SHA-256 integrity.
    """
    doc = store.get_document(doc_id)
    if not doc:
        raise ValueError("No document found in DocumentStore to export")
    return generate_review_pdf(doc, output_path, verify_integrity=verify_integrity)

