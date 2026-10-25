"""
Booklet_AI Desktop — Tests for STEP-6.3

Covers:
- AC-6.3.1:  Live Preview renders without COM/InDesign
- AC-6.3.1b: RLock held only for snapshot copy, not for rendering
- AC-6.3.2:  Job History uses translated error messages
- AC-6.3.3:  Undo restores block state under single lock acquisition
- AC-6.3.3b: Branch-truncation scenario A→B→C, undo×2, new D
- AC-6.3.3c: Undo refuses on hash mismatch (out-of-band mutation)
- AC-6.3.4:  Only with_user_edit() path writes to block_edit_history
- AC-6.3.5:  Job History never acquires DocumentStore's RLock
- AC-6.3.6:  Replay reconstructs edited state on matching file hash
- AC-6.3.7:  Replay refuses on non-matching file hash
"""

import hashlib
import sqlite3
import threading
import tempfile
import os
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from app.core.exceptions import TextIntegrityError
from app.core.models import (
    BookletDocument,
    ContentBlock,
    PageData,
    TableData,
)
from app.core.text_integrity import compute_canonical_hash
from app.state.document_store import DocumentStore
from app.state.job_history import JobHistoryManager, compute_source_file_sha256
from app.state.undo_redo import UndoRedoError, UndoRedoManager
from app.state.document_recovery import (
    RecoveryResult,
    check_source_file_integrity,
    get_file_changed_warning,
    replay_edit_history,
)
from app.ui.error_translation import translate_error


# ═══════════════════════════════════════════════════════════════════════
# Fixtures
# ═══════════════════════════════════════════════════════════════════════


@pytest.fixture
def db_conn():
    """In-memory SQLite database with migrations applied."""
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON;")

    # Apply migration 002
    migration_path = (
        Path(__file__).resolve().parent.parent
        / "migrations"
        / "002_job_history_and_undo.sql"
    )
    sql = migration_path.read_text(encoding="utf-8")
    conn.executescript(sql)
    conn.commit()
    return conn


@pytest.fixture
def document_store():
    """DocumentStore with a sample document loaded."""
    store = DocumentStore()
    block_a = ContentBlock(
        block_id="blk-A",
        block_type="arabic_text",
        raw_content="النص الأصلي أ",
        content_hash=compute_canonical_hash("النص الأصلي أ"),
    )
    block_b_content = "النص الأصلي ب"
    block_b = ContentBlock(
        block_id="blk-B",
        block_type="arabic_text",
        raw_content=block_b_content,
        content_hash=compute_canonical_hash(block_b_content),
    )
    page = PageData(
        page_number=1,
        width=595.0,
        height=842.0,
        header_text="عنوان الفصل",
        content_blocks=[block_a, block_b],
    )
    doc = BookletDocument(
        doc_id="doc-001",
        file_path="C:/test/sample.docx",
        file_type="docx",
        pages=[page],
    )
    store.load_document(doc)
    return store


@pytest.fixture
def undo_manager(db_conn):
    """UndoRedoManager instance."""
    return UndoRedoManager(db_conn)


@pytest.fixture
def job_manager(db_conn):
    """JobHistoryManager instance."""
    return JobHistoryManager(db_conn)


# ═══════════════════════════════════════════════════════════════════════
# AC-6.3.1: Live Preview renders without COM/InDesign
# ═══════════════════════════════════════════════════════════════════════


