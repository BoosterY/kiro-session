"""Extractor — read-only structured data extraction from kiro-cli DB (Layer 0)."""
import sqlite3
import re
import time
from collections import Counter
from pathlib import Path

try:
    import orjson
    def json_loads(s): return orjson.loads(s)
    def json_dumps(o): return orjson.dumps(o).decode()
except ImportError:
    import json
    def json_loads(s): return json.loads(s)
    def json_dumps(o): return json.dumps(o)

import index_store as idx
from config import load_config, get

KIRO_DB = Path.home() / ".local" / "share" / "kiro-cli" / "data.sqlite3"
KIRO_SESSIONS_DIR = Path.home() / ".kiro" / "sessions" / "cli"

# Normalization for FTS: replace hyphens/underscores with spaces
_NORM_RE = re.compile(r"[-_./]")
_CJK_RE = re.compile(r"[\u4e00-\u9fff\u3400-\u4dbf]+")

try:
    import jieba
    jieba.setLogLevel(jieba.logging.WARNING)
    _HAS_JIEBA = True
except ImportError:
    _HAS_JIEBA = False


def normalize_text(text: str) -> str:
    text = _NORM_RE.sub(" ", text)
    if _HAS_JIEBA and _CJK_RE.search(text):
        text = " ".join(jieba.cut(text))
    return text


def kiro_connect() -> sqlite3.Connection:
    return sqlite3.connect(str(KIRO_DB), timeout=5)


def read_session_data(sid: str) -> dict | None:
    """Read full session data from either SQLite or JSONL source."""
    # Try SQLite first
    try:
        kiro = kiro_connect()
        row = kiro.execute(
            "SELECT value FROM conversations_v2 WHERE conversation_id = ?", (sid,)
        ).fetchone()
        if row:
            return json_loads(row[0])
    except Exception:
        pass

    # Try JSONL
    meta_file = KIRO_SESSIONS_DIR / f"{sid}.json"
    jsonl_file = KIRO_SESSIONS_DIR / f"{sid}.jsonl"
    if not meta_file.exists():
        return None

    import json as _json
    with open(meta_file) as f:
        meta = _json.load(f)

    # Read JSONL entries and convert to ConversationState format
    entries = []
    if jsonl_file.exists():
        with open(jsonl_file) as f:
            for line in f:
                entries.append(_json.loads(line))

    history = _jsonl_to_conversation_state(entries, meta)

    # Build ConversationState with template from SQLite if available
    template = _get_conversation_template()
    result = dict(template) if template else {
        "conversation_id": sid, "history": [], "transcript": [],
        "valid_history_range": [0, 0], "next_message": None,
    }
    result["conversation_id"] = sid
    result["history"] = history
    result["transcript"] = []
    result["valid_history_range"] = [0, len(history)]
    result["next_message"] = None
    result["latest_summary"] = None
    result["_meta"] = meta
    return result


