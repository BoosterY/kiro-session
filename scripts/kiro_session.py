#!/usr/bin/env python3
"""kiro-session v2 — CLI entry point and command routing."""
import argparse
import json
import os
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

import index_store as idx
import extractor
import searcher
import splitter
import ui
import config as cfg
from llm_provider import get_provider


def main():
    parser = argparse.ArgumentParser(prog="kiro-session", description="Smart session manager for Kiro CLI")
    parser.add_argument("--json", action="store_true", help="JSON output for skill integration")
    sub = parser.add_subparsers(dest="command")

    # list
    p_list = sub.add_parser("list", help="List/filter sessions")
    p_list.add_argument("--json", action="store_true")
    p_list.add_argument("--file", dest="file_filter", default="")
    p_list.add_argument("--cmd", dest="cmd_filter", default="")
    p_list.add_argument("--dir", "-d", dest="dir_filter", default="")
    p_list.add_argument("--recent", "-r", default="")
    p_list.add_argument("--plain", action="store_true")
    p_list.add_argument("session_id", nargs="?")

    # search
    p_search = sub.add_parser("search", help="Search sessions")
    p_search.add_argument("query")
    p_search.add_argument("--json", action="store_true")
    p_search.add_argument("--file", dest="file_filter", default="")
    p_search.add_argument("--cmd", dest="cmd_filter", default="")
    p_search.add_argument("--dir", "-d", dest="dir_filter", default="")
    p_search.add_argument("--recent", "-r", default="")

    # index
    p_index = sub.add_parser("enrich", aliases=["index"], help="LLM enrich sessions (names, topics, tags)")
    p_index.add_argument("--rebuild", action="store_true", help="Full rebuild (re-scan + re-enrich)")
    p_index.add_argument("--force", action="store_true", help="Re-enrich all sessions")

    # export
    p_export = sub.add_parser("export", help="Export session(s) as Markdown")
    p_export.add_argument("--all", action="store_true", dest="export_all", help="Export all sessions")
    p_export.add_argument("--dir", dest="export_dir", default=None, help="Output directory")
    p_export.add_argument("session_ids", nargs="*")

    # save
    p_save = sub.add_parser("save", help="Export session to JSON")
    p_save.add_argument("session_id")
    p_save.add_argument("path", nargs="?")

    # restore
    p_restore = sub.add_parser("restore", help="Import session from JSON")
    p_restore.add_argument("path")

    # delete
    p_delete = sub.add_parser("delete", help="Delete session(s)")
    p_delete.add_argument("session_ids", nargs="+")

    # delete-topic
    p_dtopic = sub.add_parser("delete-topic", help="Delete a topic from session")
    p_dtopic.add_argument("session_id")
    p_dtopic.add_argument("--topic", type=int, required=True)

    # tag
    p_tag = sub.add_parser("tag", help="Add/remove user tags")
    p_tag.add_argument("--json", action="store_true")
    p_tag.add_argument("--batch", action="store_true", help="Batch mode: multiple IDs then tags")
    p_tag.add_argument("session_id")
    p_tag.add_argument("tags", nargs="*")
    p_tag.add_argument("--remove", default="")

    # cleanup
    p_cleanup = sub.add_parser("cleanup", help="Review cleanup suggestions")
    p_cleanup.add_argument("--json", action="store_true")

    # redact
    p_redact = sub.add_parser("redact", help="Remove turn from index")
    p_redact.add_argument("session_id")
    p_redact.add_argument("--turn", type=int, required=True)

    # config
    p_config = sub.add_parser("config", help="View/set configuration")
    p_config.add_argument("key", nargs="?")
    p_config.add_argument("value", nargs="?")

    # private
    p_private = sub.add_parser("private", help="Start a private session (auto-deleted on exit)")
    p_private.add_argument("--trust-all-tools", "-a", action="store_true")
    p_private.add_argument("extra", nargs="*", help="Extra args passed to kiro-cli")

    # resume
    p_resume = sub.add_parser("resume", help="Resume a session by ID")
    p_resume.add_argument("session_id")
    p_resume.add_argument("--topic", type=int, default=None, help="Resume specific topic")

    # rename
    p_rename = sub.add_parser("rename", help="Rename a session")
    p_rename.add_argument("session_id")
    p_rename.add_argument("name")

    # context
    p_ctx = sub.add_parser("context", help="Generate context summary for /context add")
    p_ctx.add_argument("session_id")
    p_ctx.add_argument("--topic", type=int, default=None, help="Only summarize a specific topic")

    args = parser.parse_args()
    # Propagate --json to args if not set by subparser
    if not hasattr(args, 'json'):
        args.json = False
    conn = idx.connect()

    # Layer 0 incremental index on every command
    extractor.ensure_index_fresh(conn, progress_cb=_progress if not args.json else None)

    cmd = args.command
    if cmd is None:
        cmd_browse(conn, args)
    elif cmd == "list":
        cmd_list(conn, args)
    elif cmd == "search":
        cmd_search(conn, args)
    elif cmd == "enrich" or cmd == "index":
        cmd_index(conn, args)
    elif cmd == "save":
        cmd_save(conn, args)
    elif cmd == "export":
        cmd_export(conn, args)
    elif cmd == "restore":
        cmd_restore(args)
    elif cmd == "delete":
        cmd_delete(conn, args)
    elif cmd == "delete-topic":
        cmd_delete_topic(conn, args)
    elif cmd == "tag":
        cmd_tag(conn, args)
    elif cmd == "cleanup":
        cmd_cleanup(conn, args)
    elif cmd == "redact":
        cmd_redact(conn, args)
    elif cmd == "config":
        cmd_config(args)
    elif cmd == "private":
        cmd_private(args)
        return  # skip conn.close etc
    elif cmd == "resume":
        cmd_resume(conn, args)
    elif cmd == "rename":
        cmd_rename(conn, args)
    elif cmd == "context":
        cmd_context(conn, args)