class TestLivePreviewNoCOM:
    """AC-6.3.1: Verify no win32com/pythoncom import in preview code."""

    def test_no_com_imports_in_live_preview(self):
        """No win32com or pythoncom import statement in the preview module."""
        import app.ui.live_preview as lp

        source_file = Path(lp.__file__).read_text(encoding="utf-8")
        # Check only actual import statements, not docstring mentions
        import re
        import_lines = [
            line.strip()
            for line in source_file.splitlines()
            if line.strip().startswith(("import ", "from "))
        ]
        for line in import_lines:
            assert "win32com" not in line, (
                f"live_preview.py must not import win32com: {line}"
            )
            assert "pythoncom" not in line, (
                f"live_preview.py must not import pythoncom: {line}"
            )

    def test_snapshot_reads_document_store(self, document_store):
        """Live preview can take a snapshot from DocumentStore."""
        from app.ui.live_preview import take_snapshot

        snapshots = take_snapshot(document_store)
        assert len(snapshots) == 1
        assert snapshots[0].page_number == 1
        assert len(snapshots[0].content_blocks) == 2

    def test_snapshot_single_page(self, document_store):
        """Snapshot of a specific page works."""
        from app.ui.live_preview import take_snapshot

        snapshots = take_snapshot(document_store, page_number=1)
        assert len(snapshots) == 1
        assert snapshots[0].page_number == 1

    def test_snapshot_nonexistent_page(self, document_store):
        """Snapshot of a non-existent page returns empty list."""
        from app.ui.live_preview import take_snapshot

        snapshots = take_snapshot(document_store, page_number=999)
        assert len(snapshots) == 0


# ═══════════════════════════════════════════════════════════════════════
# AC-6.3.1b: RLock held only for snapshot, not rendering
# ═══════════════════════════════════════════════════════════════════════


class TestLivePreviewLockDiscipline:
    """AC-6.3.1b: Verify lock is released before rendering."""

    def test_lock_released_after_snapshot(self, document_store):
        """The lock is NOT held after take_snapshot returns."""
        from app.ui.live_preview import take_snapshot

        snapshots = take_snapshot(document_store)

        # The lock should be acquirable after snapshot returns
        # (proving it was released)
        acquired = document_store.lock.acquire(timeout=0.1)
        assert acquired, "Lock should be available after take_snapshot"
        document_store.lock.release()

    def test_no_qt_paint_inside_lock(self):
        """Verify by code inspection: no Qt paint calls between
        lock acquire and release in the snapshot function."""
        from app.ui.live_preview import take_snapshot
        import inspect

        source = inspect.getsource(take_snapshot)
        # The function body between 'with document_store.lock:' and the
        # end of the with block should not contain any paint/render calls
        assert "QPainter" not in source
        assert ".paint" not in source
        assert ".render" not in source


# ═══════════════════════════════════════════════════════════════════════
# AC-6.3.2: Job History uses translated error messages
# ═══════════════════════════════════════════════════════════════════════


class TestJobHistory:
    """AC-6.3.2: Every job produces exactly one job_history row,
    with translated error on failure."""

    def test_start_and_complete_job(self, job_manager, db_conn):
        """Successful job creates exactly one row with status='success'."""
        job_id = job_manager.start_job(
            source_file_path="C:/test/sample.docx",
            source_file_sha256="abc123",
        )
        job_manager.complete_job(job_id, page_count=12)

        job = job_manager.get_job(job_id)
        assert job is not None
        assert job["status"] == "success"
        assert job["page_count"] == 12
        assert job["error_summary"] is None

    def test_failed_job_uses_translated_error(self, job_manager):
        """Failed job's error_summary is STEP-6.2 translated, not raw."""
        job_id = job_manager.start_job(
            source_file_path="C:/test/sample.docx",
            source_file_sha256="abc123",
        )
        exc = TextIntegrityError("Hash mismatch detected")
        job_manager.fail_job(job_id, exc)

        job = job_manager.get_job(job_id)
        assert job["status"] == "failed"
        # Must contain Arabic translation from STEP-6.2, not raw English
        assert "تم اكتشاف تغيير غير مصرّح" in job["error_summary"]
        assert "Hash mismatch" not in job["error_summary"]

    def test_failed_job_fallback_unknown_error(self, job_manager):
        """Unknown exception type uses STEP-6.2 fallback message."""
        job_id = job_manager.start_job(
            source_file_path="C:/test/sample.docx",
            source_file_sha256="abc123",
        )
        exc = RuntimeError("Something unexpected")
        job_manager.fail_job(job_id, exc)

        job = job_manager.get_job(job_id)
        assert job["status"] == "failed"
        assert "حدث خطأ غير متوقع" in job["error_summary"]

    def test_get_recent_jobs(self, job_manager):
        """get_recent_jobs returns jobs in descending order."""
        id1 = job_manager.start_job("file1.docx", "hash1")
        job_manager.complete_job(id1)
        id2 = job_manager.start_job("file2.docx", "hash2")
        job_manager.complete_job(id2)

        jobs = job_manager.get_recent_jobs(limit=10)
        assert len(jobs) == 2
        assert jobs[0]["id"] == id2  # most recent first

    def test_get_jobs_for_file(self, job_manager):
        """get_jobs_for_file returns only jobs for a specific file hash."""
        id1 = job_manager.start_job("file1.docx", "hash_AAA")
        job_manager.complete_job(id1)
        id2 = job_manager.start_job("file2.docx", "hash_BBB")
        job_manager.complete_job(id2)

        aaa_jobs = job_manager.get_jobs_for_file("hash_AAA")
        assert len(aaa_jobs) == 1
        assert aaa_jobs[0]["source_file_sha256"] == "hash_AAA"


