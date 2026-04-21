"""Searcher — FTS5 fast mode + LLM smart mode with filters."""
import json
import sqlite3
import index_store as idx
from llm_provider import get_provider
from extractor import normalize_text


def search(conn: sqlite3.Connection, query: str = "", smart: bool = False,
           file_filter: str = "", cmd_filter: str = "",
           dir_filter: str = "", recent: str = "") -> list[dict]:
    """Unified search interface. Returns list of {session, snippet/explanation}."""
    # Build candidate set from filters
    candidates = _apply_filters(conn, file_filter, cmd_filter, dir_filter, recent)

    if smart:
        return _smart_search(conn, query, candidates)
    return _fast_search(conn, query, candidates)


def _fast_search(conn: sqlite3.Connection, query: str,
                 candidates: set[str] | None) -> list[dict]:
    """FTS5 full-text search with snippets from original text."""
    if not query:
        # No query — just return filtered sessions
        sessions = idx.get_all_sessions(conn)
        if candidates is not None:
            sessions = [s for s in sessions if s["id"] in candidates]
        return [{"session": s, "snippet": ""} for s in sessions]

    normalized = normalize_text(query)
    # Build FTS match expression: prefix match on each term
    terms = normalized.split()
    match_expr = " AND ".join(f'"{t}"*' for t in terms if t.strip())
    if not match_expr:
        return []

    # FTS for search + ranking; get turn_index for snippet extraction
    rows = conn.execute(
        "SELECT session_id, turn_index, rank "
        "FROM fts_content WHERE fts_content MATCH ? ORDER BY rank LIMIT 200",
        (match_expr,),
    ).fetchall()

    # Group by session, pick the best-ranked turn_index per session
    seen = {}
    for sid, turn_idx, rank in rows:
        if candidates is not None and sid not in candidates:
            continue
        if sid not in seen:
            seen[sid] = (turn_idx, rank)

    # Build snippets from original text in turns table
    raw_terms = query.lower().split()
    results = []
    for sid, (turn_idx, _rank) in seen.items():
        session = idx.get_session(conn, sid)
        if not session:
            continue
        # Get original text for this turn
        row = conn.execute(
            "SELECT user_prompt, assistant_response FROM turns "
            "WHERE session_id = ? AND turn_index = ?", (sid, turn_idx)
        ).fetchone()
        snippet = ""
        if row:
            snippet = _extract_snippet(row[0] or "", row[1] or "", raw_terms)
        results.append({"session": session, "snippet": snippet})
    return results


def _extract_snippet(user_text: str, assistant_text: str, terms: list[str],
                     context_chars: int = 60, max_len: int = 150) -> str:
    """Extract a snippet from original text around the first matching term."""
    combined = user_text + "\n" + assistant_text
    text_lower = combined.lower()

    # Find the best match position (earliest term occurrence)
    best_pos = -1
    best_term = ""
    for t in terms:
        pos = text_lower.find(t)
        if pos >= 0 and (best_pos < 0 or pos < best_pos):
            best_pos = pos
            best_term = t

    if best_pos < 0:
        # Fallback: return start of text
        return combined[:max_len].replace("\n", " ").strip()

    # Extract window around match
    start = max(0, best_pos - context_chars)
    end = min(len(combined), best_pos + len(best_term) + context_chars)
    snippet = combined[start:end].replace("\n", " ").strip()

    # Add ellipsis
    if start > 0:
        snippet = "..." + snippet
    if end < len(combined):
        snippet = snippet + "..."

    # Highlight all matching terms (case-insensitive)
    for t in terms:
        import re
        snippet = re.sub(re.escape(t), lambda m: f">>>{m.group(0)}<<<", snippet, flags=re.IGNORECASE)

    return snippet


def _smart_search(conn: sqlite3.Connection, query: str,
                  candidates: set[str] | None) -> list[dict]:
    """LLM-based semantic search."""
    provider = get_provider()
    if not provider.is_available() or provider.name == "NoneProvider":
        # Fallback to fast search
        return _fast_search(conn, query, candidates)

    sessions = idx.get_all_sessions(conn)
    if candidates is not None:
        sessions = [s for s in sessions if s["id"] in candidates]

    if not sessions:
        return []

    # Build compact summaries
    summaries = []
    for s in sessions:
        topics = idx.get_topics(conn, s["id"])
        topic_str = ", ".join(t["title"] for t in topics) if topics else ""
        tags = json.loads(s.get("auto_tags", "[]")) + json.loads(s.get("user_tags", "[]"))
        tag_str = ", ".join(tags) if tags else ""
        line = f'{s["id"][:8]} | {s["name"]} | topics: {topic_str} | tags: {tag_str} | {s["user_turn_count"]} turns'
        summaries.append(line)

    prompt = (
        "Given these session summaries, find sessions relevant to the query.\n"
        "Return ONLY a JSON array of objects: [{\"id\": \"<8-char-id>\", \"explanation\": \"<why relevant>\"}]\n"
        "Return empty array [] if nothing matches. No markdown.\n\n"
        f"Query: {query}\n\n"
        f"Sessions:\n" + "\n".join(summaries)
    )

    response = provider.query(prompt, timeout=30)
    if not response:
        return _fast_search(conn, query, candidates)

    try:
        # Extract JSON from response
        import re
        match = re.search(r"\[.*\]", response, re.DOTALL)
        if not match:
            return _fast_search(conn, query, candidates)
        matches = json.loads(match.group())
    except (json.JSONDecodeError, ValueError):
        return _fast_search(conn, query, candidates)

    results = []
    for m in matches:
        mid = m.get("id", "")
        # Find full session by prefix
        for s in sessions:
            if s["id"].startswith(mid):
                results.append({"session": s, "snippet": m.get("explanation", "")})
                break
    return results


def _apply_filters(conn: sqlite3.Connection, file_filter: str, cmd_filter: str,
                   dir_filter: str, recent: str) -> set[str] | None:
    """Apply structured filters, return candidate session IDs or None (no filter)."""
    filters_active = any([file_filter, cmd_filter, dir_filter, recent])
    if not filters_active:
        return None

    candidates = None

    if file_filter:
        rows = conn.execute(
            "SELECT DISTINCT session_id FROM files_used WHERE file_path LIKE ?",
            (f"%{file_filter}%",),
        ).fetchall()
        ids = {r[0] for r in rows}
        candidates = ids if candidates is None else candidates & ids

    if cmd_filter:
        rows = conn.execute(
            "SELECT DISTINCT session_id FROM commands WHERE command LIKE ?",
            (f"%{cmd_filter}%",),
        ).fetchall()
        ids = {r[0] for r in rows}
        candidates = ids if candidates is None else candidates & ids

    if dir_filter:
        rows = conn.execute(
            "SELECT id FROM sessions WHERE directory LIKE ? OR directory LIKE ?",
            (f"%{dir_filter}%", f"%/{dir_filter}"),
        ).fetchall()
        ids = {r[0] for r in rows}
        candidates = ids if candidates is None else candidates & ids

    if recent:
        import time, re
        m = re.match(r"(\d+)([dhm])", recent)
        if m:
            val, unit = int(m.group(1)), m.group(2)
            multiplier = {"d": 86400, "h": 3600, "m": 60}[unit]
            cutoff = int((time.time() - val * multiplier) * 1000)
            rows = conn.execute(
                "SELECT id FROM sessions WHERE updated_at > ?", (cutoff,)
            ).fetchall()
            ids = {r[0] for r in rows}
            candidates = ids if candidates is None else candidates & ids

    return candidates if candidates is not None else set()
