"""
Shared pytest fixtures and markers for Booklet_AI Desktop tests.
"""

from __future__ import annotations

import pytest

from app.core.docx_extractor import _discover_xslt
from app.core.exceptions import MathExtractionError


def is_xslt_available(preferred_path: str = "assets/xslt/OMML2MML.XSL") -> bool:
    """Check if OMML2MML.XSL is discoverable using the extractor's exact lookup logic."""
    try:
        _discover_xslt(preferred_path)
        return True
    except MathExtractionError:
        return False


requires_xslt = pytest.mark.skipif(
    not is_xslt_available(),
    reason="OMML2MML.XSL not available (not redistributed in the repo)",
)