def _jsonl_to_conversation_state(entries: list[dict], meta: dict) -> list[dict]:
    """Convert JSONL v1 wire entries to ConversationState history turns.

    JSONL pattern: Prompt → (AssistantMessage → ToolResults)* → AssistantMessage
    ConversationState: each entry = {user: {...}, assistant: {...}}

    Mapping:
      Prompt         → user.content = {"Prompt": {"prompt": text}}
      ToolResults    → user.content = {"ToolUseResults": {"tool_use_results": [...]}}
      AssistantMessage with toolUse → assistant = {"ToolUse": {...}}
      AssistantMessage without     → assistant = {"Response": {...}}
    """
    cwd = meta.get("cwd", "")
    history = []
    i = 0
    while i < len(entries):
        e = entries[i]
        kind = e.get("kind")
        d = e.get("data", {})

        if kind == "Prompt":
            # Extract prompt text
            prompt_text = ""
            for c in d.get("content", []):
                if c.get("kind") == "text":
                    prompt_text = c.get("data", "")
                    break
            ts = d.get("meta", {}).get("timestamp")
            timestamp = None
            if ts:
                from datetime import datetime, timezone
                try:
                    timestamp = datetime.fromtimestamp(ts, tz=timezone.utc).isoformat()
                except Exception:
                    pass

            user = {
                "additional_context": "",
                "env_context": {"env_state": {
                    "operating_system": "linux",
                    "current_working_directory": cwd,
                    "environment_variables": [],
                }},
                "content": {"Prompt": {"prompt": prompt_text}},
                "timestamp": timestamp,
                "images": None,
            }
            assistant = _next_assistant(entries, i + 1)
            history.append({"user": user, "assistant": assistant[0],
                            "request_metadata": _make_metadata(assistant[0])})
            i = assistant[1]

        elif kind == "ToolResults":
            results = []
            for c in d.get("content", []):
                if c.get("kind") == "toolResult":
                    td = c.get("data", {})
                    result_content = []
                    for rc in td.get("content", []):
                        if isinstance(rc, dict):
                            rcd = rc.get("data", "")
                            if isinstance(rcd, str):
                                result_content.append({"Text": rcd})
                            else:
                                import json as _j
                                result_content.append({"Text": _j.dumps(rcd, ensure_ascii=False)})
                        else:
                            result_content.append({"Text": str(rc)})
                    status = td.get("status", "success")
                    results.append({
                        "tool_use_id": td.get("toolUseId", ""),
                        "content": result_content,
                        "status": status.capitalize() if status else "Success",
                    })
            user = {
                "additional_context": "",
                "env_context": {"env_state": {
                    "operating_system": "linux",
                    "current_working_directory": cwd,
                    "environment_variables": [],
                }},
                "content": {"ToolUseResults": {"tool_use_results": results}},
                "timestamp": None,
                "images": None,
            }
            assistant = _next_assistant(entries, i + 1)
            history.append({"user": user, "assistant": assistant[0],
                            "request_metadata": _make_metadata(assistant[0])})
            i = assistant[1]

        elif kind == "Clear":
            i += 1
        else:
            i += 1

    return history


_TEMPLATE_CACHE = None


def _get_conversation_template() -> dict | None:
    """Get a ConversationState template from any existing SQLite session."""
    global _TEMPLATE_CACHE
    if _TEMPLATE_CACHE is not None:
        return _TEMPLATE_CACHE
    try:
        kiro = kiro_connect()
        row = kiro.execute("SELECT value FROM conversations_v2 LIMIT 1").fetchone()
        if row:
            data = json_loads(row[0])
            data["history"] = []
            data["transcript"] = []
            _TEMPLATE_CACHE = data
            return data
    except Exception:
        pass
    return None


def _make_metadata(assistant: dict) -> dict:
    """Build minimal request_metadata from assistant message."""
    mid = ""
    for key in ("Response", "ToolUse"):
        if key in assistant:
            mid = assistant[key].get("message_id", "")
            break
    return {
        "request_id": "", "message_id": mid, "context_usage_percentage": 0.0,
        "request_start_timestamp_ms": 0, "stream_end_timestamp_ms": 0,
        "time_to_first_chunk": {"secs": 0, "nanos": 0},
        "time_between_chunks": [],
        "user_prompt_length": 0, "response_size": 0,
        "chat_conversation_type": "ToolUse" if "ToolUse" in assistant else "NotToolUse",
        "tool_use_ids_and_names": [], "model_id": "", "message_meta_tags": [],
    }


def _next_assistant(entries: list[dict], start: int) -> tuple[dict, int]:
    """Find the next AssistantMessage from start index. Returns (assistant_dict, next_index)."""
    if start >= len(entries):
        return {"Response": {"message_id": "", "content": ""}}, start

    e = entries[start]
    if e.get("kind") != "AssistantMessage":
        return {"Response": {"message_id": "", "content": ""}}, start

    d = e.get("data", {})
    mid = d.get("message_id", "")
    content_parts = d.get("content", [])

    # Check if this message has tool uses
    tool_uses = []
    text_parts = []
    for c in content_parts:
        ck = c.get("kind", "")
        if ck == "toolUse":
            td = c.get("data", {})
            tool_uses.append({
                "id": td.get("toolUseId", ""),
                "name": td.get("name", ""),
                "orig_name": td.get("name", ""),
                "args": td.get("input", {}),
                "orig_args": td.get("input", {}),
            })
        elif ck == "text":
            text_parts.append(c.get("data", ""))

    text = "\n\n".join(text_parts)

    if tool_uses:
        return {"ToolUse": {"message_id": mid, "content": text, "tool_uses": tool_uses}}, start + 1
    else:
        return {"Response": {"message_id": mid, "content": text}}, start + 1