def _progress(i, total):
    if i == 1:
        print(f"Scanning {total} session(s)...", file=sys.stderr, end="", flush=True)
    if i == total:
        print(" ok.", file=sys.stderr)


def _resolve_session(conn, prefix: str) -> dict | None:
    sessions = idx.get_all_sessions(conn)
    matches = [s for s in sessions if s["id"].startswith(prefix)]
    if len(matches) == 1:
        return matches[0]
    if len(matches) > 1:
        print(f"Ambiguous ID prefix '{prefix}', {len(matches)} matches.", file=sys.stderr)
    else:
        print(f"No session matching '{prefix}'.", file=sys.stderr)
    return None


# --- Commands ---

def cmd_browse(conn, args):
    sessions = idx.get_all_sessions(conn)
    if not sessions:
        print("No sessions found.")
        return
    while True:
        selected = ui.session_picker(conn, sessions)
        if not selected:
            return
        ui.show_detail(conn, selected)
        # Refresh sessions after potential changes
        sessions = idx.get_all_sessions(conn)


def cmd_list(conn, args):
    if args.session_id:
        s = _resolve_session(conn, args.session_id)
        if s:
            ui.show_detail(conn, s)
        return

    results = searcher.search(conn, file_filter=args.file_filter,
                              cmd_filter=args.cmd_filter, dir_filter=args.dir_filter,
                              recent=args.recent)
    sessions = [r["session"] for r in results]

    if args.json:
        _json_output([_session_json(s) for s in sessions])
        return

    if args.plain or not sys.stdout.isatty():
        color = sys.stdout.isatty()
        print(f"Sessions ({len(sessions)} total):")
        for i, s in enumerate(sessions, 1):
            sid = s["id"][:8]
            name = (s.get("name") or "(unnamed)")[:50]
            age = ui.format_age(s.get("updated_at", 0))
            turns = s.get("user_turn_count", 0)
            directory = Path(s.get("directory", "")).name or "~"
            enriched = s.get("llm_enriched", 0)
            topics = idx.get_topics(conn, s["id"])
            topic_info = f"{len(topics)} topics, "
            meta = f"{age}, {turns} prompts, {topic_info}{directory}"
            prefix = "⏳" if enriched == 0 else ("🔄" if enriched == 2 else "✅")
            if color:
                print(f"\033[33m{prefix}\033[36m{i:3d}. {sid}\033[0m  {name}  \033[90m({meta})\033[0m")
            else:
                print(f"{prefix}{i:3d}. {sid}  {name}  ({meta})")
        return

    if not sessions:
        print("No sessions match the filter.")
        return
    selected = ui.session_picker(conn, sessions)
    if selected:
        ui.show_detail(conn, selected)


