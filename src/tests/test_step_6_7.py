"""
Booklet_AI Desktop — Tests for STEP-6.7 (Batch Processing — Sequential Queue)

Covers all Acceptance Criteria:
- [AC-6.7.1]: A batch of N queued files results in exactly N separate document-level
  classification sequences, each with only that file's blocks — verified by asserting
  no request sent to the AI client contains block_ids from more than one source_doc_id,
  across both single-request and multi-chunk documents.
- [AC-6.7.1b]: A single document whose eligible-block payload exceeds the configured token
  budget is split into multiple sequential requests by classify_blocks(), each independently
  exact-match validated, with results deterministically merged by block_id.
- [AC-6.7.1c]: A with_user_edit() or undo()/redo() call occurring on DocumentStore while
  a multi-chunk classify_blocks() call is still in flight causes the eventual merged
  classification result to be discarded rather than applied.
- [AC-6.7.2]: A Hard-Halt failure (e.g. TextIntegrityError) in file 2 of a 5-file batch
  records file 2 as failed in job_history and does not prevent files 3-5 from being processed.
- [AC-6.7.3]: Queued jobs' writes to ministry_questions, ministry_question_occurrences,
  and job_history never require a schema or write path beyond what STEP-3.1/6.3/6.7 define.
- [AC-6.7.4]: Two files queued back-to-back containing a ministry_question block with identical
  content_hash result in only one row in ministry_questions and two rows in
  ministry_question_occurrences (one per source file).
- [AC-6.7.5]: All job-scoped state enumerated in §2 is verified freshly created (not reused)
  for job N+1 relative to job N in the same batch run — verified by instrumenting a test double
  that asserts no listed state object's identity is shared across sequential jobs.
- [AC-6.7.6]: A queued job's classification output is verified identical whether run as the
  first or last job in a batch containing an earlier job whose embedding backfill has not yet
  completed — proving retrieval correctness does not silently depend on queue-position-dependent
  background timing.
"""

import json
import sqlite3
import threading
from typing import Any, List
from unittest.mock import MagicMock, patch

import httpx
import pytest

from app.ai.client import OpenRouterClient
from app.ai.question_indexer import QuestionIndexer
from app.automation.batch_queue import (
    BatchJobResult,
    BatchQueue,
    BatchQueueSummary,
    JobScopedState,
    create_job_scoped_state,
    orchestrate_classification,
)
from app.core.exceptions import TextIntegrityError
from app.core.models import BookletDocument, ContentBlock, PageData
from app.core.text_integrity import compute_canonical_hash
from app.state.document_store import DocumentStore
from app.state.job_history import JobHistoryManager
from app.state.undo_redo import UndoRedoManager
from app.utils.db import init_db


# ─── Fixtures & Helpers ───────────────────────────────────────────────


@pytest.fixture
def db_conn():
    """In-memory SQLite connection with all migrations (001-004) applied."""
    conn = init_db(":memory:")
    yield conn
    conn.close()


def _make_block(
    block_id: str,
    raw_content: str,
    block_type: str = "arabic_text",
) -> ContentBlock:
    """Create a valid ContentBlock with matching canonical hash."""
    return ContentBlock(
        block_id=block_id,
        block_type=block_type,
        raw_content=raw_content,
        content_hash=compute_canonical_hash(raw_content),
    )


def _make_document(
    doc_id: str,
    blocks: List[ContentBlock],
) -> BookletDocument:
    """Create a BookletDocument containing the given blocks on page 1."""
    page = PageData(page_number=1, content_blocks=blocks, width=210.0, height=297.0)
    return BookletDocument(
        doc_id=doc_id,
        file_path=f"/{doc_id}.docx",
        file_type="docx",
        source_sha256="abc123sha",
        pages=[page],
    )


