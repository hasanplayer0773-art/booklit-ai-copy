"""
Booklet_AI Desktop — SQLite & sqlite-vec Database Utilities (STEP-3.1)

Provides connection management, extension loading, and lightweight schema
migration for the local embedded question bank database.

Zero-Service Embedded Architecture:
- Single SQLite database file stored at %APPDATA%/Booklet_AI/question_bank.db
  (or local user directory).
- Uses the sqlite-vec runtime extension for in-process KNN vector search.
- Zero external services, zero background server processes, zero open network ports.
- Minimal migration runner tracking applied versions in schema_version table.
"""

import logging
import os
import re
import sqlite3
from pathlib import Path
from typing import List, Optional, Tuple, Union

import sqlite_vec

logger = logging.getLogger("booklet_ai.db")

_APP_DIR_NAME = "Booklet_AI"
_DB_FILE_NAME = "question_bank.db"


def get_default_db_dir() -> Path:
    """Get the default directory for Booklet_AI local application data.

    Returns:
        Path to %APPDATA%/Booklet_AI on Windows, or ~/.booklet_ai fallback.
    """
    app_data = os.environ.get("APPDATA")
    if app_data:
        base_dir = Path(app_data) / _APP_DIR_NAME
    else:
        base_dir = Path.home() / f".{_APP_DIR_NAME.lower()}"
    base_dir.mkdir(parents=True, exist_ok=True)
    return base_dir


def get_default_db_path() -> Path:
    """Get the default file path for the question bank SQLite database.

    Returns:
        Path to question_bank.db inside the application data directory.
    """
    return get_default_db_dir() / _DB_FILE_NAME


def get_default_migrations_dir() -> Path:
    """Resolve the default directory containing SQL migration files.

    Searches standard project locations:
    1. src/migrations
    2. migrations/ (repository root)
    3. Relative to this module

    Returns:
        Path to the resolved migrations directory.
    """
    candidates = [
        Path(__file__).resolve().parent.parent.parent / "migrations",  # src/migrations
        Path(__file__).resolve().parent.parent.parent.parent / "migrations",  # repo root migrations
        Path(__file__).resolve().parent / "migrations",
    ]
    for candidate in candidates:
        if candidate.is_dir() and any(candidate.glob("*.sql")):
            return candidate
    # Default to candidate 0 even if empty yet
    return candidates[0]


def get_db_connection(
    db_path: Optional[Union[str, Path]] = None,
    load_vec: bool = True,
    check_same_thread: bool = False,
) -> sqlite3.Connection:
    """Create a SQLite connection configured for the Booklet_AI Question Bank.

    Loads the sqlite-vec vector extension into the connection, enables foreign
    key enforcement, and configures WAL journal mode for file-based databases.

    Args:
        db_path: Optional path to the database file or ':memory:'. If None,
            the default application data path is used.
        load_vec: Whether to load the sqlite-vec extension. Default True.
        check_same_thread: Whether to allow multi-threaded access. Default False.

    Returns:
        Configured sqlite3.Connection instance.
    """
    if db_path is None:
        db_path = get_default_db_path()

    is_memory = str(db_path) == ":memory:"
    if not is_memory:
        target_path = Path(db_path)
        target_path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(str(target_path), check_same_thread=check_same_thread)
    else:
        conn = sqlite3.connect(":memory:", check_same_thread=check_same_thread)

    conn.row_factory = sqlite3.Row

    # Enable foreign keys
    conn.execute("PRAGMA foreign_keys = ON;")

    # Enable WAL mode for disk files to optimize concurrent reads & writes
    if not is_memory:
        try:
            conn.execute("PRAGMA journal_mode = WAL;")
        except sqlite3.Error as exc:
            logger.debug("Failed to set WAL journal mode: %s", exc)

    # Load sqlite-vec extension
    if load_vec:
        conn.enable_load_extension(True)
        try:
            sqlite_vec.load(conn)
        finally:
            conn.enable_load_extension(False)

    return conn


def apply_migrations(
    conn: sqlite3.Connection,
    migrations_dir: Optional[Union[str, Path]] = None,
) -> int:
    """Apply pending versioned SQL migrations to the database.

    Tracks applied migration versions in a `schema_version` table.
    Migrations are expected to be named with a leading integer version prefix,
    e.g. '001_ministry_questions.sql', '002_add_index.sql'.

    Args:
        conn: Open sqlite3.Connection with sqlite-vec loaded.
        migrations_dir: Path to directory containing migration .sql files.
            If None, auto-detected via get_default_migrations_dir().

    Returns:
        Number of newly applied migrations.
    """
    if migrations_dir is None:
        target_dir = get_default_migrations_dir()
    else:
        target_dir = Path(migrations_dir)

    if not target_dir.is_dir():
        logger.warning("Migrations directory not found at: %s", target_dir)
        return 0

    # Ensure schema_version table exists
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS schema_version (
            version INTEGER PRIMARY KEY,
            filename TEXT NOT NULL,
            applied_at TEXT NOT NULL DEFAULT (datetime('now'))
        );
        """
    )
    conn.commit()

    # Query currently applied versions
    cursor = conn.execute("SELECT version FROM schema_version")
    applied_versions = {row[0] for row in cursor.fetchall()}

    # Discover and sort migration files
    migration_files: List[Tuple[int, Path]] = []
    for file_path in target_dir.glob("*.sql"):
        match = re.match(r"^(\d+)_", file_path.name)
        if match:
            version = int(match.group(1))
            migration_files.append((version, file_path))

    migration_files.sort(key=lambda item: item[0])

    new_migrations_count = 0
    for version, file_path in migration_files:
        if version in applied_versions:
            continue

        logger.info("Applying migration version %d: %s", version, file_path.name)
        sql_content = file_path.read_text(encoding="utf-8")

        try:
            conn.executescript(sql_content)
            conn.execute(
                "INSERT INTO schema_version (version, filename) VALUES (?, ?)",
                (version, file_path.name),
            )
            conn.commit()
            new_migrations_count += 1
            logger.info("Successfully applied migration %s", file_path.name)
        except Exception as exc:
            conn.rollback()
            logger.error("Failed to apply migration %s: %s", file_path.name, exc)
            raise

    return new_migrations_count


def init_db(
    db_path: Optional[Union[str, Path]] = None,
    migrations_dir: Optional[Union[str, Path]] = None,
    check_same_thread: bool = False,
) -> sqlite3.Connection:
    """Initialize a database connection and apply all pending migrations.

    Args:
        db_path: Optional path to the database file or ':memory:'.
        migrations_dir: Optional path to the migrations directory.
        check_same_thread: Whether to allow multi-threaded access. Default False.

    Returns:
        Configured and fully migrated sqlite3.Connection.
    """
    conn = get_db_connection(db_path=db_path, load_vec=True, check_same_thread=check_same_thread)
    apply_migrations(conn, migrations_dir=migrations_dir)
    return conn