def cmd_search(conn, args):
    results = searcher.search(conn, query=args.query,
                              file_filter=args.file_filter, cmd_filter=args.cmd_filter,
                              dir_filter=args.dir_filter, recent=args.recent)
    if args.json:
        out = []
        for r in results:
            d = _session_json(r["session"])
            d["snippet"] = r.get("snippet", "")
            out.append(d)
        _json_output(out)
        return

    if not results:
        print("No results.")
        return
    for i, r in enumerate(results, 1):
        s = r["session"]
        turns = s.get('user_turn_count', 0)
        print(f"\033[36m{i:2d}. {s['id'][:8]}\033[0m  {s.get('name', '?')[:50]}  \033[90m({ui.format_age(s.get('updated_at', 0))}, {turns} prompts, {s.get('directory', '?')})\033[0m")
        if r.get("snippet"):
            snip = r['snippet'][:200].replace(">>>", "\033[1;31m").replace("<<<", "\033[0;90m")
            print(f"    \033[90m{snip}\033[0m")
    first_id = results[0]["session"]["id"][:8]
    print(f"\n\033[33mTip: kiro-session resume {first_id} to continue a session\033[0m")


def cmd_index(conn, args):
    if args.rebuild:
        # Drop and rebuild
        for table in ("sessions", "turns", "files_used", "commands", "topics"):
            conn.execute(f"DELETE FROM {table}")
        conn.execute("DELETE FROM fts_content")
        conn.commit()
        extractor.ensure_index_fresh(conn, progress_cb=_progress)

    provider = get_provider()
    print(f"LLM provider: {provider.name}", file=sys.stderr)
    count = splitter.enrich_batch(conn, provider, force=args.force,
                                  progress_cb=lambda i, t: print(f"  Enriching {i}/{t}...", file=sys.stderr))
    print(f"✔ Enriched {count} session(s).", file=sys.stderr)


def cmd_save(conn, args):
    s = _resolve_session(conn, args.session_id)
    if not s:
        return
    data = extractor.read_session_data(s["id"])
    if not data:
        print("Session not found.", file=sys.stderr)
        return
    name = (s.get("name") or "session").replace(" ", "_").replace("/", "_")[:50]
    path = args.path or f"session-{name}.json"
    with open(path, "w") as f:
        json.dump(data, f, ensure_ascii=False)
    print(f"✔ Saved to {path}")


def cmd_export(conn, args):
    if args.export_all:
        sessions = idx.get_all_sessions(conn)
    elif args.session_ids:
        sessions = []
        for sid in args.session_ids:
            s = _resolve_session(conn, sid)
            if not s:
                return
            sessions.append(s)
    else:
        print("Provide session ID(s) or --all.", file=sys.stderr)
        return

    out_dir = Path(args.export_dir) if args.export_dir else None
    if out_dir:
        out_dir.mkdir(parents=True, exist_ok=True)

    for s in sessions:
        _export_one(conn, s, out_dir)

    if len(sessions) > 1:
        print(f"✔ Exported {len(sessions)} session(s).")


def _export_one(conn, s: dict, out_dir: Path | None):
    data = extractor.read_session_data(s["id"])
    if not data:
        print(f"Session {s['id'][:8]} not found.", file=sys.stderr)
        return

    turns = _extract_md_turns(data)
    name = s.get("name") or "(unnamed)"
    directory = s.get("directory") or data.get("_meta", {}).get("cwd", "?")
    updated = s.get("updated_at", 0)
    from datetime import datetime
    date_str = datetime.fromtimestamp(updated / 1000).strftime("%Y-%m-%d %H:%M") if updated else "?"

    lines = [
        f"# Session: {name}",
        f"- ID: {s['id']}",
        f"- Directory: {directory}",
        f"- Date: {date_str}",
        f"- Turns: {len(turns)}",
        "",
    ]
    for role, text in turns:
        lines.append("---")
        lines.append("")
        lines.append(f"## {role}")
        lines.append(text)
        lines.append("")

    md = "\n".join(lines)
    slug = (s.get("name") or "session").replace(" ", "_").replace("/", "_")[:50]
    path = (out_dir / f"session-{slug}.md") if out_dir else Path(f"session-{slug}.md")
    with open(path, "w") as f:
        f.write(md)
    print(f"✔ Exported to {path}")


