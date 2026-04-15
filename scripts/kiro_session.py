# /// script
# requires-python = ">=3.10"
# dependencies = [
#   "pick>=2.3.2",
# ]
# ///
"""kiro-session: Interactive session manager for Kiro CLI."""

import argparse
import json
import os
import platform
import re
import sqlite3
import subprocess
import sys
import time
import uuid
from pathlib import Path

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

INDEX_PATH = Path.home() / ".kiro" / "session-index.json"

def _db_path() -> Path:
    if platform.system() == "Darwin":
        return Path.home() / "Library" / "Application Support" / "kiro-cli" / "data.sqlite3"
    return Path.home() / ".local" / "share" / "kiro-cli" / "data.sqlite3"

# ---------------------------------------------------------------------------
# DB helpers
# ---------------------------------------------------------------------------

def db_connect():
    p = _db_path()
    if not p.exists():
        print(f"Error: Kiro CLI database not found at {p}", file=sys.stderr)
        sys.exit(1)
    return sqlite3.connect(str(p))

def db_fetch_sessions(conn) -> list[dict]:
    """Return lightweight metadata for every session (no full value)."""
    cur = conn.execute(
        "SELECT key, conversation_id, created_at, updated_at, length(value) "
        "FROM conversations_v2 ORDER BY updated_at DESC"
    )
    return [
        {"dir": r[0], "id": r[1], "created_at": r[2], "updated_at": r[3], "size": r[4]}
        for r in cur.fetchall()
    ]

def db_fetch_conversation(conn, conv_id: str) -> dict | None:
    cur = conn.execute(
        "SELECT key, value FROM conversations_v2 WHERE conversation_id = ?", (conv_id,)
    )
    row = cur.fetchone()
    if not row:
        return None
    return {"dir": row[0], "data": json.loads(row[1])}

def db_insert_conversation(conn, directory: str, conv_id: str, value: dict):
    now = int(time.time() * 1000)
    conn.execute(
        "INSERT INTO conversations_v2 (key, conversation_id, value, created_at, updated_at) "
        "VALUES (?, ?, ?, ?, ?)",
        (directory, conv_id, json.dumps(value), now, now),
    )

# ---------------------------------------------------------------------------
# Index management
# ---------------------------------------------------------------------------

def load_index() -> dict:
    if INDEX_PATH.exists():
        return json.loads(INDEX_PATH.read_text())
    return {"version": 1, "sessions": {}, "last_audit": None, "audit_interval_days": 7}

def save_index(index: dict):
    INDEX_PATH.parent.mkdir(parents=True, exist_ok=True)
    INDEX_PATH.write_text(json.dumps(index, indent=2, ensure_ascii=False))

def _extract_user_prompts(history: list) -> list[str]:
    """Extract only actual user prompts from history (skip ToolUseResults)."""
    prompts = []
    for turn in history:
        user = turn.get("user", {})
        content = user.get("content", {})
        if isinstance(content, dict) and "Prompt" in content:
            p = content["Prompt"]
            if isinstance(p, dict):
                prompts.append(p.get("prompt", ""))
            else:
                prompts.append(str(p))
    return prompts

def _extract_assistant_responses(history: list) -> list[str]:
    """Extract assistant text responses."""
    responses = []
    for turn in history:
        assistant = turn.get("assistant", {})
        if isinstance(assistant, dict):
            for key in ("Response", "ToolUse"):
                if key in assistant:
                    text = assistant[key].get("content", "")
                    if text:
                        responses.append(text)
                    break
    return responses

def _count_user_turns(history: list) -> int:
    return sum(
        1 for t in history
        if isinstance(t.get("user", {}).get("content", {}), dict)
        and "Prompt" in t.get("user", {}).get("content", {})
    )

def _auto_summarize(history: list) -> dict:
    """Generate a basic summary — first user prompt as name, key prompts as topics."""
    prompts = _extract_user_prompts(history)
    name = prompts[0][:80] if prompts else "Empty session"
    topics = []
    for i, p in enumerate(prompts[:10]):
        topics.append({"title": p[:100], "turn_index": i})
    return {"name": name, "topics": topics}

def _llm_query(query: str, timeout: int = 60) -> str | None:
    """Call kiro-cli headless and return stripped output. Cleans up leftover sessions."""
    marker = f"__kiro_session_tmp_{uuid.uuid4().hex[:12]}__"
    tagged_query = f"[{marker}] {query}"
    try:
        result = subprocess.run(
            ["kiro-cli", "chat", "--no-interactive", "--trust-all-tools", tagged_query],
            capture_output=True, text=True, timeout=timeout,
            stdin=subprocess.DEVNULL,
        )
        output = result.stdout
    except (subprocess.TimeoutExpired, FileNotFoundError):
        output = None
    # Delete only the session containing our unique marker
    conn = db_connect()
    cur = conn.execute(
        "SELECT conversation_id FROM conversations_v2 WHERE value LIKE ?",
        (f"%{marker}%",),
    )
    for row in cur:
        conn.execute("DELETE FROM conversations_v2 WHERE conversation_id = ?", (row[0],))
    conn.commit()
    conn.close()
    if not output:
        return None
    output = re.sub(r'\x1b\[[0-9;]*[a-zA-Z]', '', output)
    output = re.sub(r'\x1b\[\?25[hl]', '', output)
    return output

