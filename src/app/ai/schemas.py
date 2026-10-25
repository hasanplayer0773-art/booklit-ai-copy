"""
Booklet_AI Desktop — AI Classification Schemas (STEP-1.4)

Pydantic v2 models for the structured JSON response expected from
the OpenRouter LLM classification endpoint.

Both models are frozen (immutable) per project convention.
BlockType is reused from app.core.models to guarantee only valid
taxonomy values pass schema validation.
"""

from typing import List

from pydantic import BaseModel, ConfigDict

from app.core.models import BlockType


class BlockClassification(BaseModel):
    """A single block's classification result from the LLM.

    Attributes:
        block_id: The block identifier matching the request payload.
        type: The semantic block type assigned by the classifier.
    """

    model_config = ConfigDict(frozen=True)

    block_id: str
    type: BlockType


class ClassificationResponse(BaseModel):
    """Top-level response wrapper for LLM classification results.

    Attributes:
        classifications: List of per-block classification results.
    """

    model_config = ConfigDict(frozen=True)

    classifications: List[BlockClassification]