def _extract_md_turns(data: dict) -> list[tuple[str, str]]:
    """Extract (role, text) pairs from session data (ConversationState format)."""
    turns = []
    for entry in data.get("history", []):
        user = entry.get("user", {})
        assistant = entry.get("assistant", {})

        # User prompt
        content = user.get("content", {})
        prompt = ""
        if isinstance(content, dict):
            p = content.get("Prompt", "")
            prompt = p.get("prompt", "") if isinstance(p, dict) else (p if isinstance(p, str) else "")
        elif isinstance(content, str):
            prompt = content
        if prompt:
            turns.append(("User", prompt))

        # Assistant
        if isinstance(assistant, dict):
            resp = assistant.get("Response", {})
            if isinstance(resp, dict) and (resp.get("content") or resp.get("value")):
                turns.append(("Assistant", resp.get("content", "") or resp.get("value", "")))
            elif isinstance(resp, str) and resp:
                turns.append(("Assistant", resp))

            tool_use = assistant.get("ToolUse", {})
            if isinstance(tool_use, dict) and tool_use.get("tool_uses"):
                tools = [tu.get("name", "?") for tu in tool_use["tool_uses"]]
                text = tool_use.get("content", "") or tool_use.get("text", "")
                parts = []
                if text:
                    parts.append(text)
                parts.append(f"*Tools used: {', '.join(tools)}*")
                turns.append(("Assistant", "\n\n".join(parts)))
    return turns


def cmd_restore(args):
    path = Path(args.path)
    if not path.exists():
        print(f"File not found: {path}", file=sys.stderr)
        return
    print(f"Restore session from {path}:")
    print(f"  Start kiro-cli, then run: /chat load {path.absolute()}")


def cmd_delete(conn, args):
    sessions = []
    for sid in args.session_ids:
        s = _resolve_session(conn, sid)
        if not s:
            return
        sessions.append(s)
    names = "\n".join(f"  {s['id'][:8]}  {s.get('name', '?')}" for s in sessions)
    try:
        from simple_term_menu import TerminalMenu
        menu = TerminalMenu(
            ["[y] Yes, delete", "[n] No, cancel"],
            title=f"Delete {len(sessions)} session(s)?\n{names}",
            quit_keys=("escape",),
            shortcut_key_highlight_style=("fg_red", "bold"),
        )
        choice = menu.show()
        if choice != 0:
            return
    except ImportError:
        confirm = input(f"Delete {len(sessions)} session(s)?\n{names}\n[y/N] ").strip().lower()
        if confirm != "y":
            return
    _batch_delete(conn, sessions)


def cmd_delete_topic(conn, args):
    s = _resolve_session(conn, args.session_id)
    if not s:
        return
    topics = idx.get_topics(conn, s["id"])
    if not topics:
        print("No topics. Run 'kiro-session enrich' first.", file=sys.stderr)
        return
    ti = args.topic
    if ti < 0 or ti >= len(topics):
        print(f"Invalid topic index. Valid: 0-{len(topics)-1}", file=sys.stderr)
        return
    # Delegate to UI which handles the full flow
    ui._action_delete_topic(conn, s, topics, topic_index=ti)


def cmd_tag(conn, args):
    if args.batch:
        # In batch mode: session_id + tags are mixed positional args.
        # Try resolving each as session; non-matching ones are tags.
        all_args = [args.session_id] + args.tags
        sessions, tags = [], []
        all_sessions = idx.get_all_sessions(conn)
        for a in all_args:
            matches = [s for s in all_sessions if s["id"].startswith(a)]
            if len(matches) == 1:
                sessions.append(matches[0])
            else:
                tags.append(a)
        if not sessions or not tags:
            print("Need at least one session ID and one tag.", file=sys.stderr)
            return
        for s in sessions:
            current = json.loads(s.get("user_tags", "[]"))
            if args.remove:
                current = [t for t in current if t != args.remove]
            for t in tags:
                if t not in current:
                    current.append(t)
            idx.update_user_tags(conn, s["id"], current)
        conn.commit()
        print(f"✔ Tagged {len(sessions)} session(s) with: {', '.join(tags)}")
        return

    s = _resolve_session(conn, args.session_id)
    if not s:
        return
    current = json.loads(s.get("user_tags", "[]"))
    if args.remove:
        current = [t for t in current if t != args.remove]
    for t in args.tags:
        if t not in current:
            current.append(t)
    idx.update_user_tags(conn, s["id"], current)
    conn.commit()
    if args.json:
        _json_output({"tags": current})
    else:
        print(f"✔ Tags: {', '.join(current) if current else '(none)'}")


