-- migrations/001_ministry_questions.sql
-- sqlite-vec loaded as a runtime extension (app/utils/db.py loads it via
-- sqlite3.Connection.enable_load_extension + sqlite_vec.load(conn))

CREATE TABLE IF NOT EXISTS ministry_questions (
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
CREATE VIRTUAL TABLE IF NOT EXISTS ministry_questions_vec USING vec0(
    embedding FLOAT[1536]
);
