-- migrations/004_question_occurrences.sql (Phase 6 addition to STEP-3.1's schema)
-- Preserves STEP-3.1's canonical-row-per-unique-question model exactly;
-- adds a separate occurrence log so a duplicate's OTHER appearances are
-- never silently discarded (AC-6.7.3, AC-6.7.4).

CREATE TABLE IF NOT EXISTS ministry_question_occurrences (
    id                   INTEGER PRIMARY KEY AUTOINCREMENT,
    question_id          INTEGER NOT NULL REFERENCES ministry_questions(id),
    source_doc_id         TEXT NOT NULL,
    source_page           INTEGER,
    block_id              TEXT NOT NULL,
    recorded_at            TEXT NOT NULL,
    UNIQUE(question_id, source_doc_id, block_id)
);