PRIVATE_DIR = Path.home() / ".kiro" / "skills" / "session-manager" / "private"


def _cleanup_private_dir():
    """Delete any sessions from the private sandbox directory (crash recovery)."""
    import subprocess
    private = str(PRIVATE_DIR)

    # SQLite
    try:
        kiro = kiro_connect()
        rows = kiro.execute(
            "SELECT conversation_id FROM conversations_v2 WHERE key LIKE ?",
            (private + "%",)
        ).fetchall()
        for (cid,) in rows:
            subprocess.run(["kiro-cli", "chat", "--delete-session", cid],
                           capture_output=True, timeout=10)
    except Exception:
        pass

    # JSONL
    import json as _json
    if KIRO_SESSIONS_DIR.exists():
        for meta_file in KIRO_SESSIONS_DIR.glob("*.json"):
            try:
                with open(meta_file) as f:
                    meta = _json.load(f)
                if meta.get("cwd", "").startswith(private):
                    subprocess.run(["kiro-cli", "chat", "--delete-session", meta["session_id"]],
                                   capture_output=True, timeout=10)
            except Exception:
                continue


def ensure_index_fresh(index_conn: sqlite3.Connection, progress_cb=None):
    """Layer 0 incremental index update. Returns number of sessions processed."""
    # Pre-scan: clean up any private sandbox sessions left by abnormal exit
    _cleanup_private_dir()

    cfg = load_config()
    exclude_dirs = set(get(cfg, "privacy.exclude_dirs") or [])
    exclude_ids = set(get(cfg, "privacy.exclude_sessions") or [])

    # --- Source 1: SQLite DB (legacy v1) ---
    kiro_sessions = {}
    _excluded_ids = []  # sessions to auto-purge from kiro DB
    try:
        kiro = kiro_connect()
        for cid, key, updated in kiro.execute(
            "SELECT conversation_id, key, updated_at FROM conversations_v2"
        ):
            if cid in exclude_ids:
                continue
            if any(key.startswith(d) for d in exclude_dirs):
                _excluded_ids.append(cid)
                continue
            kiro_sessions[cid] = {"directory": key, "updated_at": updated, "source": "sqlite"}
    except Exception:
        pass  # DB might not exist

    # --- Source 2: JSON/JSONL files (new v2) ---
    if KIRO_SESSIONS_DIR.exists():
        for meta_file in KIRO_SESSIONS_DIR.glob("*.json"):
            try:
                import json as _json
                with open(meta_file) as f:
                    meta = _json.load(f)
                sid = meta.get("session_id", "")
                if not sid or sid in exclude_ids:
                    continue
                cwd = meta.get("cwd", "")
                if any(cwd.startswith(d) for d in exclude_dirs):
                    _excluded_ids.append(sid)
                    continue
                updated = _parse_iso_timestamp(meta.get("updated_at", ""))
                if sid in kiro_sessions:
                    # Prefer newer updated_at
                    if updated and updated > kiro_sessions[sid]["updated_at"]:
                        kiro_sessions[sid] = {"directory": cwd, "updated_at": updated, "source": "jsonl"}
                else:
                    kiro_sessions[sid] = {"directory": cwd, "updated_at": updated or 0, "source": "jsonl"}
            except Exception:
                continue

    our_updated = idx.get_session_updated(index_conn)
    our_ids = set(our_updated.keys())
    kiro_ids = set(kiro_sessions.keys())

    # Detect external deletions
    deleted = our_ids - kiro_ids
    for sid in deleted:
        idx.delete_session(index_conn, sid)

    # Detect new or changed sessions
    to_process = []
    for cid, info in kiro_sessions.items():
        if cid not in our_updated or our_updated[cid] != info["updated_at"]:
            to_process.append(cid)

    if not to_process and not deleted:
        return 0

    # Process changed/new sessions
    total = len(to_process)
    for i, cid in enumerate(to_process):
        if progress_cb:
            progress_cb(i + 1, total)
        info = kiro_sessions[cid]
        if info["source"] == "sqlite":
            _process_sqlite_session(index_conn, cid, info)
        else:
            _process_jsonl_session(index_conn, cid, info)

    # Clean temp files for completed derivations
    _clean_temp_files(index_conn)

    # Auto-purge excluded sessions from kiro DB
    if _excluded_ids:
        import subprocess
        for cid in _excluded_ids:
            idx.delete_session(index_conn, cid)
            subprocess.run(
                ["kiro-cli", "chat", "--delete-session", cid],
                capture_output=True, timeout=10,
            )

    index_conn.commit()
    return total + len(deleted)


