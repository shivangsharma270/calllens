"""SQLite schema and helpers.

One shared autocommit connection. All database access happens on the event-loop thread
(route handlers are `async def`, background work is asyncio tasks), so no locking is needed.
Never touch these helpers from inside `asyncio.to_thread`.
"""

import json
import sqlite3
from collections.abc import Iterable, Iterator
from contextlib import contextmanager
from typing import Any

from app.config import DB_PATH

SCHEMA = """
CREATE TABLE IF NOT EXISTS call (
    call_id TEXT PRIMARY KEY,
    recording_url TEXT,
    has_recording INTEGER NOT NULL DEFAULT 0,
    disposition TEXT,
    disposition_group TEXT,
    is_call_answered TEXT,
    entered_on TEXT,
    lead_id TEXT,
    raw_json TEXT,
    audio_path TEXT,
    duration_s REAL,
    channels INTEGER,
    language_code TEXT,
    language_probability REAL,
    speaker_method TEXT,
    status TEXT NOT NULL DEFAULT 'imported',
    error TEXT
);
CREATE INDEX IF NOT EXISTS ix_call_status ON call(status);
CREATE INDEX IF NOT EXISTS ix_call_disposition ON call(disposition);

CREATE TABLE IF NOT EXISTS utterance (
    call_id TEXT NOT NULL, idx INTEGER NOT NULL, speaker TEXT NOT NULL,
    start_s REAL, end_s REAL, text TEXT NOT NULL,
    PRIMARY KEY (call_id, idx)
);
CREATE TABLE IF NOT EXISTS signal (
    call_id TEXT NOT NULL, type TEXT NOT NULL, utterance_idx INTEGER, detail TEXT
);
CREATE INDEX IF NOT EXISTS ix_signal_call ON signal(call_id, type);

CREATE TABLE IF NOT EXISTS grade (
    call_id TEXT PRIMARY KEY,
    grade TEXT NOT NULL, fatal_code TEXT, reason TEXT, failure_category TEXT,
    root_cause_hint TEXT, outcome TEXT, who_ended_call TEXT,
    risk_flags TEXT, requirement TEXT, disposition_check TEXT, non_fatal_issues TEXT,
    confidence REAL, needs_review INTEGER NOT NULL DEFAULT 0,
    prompt_hash TEXT, raw_json TEXT, created_at TEXT DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS ix_grade_grade ON grade(grade);
CREATE TABLE IF NOT EXISTS evidence (
    call_id TEXT NOT NULL, utterance_idx INTEGER, quote TEXT, supports TEXT, start_s REAL
);
CREATE INDEX IF NOT EXISTS ix_evidence_call ON evidence(call_id);

CREATE TABLE IF NOT EXISTS human_label (
    call_id TEXT NOT NULL, labeller TEXT NOT NULL, grade TEXT NOT NULL, fatal_code TEXT,
    note TEXT, is_holdout INTEGER NOT NULL DEFAULT 0, created_at TEXT DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (call_id, labeller)
);
CREATE TABLE IF NOT EXISTS root_cause (
    id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT, description TEXT, category TEXT,
    call_count INTEGER, frequency REAL, severity REAL, priority REAL,
    created_at TEXT DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE IF NOT EXISTS root_cause_call (root_cause_id INTEGER NOT NULL, call_id TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS bot_prompt (
    id INTEGER PRIMARY KEY AUTOINCREMENT, text TEXT, source TEXT, parent_id INTEGER,
    created_at TEXT DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE IF NOT EXISTS prompt_fix (
    id INTEGER PRIMARY KEY AUTOINCREMENT, root_cause_id INTEGER, base_prompt_id INTEGER,
    new_prompt_id INTEGER, diff TEXT, rationale TEXT, target_call_ids TEXT, status TEXT,
    created_at TEXT DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE IF NOT EXISTS fix_test (
    id INTEGER PRIMARY KEY AUTOINCREMENT, prompt_fix_id INTEGER, scenarios INTEGER, runs INTEGER,
    fail_before REAL, fail_after REAL, holdout_fail_before REAL, holdout_fail_after REAL,
    started_at TEXT, finished_at TEXT
);
CREATE TABLE IF NOT EXISTS sim_conversation (
    fix_test_id INTEGER, scenario TEXT, variant TEXT, run_idx INTEGER, transcript TEXT, grade TEXT
);
CREATE TABLE IF NOT EXISTS review_item (
    id INTEGER PRIMARY KEY AUTOINCREMENT, call_id TEXT NOT NULL, reason TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'open', note TEXT,
    UNIQUE (call_id, reason)
);
CREATE TABLE IF NOT EXISTS job (
    id INTEGER PRIMARY KEY AUTOINCREMENT, kind TEXT, status TEXT, total INTEGER DEFAULT 0,
    done INTEGER DEFAULT 0, errors INTEGER DEFAULT 0, started_at TEXT DEFAULT CURRENT_TIMESTAMP,
    finished_at TEXT, message TEXT
);
CREATE TABLE IF NOT EXISTS kv (key TEXT PRIMARY KEY, value TEXT, updated_at TEXT DEFAULT CURRENT_TIMESTAMP);
CREATE TABLE IF NOT EXISTS api_call (
    purpose TEXT, model TEXT, prompt_tokens INTEGER DEFAULT 0, completion_tokens INTEGER DEFAULT 0,
    latency_ms INTEGER, status TEXT, cost_inr REAL DEFAULT 0, created_at TEXT DEFAULT CURRENT_TIMESTAMP
);
"""