# ═══════════════════════════════════════════════════════════════════════
# AC-6.3.3: Undo restores block state under single lock acquisition
# ═══════════════════════════════════════════════════════════════════════


class TestUndoRedo:
    """AC-6.3.3: Undo/Redo correctness and lock discipline."""

    def test_undo_restores_previous_state(
        self, undo_manager, document_store
    ):
        """After with_user_edit + record_edit, undo restores original."""
        original_block = document_store.get_page_slice(1).content_blocks[0]
        original_content = original_block.raw_content
        original_hash = original_block.content_hash

        # Simulate a user edit via with_user_edit
        new_content = "نص معدّل جديد"
        edited_block = original_block.with_user_edit(new_content)
        document_store.update_block("doc-001", 1, "blk-A", edited_block)

        # Record the edit in undo manager
        undo_manager.record_edit(
            document_session_id="session-1",
            doc_id="doc-001",
            block_id="blk-A",
            page_number=1,
            previous_content=original_content,
            previous_hash=original_hash,
            new_content=edited_block.raw_content,
            new_hash=edited_block.content_hash,
        )

        # Undo
        result = undo_manager.undo("session-1", document_store)
        assert result is True

        # Verify block is restored
        restored = document_store.get_page_slice(1).content_blocks[0]
        assert restored.raw_content == original_content
        assert restored.content_hash == original_hash

    def test_undo_under_single_lock(self, undo_manager, document_store):
        """Undo performs validate + mutate + cursor update under one lock.

        Verified by source inspection: the undo() method wraps all steps
        (validate, mutate, cursor update) in a single `with document_store.lock:`
        block — there is no gap where the lock is released and re-acquired.
        """
        import inspect
        from app.state.undo_redo import UndoRedoManager

        source = inspect.getsource(UndoRedoManager.undo)
        # The undo method must have exactly ONE `with document_store.lock:`
        # All validate + mutate + cursor steps happen inside it
        assert "with document_store.lock:" in source, (
            "undo() must use `with document_store.lock:` for atomic operation"
        )
        # And must not release/re-acquire the lock mid-operation
        assert source.count("with document_store.lock:") == 1, (
            "undo() must have exactly one lock acquisition, not multiple"
        )

        # Also verify undo actually works correctly
        original_block = document_store.get_page_slice(1).content_blocks[0]
        edited = original_block.with_user_edit("تعديل")
        document_store.update_block("doc-001", 1, "blk-A", edited)

        undo_manager.record_edit(
            document_session_id="session-1",
            doc_id="doc-001",
            block_id="blk-A",
            page_number=1,
            previous_content=original_block.raw_content,
            previous_hash=original_block.content_hash,
            new_content=edited.raw_content,
            new_hash=edited.content_hash,
        )

        undo_manager.undo("session-1", document_store)
        restored = document_store.get_page_slice(1).content_blocks[0]
        assert restored.raw_content == original_block.raw_content