def _process_sqlite_session(conn, cid, info):
    """Process a session from SQLite DB."""
    kiro = kiro_connect()
    row = kiro.execute(
        "SELECT value, updated_at FROM conversations_v2 WHERE conversation_id = ?", (cid,)
    ).fetchone()
    if not row:
        return
    data = json_loads(row[0])
    if _is_llm_garbage(data):
        return
    _index_session(conn, cid, data, info["directory"], info["updated_at"])


def _process_jsonl_session(conn, cid, info):
    """Process a session from JSONL files."""
    import json as _json
    jsonl_file = KIRO_SESSIONS_DIR / f"{cid}.jsonl"
    meta_file = KIRO_SESSIONS_DIR / f"{cid}.json"
    if not jsonl_file.exists():
        return

    with open(meta_file) as f:
        meta = _json.load(f)

    turns = []
    files = []
    cmds = []
    user_turn_count = 0
    first_prompt = ""
    word_counter = Counter()
    fts_entries = []
    turn_index = 0

    with open(jsonl_file) as f:
        for line in f:
            entry = _json.loads(line)
            kind = entry.get("kind")
            data = entry.get("data", {})

            if kind == "Prompt":
                content = data.get("content", [])
                prompt = ""
                for c in content:
                    if c.get("kind") == "text":
                        prompt = c.get("data", "")
                        break
                if not prompt:
                    continue

                user_turn_count += 1
                if not first_prompt:
                    first_prompt = prompt

                for w in re.findall(r"\w{3,}", prompt.lower()):
                    word_counter[w] += 1

                env = data.get("env_state", {})
                cwd = env.get("current_working_directory", "")

                fts_entries.append({"turn_index": turn_index, "content": normalize_text(prompt[:10000])})
                turns.append({
                    "turn_index": turn_index,
                    "user_prompt": prompt[:5000],
                    "assistant_response": None,
                    "working_dir": cwd,
                    "files_touched": [],
                    "commands_run": [],
                    "tools_used": [],
                    "timestamp": _parse_iso_timestamp(data.get("timestamp")),
                })
                turn_index += 1

            elif kind == "AssistantMessage":
                content = data.get("content", [])
                response_text = ""
                for c in content:
                    ck = c.get("kind", "")
                    if ck == "text":
                        response_text += c.get("data", "")
                    elif ck == "toolUse":
                        tu = c.get("data", {})
                        tool_name = tu.get("name", "")
                        if tool_name:
                            _extract_tool_data_v2(tu, cid, turn_index - 1, files, cmds)

                if response_text and turns:
                    turns[-1]["assistant_response"] = response_text[:5000]
                    fts_entries.append({"turn_index": turn_index - 1, "content": normalize_text(response_text[:10000])})

    # Rebuild per-turn tools/files/commands
    turn_files = {}
    turn_cmds = {}
    for f_entry in files:
        ti = f_entry["turn_index"]
        turn_files.setdefault(ti, []).append(f_entry["file_path"])
    for c_entry in cmds:
        ti = c_entry["turn_index"]
        turn_cmds.setdefault(ti, []).append(c_entry["command"])
    for t in turns:
        ti = t["turn_index"]
        t["files_touched"] = turn_files.get(ti, [])
        t["commands_run"] = turn_cmds.get(ti, [])
        tools = set()
        if turn_files.get(ti):
            tools.add("fs_read")
        if turn_cmds.get(ti):
            tools.add("execute_bash")
        t["tools_used"] = list(tools)

    auto_tags = _infer_tags(files, cmds, info["directory"])
    keywords = [w for w, _ in word_counter.most_common(20) if w not in _STOP_WORDS][:10]
    name = meta.get("title") or _generate_name(first_prompt, keywords)

    existing = idx.get_session(conn, cid)

    idx.upsert_session(conn, cid,
        name=name,
        directory=info["directory"],
        created_at=_parse_iso_timestamp(meta.get("created_at")),
        updated_at=info["updated_at"],
        user_turn_count=user_turn_count,
        total_turn_count=len(turns),
        llm_enriched=2 if (existing and existing["llm_enriched"] == 1) else 0,
        auto_tags=json_dumps(auto_tags),
        keywords=json_dumps(keywords),
    )
    idx.replace_turns(conn, cid, turns)
    idx.replace_fts(conn, cid, fts_entries)
    idx.replace_files(conn, cid, files)
    idx.replace_commands(conn, cid, cmds)
    if existing and existing["llm_enriched"]:
        idx.replace_topics(conn, cid, [])


