"""Searcher — Hybrid search: FTS5 keyword + embedding semantic + RRF merge."""
import json
import re
import sqlite3
import time

import numpy as np
import embed
import index_store as idx
from extractor import normalize_text


def search(conn: sqlite3.Connection, query: str = "",
           file_filter: str = "", cmd_filter: str = "",
           dir_filter: str = "", recent: str = "") -> list[dict]:
    """Unified search interface. Returns list of {session, snippet/explanation}."""
    candidates = _apply_filters(conn, file_filter, cmd_filter, dir_filter, recent)
    return _hybrid_search(conn, query, candidates)


def _hybrid_search(conn: sqlite3.Connection, query: str,
                   candidates: set[str] | None) -> list[dict]:
    """Hybrid search: FTS5 + embedding semantic, merged with RRF."""
    if not query:
        sessions = idx.get_all_sessions(conn)
        if candidates is not None:
            sessions = [s for s in sessions if s["id"] in candidates]
        return [{"session": s, "snippet": ""} for s in sessions]

    fts_ranked = _fts_search(conn, query, candidates)
    sem_ranked = _semantic_search(conn, query, candidates)

    if not sem_ranked:
        return _build_results(conn, fts_ranked, query)

    merged = _rrf_merge(fts_ranked, sem_ranked)
    return _build_results(conn, merged, query)


def _fts_search(conn: sqlite3.Connection, query: str,
                candidates: set[str] | None) -> list[tuple[str, int]]:
    """FTS5 keyword search. Returns [(session_id, turn_index), ...] ranked."""
    normalized = normalize_text(query)
    terms = normalized.split()
    safe_terms = [t.replace('"', '').strip() for t in terms]
    match_expr = " AND ".join(f'"{t}"*' for t in safe_terms if t)
    if not match_expr:
        return []

    rows = conn.execute(
        "SELECT session_id, turn_index, rank "
        "FROM fts_content WHERE fts_content MATCH ? ORDER BY rank LIMIT 200",
        (match_expr,),
    ).fetchall()

    seen = {}
    for sid, turn_idx, rank in rows:
        if candidates is not None and sid not in candidates:
            continue
        if sid not in seen:
            seen[sid] = turn_idx
    return list(seen.items())


def _semantic_search(conn: sqlite3.Connection, query: str,
                     candidates: set[str] | None, top_k: int = 50) -> list[tuple[str, int]]:
    """Embedding cosine similarity search. Returns [(session_id, turn_index), ...] ranked."""
    all_emb = idx.get_all_embeddings(conn)
    if not all_emb:
        return []

    model = embed.get_model()
    query_vec = np.array(list(model.embed([query]))[0], dtype=np.float32)
    query_norm = query_vec / (np.linalg.norm(query_vec) + 1e-9)

    scores = []
    for sid, turn_idx, blob in all_emb:
        if candidates is not None and sid not in candidates:
            continue
        vec = np.frombuffer(blob, dtype=np.float32)
        sim = float(np.dot(query_norm, vec / (np.linalg.norm(vec) + 1e-9)))
        scores.append((sid, turn_idx, sim))

    scores.sort(key=lambda x: x[2], reverse=True)

    # Deduplicate: best turn per session
    seen = {}
    for sid, turn_idx, sim in scores[:top_k]:
        if sid not in seen:
            seen[sid] = turn_idx
    return list(seen.items())


def _rrf_merge(fts_ranked: list[tuple[str, int]], sem_ranked: list[tuple[str, int]],
               k: int = 60) -> list[tuple[str, int]]:
    """Reciprocal Rank Fusion. Returns merged [(session_id, turn_index), ...]."""
    scores = {}  # sid -> (rrf_score, best_turn_idx)

    for rank, (sid, ti) in enumerate(fts_ranked):
        rrf = 1.0 / (k + rank + 1)
        if sid not in scores or rrf > scores[sid][0]:
            scores[sid] = (rrf, ti)

    for rank, (sid, ti) in enumerate(sem_ranked):
        rrf = 1.0 / (k + rank + 1)
        if sid in scores:
            old_score, old_ti = scores[sid]
            scores[sid] = (old_score + rrf, old_ti)
        else:
            scores[sid] = (rrf, ti)

    ranked = sorted(scores.items(), key=lambda x: x[1][0], reverse=True)
    return [(sid, ti) for sid, (_, ti) in ranked]


def _build_results(conn: sqlite3.Connection, ranked: list[tuple[str, int]],
                   query: str) -> list[dict]:
    """Build result dicts with snippets from ranked (session_id, turn_index) pairs."""
    raw_terms = query.lower().split()
    results = []
    for sid, turn_idx in ranked:
        session = idx.get_session(conn, sid)
        if not session:
            continue
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
        snippet = re.sub(re.escape(t), lambda m: f">>>{m.group(0)}<<<", snippet, flags=re.IGNORECASE)

    return snippet


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
