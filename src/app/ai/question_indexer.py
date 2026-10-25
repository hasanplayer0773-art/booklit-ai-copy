"""
Booklet_AI Desktop — Question Indexer & Vector Store Pipeline (STEP-3.1)

Persists ministry_question blocks to the local sqlite-vec-backed store
(no server process — a single .db file alongside the app's other local data).

Architecture & Guarantees:
- Zero-Mutation: Source text and canonical hashes are never altered.
- Append-Only: Corrections link via superseded_by, never in-place mutation.
- Duplicate Detection: Content-hash exact match short-circuits before embedding API call.
- Graceful Degradation: Embedding API failures persist with pending_embedding=1 and do
  not block document generation (AC-3.1.2).
- Fail-Fast Isolation: Database write failures raise QuestionIndexError and rollback (AC-3.1.3).
- Fast KNN Search: Sub-200ms vector similarity over sqlite-vec (AC-3.1.4).
- Zero External Services: Pure in-process SQLite + sqlite-vec (AC-3.1.5).
"""

import logging
import sqlite3
from typing import Any, Callable, List, Optional, Union

import sqlite_vec
from pydantic import BaseModel, ConfigDict

from app.core.exceptions import QuestionIndexError
from app.core.models import BookletDocument, ContentBlock

logger = logging.getLogger("booklet_ai.ai.indexer")


class QuestionRecord(BaseModel):
    """Immutable representation of a persisted ministry question record.

    Follows Pydantic v2 with frozen=True (.continuerules).
    """

    model_config = ConfigDict(frozen=True)

    id: int
    block_id: str
    content_hash: str
    raw_content: str
    source_doc_id: str
    source_page: Optional[int] = None
    subject: Optional[str] = None
    grade_level: Optional[str] = None
    pending_embedding: bool = False
    superseded_by: Optional[int] = None
    created_at: Optional[str] = None
    distance: Optional[float] = None


