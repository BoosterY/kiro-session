---
name: session-manager
description: "Manage Kiro CLI chat sessions — search, list, browse, and manage. Use when users want to find previous sessions, search conversation history, list recent sessions, or start a private conversation. Triggers: 'find session', 'search sessions', 'list sessions', 'previous conversation', 'old chat', 'session history', 'private session', 'incognito', 'don't save this'."
license: Proprietary
compatibility: Requires Python 3.10+, kiro-session installed via install.sh
runtimes:
  - kiro
metadata:
  version: "0.6.0"
  short_description: Interactive session manager for Kiro CLI — search, browse, resume by topic.
  authors:
    - "kiro-session contributors"
  roles:
    - developer
---

# Session Manager

Search and manage Kiro CLI chat sessions from within a conversation or via standalone CLI.

## In-Chat Usage (AI-executable)

These commands can be run via bash tool inside a kiro chat session:

### search (primary use case)

```bash
python3 ~/.kiro/skills/session-manager/scripts/kiro_session.py search "query" --json
```

Hybrid search: FTS5 keyword + embedding semantic, merged with RRF. Cross-language (e.g. "容器部署" finds "docker" sessions).

JSON output format — array of objects:
```json
[
  {
    "id": "1ec9859a",
    "full_id": "1ec9859a-9c90-427f-...",
    "name": "Docker开发容器构建与多机部署",
    "dir": "/home/user/dev-env",
    "updated": "12h ago",
    "turns": 111,
    "tags": ["docker", "podman"],
    "snippet": "...configured nginx reverse proxy for >>>docker<<< containers..."
  }
]
```

Combine with filters:
```bash
python3 ~/.kiro/skills/session-manager/scripts/kiro_session.py search "query" --dir docs --json
python3 ~/.kiro/skills/session-manager/scripts/kiro_session.py search "query" --recent 7d --json
python3 ~/.kiro/skills/session-manager/scripts/kiro_session.py search "query" --file main.py --json
```

### list

```bash
python3 ~/.kiro/skills/session-manager/scripts/kiro_session.py list --plain
python3 ~/.kiro/skills/session-manager/scripts/kiro_session.py list --plain --recent 7d
python3 ~/.kiro/skills/session-manager/scripts/kiro_session.py list --plain --dir projectname
python3 ~/.kiro/skills/session-manager/scripts/kiro_session.py list <session-id-prefix>  # show detail
```

Use `--plain` for non-interactive output. Use `--json` for structured output.

## Terminal-Only Commands

These require an interactive terminal. When users ask for these, provide the command for them to run:

| Command | What it does |
|---------|-------------|
| `kiro-session` | Interactive browser with `/` filter and `s` semantic search |
| `kiro-session resume <id>` | Resume session via PTY automation |
| `kiro-session resume <id> --topic N` | Resume specific topic only |
| `kiro-session index` | LLM enrichment (names, topics, tags) |
| `kiro-session save <id>` | Export to JSON |
| `kiro-session export <id>` | Export to Markdown |
| `kiro-session delete <id>` | Delete session |
| `kiro-session cleanup` | Review stale session suggestions |
| `kiro-session tag <id> "tag"` | Add/remove tags |
| `kiro-session rename <id> "name"` | Rename session |
| `kiro-session config` | View/set configuration |

## Private Sessions

If a user asks for a private conversation, says "don't save this", or wants to ask something sensitive, respond with:

> This conversation is already being recorded. To start a private session that won't be saved, open a new terminal and run:
> ```
> kiro-session private
> ```
> Everything in that session will be automatically deleted when you exit.

Do NOT run `kiro-session private` from within an existing chat — the child session's content would be captured in the parent session's tool results, defeating the purpose.

## Technical Notes

- Index file: `~/.kiro/session-index.db` (auto-updated on every command)
- Coexists with native kiro-cli session management; index self-heals if sessions are deleted externally
- Embedding model: BAAI/bge-small-zh-v1.5 via fastembed (local, no API key)