def _index_session(conn: sqlite3.Connection, sid: str, data: dict,
                   directory: str, updated_at: int):
    """Extract and index a single session."""
    history = data.get("history", [])
    transcript = data.get("transcript", [])

    # Was previously LLM enriched? Reset if content changed.
    existing = idx.get_session(conn, sid)

    # Extract turns
    turns = []
    files = []
    cmds = []
    all_tools = []
    user_turn_count = 0
    first_prompt = ""
    word_counter = Counter()

    for ti, turn in enumerate(history):
        user = turn.get("user", {})
        assistant = turn.get("assistant", {})

        # User prompt
        prompt = ""
        content = user.get("content", {})
        if isinstance(content, dict):
            p = content.get("Prompt", "")
            if isinstance(p, dict):
                prompt = p.get("prompt", "")
            elif isinstance(p, str):
                prompt = p
        elif isinstance(content, str):
            prompt = content
        if not isinstance(prompt, str):
            prompt = str(prompt) if prompt else ""

        if prompt:
            user_turn_count += 1
            if not first_prompt:
                first_prompt = prompt
            # Count words for keywords
            for w in re.findall(r"\w{3,}", prompt.lower()):
                word_counter[w] += 1

        # Working directory
        env = user.get("env_context", {})
        env_state = env.get("env_state", {}) if isinstance(env, dict) else {}
        cwd = env_state.get("current_working_directory", "")

        # Assistant response
        response = ""
        if isinstance(assistant, dict):
            resp = assistant.get("Response", {})
            if isinstance(resp, dict):
                response = resp.get("value", "")
            elif isinstance(resp, str):
                response = resp
            # Tool use extraction — ToolUse has tool_uses array
            tool_use = assistant.get("ToolUse", {})
            if isinstance(tool_use, dict):
                for tu in tool_use.get("tool_uses", []):
                    tool_name = tu.get("name", "")
                    if tool_name:
                        all_tools.append(tool_name)
                        _extract_tool_data(tu, sid, ti, files, cmds)

        turns.append({
            "turn_index": ti,
            "user_prompt": prompt[:5000] if prompt else None,
            "assistant_response": response[:5000] if response else None,
            "working_dir": cwd,
            "files_touched": [],
            "commands_run": [],
            "tools_used": [],
            "timestamp": _parse_timestamp(user.get("timestamp")),
        })

    # Rebuild per-turn tools_used from extraction
    turn_tools = {}
    for f in files:
        turn_tools.setdefault(f["turn_index"], set()).add("fs_read" if f["operation"] == "read" else "fs_write")
    for c in cmds:
        turn_tools.setdefault(c["turn_index"], set()).add("execute_bash")
    for t in turns:
        t["tools_used"] = list(turn_tools.get(t["turn_index"], []))
        t["files_touched"] = [f["file_path"] for f in files if f["turn_index"] == t["turn_index"]]
        t["commands_run"] = [c["command"] for c in cmds if c["turn_index"] == t["turn_index"]]

    # FTS content from turns (same source as turns table for consistent turn_index)
    fts_entries = []
    for t in turns:
        parts = []
        if t["user_prompt"]:
            parts.append(t["user_prompt"])
        if t["assistant_response"]:
            parts.append(t["assistant_response"])
        if parts:
            fts_entries.append({
                "turn_index": t["turn_index"],
                "content": normalize_text(" ".join(parts)[:10000]),
            })

    # Auto-tags from file types and commands
    auto_tags = _infer_tags(files, cmds, directory)

    # Keywords (top 10 by frequency, excluding common words)
    keywords = [w for w, _ in word_counter.most_common(20) if w not in _STOP_WORDS][:10]

    # Session name
    name = _generate_name(first_prompt, keywords)

    # Detect _kiro_session_source marker for derivation
    source_marker = data.get("_kiro_session_source")
    if source_marker and isinstance(source_marker, dict):
        _record_derivation(conn, sid, source_marker)

    # Write to index
    idx.upsert_session(conn, sid,
        name=name,
        directory=directory,
        created_at=_parse_timestamp(history[0]["user"].get("timestamp")) if history else updated_at,
        updated_at=updated_at,
        user_turn_count=user_turn_count,
        total_turn_count=len(history),
        llm_enriched=2 if (existing and existing["llm_enriched"] == 1) else 0,
        auto_tags=json_dumps(auto_tags),
        keywords=json_dumps(keywords),
    )
    idx.replace_turns(conn, sid, turns)
    idx.replace_fts(conn, sid, fts_entries)
    idx.replace_files(conn, sid, files)
    idx.replace_commands(conn, sid, cmds)

    # Clear topics if session was re-indexed (content changed)
    if existing and existing["llm_enriched"]:
        idx.replace_topics(conn, sid, [])