def _llm_summarize(history: list, split_prefs: str = "") -> dict | None:
    """Use Kiro CLI headless mode to generate a better summary via LLM."""
    prompts = _extract_user_prompts(history)
    responses = _extract_assistant_responses(history)
    if not prompts:
        return None

    excerpt_parts = []
    for i, (p, r) in enumerate(zip(prompts[:15], responses[:15])):
        excerpt_parts.append(f"User[{i}]: {p[:200]}")
        excerpt_parts.append(f"Assistant[{i}]: {r[:200]}")
    excerpt = "\n".join(excerpt_parts)

    pref_line = f"\nUser's splitting preference: {split_prefs}\n" if split_prefs else ""

    query = (
        "Analyze this conversation excerpt and return ONLY a JSON object with:\n"
        '- "name": a concise name for this session (max 60 chars, in the conversation\'s language)\n'
        '- "topics": array of {"title": "...", "summary": "...", "turns": [indices]} '
        "grouping turns by semantic topic\n"
        "title: short label (max 80 chars). summary: 1-2 sentence description of what was discussed/done.\n"
        "Group turns by meaning and project context, NOT by sequential order. "
        "If a user returns to a previous topic later in the conversation, those turns should be "
        "grouped with the original topic. "
        "Only include multiple topics if there are clearly distinct subjects.\n"
        "Respond with raw JSON only, no markdown.\n"
        f"{pref_line}\n"
        f"Conversation ({len(prompts)} user turns):\n{excerpt}"
    )

    output = _llm_query(query)
    if not output:
        return None
    return _parse_json_from_output(output, required_keys=["name", "topics"])

def _parse_json_from_output(output: str, required_keys: list[str]) -> dict | None:
    """Extract a JSON object containing required keys from LLM output."""
    pattern = r'\{.*' + '.*'.join(f'"{k}"' for k in required_keys) + r'.*\}'
    match = re.search(pattern, output, re.DOTALL)
    if not match:
        return None
    raw = match.group()
    for end in range(len(raw), 0, -1):
        if raw[end-1] == '}':
            try:
                return json.loads(raw[:end])
            except json.JSONDecodeError:
                continue
    return None

def ensure_index_fresh(conn, index: dict, use_llm: bool = False) -> bool:
    """Update index for any sessions that are new or changed. Returns True if updated."""
    # Quick check: if max updated_at and count haven't changed, skip full scan
    cur = conn.execute("SELECT MAX(updated_at), COUNT(*) FROM conversations_v2")
    db_max_ts, db_count = cur.fetchone()
    idx_max_ts = max((s.get("updated_at", 0) for s in index["sessions"].values()), default=0)
    if db_max_ts == idx_max_ts and db_count == len(index["sessions"]):
        return False

    sessions_meta = db_fetch_sessions(conn)
    updated = False
    stale = []
    for s in sessions_meta:
        cid = s["id"]
        existing = index["sessions"].get(cid)
        if existing and existing.get("updated_at") == s["updated_at"]:
            continue
        stale.append((s, existing))

    if stale and use_llm:
        print(f"Indexing {len(stale)} session(s) with LLM...", file=sys.stderr)

    split_prefs = index.get("split_preferences", {}).get("derived_rules", "")

    for s, existing in stale:
        cid = s["id"]
        conv = db_fetch_conversation(conn, cid)
        if not conv:
            continue
        data = conv["data"]
        history = data.get("history", [])

        summary = None
        if use_llm and _count_user_turns(history) >= 2:
            summary = _llm_summarize(history, split_prefs=split_prefs)
            if summary:
                print(f"  ✔ [{_short_id(cid)}] {summary['name']}", file=sys.stderr)
        if not summary:
            summary = _auto_summarize(history)

        index["sessions"][cid] = {
            "name": summary["name"],
            "directory": s["dir"],
            "created_at": s["created_at"],
            "updated_at": s["updated_at"],
            "message_count": len(history),
            "user_turn_count": _count_user_turns(history),
            "size_bytes": s["size"],
            "topics": summary["topics"],
            "parent": existing.get("parent") if existing else None,
            "children": existing.get("children", []) if existing else [],
            "llm_indexed": summary is not None and use_llm,
        }
        updated = True
    # Remove sessions that no longer exist in DB
    db_ids = {s["id"] for s in sessions_meta}
    for cid in list(index["sessions"].keys()):
        if cid not in db_ids:
            del index["sessions"][cid]
            updated = True
    # Fix orphaned parent/children references
    for cid, info in index["sessions"].items():
        if info.get("parent") and info["parent"] not in index["sessions"]:
            info["parent"] = None
            updated = True
        if info.get("children"):
            clean = [c for c in info["children"] if c in index["sessions"]]
            if len(clean) != len(info["children"]):
                info["children"] = clean
                updated = True
    if updated:
        save_index(index)
    return updated

# ---------------------------------------------------------------------------
# Formatting helpers
# ---------------------------------------------------------------------------

