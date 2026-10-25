-- migrations/002_job_history_and_undo.sql
-- Added to the same local SQLite database as STEP-3.1's ministry_questions
-- tables (%APPDATA%/Booklet_AI/question_bank.db) — no second database file.

CREATE TABLE IF NOT EXISTS job_history (
    id                      INTEGER PRIMARY KEY AUTOINCREMENT,
    source_file_path        TEXT NOT NULL,
    -- Post-review correction: split into two explicit hash fields so
    -- neither is mistaken for the other:
    source_file_sha256      TEXT NOT NULL,  -- SHA-256 of the raw source file bytes; identifies WHICH FILE this job was
    document_content_hash   TEXT,           -- STEP-1.1 compute_canonical_hash() of the extracted text tree, if applicable
    page_count               INTEGER,
    status                   TEXT NOT NULL,  -- 'success' | 'failed' | 'in_progress'
    error_summary             TEXT,           -- translated (STEP-6.2) summary, if failed
    started_at               TEXT NOT NULL,
    completed_at              TEXT
);

-- Ordered edit sequence with an explicit cursor, replacing the prior
-- draft's single "undone" boolean, which could not represent
-- undo -> redo -> new-edit branching correctly.
CREATE TABLE IF NOT EXISTS block_edit_history (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    -- Nullable: an edit may occur in the preview/edit screen before the
    -- user has ever submitted a generation job for this document, i.e.
    -- before any job_history row exists yet. Populated retroactively
    -- once the document is submitted as a job (see Preconditions).
    job_id              INTEGER REFERENCES job_history(id),
    document_session_id TEXT NOT NULL,  -- groups edits belonging to one open-document session, independent of job_id
    sequence_index       INTEGER NOT NULL,  -- monotonically increasing position within this document_session_id
    block_id             TEXT NOT NULL,
    previous_content      TEXT NOT NULL,
    previous_hash          TEXT NOT NULL,
    new_content            TEXT NOT NULL,
    new_hash                TEXT NOT NULL,
    edited_at              TEXT NOT NULL,
    UNIQUE(document_session_id, sequence_index)
);

-- The current cursor position per open document session — the single
-- source of truth for "what is the current undo/redo position," rather
-- than inferring it from scattered boolean flags across rows.
CREATE TABLE IF NOT EXISTS edit_cursor (
    document_session_id TEXT PRIMARY KEY,
    current_sequence_index INTEGER NOT NULL  -- 0 = no edits applied; N = sequence_index N is the most recently applied edit
);
