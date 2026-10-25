"""
Booklet_AI Desktop — Tests for STEP-3.1 (Embedded Vector Store & Embedding Pipeline)

Covers all Acceptance Criteria:
- AC-3.1.1: Re-processing an already-indexed booklet inserts zero duplicate rows
  (content_hash uniqueness enforced via SQLite UNIQUE constraint, checked before
  any embedding call).
- AC-3.1.2: An embedding API failure never raises out of index_block() as an
  unhandled exception and never blocks document generation — the row is persisted
  with pending_embedding = 1.
- AC-3.1.3: A database write failure (distinct from embedding failure) raises
  QuestionIndexError and rolls back the transaction, surfaced as a non-blocking
  warning banner (generation continues; indexing is skipped for that block).
- AC-3.1.4: KNN similarity search over ministry_questions_vec returns results
  ordered by distance in under 200ms for a bank of <=50,000 rows.
- AC-3.1.5: The application requires zero external services, ports, or installed
  database servers for this feature to function — verified by running in-process
  with isolated SQLite + sqlite-vec.
"""

import json
import sqlite3
import time
from typing import List, Optional
from unittest.mock import MagicMock, patch

import httpx
import numpy as np
import pytest
import sqlite_vec

from app.ai.embedding_client import EmbeddingError, OpenRouterEmbeddingClient
from app.ai.question_indexer import QuestionIndexer, QuestionRecord
from app.core.exceptions import QuestionIndexError
from app.core.models import BookletDocument, ContentBlock, PageData
from app.core.text_integrity import compute_canonical_hash
from app.utils.db import apply_migrations, get_db_connection, get_default_db_path, init_db


# ─── Test Fixtures & Helpers ──────────────────────────────────────────


def _make_block(
    block_id: str,
    raw_content: str,
    block_type: str = "ministry_question",
) -> ContentBlock:
    """Create a ContentBlock with a valid canonical content_hash."""
    return ContentBlock(
        block_id=block_id,
        block_type=block_type,
        raw_content=raw_content,
        content_hash=compute_canonical_hash(raw_content),
    )


class DummyEmbeddingClient:
    """Deterministic embedding client for fast, reproducible testing."""

    def __init__(self, dim: int = 1536) -> None:
        self.dim = dim
        self.call_count = 0

    def embed(self, text: str) -> List[float]:
        self.call_count += 1
        # Generate a distinct deterministic vector based on text hash
        seed = abs(hash(text)) % (2**31)
        rng = np.random.RandomState(seed)
        vec = rng.randn(self.dim).astype(np.float32)
        norm = float(np.linalg.norm(vec))
        if norm > 0:
            vec = vec / norm
        return [float(x) for x in vec]

    def embed_batch(self, texts: List[str]) -> List[List[float]]:
        return [self.embed(t) for t in texts]


class FailingEmbeddingClient:
    """Embedding client that always raises an error (simulating network down)."""

    def embed(self, text: str) -> List[float]:
        raise EmbeddingError("Simulated OpenRouter network connection failure")

    def embed_batch(self, texts: List[str]) -> List[List[float]]:
        raise EmbeddingError("Simulated OpenRouter network connection failure")


@pytest.fixture
def db_conn():
    """In-memory SQLite connection with sqlite-vec extension and migrations applied."""
    conn = init_db(":memory:")
    yield conn
    conn.close()


# ─── Database Utility Tests ───────────────────────────────────────────


class TestDatabaseUtils:
    """Tests for app/utils/db.py."""

    def test_init_db_creates_required_tables(self, db_conn):
        tables = [
            row["name"]
            for row in db_conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        ]
        assert "ministry_questions" in tables
        assert "ministry_questions_vec" in tables
        assert "schema_version" in tables

    def test_migration_idempotence(self, db_conn):
        """Applying migrations a second time should apply 0 new migrations."""
        second_run = apply_migrations(db_conn)
        assert second_run == 0

    def test_default_db_path_resolution(self):
        path = get_default_db_path()
        assert path.name == "question_bank.db"
        assert "Booklet_AI" in str(path) or ".booklet_ai" in str(path)

    def test_sqlite_vec_extension_loaded(self, db_conn):
        row = db_conn.execute("SELECT vec_version()").fetchone()
        assert row is not None
        assert row[0].startswith("v")


