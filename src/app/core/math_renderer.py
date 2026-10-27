"""
Booklet_AI Desktop — MathML to SVG Vector Renderer (STEP-7.4)

Renders MathML expressions into standalone, font-embedded SVG vector image files
suitable for inline placement in Adobe InDesign via ExtendScript.
Uses ziamath (pure Python, MIT license) and ziafont (pure Python, MIT license).
"""

from __future__ import annotations

import html
import logging
from pathlib import Path
import re
from typing import Union
import xml.etree.ElementTree as ET

import ziamath as zm

# Disable SVG2 (<symbol> + <use>) output so InDesign's SVG import engine
# receives plain <path> elements and renders glyphs correctly.
zm.config.svg2 = False

from app.core.exceptions import MathRenderError

logger = logging.getLogger(__name__)


def validate_mathml_root(mathml: str) -> None:
    """Parse the input as XML and require the root element local-name to be 'math' (any namespace prefix).

    Args:
        mathml: MathML string to validate.

    Raises:
        MathRenderError: If input is not valid XML or root element local-name is not 'math'.
    """
    if not mathml or not isinstance(mathml, str):
        raise MathRenderError("MathML input is empty or not a string")

    s = mathml.strip()
    if not s:
        raise MathRenderError("MathML input is empty")

    # Strip XML declaration (e.g. <?xml version="1.0" ... ?>)
    s = re.sub(r"<\?xml[^>]*\?>", "", s).strip()

    try:
        root = ET.fromstring(s)
    except Exception:
        # Check if namespace prefix declaration was missing (e.g. <mml:math> without xmlns:mml)
        s_declared = re.sub(
            r"<([a-zA-Z0-9_]+):([a-zA-Z0-9_]+)(?=[\s>])",
            r'<\1:\2 xmlns:\1="http://www.w3.org/1998/Math/MathML"',
            s,
            count=1,
        )
        try:
            root = ET.fromstring(s_declared)
        except Exception as exc:
            raise MathRenderError(f"MathML input is not valid XML: {exc}") from exc

    tag = root.tag
    local_name = tag.split("}")[-1] if "}" in tag else tag
    if ":" in local_name:
        local_name = local_name.split(":")[-1]

    if local_name.lower() != "math":
        raise MathRenderError(
            f"Expected MathML root element to be 'math', got '{local_name}'"
        )


def normalize_mathml(mathml: str) -> str:
    """Normalize MathML markup to ensure robust parsing across various sources.

    Validates that the input is valid XML with root element local-name 'math',
    then normalizes namespace prefixes (<mml:math> -> <math>) and ensures the
    standard MathML namespace is declared.

    Args:
        mathml: Raw MathML markup string.

    Returns:
        Cleaned, well-formed MathML string.

    Raises:
        MathRenderError: If input is empty, not valid XML, or root element is not 'math'.
    """
    validate_mathml_root(mathml)

    s = mathml.strip()
    # Strip XML declaration (e.g. <?xml version="1.0" ... ?>)
    s = re.sub(r"<\?xml[^>]*\?>", "", s).strip()

    # Normalize namespace prefixes: <mml:foo -> <foo and </mml:foo -> </foo
    s = re.sub(r"<(/?)mml:", r"<\1", s)

    # Remove xmlns:mml="..." attribute if present to avoid syntax issues after stripping prefix
    s = re.sub(r'\s+xmlns:mml=(["\'][^"\']*["\'])', "", s)

    # Ensure default MathML namespace on root <math> tag
    if "xmlns=" not in s:
        s = s.replace("<math", '<math xmlns="http://www.w3.org/1998/Math/MathML"', 1)

    return s


def _replace_ascii_minus(root: ET.Element) -> None:
    """Replace ASCII minus '-' with Unicode minus '\u2212' in <mo> and leading <mn>.

    Works around a bug in ziamath 0.13 where ziamath.escapes.unescape() runs greedy regexes:
    - re.sub(r'<mo.*>\\s*-\\s*</mo>', '<mo> − </mo>', xml)
    - re.sub(r'<mn.*>\\s*-', '<mn>−', xml)
    The '.*' is greedy, causing content between the first <mo>/<mn> and the minus sign to be
    deleted when passed a string (producing mismatched tag errors or silent content loss).
    By replacing ASCII minus directly on the ElementTree and passing the Element to ziamath.Math,
    ziamath's unescape() is never called.
    """
    for elem in root.iter():
        tag = elem.tag.split("}")[-1] if "}" in elem.tag else elem.tag
        if tag == "mo":
            if elem.text is not None and elem.text.strip() == "-":
                elem.text = "\u2212"
        elif tag == "mn":
            if elem.text is not None and elem.text.lstrip().startswith("-"):
                lstripped = elem.text.lstrip()
                leading_spaces = elem.text[: len(elem.text) - len(lstripped)]
                elem.text = leading_spaces + "\u2212" + lstripped[1:]


def render_mathml_to_svg(
    mathml: str,
    out_path: Union[Path, str],
    size_pt: float = 12.0,
) -> Path:
    """Render a MathML equation string into a standalone SVG vector image file.

    Uses ziamath.Math(element, size=size_pt).svg() to produce pure vector paths
    with embedded glyph data, requiring no external LaTeX distribution or InDesign fonts.
    Parses the input as XML, requires the root element local-name to be 'math',
    and passes the parsed ElementTree Element to bypass ziamath's greedy unescape regex bug.

    Args:
        mathml: MathML string (supports <mml:math>, <mfenced>, etc.).
        out_path: File system path where the SVG file will be written.
        size_pt: Target font size in typographical points (pt). Defaults to 12.0.

    Returns:
        Resolved Path to the created SVG file.

    Raises:
        MathRenderError: If parsing or rendering fails, or root element is not 'math'.
    """
    try:
        norm_mathml = normalize_mathml(mathml)
        root = ET.fromstring(norm_mathml)
        _replace_ascii_minus(root)
        math_obj = zm.Math(root, size=float(size_pt))
        svg_content = math_obj.svg()

        if isinstance(svg_content, bytes):
            svg_str = svg_content.decode("utf-8")
        else:
            svg_str = str(svg_content)

        if not svg_str or "<svg" not in svg_str:
            raise MathRenderError("Renderer produced empty or malformed SVG output")

        target = Path(out_path).resolve()
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(svg_str, encoding="utf-8")
        return target

    except MathRenderError:
        raise
    except Exception as exc:
        logger.error("Failed to render MathML to SVG: %s", exc)
        raise MathRenderError(f"Failed to render MathML to SVG: {exc}") from exc


def extract_alt_text(mathml: str) -> str:
    """Extract a short, human-readable plain text representation from MathML for alt text.

    Strips XML tags and unescapes XML/HTML entities. Returns a concise fallback
    string 'equation' if extraction produces empty text.

    Args:
        mathml: MathML string.

    Returns:
        Clean plain text string or 'equation'.
    """
    if not mathml or not isinstance(mathml, str):
        return "equation"

    try:
        # Strip XML tags
        text = re.sub(r"<[^>]+>", " ", mathml)
        # Decode entities (&theta;, &#x03B8;, &amp;, etc.)
        text = html.unescape(text)
        # Collapse whitespace
        words = text.split()
        if not words:
            return "equation"
        cleaned = " ".join(words)
        # Keep alt text reasonably concise (up to 120 chars)
        if len(cleaned) > 120:
            return cleaned[:117] + "..."
        return cleaned
    except Exception:
        return "equation"
