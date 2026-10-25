"""
Booklet_AI Desktop — Undo/Redo Manager (STEP-6.3)

Cursor-based undo/redo with branch-truncation for ContentBlock edits.

Architecture:
- An in-memory bounded list of pending edit actions sits in front of the
  SQLite table for interactive Ctrl+Z/Ctrl+Y responsiveness.
- The SQLite table remains the durable record, written when an edit is
  finalized or when the document is saved/closed.
- The in-memory stack is discarded and rebuilt from SQLite when a document
  is reopened (see rebuild_from_db).

Lock discipline (STEP-6.3 §2, corrected):
- undo() and redo() acquire DocumentStore's threading.RLock for the full
  duration of the operation: validate → mutate DocumentStore → update cursor.
  This is the SAME lock with_user_edit()'s caller already requires.
- record_edit() does NOT need the DocumentStore lock — it is called
  AFTER a successful with_user_edit() has already released the lock, and
  only writes to the undo/redo data structures (in-memory + SQLite).

Branch-truncation rule:
  If the cursor is at position K (K < max sequence_index) and a NEW edit
  is made, all rows with sequence_index > K are deleted before the new
  edit is inserted at K+1. This is the standard linear-undo-history rule.
"""

import logging
import sqlite3
import threading
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Dict, List, Optional

from app.core.text_integrity import compute_canonical_hash
from app.ui.error_translation import translate_error

if TYPE_CHECKING:
    from app.state.document_store import DocumentStore

logger = logging.getLogger("booklet_ai.undo_redo")

# Maximum number of undo steps kept in memory per session.
_MAX_IN_MEMORY_STACK_SIZE = 200


@dataclass
class EditAction:
    """A single recorded edit action (in-memory representation).

    Attributes:
        sequence_index: Monotonically increasing position within the session.
        block_id: ID of the edited ContentBlock.
        page_number: Page number containing the block.
        previous_content: Content before the edit.
        previous_hash: Hash before the edit.
        new_content: Content after the edit.
        new_hash: Hash after the edit.
        edited_at: ISO 8601 timestamp of when the edit occurred.
    """

    sequence_index: int
    block_id: str
    page_number: int
    previous_content: str
    previous_hash: str
    new_content: str
    new_hash: str
    edited_at: str


@dataclass
class UndoRedoSession:
    """In-memory undo/redo state for one open document session.

    Attributes:
        document_session_id: Unique session identifier.
        doc_id: DocumentStore doc_id for this session.
        actions: Ordered list of edit actions.
        cursor_position: Current undo/redo cursor (0 = no edits applied).
    """

    document_session_id: str
    doc_id: str
    actions: List[EditAction] = field(default_factory=list)
    cursor_position: int = 0


class UndoRedoError(Exception):
    """Raised when an undo/redo operation cannot be completed safely."""


