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
    """Interactive session picker using simple-term-menu."""
    if not sessions:
        print("No sessions found.", file=sys.stderr)
        return None

    try:
        from simple_term_menu import TerminalMenu
    except ImportError:
        return _fallback_picker(sessions,
            [format_session_line_plain(s, conn) for s in sessions])

    try:
        cols = os.get_terminal_size().columns - 8
    except OSError:
        cols = 74

    def _build_entries(sess_list):
        entries = []
        for i, s in enumerate(sess_list):
            sid = s["id"][:8]
            name = (s.get("name") or "(unnamed)")[:50]
            age = format_age(s.get("updated_at", 0))
            turns = s.get("user_turn_count", 0)
            directory = Path(s.get("directory", "")).name or "~"
            enriched = s.get("llm_enriched", 0)
            topics = idx.get_topics(conn, s["id"])
            topic_info = f"{len(topics)} topics" if topics else f"{turns} prompts"
            prefix = "⚡" if not enriched else "  "
            line = f"{prefix}{i+1}. {sid}  {name}  ({age}, {topic_info}, {directory})"
            entries.append(_truncate_to_width(line, cols))
        return entries

    all_sessions = sessions
    current_sessions = list(sessions)

    while True:
        entries = _build_entries(current_sessions)
        if not entries:
            print("No matching sessions.", file=sys.stderr)
            current_sessions = list(all_sessions)
            continue

        total = len(all_sessions)
        shown = len(current_sessions)
        filter_info = f" (filtered: {shown}/{total})" if shown != total else ""
        title = f"Sessions ({shown}{filter_info})  ⚡= LLM Index Pending"

        menu = TerminalMenu(
            entries,
            title=title,
            menu_cursor="> ",
            menu_cursor_style=("fg_cyan", "bold"),
            menu_highlight_style=("fg_cyan", "bold"),
            search_key="/",
            show_search_hint=True,
            quit_keys=("escape", "q"),
            cycle_cursor=False,
            status_bar="Enter: select | /: search | q: quit",
            status_bar_style=("fg_yellow",),
        )

        idx_selected = menu.show()
        if idx_selected is None:
            return None
        return current_sessions[idx_selected]


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
    import unicodedata
    w = 0
    for i, ch in enumerate(s):
        eaw = unicodedata.east_asian_width(ch)
        cw = 2 if eaw in ('W', 'F') else 1
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

    try:
        from simple_term_menu import TerminalMenu
    except ImportError:
        print("Error: simple-term-menu required.", file=sys.stderr)
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
        marker = f"  ⚠ Stale ({int(age_days)}d, {s['user_turn_count']} turns)"
    if topics and all(i in derived_topics for i in range(len(topics))):
        marker = f"  📦 Fully derived"

    # Build title
    sep = "─" * 56
    tag_str = " ".join(f"[{t}]" for t in tags) if tags else ""
    title_lines = [
        sep,
        f"  Session: {s['name']}",
        f"  ID:      {sid[:8]}",
        f"  Dir:     {s.get('directory', '?')}",
        f"  Updated: {format_age(s.get('updated_at', 0))}",
        f"  Turns:   {s['user_turn_count']} prompts{marker}",
    ]
    if tag_str:
        title_lines.append(f"  Tags:    {tag_str}")
    if topics:
        title_lines.append("")
        title_lines.append(f"  Topics ({len(topics)}):")
        for t in topics:
            dm = " ✔" if t["topic_index"] in derived_topics else ""
            title_lines.append(f"    {t['topic_index']+1}. {t['title']}{dm}")
    title_lines.append(sep)
    title_lines.append("  Actions:")
    title = "\n".join(title_lines)

    # Build menu entries
    entries = ["[r] Resume full session"]
    actions = ["resume"]
    for t in topics:
        entries.append(f"[{t['topic_index']+1}] Resume: {t['title']}")
        actions.append(f"topic_{t['topic_index']}")
    entries.append(None)
    actions.append(None)
    entries.append("[t] Edit tags")
    actions.append("tags")
    entries.append("[n] Rename")
    actions.append("rename")
    entries.append("[v] Save session")
    actions.append("save")
    if not s.get("llm_enriched"):
        entries.append("[i] Index (LLM enrich)")
        actions.append("index")
    if topics:
        entries.append("[f] Feedback (re-analyze topics)")
        actions.append("feedback")
    entries.append(None)
    actions.append(None)
    entries.append("[d] Delete session")
    actions.append("delete")

    menu = TerminalMenu(
        entries,
        title=title,
        menu_cursor="> ",
        menu_cursor_style=("fg_cyan", "bold"),
        menu_highlight_style=("fg_cyan", "bold"),
        shortcut_key_highlight_style=("fg_yellow",),
        shortcut_brackets_highlight_style=("fg_gray",),
        skip_empty_entries=True,
        accept_keys=("enter", "q"),
        quit_keys=("escape",),
        status_bar="Enter: select | Esc: back | q: quit",
        status_bar_style=("fg_yellow",),
        clear_screen=True,
    )

    choice_idx = menu.show()
    if choice_idx is None:
        return  # Esc → back to list
    if menu.chosen_accept_key == "q":
        sys.exit(0)

    action = actions[choice_idx]
    if action == "resume":
        _action_resume(conn, s, tools, go=True)
    elif action and action.startswith("topic_"):
        ti = int(action.split("_")[1])
        _action_resume_topic(conn, s, ti, tools, go=True)
    elif action == "tags":
        _action_edit_tags(conn, s)
        show_detail(conn, session)
    elif action == "rename":
        _action_rename(conn, s)
        show_detail(conn, session)
    elif action == "save":
        _action_save(conn, s)
    elif action == "index":
        _action_index(conn, sid)
        show_detail(conn, session)
    elif action == "feedback":
        _action_feedback(conn, sid, session)
    elif action == "delete":
        if _action_delete(conn, s):
            return


