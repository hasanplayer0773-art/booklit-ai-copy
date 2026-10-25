<!-- من الخطة الرئيسية Booklet_AI Desktop Master Plan -->

### [STEP-1.3] PDF Extraction Engine — Geometry, Table Deduplication, Collinear Math

#### 1. Components & Tooling

- **PyMuPDF (`fitz`):** Extracts text with microscopic geometric coordinates (spans, bbox, origin, font_size).
- **`fitz.Rect` & Intersection Logic:** Detects and defines table bounding boxes, stripping any text block that falls within them to prevent duplication.
- **Collinear Math Merger:** Groups horizontally adjacent math fragments sharing a baseline into a single unified `math_formula` block.

#### 2. Targeted Geometric Problems

- **Table Overlap Deduplication:** Tables are extracted as distinct entities first; any raw text block whose geometric position intersects a table area by **≥ 50%** is excluded from `content_blocks`.
- **Safe Margin Clipping (Header/Footer):** Headers/footers excluded via absolute page-coordinate margins, not arbitrary percentage cuts that risk clipping body content.
- **Logical Arabic Text Encoding:** Reads `ToUnicode` CMaps without applying distortive reversal functions; isolates Arabic vs. Latin text blocks correctly.
- **Multi-Column Spatial Reading Order:** PyMuPDF's default block extraction order reflects the PDF's internal content-stream order, which frequently does **not** match visual reading order on multi-column pages — the first line of the right column and the first line of the left column can be extracted adjacently, splicing unrelated content (e.g. a question from one column merging with an answer from the other) into a single logical sequence. This is corrected with an explicit spatial sort pass, run **before** blocks are assigned to `ContentBlock` sequence. **A single x-coordinate clustering pass is insufficient on its own**: a full-width chapter title or ministry-question banner spanning both columns has a bounding box that overlaps both column x-ranges, so naive x-only clustering would merge it into whichever column happens to overlap more — scrambling the sequence in exactly the pages where structure matters most (math/physics booklets frequently open a page with a full-width heading over two columns of worked explanation). The corrected approach decomposes the page into horizontal bands first (a block occupying ≥80% of page width is its own single-item band), and only applies right-to-left column clustering within the bands that are genuinely multi-column.

#### 3. Declarative Definitions

```python
# app/core/pdf_extractor.py
import fitz
from typing import List, Tuple
from app.core.models import PageData, ContentBlock, TableData
from app.core.text_integrity import compute_canonical_hash
from app.core.exceptions import BookletError

class PDFExtractionError(BookletError):
    """Raised when PDF structure extraction fails."""

# Geometric overlap threshold for excluding text blocks that belong to tables
TABLE_OVERLAP_THRESHOLD: float = 0.50

# Minimum horizontal gap (points) between column clusters to be treated as
# separate columns rather than justified spacing within one column
COLUMN_GAP_THRESHOLD_PT: float = 20.0

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

    FULL_WIDTH_RATIO_THRESHOLD = 0.80  # bbox width >= 80% of page width => full-width band

    # 1. Sort all blocks top-to-bottom first, to walk the page in Y order
    by_y = sorted(blocks, key=lambda b: b["bbox"][1])

    # 2. Decompose into bands: a full-width block is its own band;
    #    consecutive non-full-width blocks with overlapping Y ranges are
    #    grouped into one multi-column band.
    bands: List[List[dict]] = []
    current_band: List[dict] = []

    def block_is_full_width(blk: dict) -> bool:
        width = blk["bbox"][2] - blk["bbox"][0]
        return (width / page_width) >= FULL_WIDTH_RATIO_THRESHOLD

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

        clusters.sort(key=lambda cluster: min(b["bbox"][0] for b in cluster), reverse=True)

        for cluster in clusters:
            cluster.sort(key=lambda b: b["bbox"][1])
            ordered.extend(cluster)

    return ordered

class PDFExtractor:
    """
    Advanced PDF extractor using geometric table isolation and math
    fragment merging, while fully preserving original text integrity.
    """
    def __init__(self, file_path: str):
        self.file_path = file_path

    def _extract_tables(self, page: fitz.Page) -> Tuple[List[TableData], List[fitz.Rect]]:
        """Extracts tables and computes their bounding boxes for exclusion from text blocks."""
        tables_data = []
        table_rects = []
        try:
            tabs = page.find_tables()
            for idx, tab in enumerate(tabs.tables):
                t_rect = fitz.Rect(tab.bbox)
                table_rects.append(t_rect)
                extracted = tab.extract()
                if not extracted:
                    continue
                headers = [str(cell).strip() if cell is not None else "" for cell in extracted[0]]
                rows = [
                    [str(cell).strip() if cell is not None else "" for cell in row]
                    for row in extracted[1:]
                ] if len(extracted) > 1 else []

                tables_data.append(TableData(
                    table_id=f"p{page.number}_t{idx+1}",
                    headers=headers,
                    rows=rows,
                    source_bbox=[t_rect.x0, t_rect.y0, t_rect.x1, t_rect.y1]
                ))
        except Exception as exc:
            raise PDFExtractionError(f"Table extraction error on page {page.number+1}: {exc}") from exc
        return tables_data, table_rects

    def _is_inside_tables(self, bbox: fitz.Rect, table_rects: List[fitz.Rect]) -> bool:
        """
        Checks whether a text block lies within any table's bounds,
        based on geometric overlap ratio (>= TABLE_OVERLAP_THRESHOLD).
        """
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
```

**Threshold rationale (0.50):** Text blocks adjacent to table edges may show minor incidental overlap (10–30%) due to margin bleed or extension lines — a lower threshold (e.g. 40%) risks dropping legitimate body text sitting just outside a table. Text genuinely belonging to table cells overlaps 85–100%. A 50% threshold cleanly separates the two cases.

#### 4. Acceptance Criteria

- **[AC-1.3.1]** No table cell content is duplicated as a standalone text block in `content_blocks` (0% duplication rate, using the `TABLE_OVERLAP_THRESHOLD = 0.50` constant consistently between spec and code).
- **[AC-1.3.2]** Math symbols/fragments sharing a horizontal baseline (within ±3pt tolerance) are merged into a single `math_formula` block.
- **[AC-1.3.3]** Every extracted text block carries a `content_hash` matching `compute_canonical_hash()` and passes integrity verification immediately upon extraction.
- **[AC-1.3.4]** On a synthetic two-column test page with distinguishable content per column, `sort_blocks_reading_order()` outputs the right column's blocks (top-to-bottom) before any left column block — verified against a fixed test fixture, not visual inspection alone.
- **[AC-1.3.5]** A single-column page produces the same block order with or without the spatial sort pass applied (i.e. the sort is a no-op on single-column content, not just "usually correct").
- **[AC-1.3.6]** On a synthetic page with a full-width heading (≥80% page width) above two columns, the heading is output first, followed by the right column top-to-bottom, followed by the left column top-to-bottom — verified against a fixed test fixture reproducing the math/physics full-width-header-over-two-columns layout.

---

---
