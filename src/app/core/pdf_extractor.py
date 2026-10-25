"""
Booklet_AI Desktop — STEP-1.3: PDF Extraction Engine
Geometry, Table Deduplication, Collinear Math

Extracts content from PDF files using PyMuPDF with:
- Geometric table isolation and deduplication
- Collinear math fragment merging
- Multi-column spatial reading order correction
- Safe margin clipping for headers/footers
- Full text integrity verification via SHA-256

Design contract:
- Every text block carries a content_hash matching compute_canonical_hash().
- Table cell text is never duplicated as standalone text blocks.
- Math fragments sharing a baseline (±3pt) are merged into one block.
- Multi-column pages are read right-to-left, top-to-bottom (Arabic order).
- Full-width elements get their own horizontal band before column clustering.
"""

import logging
import os
import tempfile
import uuid
from typing import List, Optional, Tuple

import fitz  # PyMuPDF

from app.core.exceptions import BookletError
from app.core.models import (
    BookletDocument,
    ContentBlock,
    ImageAsset,
    PageData,
    TableData,
)
from app.core.text_integrity import compute_canonical_hash, verify_text_integrity

logger = logging.getLogger(__name__)


# ─── Custom Exception ──────────────────────────────────────────────────

class PDFExtractionError(BookletError):
    """Raised when PDF structure extraction fails."""


# ─── Constants ─────────────────────────────────────────────────────────

# Geometric overlap threshold for excluding text blocks that belong to tables
TABLE_OVERLAP_THRESHOLD: float = 0.50

# Minimum horizontal gap (points) between column clusters to be treated as
# separate columns rather than justified spacing within one column
COLUMN_GAP_THRESHOLD_PT: float = 20.0

# Baseline tolerance (points) for merging adjacent math fragments
MATH_BASELINE_TOLERANCE_PT: float = 3.0

# Safe margin clipping for header/footer exclusion (points from page edge)
HEADER_MARGIN_PT: float = 50.0
FOOTER_MARGIN_PT: float = 50.0

# Full-width ratio threshold: bbox width >= this ratio of page width => full-width band
FULL_WIDTH_RATIO_THRESHOLD: float = 0.80


# ─── Spatial Reading Order ─────────────────────────────────────────────

def sort_blocks_reading_order(blocks: List[dict], page_width: float) -> List[dict]:
    """
    Corrects PyMuPDF's raw content-stream block order into visual Arabic
    reading order for multi-column pages. Each block dict must carry a
    'bbox' key (x0, y0, x1, y1).

    Correction to an earlier version of this function: a naive single-pass
    x-coordinate clustering fails on pages where a full-width element (a
    chapter title or a wide ministry_question banner) spans both columns —
    such a block's bbox overlaps both column x-ranges simultaneously, so
    x-only clustering merges it incorrectly into one column's cluster and
    scrambles the true sequence. This version first decomposes the page
    into horizontal bands, and only applies column clustering within bands
    that are NOT full-width.
    """
    if not blocks:
        return blocks

    # 1. Sort all blocks top-to-bottom first, to walk the page in Y order
    by_y = sorted(blocks, key=lambda b: b["bbox"][1])

    # 2. Decompose into bands: a full-width block is its own band;
    #    consecutive non-full-width blocks with overlapping Y ranges are
    #    grouped into one multi-column band.
    bands: List[List[dict]] = []
    current_band: List[dict] = []

    def block_is_full_width(blk: dict) -> bool:
        width = blk["bbox"][2] - blk["bbox"][0]
        return (width / page_width) >= FULL_WIDTH_RATIO_THRESHOLD if page_width > 0 else False

    for blk in by_y:
        if block_is_full_width(blk):
            if current_band:
                bands.append(current_band)
                current_band = []
            bands.append([blk])  # full-width block is its own single-item band
        else:
            current_band.append(blk)
    if current_band:
        bands.append(current_band)

    # 3. Within each multi-column band (skip single-item full-width bands),
    #    cluster by x-coordinate and order right-to-left, top-to-bottom —
    #    same logic as the original column-clustering pass, now scoped
    #    correctly to only the blocks that are genuinely multi-column.
    ordered: List[dict] = []
    for band in bands:
        if len(band) == 1:
            ordered.extend(band)
            continue

        sorted_by_x = sorted(band, key=lambda b: b["bbox"][0])
        clusters: List[List[dict]] = [[sorted_by_x[0]]]
        for blk in sorted_by_x[1:]:
            prev_x1 = max(b["bbox"][2] for b in clusters[-1])
            if blk["bbox"][0] - prev_x1 > COLUMN_GAP_THRESHOLD_PT:
                clusters.append([blk])
            else:
                clusters[-1].append(blk)

        # Arabic reading order: right-to-left columns
        clusters.sort(
            key=lambda cluster: min(b["bbox"][0] for b in cluster),
            reverse=True,
        )

        for cluster in clusters:
            cluster.sort(key=lambda b: b["bbox"][1])
            ordered.extend(cluster)

    return ordered