def _ts_to_relative(ts_ms: int) -> str:
    if not ts_ms:
        return "unknown"
    diff = time.time() - ts_ms / 1000
    if diff < 60:
        return "just now"
    if diff < 3600:
        return f"{int(diff/60)}m ago"
    if diff < 86400:
        return f"{int(diff/3600)}h ago"
    return f"{int(diff/86400)}d ago"

def _short_id(conv_id: str) -> str:
    return conv_id[:8]

def _format_session_line(cid: str, info: dict) -> str:
    name = info["name"][:50]
    rel = _ts_to_relative(info.get("updated_at", 0))
    d = os.path.basename(info.get("directory", "")) or info.get("directory", "")
    children = info.get("children", [])
    parent = info.get("parent")

    if children:
        detail = f"{len(children)} children"
        llm_mark = "  "
    elif info.get("llm_indexed"):
        n_topics = len(info.get("topics", []))
        detail = f"{n_topics} topics"
        llm_mark = "  "
    else:
        turns = info.get("user_turn_count", info.get("message_count", 0))
        detail = f"{turns} turns"
        llm_mark = "⚡"

    origin_mark = f" [←{_short_id(parent)}]" if parent else ""
    return f"{llm_mark} {_short_id(cid)}  {name}  ({rel}, {detail}, {d}){origin_mark}"

# ---------------------------------------------------------------------------
# Subcommand: list
# ---------------------------------------------------------------------------

def cmd_list(args):
    conn = db_connect()
    index = load_index()
    ensure_index_fresh(conn, index)

    sessions = index["sessions"]
    if not sessions:
        print("No sessions found.")
        return

    # Apply filters
    items = list(sessions.items())
    if args.search:
        q = args.search.lower()
        if getattr(args, 'deep', False):
            # Full-text search in DB
            items = _fulltext_search(conn, q, sessions)
        else:
            # Fast: search index metadata only
            items = [(cid, info) for cid, info in items if q in info.get("name", "").lower()
                     or q in info.get("directory", "").lower()
                     or any(q in t.get("title", "").lower() for t in info.get("topics", []))]
    if args.dir:
        d = args.dir
        items = [(cid, info) for cid, info in items
                 if info.get("directory", "").startswith(d) or os.path.basename(info.get("directory", "")) == d]
    if args.recent:
        cutoff = time.time() * 1000 - _parse_duration(args.recent)
        items = [(cid, info) for cid, info in items if info.get("updated_at", 0) >= cutoff]

    # Sort by updated_at desc
    items.sort(key=lambda x: x[1].get("updated_at", 0), reverse=True)

    if not items:
        print("No sessions match the filter.")
        return

    # Plain mode
    if args.plain or not sys.stdout.isatty():
        for cid, info in items:
            print(_format_session_line(cid, info))
        return

    # Interactive mode
    from pick import pick
    while True:
        options = [_format_session_line(cid, info) for cid, info in items]
        selected, idx = pick(options, "Sessions (↑↓/jk navigate, Enter select, q quit)\n⚡= LLM Summary Pending", indicator="→", quit_keys=[ord('q')])
        if selected is None:
            return

        cid, info = items[idx]
        while True:
            action = _show_session_detail(cid, info, conn, index)
            if action == "refresh":
                info = index["sessions"].get(cid, info)
                continue
            break
        if action != "back":
            return

def _show_session_detail(cid: str, info: dict, conn, index: dict) -> str:
    """Show session detail and action menu. Returns 'back' to go back to list."""
    print(f"\n{'='*60}")
    print(f"Session: {info['name']}")
    print(f"ID:      {cid}")
    print(f"Dir:     {info.get('directory', 'N/A')}")
    print(f"Updated: {_ts_to_relative(info.get('updated_at', 0))}")
    print(f"Turns:   {info.get('user_turn_count', '?')} prompts")

    # Show lineage
    if info.get("parent"):
        parent_info = index["sessions"].get(info["parent"], {})
        print(f"Origin:  split from [{_short_id(info['parent'])}] {parent_info.get('name', '?')}")
    if info.get("children"):
        print("Children:")
        for child_id in info["children"]:
            child_info = index["sessions"].get(child_id, {})
            print(f"  └── [{_short_id(child_id)}] {child_info.get('name', '?')}")

    # Show topics
    topics = info.get("topics", [])
    if topics:
        print(f"\nTopics ({len(topics)}):")
        for i, t in enumerate(topics):
            print(f"  {i+1}. {t['title']}")

    print(f"{'='*60}")

    if not info.get("llm_indexed"):
        print("⚡ Basic index only. Run 'kiro-session index' for better summaries and split suggestions.")

    # Action menu
    if sys.stdout.isatty():
        print("\n  [r] Resume  — continue this conversation in Kiro CLI")
        print("  [s] Split   — break into topic-based sessions")
        print("  [v] Save    — export to JSON file")
        print("  [d] Delete  — remove this session from DB")
        if not info.get("llm_indexed"):
            print("  [i] Index   — generate LLM summary for this session (~5s)")
        print("  [b] Back    [q] Quit")
        choice = input("> ").strip().lower()
        if choice == "r":
            _resume_session(cid, info.get("directory", ""))
        elif choice == "s":
            _split_interactive(cid, conn, index)
        elif choice == "v":
            _save_session(cid, conn)
        elif choice == "d":
            _delete_session(cid, info, conn, index)
            return "back"
        elif choice == "i" and not info.get("llm_indexed"):
            _index_single_session(cid, info, conn, index)
            return "refresh"
        elif choice == "b":
            return "back"
    return "done"

