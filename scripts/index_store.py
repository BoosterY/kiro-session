"""Index store — SQLite schema and CRUD for kiro-session v2."""
import sqlite3
import json
from pathlib import Path

INDEX_DB = Path.home() / ".kiro" / "session-index.db"

SCHEMA = """
PRAGMA journal_mode=WAL;

CREATE TABLE IF NOT EXISTS sessions (
    id TEXT PRIMARY KEY,
    name TEXT,
    user_name TEXT,
    directory TEXT,
    created_at INTEGER,
    updated_at INTEGER,
    user_turn_count INTEGER DEFAULT 0,
    total_turn_count INTEGER DEFAULT 0,
    llm_enriched BOOLEAN DEFAULT 0,
    auto_tags TEXT DEFAULT '[]',
    user_tags TEXT DEFAULT '[]',
    keywords TEXT DEFAULT '[]'
);

CREATE TABLE IF NOT EXISTS turns (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT,
    turn_index INTEGER,
    user_prompt TEXT,
    assistant_response TEXT,
    working_dir TEXT,
    files_touched TEXT DEFAULT '[]',
    commands_run TEXT DEFAULT '[]',
    tools_used TEXT DEFAULT '[]',
    timestamp INTEGER
);
CREATE INDEX IF NOT EXISTS idx_turns_session ON turns(session_id);

CREATE TABLE IF NOT EXISTS files_used (
    session_id TEXT,
    turn_index INTEGER,
    file_path TEXT,
    operation TEXT
);
CREATE INDEX IF NOT EXISTS idx_files_session ON files_used(session_id);
CREATE INDEX IF NOT EXISTS idx_files_path ON files_used(file_path);

CREATE TABLE IF NOT EXISTS commands (
    session_id TEXT,
    turn_index INTEGER,
    command TEXT,
    exit_code INTEGER
);
CREATE INDEX IF NOT EXISTS idx_commands_session ON commands(session_id);

CREATE TABLE IF NOT EXISTS topics (
    session_id TEXT,
    topic_index INTEGER,
    title TEXT,
    summary TEXT,
    turn_indices TEXT
);
CREATE INDEX IF NOT EXISTS idx_topics_session ON topics(session_id);

CREATE TABLE IF NOT EXISTS derivations (
    source_session_id TEXT,
    topic_index INTEGER,
    derived_session_id TEXT,
    root_session_id TEXT,
    created_at INTEGER
);
CREATE INDEX IF NOT EXISTS idx_deriv_source ON derivations(source_session_id);
CREATE INDEX IF NOT EXISTS idx_deriv_derived ON derivations(derived_session_id);
CREATE INDEX IF NOT EXISTS idx_deriv_root ON derivations(root_session_id);
"""

FTS_SCHEMA = """
CREATE VIRTUAL TABLE IF NOT EXISTS fts_content USING fts5(
    session_id,
    turn_index,
    content,
    tokenize='unicode61'
);
"""


def connect() -> sqlite3.Connection:
    INDEX_DB.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(INDEX_DB))
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
    conn.executescript(FTS_SCHEMA)
    # Migrations
    try:
        conn.execute("ALTER TABLE sessions ADD COLUMN user_name TEXT")
    except sqlite3.OperationalError:
        pass  # column already exists
    return conn


# --- Sessions ---

def upsert_session(conn: sqlite3.Connection, sid: str, **fields):
    cols = list(fields.keys())
    placeholders = ", ".join(f"{c} = excluded.{c}" for c in cols)
    sql = (
        f"INSERT INTO sessions (id, {', '.join(cols)}) "
        f"VALUES (?, {', '.join('?' for _ in cols)}) "
        f"ON CONFLICT(id) DO UPDATE SET {placeholders}"
    )
    conn.execute(sql, [sid] + [fields[c] for c in cols])


def get_session(conn: sqlite3.Connection, sid: str) -> dict | None:
    row = conn.execute("SELECT * FROM sessions WHERE id = ?", (sid,)).fetchone()
    if not row:
        return None
    d = dict(row)
    if d.get("user_name"):
        d["name"] = d["user_name"]
    return d


def get_all_sessions(conn: sqlite3.Connection) -> list[dict]:
    rows = [dict(r) for r in conn.execute("SELECT * FROM sessions ORDER BY updated_at DESC")]
    for d in rows:
        if d.get("user_name"):
            d["name"] = d["user_name"]
    return rows


def get_session_ids(conn: sqlite3.Connection) -> set[str]:
    return {r[0] for r in conn.execute("SELECT id FROM sessions")}


def get_session_updated(conn: sqlite3.Connection) -> dict[str, int]:
    """Return {session_id: updated_at} for quick change detection."""
    return {r[0]: r[1] for r in conn.execute("SELECT id, updated_at FROM sessions")}


