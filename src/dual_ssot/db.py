"""SQLite (WAL mode) transactional storage layer for Domain SSOT and Global Personal SSOT."""

from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

from dual_ssot.models import DEFAULT_CORE_CANONICAL_KEYS


DOMAIN_SCHEMA_SQL = """
PRAGMA journal_mode=WAL;
PRAGMA synchronous=NORMAL;
PRAGMA foreign_keys=ON;
PRAGMA busy_timeout=5000;

CREATE TABLE IF NOT EXISTS ssot_meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS canonical_keys (
    topic_key TEXT PRIMARY KEY,
    origin TEXT NOT NULL CHECK(origin IN ('core_template', 'auto_broadcast', 'manual')),
    created_by_session TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS base_decisions (
    topic_key TEXT PRIMARY KEY,
    decision_value TEXT NOT NULL,
    rationale TEXT NOT NULL,
    evidence_json TEXT NOT NULL,
    leaf_files_json TEXT NOT NULL,
    pinned INTEGER NOT NULL DEFAULT 0,
    tier TEXT NOT NULL DEFAULT 'L1_HOT' CHECK(tier IN ('L1_HOT', 'L2_COLD')),
    version INTEGER NOT NULL DEFAULT 1,
    step_order INTEGER NOT NULL DEFAULT 1,
    updated_by_session TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS cold_history (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    topic_key TEXT NOT NULL,
    old_value TEXT NOT NULL,
    new_value TEXT NOT NULL,
    rationale TEXT NOT NULL,
    evidence_json TEXT NOT NULL,
    superseded_by_session TEXT NOT NULL,
    reason TEXT NOT NULL,
    step_order INTEGER NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS negative_knowledge (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    topic_key TEXT NOT NULL,
    rejected_hypothesis TEXT NOT NULL,
    chosen_alternative TEXT,
    rejection_reason TEXT NOT NULL,
    evidence_json TEXT NOT NULL,
    origin_session_id TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS sessions (
    session_id TEXT PRIMARY KEY,
    base_revision_at_init INTEGER NOT NULL,
    status TEXT NOT NULL CHECK(status IN (
        'ACTIVE',
        'PROMOTED',
        'CONFLICT_PENDING',
        'BRAINSTORM_ARCHIVED',
        'NEGATIVE_ARCHIVED'
    )),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS staged_decisions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT NOT NULL,
    topic_key TEXT NOT NULL,
    decision_value TEXT NOT NULL,
    rationale TEXT NOT NULL,
    evidence_json TEXT NOT NULL,
    file_scope_json TEXT NOT NULL,
    leaf_files_json TEXT NOT NULL,
    pinned INTEGER NOT NULL DEFAULT 0,
    scope_target TEXT NOT NULL DEFAULT 'domain',
    proof_json TEXT,
    starl_json TEXT,
    habit_json TEXT,
    stage_order INTEGER NOT NULL,
    status TEXT NOT NULL DEFAULT 'STAGED' CHECK(status IN ('STAGED', 'PROMOTED', 'ARCHIVED_BRAINSTORM', 'ARCHIVED_NEGATIVE')),
    updated_at TEXT NOT NULL,
    UNIQUE(session_id, topic_key)
);

CREATE TABLE IF NOT EXISTS staged_negatives (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT NOT NULL,
    topic_key TEXT NOT NULL,
    rejected_hypothesis TEXT NOT NULL,
    chosen_alternative TEXT,
    rejection_reason TEXT NOT NULL,
    evidence_json TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'STAGED' CHECK(status IN ('STAGED', 'PROMOTED')),
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS conflicts (
    conflict_id TEXT PRIMARY KEY,
    session_id TEXT NOT NULL,
    conflict_type TEXT NOT NULL,
    topic_key TEXT NOT NULL,
    incoming_value TEXT NOT NULL,
    existing_topic_key TEXT NOT NULL,
    existing_value TEXT NOT NULL,
    existing_session_id TEXT NOT NULL,
    existing_version INTEGER NOT NULL,
    overlapping_leaf_files_json TEXT NOT NULL,
    competing_sessions_json TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'OPEN' CHECK(status IN ('OPEN', 'RESOLVED')),
    resolution_action TEXT,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS transaction_log (
    tx_id TEXT PRIMARY KEY,
    session_id TEXT NOT NULL,
    operation TEXT NOT NULL,
    domain_snapshot_json TEXT NOT NULL,
    personal_snapshot_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    rolled_back INTEGER NOT NULL DEFAULT 0
);
"""

