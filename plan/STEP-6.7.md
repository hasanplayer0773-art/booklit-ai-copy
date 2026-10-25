<!-- من الخطة الرئيسية Booklet_AI Desktop Master Plan -->

### [STEP-6.7] Batch Processing — Sequential Queue

#### 1. Rationale

Processing multiple source files without requiring the operator to manually re-launch a job for each one. This is explicitly scoped as a **sequential job queue**, not concurrent or merged-context processing — the design goal is specifically to avoid ever submitting more than one document's content to a single AI classification call, which would risk exceeding a model's context window on large, formula/table-heavy booklets.

**Post-review correction:** the original draft's file-to-file isolation was sound but incomplete in three confirmed ways: (1) isolating *between* files does not guarantee a single *large* file's own content fits in one `classify_blocks()` call — that guarantee was never actually implemented; (2) several categories of shared/transient state beyond the AI payload (temp directories, caches, error counters, output paths) were not enumerated as requiring per-job isolation; (3) the `ministry_questions` deduplication behavior (STEP-3.1), when exercised across a batch, silently discards the fact that a duplicate question also appeared in a second file — losing exactly the source traceability the table's own design goal claims to provide. All three are corrected below, along with specifying the missing queue-manager component and a note on embedding-backfill ordering.

#### 2. Architectural Placement — Explicit Isolation Guarantees

- **Each queued file is processed as a fully independent job**, from extraction (STEP-1.1–1.3) through AI classification (STEP-1.4) through InDesign generation (Phase 2), start to finish, before the next queued file begins. There is no point in the pipeline where content from two different source files coexists in the same `DocumentStore` instance, the same `OpenRouterClient.classify_blocks()` call, or the same `COMSupervisor` job.
- **`classify_blocks()` itself is extended to chunk a single large document's blocks across multiple AI requests when needed**, closing the confirmed gap that file-to-file isolation alone does not bound a single file's payload size. **Post-follow-up-review correction:** this also closes a confirmed race with STEP-6.3's Undo/Redo — because a multi-chunk classification run spans multiple sequential network requests, the `DocumentStore` could in principle be edited or undone by the operator midway through, and a later chunk's response should never be silently merged onto content that has since changed underneath it. The corrected version snapshots a `document_revision` once at the start of the call and validates it has not advanced before merging each chunk's result:

```python
# app/ai/client.py (Phase 6 extension of STEP-1.4's OpenRouterClient)
def classify_blocks(self, blocks: list[ContentBlock], document_revision: int) -> list[ContentBlock]:
    """
    Extended from the STEP-1.4 version: if the eligible-block payload for
    THIS SINGLE document exceeds a token budget, it is split into
    sequential chunks, each independently validated with the same
    exact-match rules AC-1.4.2/2b/2c already require (no partial/foreign/
    duplicate block_ids per chunk), then deterministically merged by
    block_id before returning. This is still a single-document operation
    — chunking never combines blocks from more than one source_doc_id
    into any request, which remains the one invariant this feature exists
    to protect (see Preconditions).

    document_revision is a monotonically increasing counter STEP-6.3's
    with_user_edit()/undo()/redo() already increments on every DocumentStore
    mutation (an addition to those functions' existing responsibilities).
    The CALLER (the orchestration layer, not this function) is responsible
    for re-validating this revision against DocumentStore's current
    revision under the lock before applying the merged result — see
    Preconditions below. This function's own job is limited to chunking
    and merging; ownership of the revision check belongs where the lock
    already lives (STEP-6.3), not duplicated here.
    """
    eligible_blocks = [
        b for b in blocks
        if b.block_type not in PRECLASSIFIED_TYPES and b.raw_content.strip()  # STEP-1.4 constant (includes legacy_equation_placeholder)
    ]
    if not eligible_blocks or not self.api_key:
        return RuleBasedClassifier.classify_document_blocks(blocks)

    chunks = self._split_by_token_budget(eligible_blocks)
    classified_by_id: dict[str, ContentBlock] = {}
    for chunk in chunks:
        chunk_eligible_ids = {b.block_id for b in chunk}
        try:
            payload = self._build_payload(chunk)
            response = self._send_request(payload)  # STEP-1.4's existing httpx call, scoped to this chunk
            parsed = ClassificationResponse.model_validate_json(
                response.json()["choices"][0]["message"]["content"]
            )
            chunk_result = self._apply_classifications(chunk, parsed, chunk_eligible_ids)
            for b in chunk_result:
                classified_by_id[b.block_id] = b
        except Exception as exc:
            # A chunk failure falls back to RuleBasedClassifier for THAT
            # CHUNK only, not the whole document — consistent with
            # STEP-1.4's existing per-call Audited Graceful Degradation.
            for b in RuleBasedClassifier.classify_document_blocks(chunk):
                classified_by_id[b.block_id] = b

    return [classified_by_id.get(b.block_id, b) for b in blocks]

def _split_by_token_budget(self, blocks: list[ContentBlock]) -> list[list[ContentBlock]]:
    """
    Concrete token-budget chunking: uses a conservative character-count
    proxy for token count (avoiding a heavyweight tokenizer dependency
    for this estimate — same character-proxy philosophy STEP-2.2 already
    uses for table column widths). CHARS_PER_TOKEN_ESTIMATE is
    deliberately conservative (under-counts capacity) to keep the actual
    request safely under the model's real limit. Blocks are never split
    mid-block; a single block larger than the whole budget becomes its
    own one-block chunk (accepted as an edge case rather than fragmenting
    a block's raw_content, which would violate Zero-Mutation).
    """
    CHARS_PER_TOKEN_ESTIMATE = 3.0
    MAX_TOKENS_PER_REQUEST = 6000  # conservative; well under typical model context limits
    max_chars = int(MAX_TOKENS_PER_REQUEST * CHARS_PER_TOKEN_ESTIMATE)

    chunks: list[list[ContentBlock]] = []
    current: list[ContentBlock] = []
    current_chars = 0
    for block in blocks:
        block_chars = len(block.raw_content)
        if current and current_chars + block_chars > max_chars:
            chunks.append(current)
            current, current_chars = [], 0
        current.append(block)
        current_chars += block_chars
    if current:
        chunks.append(current)
    return chunks
```

