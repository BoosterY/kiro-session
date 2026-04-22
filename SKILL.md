---
name: session-manager
description: "Manage Kiro CLI chat sessions — list, browse, search, split by topic, save/restore, and cleanup. This skill should be used when users want to find previous sessions, browse session history, split a long session into topics, export/import sessions, clean up stale sessions, or start a private/incognito conversation. Triggers include: 'list sessions', 'find session', 'browse sessions', 'split session', 'session topics', 'save session', 'restore session', 'cleanup sessions', 'session history', 'previous conversation', 'old chat', 'private session', 'incognito', 'private conversation', 'sensitive question', 'don't save this', 'topic feedback', 'redo topics'."
license: Proprietary
compatibility: Requires Python 3.10+, simple-term-menu library, and access to Kiro CLI SQLite database
runtimes:
  - kiro
metadata:
  version: "0.5.0"
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

Browse and filter sessions. Default command when no subcommand is given.

```bash
kiro-session                                        # interactive browser
kiro-session list --plain                           # non-interactive output
kiro-session list --dir /path/to/project            # filter by directory
kiro-session list --recent 7d                       # last 7 days
kiro-session list --file main.py                    # filter by file touched
kiro-session list --cmd docker                      # filter by command run
kiro-session list <session-id-prefix>               # show detail for specific session
```

In interactive mode, selecting a session shows detail view with actions: resume, resume by topic, tag, save, rename, delete.

### search

Full-text search across all session content.

```bash
kiro-session search "keyword"                       # FTS5 fast search (<50ms)
kiro-session search "keyword" --smart               # LLM semantic search
kiro-session search "keyword" --recent 7d           # combine with filters
kiro-session search "keyword" --json                # structured output for skill mode
```

### index

Build or refresh the session index with LLM-generated summaries and topic analysis.

```bash
kiro-session index                                  # enrich unindexed sessions
kiro-session index --rebuild                        # rebuild from scratch
```

### resume

Resume a session directly from CLI.

```bash
kiro-session resume <session-id>                    # resume full session
```

### save / restore

Export sessions to JSON files and restore them.

```bash
kiro-session save <session-id> [output-path]        # export to JSON
kiro-session restore <path>                         # import from JSON to DB
```

Saved files use ConversationState format, compatible with `/chat load`. For JSONL-only sessions, the wire format is automatically converted during save.

### export

Export session as Markdown.

```bash
kiro-session export <session-id>                    # export to Markdown
kiro-session export <session-id> --dir /path        # specify output directory
```

### tag / rename

```bash
kiro-session tag <session-id> "tag1" "tag2"         # add tags
kiro-session tag <session-id> --remove "tag1"       # remove tag
kiro-session rename <session-id> "new name"         # rename session
```

### delete / delete-topic

```bash
kiro-session delete <session-id>                    # delete session
kiro-session delete-topic <session-id> --topic N    # delete a specific topic
```

### redact

```bash
kiro-session redact <session-id> --turn N           # remove turn from index
```

### cleanup

Review and act on cleanup suggestions for stale or redundant sessions.

```bash
kiro-session cleanup                                # review suggestions
```

### config

```bash
kiro-session config                                 # show all settings
kiro-session config llm.provider                    # get value
kiro-session config llm.provider ollama             # set value
```

### private

Start a private/incognito session that is automatically deleted when you exit.

```bash
kiro-session private          # start private session
kiro-session private -a       # with all tools trusted
```

How it works:
- Runs kiro-cli in a sandboxed directory (`~/.kiro/skills/session-manager/private/`)
- On normal exit: session is immediately deleted from all local storage
- On abnormal exit (window close, crash): cleaned up on next `kiro-session` invocation

**Note:** This only deletes local session data (kiro DB, JSONL files, index). Conversation content sent to the LLM provider during the session may still be retained server-side per the provider's data policies.

**Important:** If a user asks to have a private conversation, ask a sensitive question, or says "don't save this" within an existing chat session, respond with:

> This conversation is already being recorded. To start a private session that won't be saved, please open a new terminal and run:
> ```
> kiro-session private
> ```
> Everything in that session will be automatically deleted when you exit.

Do NOT attempt to run `kiro-session private` from within an existing kiro chat — the child session's content would be captured in the parent session's tool results, defeating the purpose.

## Index File

Session metadata is cached at `~/.kiro/session-index.db`. The index is automatically updated (lazy indexing) on every command when DB changes are detected.

The index tracks:
- Session names and topic lists
- LLM-generated topic boundaries and turn indices
- Parent-child relationships from topic splits
- User split preferences (learned from feedback)
- Full-text search index of all user prompts

## Compatibility with Native Kiro CLI

This tool coexists with native session management (`--resume`, `--delete-session`, etc.). If sessions are deleted externally, the index self-heals on next access by removing stale entries and fixing orphaned parent/children references.

## In-Chat Usage

When triggered within a Kiro chat session, run the script via bash tool:

```bash
python3 ~/.kiro/skills/session-manager/scripts/kiro_session.py search "keyword" --json
python3 ~/.kiro/skills/session-manager/scripts/kiro_session.py list --plain
```

For interactive operations (split, cleanup), run in the user's terminal directly.
