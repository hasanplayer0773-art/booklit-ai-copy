-- migrations/003_preflight_overrides.sql
-- Records preflight blocker overrides in the local SQLite database
-- for auditability (STEP-6.4 AC-6.4.1b).

CREATE TABLE IF NOT EXISTS preflight_overrides (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    job_id              INTEGER REFERENCES job_history(id),
    blocker_code        TEXT NOT NULL,
    reason              TEXT NOT NULL,
    overridden_at       TEXT NOT NULL,
    user_identifier     TEXT DEFAULT 'designer'
);