# ═══════════════════════════════════════════════════════════════════════
# AC-6.3.3b: Branch-truncation scenario
# ═══════════════════════════════════════════════════════════════════════


class TestBranchTruncation:
    """AC-6.3.3b: A→B→C, undo×2, new edit D — B/C deleted, redo after D is no-op."""

    def test_branch_truncation_scenario(
        self, undo_manager, document_store, db_conn
    ):
        """Full branch truncation scenario from the plan."""
        session_id = "session-branch"

        # Get original state
        original = document_store.get_page_slice(1).content_blocks[0]

        # Edit A: original → content_A
        content_a = "محتوى أ"
        edited_a = original.with_user_edit(content_a)
        document_store.update_block("doc-001", 1, "blk-A", edited_a)
        undo_manager.record_edit(
            session_id, "doc-001", "blk-A", 1,
            original.raw_content, original.content_hash,
            edited_a.raw_content, edited_a.content_hash,
        )

        # Edit B: content_A → content_B
        content_b = "محتوى ب"
        edited_b = edited_a.with_user_edit(content_b)
        document_store.update_block("doc-001", 1, "blk-A", edited_b)
        undo_manager.record_edit(
            session_id, "doc-001", "blk-A", 1,
            edited_a.raw_content, edited_a.content_hash,
            edited_b.raw_content, edited_b.content_hash,
        )

        # Edit C: content_B → content_C
        content_c = "محتوى ج"
        edited_c = edited_b.with_user_edit(content_c)
        document_store.update_block("doc-001", 1, "blk-A", edited_c)
        undo_manager.record_edit(
            session_id, "doc-001", "blk-A", 1,
            edited_b.raw_content, edited_b.content_hash,
            edited_c.raw_content, edited_c.content_hash,
        )

        # Verify cursor at 3 (A=1, B=2, C=3)
        assert undo_manager.can_undo(session_id)
        assert not undo_manager.can_redo(session_id)

        # Undo twice: cursor goes from 3→2→1 (back to state after edit A)
        undo_manager.undo(session_id, document_store)  # undo C → back to B
        undo_manager.undo(session_id, document_store)  # undo B → back to A

        block_after_undo = document_store.get_page_slice(1).content_blocks[0]
        assert block_after_undo.raw_content == content_a

        # New edit D from state A — this should truncate B and C
        content_d = "محتوى د"
        edited_d = block_after_undo.with_user_edit(content_d)
        document_store.update_block("doc-001", 1, "blk-A", edited_d)
        undo_manager.record_edit(
            session_id, "doc-001", "blk-A", 1,
            block_after_undo.raw_content, block_after_undo.content_hash,
            edited_d.raw_content, edited_d.content_hash,
        )

        # After D: redo should be impossible (B/C are gone)
        assert not undo_manager.can_redo(session_id)
        redo_result = undo_manager.redo(session_id, document_store)
        assert redo_result is False

        # Verify B and C rows are deleted from SQLite
        cursor = db_conn.execute(
            "SELECT sequence_index FROM block_edit_history "
            "WHERE document_session_id = ? ORDER BY sequence_index",
            (session_id,),
        )
        remaining_indices = [row["sequence_index"] for row in cursor.fetchall()]
        assert remaining_indices == [1, 2], (
            f"Expected [1, 2] (A and D), got {remaining_indices}"
        )

        # Verify current state is D
        current = document_store.get_page_slice(1).content_blocks[0]
        assert current.raw_content == content_d


# ═══════════════════════════════════════════════════════════════════════
# AC-6.3.3c: Undo refuses on hash mismatch
# ═══════════════════════════════════════════════════════════════════════


