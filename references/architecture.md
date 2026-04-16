# Architecture

## Overview

kiro-session is a read-only index layer on top of Kiro CLI's session storage. It never writes to kiro-cli's data — all mutations go through `kiro-cli chat --delete-session`. Own data lives in a separate SQLite database.

```
┌─────────────────────────────────────────────────┐
│  User                                           │
│  kiro-session list / search / private / ...     │
└──────────────┬──────────────────────────────────┘
               │
┌──────────────▼──────────────────────────────────┐
│  Wrapper (kiro-session bash script)             │
│  - Cleanup startup check                        │
│  - Delegates to kiro_session.py                 │
└──────────────┬──────────────────────────────────┘
               │
┌──────────────▼──────────────────────────────────┐
│  kiro_session.py (command routing)              │
│  ┌────────────┐  ┌──────────┐  ┌─────────────┐ │
│  │ Extractor  │  │ Index DB │  │ LLM Provider│ │
│  │ (read-only)│  │ (SQLite) │  │ (kiro/ollama│ │
│  └─────┬──────┘  └────┬─────┘  └──────┬──────┘ │
│        │              │               │         │
└────────┼──────────────┼───────────────┼─────────┘
         │              │               │
    ┌────▼────┐    ┌────▼────┐    kiro-cli chat
    │ kiro DB │    │ index   │    --no-interactive
    │ SQLite  │    │ .db     │
    │ (v1)    │    └─────────┘
    ├─────────┤
    │ JSONL   │
    │ files   │
    │ (v2)    │
    └─────────┘
```

## Data Sources (read-only)

### v1: SQLite DB

`~/.local/share/kiro-cli/data.sqlite3` → `conversations_v2` table.

### v2: JSON + JSONL files

`~/.kiro/sessions/cli/` → `<id>.json` (metadata) + `<id>.jsonl` (conversation events).

`extractor.py` scans both and merges into a unified index. `read_session_data()` provides a single interface to read from either source.

## Index Layers

### Layer 0: Local Index (automatic, zero cost)

Runs on every startup, incremental (<500ms). Extracts:
- Session metadata (id, directory, timestamps, turn counts)
- Transcript text → FTS5 full-text index
- File paths, commands, tools used
- Auto-tags (inferred from file types, directories)
- Keywords (frequency analysis)

### Layer 1: LLM Enrichment (on-demand)

Triggered by `kiro-session index`, detail page `[i]`, or background auto. Produces:
- Better session names
- Semantic topic groups with summaries
- Smart tags

Uses chunk-analyze-merge for large sessions (50+ turns).

## Module Structure

```
scripts/
├── kiro_session.py      # CLI entry + command routing
├── extractor.py         # Read-only scan of kiro DB + JSONL
├── index_store.py       # Index SQLite (WAL mode)
├── searcher.py          # FTS5 fast search + LLM smart search
├── llm_provider.py      # LLM abstraction (kiro/ollama/API)
├── splitter.py          # Topic analysis + resume by topic
├── ui.py                # Interactive UI (picker, detail page)
└── config.py            # Configuration management
```

## Key Flows

### Resume (full or by topic)

1. `read_session_data()` reads from SQLite or JSONL
2. For topic: cherry-pick turns by `turn_indices`
3. Write temp JSON to `~/.kiro/tmp/`
4. Print `cd <dir> && kiro-cli chat` + `/chat load <path>`
5. User loads in kiro-cli → creates new independent session
6. Temp files auto-cleaned (topic: after derivation recorded, resume: after 1 day)

### Private Session

1. `kiro-session private` runs kiro-cli in `~/.kiro/skills/session-manager/private/`
2. Normal exit → `finally` block deletes session from kiro DB
3. Abnormal exit → `_cleanup_private_dir()` runs before next scan

### Privacy: exclude_dirs

1. `config privacy.exclude_dirs /path` → immediate purge of matching sessions
2. Every scan: matching sessions auto-deleted from kiro DB before indexing

### Search

- Fast (default): FTS5 with BM25 ranking, snippets via `snippet()`
- Smart (`--smart`): LLM judges relevance from session summaries

### Delete

All deletes go through `kiro-cli chat --delete-session` (handles both v1/v2 storage), then clean our index.

## Index Store

`~/.kiro/session-index.db` (SQLite, WAL mode). Tables: `sessions`, `turns`, `fts_content` (FTS5), `files_used`, `commands`, `topics`, `derivations`.

See [v2-design.md](v2-design.md) for full schema.