class QuestionIndexer:
    """Persists ministry_question blocks to the local sqlite-vec-backed store.

    Embedding failures degrade to pending_embedding=True; database
    write failures raise QuestionIndexError (distinct from the
    enrichment-layer graceful degradation used for embedding itself).
    """

    def __init__(self, db_connection: sqlite3.Connection, embedding_client: Any = None) -> None:
        """Initialize QuestionIndexer.

        Args:
            db_connection: sqlite3.Connection with sqlite-vec extension loaded.
            embedding_client: Client implementing embed(text: str) -> List[float].
        """
        self.db = db_connection
        self.embedding_client = embedding_client

    def index_block(
        self,
        block: ContentBlock,
        source_doc_id: str,
        source_page: int,
        subject: Optional[str] = None,
        grade_level: Optional[str] = None,
    ) -> Optional[int]:
        """Index a single ContentBlock if it is classified as a ministry_question.

        Enforces:
        - Only ministry_question blocks are indexed (other block types ignored).
        - Exact duplicate check via canonical content_hash (short-circuits before API call).
        - Graceful degradation: embedding API failure saves row with pending_embedding=1.
        - Database write failure raises QuestionIndexError and rolls back.

        Args:
            block: The ContentBlock to index.
            source_doc_id: Document ID where this block originated.
            source_page: 1-based page number where this block was found.
            subject: Optional academic subject (e.g. 'الكيمياء').
            grade_level: Optional academic grade level (e.g. 'السادس العلمي').

        Returns:
            The integer database row ID of the inserted question, or None if skipped/duplicate.

        Raises:
            QuestionIndexError: If an unrecoverable database write failure occurs.
        """
        if block.block_type != "ministry_question":
            return None

        # AC-3.1.1: Exact canonical content_hash uniqueness check before any embedding call
        try:
            existing = self.db.execute(
                "SELECT id FROM ministry_questions WHERE content_hash = ?",
                (block.content_hash,),
            ).fetchone()
        except Exception as exc:
            raise QuestionIndexError(
                f"Failed to persist question block {block.block_id}: {exc}"
            ) from exc

        if existing is not None:
            logger.debug(
                "Block %s (hash: %s) already indexed as id=%s — recording occurrence.",
                block.block_id,
                block.content_hash,
                existing[0],
            )
            # AC-6.7.4: Record occurrence without inserting duplicate question row
            try:
                self.db.execute(
                    """INSERT OR IGNORE INTO ministry_question_occurrences
                       (question_id, source_doc_id, source_page, block_id, recorded_at)
                       VALUES (?, ?, ?, ?, datetime('now'))""",
                    (existing[0], source_doc_id, source_page, block.block_id),
                )
                self.db.commit()
            except Exception as exc:
                self.db.rollback()
                raise QuestionIndexError(
                    f"Failed to record occurrence for question block {block.block_id}: {exc}"
                ) from exc
            return None

        # Attempt embedding generation (AC-3.1.2: Embedding failures degrade gracefully)
        embedding: Optional[List[float]] = None
        pending = True
        if self.embedding_client is not None:
            try:
                embedding = self.embedding_client.embed(block.raw_content)
                pending = False
            except Exception as exc:
                logger.warning(
                    "Embedding generation failed for block %s: %s. Persisting with pending_embedding=1.",
                    block.block_id,
                    exc,
                )
                pending = True

        # Persist to database within a transaction (AC-3.1.3: DB failures raise QuestionIndexError)
        try:
            cursor = self.db.execute(
                """INSERT INTO ministry_questions
                   (block_id, content_hash, raw_content, source_doc_id, source_page,
                    pending_embedding, subject, grade_level)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    block.block_id,
                    block.content_hash,
                    block.raw_content,
                    source_doc_id,
                    source_page,
                    1 if pending else 0,
                    subject,
                    grade_level,
                ),
            )
            question_id = cursor.lastrowid
            if embedding is not None and question_id is not None:
                serialized_vec = sqlite_vec.serialize_float32(embedding)
                self.db.execute(
                    "INSERT INTO ministry_questions_vec (rowid, embedding) VALUES (?, ?)",
                    (question_id, serialized_vec),
                )
            if question_id is not None:
                self.db.execute(
                    """INSERT OR IGNORE INTO ministry_question_occurrences
                       (question_id, source_doc_id, source_page, block_id, recorded_at)
                       VALUES (?, ?, ?, ?, datetime('now'))""",
                    (question_id, source_doc_id, source_page, block.block_id),
                )
            self.db.commit()
            return question_id
        except Exception as exc:
            self.db.rollback()
            raise QuestionIndexError(
                f"Failed to persist question block {block.block_id}: {exc}"
            ) from exc

    def index_document(
        self,
        doc: BookletDocument,
        subject: Optional[str] = None,
        grade_level: Optional[str] = None,
        on_error: Optional[Callable[[str, Exception], None]] = None,
    ) -> List[int]:
        """Index all ministry_question blocks from a BookletDocument.

        Catches QuestionIndexError per block and surfaces it via the on_error
        callback, ensuring document generation is never blocked (AC-3.1.3).

        Args:
            doc: BookletDocument to extract and index ministry questions from.
            subject: Optional academic subject.
            grade_level: Optional academic grade level.
            on_error: Optional callback(block_id, exception) for non-blocking error display.

        Returns:
            List of newly inserted question row IDs.
        """
        indexed_ids: List[int] = []
        for page in doc.pages:
            for block in page.content_blocks:
                if block.block_type == "ministry_question":
                    try:
                        qid = self.index_block(
                            block=block,
                            source_doc_id=doc.doc_id,
                            source_page=page.page_number,
                            subject=subject,
                            grade_level=grade_level,
                        )
                        if qid is not None:
                            indexed_ids.append(qid)
                    except QuestionIndexError as exc:
                        logger.error(
                            "Database write failed for question block %s: %s",
                            block.block_id,
                            exc,
                        )
                        if on_error:
                            on_error(block.block_id, exc)

        return indexed_ids

    def find_similar(
        self,
        query: Union[str, List[float]],
        k: int = 5,
        threshold: Optional[float] = None,
    ) -> List[QuestionRecord]:
        """KNN similarity search over indexed ministry questions (AC-3.1.4).

        Args:
            query: Either a query text string to embed, or a pre-computed vector.
            k: Maximum number of nearest neighbors to retrieve.
            threshold: Optional maximum distance cutoff. Results with distance > threshold
                are excluded.

        Returns:
            List of QuestionRecord instances ordered by ascending distance.
        """
        if isinstance(query, str):
            if self.embedding_client is None:
                raise ValueError("Embedding client required when querying by text string.")
            query_vec = self.embedding_client.embed(query)
        else:
            query_vec = query

        serialized_query = sqlite_vec.serialize_float32(query_vec)

        sql = """
            SELECT
                q.id,
                q.block_id,
                q.content_hash,
                q.raw_content,
                q.source_doc_id,
                q.source_page,
                q.subject,
                q.grade_level,
                q.pending_embedding,
                q.superseded_by,
                q.created_at,
                v.distance
            FROM ministry_questions_vec v
            JOIN ministry_questions q ON q.id = v.rowid
            WHERE v.embedding MATCH ?
              AND v.k = ?
            ORDER BY v.distance ASC
        """
        rows = self.db.execute(sql, (serialized_query, k)).fetchall()

        results: List[QuestionRecord] = []
        for r in rows:
            dist = float(r["distance"])
            if threshold is not None and dist > threshold:
                continue
            record = QuestionRecord(
                id=int(r["id"]),
                block_id=str(r["block_id"]),
                content_hash=str(r["content_hash"]),
                raw_content=str(r["raw_content"]),
                source_doc_id=str(r["source_doc_id"]),
                source_page=int(r["source_page"]) if r["source_page"] is not None else None,
                subject=r["subject"],
                grade_level=r["grade_level"],
                pending_embedding=bool(r["pending_embedding"]),
                superseded_by=int(r["superseded_by"]) if r["superseded_by"] is not None else None,
                created_at=str(r["created_at"]) if r["created_at"] is not None else None,
                distance=dist,
            )
            results.append(record)

        return results

    def find_near_duplicates(
        self,
        block: ContentBlock,
        threshold: float = 0.25,
        k: int = 5,
    ) -> List[QuestionRecord]:
        """Detect potential near-duplicate questions across years/exams.

        Checks canonical content_hash first; if not found, runs KNN embedding
        similarity search with distance threshold for human review.

        Args:
            block: ContentBlock to check for duplicates.
            threshold: Distance threshold for near-duplicate surfacing (default: 0.25).
            k: Maximum number of candidates to return.

        Returns:
            List of candidate QuestionRecord matches ordered by distance.
        """
        # Exact match check first
        exact = self.get_by_hash(block.content_hash)
        if exact is not None:
            return [exact.model_copy(update={"distance": 0.0})]

        # Near-duplicate KNN search
        if self.embedding_client is None:
            return []

        try:
            return self.find_similar(query=block.raw_content, k=k, threshold=threshold)
        except Exception as exc:
            logger.warning("Near-duplicate search failed for block %s: %s", block.block_id, exc)
            return []

    def backfill_pending(self, batch_size: int = 50) -> int:
        """Backfill embeddings for rows previously marked with pending_embedding=1.

        Args:
            batch_size: Maximum number of rows to process in this run.

        Returns:
            Number of question embeddings successfully backfilled.
        """
        if self.embedding_client is None:
            logger.warning("Cannot backfill embeddings: no embedding client configured.")
            return 0

        cursor = self.db.execute(
            """SELECT id, raw_content FROM ministry_questions
               WHERE pending_embedding = 1
               ORDER BY id ASC
               LIMIT ?""",
            (batch_size,),
        )
        rows = cursor.fetchall()
        if not rows:
            return 0

        backfilled_count = 0
        for row in rows:
            qid = row["id"]
            raw_text = row["raw_content"]
            try:
                embedding = self.embedding_client.embed(raw_text)
                serialized = sqlite_vec.serialize_float32(embedding)
                self.db.execute(
                    "INSERT INTO ministry_questions_vec (rowid, embedding) VALUES (?, ?)",
                    (qid, serialized),
                )
                self.db.execute(
                    "UPDATE ministry_questions SET pending_embedding = 0 WHERE id = ?",
                    (qid,),
                )
                self.db.commit()
                backfilled_count += 1
            except Exception as exc:
                self.db.rollback()
                logger.warning("Failed to backfill embedding for question id=%d: %s", qid, exc)

        return backfilled_count

    def mark_superseded(self, old_id: int, new_id: int) -> None:
        """Link an older question row to the new row that superseded it.

        Supports the append-only rule: corrections create a new row with
        superseded_by link, preserving the original record unchanged.

        Args:
            old_id: Database ID of the obsolete record.
            new_id: Database ID of the new/corrected record.
        """
        self.db.execute(
            "UPDATE ministry_questions SET superseded_by = ? WHERE id = ?",
            (new_id, old_id),
        )
        self.db.commit()

    def get_by_id(self, question_id: int) -> Optional[QuestionRecord]:
        """Fetch a QuestionRecord by its database ID."""
        row = self.db.execute(
            "SELECT * FROM ministry_questions WHERE id = ?",
            (question_id,),
        ).fetchone()
        if row is None:
            return None
        return self._row_to_record(row)

    def get_by_hash(self, content_hash: str) -> Optional[QuestionRecord]:
        """Fetch a QuestionRecord by its canonical content hash."""
        row = self.db.execute(
            "SELECT * FROM ministry_questions WHERE content_hash = ?",
            (content_hash,),
        ).fetchone()
        if row is None:
            return None
        return self._row_to_record(row)

    def count_questions(self) -> int:
        """Return the total count of indexed ministry questions."""
        cursor = self.db.execute("SELECT COUNT(*) FROM ministry_questions")
        return int(cursor.fetchone()[0])

    def count_pending_embeddings(self) -> int:
        """Return the count of ministry questions awaiting vector backfill."""
        cursor = self.db.execute(
            "SELECT COUNT(*) FROM ministry_questions WHERE pending_embedding = 1"
        )
        return int(cursor.fetchone()[0])

    def get_occurrences(self, question_id: int) -> List[sqlite3.Row]:
        """Fetch all recorded occurrences for a given ministry question ID (AC-6.7.4)."""
        return self.db.execute(
            """SELECT * FROM ministry_question_occurrences
               WHERE question_id = ?
               ORDER BY id ASC""",
            (question_id,),
        ).fetchall()

    @staticmethod
    def _row_to_record(row: Any, distance: Optional[float] = None) -> QuestionRecord:
        """Convert a SQLite row to an immutable QuestionRecord."""
        return QuestionRecord(
            id=int(row["id"]),
            block_id=str(row["block_id"]),
            content_hash=str(row["content_hash"]),
            raw_content=str(row["raw_content"]),
            source_doc_id=str(row["source_doc_id"]),
            source_page=int(row["source_page"]) if row["source_page"] is not None else None,
            subject=row["subject"],
            grade_level=row["grade_level"],
            pending_embedding=bool(row["pending_embedding"]),
            superseded_by=int(row["superseded_by"]) if row["superseded_by"] is not None else None,
            created_at=str(row["created_at"]) if row["created_at"] is not None else None,
            distance=distance,
        )