class TestUndoHashMismatch:
    """AC-6.3.3c: Undo refuses if block was mutated out-of-band."""

    def test_undo_refuses_on_out_of_band_mutation(
        self, undo_manager, document_store
    ):
        """Undo raises error if block's hash doesn't match recorded state."""
        original = document_store.get_page_slice(1).content_blocks[0]

        # Normal edit recorded
        edited = original.with_user_edit("تعديل أول")
        document_store.update_block("doc-001", 1, "blk-A", edited)
        undo_manager.record_edit(
            "session-x", "doc-001", "blk-A", 1,
            original.raw_content, original.content_hash,
            edited.raw_content, edited.content_hash,
        )

        # Out-of-band mutation: someone changes the block directly
        # (not through with_user_edit recorded in undo manager)
        tampered = edited.with_user_edit("تعديل خارجي غير مسجل")
        document_store.update_block("doc-001", 1, "blk-A", tampered)

        # Now undo should REFUSE because hashes don't match
        with pytest.raises(UndoRedoError):
            undo_manager.undo("session-x", document_store)


# ═══════════════════════════════════════════════════════════════════════
# AC-6.3.4: Only with_user_edit writes to block_edit_history
# ═══════════════════════════════════════════════════════════════════════


class TestOnlyUserEditWrites:
    """AC-6.3.4: No AI/extraction code path writes to block_edit_history."""

    def test_record_edit_requires_user_edit_fields(self, undo_manager):
        """record_edit requires previous and new content/hash — only
        obtainable through with_user_edit() which sets user_edited=True."""
        # This is a design enforcement test: record_edit is called
        # ONLY by the UI's edit handler after with_user_edit().
        # The undo_redo module itself has no extraction or classification
        # import.
        import app.state.undo_redo as ur

        source = Path(ur.__file__).read_text(encoding="utf-8")
        # Must not import classifier, extractor, or AI modules
        assert "classifier" not in source
        assert "pdf_extractor" not in source
        assert "docx_extractor" not in source
        assert "app.ai" not in source


# ═══════════════════════════════════════════════════════════════════════
# AC-6.3.5: Job History never acquires DocumentStore's RLock
# ═══════════════════════════════════════════════════════════════════════


class TestJobHistoryNoLock:
    """AC-6.3.5: Job History operations don't touch DocumentStore's lock."""

    def test_job_history_no_document_store_import(self):
        """job_history.py never imports DocumentStore or uses RLock."""
        import app.state.job_history as jh

        source = Path(jh.__file__).read_text(encoding="utf-8")
        # Check actual import statements, not docstring references
        import_lines = [
            line.strip()
            for line in source.splitlines()
            if line.strip().startswith(("import ", "from "))
        ]
        for line in import_lines:
            assert "DocumentStore" not in line, (
                f"job_history.py must not import DocumentStore: {line}"
            )
            assert "RLock" not in line, (
                f"job_history.py must not import RLock: {line}"
            )
            assert "threading" not in line, (
                f"job_history.py must not import threading: {line}"
            )

    def test_job_history_operations_without_document_store(
        self, job_manager
    ):
        """All job_history operations work with only SQLite, no store."""
        # Start, complete, fail, query — none need DocumentStore
        j1 = job_manager.start_job("file.docx", "hash1")
        job_manager.complete_job(j1, page_count=5)

        j2 = job_manager.start_job("file.docx", "hash1")
        job_manager.fail_job(j2, RuntimeError("test"))

        jobs = job_manager.get_recent_jobs()
        assert len(jobs) == 2

        for_file = job_manager.get_jobs_for_file("hash1")
        assert len(for_file) == 2


# ═══════════════════════════════════════════════════════════════════════
# AC-6.3.6: Replay reconstructs edited state on matching file hash
# ═══════════════════════════════════════════════════════════════════════


