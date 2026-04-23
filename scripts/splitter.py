"""Splitter — LLM topic analysis, enrichment, and resume by topic."""
import json
import re
import time
import uuid
from pathlib import Path

import numpy as np
import embed
import index_store as idx
from llm_provider import get_provider

TMP_DIR = Path.home() / ".kiro" / "tmp"
EXCERPT_LIMIT = 80000  # chars; beyond this, use chunked multi-turn


def generate_embeddings(conn, sid: str):
    """Generate and store per-turn embeddings for a session."""
    turns = conn.execute(
        "SELECT turn_index, user_prompt FROM turns WHERE session_id = ? ORDER BY turn_index",
        (sid,),
    ).fetchall()
    texts = [(t[0], t[1]) for t in turns if t[1] and t[1].strip()]
    if not texts:
        return
    model = embed.get_model()
    turn_indices, prompts = zip(*texts)
    vectors = list(model.embed(list(prompts)))
    entries = []
    for ti, vec in zip(turn_indices, vectors):
        entries.append((ti, np.array(vec, dtype=np.float32).tobytes()))
    idx.replace_embeddings(conn, sid, entries)
    conn.commit()


def enrich_session(conn, sid: str, provider=None, feedback: str = "") -> bool:
    """Layer 1: LLM-enrich a single session. Returns True on success."""
    session = idx.get_session(conn, sid)
    if not session:
        return False

    provider = provider or get_provider()
    if provider.name == "NoneProvider":
        return False

    turns = conn.execute(
        "SELECT turn_index, user_prompt FROM turns WHERE session_id = ? ORDER BY turn_index",
        (sid,),
    ).fetchall()

    prompts = [(t[0], t[1]) for t in turns if t[1]]
    if not prompts:
        idx.upsert_session(conn, sid, llm_enriched=1)
        conn.commit()
        return True

    excerpt = _build_excerpt(prompts)
    if len(excerpt) <= EXCERPT_LIMIT:
        topics, name, tags = _analyze(excerpt, len(prompts), provider, feedback, conn, sid)
    else:
        topics, name, tags = _analyze_chunked(prompts, provider, feedback, conn, sid)

    if not name:
        return False

    idx.upsert_session(conn, sid, name=name, llm_enriched=1,
                       auto_tags=json.dumps(tags))
    if topics:
        idx.replace_topics(conn, sid, topics)
    conn.commit()
    generate_embeddings(conn, sid)
    return True


def enrich_batch(conn, provider=None, force=False, progress_cb=None) -> int:
    """Layer 1: enrich all un-enriched sessions. Returns count."""
    provider = provider or get_provider()
    if provider.name == "NoneProvider":
        return 0

    if force:
        rows = conn.execute(
            "SELECT id FROM sessions WHERE user_turn_count >= 1"
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT id FROM sessions WHERE llm_enriched = 0 AND user_turn_count >= 1"
        ).fetchall()

    total = len(rows)
    done = 0
    for r in rows:
        if progress_cb:
            progress_cb(done + 1, total)
        if enrich_session(conn, r[0], provider):
            done += 1
    return done


def _analyze(excerpt: str, turn_count: int, provider,
             feedback: str = "", conn=None, sid: str = "") -> tuple[list, str, list]:
    """Single-call analysis for most sessions."""
    feedback_block = ""
    if feedback and conn and sid:
        prev_topics = idx.get_topics(conn, sid)
        if prev_topics:
            prev = "\n".join(f'- "{t["title"]}" (turns: {t["turn_indices"]})' for t in prev_topics)
            feedback_block = (
                f"\nPrevious topic grouping:\n{prev}\n"
                f"\nUser feedback: {feedback}\n"
                f"Please re-analyze with this feedback in mind.\n"
            )

    prompt = (
        "Analyze this conversation and return ONLY a JSON object with:\n"
        '- "name": concise session name (max 60 chars, in conversation\'s language)\n'
        '- "topics": array of {"title": "...", "summary": "...", "turns": [indices]}\n'
        '- "tags": array of keyword tags for this session\n'
        "title: short label (max 80 chars). summary: 1-2 sentences.\n"
        "Group turns by semantic meaning, not sequential order.\n"
        "Only create multiple topics if clearly distinct subjects exist.\n"
        "Respond with raw JSON only.\n"
        f"{feedback_block}\n"
        f"Conversation ({turn_count} user turns):\n{excerpt}"
    )
    return _parse_analysis(provider.query(prompt, timeout=90))