_LLM_GARBAGE_MARKERS = (
    "Analyze this conversation",
    "Merge these topic groups",
    "return ONLY a JSON",
)


def _is_llm_garbage(data: dict) -> bool:
    """Detect sessions created by our LLM enrichment calls."""
    history = data.get("history", [])
    if not history or len(history) > 3:
        return False
    user = history[0].get("user", {})
    content = user.get("content", {})
    prompt = ""
    if isinstance(content, dict):
        p = content.get("Prompt", "")
        if isinstance(p, dict):
            prompt = p.get("prompt", "")
        elif isinstance(p, str):
            prompt = p
    return any(m in prompt for m in _LLM_GARBAGE_MARKERS)


def _extract_tool_data(tool_use: dict, sid: str, turn_index: int,
                       files: list, cmds: list):
    """Extract file paths and commands from tool_use entry."""
    name = tool_use.get("name", "")
    inp = tool_use.get("args", {}) or tool_use.get("input", {})
    if not isinstance(inp, dict):
        return

    if name in ("fs_read", "read"):
        for op in inp.get("operations", []):
            if isinstance(op, dict):
                p = op.get("path", "")
                if p:
                    files.append({"turn_index": turn_index, "file_path": p, "operation": "read"})
        path = inp.get("path") or inp.get("file_path", "")
        if path and not inp.get("operations"):
            files.append({"turn_index": turn_index, "file_path": path, "operation": "read"})

    elif name in ("fs_write", "write"):
        path = inp.get("path", "")
        if path:
            op = inp.get("command", "create")
            files.append({"turn_index": turn_index, "file_path": path, "operation": op})

    elif name in ("execute_bash", "shell"):
        cmd = inp.get("command", "")
        if cmd:
            cmds.append({"turn_index": turn_index, "command": cmd[:500], "exit_code": None})

    elif name == "glob":
        pattern = inp.get("pattern", "")
        if pattern:
            files.append({"turn_index": turn_index, "file_path": pattern, "operation": "glob"})

    elif name == "grep":
        path = inp.get("path", "")
        if path:
            files.append({"turn_index": turn_index, "file_path": path, "operation": "grep"})