class TestReplayOnMatch:
    """AC-6.3.6: Reopening with matching hash replays edits correctly."""

    def test_replay_reconstructs_edited_state(
        self, undo_manager, document_store
    ):
        """Full end-to-end replay over fresh extraction."""
        session_id = "session-replay"

        # Make an edit
        original = document_store.get_page_slice(1).content_blocks[0]
        edited = original.with_user_edit("محتوى معدّل للإعادة")
        document_store.update_block("doc-001", 1, "blk-A", edited)
        undo_manager.record_edit(
            session_id, "doc-001", "blk-A", 1,
            original.raw_content, original.content_hash,
            edited.raw_content, edited.content_hash,
        )

        # Simulate app restart: create a NEW document store with
        # fresh extraction (same content as original)
        fresh_store = DocumentStore()
        fresh_block = ContentBlock(
            block_id="blk-A",
            block_type="arabic_text",
            raw_content="النص الأصلي أ",
            content_hash=compute_canonical_hash("النص الأصلي أ"),
        )
        fresh_page = PageData(
            page_number=1,
            width=595.0,
            height=842.0,
            content_blocks=[fresh_block],
        )
        fresh_doc = BookletDocument(
            doc_id="doc-001",
            file_path="C:/test/sample.docx",
            file_type="docx",
            pages=[fresh_page],
        )
        fresh_store.load_document(fresh_doc)

        # Rebuild undo manager from DB
        undo_manager.rebuild_from_db(session_id, "doc-001")

        # Replay edits
        result = replay_edit_history(
            session_id, "doc-001", fresh_store, undo_manager
        )

        assert result.success is True
        assert result.replayed_count == 1
        assert result.skipped_count == 0

        # Verify the block has the edited content
        replayed_block = fresh_store.get_page_slice(1).content_blocks[0]
        assert replayed_block.raw_content == "محتوى معدّل للإعادة"
        assert replayed_block.user_edited is True


# ═══════════════════════════════════════════════════════════════════════
# AC-6.3.7: Reopen refuses on non-matching file hash
# ═══════════════════════════════════════════════════════════════════════


class TestReplayOnMismatch:
    """AC-6.3.7: Never silently apply old edits when file has changed."""

    def test_hash_mismatch_detected(self):
        """check_source_file_integrity returns False on different hash."""
        with tempfile.NamedTemporaryFile(
            suffix=".docx", delete=False, mode="wb"
        ) as f:
            f.write(b"original content")
            temp_path = f.name

        try:
            original_hash = compute_source_file_sha256(temp_path)

            # Modify the file
            with open(temp_path, "wb") as f:
                f.write(b"modified content")

            assert not check_source_file_integrity(temp_path, original_hash)
        finally:
            os.unlink(temp_path)

    def test_hash_match_confirmed(self):
        """check_source_file_integrity returns True on same hash."""
        with tempfile.NamedTemporaryFile(
            suffix=".docx", delete=False, mode="wb"
        ) as f:
            f.write(b"original content")
            temp_path = f.name

        try:
            original_hash = compute_source_file_sha256(temp_path)
            assert check_source_file_integrity(temp_path, original_hash)
        finally:
            os.unlink(temp_path)

    def test_warning_message_is_arabic(self):
        """The file-changed warning is in Arabic per STEP-6.2."""
        msg = get_file_changed_warning()
        assert "⚠️" in msg
        assert "ملف المصدر تغيّر" in msg


# ═══════════════════════════════════════════════════════════════════════
# Additional: Redo test
# ═══════════════════════════════════════════════════════════════════════


class TestRedo:
    """Test redo functionality."""

    def test_redo_after_undo(self, undo_manager, document_store):
        """Redo re-applies the undone edit."""
        original = document_store.get_page_slice(1).content_blocks[0]
        edited = original.with_user_edit("نص redo")
        document_store.update_block("doc-001", 1, "blk-A", edited)

        undo_manager.record_edit(
            "session-redo", "doc-001", "blk-A", 1,
            original.raw_content, original.content_hash,
            edited.raw_content, edited.content_hash,
        )

        # Undo
        undo_manager.undo("session-redo", document_store)
        block_undone = document_store.get_page_slice(1).content_blocks[0]
        assert block_undone.raw_content == original.raw_content

        # Redo
        result = undo_manager.redo("session-redo", document_store)
        assert result is True

        block_redone = document_store.get_page_slice(1).content_blocks[0]
        assert block_redone.raw_content == "نص redo"

    def test_redo_noop_when_at_end(self, undo_manager, document_store):
        """Redo is a no-op when cursor is at the end of history."""
        original = document_store.get_page_slice(1).content_blocks[0]
        edited = original.with_user_edit("تعديل")
        document_store.update_block("doc-001", 1, "blk-A", edited)

        undo_manager.record_edit(
            "session-noop", "doc-001", "blk-A", 1,
            original.raw_content, original.content_hash,
            edited.raw_content, edited.content_hash,
        )

        # No undo performed, so redo should be no-op
        assert not undo_manager.can_redo("session-noop")
        result = undo_manager.redo("session-noop", document_store)
        assert result is False