# ─── Collinear Math Fragment Merging ───────────────────────────────────

def _is_math_like(text: str) -> bool:
    """Heuristic: returns True if text looks like a math fragment.

    Detects text containing math operators, Greek letters, or consisting
    primarily of symbols/digits rather than natural language words.
    """
    if not text or not text.strip():
        return False

    math_chars = set("+-×÷=≠<>≤≥∑∏∫∂√∞±∓∈∉⊂⊃∪∩∧∨¬∀∃αβγδεζηθικλμνξπρστυφχψω")
    stripped = text.strip()

    # Contains math operator or Greek letter
    if any(c in math_chars for c in stripped):
        return True

    # Primarily digits and operators (like "2x + 3")
    non_alpha = sum(1 for c in stripped if not c.isalpha() or c in math_chars)
    if len(stripped) > 0 and non_alpha / len(stripped) >= 0.5:
        return True

    return False


def merge_collinear_math_fragments(
    blocks: List[dict],
) -> List[dict]:
    """Merge horizontally adjacent math fragments sharing a baseline.

    Groups blocks that:
    1. Are identified as math-like content
    2. Share a horizontal baseline within ±MATH_BASELINE_TOLERANCE_PT
    3. Are horizontally adjacent (sorted left-to-right)

    Merged blocks get their text concatenated with a space separator
    and their bbox expanded to cover all constituent fragments.

    Args:
        blocks: List of block dicts with 'bbox' and 'text' keys.

    Returns:
        New list with collinear math fragments merged.
    """
    if not blocks:
        return blocks

    result: List[dict] = []
    i = 0

    while i < len(blocks):
        blk = blocks[i]
        text = blk.get("text", "").strip()

        if not _is_math_like(text):
            result.append(blk)
            i += 1
            continue

        # Start a merge group with this math-like block
        group = [blk]
        baseline = blk["bbox"][3]  # y1 = bottom of bbox as baseline proxy

        j = i + 1
        while j < len(blocks):
            next_blk = blocks[j]
            next_text = next_blk.get("text", "").strip()
            next_baseline = next_blk["bbox"][3]

            # Must be math-like and share baseline
            if (
                _is_math_like(next_text)
                and abs(next_baseline - baseline) <= MATH_BASELINE_TOLERANCE_PT
            ):
                group.append(next_blk)
                j += 1
            else:
                break

        if len(group) == 1:
            result.append(blk)
        else:
            # Merge: combine text and expand bbox
            merged_text = " ".join(b.get("text", "").strip() for b in group)
            merged_bbox = (
                min(b["bbox"][0] for b in group),  # x0
                min(b["bbox"][1] for b in group),  # y0
                max(b["bbox"][2] for b in group),  # x1
                max(b["bbox"][3] for b in group),  # y1
            )
            merged_block = {
                "bbox": merged_bbox,
                "text": merged_text,
                "is_merged_math": True,
            }
            result.append(merged_block)

        i = j

    return result


