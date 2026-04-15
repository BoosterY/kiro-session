# kiro-session

Interactive session manager for [Kiro CLI](https://kiro.dev/docs/cli/) — browse, search, split, save/restore, and clean up your chat sessions.

📺 **[See the interactive showcase](https://boostery.github.io/kiro-session/)**

## Why

Kiro CLI's built-in session management (`--list-sessions`, `--resume-picker`) shows only session IDs and one-line summaries. When you have dozens of sessions across multiple projects, it's hard to find what you need. And if you discussed multiple topics in one long session, there's no way to separate them.

kiro-session solves this with:
- **Rich browsing** — interactive picker with names, topics, directory, and lineage
- **Full-text search** — find sessions by any keyword in the conversation
- **Semantic splitting** — LLM groups turns by meaning (not sequence), so related turns stay together even if interleaved
- **Preference learning** — split suggestions improve based on your feedback
- **Lifecycle management** — cleanup suggestions with safety guardrails (no auto-deletion)

## Install

### Quick install (from tarball)

```bash
tar xzf kiro-session-v0.3.0.tar.gz
cd session-manager
./install.sh
```

The installer checks dependencies, creates a Python venv, installs the `pick` library, and symlinks the CLI command.

### Requirements

- Python 3.10+
- python3-venv
- Kiro CLI installed and configured

## Quick Start

```bash
# Browse all sessions interactively
kiro-session

# List sessions (non-interactive)
kiro-session list --plain

# Search by keyword
kiro-session list --search "docker"

# Deep search (searches full conversation content)
kiro-session list --search "error" --deep

# Filter by directory (basename or full path)
kiro-session list --dir temp

# Filter by recency
kiro-session list --recent 7d

# Split a long session into topic-based sessions
kiro-session split

# Export a session to a file
kiro-session save abc12345

# Restore from file
kiro-session restore session-abc12345.json

# Review cleanup suggestions
kiro-session cleanup
```

## Session Detail & Actions

Select a session in the interactive browser to see its detail page:

```
============================================================
Session: API Gateway Migration Plan
ID:      612381ac
Dir:     /home/user/docs
Updated: 4d ago
Turns:   58 prompts

Topics (3):
  1. REST API endpoint refactoring
  2. Auth middleware integration
  3. Load testing and optimization
============================================================
⚡ Basic index only. Run 'kiro-session index' for better summaries and split suggestions.

  [r] Resume  — continue this conversation in Kiro CLI
  [s] Split   — break into topic-based sessions
  [v] Save    — export to JSON file
  [d] Delete  — remove this session from DB
  [i] Index   — generate LLM summary for this session (~5s)
  [b] Back    [q] Quit
```

- `[i]` only appears for sessions without LLM summaries (marked ⚡ in the list)
- `[r]` marks the session as most recent and gives precise resume commands for both terminal and TUI mode
- `[d]` requires confirmation before deleting

## Commands

| Command | Description |
|---------|-------------|
| `kiro-session` | Interactive session browser (default) |
| `kiro-session list [options]` | List/search/filter sessions |
| `kiro-session index` | Build LLM index (runs automatically in background) |
| `kiro-session split [id]` | Interactive topic splitting |
| `kiro-session undo-split [id]` | Revert a split |
| `kiro-session save <id> [path]` | Export session to JSON |
| `kiro-session restore <path>` | Import session from JSON |
| `kiro-session cleanup` | Review and clean up stale sessions |

### list options

| Flag | Description |
|------|-------------|
| `--search`, `-s` | Filter by keyword (searches index metadata) |
| `--deep` | Search full conversation content (use with `--search`) |
| `--dir`, `-d` | Filter by directory (basename or full path) |
| `--recent`, `-r` | Filter by recency (e.g. `7d`, `24h`) |
| `--plain` | Non-interactive output |

## How It Works

### Indexing

Session metadata is cached in `~/.kiro/session-index.json`. Updated lazily on every command (<100ms). LLM indexing generates better names, topic summaries, and semantic split groups.

LLM indexing runs automatically in the background:
- Triggers when ≥3 sessions are unindexed and last index was >1 day ago
- Starts after a 30-minute delay to avoid competing with active work
- Can also be triggered manually: `kiro-session index` or `[i]` in session detail

### Splitting

LLM groups conversation turns by semantic meaning and project context — not by sequential order. If you discussed Docker at turns 0-5 and returned to it at turns 11-15, those turns are grouped together.

You review a preview, accept or give natural language feedback ("merge the first two"), and LLM adjusts. Preferences are learned over time (after 3+ feedbacks).

### Cleanup

- Sessions >60 days old with ≤1 turn trigger a startup warning
- Split parent sessions are suggested for archival after 30 days (when all children exist)
- **No auto-deletion** — all cleanup actions require user confirmation

### Compatibility

Works alongside native Kiro CLI session commands. If you delete a session with `--delete-session`, the index self-heals on next access. LLM calls use kiro-cli's own headless mode — no API keys or extra config needed. Temporary sessions created by LLM calls are automatically cleaned up.

## Startup Notifications

Every time you run `kiro-session`, the wrapper shows relevant hints:
- ⚠ Stale sessions (>60d) → `kiro-session cleanup`
- ℹ Sessions without LLM summaries (auto-indexing in background)
- ✂ Sessions with multiple topics → `kiro-session split`

## Architecture

See [references/architecture.md](references/architecture.md) for design details.

## License

MIT