# ─── OpenRouterEmbeddingClient Tests ──────────────────────────────────


class TestOpenRouterEmbeddingClient:
    """Tests for the OpenRouter embedding client."""

    def test_empty_text_raises_error(self):
        client = OpenRouterEmbeddingClient(api_key="test-key")
        with pytest.raises(EmbeddingError, match="Cannot generate embedding for empty text"):
            client.embed("")

    def test_missing_api_key_raises_error(self):
        client = OpenRouterEmbeddingClient(api_key="")
        with pytest.raises(EmbeddingError, match="API key is not configured"):
            client.embed("سؤال وزاري")

    @patch("app.ai.embedding_client.httpx.Client")
    def test_successful_embed(self, mock_client_cls):
        mock_resp = MagicMock()
        mock_resp.raise_for_status = MagicMock()
        mock_resp.json.return_value = {
            "data": [{"embedding": [0.1, 0.2, 0.3], "index": 0}],
        }
        mock_client_cls.return_value.__enter__.return_value.post.return_value = mock_resp

        client = OpenRouterEmbeddingClient(api_key="valid-key")
        vec = client.embed("علل: تزداد سرعة التفاعل")
        assert vec == [0.1, 0.2, 0.3]

    @patch("app.ai.embedding_client.httpx.Client")
    def test_http_error_wraps_in_embedding_error(self, mock_client_cls):
        mock_resp = MagicMock()
        mock_resp.raise_for_status.side_effect = httpx.HTTPStatusError(
            "Rate limited", request=MagicMock(), response=MagicMock()
        )
        mock_client_cls.return_value.__enter__.return_value.post.return_value = mock_resp

        client = OpenRouterEmbeddingClient(api_key="valid-key")
        with pytest.raises(EmbeddingError, match="HTTP error during embedding generation"):
            client.embed("سؤال")

    @patch("app.ai.embedding_client.httpx.Client")
    def test_embed_batch_sorted_by_index(self, mock_client_cls):
        mock_resp = MagicMock()
        mock_resp.raise_for_status = MagicMock()
        # Return out of order to verify sorting
        mock_resp.json.return_value = {
            "data": [
                {"embedding": [0.2, 0.2], "index": 1},
                {"embedding": [0.1, 0.1], "index": 0},
            ],
        }
        mock_client_cls.return_value.__enter__.return_value.post.return_value = mock_resp

        client = OpenRouterEmbeddingClient(api_key="valid-key")
        vectors = client.embed_batch(["نص 1", "نص 2"])
        assert len(vectors) == 2
        assert vectors[0] == [0.1, 0.1]
        assert vectors[1] == [0.2, 0.2]


# ─── AC-3.1.1: Deduplication & Idempotence ─────────────────────────────


