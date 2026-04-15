---
name: session-manager
description: "Manage Kiro CLI chat sessions — list, browse, search, split by topic, save/restore, and cleanup. This skill should be used when users want to find previous sessions, browse session history, split a long session into topics, export/import sessions, or clean up stale sessions. Triggers include: 'list sessions', 'find session', 'browse sessions', 'split session', 'session topics', 'save session', 'restore session', 'cleanup sessions', 'session history', 'previous conversation', 'old chat'."
license: Proprietary
compatibility: Requires Python 3.10+, pick library, and access to Kiro CLI SQLite database
runtimes:
  - kiro
metadata:
  version: "0.3.0"
  short_description: Interactive session manager for Kiro CLI — browse, search, split, save/restore.
  authors:
    - "kiro-session contributors"
  roles:
    - developer
---

# Session Manager

Manage Kiro CLI chat sessions with enhanced browsing, topic splitting, and lifecycle management.

## CLI Usage

Run via the `kiro-session` wrapper (symlinked to `~/.local/bin/`):

```bash
kiro-session [command] [options]
```

Or directly:

```bash
python3 ~/.kiro/skills/session-manager/scripts/kiro_session.py [command] [options]
```

## Commands

### list (default)

Browse and search sessions. Default command when no subcommand is given.

```bash
kiro-session                                        # interactive browser
kiro-session list --plain                           # non-interactive output
kiro-session list --search "keyword"                # search index metadata
kiro-session list --search "keyword" --deep         # search full conversation content (slower)
kiro-session list --dir /path/to/project            # filter by directory
kiro-session list --recent 7d                       # last 7 days
kiro-session list <session-id-prefix>               # show detail for specific session
```

In interactive mode, selecting a session shows detail view with actions: resume, split, save.

### index

Build or refresh the session index with LLM-generated summaries and topic analysis. Run in background for large session counts.

```bash
kiro-session index          # LLM summarize + auto-split sessions with multiple topics
nohup kiro-session index &  # run in background
```

The index command:
1. Recovers any uncommitted splits from a previous crash
2. Generates LLM summaries (name, topics, split boundaries) for new/changed sessions
3. Automatically splits sessions with multiple detected topics
4. Applies user split preferences learned from previous feedback

### split

Interactively split a session into topic-based sessions. Original session is preserved.

```bash
kiro-session split              # interactive: pick session → review preview → execute
kiro-session split <session-id> # split specific session
```

The split flow:
1. Show split preview with topic names, turn counts, first/last prompts
2. User can: execute (e), retry with natural language feedback (r), or cancel (c)
3. On retry, LLM adjusts boundaries based on feedback; preferences are learned over time
4. New sessions written to DB, parent-child relationship tracked in index

### undo-split

Revert a split by deleting child sessions. Parent session remains unchanged.

```bash
kiro-session undo-split              # interactive: pick from split sessions
kiro-session undo-split <parent-id>  # undo specific split
```

### save / restore

Export sessions to JSON files and restore them.

```bash
kiro-session save <session-id> [output-path]   # export to file
kiro-session save <session-id> --force          # overwrite existing
kiro-session restore <path>                     # import from file to DB
kiro-session restore <path> --force             # overwrite existing session
```

Saved files are compatible with `/chat load`.

### cleanup

Review and act on cleanup suggestions for stale or redundant sessions.

```bash
kiro-session cleanup                  # default: 30 day threshold
kiro-session cleanup --stale-days 14  # custom threshold
```

Suggestions include:
- Stale sessions (old + few turns) → delete
- Fully split parent sessions → archive to `~/.kiro/session-archive/`

## Startup Notifications

The wrapper script checks on every invocation and shows relevant hints:
- Audit overdue → suggests `kiro-session cleanup`
- Sessions without LLM summaries → suggests `kiro-session index`
- Sessions with multiple topics ready to split → suggests `kiro-session split`

## Index File

Session metadata is cached at `~/.kiro/session-index.json`. The index is automatically updated (lazy indexing) on every command when DB changes are detected.

The index tracks:
- Session names and topic lists
- LLM-generated split boundary suggestions
- Parent-child relationships from splits
- User split preferences (learned from feedback)
- Audit and LLM index timestamps
- Pending split state for crash recovery

## Compatibility with Native Kiro CLI

This tool coexists with native session management (`--resume`, `--delete-session`, etc.). If sessions are deleted externally, the index self-heals on next access by removing stale entries and fixing orphaned parent/children references.

## In-Chat Usage

When triggered within a Kiro chat session, run the script via bash tool:

```bash
python3 ~/.kiro/skills/session-manager/scripts/kiro_session.py list --plain --search "keyword"
```

For interactive operations (split, cleanup), run in the user's terminal directly.

## References

- [db_schema.md](references/db_schema.md) — Kiro CLI database schema documentation
