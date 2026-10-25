"""
Booklet_AI Desktop — Document Reopen Recovery (STEP-6.3 §4)

Handles the document-reopen recovery sequence: when a document with
recorded edit history is reopened after the application restarts.

Recovery sequence:
1. Recompute source_file_sha256 (raw file bytes) and compare against
   the stored value.
2. If hash matches: replay block_edit_history in sequence_index order
   over a fresh extraction, using with_user_edit() for each.
3. If hash does NOT match: show translated warning, do NOT apply old
   edits automatically. The user can view old history for reference
   or start fresh.

Preconditions:
- The source file must be re-extracted (STEP-1.1–1.3) and re-classified
  (STEP-1.4) BEFORE calling replay_edit_history().
- Replay uses with_user_edit() exclusively — no other mutation path.
"""

import logging
from typing import TYPE_CHECKING, List, Optional, Tuple

from app.core.models import ContentBlock
from app.state.job_history import compute_source_file_sha256
from app.state.undo_redo import EditAction, UndoRedoManager

if TYPE_CHECKING:
    from app.state.document_store import DocumentStore

logger = logging.getLogger("booklet_ai.document_recovery")


class RecoveryResult:
    """Result of a document-reopen recovery attempt.

    Attributes:
        success: True if recovery completed without errors.
        replayed_count: Number of edits successfully replayed.
        skipped_count: Number of edits skipped (block not found, hash mismatch).
        hash_mismatch: True if the source file hash didn't match.
        warnings: List of warning messages for display.
    """

    __slots__ = (
        "success",
        "replayed_count",
        "skipped_count",
        "hash_mismatch",
        "warnings",
    )

    def __init__(self) -> None:
        self.success: bool = True
        self.replayed_count: int = 0
        self.skipped_count: int = 0
        self.hash_mismatch: bool = False
        self.warnings: List[str] = []


def check_source_file_integrity(
    file_path: str,
    stored_sha256: str,
) -> bool:
    """Check if the source file on disk matches the stored hash.

    Args:
        file_path: Absolute path to the source file.
        stored_sha256: The SHA-256 hash from the previous session.

    Returns:
        True if the file hash matches, False if the file has changed.
    """
    try:
        current_hash = compute_source_file_sha256(file_path)
        return current_hash == stored_sha256
    except (OSError, IOError) as exc:
        logger.error("Cannot read source file for hash check: %s", exc)
        return False


def replay_edit_history(
    document_session_id: str,
    doc_id: str,
    document_store: "DocumentStore",
    undo_manager: UndoRedoManager,
) -> RecoveryResult:
    """Replay recorded edit history onto a freshly-extracted document.

    Called when the source file hash MATCHES the stored hash — safe
    to replay because block_ids from the old extraction correspond to
    the same logical content in the new extraction.

    The replay:
    1. Gets the list of actions up to the cursor position.
    2. For each action, finds the corresponding block in DocumentStore.
    3. Applies with_user_edit() to each block.
    4. Updates DocumentStore with the edited block.

    All mutations go through with_user_edit() exclusively (AC-6.3.4).

    Args:
        document_session_id: Session to replay.
        doc_id: DocumentStore doc_id for the fresh extraction.
        document_store: DocumentStore with the fresh extraction loaded.
        undo_manager: UndoRedoManager with the session rebuilt from DB.

    Returns:
        RecoveryResult describing what happened.
    """
    result = RecoveryResult()
    actions = undo_manager.get_replay_actions(document_session_id)

    if not actions:
        logger.info("No edit history to replay for session %s", document_session_id)
        return result

    logger.info(
        "Replaying %d edits for session %s onto document %s",
        len(actions),
        document_session_id,
        doc_id,
    )

    for action in actions:
        try:
            _replay_single_edit(action, doc_id, document_store)
            result.replayed_count += 1
        except _ReplaySkipped as skip:
            result.skipped_count += 1
            result.warnings.append(str(skip))
            logger.warning(
                "Skipped replay of edit seq=%d block=%s: %s",
                action.sequence_index,
                action.block_id,
                skip,
            )

    if result.skipped_count > 0:
        result.success = False

    logger.info(
        "Replay complete for session %s: %d applied, %d skipped",
        document_session_id,
        result.replayed_count,
        result.skipped_count,
    )
    return result


class _ReplaySkipped(Exception):
    """Internal: raised when a single replay step must be skipped."""


def _replay_single_edit(
    action: EditAction,
    doc_id: str,
    document_store: "DocumentStore",
) -> None:
    """Replay a single edit action onto the document.

    Args:
        action: The EditAction to replay.
        doc_id: DocumentStore doc_id.
        document_store: The DocumentStore to mutate.

    Raises:
        _ReplaySkipped: If the block can't be found or hash doesn't match.
    """
    with document_store.lock:
        doc = document_store.get_document(doc_id)
        if doc is None:
            raise _ReplaySkipped(
                f"Document {doc_id} not found in store"
            )

        # Find the block and its page
        found_page: Optional[int] = None
        found_block: Optional[ContentBlock] = None
        for page in doc.pages:
            for block in page.content_blocks:
                if block.block_id == action.block_id:
                    found_block = block
                    found_page = page.page_number
                    break
            if found_block is not None:
                break

        if found_block is None or found_page is None:
            raise _ReplaySkipped(
                f"Block {action.block_id} not found in fresh extraction — "
                f"block may have been removed or renamed."
            )

        # Verify the block's current content matches what we expect
        # (the previous_content of the first edit should match the
        # original extraction, subsequent edits chain from there)
        if found_block.content_hash != action.previous_hash:
            raise _ReplaySkipped(
                f"Block {action.block_id} content hash mismatch during "
                f"replay: expected {action.previous_hash[:16]}... but "
                f"found {found_block.content_hash[:16]}... — the extraction "
                f"produced different content for this block."
            )

        # Apply the edit using the ONLY sanctioned mutation path
        edited_block = found_block.with_user_edit(action.new_content)

        # Update in DocumentStore (still under the lock)
        document_store.update_block(
            doc_id=doc_id,
            page_number=found_page,
            block_id=action.block_id,
            new_block=edited_block,
        )


def get_file_changed_warning() -> str:
    """Get the translated warning message for when the source file
    has changed on disk since the recorded session.

    This warning is shown to the user per AC-6.3.7 — old edit history
    is NEVER silently applied when the source file has changed.

    Returns:
        Arabic warning message string.
    """
    return (
        "⚠️ ملف المصدر تغيّر منذ آخر جلسة تحرير — "
        "لا يمكن تطبيق التعديلات السابقة تلقائياً لأن هوية الكتل النصية "
        "في الاستخراج القديم قد لا تتوافق مع الاستخراج الجديد.\n\n"
        "يمكنك عرض سجل التعديلات السابقة كمرجع يدوي، أو البدء من جديد."
    )