class UndoRedoManager:
    """Manages undo/redo history for all open document sessions.

    Thread safety:
    - Each undo/redo operation on DocumentStore acquires DocumentStore's
      RLock for the full duration (validate + mutate + cursor update).
    - Internal _sessions dict is protected by its own lock for concurrent
      session creation/lookup, separate from DocumentStore's lock.
    - SQLite writes use a dedicated connection with its own serialization.
    """

    def __init__(self, db_conn: sqlite3.Connection) -> None:
        """Initialize the UndoRedoManager.

        Args:
            db_conn: Open sqlite3.Connection to the STEP-3.1 database
                     (question_bank.db) with the 002_job_history_and_undo
                     migration already applied.
        """
        self._db_conn = db_conn
        self._sessions: Dict[str, UndoRedoSession] = {}
        self._sessions_lock = threading.Lock()

    # ═══════════════════════════════════════════════════════════════════
    # Session lifecycle
    # ═══════════════════════════════════════════════════════════════════

    def get_or_create_session(
        self, document_session_id: str, doc_id: str
    ) -> UndoRedoSession:
        """Get an existing session or create a new one.

        Args:
            document_session_id: Unique session identifier.
            doc_id: DocumentStore doc_id for this session.

        Returns:
            The UndoRedoSession for this document_session_id.
        """
        with self._sessions_lock:
            if document_session_id not in self._sessions:
                session = UndoRedoSession(
                    document_session_id=document_session_id,
                    doc_id=doc_id,
                )
                self._sessions[document_session_id] = session
                # Initialize cursor in SQLite if not present
                self._db_conn.execute(
                    "INSERT OR IGNORE INTO edit_cursor "
                    "(document_session_id, current_sequence_index) VALUES (?, 0)",
                    (document_session_id,),
                )
                self._db_conn.commit()
            return self._sessions[document_session_id]

    def discard_session(self, document_session_id: str) -> None:
        """Discard the in-memory session (e.g., on document close).

        The SQLite data is preserved for future reopening via
        rebuild_from_db().

        Args:
            document_session_id: Session to discard from memory.
        """
        with self._sessions_lock:
            self._sessions.pop(document_session_id, None)

    # ═══════════════════════════════════════════════════════════════════
    # Record Edit
    # ═══════════════════════════════════════════════════════════════════

    def record_edit(
        self,
        document_session_id: str,
        doc_id: str,
        block_id: str,
        page_number: int,
        previous_content: str,
        previous_hash: str,
        new_content: str,
        new_hash: str,
    ) -> None:
        """Record a user edit after a successful with_user_edit() call.

        Called by ContentBlock.with_user_edit()'s caller immediately after
        a successful edit. Applies the branch-truncation rule before
        inserting the new row, then advances edit_cursor.

        This does NOT acquire DocumentStore's lock — it only writes to
        the undo/redo data structures.

        Args:
            document_session_id: Current session identifier.
            doc_id: DocumentStore doc_id.
            block_id: ID of the edited ContentBlock.
            page_number: Page number containing the block.
            previous_content: Content before the edit.
            previous_hash: Hash before the edit.
            new_content: Content after the edit.
            new_hash: Hash after the edit.
        """
        session = self.get_or_create_session(document_session_id, doc_id)
        now_iso = datetime.now(timezone.utc).isoformat()

        # Branch-truncation: if cursor < max sequence_index, truncate
        # all actions after cursor position (the standard linear-undo rule)
        if session.cursor_position < len(session.actions):
            # Remove in-memory actions after cursor
            truncated = session.actions[session.cursor_position:]
            session.actions = session.actions[:session.cursor_position]
            logger.debug(
                "Branch truncation: removed %d actions after cursor %d in session %s",
                len(truncated),
                session.cursor_position,
                document_session_id,
            )

        new_seq = session.cursor_position + 1

        action = EditAction(
            sequence_index=new_seq,
            block_id=block_id,
            page_number=page_number,
            previous_content=previous_content,
            previous_hash=previous_hash,
            new_content=new_content,
            new_hash=new_hash,
            edited_at=now_iso,
        )

        session.actions.append(action)
        session.cursor_position = new_seq

        # Enforce bounded in-memory stack
        if len(session.actions) > _MAX_IN_MEMORY_STACK_SIZE:
            session.actions = session.actions[-_MAX_IN_MEMORY_STACK_SIZE:]

        # Persist to SQLite
        self._persist_edit(document_session_id, action, new_seq)

    def _persist_edit(
        self,
        document_session_id: str,
        action: EditAction,
        new_cursor: int,
    ) -> None:
        """Persist an edit action and cursor update to SQLite.

        Applies branch-truncation in SQLite: deletes rows with
        sequence_index > old cursor before inserting the new one.

        Args:
            document_session_id: Session identifier.
            action: The EditAction to persist.
            new_cursor: The new cursor position.
        """
        try:
            # Branch-truncation in SQLite: delete future rows
            self._db_conn.execute(
                "DELETE FROM block_edit_history "
                "WHERE document_session_id = ? AND sequence_index >= ?",
                (document_session_id, action.sequence_index),
            )

            # Insert the new edit
            self._db_conn.execute(
                "INSERT INTO block_edit_history "
                "(job_id, document_session_id, sequence_index, block_id, "
                "previous_content, previous_hash, new_content, new_hash, edited_at) "
                "VALUES (NULL, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    document_session_id,
                    action.sequence_index,
                    action.block_id,
                    action.previous_content,
                    action.previous_hash,
                    action.new_content,
                    action.new_hash,
                    action.edited_at,
                ),
            )

            # Update cursor
            self._db_conn.execute(
                "INSERT OR REPLACE INTO edit_cursor "
                "(document_session_id, current_sequence_index) VALUES (?, ?)",
                (document_session_id, new_cursor),
            )
            self._db_conn.commit()
        except sqlite3.Error:
            self._db_conn.rollback()
            logger.exception(
                "Failed to persist edit for session %s", document_session_id
            )
            raise

    # ═══════════════════════════════════════════════════════════════════
    # Undo
    # ═══════════════════════════════════════════════════════════════════

    def undo(
        self,
        document_session_id: str,
        document_store: "DocumentStore",
    ) -> bool:
        """Undo the most recently applied edit in the given session.

        Single logical operation executed under DocumentStore's RLock:
          1. Acquire document_store.lock
          2. Read cursor; if == 0, no-op
          3. Fetch the action at current_sequence_index
          4. Validate block's current hash matches action.new_hash
          5. Replace block in DocumentStore with previous_content/hash
          6. Decrement cursor
          7. Release document_store.lock

        Args:
            document_session_id: Session to undo within.
            document_store: The DocumentStore to mutate.

        Returns:
            True if an undo was performed, False if nothing to undo.

        Raises:
            UndoRedoError: If the block's current state doesn't match
                           the expected state (out-of-band mutation detected).
        """
        with self._sessions_lock:
            session = self._sessions.get(document_session_id)
        if session is None:
            return False

        with document_store.lock:
            if session.cursor_position == 0:
                return False

            # Fetch the action to undo
            action_idx = session.cursor_position - 1  # 0-based index
            if action_idx < 0 or action_idx >= len(session.actions):
                return False
            action = session.actions[action_idx]

            # Validate current block state matches what we recorded
            doc = document_store.get_document(session.doc_id)
            if doc is None:
                return False

            current_block = self._find_block(doc, action.block_id)
            if current_block is None:
                raise UndoRedoError(
                    f"Block {action.block_id} not found in document — "
                    f"cannot undo."
                )

            if current_block.content_hash != action.new_hash:
                raise UndoRedoError(
                    "⚠️ لا يمكن التراجع — تم تعديل الكتلة النصية من مصدر آخر "
                    "بعد التعديل الأصلي. التجزئة الحالية لا تتطابق مع التجزئة المسجلة."
                )

            # Replace block with previous content
            restored_block = current_block.model_copy(
                update={
                    "raw_content": action.previous_content,
                    "content_hash": action.previous_hash,
                    "user_edited": True,
                    "edit_timestamp": datetime.now(timezone.utc).isoformat(),
                }
            )

            document_store.update_block(
                doc_id=session.doc_id,
                page_number=action.page_number,
                block_id=action.block_id,
                new_block=restored_block,
            )

            # Decrement cursor — still inside the lock
            session.cursor_position -= 1

            # Persist cursor to SQLite
            self._update_cursor_db(
                document_session_id, session.cursor_position
            )

        logger.info(
            "Undo: session=%s, block=%s, cursor now at %d",
            document_session_id,
            action.block_id,
            session.cursor_position,
        )
        return True

    # ═══════════════════════════════════════════════════════════════════
    # Redo
    # ═══════════════════════════════════════════════════════════════════

    def redo(
        self,
        document_session_id: str,
        document_store: "DocumentStore",
    ) -> bool:
        """Re-apply the next edit in the given session.

        Mirror of undo(): re-applies new_content/new_hash at cursor+1,
        under the same single-lock discipline.

        Args:
            document_session_id: Session to redo within.
            document_store: The DocumentStore to mutate.

        Returns:
            True if a redo was performed, False if nothing to redo.

        Raises:
            UndoRedoError: If the block's current state doesn't match
                           the expected pre-redo state.
        """
        with self._sessions_lock:
            session = self._sessions.get(document_session_id)
        if session is None:
            return False

        with document_store.lock:
            if session.cursor_position >= len(session.actions):
                return False

            # Fetch the action to redo (0-based: cursor_position is next)
            action = session.actions[session.cursor_position]

            # Validate current block state matches the action's previous state
            doc = document_store.get_document(session.doc_id)
            if doc is None:
                return False

            current_block = self._find_block(doc, action.block_id)
            if current_block is None:
                raise UndoRedoError(
                    f"Block {action.block_id} not found in document — "
                    f"cannot redo."
                )

            if current_block.content_hash != action.previous_hash:
                raise UndoRedoError(
                    "⚠️ لا يمكن الإعادة — تم تعديل الكتلة النصية من مصدر آخر "
                    "بعد التراجع. التجزئة الحالية لا تتطابق مع التجزئة المسجلة."
                )

            # Replace block with new content
            reapplied_block = current_block.model_copy(
                update={
                    "raw_content": action.new_content,
                    "content_hash": action.new_hash,
                    "user_edited": True,
                    "edit_timestamp": datetime.now(timezone.utc).isoformat(),
                }
            )

            document_store.update_block(
                doc_id=session.doc_id,
                page_number=action.page_number,
                block_id=action.block_id,
                new_block=reapplied_block,
            )

            # Advance cursor — still inside the lock
            session.cursor_position += 1

            # Persist cursor to SQLite
            self._update_cursor_db(
                document_session_id, session.cursor_position
            )

        logger.info(
            "Redo: session=%s, block=%s, cursor now at %d",
            document_session_id,
            action.block_id,
            session.cursor_position,
        )
        return True

    # ═══════════════════════════════════════════════════════════════════
    # Query state
    # ═══════════════════════════════════════════════════════════════════

    def can_undo(self, document_session_id: str) -> bool:
        """Check if undo is available for the given session."""
        with self._sessions_lock:
            session = self._sessions.get(document_session_id)
        return session is not None and session.cursor_position > 0

    def can_redo(self, document_session_id: str) -> bool:
        """Check if redo is available for the given session."""
        with self._sessions_lock:
            session = self._sessions.get(document_session_id)
        return (
            session is not None
            and session.cursor_position < len(session.actions)
        )

    # ═══════════════════════════════════════════════════════════════════
    # Rebuild from database (document reopen)
    # ═══════════════════════════════════════════════════════════════════

    def rebuild_from_db(
        self,
        document_session_id: str,
        doc_id: str,
    ) -> UndoRedoSession:
        """Rebuild the in-memory undo/redo stack from SQLite.

        Called when a document is reopened. Reads all block_edit_history
        rows for this session in sequence_index order and reconstructs
        the in-memory actions list and cursor position.

        Args:
            document_session_id: Session to rebuild.
            doc_id: DocumentStore doc_id for this session.

        Returns:
            The rebuilt UndoRedoSession.
        """
        cursor = self._db_conn.execute(
            "SELECT sequence_index, block_id, previous_content, previous_hash, "
            "new_content, new_hash, edited_at "
            "FROM block_edit_history "
            "WHERE document_session_id = ? "
            "ORDER BY sequence_index ASC",
            (document_session_id,),
        )
        rows = cursor.fetchall()

        # Read cursor position
        cursor_row = self._db_conn.execute(
            "SELECT current_sequence_index FROM edit_cursor "
            "WHERE document_session_id = ?",
            (document_session_id,),
        ).fetchone()
        cursor_pos = cursor_row["current_sequence_index"] if cursor_row else 0

        actions: List[EditAction] = []
        for row in rows:
            # page_number is not stored in SQLite — we set 0 as placeholder;
            # during replay, the actual page is looked up from DocumentStore
            actions.append(
                EditAction(
                    sequence_index=row["sequence_index"],
                    block_id=row["block_id"],
                    page_number=0,  # resolved during replay
                    previous_content=row["previous_content"],
                    previous_hash=row["previous_hash"],
                    new_content=row["new_content"],
                    new_hash=row["new_hash"],
                    edited_at=row["edited_at"],
                )
            )

        session = UndoRedoSession(
            document_session_id=document_session_id,
            doc_id=doc_id,
            actions=actions,
            cursor_position=cursor_pos,
        )

        with self._sessions_lock:
            self._sessions[document_session_id] = session

        logger.info(
            "Rebuilt session %s from DB: %d actions, cursor at %d",
            document_session_id,
            len(actions),
            cursor_pos,
        )
        return session

    def get_replay_actions(
        self, document_session_id: str
    ) -> List[EditAction]:
        """Get the list of actions to replay up to the cursor position.

        Used during document-reopen recovery to replay edits onto a
        fresh extraction. Only returns actions up to cursor_position
        (not undone actions beyond the cursor).

        Args:
            document_session_id: Session to get replay actions for.

        Returns:
            Ordered list of EditActions to replay.
        """
        with self._sessions_lock:
            session = self._sessions.get(document_session_id)
        if session is None:
            return []
        return session.actions[:session.cursor_position]

    # ═══════════════════════════════════════════════════════════════════
    # Job ID backfill
    # ═══════════════════════════════════════════════════════════════════

    def backfill_job_id(
        self, document_session_id: str, job_id: int
    ) -> None:
        """Backfill job_id for all edits in a session.

        Called when a document is submitted as a generation job —
        ties pre-job edits to their eventual job_history row.

        Args:
            document_session_id: Session whose edits to backfill.
            job_id: The job_history.id to set.
        """
        try:
            self._db_conn.execute(
                "UPDATE block_edit_history SET job_id = ? "
                "WHERE document_session_id = ? AND job_id IS NULL",
                (job_id, document_session_id),
            )
            self._db_conn.commit()
            logger.info(
                "Backfilled job_id=%d for session %s",
                job_id,
                document_session_id,
            )
        except sqlite3.Error:
            self._db_conn.rollback()
            logger.exception(
                "Failed to backfill job_id for session %s",
                document_session_id,
            )
            raise

    # ═══════════════════════════════════════════════════════════════════
    # Private helpers
    # ═══════════════════════════════════════════════════════════════════

    @staticmethod
    def _find_block(
        doc: "BookletDocument", block_id: str
    ) -> Optional["ContentBlock"]:
        """Find a ContentBlock by block_id across all pages.

        Args:
            doc: The BookletDocument to search.
            block_id: The block ID to find.

        Returns:
            The ContentBlock if found, None otherwise.
        """
        from app.core.models import ContentBlock

        for page in doc.pages:
            for block in page.content_blocks:
                if block.block_id == block_id:
                    return block
        return None

    def _update_cursor_db(
        self, document_session_id: str, position: int
    ) -> None:
        """Update the cursor position in SQLite.

        Args:
            document_session_id: Session identifier.
            position: New cursor position.
        """
        try:
            self._db_conn.execute(
                "INSERT OR REPLACE INTO edit_cursor "
                "(document_session_id, current_sequence_index) VALUES (?, ?)",
                (document_session_id, position),
            )
            self._db_conn.commit()
        except sqlite3.Error:
            self._db_conn.rollback()
            logger.exception(
                "Failed to update cursor for session %s",
                document_session_id,
            )
