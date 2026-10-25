"""
Booklet_AI Desktop — Document Store

Central heavy store for the full extracted content tree (blocks,
tables, pages). Protected by threading.RLock for thread-safe access.

Design principles (Two-Tier State Separation per STEP-1.1 §4):
- Widgets read on-demand slices via get_page_slice() — never a full
  deep copy on every update.
- Full Pydantic document objects are NEVER passed through Qt Signals.
- The lock is held only briefly for reads/writes — rendering and
  heavy processing must happen AFTER releasing the lock (AC-1.1.4).
"""

import threading
from typing import Dict, List, Optional

from app.core.exceptions import TextIntegrityError
from app.core.models import BookletDocument, ContentBlock, PageData
from app.core.text_integrity import compute_canonical_hash


class DocumentStore:
    """Thread-safe central store for extracted document content.

    Holds the full extracted content tree. All access is synchronized
    via an RLock to support recursive and multi-method locking.

    Usage pattern:
        store = DocumentStore()
        store.load_document(doc)
        page = store.get_page_slice(1)  # brief lock, returns copy
        # ... render page outside the lock ...
    """

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._documents: Dict[str, BookletDocument] = {}
        self._active_doc_id: Optional[str] = None
        self._revision: int = 0

    @property
    def lock(self) -> threading.RLock:
        """Expose lock for external synchronization (e.g. undo/redo)."""
        return self._lock

    @property
    def revision(self) -> int:
        """Return the current monotonically increasing document revision counter."""
        with self._lock:
            return self._revision

    @property
    def document_revision(self) -> int:
        """Alias for revision (AC-6.7.1c)."""
        with self._lock:
            return self._revision

    def increment_revision(self) -> int:
        """Increment the document revision counter under the lock."""
        with self._lock:
            self._revision += 1
            return self._revision

    def load_document(self, document: BookletDocument) -> None:
        """Load a document into the store and set it as active.

        Args:
            document: The BookletDocument to store.
        """
        with self._lock:
            self._documents[document.doc_id] = document
            self._active_doc_id = document.doc_id

    def get_document(self, doc_id: Optional[str] = None) -> Optional[BookletDocument]:
        """Retrieve a document by ID, or the active document if no ID given.

        Args:
            doc_id: Optional document ID. If None, returns the active document.

        Returns:
            The BookletDocument, or None if not found.
        """
        with self._lock:
            target_id = doc_id or self._active_doc_id
            if target_id is None:
                return None
            return self._documents.get(target_id)

    def get_page_slice(self, page_number: int, doc_id: Optional[str] = None) -> Optional[PageData]:
        """Retrieve a single page's data from the active document.

        The lock is held only long enough to extract the page reference.
        Callers should perform rendering AFTER this returns (AC-1.1.4).

        Args:
            page_number: 1-based page number.
            doc_id: Optional document ID. If None, uses the active document.

        Returns:
            The PageData for the requested page, or None if not found.
        """
        with self._lock:
            doc = self.get_document(doc_id)
            if doc is None:
                return None
            for page in doc.pages:
                if page.page_number == page_number:
                    return page
            return None

    def get_all_pages(self, doc_id: Optional[str] = None) -> List[PageData]:
        """Retrieve all pages from a document.

        Args:
            doc_id: Optional document ID. If None, uses the active document.

        Returns:
            List of PageData objects, or empty list if document not found.
        """
        with self._lock:
            doc = self.get_document(doc_id)
            if doc is None:
                return []
            return list(doc.pages)

    def update_block(
        self,
        doc_id: str,
        page_number: int,
        block_id: str,
        new_block: ContentBlock,
    ) -> None:
        """Replace a content block within a document page.

        Verifies text integrity of the new block before applying.
        If the block is not user_edited, the hash must match the
        raw_content — otherwise TextIntegrityError is raised (Fail-Fast).

        Args:
            doc_id: Document ID.
            page_number: 1-based page number containing the block.
            block_id: ID of the block to replace.
            new_block: The replacement ContentBlock.

        Raises:
            TextIntegrityError: If the new block's hash doesn't match
                                its content (and it's not user-edited).
            KeyError: If the document, page, or block is not found.
        """
        # Verify integrity of the new block (skip for user-edited blocks
        # since with_user_edit() already recomputed the hash)
        if not new_block.user_edited:
            actual_hash = compute_canonical_hash(new_block.raw_content)
            if actual_hash != new_block.content_hash:
                raise TextIntegrityError(
                    f"Block {block_id} integrity check failed during update: "
                    f"hash mismatch detected. Update rejected (Fail-Fast)."
                )

        with self._lock:
            doc = self._documents.get(doc_id)
            if doc is None:
                raise KeyError(f"Document {doc_id} not found in store.")

            # Find and replace the block in the target page
            new_pages: list = []
            found = False
            for page in doc.pages:
                if page.page_number == page_number:
                    new_blocks = []
                    for block in page.content_blocks:
                        if block.block_id == block_id:
                            new_blocks.append(new_block)
                            found = True
                        else:
                            new_blocks.append(block)
                    if not found:
                        raise KeyError(
                            f"Block {block_id} not found on page {page_number}."
                        )
                    new_pages.append(
                        page.model_copy(update={"content_blocks": new_blocks})
                    )
                else:
                    new_pages.append(page)

            if not found:
                raise KeyError(
                    f"Page {page_number} not found in document {doc_id}."
                )

            # Replace the document with updated pages
            self._documents[doc_id] = doc.model_copy(
                update={"pages": new_pages}
            )
            self._revision += 1

    def with_user_edit(
        self,
        doc_id: str,
        page_number: int,
        block_id: str,
        new_content: str,
    ) -> ContentBlock:
        """Apply user edit to a block in the document store and increment revision.

        Mutates DocumentStore under the lock, updating both the block and
        the document_revision counter (AC-6.7.1c).
        """
        with self._lock:
            doc = self._documents.get(doc_id)
            if doc is None:
                raise KeyError(f"Document {doc_id} not found in store.")
            for page in doc.pages:
                if page.page_number == page_number:
                    for b in page.content_blocks:
                        if b.block_id == block_id:
                            edited = b.with_user_edit(new_content)
                            self.update_block(doc_id, page_number, block_id, edited)
                            return edited
                    raise KeyError(f"Block {block_id} not found on page {page_number}.")
            raise KeyError(f"Page {page_number} not found in document {doc_id}.")

    def apply_classified_blocks(
        self,
        classified_blocks: List[ContentBlock],
        expected_revision: int,
        doc_id: Optional[str] = None,
    ) -> bool:
        """Apply classified blocks to the document store if document_revision has not advanced.

        Under the lock:
        - If self._revision != expected_revision: discards the result and returns False.
        - If matches: updates the blocks in the document and returns True (AC-6.7.1c).
        """
        with self._lock:
            if self._revision != expected_revision:
                return False

            target_id = doc_id or self._active_doc_id
            if target_id is None:
                return False
            doc = self._documents.get(target_id)
            if doc is None:
                return False

            classified_map = {b.block_id: b for b in classified_blocks}
            new_pages = []
            for page in doc.pages:
                new_blocks = []
                for b in page.content_blocks:
                    new_blocks.append(classified_map.get(b.block_id, b))
                new_pages.append(page.model_copy(update={"content_blocks": new_blocks}))

            self._documents[target_id] = doc.model_copy(update={"pages": new_pages})
            return True

    def remove_document(self, doc_id: str) -> None:
        """Remove a document from the store.

        Args:
            doc_id: Document ID to remove.
        """
        with self._lock:
            self._documents.pop(doc_id, None)
            if self._active_doc_id == doc_id:
                self._active_doc_id = None

    def set_active_document(self, doc_id: str) -> None:
        """Set the active document by ID.

        Args:
            doc_id: Document ID to activate.

        Raises:
            KeyError: If the document is not in the store.
        """
        with self._lock:
            if doc_id not in self._documents:
                raise KeyError(f"Document {doc_id} not found in store.")
            self._active_doc_id = doc_id

    @property
    def active_doc_id(self) -> Optional[str]:
        """Return the active document's ID, or None."""
        with self._lock:
            return self._active_doc_id

    @property
    def document_count(self) -> int:
        """Return the number of loaded documents."""
        with self._lock:
            return len(self._documents)