PERSONAL_SCHEMA_SQL = """
PRAGMA journal_mode=WAL;
PRAGMA synchronous=NORMAL;
PRAGMA foreign_keys=ON;
PRAGMA busy_timeout=5000;

CREATE TABLE IF NOT EXISTS starl_achievements (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    project_id TEXT NOT NULL,
    session_id TEXT NOT NULL,
    title TEXT NOT NULL,
    situation TEXT NOT NULL,
    task TEXT NOT NULL,
    action TEXT NOT NULL,
    result_metric TEXT NOT NULL,
    lesson TEXT NOT NULL,
    evidence_json TEXT NOT NULL,
    proof_json TEXT NOT NULL,
    tags_json TEXT NOT NULL,
    tx_id TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS coding_habits (
    habit_key TEXT PRIMARY KEY,
    rule_statement TEXT NOT NULL,
    rationale TEXT NOT NULL,
    projects_json TEXT NOT NULL,
    evidence_json TEXT NOT NULL,
    status TEXT NOT NULL CHECK(status IN ('CANDIDATE', 'ACTIVE')),
    min_projects_required INTEGER NOT NULL DEFAULT 2,
    tx_id TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
"""


def connect_sqlite(db_path: Path) -> sqlite3.Connection:
    """Opens a SQLite connection configured with Row factory and WAL concurrency."""
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db_path), timeout=10.0, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.execute("PRAGMA synchronous=NORMAL;")
    conn.execute("PRAGMA foreign_keys=ON;")
    conn.execute("PRAGMA busy_timeout=5000;")
    return conn


@contextmanager
def atomic_tx(conn: sqlite3.Connection) -> Iterator[sqlite3.Connection]:
    """Executes a block inside an IMMEDIATE transaction for deterministic MVCC locking."""
    conn.execute("BEGIN IMMEDIATE;")
    try:
        yield conn
        conn.execute("COMMIT;")
    except Exception:
        if conn.in_transaction:
            conn.execute("ROLLBACK;")
        raise


def initialize_domain_db(db_path: Path, project_id: str, now_iso: str) -> None:
    """Initializes Domain SSOT tables and seeds the 30 Strategy 3C core canonical keys."""
    conn = connect_sqlite(db_path)
    try:
        conn.executescript(DOMAIN_SCHEMA_SQL)
        with atomic_tx(conn):
            conn.execute(
                "INSERT OR IGNORE INTO ssot_meta (key, value) VALUES ('base_revision', '0')"
            )
            conn.execute(
                "INSERT OR IGNORE INTO ssot_meta (key, value) VALUES ('step_clock', '0')"
            )
            conn.execute(
                "INSERT OR IGNORE INTO ssot_meta (key, value) VALUES ('project_id', ?)",
                (project_id,),
            )
            for key in DEFAULT_CORE_CANONICAL_KEYS:
                conn.execute(
                    """
                    INSERT OR IGNORE INTO canonical_keys (topic_key, origin, created_by_session, created_at)
                    VALUES (?, 'core_template', 'system_init', ?)
                    """,
                    (key, now_iso),
                )
    finally:
        conn.close()


def initialize_personal_db(db_path: Path) -> None:
    """Initializes Global Personal SSOT SQLite tables."""
    conn = connect_sqlite(db_path)
    try:
        conn.executescript(PERSONAL_SCHEMA_SQL)
    finally:
        conn.close()