def cmd_cleanup(conn, args):
    sessions = idx.get_all_sessions(conn)
    now = time.time() * 1000
    stale = []
    empty = []
    derived = []

    for s in sessions:
        age_days = (now - (s.get("updated_at", 0))) / (86400 * 1000)
        if s["user_turn_count"] == 0:
            empty.append((s, age_days))
        elif age_days > 90 and s["user_turn_count"] <= 2:
            stale.append((s, age_days))

        topics = idx.get_topics(conn, s["id"])
        if topics:
            derivations = idx.get_derivations_for_source(conn, s["id"])
            derived_topics = {d["topic_index"] for d in derivations}
            if all(i in derived_topics for i in range(len(topics))) and age_days > 30:
                derived.append((s, len(topics)))

    if not stale and not empty and not derived:
        print("No cleanup suggestions. Everything looks good!")
        return

    if args.json:
        out = {"stale": [_session_json(s) for s, _ in stale],
               "empty": [_session_json(s) for s, _ in empty],
               "fully_derived": [_session_json(s) for s, _ in derived]}
        _json_output(out)
        return

    try:
        from simple_term_menu import TerminalMenu
    except ImportError:
        print("Error: simple-term-menu required.", file=sys.stderr)
        return

    all_candidates = []
    entries = []
    for s, age in stale:
        all_candidates.append(s)
        entries.append(f"🗑 {s['id'][:8]}  {s.get('name', '?')[:40]}  ({int(age)}d, {s['user_turn_count']} turns)")
    for s, age in empty:
        all_candidates.append(s)
        entries.append(f"🗑 {s['id'][:8]}  (empty)  ({int(age)}d)")
    for s, tc in derived:
        all_candidates.append(s)
        entries.append(f"📦 {s['id'][:8]}  {s.get('name', '?')[:40]}  ({tc}/{tc} derived)")

    menu = TerminalMenu(
        entries,
        title="Cleanup: Space to toggle, Enter to delete selected, Esc to cancel",
        multi_select=True,
        show_multi_select_hint=True,
        multi_select_select_on_accept=False,
        multi_select_empty_ok=True,
        quit_keys=("escape", "q"),
        preselected_entries=list(range(len(entries))),
    )
    selected = menu.show()
    if selected is None or len(selected) == 0:
        print("Cancelled.")
        return
    to_delete = [all_candidates[i] for i in selected]
    _batch_delete(conn, to_delete)


def _batch_delete(conn, sessions: list[dict]):
    import subprocess
    for s in sessions:
        subprocess.run(["kiro-cli", "chat", "--delete-session", s["id"]],
                       capture_output=True, text=True)
        idx.delete_session(conn, s["id"])
    idx.optimize_fts(conn)
    conn.commit()
    print(f"✔ Deleted {len(sessions)} session(s).")


def cmd_redact(conn, args):
    s = _resolve_session(conn, args.session_id)
    if not s:
        print("Session not found.", file=sys.stderr)
        return
    turn = args.turn
    exists = conn.execute("SELECT 1 FROM turns WHERE session_id = ? AND turn_index = ?", (s["id"], turn)).fetchone()
    if not exists:
        print(f"Turn {turn} not found in session {s['id'][:8]}.", file=sys.stderr)
        return
    conn.execute("DELETE FROM turns WHERE session_id = ? AND turn_index = ?", (s["id"], turn))
    conn.execute("DELETE FROM fts_content WHERE session_id = ? AND turn_index = ?", (s["id"], str(turn)))
    conn.execute("DELETE FROM files_used WHERE session_id = ? AND turn_index = ?", (s["id"], turn))
    conn.execute("DELETE FROM commands WHERE session_id = ? AND turn_index = ?", (s["id"], turn))
    idx.optimize_fts(conn)
    conn.commit()
    print(f"✔ Redacted turn {turn} from session {s['id'][:8]}.")


def cmd_resume(conn, args):
    s = _resolve_session(conn, args.session_id)
    if not s:
        print("Session not found.", file=sys.stderr)
        return
    tools = idx.get_all_tools_used(conn, s["id"])
    if args.topic is not None:
        ui._action_resume_topic(conn, s, args.topic, tools, go=True)
    else:
        ui._action_resume(conn, s, tools, go=True)