def _resume_session(conv_id: str, directory: str):
    """Make this session the most recent so --resume picks it up."""
    conn = db_connect()
    now = int(time.time() * 1000)
    conn.execute(
        "UPDATE conversations_v2 SET updated_at = ? WHERE conversation_id = ?",
        (now, conv_id),
    )
    conn.commit()
    print(f"\nSession [{_short_id(conv_id)}] marked as most recent.")
    print(f"Resume in terminal:")
    print(f"  cd {directory} && kiro-cli chat --resume")
    print(f"Resume in TUI:")
    print(f"  cd {directory} && kiro-cli chat --resume --tui")

def _save_session(conv_id: str, conn):
    """Quick save a session to a JSON file."""
    conv = db_fetch_conversation(conn, conv_id)
    if not conv:
        print("Session not found.", file=sys.stderr)
        return
    path = f"session-{_short_id(conv_id)}.json"
    with open(path, "w") as f:
        json.dump(conv["data"], f, indent=2, ensure_ascii=False)
    print(f"✔ Saved to {path}")
    print(f"  Restore with: kiro-session restore {path}")

def _delete_session(cid: str, info: dict, conn, index: dict):
    """Delete a session from DB and index with confirmation."""
    confirm = input(f"Delete [{_short_id(cid)}] {info['name'][:50]}? [y/N] ").strip().lower()
    if confirm != "y":
        print("Cancelled.")
        return
    conn.execute("DELETE FROM conversations_v2 WHERE conversation_id = ?", (cid,))
    conn.commit()
    index["sessions"].pop(cid, None)
    save_index(index)
    print(f"  ✔ Deleted [{_short_id(cid)}]")

def _index_single_session(cid: str, info: dict, conn, index: dict):
    """Run LLM indexing on a single session."""
    print("Indexing with LLM...", end="", flush=True)
    conv = db_fetch_conversation(conn, cid)
    if not conv:
        print(" failed (session not found).")
        return
    history = conv["data"].get("history", [])
    split_prefs = index.get("split_preferences", {}).get("derived_rules", "")
    summary = _llm_summarize(history, split_prefs=split_prefs)
    if summary:
        info["name"] = summary["name"]
        info["topics"] = summary["topics"]
        info["llm_indexed"] = True
        save_index(index)
        print(f" done: {summary['name']}")
    else:
        print(" failed (LLM returned no result).")

def _parse_duration(s: str) -> int:
    """Parse duration string like '7d', '24h', '30m' to milliseconds."""
    s = s.strip().lower()
    if s.endswith("d"):
        return int(s[:-1]) * 86400 * 1000
    if s.endswith("h"):
        return int(s[:-1]) * 3600 * 1000
    if s.endswith("m"):
        return int(s[:-1]) * 60 * 1000
    return int(s) * 86400 * 1000  # default days

def _fulltext_search(conn, query: str, sessions: dict) -> list[tuple[str, dict]]:
    """Search full conversation content in DB using SQL LIKE."""
    cur = conn.execute(
        "SELECT conversation_id FROM conversations_v2 WHERE value LIKE ?",
        (f"%{query}%",),
    )
    return [(r[0], sessions[r[0]]) for r in cur if r[0] in sessions]

# ---------------------------------------------------------------------------
# Subcommand: index (LLM-powered, run in background)
# ---------------------------------------------------------------------------

def cmd_index(args):
    lock = Path.home() / ".kiro" / ".index-lock"
    if lock.exists():
        # Check if background is actively indexing (lock content = "active")
        # or still in sleep delay (lock content = "waiting")
        state = lock.read_text().strip() if lock.exists() else ""
        if state == "active":
            print("Background index is already running. Results will appear on next launch.", file=sys.stderr)
            return
        else:
            # Still in sleep delay — cancel it and take over
            lock.unlink(missing_ok=True)

    conn = db_connect()
    index = load_index()

    # Check for uncommitted pending splits from a previous crash
    _recover_pending_splits(conn, index)

    ensure_index_fresh(conn, index, use_llm=True)
    index["last_llm_index"] = int(time.time() * 1000)
    save_index(index)
    print("Index up to date.", file=sys.stderr)

    # Auto-split sessions with multiple topics
    _auto_split(conn, index)


def _has_split_groups(info: dict) -> bool:
    """Check if session has LLM topic groups suitable for splitting."""
    topics = info.get("topics", [])
    return len(topics) > 1 and isinstance(topics[0].get("turns"), list)