_conn: sqlite3.Connection | None = None


def conn() -> sqlite3.Connection:
    global _conn
    if _conn is None:
        DB_PATH.parent.mkdir(parents=True, exist_ok=True)
        _conn = sqlite3.connect(DB_PATH, timeout=30, isolation_level=None, check_same_thread=False)
        _conn.row_factory = sqlite3.Row
        _conn.execute("PRAGMA journal_mode=WAL")
        _conn.executescript(SCHEMA)
        _migrate(_conn)
    return _conn


# Columns added after the first version; applied to databases created before them.
_ADDED_COLUMNS = [
    ("call", "source", "TEXT NOT NULL DEFAULT 'csv'"),  # 'csv' = organisers' log, 'folder' = local recordings
    ("call", "original_name", "TEXT"),
    ("utterance", "raw_text", "TEXT"),  # Sarvam's text when the shown text was repaired
    ("api_call", "call_id", "TEXT"),  # which call the spend was for; NULL = shared (summaries, experiments)
]


def _migrate(c: sqlite3.Connection) -> None:
    for table, column, definition in _ADDED_COLUMNS:
        if column not in {row[1] for row in c.execute(f"PRAGMA table_info({table})")}:
            c.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")


def q(sql: str, *params: Any) -> list[sqlite3.Row]:
    return conn().execute(sql, params).fetchall()


def one(sql: str, *params: Any) -> sqlite3.Row | None:
    return conn().execute(sql, params).fetchone()


def val(sql: str, *params: Any) -> Any:
    row = conn().execute(sql, params).fetchone()
    return row[0] if row else None


def ex(sql: str, *params: Any) -> sqlite3.Cursor:
    return conn().execute(sql, params)


def many(sql: str, rows: Iterable[tuple[Any, ...]]) -> None:
    conn().executemany(sql, rows)


@contextmanager
def tx() -> Iterator[None]:
    """Group several writes so they land together or not at all."""
    c = conn()
    c.execute("BEGIN")
    try:
        yield
    except BaseException:
        c.execute("ROLLBACK")
        raise
    c.execute("COMMIT")


def placeholders(items: Iterable[Any]) -> str:
    return ",".join("?" for _ in items)


def loads(text: str | None, default: Any = None) -> Any:
    return json.loads(text) if text else default