def cmd_context(conn, args):
    """Generate a context summary file from a session or topic for /context add."""
    s = _resolve_session(conn, args.session_id)
    if not s:
        return

    sid = s["id"]

    # Get turns — filter by topic if specified
    if args.topic is not None:
        topics = idx.get_topics(conn, sid)
        if not topics or args.topic >= len(topics):
            print(f"Topic {args.topic} not found. Run 'kiro-session enrich' first.", file=sys.stderr)
            return
        topic = topics[args.topic]
        indices = json.loads(topic["turn_indices"]) if isinstance(topic["turn_indices"], str) else topic["turn_indices"]
        turns = conn.execute(
            "SELECT turn_index, user_prompt, assistant_response FROM turns WHERE session_id = ? ORDER BY turn_index",
            (sid,),
        ).fetchall()
        turns = [t for t in turns if t[0] in indices]
        title = f"Topic: {topic.get('title', f'#{args.topic}')}"
    else:
        turns = conn.execute(
            "SELECT turn_index, user_prompt, assistant_response FROM turns WHERE session_id = ? ORDER BY turn_index",
            (sid,),
        ).fetchall()
        title = s.get("name", sid[:8])

    if not turns:
        print("No turns found.", file=sys.stderr)
        return

    # Try LLM summary first, fall back to extraction
    summary = _context_summary_llm(turns, title)
    if not summary:
        summary = _context_summary_extract(turns, title, s)

    # Save
    TMP_DIR = Path.home() / ".kiro" / "tmp"
    TMP_DIR.mkdir(parents=True, exist_ok=True)
    suffix = f"-topic-{args.topic}" if args.topic is not None else ""
    out_path = TMP_DIR / f"{sid[:8]}{suffix}-context.md"
    with open(out_path, "w") as f:
        f.write(summary)

    print(f"✔ Context saved to {out_path}")
    print(f"\n  /context add {out_path}")


def _context_summary_llm(turns, title: str) -> str | None:
    """Try to generate a summary using LLM. Returns None if unavailable."""
    provider = get_provider()
    if provider.name == "NoneProvider":
        return None

    excerpt = "\n".join(
        f"[User] {t[1][:300]}\n[Assistant] {(t[2] or '')[:300]}" for t in turns if t[1]
    )
    prompt = (
        "Summarize this conversation into a concise context document. Include:\n"
        "- Key decisions and conclusions\n"
        "- Important code snippets or commands\n"
        "- Technical context that would help continue this work\n"
        "Format as Markdown. Be concise.\n\n"
        f"Title: {title}\n\n{excerpt}"
    )
    response = provider.query(prompt, timeout=60)
    if not response:
        return None
    return f"# Context: {title}\n\n{response}\n"


def _context_summary_extract(turns, title: str, session: dict) -> str:
    """Extract-based summary (no LLM). Bullet points of prompts + truncated responses."""
    lines = [f"# Context: {title}\n"]
    lines.append(f"Source: session {session['id'][:8]} ({session.get('directory', '?')})\n")

    for t in turns:
        prompt = (t[1] or "").strip()
        response = (t[2] or "").strip()[:200]
        if not prompt:
            continue
        lines.append(f"- **Q**: {prompt[:200]}")
        if response:
            lines.append(f"  **A**: {response}")

    return "\n".join(lines) + "\n"


def cmd_rename(conn, args):
    s = _resolve_session(conn, args.session_id)
    if not s:
        return
    idx.update_session_name(conn, s["id"], args.name)
    conn.commit()
    print(f"✔ Renamed {s['id'][:8]} → {args.name}")


def cmd_config(args):
    c = cfg.load_config()
    if not args.key:
        import yaml
        print(yaml.dump(c, default_flow_style=False))
        return
    if args.value is None:
        val = cfg.get(c, args.key)
        print(f"{args.key} = {val}")
        return
    if args.key == "privacy.purge":
        if idx.INDEX_DB.exists():
            idx.INDEX_DB.unlink()
            print("✔ Index purged.")
        return
    if args.key == "privacy.exclude_dirs":
        # Add dir to exclude list and purge matching sessions
        dirs = cfg.get(c, "privacy.exclude_dirs") or []
        new_dir = os.path.expanduser(args.value)
        if new_dir not in dirs:
            dirs.append(new_dir)
            c = cfg.set_value(c, "privacy.exclude_dirs", dirs)
            cfg.save_config(c)
        _purge_dir(new_dir)
        print(f"✔ privacy.exclude_dirs = {dirs}")
        return
    c = cfg.set_value(c, args.key, args.value)
    cfg.save_config(c)
    print(f"✔ {args.key} = {cfg.get(c, args.key)}")


