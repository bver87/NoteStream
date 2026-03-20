import logging
import os
import sqlite3
from contextlib import contextmanager

log = logging.getLogger("notestream.db")

DB_PATH = os.getenv("DB_PATH", "/data/app.db")


def init_db() -> None:
    db_dir = os.path.dirname(DB_PATH)
    if db_dir:
        os.makedirs(db_dir, exist_ok=True)

    with sqlite3.connect(DB_PATH) as conn:

        # WAL mode: allows concurrent reads while a write is in progress.
        # Critical now that the thread pool can write progress updates
        # while the web server reads status — without WAL these block each other.
        conn.execute("PRAGMA journal_mode=WAL;")
        conn.execute("PRAGMA synchronous=NORMAL;")   # safe with WAL, faster than FULL

        # ================= USERS =================
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS users (
                id           TEXT PRIMARY KEY,
                email        TEXT UNIQUE NOT NULL,
                password_hash TEXT NOT NULL,
                created_at   TEXT NOT NULL
            );
            """
        )

        # ================= PASSWORD RESETS =================
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS password_resets (
                id          TEXT PRIMARY KEY,
                user_id     TEXT NOT NULL,
                token_hash  TEXT NOT NULL,
                expires_at  INTEGER NOT NULL,
                used_at     INTEGER,
                created_at  INTEGER NOT NULL,
                FOREIGN KEY(user_id) REFERENCES users(id)
            );
            """
        )

        _migrate_columns(conn, "password_resets", {
            "id":         "ALTER TABLE password_resets ADD COLUMN id TEXT",
            "token_hash": "ALTER TABLE password_resets ADD COLUMN token_hash TEXT",
            "used_at":    "ALTER TABLE password_resets ADD COLUMN used_at INTEGER",
            "created_at": "ALTER TABLE password_resets ADD COLUMN created_at INTEGER",
        })

        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_pr_user_id   ON password_resets(user_id);"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_pr_expires_at ON password_resets(expires_at);"
        )

        # ================= JOBS =================
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS jobs (
                id            TEXT PRIMARY KEY,
                user_id       TEXT NOT NULL,
                status        TEXT NOT NULL,
                progress      INTEGER DEFAULT 0,
                current_chunk INTEGER DEFAULT 0,
                total_chunks  INTEGER DEFAULT 0,
                eta_seconds   INTEGER,
                output_path   TEXT,
                audio_path    TEXT,      -- stored so retry can re-queue the file
                agenda        TEXT,
                text_token    TEXT,      -- random token for the /share/ endpoint
                model_size    TEXT DEFAULT 'medium',  -- whisper model used for this job
                created_at    INTEGER NOT NULL,
                FOREIGN KEY(user_id) REFERENCES users(id)
            );
            """
        )

        _migrate_columns(conn, "jobs", {
            "progress":      "ALTER TABLE jobs ADD COLUMN progress      INTEGER DEFAULT 0",
            "current_chunk": "ALTER TABLE jobs ADD COLUMN current_chunk INTEGER DEFAULT 0",
            "total_chunks":  "ALTER TABLE jobs ADD COLUMN total_chunks  INTEGER DEFAULT 0",
            "eta_seconds":   "ALTER TABLE jobs ADD COLUMN eta_seconds   INTEGER",
            "output_path":   "ALTER TABLE jobs ADD COLUMN output_path   TEXT",
            "audio_path":    "ALTER TABLE jobs ADD COLUMN audio_path    TEXT",
            "agenda":        "ALTER TABLE jobs ADD COLUMN agenda        TEXT",
            "text_token":    "ALTER TABLE jobs ADD COLUMN text_token    TEXT",
            "model_size":    "ALTER TABLE jobs ADD COLUMN model_size    TEXT DEFAULT 'medium'",
        })

        # Indexes — each maps to a common query pattern
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_jobs_user_id    ON jobs(user_id);"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_jobs_created_at ON jobs(created_at);"
        )
        conn.execute(
            # Every /share/{token} call hits this — must be indexed
            "CREATE INDEX IF NOT EXISTS idx_jobs_text_token ON jobs(text_token);"
        )

        conn.commit()
        log.info("Database initialised at %s", DB_PATH)


def _migrate_columns(conn: sqlite3.Connection, table: str, columns: dict[str, str]) -> None:
    """Add any missing columns to an existing table (safe no-op if already present)."""
    existing = {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}
    for col, sql in columns.items():
        if col not in existing:
            conn.execute(sql)
            log.info("Migration: added column '%s' to table '%s'", col, table)


@contextmanager
def get_conn():
    """Yield a SQLite connection with Row factory enabled. Always closes on exit."""
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    # Apply WAL for every connection opened after startup too
    conn.execute("PRAGMA journal_mode=WAL;")
    try:
        yield conn
    finally:
        conn.close()