<!-- من الخطة الرئيسية Booklet_AI Desktop Master Plan -->

### [STEP-6.9] Dashboard

#### 1. Rationale

Aggregate visibility into shop-level usage (booklets processed, average processing time, error rate) over time, for the print-shop owner rather than for a single job.

**Post-review correction:** the original draft's metrics were computed directly from `job_history` rows without defining what a "job" means once STEP-6.2's `run_job_with_retry()` can produce more than one execution attempt for a single logical unit of work — without this definition, two correct implementations could report different numbers from the same underlying data. A missing-index performance concern for large history tables is also addressed.

#### 2. Architectural Placement

- The Dashboard is a read-only aggregation view over STEP-6.3's `job_history` table — it introduces no new write path and no new data beyond what `job_history` already records per job.
- **Metrics are computed per logical job, not per execution attempt.** When `run_job_with_retry()` (STEP-6.2) retries a `COMOperationTimeout`, the failed first attempt and the successful retry are two *executions* of one logical job — the Dashboard must count this as one processed booklet (if the retry succeeds) or one failed booklet (if both attempts fail), never two. This requires `job_history` to record retry attempts as related to a single logical job identifier rather than as independent rows indistinguishable from unrelated jobs — `job_history.id` remains one row per logical job; `run_job_with_retry()`'s individual attempts are attributed to that same row's final `status`, with attempt count available as supplementary detail, not as separate countable "jobs."
- **"Average processing time" is defined explicitly as `completed_at - started_at` for `status='success'` rows only**, using the logical job's overall start (first attempt) to completion (successful attempt), not summed across retries and not including queue wait time for batched jobs (STEP-6.7) — a job queued behind four others waiting its turn is not "processing" during that wait.
- **"Error rate"** is defined as `status='failed'` logical jobs divided by total completed logical jobs (`success` + `failed`) in the selected period — `status='in_progress'`/cancelled jobs are excluded from both the numerator and denominator, since they have no determinate outcome yet.
- Because it is read-only and derived entirely from `job_history`, the Dashboard has no interaction with `threading.RLock` or `DocumentStore` — consistent with STEP-6.3's concurrency placement (AC-6.3.5).

#### 3. Declarative Definitions

```sql
-- migrations/004_job_history_indexes.sql
-- Required for Dashboard query performance at print-shop-realistic
-- volumes (thousands of jobs over time) — without these, aggregate
-- queries scan the full table on every Dashboard view/refresh.
CREATE INDEX idx_job_history_started_at ON job_history(started_at);
CREATE INDEX idx_job_history_status ON job_history(status);
CREATE INDEX idx_job_history_status_started_at ON job_history(status, started_at);
```

#### 4. Preconditions

- Dashboard aggregate figures (processed count, average time, error rate) must be computed via SQL aggregate queries (`COUNT`, `AVG`) executed against `job_history` directly — never by loading all rows into Python and aggregating in application code, which does not scale to a print shop's realistic multi-year job volume.
- A job's retry attempts (STEP-6.2) must never be counted as separate logical jobs in any Dashboard metric — this is a standing requirement on how `job_history` rows relate to retry attempts, not merely a Dashboard-side filtering convenience.

#### 5. Acceptance Criteria

- **[AC-6.9.1]** The Dashboard's "booklets processed" count for a given period matches the count of `job_history` rows with `status='success'` in that period — verified against the same table STEP-6.3 writes, not a separately-maintained counter.
- **[AC-6.9.1b]** A logical job that fails once (`COMOperationTimeout`) and succeeds on `run_job_with_retry()`'s single permitted retry is counted as exactly one successful booklet in "booklets processed," not two — verified directly against this exact retry scenario.
- **[AC-6.9.2]** The Dashboard's error-rate figure matches `failed` rows divided by (`failed` + `success`) rows in `job_history` for the selected period — recomputed from `job_history` on each view, not cached in a way that could drift from the underlying table, and excluding `in_progress`/cancelled rows from both numerator and denominator.
- **[AC-6.9.3]** Viewing the Dashboard never acquires `DocumentStore`'s `threading.RLock` — verified by confirming the Dashboard's data path touches only the SQLite connection.
- **[AC-6.9.4]** Average processing time excludes any time a batched job (STEP-6.7) spent queued waiting for prior jobs — verified by confirming the computation uses each job's own `started_at` (when its own execution began), not its enqueue time.
- **[AC-6.9.5]** `job_history` queries backing the Dashboard use the indexes defined in `004_job_history_indexes.sql` — verified via query plan inspection (e.g. `EXPLAIN QUERY PLAN`) confirming an index scan rather than a full table scan for the standard Dashboard queries.

---

## PHASE 6 STATUS: v5 — 4 CRITICAL DEFECTS FROM THE 3-MODEL FOLLOW-UP REVIEW FIXED (RETRY-STATE WIRING & UNDO-PRESERVING RETRY SCOPE, IMAGEASSET EXTRACTION WIRING + DUAL-AXIS DPI, WEASYPRINT PACKAGING/MATHML CORRECTION, CLASSIFICATION-VS-UNDO/REDO REVISION RACE). REMAINING LOWER-PRIORITY ITEMS FROM THAT REVIEW (E.G. EXACT DIGIT-SHAPING PROPERTY NAME, QUERY-PLAN-LEVEL DASHBOARD VERIFICATION, FULL SESSION-TABLE MODEL) ARE LEFT AS IMPLEMENTATION-TIME DETAILS RATHER THAN PLAN-LEVEL ARCHITECTURE GAPS.

**Standing note on embedded code throughout this document:** all Python/JSX/SQL shown across this master plan is illustrative pseudocode establishing the required contracts, invariants, and data shapes — it is a specification for what must exist and how the pieces must fit together, not a final, directly-runnable implementation. Actual implementation (in Claude Code or otherwise) is responsible for producing working code that satisfies every stated Acceptance Criterion; discovering that a shown code snippet needs adjustment during real implementation is expected and does not by itself indicate a flaw in the architecture, provided the resulting code still satisfies the ACs and preserves the stated invariants.

**Explicit scope boundary:** the following were evaluated during planning and deliberately excluded from this project. They must not be reintroduced into Phase 6 or any future phase without a new, explicit decision:
- OCR / scanned-PDF support
- English-language pattern rules for the offline `RuleBasedClassifier`
- A style/font "profile" system
- Portable-mode packaging (the Adobe InDesign dependency itself cannot be made portable regardless of how this application is packaged)
- Fixed per-page instructor-name/specialty headers
- Any remote-access or remote-support capability

---

## MASTER PLAN STATUS: PHASES 1–5 COMPLETE AND ADVERSARIALLY REVIEWED. PHASE 6 AT v5 AFTER TWO ROUNDS OF MULTI-MODEL ADVERSARIAL REVIEW. READY TO PROCEED TO IMPLEMENTATION, WITH THE UNDERSTANDING THAT EMBEDDED CODE IS ARCHITECTURAL PSEUDOCODE TO BE REALIZED AND VALIDATED AGAINST EACH ACCEPTANCE CRITERION DURING ACTUAL DEVELOPMENT.