# ─── PDF Extractor ─────────────────────────────────────────────────────

class PDFExtractor:
    """
    Advanced PDF extractor using geometric table isolation and math
    fragment merging, while fully preserving original text integrity.
    """

    def __init__(self, file_path: str, job_temp_dir: Optional[str] = None):
        self.file_path = file_path
        self._job_temp_dir = job_temp_dir or tempfile.mkdtemp(prefix="booklet_ai_pdf_images_")
        os.makedirs(self._job_temp_dir, exist_ok=True)

    def _extract_tables(
        self, page: fitz.Page
    ) -> Tuple[List[TableData], List[fitz.Rect]]:
        """Extracts tables and computes their bounding boxes for exclusion from text blocks."""
        tables_data: List[TableData] = []
        table_rects: List[fitz.Rect] = []
        try:
            tabs = page.find_tables()
            for idx, tab in enumerate(tabs.tables):
                t_rect = fitz.Rect(tab.bbox)
                table_rects.append(t_rect)
                extracted = tab.extract()
                if not extracted:
                    continue
                headers = [
                    str(cell).strip() if cell is not None else ""
                    for cell in extracted[0]
                ]
                rows = (
                    [
                        [
                            str(cell).strip() if cell is not None else ""
                            for cell in row
                        ]
                        for row in extracted[1:]
                    ]
                    if len(extracted) > 1
                    else []
                )

                tables_data.append(
                    TableData(
                        table_id=f"p{page.number}_t{idx + 1}",
                        headers=headers,
                        rows=rows,
                        source_bbox=[t_rect.x0, t_rect.y0, t_rect.x1, t_rect.y1],
                    )
                )
        except Exception as exc:
            raise PDFExtractionError(
                f"Table extraction error on page {page.number + 1}: {exc}"
            ) from exc
        return tables_data, table_rects

    def _is_inside_tables(
        self, bbox: fitz.Rect, table_rects: List[fitz.Rect]
    ) -> bool:
        """Checks whether a text block lies within any table's bounds,
        based on geometric overlap ratio (>= TABLE_OVERLAP_THRESHOLD)."""
        if not bbox.is_valid or bbox.is_empty:
            return False

        bbox_area = bbox.get_area()
        if bbox_area <= 0:
            return False

        for t_rect in table_rects:
            if not t_rect.is_valid or t_rect.is_empty:
                continue

            intersection = bbox & t_rect
            if not intersection.is_empty and intersection.is_valid:
                overlap_ratio = intersection.get_area() / bbox_area
                if overlap_ratio >= TABLE_OVERLAP_THRESHOLD:
                    return True

        return False

    def _is_in_header_footer(
        self, bbox: Tuple[float, float, float, float], page_height: float
    ) -> bool:
        """Check if a block is within header or footer margins.

        Uses absolute page-coordinate margins, not arbitrary percentage cuts.
        """
        y0 = bbox[1]
        y1 = bbox[3]

        # Header zone: top of page to HEADER_MARGIN_PT
        if y1 <= HEADER_MARGIN_PT:
            return True

        # Footer zone: page_height - FOOTER_MARGIN_PT to bottom
        if y0 >= page_height - FOOTER_MARGIN_PT:
            return True

        return False

    def _detect_block_type(self, text: str, is_merged_math: bool = False) -> str:
        """Determine the block type based on content heuristics."""
        if is_merged_math:
            return "math_formula"

        if _is_math_like(text):
            return "math_formula"

        stripped = text.strip()
        if not stripped:
            return "arabic_text"

        # Simple heuristic: if mostly ASCII, it's English
        ascii_count = sum(1 for c in stripped if ord(c) < 128)
        if len(stripped) > 0 and ascii_count / len(stripped) > 0.7:
            return "english_text"

        return "arabic_text"

    def _extract_header_footer_text(
        self, page: fitz.Page
    ) -> Tuple[str, str]:
        """Extract header and footer text from page margins."""
        page_height = page.rect.height
        header_texts: List[str] = []
        footer_texts: List[str] = []

        blocks = page.get_text("dict", flags=fitz.TEXT_PRESERVE_WHITESPACE)["blocks"]
        for block in blocks:
            if block.get("type") != 0:  # text blocks only
                continue
            bbox = block.get("bbox", (0, 0, 0, 0))
            text = ""
            for line in block.get("lines", []):
                for span in line.get("spans", []):
                    text += span.get("text", "")
            text = text.strip()
            if not text:
                continue

            if bbox[3] <= HEADER_MARGIN_PT:
                header_texts.append(text)
            elif bbox[1] >= page_height - FOOTER_MARGIN_PT:
                footer_texts.append(text)

        return " ".join(header_texts), " ".join(footer_texts)

    def extract(self) -> BookletDocument:
        """Extract all content from the PDF file.

        Returns:
            BookletDocument with pages, content blocks, and tables.

        Raises:
            PDFExtractionError: If extraction fails.
        """
        try:
            doc = fitz.open(self.file_path)
        except Exception as exc:
            raise PDFExtractionError(
                f"Failed to open PDF file '{self.file_path}': {exc}"
            ) from exc

        doc_id = str(uuid.uuid4())
        pages: List[PageData] = []

        try:
            for page_num in range(len(doc)):
                page = doc[page_num]
                page_width = page.rect.width
                page_height = page.rect.height

                # 1. Extract tables first (for deduplication)
                tables_data, table_rects = self._extract_tables(page)

                # 2. Extract header/footer text
                header_text, footer_text = self._extract_header_footer_text(page)

                # 3. Extract text blocks with geometric info
                raw_blocks = page.get_text(
                    "dict", flags=fitz.TEXT_PRESERVE_WHITESPACE
                )["blocks"]

                # Convert to our block format
                text_blocks: List[dict] = []
                for block in raw_blocks:
                    if block.get("type") != 0:  # text blocks only
                        continue

                    bbox = block.get("bbox", (0, 0, 0, 0))

                    # Skip header/footer zones
                    if self._is_in_header_footer(bbox, page_height):
                        continue

                    # Skip blocks inside tables (geometric deduplication)
                    block_rect = fitz.Rect(bbox)
                    if self._is_inside_tables(block_rect, table_rects):
                        continue

                    # Extract text from spans
                    text = ""
                    for line in block.get("lines", []):
                        for span in line.get("spans", []):
                            text += span.get("text", "")
                    text = text.strip()

                    if not text:
                        continue

                    text_blocks.append({
                        "bbox": bbox,
                        "text": text,
                        "is_merged_math": False,
                    })

                # 4. Apply spatial reading order correction
                text_blocks = sort_blocks_reading_order(text_blocks, page_width)

                # 5. Merge collinear math fragments
                text_blocks = merge_collinear_math_fragments(text_blocks)

                # 6. Convert to ContentBlock models
                content_blocks: List[ContentBlock] = []
                for seq_idx, blk in enumerate(text_blocks):
                    raw_text = blk["text"]
                    content_hash = compute_canonical_hash(raw_text)
                    block_type = self._detect_block_type(
                        raw_text,
                        is_merged_math=blk.get("is_merged_math", False),
                    )

                    cb = ContentBlock(
                        block_id=f"p{page_num}_b{seq_idx}",
                        block_type=block_type,
                        raw_content=raw_text,
                        content_hash=content_hash,
                        source_position={
                            "page": page_num + 1,
                            "bbox": list(blk["bbox"]),
                            "sequence": seq_idx,
                        },
                    )

                    # Verify integrity immediately upon extraction (AC-1.3.3)
                    verify_text_integrity(
                        cb.raw_content,
                        cb.content_hash,
                        context=f"PDF extraction block {cb.block_id}",
                    )

                    content_blocks.append(cb)

                # 7. Extract embedded raster images (STEP-6.4 / AC-6.4.2d)
                image_blocks: List[ContentBlock] = []
                page_images = page.get_images(full=True)
                for img_idx, img_info in enumerate(page_images):
                    xref = img_info[0]
                    try:
                        img_dict = doc.extract_image(xref)
                    except Exception as exc:
                        logger.debug("Could not extract image xref %d: %s", xref, exc)
                        continue

                    if not img_dict or "image" not in img_dict:
                        continue

                    ext = img_dict.get("ext", "png")
                    pixel_width = img_dict.get("width", 0)
                    pixel_height = img_dict.get("height", 0)
                    img_bytes = img_dict["image"]

                    temp_filename = f"pdf_img_p{page_num}_{img_idx}_{xref}.{ext}"
                    temp_path = os.path.join(self._job_temp_dir, temp_filename)
                    with open(temp_path, "wb") as f:
                        f.write(img_bytes)

                    # Fallback to Pillow for pixel dimensions if missing
                    if pixel_width <= 0 or pixel_height <= 0:
                        try:
                            from PIL import Image
                            with Image.open(temp_path) as im:
                                pixel_width, pixel_height = im.size
                        except Exception:
                            pass

                    # Resolve placement rect on the page if PyMuPDF can resolve it
                    intended_width_mm = None
                    intended_height_mm = None
                    bbox_list = None
                    try:
                        rects = page.get_image_rects(xref)
                        if rects and len(rects) > 0:
                            r = rects[0]
                            if r.is_valid and not r.is_empty:
                                # 1 pt = 25.4 / 72.0 mm
                                intended_width_mm = r.width * (25.4 / 72.0)
                                intended_height_mm = r.height * (25.4 / 72.0)
                                bbox_list = [r.x0, r.y0, r.x1, r.y1]
                    except Exception as exc:
                        logger.debug("Could not resolve placement rect for xref %d: %s", xref, exc)

                    asset = ImageAsset(
                        source_path=os.path.abspath(temp_path),
                        pixel_width=pixel_width,
                        pixel_height=pixel_height,
                        intended_width_mm=intended_width_mm,
                        intended_height_mm=intended_height_mm,
                    )

                    raw_content = f"[image:{temp_filename}]"
                    content_hash = compute_canonical_hash(raw_content)
                    img_cb = ContentBlock(
                        block_id=f"p{page_num}_img{img_idx}",
                        block_type="image_prompt",
                        raw_content=raw_content,
                        content_hash=content_hash,
                        source_position={
                            "page": page_num + 1,
                            "image_asset": asset,
                            "bbox": bbox_list,
                            "type": "raster_image",
                            "xref": xref,
                        },
                    )
                    verify_text_integrity(
                        img_cb.raw_content,
                        img_cb.content_hash,
                        context=f"PDF extraction image {img_cb.block_id}",
                    )
                    image_blocks.append(img_cb)

                # Combine text content blocks and image blocks in document order
                all_page_blocks = content_blocks + image_blocks
                all_page_blocks.sort(
                    key=lambda b: (
                        b.source_position.get("bbox")[1]
                        if b.source_position.get("bbox") is not None
                        else 999999.0
                    )
                )

                pages.append(
                    PageData(
                        page_number=page_num + 1,
                        width=page_width,
                        height=page_height,
                        header_text=header_text,
                        footer_text=footer_text,
                        content_blocks=all_page_blocks,
                        tables=tables_data,
                    )
                )
        except PDFExtractionError:
            raise
        except Exception as exc:
            raise PDFExtractionError(
                f"Unexpected error during PDF extraction: {exc}"
            ) from exc
        finally:
            doc.close()

        return BookletDocument(
            doc_id=doc_id,
            file_path=self.file_path,
            file_type="pdf",
            pages=pages,
        )