def delete_session(conn: sqlite3.Connection, sid: str):
    for table in ("sessions", "turns", "files_used", "commands", "topics"):
        col = "session_id" if table != "sessions" else "id"
        conn.execute(f"DELETE FROM {table} WHERE {col} = ?", (sid,))
    conn.execute("DELETE FROM fts_content WHERE session_id = ?", (sid,))
    conn.execute("DELETE FROM derivations WHERE derived_session_id = ? OR source_session_id = ?", (sid, sid))


# --- Turns ---

def replace_turns(conn: sqlite3.Connection, sid: str, turns: list[dict]):
    """Replace all turns for a session (used during re-index)."""
    conn.execute("DELETE FROM turns WHERE session_id = ?", (sid,))
    for t in turns:
        conn.execute(
            "INSERT INTO turns (session_id, turn_index, user_prompt, assistant_response, "
            "working_dir, files_touched, commands_run, tools_used, timestamp) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (sid, t["turn_index"], t.get("user_prompt"), t.get("assistant_response"),
             t.get("working_dir"), json.dumps(t.get("files_touched", [])),
             json.dumps(t.get("commands_run", [])), json.dumps(t.get("tools_used", [])),
             t.get("timestamp")),
        )


# --- FTS ---

def replace_fts(conn: sqlite3.Connection, sid: str, entries: list[dict]):
    """Replace FTS content for a session."""
    conn.execute("DELETE FROM fts_content WHERE session_id = ?", (sid,))
    for e in entries:
        conn.execute(
            "INSERT INTO fts_content (session_id, turn_index, content) VALUES (?, ?, ?)",
            (sid, e["turn_index"], e["content"]),
        )


def optimize_fts(conn: sqlite3.Connection):
    """Physically purge deleted FTS content from disk."""
    conn.execute("INSERT INTO fts_content(fts_content) VALUES('optimize')")


# --- Files / Commands ---

def replace_files(conn: sqlite3.Connection, sid: str, files: list[dict]):
    conn.execute("DELETE FROM files_used WHERE session_id = ?", (sid,))
    for f in files:
        conn.execute(
            "INSERT INTO files_used (session_id, turn_index, file_path, operation) VALUES (?, ?, ?, ?)",
            (sid, f["turn_index"], f["file_path"], f.get("operation")),
        )


def replace_commands(conn: sqlite3.Connection, sid: str, cmds: list[dict]):
    conn.execute("DELETE FROM commands WHERE session_id = ?", (sid,))
    for c in cmds:
        conn.execute(
            "INSERT INTO commands (session_id, turn_index, command, exit_code) VALUES (?, ?, ?, ?)",
            (sid, c["turn_index"], c["command"], c.get("exit_code")),
        )


# --- Topics ---

def replace_topics(conn: sqlite3.Connection, sid: str, topics: list[dict]):
    conn.execute("DELETE FROM topics WHERE session_id = ?", (sid,))
    for i, t in enumerate(topics):
        conn.execute(
            "INSERT INTO topics (session_id, topic_index, title, summary, turn_indices) "
            "VALUES (?, ?, ?, ?, ?)",
            (sid, i, t["title"], t.get("summary"), json.dumps(t.get("turns", []))),
        )


def get_topics(conn: sqlite3.Connection, sid: str) -> list[dict]:
    rows = conn.execute(
        "SELECT * FROM topics WHERE session_id = ? ORDER BY topic_index", (sid,)
    ).fetchall()
    return [dict(r) for r in rows]


# --- Derivations ---

def add_derivation(conn: sqlite3.Connection, source_id: str, topic_index: int,
                   derived_id: str, root_id: str, created_at: int):
    conn.execute(
        "INSERT INTO derivations (source_session_id, topic_index, derived_session_id, "
        "root_session_id, created_at) VALUES (?, ?, ?, ?, ?)",
        (source_id, topic_index, derived_id, root_id, created_at),
    )


def get_derivations_for_source(conn: sqlite3.Connection, source_id: str) -> list[dict]:
    rows = conn.execute(
        "SELECT * FROM derivations WHERE source_session_id = ?", (source_id,)
    ).fetchall()
    return [dict(r) for r in rows]


# --- Tags ---

def update_user_tags(conn: sqlite3.Connection, sid: str, tags: list[str]):
    conn.execute("UPDATE sessions SET user_tags = ? WHERE id = ?", (json.dumps(tags), sid))


def update_session_name(conn: sqlite3.Connection, sid: str, name: str):
    conn.execute("UPDATE sessions SET user_name = ? WHERE id = ?", (name, sid))


def get_all_tools_used(conn: sqlite3.Connection, sid: str) -> list[str]:
    """Get distinct tools used across all turns of a session."""
    rows = conn.execute(
        "SELECT DISTINCT value FROM turns, json_each(tools_used) WHERE session_id = ?",
        (sid,),
    ).fetchall()
    return [r[0] for r in rows]
