# Architecture

## Overview

kiro-session is a CLI tool + Kiro skill that enhances session management by adding an index layer on top of Kiro CLI's SQLite database.

```
┌─────────────────────────────────────────────────┐
│  User                                           │
│  kiro-session list / split / index / ...        │
└──────────────┬──────────────────────────────────┘
               │
┌──────────────▼──────────────────────────────────┐
│  Wrapper (kiro-session bash script)             │
│  - Startup checks & notifications              │
│  - Background auto-index trigger                │
│  - Delegates to kiro_session.py                 │
└──────────────┬──────────────────────────────────┘
               │
┌──────────────▼──────────────────────────────────┐
│  kiro_session.py                                │
│  ┌────────────┐  ┌──────────┐  ┌─────────────┐ │
│  │ Index Mgr  │  │ DB Layer │  │ LLM Bridge  │ │
│  │ (JSON)     │  │ (SQLite) │  │ (headless)  │ │
│  └─────┬──────┘  └────┬─────┘  └──────┬──────┘ │
│        │              │               │         │
└────────┼──────────────┼───────────────┼─────────┘
         │              │               │
   ~/.kiro/         ~/.local/share/   kiro-cli chat
   session-         kiro-cli/         --no-interactive
   index.json       data.sqlite3
```

## Data Flow

### Lazy Indexing (every command)

```
cmd_list / cmd_split / ...
  → ensure_index_fresh()
    → Quick check: SELECT MAX(updated_at), COUNT(*) FROM conversations_v2
    → If unchanged: return (<100ms)
    → If changed: scan for stale sessions, generate basic summaries
    → Save index JSON
```

### Background Auto-Index

```
Wrapper startup:
  → Check .index-last mtime (>24h?) and .index-lock absent
  → If yes: write "waiting" to lock, fork background process
    → sleep 1800 (30 min delay)
    → Check lock still exists (not cancelled by manual index)
    → Write "active" to lock
    → Run cmd_index() → LLM summarize each unindexed session
    → Remove lock
```

### LLM Indexing (kiro-session index)

```
cmd_index()
  → Check lock state: "active" → abort (already running), "waiting" → cancel & take over
  → _recover_pending_splits()     # crash recovery
  → ensure_index_fresh(use_llm=True)
    → For each stale session with ≥2 user turns:
      → _llm_summarize()
        → Build conversation excerpt (first 15 turns, 200 chars each)
        → Inject split_preferences.derived_rules if available
        → Call: _llm_query() → kiro-cli chat --no-interactive
        → Parse JSON: {name, topics: [{title, summary, turns}]}
      → Fallback to _auto_summarize() on failure
  → _auto_split()
    → For sessions with topic groups (≥2 topics with turns arrays):
      → Record pending_splits in index (crash safety)
      → Write new sessions to DB (non-contiguous turn selection)
      → Update index with parent-child relationships
      → Clear pending_splits
```

### LLM Query Pipeline

```
_llm_query(query)
  → Inject unique marker: __kiro_session_tmp_<random>__
  → subprocess.run(kiro-cli chat --no-interactive, stdin=DEVNULL)
  → Query DB for sessions containing marker
  → Delete matched sessions (precise cleanup, multi-window safe)
  → Strip ANSI codes, return output
```

### Interactive Split (kiro-session split)

```
cmd_split()
  → Pick session (interactive or by ID)
  → _split_interactive()
    → Load topic groups from index (semantic, non-contiguous turns)
    → Review loop:
      → _show_split_preview()     # title + summary + turn count
      → User: [e]xecute / [r]etry / [c]ancel
      → On retry:
        → _record_split_feedback()  # store feedback, derive rules after 3+
        → _llm_resplit()            # LLM adjusts topic groups
    → Execute: write new sessions to DB (cherry-pick history entries)
    → Print undo command
```

## Index File Structure

`~/.kiro/session-index.json`:

```json
{
  "version": 1,
  "last_audit": 1776230728829,
  "last_llm_index": 1776230728829,
  "audit_interval_days": 7,
  "sessions": {
    "<conversation_id>": {
      "name": "Session name (from LLM or first prompt)",
      "directory": "/path/to/project",
      "created_at": 1776193008145,
      "updated_at": 1776193008145,
      "message_count": 210,
      "user_turn_count": 53,
      "size_bytes": 1186471,
      "topics": [
        {"title": "Topic title", "summary": "1-2 sentence description", "turns": [0,1,2,3,11,12]},
        {"title": "Another topic", "summary": "What was discussed", "turns": [4,5,6,7,8,9,10]}
      ],
      "parent": null,
      "children": ["child-uuid-1", "child-uuid-2"],
      "llm_indexed": true
    }
  },
  "split_preferences": {
    "feedback_history": ["merge related sub-topics", "too granular"],
    "derived_rules": "User prefers coarse-grained splits..."
  },
  "pending_splits": {}
}
```

## Semantic Topic Grouping

Unlike sequential boundary-based splitting, topics use turn index arrays that can be non-contiguous:

```
Conversation: Docker(0-5) → BugA(6-10) → Docker(11-15) → BugA(16-20)

Sequential split: [0-10] [11-20]  ← mixes topics
Semantic groups:  Docker=[0-5,11-15]  BugA=[6-10,16-20]  ← clean separation
```

When splitting, history entries are cherry-picked by index, preserving full assistant responses and tool use for each selected turn.

## Preference Learning

Split feedback is collected during interactive retry:

1. User gives feedback → stored in `split_preferences.feedback_history` (last 10)
2. After 3+ feedbacks → LLM derives `derived_rules` (1-2 sentence summary)
3. On next index → `derived_rules` injected into LLM summarize prompt
4. LLM generates topic groups that match user's style

## Crash Recovery

The `pending_splits` mechanism ensures atomicity:

```
Normal:   pending_splits[parent] = [child_ids]
          → DB writes
          → del pending_splits[parent]

Crash:    pending_splits[parent] still exists on next run
          → _recover_pending_splits() deletes child sessions from DB
          → Clears pending_splits
          → Parent unchanged, as if split never happened
```

## Cleanup Strategy

- **No auto-deletion** — all deletions require user confirmation
- Sessions >60d with ≤1 turn → startup warning
- Split parents >30d with all children present → suggested for archival in `cleanup`
- Detail page `[d]` Delete → requires `[y/N]` confirmation

## Compatibility with Native Kiro CLI

kiro-session reads from the same SQLite database but never modifies existing sessions (only inserts new ones for splits). The index self-heals when external changes are detected:

- Session deleted externally → removed from index on next access
- Parent deleted → children's `parent` reference cleared
- Child deleted → parent's `children` list cleaned

LLM calls go through `_llm_query()` which uses unique markers to precisely identify and clean up temporary sessions, safe for multi-window use.

## File Layout

```
session-manager/
├── README.md                    # User guide
├── SKILL.md                     # Kiro skill definition
├── kiro-session                 # CLI wrapper (bash)
├── install.sh                   # Auto-installer
├── .gitignore
├── scripts/
│   └── kiro_session.py          # Main script
└── references/
    ├── architecture.md          # This file
    └── db_schema.md             # Kiro CLI database schema
```