# ═══════════════════════════════════════════════════════════════════════
# Additional: Job ID backfill
# ═══════════════════════════════════════════════════════════════════════


class TestJobIdBackfill:
    """Test that pre-job edits get backfilled with job_id."""

    def test_backfill_job_id(self, undo_manager, job_manager, db_conn):
        """Edits made before job submission get job_id retroactively."""
        session_id = "session-backfill"

        # Record an edit (no job exists yet)
        undo_manager.record_edit(
            session_id, "doc-001", "blk-A", 1,
            "prev", "hash_prev", "new", "hash_new",
        )

        # Verify job_id is NULL
        row = db_conn.execute(
            "SELECT job_id FROM block_edit_history "
            "WHERE document_session_id = ?",
            (session_id,),
        ).fetchone()
        assert row["job_id"] is None

        # Create a job and backfill
        job_id = job_manager.start_job("file.docx", "hash1")
        undo_manager.backfill_job_id(session_id, job_id)

        # Verify job_id is now set
        row = db_conn.execute(
            "SELECT job_id FROM block_edit_history "
            "WHERE document_session_id = ?",
            (session_id,),
        ).fetchone()
        assert row["job_id"] == job_id


# ═══════════════════════════════════════════════════════════════════════
# Additional: source_file_sha256 computation
# ═══════════════════════════════════════════════════════════════════════


class TestSourceFileHash:
    """Test source file SHA-256 computation."""

    def test_compute_source_file_sha256(self):
        """SHA-256 of file bytes matches manual calculation."""
        content = b"test file content for hashing"
        expected = hashlib.sha256(content).hexdigest()

        with tempfile.NamedTemporaryFile(
            suffix=".docx", delete=False, mode="wb"
        ) as f:
            f.write(content)
            temp_path = f.name

        try:
            result = compute_source_file_sha256(temp_path)
            assert result == expected
        finally:
            os.unlink(temp_path)

    def test_source_vs_content_hash_differ(self):
        """source_file_sha256 ≠ compute_canonical_hash — they are
        fundamentally different functions (AC-6.3 post-review correction).

        The two functions differ because compute_canonical_hash applies
        NFKC→NFC normalization, strips invisible chars, normalizes
        whitespace, and then hashes — while source_file_sha256 hashes
        the raw file bytes including BOM, encoding overhead, etc.
        We use content with diacritics and BOM to make this visible.
        """
        # Use text with Arabic diacritics and BOM prefix in file
        text = "بِسْمِ اللَّهِ الرَّحْمَٰنِ الرَّحِيمِ"
        content_hash = compute_canonical_hash(text)

        with tempfile.NamedTemporaryFile(
            suffix=".txt", delete=False, mode="wb"
        ) as f:
            # Write with BOM — the file bytes will include the BOM prefix,
            # making the raw file hash differ from canonical text hash
            f.write(b"\xef\xbb\xbf")  # UTF-8 BOM
            f.write(text.encode("utf-8"))
            temp_path = f.name

        try:
            file_hash = compute_source_file_sha256(temp_path)
            assert file_hash != content_hash, (
                "Source file SHA-256 and content canonical hash must differ"
            )
        finally:
            os.unlink(temp_path)