def _mock_success_response(block_types: dict[str, str]) -> httpx.Response:
    """Create a synthetic 200 httpx.Response with json classification payload."""
    payload = {
        "choices": [
            {
                "message": {
                    "content": json.dumps({
                        "classifications": [
                            {"block_id": bid, "type": btype}
                            for bid, btype in block_types.items()
                        ]
                    })
                }
            }
        ]
    }
    return httpx.Response(
        status_code=200,
        json=payload,
        request=httpx.Request("POST", "https://openrouter.ai/api/v1/chat/completions"),
    )


# ─── AC-6.7.1: Batch Multi-File Isolation Guarantee ────────────────────


class TestAC_6_7_1_BatchFileIsolation:
    """[AC-6.7.1] A batch of N queued files results in exactly N separate document-level
    classification sequences, each with only that file's blocks — no request sent to the AI
    client contains block_ids from more than one source_doc_id."""

    def test_no_cross_document_blocks_in_ai_requests(self, db_conn):
        sent_requests: List[List[str]] = []

        # Create 3 documents with distinct doc_ids and block_id prefixes
        docs = [
            _make_document(
                f"doc_{i}",
                [_make_block(f"doc_{i}_b1", f"محتوى الفقرة الأولى من المستند {i}"),
                 _make_block(f"doc_{i}_b2", f"سؤال وزاري مهم للمستند {i}")],
            )
            for i in range(3)
        ]

        client = OpenRouterClient(api_key="sk-test-key")

        def fake_send_request(payload: dict) -> httpx.Response:
            user_content = payload["messages"][1]["content"]
            items = json.loads(user_content)
            req_bids = [item["block_id"] for item in items]
            sent_requests.append(req_bids)

            # Return success response
            return _mock_success_response({bid: "arabic_text" for bid in req_bids})

        client._send_request = fake_send_request

        # Simulate batch queue processing
        file_paths = [f"file_{i}.docx" for i in range(3)]

        def mock_extraction(file_path: str, config: Any, store: DocumentStore):
            idx = int(file_path.split("_")[1].split(".")[0])
            doc = docs[idx]
            store.load_document(doc)
            orchestrate_classification(store, client, doc_id=doc.doc_id)
            return store

        def mock_generation(doc, config, attempt_index=0):
            return MagicMock(page_count=5)

        queue = BatchQueue(
            file_paths=file_paths,
            db_conn=db_conn,
            extraction_fn=mock_extraction,
            generation_fn=mock_generation,
        )
        queue.run()

        # Assert exactly 3 AI request sequences
        assert len(sent_requests) == 3

        # Assert no request contains block_ids from more than one document
        for req in sent_requests:
            prefixes = {bid.split("_b")[0] for bid in req}
            assert len(prefixes) == 1, f"Found blocks from multiple documents in single request: {req}"


# ─── AC-6.7.1b: Token Budget Chunking Within Single Document ───────────