def _purge_dir(directory: str):
    """Purge all sessions from a directory — index + kiro DB + JSONL files."""
    import time
    conn = idx.connect()
    purged = []

    # Find matching sessions in our index
    sessions = conn.execute(
        "SELECT id, directory FROM sessions WHERE directory LIKE ?", (f"{directory}%",)
    ).fetchall()

    for sid, sdir in sessions:
        # Delete from kiro DB via kiro-cli
        result = subprocess.run(
            ["kiro-cli", "chat", "--delete-session", sid],
            capture_output=True, timeout=10,
        )
        # Delete from our index
        idx.delete_session(conn, sid)
        purged.append(sid)
        print(f"  🗑 {sid[:8]}  ({sdir})")

    conn.commit()

    # Log purge
    if purged:
        c = cfg.load_config()
        log = cfg.get(c, "privacy.purge_log") or []
        log.append({
            "time": time.strftime("%Y-%m-%dT%H:%M:%S"),
            "dir": directory,
            "sessions": [s[:8] for s in purged],
        })
        c.setdefault("privacy", {})["purge_log"] = log
        cfg.save_config(c)
        print(f"  ✔ Purged {len(purged)} session(s) from '{directory}'")


PRIVATE_DIR = Path.home() / ".kiro" / "skills" / "session-manager" / "private"


def _cleanup_private_sessions():
    """Delete all sessions created in the private directory from kiro DB."""
    # Check SQLite
    try:
        kiro = extractor.kiro_connect()
        rows = kiro.execute(
            "SELECT conversation_id FROM conversations_v2 WHERE key LIKE ?",
            (str(PRIVATE_DIR) + "%",)
        ).fetchall()
        for (cid,) in rows:
            subprocess.run(
                ["kiro-cli", "chat", "--delete-session", cid],
                capture_output=True, timeout=10,
            )
    except Exception:
        pass

    # Check JSONL
    import json as _json
    sessions_dir = extractor.KIRO_SESSIONS_DIR
    if sessions_dir.exists():
        for meta_file in sessions_dir.glob("*.json"):
            try:
                with open(meta_file) as f:
                    meta = _json.load(f)
                if meta.get("cwd", "").startswith(str(PRIVATE_DIR)):
                    subprocess.run(
                        ["kiro-cli", "chat", "--delete-session", meta["session_id"]],
                        capture_output=True, timeout=10,
                    )
            except Exception:
                continue


def cmd_private(args):
    """Start a private session that is auto-deleted on exit."""
    PRIVATE_DIR.mkdir(parents=True, exist_ok=True)

    # Clean any leftover private sessions from previous crashes
    _cleanup_private_sessions()

    cmd = ["kiro-cli", "chat"]
    if args.trust_all_tools:
        cmd.append("--trust-all-tools")
    cmd.extend(args.extra)

    print("🔒 Private session — local data will be deleted on exit.", file=sys.stderr)
    print("   Note: content sent to LLM provider may still be retained server-side.", file=sys.stderr)
    try:
        subprocess.run(cmd, cwd=str(PRIVATE_DIR))
    except KeyboardInterrupt:
        pass
    finally:
        print("\n🗑 Cleaning up private session...", file=sys.stderr)
        _cleanup_private_sessions()
        print("✔ Private session deleted.", file=sys.stderr)


def _session_json(s: dict) -> dict:
    return {
        "id": s["id"][:8],
        "full_id": s["id"],
        "name": s.get("name", ""),
        "dir": s.get("directory", ""),
        "updated": ui.format_age(s.get("updated_at", 0)),
        "turns": s.get("user_turn_count", 0),
        "tags": json.loads(s.get("auto_tags", "[]")) + json.loads(s.get("user_tags", "[]")),
    }


def _json_output(data):
    print(json.dumps(data, ensure_ascii=False))


if __name__ == "__main__":
    main()