def _auto_split(conn, index: dict):
    """Automatically split sessions that have LLM-suggested topic groups."""
    candidates = [
        (cid, info) for cid, info in index["sessions"].items()
        if _has_split_groups(info)
        and not info.get("children") and not info.get("parent")
        and info.get("user_turn_count", 0) >= 4
    ]
    if not candidates:
        return

    print(f"\nAuto-splitting {len(candidates)} session(s)...", file=sys.stderr)
    for cid, info in candidates:
        conv = db_fetch_conversation(conn, cid)
        if not conv:
            continue
        data = conv["data"]
        history = data.get("history", [])
        prompts = _extract_user_prompts(history)
        topics = info.get("topics", [])

        prompt_to_history = []
        for hi, turn in enumerate(history):
            user = turn.get("user", {})
            content = user.get("content", {})
            if isinstance(content, dict) and "Prompt" in content:
                prompt_to_history.append(hi)

        segments = _build_segments_from_groups(topics, prompts, prompt_to_history, history)

        new_ids = [str(uuid.uuid4()) for _ in segments]
        index.setdefault("pending_splits", {})[cid] = new_ids
        save_index(index)

        children = info.get("children", [])
        for seg, new_id in zip(segments, new_ids):
            new_data = dict(data)
            new_data["conversation_id"] = new_id
            new_data["history"] = [history[hi] for hi in seg["history_indices"]]
            new_data["transcript"] = []
            new_data["latest_summary"] = None
            db_insert_conversation(conn, conv["dir"], new_id, new_data)
            children.append(new_id)

            index["sessions"][new_id] = {
                "name": seg["name"],
                "directory": conv["dir"],
                "created_at": int(time.time() * 1000),
                "updated_at": int(time.time() * 1000),
                "message_count": len(seg["history_indices"]),
                "user_turn_count": len(seg["turn_indices"]),
                "size_bytes": 0,
                "topics": [{"title": seg["name"], "turns": seg["turn_indices"]}],
                "parent": cid,
                "children": [],
            }

        index["sessions"][cid]["children"] = children
        conn.commit()
        del index["pending_splits"][cid]
        save_index(index)
        print(f"  ✔ [{_short_id(cid)}] → {len(segments)} sessions", file=sys.stderr)

    if not index.get("pending_splits"):
        index.pop("pending_splits", None)
        save_index(index)


def _recover_pending_splits(conn, index: dict):
    """Roll back any uncommitted splits from a previous crash."""
    pending = index.get("pending_splits", {})
    if not pending:
        return
    print(f"Recovering {len(pending)} uncommitted split(s)...", file=sys.stderr)
    for parent_id, child_ids in list(pending.items()):
        for child_id in child_ids:
            conn.execute("DELETE FROM conversations_v2 WHERE conversation_id = ?", (child_id,))
            index["sessions"].pop(child_id, None)
        conn.commit()
        # Clean parent's children list
        parent = index["sessions"].get(parent_id)
        if parent:
            parent["children"] = [c for c in parent.get("children", []) if c not in child_ids]
        print(f"  ↩ Rolled back split of [{_short_id(parent_id)}]", file=sys.stderr)
    index.pop("pending_splits", None)
    save_index(index)

# ---------------------------------------------------------------------------
# Subcommand: undo-split
# ---------------------------------------------------------------------------

def cmd_undo_split(args):
    conn = db_connect()
    index = load_index()
    ensure_index_fresh(conn, index)

    if args.id:
        cid = _resolve_session_id(args.id, index)
    else:
        # Pick from sessions that have children
        parents = {k: v for k, v in index["sessions"].items() if v.get("children")}
        if not parents:
            print("No split sessions found.")
            return
        cid = _pick_session_from(parents, "Select split session to undo:")
    if not cid:
        return

    info = index["sessions"].get(cid, {})
    children = info.get("children", [])
    if not children:
        print(f"Session [{_short_id(cid)}] has no children to undo.")
        return

    print(f"\nWill delete {len(children)} child session(s) from [{_short_id(cid)}]:")
    for child_id in children:
        child_info = index["sessions"].get(child_id, {})
        print(f"  └── [{_short_id(child_id)}] {child_info.get('name', '?')}")

    confirm = input("\nProceed? [y/N] ").strip().lower()
    if confirm != "y":
        print("Cancelled.")
        return

    for child_id in children:
        conn.execute("DELETE FROM conversations_v2 WHERE conversation_id = ?", (child_id,))
        if child_id in index["sessions"]:
            del index["sessions"][child_id]
        print(f"  ✔ Deleted [{_short_id(child_id)}]")
    conn.commit()

    index["sessions"][cid]["children"] = []
    save_index(index)
    print(f"\nUndo complete. Original session [{_short_id(cid)}] unchanged.")

def _pick_session_from(sessions: dict, title: str) -> str | None:
    items = sorted(sessions.items(), key=lambda x: x[1].get("updated_at", 0), reverse=True)
    if not sys.stdout.isatty():
        print("Interactive mode requires a terminal.", file=sys.stderr)
        return None
    from pick import pick
    options = [_format_session_line(cid, info) for cid, info in items]
    _, idx = pick(options, title, indicator="→", quit_keys=[ord('q')])
    if idx is None:
        return None
    return items[idx][0]

# ---------------------------------------------------------------------------
# Subcommand: split
# ---------------------------------------------------------------------------

def cmd_split(args):
    conn = db_connect()
    index = load_index()
    ensure_index_fresh(conn, index)

    # Select session
    if args.id:
        cid = _resolve_session_id(args.id, index)
    else:
        cid = _pick_session(index, "Select session to split:")
    if not cid:
        return

    _split_interactive(cid, conn, index)

