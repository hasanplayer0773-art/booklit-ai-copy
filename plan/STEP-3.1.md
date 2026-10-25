<!-- من الخطة الرئيسية Booklet_AI Desktop Master Plan -->

### [STEP-3.1] Embedded Vector Store & Embedding Pipeline

#### 1. Components & Tooling

| Component | Tool | Rationale |
|---|---|---|
| Database | SQLite + `sqlite-vec` extension | **Correction to an earlier draft**, which specified PostgreSQL + `pgvector`. That choice is a deployment mismatch for this project: the app is distributed as a standalone `.exe` to print shops with no IT staff — requiring them to install, configure, and run a PostgreSQL *server* (plus manage its Windows service, firewall exposure, and the `pgvector` extension) is impractical, and bundling a full PostgreSQL server inside the PyInstaller package would add 500MB+ and Windows-service permission complications. `sqlite-vec` is a single-file, zero-service, zero-configuration embedded extension (tens of KB, not hundreds of MB) that ships alongside the existing SQLite standard library — no separate server process, no ports, no service to install. |
| Embedding Model | OpenRouter-hosted embedding endpoint (configurable, default `text-embedding-3-small`-class) | Consistent with STEP-1.4's OpenRouter client pattern; keeps a single API surface/key-management path for all AI calls. |
| Similarity Search | `sqlite-vec` virtual table with cosine/L2 distance functions | Fast KNN search over accumulated ministry questions across subjects/years, running in-process — no ANN service, no network hop, matching the fully local, single-binary distribution model established in STEP-4.1. |
| Migration Tool | Plain versioned `.sql` migration files applied via a minimal Python runner | `alembic` is unnecessary machinery for a single-file embedded SQLite database with a small, slow-changing schema — a lightweight versioned-migration table (`schema_version`) applied at app startup is sufficient and keeps the dependency footprint down, consistent with the project's general bias toward minimal runtime dependencies for a distributed desktop app. |

#### 2. Technical Notes

- **Write path is append-only for `ministry_question` blocks:** once a block is classified as `ministry_question` (STEP-1.4) and passes integrity verification (STEP-1.1), its `raw_content` plus embedding vector are inserted — never updated in place, since the source text must remain traceable to its original booklet/page. Corrections create a new row with a `superseded_by` link, not a mutation.
- **Duplicate detection uses the STEP-1.1 canonical hash first, embedding similarity second:** an exact `content_hash` match short-circuits to "already indexed" without an embedding call, saving API cost on re-processed booklets. Embedding similarity (cosine distance threshold) is only used to surface *near-duplicate* questions (paraphrased across years) for optional human review — it never auto-merges or auto-deletes.
- **Fail-Fast scope is narrower here than STEP-1.1/2.x:** a failed embedding call (network/API) does not block document generation — the question is still stored with `embedding = NULL` and a `pending_embedding` flag, backfilled later. This mirrors STEP-1.4's audited-fallback philosophy: enrichment failure degrades gracefully with visibility, not a hard halt, because generation must not depend on this optional database.
- **The database file itself lives alongside the app's other local data** (e.g. `%APPDATA%/Booklet_AI/question_bank.db`), consistent with the single-machine, no-services deployment model — there is no multi-user server to reason about, which also simplifies the write-path concurrency story versus a networked PostgreSQL instance.

#### 3. Declarative Definitions

```sql
-- migrations/001_ministry_questions.sql
-- sqlite-vec loaded as a runtime extension (app/utils/db.py loads it via
-- sqlite3.Connection.enable_load_extension + sqlite_vec.load(conn))

CREATE TABLE ministry_questions (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    block_id            TEXT NOT NULL,
    content_hash        TEXT NOT NULL UNIQUE,
    raw_content         TEXT NOT NULL,
    subject             TEXT,
    grade_level         TEXT,
    source_doc_id       TEXT NOT NULL,
    source_page         INTEGER,
    pending_embedding   INTEGER NOT NULL DEFAULT 1,  -- SQLite has no native BOOLEAN
    superseded_by       INTEGER REFERENCES ministry_questions(id),
    created_at          TEXT NOT NULL DEFAULT (datetime('now'))
);

-- sqlite-vec virtual table for the embedding vectors, linked by rowid
CREATE VIRTUAL TABLE ministry_questions_vec USING vec0(
    embedding FLOAT[1536]
);
```

```python
# app/core/exceptions.py (extended)
class QuestionIndexError(BookletError):
    """Raised on unrecoverable database write failure for the question bank."""
```

```python
# app/ai/question_indexer.py
from typing import List, Optional
from app.core.models import ContentBlock
from app.core.exceptions import QuestionIndexError

class QuestionIndexer:
    """
    Persists ministry_question blocks to the local sqlite-vec-backed store
    (no server process — a single .db file alongside the app's other local
    data). Embedding failures degrade to pending_embedding=True; database
    write failures raise QuestionIndexError (distinct from the
    enrichment-layer graceful degradation used for embedding itself).
    """
    def __init__(self, db_connection, embedding_client):
        self.db = db_connection  # sqlite3.Connection with sqlite-vec loaded
        self.embedding_client = embedding_client

    def index_block(self, block: ContentBlock, source_doc_id: str, source_page: int) -> None:
        if block.block_type != "ministry_question":
            return

        existing = self.db.execute(
            "SELECT id FROM ministry_questions WHERE content_hash = ?",
            (block.content_hash,)
        ).fetchone()
        if existing is not None:
            return  # already indexed, no duplicate insert, no embedding call

        embedding: Optional[list[float]] = None
        pending = True
        try:
            embedding = self.embedding_client.embed(block.raw_content)
            pending = False
        except Exception:
            pending = True  # backfilled later; does not block generation

        try:
            cursor = self.db.execute(
                """INSERT INTO ministry_questions
                   (block_id, content_hash, raw_content, source_doc_id, source_page, pending_embedding)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                (block.block_id, block.content_hash, block.raw_content,
                 source_doc_id, source_page, 0 if not pending else 1)
            )
            if embedding is not None:
                self.db.execute(
                    "INSERT INTO ministry_questions_vec (rowid, embedding) VALUES (?, ?)",
                    (cursor.lastrowid, embedding)
                )
            self.db.commit()
        except Exception as exc:
            self.db.rollback()
            raise QuestionIndexError(f"Failed to persist question block {block.block_id}: {exc}") from exc
```

#### 4. Acceptance Criteria

- **[AC-3.1.1]** Re-processing an already-indexed booklet inserts zero duplicate rows (`content_hash` uniqueness enforced via SQLite `UNIQUE` constraint, checked before any embedding call).
- **[AC-3.1.2]** An embedding API failure never raises out of `index_block()` as an unhandled exception and never blocks document generation — the row is persisted with `pending_embedding = 1`.
- **[AC-3.1.3]** A database write failure (distinct from embedding failure) raises `QuestionIndexError` and rolls back the transaction, surfaced to the user as a non-blocking warning banner (generation continues; indexing is skipped for that block).
- **[AC-3.1.4]** KNN similarity search over `ministry_questions_vec` returns results ordered by distance in under 200ms for a bank of ≤50,000 rows on typical print-shop desktop hardware.
- **[AC-3.1.5]** The application requires zero external services, ports, or installed database servers for this feature to function — verified by running the full indexing + search path with network access disabled except for the OpenRouter embedding call itself.

---

## PHASE 3 STATUS: CLOSED (STEP-3.1)

---

## PHASE 4: Packaging & Distribution
