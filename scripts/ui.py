"""Interactive UI — picker, detail page, and helpers."""
import json
import os
import sys
import subprocess
import time
from pathlib import Path

import index_store as idx
import splitter


def format_age(updated_at: int) -> str:
    if not updated_at:
        return "?"
    delta = time.time() - updated_at / 1000
    if delta < 60:
        return f"{int(delta)}s ago"
    if delta < 3600:
        return f"{int(delta / 60)}m ago"
    if delta < 86400:
        return f"{int(delta / 3600)}h ago"
    return f"{int(delta / 86400)}d ago"


def format_session_line(s: dict, conn=None) -> str:
    """Format one session for picker list."""
    sid = s["id"][:8]
    name = (s.get("name") or "(unnamed)")[:60]
    age = format_age(s.get("updated_at", 0))
    turns = s.get("user_turn_count", 0)
    directory = Path(s.get("directory", "")).name or "~"

    # Check topics / enrichment
    enriched = s.get("llm_enriched", 0)
    topics = []
    if conn:
        topics = idx.get_topics(conn, s["id"])

    info_parts = [age]
    if topics:
        info_parts.append(f"{len(topics)} topics")
    else:
        info_parts.append(f"{turns} turns")
    info_parts.append(directory)

    prefix = "\033[33m⚡\033[0m" if not enriched else " "
    meta = f"\033[90m({', '.join(info_parts)})\033[0m"
    return f"{prefix} \033[36m{sid}\033[0m  {name}  {meta}"


def format_session_line_plain(s: dict, conn=None) -> str:
    """Format one session for plain text output (no ANSI)."""
    sid = s["id"][:8]
    name = (s.get("name") or "(unnamed)")[:60]
    age = format_age(s.get("updated_at", 0))
    turns = s.get("user_turn_count", 0)
    directory = Path(s.get("directory", "")).name or "~"

    enriched = s.get("llm_enriched", 0)
    topics = []
    if conn:
        topics = idx.get_topics(conn, s["id"])

    info_parts = [age]
    if topics:
        info_parts.append(f"{len(topics)} topics")
    else:
        info_parts.append(f"{turns} turns")
    info_parts.append(directory)

    prefix = "⚡ " if not enriched else "   "
    return f"{prefix}{sid}  {name}  ({', '.join(info_parts)})"


def session_picker(conn, sessions: list[dict]) -> dict | None:
    """Interactive session picker with colors."""
    try:
        from pick import pick
    except ImportError:
        print("Error: 'pick' library required.", file=sys.stderr)
        return None

    if not sessions:
        print("No sessions found.", file=sys.stderr)
        return None

    # Truncate lines to terminal width to avoid curses overflow
    try:
        cols = os.get_terminal_size().columns - 6
    except OSError:
        cols = 74
    options = [_truncate_to_width(format_session_line_plain(s, conn), cols) for s in sessions]
    title = f"Sessions ({len(sessions)} total, ↑↓/jk navigate, Enter select, q quit)\n⚡= LLM Index Pending"

    try:
        from pick import pick, Picker

        # Disable wrap-around: stop at top/bottom
        _orig_move_up = Picker.move_up
        _orig_move_down = Picker.move_down
        def _move_down_no_wrap(self):
            if self.index + 1 < len(self.options):
                self.index += 1
        def _move_up_no_wrap(self):
            if self.index > 0:
                self.index -= 1
        Picker.move_down = _move_down_no_wrap
        Picker.move_up = _move_up_no_wrap

        result = pick(options, title, indicator="→", quit_keys=[ord('q')])
        if result is None or result[1] == -1:
            return None
        return sessions[result[1]]
    except (KeyboardInterrupt, SystemExit):
        return None
    except Exception:
        # curses overflow — fallback to plain list
        return _fallback_picker(sessions, options)


def _display_width(s: str) -> int:
    """Calculate display width accounting for CJK double-width characters."""
    w = 0
    for ch in s:
        if ord(ch) > 0x7F:
            w += 2  # CJK and other wide chars
        else:
            w += 1
    return w


def _truncate_to_width(s: str, max_width: int) -> str:
    """Truncate string to fit within max_width display columns."""
    w = 0
    for i, ch in enumerate(s):
        cw = 2 if ord(ch) > 0x7F else 1
        if w + cw > max_width:
            return s[:i]
        w += cw
    return s


def _fallback_picker(sessions, options):
    """Simple numbered list fallback when curses fails."""
    print("Sessions:")
    for i, opt in enumerate(options):
        print(f"  {i + 1}. {opt}")
    try:
        # Flush any buffered input (e.g. held-down j keys)
        import termios
        termios.tcflush(sys.stdin, termios.TCIFLUSH)
    except Exception:
        pass
    try:
        choice = input("Enter number (q to quit): ").strip()
        if choice.lower() == "q":
            return None
        idx_val = int(choice) - 1
        if 0 <= idx_val < len(sessions):
            return sessions[idx_val]
    except (ValueError, EOFError, KeyboardInterrupt):
        pass
    return None