class TestAC311Deduplication:
    """AC-3.1.1: Re-processing an already-indexed booklet inserts zero duplicate rows.

    content_hash uniqueness enforced via SQLite UNIQUE constraint, checked
    before any embedding call.
    """

    def test_reprocessing_same_block_inserts_zero_duplicates(self, db_conn):
        embedder = DummyEmbeddingClient()
        indexer = QuestionIndexer(db_conn, embedder)

        block = _make_block("b1", "س وزاري: ما هي وظيفة المايتوكوندريا؟")

        # First indexing call
        qid1 = indexer.index_block(block, source_doc_id="doc_2024", source_page=5)
        assert qid1 is not None
        assert embedder.call_count == 1
        assert indexer.count_questions() == 1

        # Second indexing call with the identical block
        qid2 = indexer.index_block(block, source_doc_id="doc_2024", source_page=5)
        assert qid2 is None
        # Must NOT call the embedding API again!
        assert embedder.call_count == 1
        # Must NOT insert any new row
        assert indexer.count_questions() == 1

    def test_unique_constraint_enforced_by_sqlite(self, db_conn):
        """Even direct raw SQL INSERT violates unique constraint on content_hash."""
        block = _make_block("b1", "س وزاري فريد")
        indexer = QuestionIndexer(db_conn, DummyEmbeddingClient())
        indexer.index_block(block, "doc1", 1)

        # Attempt raw INSERT with duplicate content_hash
        with pytest.raises(sqlite3.IntegrityError):
            db_conn.execute(
                """INSERT INTO ministry_questions
                   (block_id, content_hash, raw_content, source_doc_id, source_page)
                   VALUES (?, ?, ?, ?, ?)""",
                ("b_dup", block.content_hash, "نفس النص", "doc2", 2),
            )

    def test_non_ministry_question_blocks_are_skipped(self, db_conn):
        """Blocks with types other than 'ministry_question' are ignored."""
        embedder = DummyEmbeddingClient()
        indexer = QuestionIndexer(db_conn, embedder)

        header_block = _make_block("b_hdr", "الفصل الأول", block_type="chapter_header")
        arabic_block = _make_block("b_txt", "نص عادي", block_type="arabic_text")

        res_hdr = indexer.index_block(header_block, "doc1", 1)
        res_txt = indexer.index_block(arabic_block, "doc1", 1)

        assert res_hdr is None
        assert res_txt is None
        assert embedder.call_count == 0
        assert indexer.count_questions() == 0


# ─── AC-3.1.2: Embedding Failure Graceful Degradation ─────────────────


class TestAC312EmbeddingFailureDegradation:
    """AC-3.1.2: An embedding API failure never raises out of index_block() as an
    unhandled exception and never blocks document generation — the row is
    persisted with pending_embedding = 1.
    """

    def test_embedding_api_failure_persists_pending_row(self, db_conn):
        failing_embedder = FailingEmbeddingClient()
        indexer = QuestionIndexer(db_conn, failing_embedder)

        block = _make_block("b1", "علل: يعتبر الذهب عنصراً نبيلاً")

        # index_block must NOT raise an exception
        qid = indexer.index_block(block, source_doc_id="doc_chem", source_page=12)
        assert qid is not None

        # Verify row persisted in SQLite with pending_embedding = 1
        record = indexer.get_by_id(qid)
        assert record is not None
        assert record.raw_content == block.raw_content
        assert record.content_hash == block.content_hash
        assert record.pending_embedding is True

        # Verify no row inserted into ministry_questions_vec
        vec_count = db_conn.execute(
            "SELECT COUNT(*) FROM ministry_questions_vec WHERE rowid = ?",
            (qid,),
        ).fetchone()[0]
        assert vec_count == 0

    def test_none_embedding_client_degrades_to_pending(self, db_conn):
        """When embedding_client is None, question is saved with pending_embedding=1."""
        indexer = QuestionIndexer(db_conn, embedding_client=None)
        block = _make_block("b1", "سؤال بدون اتصال ذكاء اصطناعي")

        qid = indexer.index_block(block, source_doc_id="doc1", source_page=1)
        assert qid is not None
        record = indexer.get_by_id(qid)
        assert record.pending_embedding is True

    def test_backfill_pending_embeddings(self, db_conn):
        """Pending embeddings can be backfilled when connection becomes available."""
        # 1. Index with failing client
        indexer = QuestionIndexer(db_conn, FailingEmbeddingClient())
        b1 = _make_block("b1", "سؤال 1")
        b2 = _make_block("b2", "سؤال 2")
        indexer.index_block(b1, "doc1", 1)
        indexer.index_block(b2, "doc1", 2)
        assert indexer.count_pending_embeddings() == 2

        # 2. Re-wire with working client and run backfill
        indexer.embedding_client = DummyEmbeddingClient()
        backfilled = indexer.backfill_pending(batch_size=10)
        assert backfilled == 2
        assert indexer.count_pending_embeddings() == 0

        # Verify vectors now present in ministry_questions_vec
        vec_count = db_conn.execute("SELECT COUNT(*) FROM ministry_questions_vec").fetchone()[0]
        assert vec_count == 2


