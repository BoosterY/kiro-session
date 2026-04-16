# kiro-session

Interactive session manager for [Kiro CLI](https://kiro.dev/docs/cli/) — browse, search, resume by topic, save/restore, and manage your chat sessions.

## Why

Kiro CLI's built-in session management shows only session IDs and one-line summaries, scoped to the current directory. When you have dozens of sessions across multiple projects, it's hard to find what you need.

kiro-session solves this with:
- **Cross-directory browsing** — see all sessions from all projects in one place
- **Full-text search** — find sessions by any keyword in the conversation
- **Topic splitting** — LLM groups turns by meaning, resume just the topic you need
- **Private sessions** — incognito mode that auto-deletes local data on exit
- **Dual storage support** — reads both SQLite (v1) and JSON/JSONL (v2) kiro-cli sessions

## Install

```bash
git clone <repo> && cd kiro-session
./install.sh
```

The installer creates a Python venv, installs dependencies (`pick`, `orjson`, `pyyaml`), and symlinks `kiro-session` to `~/.local/bin/`.

### Requirements

- Python 3.10+
- Kiro CLI installed and configured

## Quick Start

```bash
kiro-session                          # interactive browser (default)
kiro-session list --plain             # non-interactive list
kiro-session search "docker"          # full-text search
kiro-session list --dir temp          # filter by directory
kiro-session list --recent 7d         # filter by recency
kiro-session list --file "main.py"    # filter by file touched
kiro-session private                  # start a private session
```

## Commands

| Command | Description |
|---------|-------------|
| `kiro-session` | Interactive session browser |
| `kiro-session list [options]` | List/filter sessions |
| `kiro-session search <query>` | Full-text search across all sessions |
| `kiro-session index [--rebuild]` | Build/refresh LLM index |
| `kiro-session save <id> [path]` | Export session to JSON |
| `kiro-session restore <path>` | Import session from JSON |
| `kiro-session delete <id>` | Delete session from kiro DB |
| `kiro-session tag <id> [tags]` | Add/remove user tags |
| `kiro-session cleanup` | Review cleanup suggestions |
| `kiro-session redact <id> --turn N` | Remove a turn from index |
| `kiro-session config [key] [value]` | View/set configuration |
| `kiro-session private [-a]` | Start private session (auto-deleted on exit) |

### list options

| Flag | Description |
|------|-------------|
| `--dir`, `-d` | Filter by directory basename or path |
| `--recent`, `-r` | Filter by recency (e.g. `7d`, `24h`) |
| `--file` | Filter by file touched in session |
| `--cmd` | Filter by command run in session |
| `--plain` | Non-interactive output |
| `--json` | JSON output |
| `<session-id>` | Show detail for specific session |

## Session Detail & Actions

Select a session to see its detail page:

```
============================================================
Session: API Gateway Migration Plan
ID:      612381ac
Dir:     /home/user/docs
Updated: 4d ago
Turns:   58 prompts
Tags:    [api] [migration] [nodejs]

Topics (3):
  1. REST API endpoint refactoring
  2. Auth middleware integration
  3. Load testing and optimization
============================================================

  [r] Resume full session
  [1-3] Resume by topic
  [t] Edit tags
  [v] Save    [d] Delete
  [x] Delete topic
  [f] Feedback (re-analyze topics)
  [i] Index
  [b] Back    [q] Quit
```

- **Resume full** — generates temp JSON, gives `kiro-cli chat` + `/chat load` command
- **Resume by topic** — cherry-picks only the turns for that topic
- **Feedback** — provide feedback on topic grouping, LLM re-analyzes with your guidance
- **Index** — runs LLM enrichment for better names, topics, and tags (~5s)
- Sessions without LLM index are marked ⚡ in the list

## Private Sessions

Start a private/incognito session that is automatically deleted when you exit:

```bash
kiro-session private          # start private session
kiro-session private -a       # with all tools trusted
```

- Runs kiro-cli in a sandboxed directory
- On normal exit: session is immediately deleted from all local storage
- On abnormal exit (window close): cleaned up on next `kiro-session` invocation

**Note:** This only deletes local session data. Content sent to the LLM provider during the session may still be retained server-side per the provider's data policies.

## Privacy

```bash
# Exclude a directory — purges existing sessions and auto-deletes future ones
kiro-session config privacy.exclude_dirs /path/to/sensitive/project

# Purge entire index
kiro-session config privacy.purge

# Redact a specific turn
kiro-session redact abc12345 --turn 3
```

## How It Works

### Architecture

```
Layer 0: Extractor (read-only scan of kiro DB + JSONL files)
Layer 1: LLM Enrichment (names, topics, tags via kiro-cli headless)
Layer 2: UI (pick-based interactive browser + CLI output)
```

### Dual Storage

kiro-cli stores sessions in two backends:
- **v1 (SQLite):** `~/.local/share/kiro-cli/data.sqlite3`
- **v2 (JSON/JSONL):** `~/.kiro/sessions/cli/*.json + *.jsonl`

kiro-session reads both and merges into a unified index at `~/.kiro/session-index.db`.

### Indexing

- Layer 0 runs automatically on every command (<100ms incremental)
- LLM enrichment generates better names, topic summaries, and semantic tags
- Auto-enrichment runs in background on every startup when unindexed sessions exist — no frequency limits
- Can also be triggered manually: `kiro-session index` or `[i]` in session detail

### Resume

Resume uses `/chat load` with a generated temp JSON file. This creates a new session with the loaded history — the original session remains unchanged.

## Configuration

```bash
kiro-session config                        # show all
kiro-session config llm.provider           # get value
kiro-session config llm.provider ollama    # set value
```

| Key | Default | Description |
|-----|---------|-------------|
| `llm.provider` | `auto` | LLM provider: `auto`, `kiro`, `ollama` |
| `llm.auto_enrich` | `true` | Auto-enrich on detail view |
| `privacy.exclude_dirs` | `[]` | Directories to exclude and purge |
| `privacy.exclude_sessions` | `[]` | Session IDs to exclude |

## License

MIT