- **Each file's AI classification session remains fully isolated at the document level** — chunking happens *within* one document's own call sequence, never across documents. Batch Processing's queue/orchestration layer sits entirely on top of this existing per-document call pattern; it does not itself need to know about chunking, which is `classify_blocks()`'s own internal concern.
- **Explicit job-scoped state inventory** — beyond AI classification isolation, the following must be reset or freshly created for every queued job, not merely "usually" isolated by virtue of sequential execution: temporary directories, prompt/response caches, placeholder maps, classification confidence/fallback counters, error/retry counters (STEP-6.2), output paths, cancellation tokens, document revisions (STEP-6.3), and the `COMSupervisor` child-process handle. Sequential execution alone does not guarantee this — a shared global cache object, for instance, could still leak state between sequentially-processed jobs if it is not explicitly scoped per job.
- **A `BatchQueue` component owns orchestration**, running on a dedicated worker thread (never the Qt main thread), holding an ordered list of pending file paths, processing them one at a time via `run_job_with_retry()` (STEP-6.2), and emitting Qt Signals for per-job start/success/failure/progress so the UI can display live queue status. The UI supports cancelling the remaining queue (not the in-flight job forcibly, but refusing to start the next one) between jobs.
- **A failure in one queued job does not halt the queue** — consistent with STEP-1.4's Audited Graceful Degradation philosophy for enrichment-stage concerns, a failed job is recorded in `job_history` (STEP-6.3) with `status='failed'`, and the queue proceeds to the next file. After the full queue completes, the UI presents a summary (N succeeded, M failed) with access to each failed job's translated error (STEP-6.2) and Problem Report.

#### 3. Question Bank Traceability Across Batch (Corrected)

STEP-3.1's original duplicate-detection behavior (skip insert entirely on `content_hash` match) is **preserved as-is for deduplication** — this correction does not change that a duplicate question produces only one canonical row. What it fixes is that the *original* design had no way to represent "this question, already known, also appeared in File B" — a real loss of traceability that becomes visible specifically once Batch Processing makes cross-file duplicates routine rather than rare.

```sql
-- migrations/003_question_occurrences.sql (Phase 6 addition to STEP-3.1's schema)
-- Preserves STEP-3.1's canonical-row-per-unique-question model exactly;
-- adds a separate occurrence log so a duplicate's OTHER appearances are
-- never silently discarded.
CREATE TABLE ministry_question_occurrences (
    id                   INTEGER PRIMARY KEY AUTOINCREMENT,
    question_id          INTEGER NOT NULL REFERENCES ministry_questions(id),
    source_doc_id         TEXT NOT NULL,
    source_page           INTEGER,
    block_id              TEXT NOT NULL,
    recorded_at            TEXT NOT NULL,
    UNIQUE(question_id, source_doc_id, block_id)
);
```

```python
# app/ai/question_indexer.py (Phase 6 correction)
def index_block(self, block: ContentBlock, source_doc_id: str, source_page: int) -> None:
    if block.block_type != "ministry_question":
        return

    existing = self.db.execute(
        "SELECT id FROM ministry_questions WHERE content_hash = ?",
        (block.content_hash,)
    ).fetchone()

    if existing is not None:
        # CORRECTED: previously returned here with no further action,
        # silently losing that this content_hash also occurred in
        # source_doc_id. Now records the occurrence without inserting a
        # second ministry_questions row — dedup behavior is unchanged,
        # traceability is restored.
        self.db.execute(
            """INSERT OR IGNORE INTO ministry_question_occurrences
               (question_id, source_doc_id, source_page, block_id, recorded_at)
               VALUES (?, ?, ?, ?, datetime('now'))""",
            (existing[0], source_doc_id, source_page, block.block_id)
        )
        self.db.commit()
        return

    # ... unchanged: insert into ministry_questions + ministry_questions_vec
    # as STEP-3.1 already specifies, THEN also record this first
    # occurrence in ministry_question_occurrences for symmetry (a
    # question's first appearance and every subsequent one are recorded
    # the same way, not treated as two different mechanisms).
```