def _split_interactive(cid: str, conn, index: dict):
    conv = db_fetch_conversation(conn, cid)
    if not conv:
        print("Session not found in DB.", file=sys.stderr)
        return

    data = conv["data"]
    history = data.get("history", [])
    prompts = _extract_user_prompts(history)

    if len(prompts) < 2:
        print("Session has too few turns to split.")
        return

    # Build prompt-to-history index
    prompt_to_history = []
    for hi, turn in enumerate(history):
        user = turn.get("user", {})
        content = user.get("content", {})
        if isinstance(content, dict) and "Prompt" in content:
            prompt_to_history.append(hi)

    # Get topic groups from LLM index
    info = index["sessions"].get(cid, {})
    topics = info.get("topics", [])

    # Check if topics have turn groups (new format) or just turn_index (old format)
    has_groups = topics and isinstance(topics[0].get("turns"), list)

    if not has_groups or len(topics) < 2:
        print("No split suggestion available. Run 'kiro-session index' first for LLM analysis.")
        return

    # Review loop
    while True:
        segments = _build_segments_from_groups(topics, prompts, prompt_to_history, history)
        _show_split_preview(segments, prompts)

        print("\n  [e] Execute this split")
        print("  [r] Retry — describe what to change and LLM will re-split")
        print("  [c] Cancel")
        choice = input("\n> ").strip().lower()

        if choice in ("c", "q"):
            print("Cancelled.")
            return
        if choice in ("e", ""):
            break
        if choice == "r":
            feedback = input("What should change? ").strip()
            if not feedback:
                continue
            _record_split_feedback(index, feedback)
            new_topics = _llm_resplit(prompts, topics, feedback)
            if new_topics:
                topics = new_topics
            else:
                print("LLM couldn't produce new grouping. Try again or execute current split.")

    # Execute split
    children = info.get("children", [])
    for seg in segments:
        new_id = str(uuid.uuid4())
        new_data = dict(data)
        new_data["conversation_id"] = new_id
        new_data["history"] = [history[hi] for hi in seg["history_indices"]]
        new_data["transcript"] = []
        new_data["latest_summary"] = None

        db_insert_conversation(conn, conv["dir"], new_id, new_data)
        children.append(new_id)

        index["sessions"][new_id] = {
            "name": seg["name"],
            "directory": conv["dir"],
            "created_at": int(time.time() * 1000),
            "updated_at": int(time.time() * 1000),
            "message_count": len(seg["history_indices"]),
            "user_turn_count": len(seg["turn_indices"]),
            "size_bytes": 0,
            "topics": [{"title": seg["name"], "turns": seg["turn_indices"]}],
            "parent": cid,
            "children": [],
        }
        print(f"  ✔ Created [{_short_id(new_id)}] {seg['name']}")

    if cid in index["sessions"]:
        index["sessions"][cid]["children"] = children
    conn.commit()
    save_index(index)
    print(f"\nOriginal session [{_short_id(cid)}] preserved. {len(segments)} new sessions created.")
    print(f"  Undo: kiro-session undo-split {cid[:8]}")


def _build_segments_from_groups(topics, prompts, prompt_to_history, history):
    """Build segments from semantic topic groups (non-contiguous turns supported)."""
    segments = []
    for topic in topics:
        turns = sorted(topic.get("turns", []))
        if not turns:
            continue
        # Map prompt indices to history indices, include assistant responses
        history_indices = []
        for t in turns:
            if t < len(prompt_to_history):
                h_start = prompt_to_history[t]
                h_end = prompt_to_history[t + 1] if t + 1 < len(prompt_to_history) else len(history)
                history_indices.extend(range(h_start, h_end))
        segments.append({
            "name": topic["title"],
            "summary": topic.get("summary", ""),
            "turn_indices": turns,
            "history_indices": sorted(set(history_indices)),
        })
    return segments


def _show_split_preview(segments, prompts):
    """Display a human-readable preview of the split."""
    print(f"\nSplit preview ({len(segments)} sessions):\n")
    for i, seg in enumerate(segments):
        n_turns = len(seg["turn_indices"])
        print(f"  {i+1}. \"{seg['name']}\" ({n_turns} turns)")
        if seg.get("summary"):
            print(f"     {seg['summary']}")


def _record_split_feedback(index: dict, feedback: str):
    """Record user feedback and derive preference rules when enough data."""
    prefs = index.setdefault("split_preferences", {"feedback_history": [], "derived_rules": ""})
    prefs["feedback_history"].append(feedback)
    # Keep last 10 feedbacks
    prefs["feedback_history"] = prefs["feedback_history"][-10:]

    # Derive rules after 3+ feedbacks
    if len(prefs["feedback_history"]) >= 3:
        history_text = "\n".join(f"- {f}" for f in prefs["feedback_history"])
        query = (
            "Based on these user feedbacks about conversation splitting, derive a concise "
            "preference rule (1-2 sentences) for future splits. Return ONLY a JSON object with "
            '"derived_rules": "...". Raw JSON only.\n\n'
            f"Feedbacks:\n{history_text}"
        )
        data = _parse_json_from_output(_llm_query(query, timeout=30) or "", required_keys=["derived_rules"])
        if data and "derived_rules" in data:
            prefs["derived_rules"] = data["derived_rules"]
            print(f"  📝 Updated split preference: {prefs['derived_rules']}", file=sys.stderr)
    save_index(index)