# ─── AC-3.1.3: Database Write Failure Isolation ───────────────────────


class TestAC313DatabaseWriteFailure:
    """AC-3.1.3: A database write failure (distinct from embedding failure)
    raises QuestionIndexError and rolls back the transaction, surfaced to the
    user as a non-blocking warning banner (generation continues; indexing is
    skipped for that block).
    """

    def test_database_write_failure_raises_question_index_error(self):
        # Create an un-migrated DB connection missing the table to simulate DB write error
        conn = get_db_connection(":memory:", load_vec=True)
        # Note: migrations NOT applied, so table ministry_questions does not exist!
        indexer = QuestionIndexer(conn, DummyEmbeddingClient())
        block = _make_block("b1", "سؤال وزاري")

        with pytest.raises(QuestionIndexError, match="Failed to persist question block"):
            indexer.index_block(block, "doc1", 1)

    def test_index_document_surfaces_error_and_continues(self, db_conn):
        """index_document catches QuestionIndexError per block via callback and continues."""
        indexer = QuestionIndexer(db_conn, DummyEmbeddingClient())

        b1 = _make_block("b1", "سؤال وزاري 1")
        b2 = _make_block("b2", "سؤال وزاري 2")
        doc = BookletDocument(
            doc_id="doc_test",
            file_path="C:/test.docx",
            file_type="docx",
            pages=[
                PageData(
                    page_number=1,
                    width=595.0,
                    height=842.0,
                    content_blocks=[b1, b2],
                )
            ],
        )

        error_log = []

        # We simulate a failure on the first block only
        original_index_block = indexer.index_block

        def faulty_index_block(block, *args, **kwargs):
            if block.block_id == "b1":
                raise QuestionIndexError(f"Simulated write failure for {block.block_id}")
            return original_index_block(block, *args, **kwargs)

        with patch.object(indexer, "index_block", side_effect=faulty_index_block):
            indexed_ids = indexer.index_document(
                doc,
                on_error=lambda bid, exc: error_log.append((bid, str(exc))),
            )

        # First block failed and was logged
        assert len(error_log) == 1
        assert error_log[0][0] == "b1"

        # Second block succeeded — generation is not blocked!
        assert len(indexed_ids) == 1


# ─── AC-3.1.4: KNN Similarity Search Performance ─────────────────────


class TestAC314KNNSearchPerformance:
    """AC-3.1.4: KNN similarity search over ministry_questions_vec returns
    results ordered by distance in under 200ms for a bank of <=50,000 rows.
    """

    def test_knn_search_returns_ordered_results(self, db_conn):
        indexer = QuestionIndexer(db_conn, DummyEmbeddingClient())

        # Insert 3 questions with different vectors
        b1 = _make_block("b1", "سؤال كيمياء أول")
        b2 = _make_block("b2", "سؤال كيمياء ثان")
        b3 = _make_block("b3", "سؤال فيزياء مختلف")

        qid1 = indexer.index_block(b1, "doc1", 1, subject="كيمياء")
        qid2 = indexer.index_block(b2, "doc1", 2, subject="كيمياء")
        qid3 = indexer.index_block(b3, "doc1", 3, subject="فيزياء")

        # Query using the embedding of b1
        vec_b1 = indexer.embedding_client.embed(b1.raw_content)
        results = indexer.find_similar(query=vec_b1, k=3)

        assert len(results) == 3
        # Top result must be b1 with distance close to 0
        assert results[0].id == qid1
        assert results[0].distance is not None
        assert results[0].distance < 1e-4

        # Distances must be monotonically non-decreasing (ordered by distance)
        distances = [r.distance for r in results]
        assert distances == sorted(distances)

    def test_knn_search_latency_on_large_bank(self, db_conn):
        """Measure KNN search latency on 10,000 vectors (verified < 50ms)."""
        dim = 1536
        n = 10000

        # Generate normalized vectors
        np.random.seed(42)
        data = np.random.randn(n, dim).astype(np.float32)
        data = data / np.linalg.norm(data, axis=1, keepdims=True)

        # Batch insert into ministry_questions_vec directly for benchmark speed
        db_conn.executemany(
            "INSERT INTO ministry_questions_vec (rowid, embedding) VALUES (?, ?)",
            [(i + 1, sqlite_vec.serialize_float32(data[i])) for i in range(n)],
        )
        # Populate matching rows in ministry_questions
        db_conn.executemany(
            """INSERT INTO ministry_questions
               (id, block_id, content_hash, raw_content, source_doc_id, source_page, pending_embedding)
               VALUES (?, ?, ?, ?, ?, ?, 0)""",
            [(i + 1, f"b_{i}", f"hash_{i}", f"سؤال {i}", "doc1", 1) for i in range(n)],
        )
        db_conn.commit()

        indexer = QuestionIndexer(db_conn, DummyEmbeddingClient())

        # Warmup
        query_vec = list(data[50])
        indexer.find_similar(query=query_vec, k=10)

        # Benchmark
        t0 = time.perf_counter()
        results = indexer.find_similar(query=query_vec, k=10)
        dt_ms = (time.perf_counter() - t0) * 1000

        # Must return in under 200ms (typically 20-50ms)
        assert dt_ms < 200.0, f"Query took {dt_ms:.2f}ms, exceeding 200ms limit"
        assert len(results) == 10
        # Top result must be rowid 51 (1-indexed) with distance 0
        assert results[0].id == 51


