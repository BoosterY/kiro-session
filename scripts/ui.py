"""Interactive UI — picker, detail page, and helpers."""
import json
import os
import sys
import subprocess
import time
import unicodedata
from pathlib import Path

import index_store as idx
import searcher
import splitter
from prompt_toolkit import prompt as pt_prompt
from prompt_toolkit.formatted_text import HTML
from simple_term_menu import TerminalMenu


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
    info_parts.append(f"{turns} prompts")
    info_parts.append(f"{len(topics)} topics")
    info_parts.append(directory)

    icon = "⏳" if enriched == 0 else ("🔄" if enriched == 2 else "✅")
    meta = f"\033[90m({', '.join(info_parts)})\033[0m"
    return f"{icon} \033[36m{sid}\033[0m  {name}  {meta}"


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
    info_parts.append(f"{turns} prompts")
    info_parts.append(f"{len(topics)} topics")
    info_parts.append(directory)

    prefix = "⏳" if enriched == 0 else ("🔄" if enriched == 2 else "✅")
    return f"{prefix}{sid}  {name}  ({', '.join(info_parts)})"



def session_picker(conn, sessions: list[dict]) -> dict | None:
    """Interactive session picker using simple-term-menu."""
    if not sessions:
        print("No sessions found.", file=sys.stderr)
        return None

    try:
        TerminalMenu  # verify import
    except Exception:
        return _fallback_picker(sessions,
            [format_session_line_plain(s, conn) for s in sessions])

    try:
        cols = os.get_terminal_size().columns - 8
    except OSError:
        cols = 74

    # Fixed columns: icon(3) idx(4) hash(9) age(8) turns(5) topics(6) dir(14)
    # Name gets the rest, capped at 40
    fixed = 3 + 4 + 10 + 9 + 7 + 6 + 14
    name_w = min(40, max(15, cols - fixed))
    dir_w = 12

    def _pad_cjk(s, width):
        """Pad string to fixed display width, accounting for CJK."""
        vis = 0
        for ch in s:
            eaw = unicodedata.east_asian_width(ch)
            vis += 2 if eaw in ('W', 'F') else 1
        return s + ' ' * max(0, width - vis)

    def _build_entries(sess_list):
        entries = []
        for i, s in enumerate(sess_list):
            sid = s["id"][:8]
            name = _pad_cjk(_truncate_to_width((s.get("name") or "(unnamed)"), name_w - 2), name_w)
            age = format_age(s.get("updated_at", 0))
            turns = s.get("user_turn_count", 0)
            d = Path(s.get("directory", "")).name or "~"
            d = d[:dir_w]
            enriched = s.get("llm_enriched", 0)
            topics = idx.get_topics(conn, s["id"])
            icon = "⏳" if enriched == 0 else ("🔄" if enriched == 2 else "✅")
            line = f"{icon} {i+1:>2}. {sid}  {name} {age:<8}{turns:>5} {len(topics):>6} {d}"
            entries.append(line)
        return entries

    all_sessions = sessions
    current_sessions = list(sessions)
    search_query = ""

    while True:
        entries = _build_entries(current_sessions)
        if not entries:
            if search_query:
                current_sessions = list(all_sessions)
                search_query = ""
                continue
            print("No sessions found.", file=sys.stderr)
            return None

        total = len(all_sessions)
        shown = len(current_sessions)
        filter_info = f" search: \"{search_query}\"" if search_query else ""
        empty_count = sum(1 for s in all_sessions if s.get("user_turn_count", 0) == 0)
        cleanup_hint = f"  ⚠ {empty_count} empty (cleanup)" if empty_count >= 5 else ""
        header = f"     {'#':>2}  {'ID':<8}  {'Name':<{name_w}} {'Used':<8}{'Turns':>5} {'Topics':>6} Dir"
        title = f"Sessions ({shown}/{total}{filter_info})  ⏳=pending 🔄=stale ✅=enriched{cleanup_hint}\n{header}"
        status = "Enter: select | /: filter | s: semantic search | q: quit"

        menu = TerminalMenu(
            entries,
            title=title,
            menu_cursor="> ",
            menu_cursor_style=("fg_cyan", "bold"),
            menu_highlight_style=("fg_cyan", "bold"),
            search_key="/",
            show_search_hint=True,
            accept_keys=("enter", "s"),
            quit_keys=("escape", "q"),
            cycle_cursor=False,
            clear_screen=True,
            clear_menu_on_exit=False,
            status_bar=status,
            status_bar_style=("fg_yellow",),
        )

        idx_selected = menu.show()
        key = menu.chosen_accept_key

        if idx_selected is None:
            if search_query:
                current_sessions = list(all_sessions)
                search_query = ""
                continue
            sys.stdout.write("\033[2J\033[H")
            return None

        # Semantic search
        if key == "s":
            try:
                # Redraw list context before search prompt
                sys.stdout.write("\033[2J\033[H")
                print(title)
                for e in entries[:15]:
                    print(e)
                if len(entries) > 15:
                    print(f"  ... and {len(entries) - 15} more")
                print()
                q = pt_prompt(HTML('<b>🔍 Search: </b>')).strip()
            except (KeyboardInterrupt, EOFError):
                continue
            if q:
                results = searcher.search(conn, q)
                result_ids = [r["session"]["id"] for r in results]
                current_sessions = [s for s in all_sessions if s["id"] in result_ids]
                id_order = {sid: i for i, sid in enumerate(result_ids)}
                current_sessions.sort(key=lambda s: id_order.get(s["id"], 999))
                search_query = q
            continue

        sys.stdout.write("\033[2J\033[H")
        return current_sessions[idx_selected]




