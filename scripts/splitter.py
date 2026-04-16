"""Splitter — LLM topic analysis, enrichment, and resume by topic."""
import json
import re
import time
import uuid
from pathlib import Path

import index_store as idx
from llm_provider import get_provider
from extractor import json_loads

TMP_DIR = Path.home() / ".kiro" / "tmp"
CHUNK_SIZE = 50  # turns per chunk for large sessions


def enrich_session(conn, sid: str, provider=None) -> bool:
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
    if len(prompts) < 2:
        # Too few turns for meaningful analysis
        idx.upsert_session(conn, sid, llm_enriched=1)
        conn.commit()
        return True

    if len(prompts) > CHUNK_SIZE:
        topics, name, tags = _chunk_analyze_merge(prompts, provider)
    else:
        topics, name, tags = _analyze_single(prompts, provider)

    if not name:
        return False

    idx.upsert_session(conn, sid, name=name, llm_enriched=1,
                       auto_tags=json.dumps(tags))
    if topics:
        idx.replace_topics(conn, sid, topics)
    conn.commit()
    return True


def enrich_batch(conn, provider=None, progress_cb=None) -> int:
    """Layer 1: enrich all un-enriched sessions. Returns count."""
    provider = provider or get_provider()
    if provider.name == "NoneProvider":
        return 0

    rows = conn.execute(
        "SELECT id FROM sessions WHERE llm_enriched = 0 AND user_turn_count >= 2"
    ).fetchall()

    total = len(rows)
    done = 0
    for r in rows:
        if progress_cb:
            progress_cb(done + 1, total)
        if enrich_session(conn, r[0], provider):
            done += 1
    return done


def _analyze_single(prompts: list[tuple[int, str]], provider) -> tuple[list, str, list]:
    """Analyze a session with <= CHUNK_SIZE turns."""
    excerpt = _build_excerpt(prompts)
    prompt = (
        "Analyze this conversation and return ONLY a JSON object with:\n"
        '- "name": concise session name (max 60 chars, in conversation\'s language)\n'
        '- "topics": array of {"title": "...", "summary": "...", "turns": [indices]}\n'
        '- "tags": array of keyword tags for this session\n'
        "title: short label (max 80 chars). summary: 1-2 sentences.\n"
        "Group turns by semantic meaning, not sequential order.\n"
        "Only create multiple topics if clearly distinct subjects exist.\n"
        "Respond with raw JSON only.\n\n"
        f"Conversation ({len(prompts)} user turns):\n{excerpt}"
    )

    response = provider.query(prompt, timeout=60)
    return _parse_analysis(response)


def _chunk_analyze_merge(prompts: list[tuple[int, str]], provider) -> tuple[list, str, list]:
    """Chunk-analyze-merge for large sessions."""
    chunks = [prompts[i:i + CHUNK_SIZE] for i in range(0, len(prompts), CHUNK_SIZE)]
    all_chunk_topics = []

    for chunk in chunks:
        excerpt = _build_excerpt(chunk)
        prompt = (
            "Analyze this conversation chunk and return ONLY a JSON object with:\n"
            '- "topics": array of {"title": "...", "summary": "...", "turns": [indices]}\n'
            "Group turns by semantic meaning. Respond with raw JSON only.\n\n"
            f"Chunk ({len(chunk)} turns):\n{excerpt}"
        )
        response = provider.query(prompt, timeout=60)
        if response:
            try:
                match = re.search(r"\{.*\}", response, re.DOTALL)
                if match:
                    data = json.loads(match.group())
                    all_chunk_topics.extend(data.get("topics", []))
            except (json.JSONDecodeError, ValueError):
                pass

    if not all_chunk_topics:
        return [], "", []

    # Merge phase: LLM consolidates chunk topics
    topic_list = "\n".join(
        f'- "{t.get("title", "?")}" (turns: {t.get("turns", [])})' for t in all_chunk_topics
    )
    merge_prompt = (
        "Merge these topic groups from different chunks of the same conversation.\n"
        "Combine topics with the same or similar subject. Return ONLY a JSON object with:\n"
        '- "name": concise session name (max 60 chars)\n'
        '- "topics": merged array of {"title": "...", "summary": "...", "turns": [all indices]}\n'
        '- "tags": array of keyword tags\n'
        "Respond with raw JSON only.\n\n"
        f"Topics to merge:\n{topic_list}"
    )
    response = provider.query(merge_prompt, timeout=60)
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

    # Cherry-pick turns
    picked = [history[i] for i in turn_indices if i < len(history)]
    if not picked:
        return None

    new_id = str(uuid.uuid4())
    new_session = {
        "conversation_id": new_id,
        "history": picked,
        "transcript": [],
        "_kiro_session_source": {
            "source_id": sid,
            "topic_index": topic_index,
            "topic_title": topic.get("title", ""),
        },
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