def _action_resume(conn, s: dict, tools: list[str], go: bool = False):
    directory = s.get("directory", "~")
    trust = ",".join(tools) if tools else ""

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

    if go:
        from launcher import launch_kiro_resume
        launched = launch_kiro_resume(directory, str(tmp_path), trust)
        if launched is not False:
            return  # unreachable — launch_kiro_resume calls sys.exit

    trust_flag = f" --trust-tools={trust}" if trust else ""
    print(f"\nResume in terminal:")
    print(f"  cd {directory} && kiro-cli chat{trust_flag}")
    print(f"  Then: /chat load {tmp_path}")
    print()


def _action_resume_topic(conn, s: dict, topic_index: int, tools: list[str], go: bool = False):
    path = splitter.generate_topic_file(conn, s["id"], topic_index)
    if not path:
        print("Failed to generate topic file.", file=sys.stderr)
        return
    topics = idx.get_topics(conn, s["id"])
    title = topics[topic_index]["title"] if topic_index < len(topics) else "?"
    directory = s.get("directory", "~")
    trust = ",".join(tools) if tools else ""

    print(f"\nTopic '{title}' ready.")

    if go:
        from launcher import launch_kiro_resume
        launched = launch_kiro_resume(directory, str(path), trust)
        if launched is not False:
            return

    trust_flag = f" --trust-tools={trust}" if trust else ""
    print(f"Resume in terminal:")
    print(f"  cd {directory} && kiro-cli chat{trust_flag}")
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
    try:
        from simple_term_menu import TerminalMenu
        menu = TerminalMenu(
            ["[y] Yes, delete", "[n] No, cancel"],
            title=f"Delete '{s['name']}'? This removes from kiro DB.",
            quit_keys=("escape",),
            shortcut_key_highlight_style=("fg_red", "bold"),
        )
        if menu.show() != 0:
            return False
    except ImportError:
        confirm = input(f"Delete session '{s['name']}'? [y/N] ").strip().lower()
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


def _action_feedback(conn, sid: str, session: dict):
    print("Current topics will be sent to LLM with your feedback for re-analysis.")
    print("Feedback: ", end="", flush=True)
    try:
        feedback = sys.stdin.buffer.readline().decode("utf-8", errors="replace").strip()
    except (KeyboardInterrupt, EOFError):
        return
    if not feedback:
        return
    print("Re-analyzing with feedback...", file=sys.stderr)
    if splitter.enrich_session(conn, sid, feedback=feedback):
        print("✔ Re-analysis complete.", file=sys.stderr)
    else:
        print("✘ Re-analysis failed.", file=sys.stderr)
    show_detail(conn, session)


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


def _action_rename(conn, s: dict):
    print(f"Current name: {s.get('name', '(unnamed)')}")
    try:
        new_name = input("New name: ").strip()
    except (KeyboardInterrupt, EOFError):
        return
    if not new_name:
        return
    idx.update_session_name(conn, s["id"], new_name)
    conn.commit()
    print(f"✔ Renamed to: {new_name}")


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
