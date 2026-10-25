"""
Booklet_AI Desktop — Text Integrity Protocol

Implements canonical hashing and verification for Arabic text content.
Ensures Zero-Mutation guarantee: any unauthorized change to extracted
text is detected immediately via SHA-256 hash comparison.

Pipeline: normalize_presentation_forms (NFKC) → NFC → strip invisible
          control chars → normalize whitespace → SHA-256.

Key design decisions:
- NFKC first: converts Arabic Presentation Forms (U+FB50–U+FDFF,
  U+FE70–U+FEFF) to logical base Arabic characters (U+0600–U+06FF).
- NFC second: merges combining sequences to composed form.
- Protected diacritics: tashkeel, tanween, hamzas, superscript alef
  are NEVER stripped — only invisible control/formatting chars are removed.
- Line-break normalization: \\r\\n and \\r → \\n for cross-platform stability.
"""

import hashlib
import re
import unicodedata

from app.core.exceptions import TextIntegrityError

# ─── Protected ranges: these characters are NEVER stripped ─────────────
# \u064B-\u0652: Fathatan, Dammatan, Kasratan, Fatha, Damma, Kasra, Shadda, Sukun
# \u0670: Superscript Alef (الألف الخنجرية)
# \u0653-\u0655: Maddah, Hamza Above, Hamza Below
PROTECTED_DIACRITICS_REGEX = re.compile(r"[\u064B-\u0652\u0670\u0653-\u0655]")

# ─── Invisible artifacts allowed to be stripped ────────────────────────
# \u200E: Left-to-Right Mark
# \u200F: Right-to-Left Mark
# \u202A-\u202E: Bidi embedding/override controls
# \uFEFF: Byte Order Mark (BOM)
# \u200B: Zero-Width Space
STRIP_CONTROL_CHARS_REGEX = re.compile(
    r"[\u200E\u200F\u202A-\u202E\uFEFF\u200B]"
)


def normalize_presentation_forms(text: str) -> str:
    """Convert Arabic Presentation Forms glyphs to logical base Arabic.

    Critical PDF-extraction safeguard: some PDFs store text using Arabic
    Presentation Forms glyphs (U+FB50–U+FDFF, U+FE70–U+FEFF) — visually
    shaped, sometimes visually-reversed glyph codepoints — instead of
    logical-order base Arabic (U+0600–U+06FF).

    NFKC decomposition maps compatibility characters (which includes most
    Presentation Forms) to their canonical base form. This is why NFKC —
    not NFC — is required here specifically for this normalization pass,
    ahead of the existing NFC hashing.

    Note: NFKC fixes glyph-shape corruption but does NOT fix visual
    character-order reversal — that is a separate extraction-layer problem
    handled in STEP-1.3's PDF spatial-ordering fix.

    Args:
        text: Raw text possibly containing Presentation Forms glyphs.

    Returns:
        Text with Presentation Forms mapped to logical base characters.
    """
    return unicodedata.normalize("NFKC", text)


def compute_canonical_hash(text: str) -> str:
    """Compute a canonical SHA-256 hash of Arabic text content.

    Processing pipeline:
    1. NFKC normalization (Presentation Forms → base characters)
    2. NFC normalization (combining sequences → composed form)
    3. Strip invisible control characters (BOM, ZWS, bidi marks)
    4. Normalize line breaks (\\r\\n, \\r → \\n)
    5. Strip leading/trailing whitespace per line
    6. Collapse horizontal whitespace (tabs, multiple spaces → single space)
    7. SHA-256 of UTF-8 encoded result

    Diacritics (tashkeel, tanween, hamzas, maddah) are NEVER stripped.
    A fully-diacritized text produces an identical hash regardless of
    whether it is input as NFD or NFC (AC-1.1.1).

    Args:
        text: Raw text content to hash.

    Returns:
        Lowercase hex SHA-256 digest string.
    """
    # Step 1: Presentation Forms → base characters
    depresented = normalize_presentation_forms(text)

    # Step 2: NFC normalization
    normalized = unicodedata.normalize("NFC", depresented)

    # Step 3: Strip invisible control characters only
    cleaned = STRIP_CONTROL_CHARS_REGEX.sub("", normalized)

    # Step 4: Normalize line breaks
    cleaned = cleaned.replace("\r\n", "\n").replace("\r", "\n")

    # Step 5: Strip leading/trailing whitespace per line
    cleaned = "\n".join(line.strip() for line in cleaned.split("\n"))

    # Step 6: Collapse horizontal whitespace
    cleaned = re.sub(r"[ \t]+", " ", cleaned).strip()

    # Step 7: SHA-256
    return hashlib.sha256(cleaned.encode("utf-8")).hexdigest()


def verify_text_integrity(
    text: str,
    expected_hash: str,
    *,
    context: str = "",
) -> None:
    """Verify that text content matches its expected hash.

    Implements the Fail-Fast policy: any mismatch raises
    TextIntegrityError immediately — no silent fallback,
    no partial continuation.

    Args:
        text: The text content to verify.
        expected_hash: The expected SHA-256 hex digest.
        context: Optional context string for error messages
                 (e.g. block_id, page number).

    Raises:
        TextIntegrityError: If the computed hash does not match
                            the expected hash.
    """
    actual_hash = compute_canonical_hash(text)
    if actual_hash != expected_hash:
        ctx = f" [{context}]" if context else ""
        raise TextIntegrityError(
            f"Text integrity check failed{ctx}: "
            f"expected hash {expected_hash[:16]}... "
            f"but computed {actual_hash[:16]}... — "
            f"unauthorized text mutation detected. "
            f"Generation halted (Fail-Fast policy)."
        )


def compute_document_content_hash(doc_or_blocks: object) -> str:
    """Compute canonical hash of non-page-number document content (STEP-6.3, STEP-6.8).

    Guarantees that page numbering style/layout choices have zero effect on
    ordinary source text content, question numbers, dates, formulas, etc. (AC-6.8.4).

    Args:
        doc_or_blocks: A BookletDocument, list of ContentBlock, or iterable of blocks.

    Returns:
        Canonical SHA-256 hex digest of the combined raw_content.
    """
    if hasattr(doc_or_blocks, "pages"):
        blocks = [b for page in getattr(doc_or_blocks, "pages") for b in getattr(page, "content_blocks", [])]
    elif isinstance(doc_or_blocks, list):
        blocks = doc_or_blocks
    else:
        blocks = list(doc_or_blocks)  # type: ignore[call-overload]

    combined_text = "".join(getattr(b, "raw_content", "") for b in blocks)
    return compute_canonical_hash(combined_text)
