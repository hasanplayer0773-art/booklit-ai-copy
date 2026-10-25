"""
Booklet_AI Desktop — Style Dispatch Table (STEP-2.3)

Maps every BlockType to exactly one InDesign paragraph style name.
This mapping is data, not logic — a plain dict keyed by BlockType,
resolved once per block at injection time. Adding a new block_type
later means adding one map entry, not new branching code.

Fail-Fast policy: if resolve_style_name() receives a block_type
with no entry in BLOCK_STYLE_MAP, it raises StyleMappingError
immediately — an unmapped block silently defaulting to body text
is a layout defect that must surface at once (AC-2.3.2).
"""

from typing import FrozenSet

from app.core.exceptions import StyleMappingError
from app.core.models import BlockType

# ─── Style Dispatch Table ──────────────────────────────────────────────
# Single source of truth linking each ContentBlock.block_type to its
# InDesign paragraph style name. Every entry must correspond to a
# paragraph style that exists in the base .idml template — verified
# once at document-load time by validate_template_styles() (AC-2.3.4).

BLOCK_STYLE_MAP: dict[BlockType, str] = {
    "chapter_header": "ChapterHeaderStyle",
    "arabic_text": "BodyArabicStyle",
    "english_text": "BodyLatinStyle",
    "ministry_question": "MinistryQuestionStyle",
    "math_formula": "MathFormulaStyle",
    "chemistry_formula": "ChemistryFormulaStyle",
    "image_prompt": "ImagePlaceholderStyle",
    "list_item": "ListItemStyle",
    # CROSS-STEP AMENDMENT: STEP-1.2 emits this type; without an entry
    # it would hit StyleMappingError. The style is deliberately visually
    # distinct (tinted/boxed) so the designer cannot miss the gap.
    "legacy_equation_placeholder": "LegacyEquationPlaceholderStyle",
}


def resolve_style_name(block_type: BlockType) -> str:
    """Resolve a block_type to its InDesign paragraph style name.

    This is the ONLY function that should be used to look up paragraph
    styles — callers must not access BLOCK_STYLE_MAP directly to allow
    centralized error handling.

    Args:
        block_type: The semantic block type from BlockType taxonomy.

    Returns:
        The InDesign paragraph style name corresponding to block_type.

    Raises:
        StyleMappingError: If block_type has no entry in BLOCK_STYLE_MAP.
            This is Fail-Fast — no silent fallback to a default style.
    """
    if block_type not in BLOCK_STYLE_MAP:
        raise StyleMappingError(
            f"No paragraph style mapped for block_type: '{block_type}'. "
            f"Every block_type must have an explicit entry in "
            f"BLOCK_STYLE_MAP — add a mapping, do not use a fallback."
        )
    return BLOCK_STYLE_MAP[block_type]


def get_all_required_style_names() -> FrozenSet[str]:
    """Return the complete set of paragraph style names required in
    the InDesign template.

    Used by validate_template_styles() to perform a one-time
    precondition check at document-load time (AC-2.3.4).

    Returns:
        A frozen set of all style name strings from BLOCK_STYLE_MAP.
    """
    return frozenset(BLOCK_STYLE_MAP.values())