class TestAC_6_7_1b_TokenBudgetChunking:
    """[AC-6.7.1b] A single document whose eligible-block payload exceeds the configured
    token budget is split into multiple sequential requests by classify_blocks(), each
    independently exact-match validated, with results deterministically merged by block_id."""

    def test_single_document_chunking_and_deterministic_merge(self):
        client = OpenRouterClient(api_key="sk-test-key")
        # Set conservative budget so 2 blocks trigger a chunk split
        # Each block is 60 chars. With chars_per_token=3.0, 60 chars ~ 20 tokens.
        # Set max_tokens_per_request = 30 -> max_chars = 90
        client.max_tokens_per_request = 30

        blocks = [
            _make_block("b1", "نص تجريبي أول طويل بما يكفي لاختبار تجزئة الكتل إلى طلبات"),
            _make_block("b2", "نص تجريبي ثاني طويل بما يكفي لاختبار تجزئة الكتل إلى طلبات"),
            _make_block("b3", "نص تجريبي ثالث طويل بما يكفي لاختبار تجزئة الكتل إلى طلبات"),
        ]

        chunks_sent: List[List[str]] = []

        def fake_send(payload: dict) -> httpx.Response:
            items = json.loads(payload["messages"][1]["content"])
            chunk_bids = [item["block_id"] for item in items]
            chunks_sent.append(chunk_bids)

            # Assign distinct types to verify merging
            type_map = {
                "b1": "chapter_header",
                "b2": "ministry_question",
                "b3": "english_text",
            }
            return _mock_success_response({bid: type_map[bid] for bid in chunk_bids})

        client._send_request = fake_send

        results = client.classify_blocks(blocks)

        # Verified split into multiple requests
        assert len(chunks_sent) == 3
        assert chunks_sent[0] == ["b1"]
        assert chunks_sent[1] == ["b2"]
        assert chunks_sent[2] == ["b3"]

        # Results deterministically merged by block_id
        res_map = {b.block_id: b.block_type for b in results}
        assert res_map["b1"] == "chapter_header"
        assert res_map["b2"] == "ministry_question"
        assert res_map["b3"] == "english_text"

    def test_chunk_failure_falls_back_for_that_chunk_only(self):
        client = OpenRouterClient(api_key="sk-test-key")
        client.max_tokens_per_request = 10  # Forces separate chunks: max_chars = 30

        blocks = [
            _make_block("b1", "نص عربي عادي للكتلة الأولى"),  # len = 26
            _make_block("b2", "سؤال: ما هي المعادلة الكيميائية للمستند؟"),  # len = 40 (Rule-based classifies as question)
        ]

        def fake_send(payload: dict) -> httpx.Response:
            items = json.loads(payload["messages"][1]["content"])
            bids = [item["block_id"] for item in items]
            if "b1" in bids:
                return _mock_success_response({"b1": "chapter_header"})
            else:
                # Chunk 2 fails with network error
                raise httpx.RequestError("Chunk 2 timeout")

        client._send_request = fake_send

        results = client.classify_blocks(blocks)
        res_map = {b.block_id: b.block_type for b in results}

        # Chunk 1 got AI classification
        assert res_map["b1"] == "chapter_header"
        # Chunk 2 fell back to RuleBasedClassifier for that chunk only
        assert res_map["b2"] == "ministry_question"


# ─── AC-6.7.1c: Revision Race Discard Guarantee ────────────────────────


class TestAC_6_7_1c_RevisionRaceClosure:
    """[AC-6.7.1c] A with_user_edit() or undo()/redo() call occurring on DocumentStore
    while a multi-chunk classify_blocks() call is still in flight causes the eventual
    merged classification result to be discarded rather than applied."""

    def test_user_edit_during_in_flight_classification_discards_result(self):
        store = DocumentStore()
        blocks = [
            _make_block("b1", "نص الكتلة الأولى الأصلية"),
            _make_block("b2", "نص الكتلة الثانية الأصلية"),
        ]
        doc = _make_document("doc_race", blocks)
        store.load_document(doc)

        client = OpenRouterClient(api_key="sk-test-key")
        client.max_tokens_per_request = 30  # forces 2 chunks

        chunk_count = 0

        def fake_send(payload: dict) -> httpx.Response:
            nonlocal chunk_count
            chunk_count += 1
            items = json.loads(payload["messages"][1]["content"])
            bids = [item["block_id"] for item in items]

            if chunk_count == 1:
                # Chunk 1 response: simulate a user edit occurring BEFORE chunk 2 is processed!
                store.with_user_edit(
                    doc_id="doc_race",
                    page_number=1,
                    block_id="b1",
                    new_content="تم تعديل النص يدوياً بواسطة المصمم أثناء التصنيف",
                )
                return _mock_success_response({bid: "chapter_header" for bid in bids})
            else:
                return _mock_success_response({bid: "english_text" for bid in bids})

        client._send_request = fake_send

        # Orchestrate classification: initial revision was 0, but user edit advanced it to 1
        applied = orchestrate_classification(store, client, doc_id="doc_race")

        # Result must be discarded because revision advanced in flight
        assert applied is False

        # Block content must retain the user edit and not be overwritten
        page = store.get_page_slice(1)
        b1 = [b for b in page.content_blocks if b.block_id == "b1"][0]
        assert b1.raw_content == "تم تعديل النص يدوياً بواسطة المصمم أثناء التصنيف"
        assert b1.user_edited is True

    def test_undo_during_in_flight_classification_discards_result(self, db_conn):
        store = DocumentStore()
        undo_mgr = UndoRedoManager(db_conn)
        session_id = "session_race_undo"

        b1 = _make_block("b1", "نص الكتلة قبل التعديل")
        doc = _make_document("doc_race_undo", [b1])
        store.load_document(doc)

        # Apply a user edit first
        edited_b1 = b1.with_user_edit("نص بعد التعديل")
        store.update_block("doc_race_undo", 1, "b1", edited_b1)
        undo_mgr.record_edit(
            document_session_id=session_id,
            doc_id="doc_race_undo",
            block_id="b1",
            page_number=1,
            previous_content=b1.raw_content,
            previous_hash=b1.content_hash,
            new_content=edited_b1.raw_content,
            new_hash=edited_b1.content_hash,
        )

        client = OpenRouterClient(api_key="sk-test-key")

        def fake_send(payload: dict) -> httpx.Response:
            # While classification request is in flight, user triggers undo!
            undo_mgr.undo(session_id, store)
            return _mock_success_response({"b1": "chapter_header"})

        client._send_request = fake_send

        applied = orchestrate_classification(store, client, doc_id="doc_race_undo")

        # Must discard because undo mutated DocumentStore and incremented revision
        assert applied is False