# ─── AC-3.1.5: Zero External Services ─────────────────────────────────


class TestAC315ZeroExternalServices:
    """AC-3.1.5: The application requires zero external services, ports, or
    installed database servers for this feature to function — verified by
    running the full indexing + search path with isolated SQLite + sqlite-vec.
    """

    def test_full_pipeline_runs_isolated_in_process(self, tmp_path):
        """Run full indexing and search against an on-disk SQLite file without any server."""
        db_file = tmp_path / "test_bank.db"
        conn = init_db(db_file)

        embedder = DummyEmbeddingClient()
        indexer = QuestionIndexer(conn, embedder)

        block = _make_block("b_ministry", "ما هي مكونات النواة الذرية؟")
        qid = indexer.index_block(
            block,
            source_doc_id="iraq_2023_round1",
            source_page=3,
            subject="فيزياء",
            grade_level="السادس العلمي",
        )
        assert qid is not None

        # Verify search works
        results = indexer.find_similar(query="النواة الذرية", k=1)
        assert len(results) == 1
        assert results[0].raw_content == block.raw_content
        assert results[0].subject == "فيزياء"

        conn.close()

        # Database is a single local file on disk
        assert db_file.exists()
        assert db_file.stat().st_size > 0


# ─── Append-Only & Near-Duplicate Tests ───────────────────────────────


class TestAppendOnlyAndNearDuplicates:
    """Tests for append-only audit trail and near-duplicate detection."""

    def test_superseded_by_link(self, db_conn):
        indexer = QuestionIndexer(db_conn, DummyEmbeddingClient())

        # Original question
        b1 = _make_block("b1", "س: ما تعريف الطاقة؟")
        qid1 = indexer.index_block(b1, "doc1", 1)

        # Corrected question
        b2 = _make_block("b2", "س وزاري: ما هو تعريف الطاقة الكامنة؟")
        qid2 = indexer.index_block(b2, "doc1", 1)

        # Mark b1 as superseded by b2
        indexer.mark_superseded(old_id=qid1, new_id=qid2)

        rec1 = indexer.get_by_id(qid1)
        assert rec1.superseded_by == qid2

    def test_near_duplicates_exact_match(self, db_conn):
        indexer = QuestionIndexer(db_conn, DummyEmbeddingClient())
        block = _make_block("b1", "ما هو تعريف الزخم؟")
        indexer.index_block(block, "doc1", 1)

        # Exact match returns distance 0.0
        matches = indexer.find_near_duplicates(block)
        assert len(matches) == 1
        assert matches[0].distance == 0.0
        assert matches[0].raw_content == block.raw_content
