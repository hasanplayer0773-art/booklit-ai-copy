"""
Booklet_AI Desktop — Data Models

Immutable (frozen) Pydantic v2 models shared across all layers:
extraction → AI classification → InDesign injection.

All models use ConfigDict(frozen=True) to enforce immutability.
The ONLY sanctioned path to mutate a ContentBlock's raw_content
is through with_user_edit() — this preserves the Zero-Mutation
guarantee while allowing deliberate user corrections.
"""

from datetime import datetime, timezone
from typing import Any, Dict, List, Literal, Optional

from pydantic import BaseModel, ConfigDict, Field


# ─── Image Asset Model (STEP-6.4 addition) ─────────────────────────────
# Captures pixel dimensions and intended placement size of an extracted
# image so that Preflight can compute effective DPI for print-quality
# assessment.  Populated during STEP-1.2/1.3 extraction.


class ImageAsset(BaseModel):
    """Metadata for an extracted image associated with an image_prompt block.

    Constructed during STEP-1.2 (DOCX) / STEP-1.3 (PDF) extraction and
    attached to ContentBlock.source_position["image_asset"].

    Attributes:
        source_path: Path to the extracted/referenced image file on disk.
        pixel_width: Image width in pixels (from the actual raster data).
        pixel_height: Image height in pixels (from the actual raster data).
        intended_width_mm: Intended physical placement width in mm, if
            known at extraction time (wp:extent in DOCX, placement Rect
            in PDF).  None if only resolvable during InDesign composition.
        intended_height_mm: Intended physical placement height in mm,
            with the same availability caveats as intended_width_mm.
    """

    model_config = ConfigDict(frozen=True)

    source_path: str
    pixel_width: int
    pixel_height: int
    intended_width_mm: Optional[float] = None
    intended_height_mm: Optional[float] = None

# ─── Block Type Taxonomy ───────────────────────────────────────────────
# Covers all content types in Iraqi academic booklets.
# "legacy_equation_placeholder" is for detected OLE/MathType equations
# that cannot be converted — see STEP-1.2.

BlockType = Literal[
    "chapter_header",
    "arabic_text",
    "english_text",
    "ministry_question",
    "math_formula",
    "chemistry_formula",
    "image_prompt",
    "list_item",
    "legacy_equation_placeholder",
]


class ContentBlock(BaseModel):
    """A single content block extracted from a source document.

    Immutable by default. Content can only be changed through
    with_user_edit(), which recomputes the hash and marks the
    block as user-edited — preserving the audit trail.

    Attributes:
        block_id: Unique identifier for this block within the document.
        block_type: Semantic type from BlockType taxonomy.
        raw_content: The original extracted text content.
        content_hash: SHA-256 hash of canonicalized raw_content.
        source_position: Extraction metadata (page, bbox, paragraph index).
        user_edited: True if this block was modified by a human user.
        edit_timestamp: ISO 8601 timestamp of the last user edit.
    """

    model_config = ConfigDict(frozen=True)

    block_id: str
    block_type: BlockType
    raw_content: str
    content_hash: str
    source_position: Dict[str, Any] = Field(default_factory=dict)
    user_edited: bool = False
    edit_timestamp: Optional[str] = None

    def with_user_edit(self, new_content: str) -> "ContentBlock":
        """The ONLY sanctioned path to change raw_content after extraction.

        Recomputes content_hash from new_content and marks user_edited=True,
        so the new hash is trusted going forward — it is not compared
        against the original extraction hash. TextIntegrityError is
        reserved for hash mismatches on blocks where user_edited is False;
        once user-edited, the block's new hash IS the new source of truth.

        This method is ONLY reachable from the UI's manual preview/edit
        screen — never called by the AI classification layer (STEP-1.4)
        or any extraction code. Those layers remain strictly read-only
        with respect to raw_content (AC-1.1.7).

        Args:
            new_content: The user-corrected text content.

        Returns:
            A new ContentBlock with updated content, hash, and audit fields.
        """
        from app.core.text_integrity import compute_canonical_hash

        return self.model_copy(
            update={
                "raw_content": new_content,
                "content_hash": compute_canonical_hash(new_content),
                "user_edited": True,
                "edit_timestamp": datetime.now(timezone.utc).isoformat(),
            }
        )


class TableData(BaseModel):
    """A table extracted from a source document.

    Attributes:
        table_id: Unique identifier for this table within the document.
        headers: Column header strings.
        rows: 2D list of cell content strings.
        source_bbox: Optional bounding box [x0, y0, x1, y1] from PDF extraction.
    """

    model_config = ConfigDict(frozen=True)

    table_id: str
    headers: List[str]
    rows: List[List[str]]
    source_bbox: Optional[List[float]] = None


class PageData(BaseModel):
    """Data for a single page of the source document.

    Attributes:
        page_number: 1-based page number in the source document.
        width: Page width in points.
        height: Page height in points.
        header_text: Running header text (chapter title, etc.).
        footer_text: Running footer text.
        content_blocks: Ordered list of content blocks on this page.
        tables: Tables detected on this page.
    """

    model_config = ConfigDict(frozen=True)

    page_number: int
    width: float
    height: float
    header_text: str = ""
    footer_text: str = ""
    content_blocks: List[ContentBlock] = Field(default_factory=list)
    tables: List[TableData] = Field(default_factory=list)


class BookletDocument(BaseModel):
    """Top-level document model representing a complete source file.

    Attributes:
        doc_id: Unique identifier for this document (UUID).
        file_path: Absolute path to the source file.
        file_type: Source file format ("pdf" or "docx").
        pages: Ordered list of page data.
    """

    model_config = ConfigDict(frozen=True)

    doc_id: str
    file_path: str
    file_type: Literal["pdf", "docx"]
    pages: List[PageData] = Field(default_factory=list)
    document_content_hash: Optional[str] = None
    source_sha256: Optional[str] = None