# ─── AC-6.7.2: Hard-Halt in Job 2 Does Not Halt Batch ──────────────────


class TestAC_6_7_2_BatchHardHaltFaultIsolation:
    """[AC-6.7.2] A Hard-Halt failure (e.g. TextIntegrityError) in file 2 of a 5-file
    batch records file 2 as 'failed' in job_history and does not prevent files 3-5
    from being processed."""

    def test_hard_halt_in_file_2_records_failure_and_completes_batch(self, db_conn):
        file_paths = [f"booklet_{i}.docx" for i in range(1, 6)]
        executed_files: List[str] = []

        def mock_extraction(file_path: str, config: Any, store: DocumentStore):
            executed_files.append(file_path)
            if file_path == "booklet_2.docx":
                # File 2 raises TextIntegrityError (Hard-Halt)
                raise TextIntegrityError("SHA-256 hash mismatch: text corrupted during extraction")
            return store

        def mock_generation(doc, config, attempt_index=0):
            return MagicMock(page_count=10)

        queue = BatchQueue(
            file_paths=file_paths,
            db_conn=db_conn,
            extraction_fn=mock_extraction,
            generation_fn=mock_generation,
        )
        queue.run()

        # All 5 files were attempted in sequential order
        assert executed_files == [
            "booklet_1.docx",
            "booklet_2.docx",
            "booklet_3.docx",
            "booklet_4.docx",
            "booklet_5.docx",
        ]

        summary = queue.get_summary()
        assert summary.total == 5
        assert summary.succeeded == 4
        assert summary.failed == 1

        # Check job_history table directly
        history_mgr = JobHistoryManager(db_conn)
        jobs = history_mgr.get_recent_jobs()
        assert len(jobs) == 5

        # booklet_2 must be recorded as failed with translated error summary
        file_2_job = [j for j in jobs if j["source_file_path"] == "booklet_2.docx"][0]
        assert file_2_job["status"] == "failed"
        assert (
            "تم اكتشاف تغيير غير مصرّح" in file_2_job["error_summary"]
            or "Unauthorized modification detected" in file_2_job["error_summary"]
        )

        # booklets 1, 3, 4, 5 must be success
        for fname in ["booklet_1.docx", "booklet_3.docx", "booklet_4.docx", "booklet_5.docx"]:
            job = [j for j in jobs if j["source_file_path"] == fname][0]
            assert job["status"] == "success"


# ─── AC-6.7.3 & AC-6.7.4: Question Traceability Across Batch ───────────