def _infer_tags(files: list, cmds: list, directory: str) -> list[str]:
    """Infer tags from file types, commands, and directory."""
    tags = set()
    ext_map = {
        ".py": "python", ".js": "javascript", ".ts": "typescript",
        ".rs": "rust", ".go": "golang", ".java": "java",
        ".yml": "yaml", ".yaml": "yaml", ".json": "json",
        ".md": "markdown", ".sh": "shell", ".bash": "shell",
        ".dockerfile": "docker", ".tf": "terraform",
        ".sql": "sql", ".html": "html", ".css": "css",
    }
    for f in files:
        path = f["file_path"].lower()
        if "dockerfile" in path or "docker-compose" in path:
            tags.add("docker")
        for ext, tag in ext_map.items():
            if path.endswith(ext):
                tags.add(tag)
    for c in cmds:
        cmd = c["command"].split()[0] if c["command"] else ""
        if cmd in ("git",):
            tags.add("git")
        elif cmd in ("docker", "docker-compose", "podman"):
            tags.add("docker")
        elif cmd in ("npm", "yarn", "pnpm"):
            tags.add("nodejs")
        elif cmd in ("pip", "pip3", "poetry"):
            tags.add("python")
    return sorted(tags)


def _generate_name(first_prompt: str, keywords: list[str]) -> str:
    """Generate session name from first prompt."""
    if first_prompt:
        # Take first line, strip whitespace, truncate
        name = first_prompt.strip().split("\n")[0][:80]
        return name
    return "(empty session)"


def _parse_timestamp(ts) -> int | None:
    if ts is None:
        return None
    if isinstance(ts, (int, float)):
        return int(ts)
    if isinstance(ts, str):
        return _parse_iso_timestamp(ts)
    return None


def _parse_iso_timestamp(ts) -> int | None:
    if not ts or not isinstance(ts, str):
        return None
    try:
        from datetime import datetime
        dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
        return int(dt.timestamp() * 1000)
    except Exception:
        return None


def _extract_tool_data_v2(tool_use: dict, sid: str, turn_index: int,
                          files: list, cmds: list):
    """Extract file paths and commands from JSONL toolUse entry."""
    name = tool_use.get("name", "")
    inp = tool_use.get("input", {})
    if not isinstance(inp, dict):
        return
    # Reuse same extraction logic
    _extract_tool_data({"name": name, "args": inp}, sid, turn_index, files, cmds)


def _record_derivation(conn: sqlite3.Connection, derived_id: str, marker: dict):
    source_id = marker.get("source_id")
    topic_index = marker.get("topic_index", 0)
    if not source_id:
        return
    # Determine root: check if source itself has a root
    existing_deriv = conn.execute(
        "SELECT root_session_id FROM derivations WHERE derived_session_id = ?",
        (source_id,),
    ).fetchone()
    root_id = existing_deriv[0] if existing_deriv else source_id
    idx.add_derivation(conn, source_id, topic_index, derived_id, root_id, int(time.time() * 1000))




def _clean_temp_files(conn: sqlite3.Connection):
    """Remove temp files for derivations that have been recorded, and stale resume files."""
    tmp_dir = Path.home() / ".kiro" / "tmp"
    if not tmp_dir.exists():
        return
    import time
    now = time.time()
    for f in tmp_dir.glob("*-topic-*.json"):
        parts = f.stem.split("-topic-")
        if len(parts) == 2:
            sid_prefix = parts[0]
            rows = conn.execute(
                "SELECT 1 FROM derivations WHERE source_session_id LIKE ?",
                (f"{sid_prefix}%",),
            ).fetchone()
            if rows:
                f.unlink(missing_ok=True)
    # Clean resume files older than 1 day
    for f in tmp_dir.glob("*-resume.json"):
        if now - f.stat().st_mtime > 86400:
            f.unlink(missing_ok=True)


# Common stop words to exclude from keywords
_STOP_WORDS = {
    "the", "and", "for", "that", "this", "with", "from", "are", "was", "were",
    "been", "have", "has", "had", "not", "but", "what", "all", "can", "her",
    "his", "one", "our", "out", "you", "your", "will", "would", "could",
    "should", "about", "which", "when", "make", "like", "just", "over",
    "such", "take", "than", "them", "very", "some", "into", "most", "other",
    "also", "back", "after", "use", "how", "its", "may", "then", "each",
    "these", "more", "need", "does", "here", "there", "where", "why",
    "的", "了", "是", "在", "我", "有", "和", "就", "不", "人", "都", "一",
    "个", "上", "也", "很", "到", "说", "要", "去", "你", "会", "着",
    "没有", "看", "好", "自己", "这", "他", "她", "它", "们", "那",
}