def _llm_resplit(prompts, current_topics, feedback):
    """Ask LLM to adjust topic groups based on user feedback."""
    prompt_list = "\n".join(f"[{i}] {p[:150]}" for i, p in enumerate(prompts[:20]))
    current_groups = json.dumps(current_topics, ensure_ascii=False)
    query = (
        f"Current topic groups: {current_groups}\n"
        f"User feedback: {feedback}\n\n"
        f"Conversation turns:\n{prompt_list}\n\n"
        "Return ONLY a JSON object with:\n"
        '- "topics": adjusted array of {"title": "...", "turns": [indices]} '
        "grouping turns by semantic topic. Respond with raw JSON only, no markdown."
    )
    data = _parse_json_from_output(_llm_query(query) or "", required_keys=["topics"])
    if data and data.get("topics"):
        topics = data["topics"]
        if all(isinstance(t.get("turns"), list) for t in topics):
            for t in topics:
                print(f"  → {t['title']}: turns {t['turns']}")
            return topics
    return None

# ---------------------------------------------------------------------------
# Subcommand: save / restore
# ---------------------------------------------------------------------------

def cmd_save(args):
    conn = db_connect()
    index = load_index()
    ensure_index_fresh(conn, index)

    cid = _resolve_session_id(args.id, index) if args.id else _pick_session(index, "Select session to save:")
    if not cid:
        return

    conv = db_fetch_conversation(conn, cid)
    if not conv:
        print("Session not found.", file=sys.stderr)
        sys.exit(1)

    info = index["sessions"].get(cid, {})
    default_name = info.get("name", cid)[:40].replace(" ", "_").replace("/", "_")
    path = args.path or f"session-{default_name}.json"

    if os.path.exists(path) and not args.force:
        confirm = input(f"File {path} exists. Overwrite? [y/N] ").strip().lower()
        if confirm != "y":
            print("Cancelled.")
            return

    with open(path, "w") as f:
        json.dump(conv["data"], f, indent=2, ensure_ascii=False)
    print(f"✔ Saved to {path}")
    print(f"  Restore with: kiro-session restore {path}")
    print(f"  Or load in Kiro: /chat load {path}")

def cmd_restore(args):
    if not os.path.exists(args.path):
        print(f"File not found: {args.path}", file=sys.stderr)
        sys.exit(1)

    with open(args.path) as f:
        data = json.load(f)

    conn = db_connect()
    # Use existing conversation_id or generate new one
    conv_id = data.get("conversation_id", str(uuid.uuid4()))

    # Check if already exists
    cur = conn.execute("SELECT 1 FROM conversations_v2 WHERE conversation_id = ?", (conv_id,))
    if cur.fetchone():
        if not args.force:
            confirm = input(f"Session {conv_id} already exists in DB. Overwrite? [y/N] ").strip().lower()
            if confirm != "y":
                print("Cancelled.")
                return
        conn.execute("DELETE FROM conversations_v2 WHERE conversation_id = ?", (conv_id,))

    directory = os.getcwd()
    db_insert_conversation(conn, directory, conv_id, data)
    conn.commit()
    print(f"✔ Restored session [{_short_id(conv_id)}] to DB (dir: {directory})")
    print(f"  Resume with: kiro-cli chat --resume")

# ---------------------------------------------------------------------------
# Subcommand: cleanup
# ---------------------------------------------------------------------------

def cmd_cleanup(args):
    conn = db_connect()
    index = load_index()
    ensure_index_fresh(conn, index)

    suggestions = []
    now_ms = int(time.time() * 1000)
    stale_days = args.stale_days or 30

    for cid, info in index["sessions"].items():
        age_days = (now_ms - info.get("updated_at", 0)) / (86400 * 1000)
        turns = info.get("user_turn_count", 0)

        # Stale + tiny sessions
        if age_days > stale_days and turns <= 1:
            suggestions.append((cid, info, f">{int(age_days)}d old, only {turns} turn(s)", "delete"))
        # Fully split parents — suggest after 7d undo grace period
        elif info.get("children") and not info.get("parent"):
            children = info["children"]
            all_exist = all(c in index["sessions"] for c in children)
            if all_exist and age_days > 30:
                size_kb = info.get("size_bytes", 0) // 1024
                suggestions.append((cid, info, f"split {len(children)}→ children, {size_kb}KB reclaimable", "archive"))

    if not suggestions:
        print("No cleanup suggestions. Everything looks good!")
        index["last_audit"] = int(time.time() * 1000)
        save_index(index)
        return

    print(f"Cleanup suggestions ({len(suggestions)}):\n")
    for i, (cid, info, reason, action) in enumerate(suggestions):
        print(f"  {i+1}. [{_short_id(cid)}] {info['name'][:50]}")
        print(f"     Reason: {reason} → suggested: {action}")

    if not sys.stdout.isatty():
        return

    print("\nEnter numbers to act on (comma-separated), or 'all', or 'q' to quit:")
    raw = input("> ").strip().lower()
    if raw in ("q", ""):
        return

    if raw == "all":
        selected = list(range(len(suggestions)))
    else:
        selected = [int(x.strip()) - 1 for x in raw.split(",")]

    for idx in selected:
        cid, info, reason, action = suggestions[idx]
        if action == "delete":
            conn.execute("DELETE FROM conversations_v2 WHERE conversation_id = ?", (cid,))
            conn.commit()
            del index["sessions"][cid]
            print(f"  ✔ Deleted [{_short_id(cid)}]")
        elif action == "archive":
            # Save to file before removing from DB
            conv = db_fetch_conversation(conn, cid)
            if conv:
                archive_dir = Path.home() / ".kiro" / "session-archive"
                archive_dir.mkdir(parents=True, exist_ok=True)
                archive_path = archive_dir / f"{cid}.json"
                archive_path.write_text(json.dumps(conv["data"], ensure_ascii=False))
                conn.execute("DELETE FROM conversations_v2 WHERE conversation_id = ?", (cid,))
                conn.commit()
                del index["sessions"][cid]
                print(f"  ✔ Archived [{_short_id(cid)}] → {archive_path}")

    index["last_audit"] = int(time.time() * 1000)
    save_index(index)
    print("\nDone.")

# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------

def _resolve_session_id(partial: str, index: dict) -> str | None:
    """Resolve a partial ID (prefix match) to full conversation_id."""
    matches = [cid for cid in index["sessions"] if cid.startswith(partial)]
    if len(matches) == 1:
        return matches[0]
    if len(matches) > 1:
        print(f"Ambiguous ID '{partial}', matches: {[_short_id(m) for m in matches]}", file=sys.stderr)
        return None
    print(f"No session found matching '{partial}'", file=sys.stderr)
    return None

def _pick_session(index: dict, title: str) -> str | None:
    items = sorted(index["sessions"].items(), key=lambda x: x[1].get("updated_at", 0), reverse=True)
    if not items:
        print("No sessions found.")
        return None
    if not sys.stdout.isatty():
        print("Interactive mode requires a terminal.", file=sys.stderr)
        return None
    from pick import pick
    options = [_format_session_line(cid, info) for cid, info in items]
    _, idx = pick(options, title, indicator="→", quit_keys=[ord('q')])
    if idx is None:
        return None
    return items[idx][0]

# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(prog="kiro-session", description="Interactive session manager for Kiro CLI")
    sub = parser.add_subparsers(dest="command")

    # list
    p_list = sub.add_parser("list", aliases=["ls"], help="List and browse sessions")
    p_list.add_argument("id", nargs="?", help="Show detail for specific session ID")
    p_list.add_argument("--search", "-s", help="Filter by keyword (searches index)")
    p_list.add_argument("--deep", action="store_true", help="Search full conversation content (slower)")
    p_list.add_argument("--dir", "-d", help="Filter by directory prefix")
    p_list.add_argument("--recent", "-r", help="Filter by recency (e.g. 7d, 24h)")
    p_list.add_argument("--plain", action="store_true", help="Non-interactive output")

    # index
    p_index = sub.add_parser("index", help="Build/refresh session index with LLM summaries (slow, run in background)")

    # undo-split
    p_undo = sub.add_parser("undo-split", help="Undo a split: delete child sessions, keep parent")
    p_undo.add_argument("id", nargs="?", help="Parent session ID")

    # split
    p_split = sub.add_parser("split", help="Split session into topic-based sessions")
    p_split.add_argument("id", nargs="?", help="Session ID to split")

    # save
    p_save = sub.add_parser("save", help="Export session to JSON file")
    p_save.add_argument("id", nargs="?", help="Session ID")
    p_save.add_argument("path", nargs="?", help="Output file path")
    p_save.add_argument("--force", "-f", action="store_true")

    # restore
    p_restore = sub.add_parser("restore", help="Restore session from JSON file")
    p_restore.add_argument("path", help="JSON file path")
    p_restore.add_argument("--force", "-f", action="store_true")

    # cleanup
    p_cleanup = sub.add_parser("cleanup", help="Review and clean up stale sessions")
    p_cleanup.add_argument("--stale-days", type=int, default=30, help="Days threshold for stale (default: 30)")

    args = parser.parse_args()

    if args.command in ("list", "ls", None):
        if not hasattr(args, "search"):
            # bare `kiro-session` with no subcommand — set defaults
            args.search = None
            args.dir = None
            args.recent = None
            args.plain = False
            args.id = None
            args.deep = False
        if args.id:
            # Show detail for specific session
            conn = db_connect()
            index = load_index()
            ensure_index_fresh(conn, index)
            cid = _resolve_session_id(args.id, index)
            if cid:
                _show_session_detail(cid, index["sessions"][cid], conn, index)
            return
        cmd_list(args)
    elif args.command == "split":
        cmd_split(args)
    elif args.command == "index":
        cmd_index(args)
    elif args.command == "undo-split":
        cmd_undo_split(args)
    elif args.command == "save":
        cmd_save(args)
    elif args.command == "restore":
        cmd_restore(args)
    elif args.command == "cleanup":
        cmd_cleanup(args)

if __name__ == "__main__":
    main()