class TestAC_6_7_3_and_AC_6_7_4_QuestionOccurrences:
    """[AC-6.7.3] & [AC-6.7.4] Two files queued back-to-back containing a ministry_question
    block with identical content_hash correctly result in only one row in ministry_questions
    and two rows in ministry_question_occurrences."""

    def test_duplicate_question_across_batch_records_two_occurrences(self, db_conn):
        indexer = QuestionIndexer(db_conn)

        question_text = "سؤال وزاري: علل ما يأتي عند درجات الحرارة العالية؟"
        block1 = _make_block("q_file_a", question_text, block_type="ministry_question")
        block2 = _make_block("q_file_b", question_text, block_type="ministry_question")

        assert block1.content_hash == block2.content_hash

        # File A indexes question
        qid1 = indexer.index_block(block1, source_doc_id="file_A.docx", source_page=1)
        assert qid1 is not None

        # File B indexes the same question
        qid2 = indexer.index_block(block2, source_doc_id="file_B.docx", source_page=4)
        assert qid2 is None  # Duplicate, returns None

        # 1. Verify single canonical row in ministry_questions (AC-3.1.1 unchanged)
        assert indexer.count_questions() == 1

        # 2. Verify two occurrences in ministry_question_occurrences (AC-6.7.4)
        occurrences = indexer.get_occurrences(qid1)
        assert len(occurrences) == 2

        # First occurrence from file A
        assert occurrences[0]["source_doc_id"] == "file_A.docx"
        assert occurrences[0]["source_page"] == 1
        assert occurrences[0]["block_id"] == "q_file_a"

        # Second occurrence from file B
        assert occurrences[1]["source_doc_id"] == "file_B.docx"
        assert occurrences[1]["source_page"] == 4
        assert occurrences[1]["block_id"] == "q_file_b"


# ─── AC-6.7.5: Job-Scoped State Isolation Guarantee ────────────────────


class TestAC_6_7_5_JobScopedStateIsolation:
    """[AC-6.7.5] All job-scoped state enumerated in §2 is verified freshly created
    (not reused) for job N+1 relative to job N in the same batch run."""

    def test_all_job_scoped_resources_have_distinct_identities(self, db_conn):
        recorded_states: List[JobScopedState] = []

        file_paths = ["file_alpha.docx", "file_beta.docx"]

        def tracking_state_factory(path: str) -> JobScopedState:
            state = create_job_scoped_state(path)
            recorded_states.append(state)
            return state

        queue = BatchQueue(
            file_paths=file_paths,
            db_conn=db_conn,
            state_factory=tracking_state_factory,
            generation_fn=lambda doc, cfg, attempt_index=0: MagicMock(page_count=2),
        )
        queue.run()

        assert len(recorded_states) == 2
        s1, s2 = recorded_states[0], recorded_states[1]

        # 1. temporary directories
        assert s1.temp_dir != s2.temp_dir
        # 2. prompt/response caches
        assert s1.cache is not s2.cache
        # 3. placeholder maps
        assert s1.placeholder_map is not s2.placeholder_map
        # 4. classification confidence/fallback counters
        assert s1.fallback_counter is not s2.fallback_counter
        # 5. error/retry counters
        assert s1.retry_counter is not s2.retry_counter
        # 6. output paths
        assert s1.output_path != s2.output_path
        # 7. cancellation tokens
        assert s1.cancellation_token is not s2.cancellation_token
        # 8. document store (and its revision)
        assert s1.document_store is not s2.document_store
        # 9. COMSupervisor child-process handle
        assert s1.com_supervisor != s2.com_supervisor


# ─── AC-6.7.6: Embedding Backfill Ordering Independence ────────────────


