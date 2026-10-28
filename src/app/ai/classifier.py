"""
Booklet_AI Desktop — Rule-Based Classifier (STEP-1.4)

Deterministic offline classification engine for Iraqi curriculum booklets.
Acts as instant fallback (<10ms) when remote AI calls are unavailable
or fail. Also used as the primary classifier when no API key is configured.

PRECLASSIFIED_TYPES is the single source of truth for block types that
are assigned at extraction time and must NEVER be re-classified — not by
the rule engine, not by the LLM. All call sites reference this constant.
"""

import re
from typing import List

from app.core.models import BlockType, ContentBlock

# CROSS-STEP AMENDMENT: STEP-1.2 (AC-1.2.5) emits `legacy_equation_placeholder`
# blocks; STEP-7.6 adds `image_prompt`. Like math/chemistry, they are assigned
# at extraction time and must NEVER be re-classified by the rule engine nor sent
# to the LLM. This single constant is the source of truth — do not hardcode the
# tuple elsewhere.
PRECLASSIFIED_TYPES: tuple[str, ...] = (
    "math_formula",
    "chemistry_formula",
    "image_prompt",
    "legacy_equation_placeholder",
)


class RuleBasedClassifier:
    """Deterministic offline classification engine for Iraqi curriculum booklets.

    Acts as instant fallback when remote AI calls are unavailable or fail.
    Classifies blocks based on regex patterns matching common Iraqi-curriculum
    patterns (e.g. علل, س وزاري, ما الفائدة).

    Pre-classified types (math_formula, chemistry_formula,
    image_prompt, legacy_equation_placeholder) are strictly immutable — returned unchanged.
    """

    HEADER_PATTERN = re.compile(
        r"^(الفصل\s+|الوحدة\s+|المبحث\s+|الدرس\s+|المحاضرة\s+)"
    )
    QUESTION_PATTERN = re.compile(
        r"^(س\s*[\:\/]|س\s*وزاري|سؤال\s*[\:\/]|علل\s*[\:\/]|وضح\s*[\:\/]|قارن\s*[\:\/]|ما\s+المقصود|ما\s+فائدة|عدد\s*[\:\/])"
    )
    LIST_PATTERN = re.compile(
        r"^([\-•●▪·]|(?:\d+|[أ-يa-zA-Z])[\.\)])\s+"
    )

    @classmethod
    def classify_block(cls, block: ContentBlock) -> BlockType:
        """Classify a single content block deterministically.

        Pre-classified types from extraction stage are strictly immutable
        and returned as-is.

        Args:
            block: The content block to classify.

        Returns:
            The determined BlockType for this block.
        """
        # Pre-classified types from extraction stage are strictly immutable
        if block.block_type in PRECLASSIFIED_TYPES:
            return block.block_type

        text = block.raw_content.strip()

        if cls.HEADER_PATTERN.match(text) and len(text) <= 90:
            return "chapter_header"

        if cls.QUESTION_PATTERN.match(text):
            return "ministry_question"

        if cls.LIST_PATTERN.match(text):
            return "list_item"

        if re.search(r"[\u0600-\u06FF]", text):
            return "arabic_text"

        if re.search(r"[a-zA-Z]", text) and not re.search(r"[\u0600-\u06FF]", text):
            return "english_text"

        return "arabic_text"

    @classmethod
    def classify_document_blocks(cls, blocks: List[ContentBlock]) -> List[ContentBlock]:
        """Classify all unassigned blocks deterministically.

        Iterates over all blocks, assigning types via classify_block().
        Pre-classified blocks are returned unchanged.

        Args:
            blocks: List of content blocks to classify.

        Returns:
            New list of ContentBlock instances with updated block_type fields.
        """
        updated_blocks = []
        for blk in blocks:
            assigned_type = cls.classify_block(blk)
            updated_blocks.append(
                blk.model_copy(update={"block_type": assigned_type})
            )
        return updated_blocks