def _analyze_chunked(prompts: list[tuple[int, str]], provider,
                     feedback: str = "", conn=None, sid: str = "") -> tuple[list, str, list]:
    """Multi-turn analysis for very large sessions (>80k chars).
    Uses --resume to keep context across chunks, then a final merge turn."""
    CHUNK_CHARS = 40000
    chunks = []
    current = []
    current_len = 0
    for p in prompts:
        entry_len = min(len(p[1]), 200) + 10
        if current and current_len + entry_len > CHUNK_CHARS:
            chunks.append(current)
            current = []
            current_len = 0
        current.append(p)
        current_len += entry_len
    if current:
        chunks.append(current)

    # First chunk: normal call (creates session in sandbox)
    first_excerpt = _build_excerpt(chunks[0])
    prompt = (
        "I'll send you a long conversation in chunks. Analyze each chunk and remember the topics.\n"
        "For this first chunk, return ONLY a JSON object with:\n"
        '- "topics": array of {"title": "...", "summary": "...", "turns": [indices]}\n'
        "Respond with raw JSON only.\n\n"
        f"Chunk 1/{len(chunks)} ({len(chunks[0])} turns):\n{first_excerpt}"
    )
    response = provider.query(prompt, timeout=90)

    # Subsequent chunks: resume to keep context
    for i, chunk in enumerate(chunks[1:], 2):
        excerpt = _build_excerpt(chunk)
        prompt = (
            f"Chunk {i}/{len(chunks)} ({len(chunk)} turns):\n{excerpt}\n\n"
            "Analyze and return topics JSON for this chunk."
        )
        response = provider.query_resume(prompt, timeout=90)

    # Final merge turn
    feedback_block = ""
    if feedback and conn and sid:
        prev_topics = idx.get_topics(conn, sid)
        if prev_topics:
            prev = "\n".join(f'- "{t["title"]}" (turns: {t["turn_indices"]})' for t in prev_topics)
            feedback_block = f"\nPrevious grouping:\n{prev}\nUser feedback: {feedback}\n"

    merge_prompt = (
        "Now merge all topics from all chunks into a final result.\n"
        "Return ONLY a JSON object with:\n"
        '- "name": concise session name (max 60 chars, in conversation\'s language)\n'
        '- "topics": merged array of {"title": "...", "summary": "...", "turns": [all indices]}\n'
        '- "tags": array of keyword tags\n'
        "Combine topics with the same subject. Respond with raw JSON only.\n"
        f"{feedback_block}"
    )
    response = provider.query_resume(merge_prompt, timeout=90)
    provider.cleanup()
    return _parse_analysis(response)


def _build_excerpt(prompts: list[tuple[int, str]], max_chars: int = 200) -> str:
    lines = []
    for ti, text in prompts:
        truncated = text[:max_chars].replace("\n", " ")
        lines.append(f"[{ti}] {truncated}")
    return "\n".join(lines)


def _parse_analysis(response: str | None) -> tuple[list, str, list]:
    """Parse LLM response into (topics, name, tags)."""
    if not response:
        return [], "", []
    try:
        match = re.search(r"\{.*\}", response, re.DOTALL)
        if not match:
            return [], "", []
        data = json.loads(match.group())
        name = data.get("name", "")
        topics = data.get("topics", [])
        tags = data.get("tags", [])
        return topics, name, tags
    except (json.JSONDecodeError, ValueError):
        return [], "", []


# --- Resume by topic ---

def generate_topic_file(conn, sid: str, topic_index: int) -> Path | None:
    """Generate temp JSON for resume by topic. Returns file path."""
    topics = idx.get_topics(conn, sid)
    if topic_index >= len(topics):
        return None

    topic = topics[topic_index]
    turn_indices = json.loads(topic["turn_indices"]) if isinstance(topic["turn_indices"], str) else topic["turn_indices"]

    # Read original session from either source
    from extractor import read_session_data
    data = read_session_data(sid)
    if not data:
        return None

    history = data.get("history", [])

    # Build prompt_index → history entry ranges mapping
    # turn_indices are prompt sequence numbers (0=first prompt, 1=second prompt...)
    # history entries include ToolUseResults between prompts
    prompt_ranges = {}  # prompt_seq → [history_indices]
    prompt_seq = 0
    for hi, entry in enumerate(history):
        user = entry.get("user", {})
        content = user.get("content", {})
        if isinstance(content, dict) and "Prompt" in content:
            prompt_ranges.setdefault(prompt_seq, []).append(hi)
            prompt_seq += 1
        elif isinstance(content, dict) and "ToolUseResults" in content:
            # Attach to previous prompt
            if prompt_seq > 0:
                prompt_ranges.setdefault(prompt_seq - 1, []).append(hi)

    # Cherry-pick all history entries for selected prompts
    picked_indices = set()
    for ti in turn_indices:
        for hi in prompt_ranges.get(ti, []):
            picked_indices.add(hi)
    picked = [history[i] for i in sorted(picked_indices)]
    if not picked:
        return None

    new_id = str(uuid.uuid4())
    # Preserve original session structure for kiro-cli compatibility
    new_session = dict(data)
    new_session["conversation_id"] = new_id
    new_session["history"] = picked
    new_session["transcript"] = []
    if "valid_history_range" in new_session:
        new_session["valid_history_range"] = [0, len(picked)]
    new_session["_kiro_session_source"] = {
        "source_id": sid,
        "type": "topic",
        "topic_index": topic_index,
        "topic_title": topic.get("title", ""),
    }

    TMP_DIR.mkdir(parents=True, exist_ok=True)
    out_path = TMP_DIR / f"{sid[:8]}-topic-{topic_index}.json"
    with open(out_path, "w") as f:
        json.dump(new_session, f)

    return out_path


def generate_other_topics_files(conn, sid: str, exclude_topic: int) -> list[Path]:
    """Generate temp files for all topics EXCEPT the excluded one (for delete-topic)."""
    topics = idx.get_topics(conn, sid)
    paths = []
    for t in topics:
        if t["topic_index"] == exclude_topic:
            continue
        p = generate_topic_file(conn, sid, t["topic_index"])
        if p:
            paths.append(p)
    return paths