def _truncate_to_width(s: str, max_width: int) -> str:
    """Truncate string to fit within max_width display columns, ignoring ANSI escapes."""
    w = 0
    i = 0
    has_ansi = False
    while i < len(s):
        # Skip ANSI escape sequences
        if s[i] == '\033' and i + 1 < len(s) and s[i + 1] == '[':
            has_ansi = True
            j = i + 2
            while j < len(s) and s[j] not in 'mABCDHJKfsu':
                j += 1
            i = j + 1
            continue
        ch = s[i]
        eaw = unicodedata.east_asian_width(ch)
        cw = 2 if eaw in ('W', 'F') else 1
        if w + cw > max_width:
            return s[:i] + ('\033[0m' if has_ansi else '')
        w += cw
        i += 1
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
        TerminalMenu  # verify import
    except Exception:
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
    enriched = s.get("llm_enriched", 0)
    e_icon, e_label = ("⏳", "pending") if enriched == 0 else (("🔄", "stale") if enriched == 2 else ("✅", "enriched"))
    title_lines = [
        sep,
        f"  Session: {s['name']}",
        f"  ID:      {sid[:8]}  {e_icon} {e_label}",
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
        num = t['topic_index'] + 1
        entries.append(f"[{num:>2}] Resume: {t['title']}")
        actions.append(f"topic_{t['topic_index']}")
    entries.append(None)
    actions.append(None)
    entries.append("[t] Edit tags")
    actions.append("tags")
    entries.append("[n] Rename")
    actions.append("rename")
    entries.append("[v] Save session")
    actions.append("save")
    if s.get("llm_enriched", 0) == 0:
        entries.append("[e] Enrich (LLM)")
    elif s.get("llm_enriched") == 2:
        entries.append("[e] Re-enrich (stale)")
    else:
        entries.append("[e] Re-enrich (force)")
    actions.append("enrich")
    if topics:
        entries.append("[f] Feedback (re-analyze topics)")
        actions.append("feedback")
    entries.append(None)
    actions.append(None)
    entries.append("[d] Delete session")
    actions.append("delete")

    # Print title, then show menu below
    print("\033[2J\033[H", end="")  # clear screen
    print(title)

    menu = TerminalMenu(
        entries,
        menu_cursor="> ",
        menu_cursor_style=("fg_cyan", "bold"),
        menu_highlight_style=("fg_cyan", "bold"),
        shortcut_key_highlight_style=("fg_yellow",),
        shortcut_brackets_highlight_style=("fg_gray",),
        skip_empty_entries=True,
        accept_keys=("enter", "q"),
        quit_keys=("escape",),
        status_bar="Enter: select | Esc: back | ^A/^E: top/bottom | q: quit",
        status_bar_style=("fg_yellow",),
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
    elif action == "enrich":
        if s.get("llm_enriched") == 1:
            ans = input("Session already enriched. Re-enrich? [y/N] ").strip().lower()
            if ans != "y":
                show_detail(conn, session)
                return
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

    if go:
        from launcher import _touch_session_in_db, launch_kiro_resume
        _touch_session_in_db(directory, s["id"])
        from config import load_config, get
        ui_mode = get(load_config(), "resume.ui") or ""
        launched = launch_kiro_resume(directory, s["id"], trust, ui_mode=ui_mode, touched=True)
        if launched is not False:
            return  # unreachable — launch_kiro_resume calls sys.exit

    trust_flag = f" --trust-tools={trust}" if trust else ""
    print(f"\nResume in terminal:")
    print(f"  cd {directory} && kiro-cli chat --resume-picker{trust_flag}")
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
        import json as _json
        with open(path) as f:
            data = _json.load(f)
        cid = data.get("conversation_id", "")

        from launcher import _write_to_kiro_db
        _write_to_kiro_db(directory, cid, data)

        from launcher import launch_kiro_resume
        from config import load_config, get
        ui_mode = get(load_config(), "resume.ui") or ""
        launched = launch_kiro_resume(directory, cid, trust, ui_mode=ui_mode, touched=True)
        if launched is not False:
            return

    trust_flag = f" --trust-tools={trust}" if trust else ""
    print(f"Resume in terminal:")
    print(f"  cd {directory} && kiro-cli chat --resume-picker{trust_flag}")
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
        TerminalMenu  # verify import
        menu = TerminalMenu(
            ["[y] Yes, delete", "[n] No, cancel"],
            title=f"Delete '{s['name']}'? This removes from kiro DB.",
            quit_keys=("escape",),
            shortcut_key_highlight_style=("fg_red", "bold"),
        )
        if menu.show() != 0:
            return False
    except Exception:
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
    print("Enriching with LLM...", file=sys.stderr)
    if splitter.enrich_session(conn, sid):
        print("✔ Enrich complete.", file=sys.stderr)
    else:
        print("✘ Enrich failed.", file=sys.stderr)


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


def _action_delete_topic(conn, s: dict, topics: list[dict], topic_index: int | None = None):
    if topic_index is None:
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
    else:
        choice = topic_index

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