def show_detail(conn, session: dict):
    """Show session detail page with actions."""
    sid = session["id"]
    s = idx.get_session(conn, sid)
    if not s:
        print("Session not found.", file=sys.stderr)
        return

    topics = idx.get_topics(conn, sid)
    derivations = idx.get_derivations_for_source(conn, sid)
    derived_topics = {d["topic_index"] for d in derivations}
    user_tags = json.loads(s.get("user_tags", "[]"))
    auto_tags = [t for t in json.loads(s.get("auto_tags", "[]")) if t not in user_tags]
    tags = user_tags + auto_tags
    tools = idx.get_all_tools_used(conn, sid)

    # Cleanup markers
    marker = ""
    age_days = (time.time() - (s.get("updated_at", 0) / 1000)) / 86400
    if s["user_turn_count"] == 0:
        marker = "  ⚠ Empty session"
    elif age_days > 90 and s["user_turn_count"] <= 2:
        marker = f"  ⚠ Suggested for cleanup (stale, {int(age_days)}d, {s['user_turn_count']} turns)"
    if topics and all(i in derived_topics for i in range(len(topics))):
        marker = f"  📦 Fully derived ({len(topics)}/{len(topics)} topics)"

    print("=" * 60)
    print(f"Session: {s['name']}{marker}")
    print(f"ID:      {sid}")
    print(f"Dir:     {s.get('directory', '?')}")
    print(f"Updated: {format_age(s.get('updated_at', 0))}")
    print(f"Turns:   {s['user_turn_count']} prompts")
    if tags:
        print(f"Tags:    {' '.join(f'[{t}]' for t in tags)}")

    if topics:
        print(f"\nTopics ({len(topics)}):")
        for t in topics:
            derived_mark = "  ✔ derived" if t["topic_index"] in derived_topics else ""
            print(f"  {t['topic_index'] + 1}. {t['title']}{derived_mark}")
            if t.get("summary"):
                print(f"     {t['summary']}")
    print("=" * 60)

    # Actions
    actions = ["\n  [r] Resume full session"]
    if len(topics) > 1:
        actions.append(f"  [1-{len(topics)}] Resume by topic")
    actions.append("  [t] Edit tags")
    actions.append("  [v] Save    [d] Delete")
    if len(topics) > 1:
        actions.append("  [x] Delete topic")
    if not s.get("llm_enriched"):
        actions.append("  [i] Index")
    actions.append("  [b] Back    [q] Quit")
    print("\n".join(actions))

    # Action loop
    while True:
        try:
            choice = input("> ").strip().lower()
        except (KeyboardInterrupt, EOFError):
            return

        if choice == "q":
            sys.exit(0)
        elif choice == "b":
            return
        elif choice == "r":
            _action_resume(conn, s, tools)
        elif choice == "v":
            _action_save(conn, s)
        elif choice == "d":
            if _action_delete(conn, s):
                return  # back to list
        elif choice == "i":
            _action_index(conn, sid)
            show_detail(conn, session)  # refresh
            return
        elif choice == "t":
            _action_edit_tags(conn, s)
            show_detail(conn, session)
            return
        elif choice == "x" and len(topics) > 1:
            _action_delete_topic(conn, s, topics)
            return
        elif choice.isdigit() and len(topics) > 1:
            ti = int(choice) - 1
            if 0 <= ti < len(topics):
                _action_resume_topic(conn, s, ti, tools)
        else:
            print("Unknown action.", file=sys.stderr)


def _action_resume(conn, s: dict, tools: list[str]):
    directory = s.get("directory", "~")
    trust = f" --trust-tools={','.join(tools)}" if tools else ""

    # Generate temp file with full session for reliable resume
    from extractor import read_session_data
    import json as _json
    data = read_session_data(s["id"])
    if not data:
        print("Session not found.", file=sys.stderr)
        return

    tmp_dir = Path.home() / ".kiro" / "tmp"
    tmp_dir.mkdir(parents=True, exist_ok=True)
    tmp_path = tmp_dir / f"{s['id'][:8]}-resume.json"
    with open(tmp_path, "w") as f:
        _json.dump(data, f, ensure_ascii=False)

    print(f"\nResume in terminal:")
    print(f"  cd {directory} && kiro-cli chat{trust}")
    print(f"  Then: /chat load {tmp_path}")
    print(f"\n  Note: This creates a new session with the loaded history.")
    print(f"  Original session remains unchanged.")
    print(f"  To delete original: kiro-session delete {s['id'][:8]}")
    print()