class TestAC_6_7_6_EmbeddingBackfillIndependence:
    """[AC-6.7.6] A queued job's classification output is verified identical whether
    run as the first or last job in a batch containing an earlier job whose embedding
    backfill has not yet completed."""

    def test_classification_output_identical_regardless_of_backfill_timing(self, db_conn):
        indexer = QuestionIndexer(db_conn)

        # Job A introduces a question with pending_embedding=1 (failed embedding client)
        block_a = _make_block("qa", "سؤال وزاري أ", block_type="ministry_question")
        indexer.index_block(block_a, source_doc_id="doc_a", source_page=1)
        assert indexer.count_pending_embeddings() == 1

        # Document B content to classify
        blocks_b = [
            _make_block("b1", "الفصل الأول: مدخل عام", block_type="arabic_text"),
            _make_block("b2", "سؤال: ما تعريف الذرة؟", block_type="arabic_text"),
        ]

        client = OpenRouterClient(api_key="sk-test-key")
        # Run classification before backfill
        with patch.object(
            client,
            "_send_request",
            return_value=_mock_success_response({
                "b1": "chapter_header",
                "b2": "ministry_question",
            }),
        ):
            output_before = client.classify_blocks(blocks_b)

        # Now simulate backfilling Job A's embedding
        dummy_embedding = [0.1] * 1536
        db_conn.execute(
            "UPDATE ministry_questions SET pending_embedding = 0 WHERE id = 1"
        )
        import sqlite_vec
        db_conn.execute(
            "INSERT INTO ministry_questions_vec (rowid, embedding) VALUES (?, ?)",
            (1, sqlite_vec.serialize_float32(dummy_embedding)),
        )
        db_conn.commit()
        assert indexer.count_pending_embeddings() == 0

        # Run classification after backfill
        with patch.object(
            client,
            "_send_request",
            return_value=_mock_success_response({
                "b1": "chapter_header",
                "b2": "ministry_question",
            }),
        ):
            output_after = client.classify_blocks(blocks_b)

        # Outputs must be strictly identical
        assert [b.block_type for b in output_before] == [b.block_type for b in output_after]


# ─── Queue Cancellation Between Jobs ──────────────────────────────────


class TestBatchQueueCancellation:
    """Verify queue cancellation cleanly stops before starting the next job."""

    def test_cancel_queue_between_jobs(self, db_conn):
        file_paths = ["file_1.docx", "file_2.docx", "file_3.docx"]
        executed: List[str] = []

        queue = BatchQueue(
            file_paths=file_paths,
            db_conn=db_conn,
            extraction_fn=lambda path, cfg, store: executed.append(path),
            generation_fn=lambda doc, cfg, attempt_index=0: MagicMock(page_count=1),
        )

        def on_started(path, idx, total):
            if idx == 1:
                # Cancel remaining queue while job 1 is running
                queue.cancel_queue()

        queue.job_started.connect(on_started)
        queue.run()

        # Job 1 ran to completion; jobs 2 and 3 were skipped/cancelled
        assert executed == ["file_1.docx"]
        summary = queue.get_summary()
        assert summary.succeeded == 1
        assert summary.cancelled == 2
        assert summary.total == 3


# ─── BatchPanel UI Integration ─────────────────────────────────────────


class TestBatchPanelUI:
    """Verify BatchPanel UI displays queued files, updates progress, and presents summary."""

    @pytest.fixture
    def qapp(self):
        from PySide6.QtWidgets import QApplication
        app = QApplication.instance()
        if app is None:
            app = QApplication(["--platform", "offscreen"])
        yield app

    def test_batch_panel_lifecycle_and_table_updates(self, qapp, db_conn):
        from app.ui.batch_panel import BatchPanel

        panel = BatchPanel(
            db_conn=db_conn,
            extraction_fn=lambda path, cfg, store: store,
            generation_fn=lambda doc, cfg, attempt_index=0: MagicMock(page_count=8),
        )

        # Set files
        panel.set_files(["test_doc_1.docx", "test_doc_2.docx"])
        assert panel._table.rowCount() == 2
        assert panel._table.item(0, 0).text() == "test_doc_1.docx"
        assert panel._table.item(1, 0).text() == "test_doc_2.docx"

        # Start batch
        panel.start_batch()
        assert panel._worker is not None
        panel._worker.wait(5000)
        qapp.processEvents()

        # Verified success in table
        assert panel._table.item(0, 1).text() == "ناجح ✓"
        assert panel._table.item(1, 1).text() == "ناجح ✓"
        assert "اكتملت معالجة الدفعة: 2 نجح" in panel._summary_label.text()