#### 4. Embedding Backfill Ordering Independence

STEP-3.1 already allows a `ministry_question` row to be persisted with `pending_embedding = 1` when the embedding call fails, backfilled later. Batch Processing surfaces a previously-latent ordering concern: if a later queued job's retrieval logic depends on an earlier job's embedding having already been backfilled, the *order* jobs happen to run in could silently change retrieval results. This must not be allowed to affect classification correctness:

- Any vector-similarity retrieval used during classification or review must operate only on embeddings available **at the moment of the query** — it is explicitly not part of this pipeline's correctness contract that a queued job's output depends on whether an earlier job's background embedding backfill has completed yet. If a future feature wants deterministic cross-job retrieval, it must make that an explicit, declared input to the job (e.g. "wait for backfill to complete" as a stated precondition), not an accidental side effect of queue timing.

#### 5. Preconditions

- The queue orchestration layer must not introduce any code path where `ContentBlock`s from more than one source file are present in a single AI request — chunking within `classify_blocks()` (per §2 above) must never merge chunks across `source_doc_id` values. This remains the single invariant this feature exists to guarantee, now correctly enforced at both the file level (queue) and the request level (chunking).
- **The orchestration layer calling `classify_blocks()` must re-validate `document_revision` against `DocumentStore`'s current revision, under `DocumentStore`'s lock (the same lock STEP-6.3's undo/redo already uses), before applying the merged classification result.** If the revision has advanced (i.e. an edit or undo/redo occurred while a multi-chunk classification was in flight), the merged result is discarded and classification is re-run against the current state — it is never applied on top of content it no longer accurately reflects. This is the required closure of the confirmed race between chunked classification (which can span multiple sequential network round-trips) and interactive editing (STEP-6.3), which both touch the same `DocumentStore`.
- `with_user_edit()`, `undo()`, and `redo()` (STEP-6.3) each increment a `document_revision` counter on `DocumentStore` as part of their existing lock-protected mutation — this is an additive responsibility on those already-defined functions, not a new independent mutation path.
- All job-scoped state enumerated in §2 (temp directories, caches, counters, output paths, cancellation tokens, document revisions, COM handles) must be freshly created or explicitly reset at the start of each queued job — no global/module-level mutable object may be reused across jobs without an explicit per-job reset step.
- `ministry_question_occurrences` writes are a required part of `QuestionIndexer.index_block()`'s duplicate-hit path — the corrected version above (recording the occurrence even on a `content_hash` match) is not optional.

#### 6. Acceptance Criteria

- **[AC-6.7.1]** A batch of N queued files results in exactly N separate document-level classification sequences, each with only that file's blocks — verified by asserting no request sent to the AI client contains `block_id`s from more than one `source_doc_id`, across both single-request and multi-chunk documents.
- **[AC-6.7.1b]** A single document whose eligible-block payload exceeds the configured token budget is split into multiple sequential requests by `classify_blocks()`, each independently exact-match validated, with results deterministically merged by `block_id` — verified against a synthetic oversized single-document fixture, proving isolation is enforced within a document, not just between documents.
- **[AC-6.7.1c]** A `with_user_edit()` or `undo()`/`redo()` call occurring on `DocumentStore` while a multi-chunk `classify_blocks()` call is still in flight (between chunk 1's response and chunk 2's request, in a test double) causes the eventual merged classification result to be discarded rather than applied — verified directly against this exact interleaving, closing the confirmed race between chunked classification and interactive editing.
- **[AC-6.7.2]** A Hard-Halt failure (e.g. `TextIntegrityError`) in file 2 of a 5-file batch records file 2 as `failed` in `job_history` and does not prevent files 3–5 from being processed.
- **[AC-6.7.3]** Queued jobs' writes to `ministry_questions`, `ministry_question_occurrences`, and `job_history` never require a schema or write path beyond what STEP-3.1/6.3/6.7 already define for a single job.
- **[AC-6.7.4]** Two files queued back-to-back that each contain a `ministry_question` block with identical `content_hash` correctly result in only one row in `ministry_questions` **and two rows in `ministry_question_occurrences`** (one per source file) — verified directly, closing the traceability gap the original draft left open.
- **[AC-6.7.5]** All job-scoped state enumerated in §2 is verified freshly created (not reused) for job N+1 relative to job N in the same batch run — verified by instrumenting a test double that fails the test if any listed state object's identity is shared across two sequential jobs.
- **[AC-6.7.6]** A queued job's classification output is verified identical whether run as the first or last job in a batch containing an earlier job whose embedding backfill has not yet completed — proving retrieval correctness does not silently depend on queue-position-dependent background timing.

---