def _action_resume_topic(conn, s: dict, topic_index: int, tools: list[str]):
    path = splitter.generate_topic_file(conn, s["id"], topic_index)
    if not path:
        print("Failed to generate topic file.", file=sys.stderr)
        return
    topics = idx.get_topics(conn, s["id"])
    title = topics[topic_index]["title"] if topic_index < len(topics) else "?"
    trust = f" --trust-tools={','.join(tools)}" if tools else ""
    print(f"\nTopic '{title}' ready.")
    print(f"Resume in terminal:")
    print(f"  cd {s.get('directory', '~')} && kiro-cli chat{trust}")
    print(f"  Then: /chat load {path}")
    print()


def _action_save(conn, s: dict):
    name = (s.get("name") or "session").replace(" ", "_").replace("/", "_")[:50]
    filename = f"session-{name}.json"
    from extractor import read_session_data
    import json as _json
    data = read_session_data(s["id"])
    if not data:
        print("Session not found.", file=sys.stderr)
        return
    with open(filename, "w") as f:
        _json.dump(data, f, ensure_ascii=False)
    print(f"✔ Saved to {filename}")
    print(f"  Restore with: kiro-session restore {filename}")


def _action_delete(conn, s: dict) -> bool:
    confirm = input(f"Delete session '{s['name']}'? This removes from kiro DB. [y/N] ").strip().lower()
    if confirm != "y":
        return False
    # Delete from kiro DB via CLI
    result = subprocess.run(
        ["kiro-cli", "chat", "--delete-session", s["id"]],
        capture_output=True, text=True,
    )
    if result.returncode != 0:
        print(f"kiro-cli delete failed: {result.stderr}", file=sys.stderr)
        return False
    # Delete from our index
    idx.delete_session(conn, s["id"])
    idx.optimize_fts(conn)
    conn.commit()
    print(f"✔ Deleted {s['id'][:8]}")
    return True


def _action_index(conn, sid: str):
    print("Indexing with LLM...", file=sys.stderr)
    if splitter.enrich_session(conn, sid):
        print("✔ LLM index complete.", file=sys.stderr)
    else:
        print("✘ LLM indexing failed.", file=sys.stderr)


def _action_edit_tags(conn, s: dict):
    current = json.loads(s.get("user_tags", "[]"))
    print(f"Current tags: {', '.join(current) if current else '(none)'}")
    raw = input("Enter tags (space-separated, prefix with - to remove): ").strip()
    if not raw:
        return
    for token in raw.split():
        if token.startswith("-") and token[1:] in current:
            current.remove(token[1:])
        elif not token.startswith("-") and token not in current:
            current.append(token)
    idx.update_user_tags(conn, s["id"], current)
    conn.commit()
    print(f"✔ Tags: {', '.join(current)}")


def _action_delete_topic(conn, s: dict, topics: list[dict]):
    print("Which topic to delete?")
    for t in topics:
        print(f"  {t['topic_index'] + 1}. {t['title']}")
    try:
        choice = int(input("Topic number: ").strip()) - 1
    except (ValueError, EOFError):
        return
    if choice < 0 or choice >= len(topics):
        print("Invalid topic number.", file=sys.stderr)
        return

    target = topics[choice]
    target_turns = json.loads(target["turn_indices"]) if isinstance(target["turn_indices"], str) else target["turn_indices"]
    other_topics = [t for t in topics if t["topic_index"] != choice]

    print(f"\nTopic to delete:")
    print(f"  {choice + 1}. \"{target['title']}\" (turns: {target_turns})")
    print(f"\nTopics to preserve as new sessions:")
    for t in other_topics:
        ti = json.loads(t["turn_indices"]) if isinstance(t["turn_indices"], str) else t["turn_indices"]
        print(f"  {t['topic_index'] + 1}. \"{t['title']}\" (turns: {ti})")

    print(f"\n⚠ Turns {target_turns} will be permanently deleted from kiro DB.")
    print(f"  This requires loading {len(other_topics)} new session(s) and deleting the original.")

    # Generate files for other topics
    paths = splitter.generate_other_topics_files(conn, s["id"], choice)
    if not paths:
        print("Failed to generate topic files.", file=sys.stderr)
        return

    print(f"\n  Step 1: Load preserved topics into kiro-cli:")
    print(f"    cd {s.get('directory', '~')} && kiro-cli chat")
    for p in paths:
        print(f"    /chat load {p}")

    confirm = input(f"\n  Step 2: Confirm deletion of original session. Proceed? [y/N] ").strip().lower()
    if confirm != "y":
        # Clean up temp files
        for p in paths:
            p.unlink(missing_ok=True)
        return

    result = subprocess.run(
        ["kiro-cli", "chat", "--delete-session", s["id"]],
        capture_output=True, text=True,
    )
    if result.returncode != 0:
        print(f"kiro-cli delete failed: {result.stderr}", file=sys.stderr)
        return
    idx.delete_session(conn, s["id"])
    idx.optimize_fts(conn)
    conn.commit()
    print(f"✔ Original session deleted. Load the preserved topics before closing kiro-cli.")
