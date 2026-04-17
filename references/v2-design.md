# kiro-session v2 Design

## Core Principles

1. **Read-only on kiro-cli DB** — never write to `conversations_v2`. All mutations go through kiro-cli's public CLI interface (`--delete-session`).
2. **Own data in own SQLite** — all index, topic, derivation data stored in `~/.kiro/session-index.db`.
3. **Local first** — usable without LLM (Layer 0). LLM is enhancement, not requirement.
4. **LLM on by default** — Layer 1 auto-enriches in background, but degrades gracefully if unavailable.
5. **Temp files are ephemeral** — generated on demand (resume by topic), cleaned up automatically.

## Project Boundary

| Project | Scope |
|---------|-------|
| kiro-session | Find + Organize + basic Reuse (resume/save/restore) |
| kiro-knowledge (future) | Export markdown, knowledge graph, solution extraction |

Shared interface: kiro-session's index SQLite.

## Data Source

kiro-cli has **two session storage backends**:

### Storage 1: SQLite DB (legacy, labeled "v1" in kiro-cli)

`~/.local/share/kiro-cli/data.sqlite3`, read-only.

Table `conversations_v2`:
- `key` — working directory
- `conversation_id` — unique ID
- `value` — JSON blob containing:
  - `history[]` — per-turn: user prompt, assistant response, tool_use, tool_result
  - `transcript[]` — rendered text (what user actually saw)
  - `env_context` — per-turn working directory, OS
  - `request_metadata` — request IDs, context usage
- `updated_at` — timestamp for change detection

All sessions across all directories are in this single table.

### Storage 2: JSON + JSONL files (new, labeled "v2" in kiro-cli)

`~/.kiro/sessions/cli/`, read-only.

Each session has two files:
- `<session-id>.json` — metadata:
  - `session_id` — unique ID
  - `cwd` — working directory
  - `created_at`, `updated_at` — ISO timestamps
  - `title` — session title
  - `session_state` — internal state dict
- `<session-id>.jsonl` — conversation events, one per line:
  - `kind: "Prompt"` — user message, `data.content[].data` = text
  - `kind: "AssistantMessage"` — assistant response
  - `kind: "ToolResults"` — tool execution results

### Extractor must read both storages

Layer 0 scans both sources and merges into a unified index. Session IDs are unique across both storages. If a session exists in both (migration scenario), prefer the newer `updated_at`.

## Index Layers

### Layer 0: Local Index (automatic, zero dependency)

**Trigger**: every startup, synchronous, incremental.

**Performance**:
- Incremental (1-3 changed sessions): < 500ms
- Full rebuild (50+ sessions, first time): < 10s (show progress)
- No change detected: < 50ms

**Token cost**: zero.

**Process**:
1. Quick change detection: compare `updated_at` from kiro DB vs our index
2. Detect externally deleted sessions: IDs in our index but not in kiro DB → remove from index
3. For changed/new sessions only: `json.loads(value)`, extract structured data
4. Clean up temp files for completed derivations
5. Write to our index SQLite (batch inserts in transactions)

**Output**:
- Session metadata (id, directory, created_at, updated_at, turn counts)
- Transcript text → FTS5 full-text index (hyphen/space normalized)
- File paths → `files_used` table
- Commands → `commands` table
- High-frequency keyword extraction
- Auto-tags (inferred from file types, directory names, commands)
- Session name = high-frequency keyword combination or first prompt truncated
- Empty sessions (0 user turns) are indexed with `user_turn_count = 0` for cleanup suggestions

### Layer 1: LLM Enrichment (on by default)

**Triggers**:
- Background auto: every startup, processes unindexed sessions (≥2 turns) — no frequency limits, no delay
- Manual bulk: `kiro-session index` (immediate)
- On-demand single: detail page `[i]` or split request
- Always pre-runs Layer 0 before starting

**Performance**: 3-10s per session.

**Token cost**: ~500-2000 tokens/session. No budget limits — all unindexed sessions are processed automatically.

**Large session strategy**: for sessions with 50+ turns, use chunk-analyze-merge:
1. Split turns into chunks of ~50 turns each
2. Each chunk: extract prompt/response summaries (first 200 chars each) → LLM analyzes topics
3. Merge: LLM reviews all chunk topic lists, merges same-title topics, deduplicates turn indices
4. Cost: ~4000 tokens/chunk + ~2000 tokens merge. 500-turn session ≈ 42000 tokens total.

**Output**:
- High-quality session name
- Semantic topic groups + summary
- Smart tags

**Concurrency**: no lock files or coordination needed. Both background and manual processes check `llm_enriched` flag before processing each session. SQLite WAL mode ensures concurrent read/write safety. Worst case: two processes enrich the same session simultaneously — harmless, last write wins.

**Re-indexing on session change**: when Layer 0 detects a session's `updated_at` has changed (e.g. user resumed and continued the conversation), it re-extracts all structured data, resets `llm_enriched = 0`, and clears existing topics. Layer 1 will re-enrich the session on its next run.

## Search Design

One search interface, two independent modes:

### fast mode (default for CLI)

FTS5 full-text search.
- < 50ms, zero tokens
- Case insensitive
- Prefix matching (`dock*` → docker, dockerfile)
- Hyphen/underscore/space normalization (`docker-compose` = `docker compose`)
- BM25 relevance ranking
- Results include context snippets via FTS5 `snippet()` function

**Known limitation**: FTS5 `unicode61` tokenizer splits Chinese text by character, not by word. Searching "容器" works but precision is lower than word-level tokenization. Acceptable for phase 1; ICU tokenizer or jieba can be evaluated later.

### smart mode (default for skill)

LLM receives all session summaries and judges relevance.
- 3-10s, consumes tokens
- No FTS5 pre-filtering — preserves LLM's semantic understanding
- Can find "Docker networking" when user searches "容器网络"

**Summary source per session**:
- LLM-enriched sessions: LLM name + topics + smart tags + user tags
- Not yet enriched: first prompt truncated + auto-tags + keywords + user tags

**All summaries sent at once** — no pagination. Each summary is compressed to ~one line:
```
"612381ac | API Gateway Migration | topics: REST API, Auth, Load test | tags: docker, nginx | 4d ago"
```
200 sessions × ~100 chars ≈ 5000 tokens — well within model limits.

If filters are provided, SQL narrows candidates first, then filtered summaries go to LLM.

**Result display**: LLM returns matched sessions with a one-sentence explanation of why each matched.

### Filters (both modes)

Filters are independent of search mode. Applied as SQL WHERE clauses.

```bash
kiro-session search "error"                        # search only
kiro-session search "error" --file app.py           # search + filter
kiro-session list --file app.py --recent 7d         # filter only
```

| Filter | Source |
|--------|--------|
| `--file <path>` | `files_used` table |
| `--cmd <command>` | `commands` table |
| `--dir <directory>` | `sessions.directory` (basename or full path) |
| `--recent <duration>` | `sessions.updated_at` |

## Tags

Two types of tags, both searchable:

- **auto_tags**: generated by Layer 0 (file types, directory names, commands) and Layer 1 (LLM smart tags)
- **user_tags**: manually added/edited by user

```bash
kiro-session tag 612381ac "docker" "production-issue"
kiro-session tag 612381ac --remove "production-issue"
```

Also editable via `[t] Edit tags` in detail page.

## Topic Splitting

No new sessions written to kiro DB.

### Data model

Layer 1 produces topic groups, stored in our index:

```sql
-- topics table
session_id: "612381ac"
topic_index: 0
title: "Docker config"
summary: "Set up Docker Compose, configured networking..."
turn_indices: "[0,1,2,11,12]"  -- JSON array, can be non-contiguous
```

Storage cost: ~hundreds of bytes per session. No source data copied.

### Topic feedback

When topic grouping is unsatisfactory, users can provide feedback via `[f]` in the detail page. The LLM receives:
- Previous topic grouping (titles + turn indices)
- User's feedback text (e.g. "merge topics 1 and 3", "Docker topics should be separate")
- Original conversation excerpt

The LLM re-analyzes and produces updated topics. For large sessions (chunk-analyze-merge), feedback is injected into the merge phase.

### Resume by topic

1. User selects topic number in detail page
2. Read original session from kiro DB
3. Cherry-pick corresponding turns from history
4. Generate temp JSON file (`~/.kiro/tmp/<id>-topic-<n>.json`) with source marker:
   ```json
   {
     "conversation_id": "new-uuid",
     "history": [...cherry-picked turns...],
     "_kiro_session_source": {
       "source_id": "612381ac",
       "topic_index": 2,
       "topic_title": "Fix bug A"
     }
   }
   ```
5. Display resume commands (terminal + TUI)
6. User runs `/chat load <path>` in kiro-cli → kiro-cli creates new independent session
7. On next Layer 0 scan:
   - Detect new session containing `_kiro_session_source` field
   - Auto-record derivation in our index
   - Delete temp file (no longer needed)

**Limitation**: kiro-cli does not support loading a session file directly from CLI args. `/chat load` is an interactive slash command, requiring the user to first start kiro-cli then type the command. This is a kiro-cli limitation.

### Tool trust inference

When generating resume commands, we infer `--trust-tools` from the session's actual tool usage:

```python
# Aggregate all tools used across turns
used_tools = index_db.execute(
    "SELECT DISTINCT json_each.value FROM turns, json_each(tools_used) WHERE session_id = ?",
    (session_id,)
)
```

Generated resume command includes the trust flag:
```
cd /home/user/project && kiro-cli chat --resume --trust-tools=fs_read,fs_write,execute_bash,grep,glob
```

This is a best-effort inference — the original session may have had different trust settings. Users can edit the command before running.

### Derivation tracking

```sql
CREATE TABLE derivations (
    source_session_id TEXT,
    topic_index INTEGER,
    derived_session_id TEXT,
    root_session_id TEXT,        -- ultimate origin session (for multi-hop chains)
    created_at INTEGER
);
```

**Recording**: automatic via `_kiro_session_source` marker detection during Layer 0 scan. `root_session_id` is inherited from the source's root, or set to source_session_id if source has no root.

**Purpose**:
- Cleanup suggestions: when all topics of a source session have derived sessions (fully derived), suggest archiving the source
- Detail page display: show derivation status per topic
- Avoid duplicate: mark topics that already have derived sessions
- Visualization: `root_session_id` enables grouping all related sessions into a tree for future kiro-knowledge visualization

### Temp file lifecycle

```
Created:  when user selects resume (full or by topic)
Cleaned:  topic files — next Layer 0 scan, after derivation is recorded
          resume files — auto-deleted after 1 day
          OR kiro-session cleanup (catches orphaned temp files)
Location: ~/.kiro/tmp/
```

## Restore

```bash
kiro-session restore session.json
```

Restores a previously saved session via `/chat load`. Same two-step process as resume by topic (kiro-cli limitation). On next Layer 0 scan, the restored session is automatically detected and indexed.

## LLM Provider Layer

Auto-detect available providers, priority order:

1. kiro-cli headless (default, zero config)
2. ollama (local, free)
3. Direct API (requires user-configured key)
4. None (degraded mode, Layer 0 only)

User can override via `kiro-session config llm.provider <name>`.

Provider interface:

```python
class LLMProvider:
    def query(self, prompt: str, timeout: int = 60) -> str | None
    def is_available(self) -> bool
```

## Configuration

File: `~/.kiro/session-config.yml`

```yaml
llm:
  provider: auto          # auto | kiro | ollama | openai | none
  auto_enrich: true       # auto-enrich unindexed sessions on every startup (background)
                          # no frequency limits — runs whenever unindexed sessions exist
                          # set to false to disable; use 'kiro-session index' for manual control

privacy:
  exclude_dirs: []        # directories to skip during indexing
  exclude_sessions: []    # session IDs to skip
```

CLI:
```bash
kiro-session config                          # show current config
kiro-session config llm.provider kiro        # set value
kiro-session config llm.auto_enrich false    # set value
```

## Index Store Schema

`~/.kiro/session-index.db` (SQLite, WAL mode):

```sql
PRAGMA journal_mode=WAL;

CREATE TABLE sessions (
    id TEXT PRIMARY KEY,
    name TEXT,
    directory TEXT,
    created_at INTEGER,
    updated_at INTEGER,
    user_turn_count INTEGER,
    total_turn_count INTEGER,
    llm_enriched BOOLEAN DEFAULT 0,
    auto_tags TEXT,               -- JSON array, from Layer 0/1
    user_tags TEXT,               -- JSON array, user manual
    keywords TEXT                 -- JSON array, from frequency analysis
);

CREATE TABLE turns (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT,
    turn_index INTEGER,
    user_prompt TEXT,
    assistant_response TEXT,
    working_dir TEXT,
    files_touched TEXT,           -- JSON array
    commands_run TEXT,            -- JSON array
    tools_used TEXT,              -- JSON array
    timestamp INTEGER
);

CREATE VIRTUAL TABLE fts_content USING fts5(
    session_id,
    turn_index,
    content,                     -- transcript text, hyphen/space normalized
    tokenize='unicode61'
);

CREATE TABLE files_used (
    session_id TEXT,
    turn_index INTEGER,
    file_path TEXT,
    operation TEXT                -- read/write/create/delete
);

CREATE TABLE commands (
    session_id TEXT,
    turn_index INTEGER,
    command TEXT,
    exit_code INTEGER
);

CREATE TABLE topics (
    session_id TEXT,
    topic_index INTEGER,
    title TEXT,
    summary TEXT,
    turn_indices TEXT             -- JSON array
);

CREATE TABLE derivations (
    source_session_id TEXT,
    topic_index INTEGER,
    derived_session_id TEXT,
    root_session_id TEXT,
    created_at INTEGER
);
```

## Trigger Timing

| Action | Trigger | Blocking? |
|--------|---------|-----------|
| Layer 0 incremental | Every startup, auto | Sync, < 500ms |
| Layer 0 full rebuild | First run or `index --rebuild` | Sync, < 10s |
| Layer 1 background | Every startup (if unindexed sessions exist), auto | Background |
| Layer 1 single session | Detail `[i]` / split on-demand | Sync, 3-10s |
| Layer 1 manual bulk | `kiro-session index` | Sync |
| Resume by topic | User selects topic number | Sync, generates temp file |
| Delete | User confirms | Sync, kiro-cli `--delete-session` + index cleanup |
| Cleanup reminder | Startup detection | Non-blocking, one line |
| Cleanup execution | `kiro-session cleanup`, user confirms | Sync |
| Temp file cleanup | Layer 0 scan / cleanup command | Auto |
| External deletion sync | Layer 0 scan | Auto |

## Commands

| Command | Description |
|---------|-------------|
| `kiro-session` | Interactive session browser (default) |
| `kiro-session list [filters]` | List/filter sessions |
| `kiro-session search <query> [filters] [--smart]` | Search sessions (fast default, --smart for LLM) |
| `kiro-session index [--rebuild]` | Build/rebuild LLM index |
| `kiro-session save <id> [path]` | Export session to JSON |
| `kiro-session restore <path>` | Import session from JSON |
| `kiro-session delete <id>` | Delete session (kiro DB + index) |
| `kiro-session delete-topic <id> --topic <N>` | Delete a sensitive topic (preserves others) |
| `kiro-session tag <id> [tags...] [--remove tag]` | Add/remove user tags |
| `kiro-session cleanup` | Review and clean up sessions |
| `kiro-session redact <id> --turn <N>` | Remove a turn from index only |
| `kiro-session config [key] [value]` | View/set configuration |

All non-interactive commands support `--json` for structured output (used by skill integration).

## Interactive Design

### Picker list

```
Sessions (↑↓/jk navigate, Enter select, q quit)
⚡= LLM Index Pending

→ ⚡ 5d32ae37  Debug production logs...  (2d, 53 turns, project-x)
     612381ac  API Gateway Migration  (4d, 3 topics, docs)
  ⚡ bcc7b244  Reply with exactly: OK  (3h, 1 turn, temp)
```

### Detail page

```
============================================================
Session: API Gateway Migration
ID:      612381ac
Dir:     /home/user/docs
Updated: 4d ago
Turns:   58 prompts
Tags:    [docker] [nginx] [production-issue]

Topics (3):
  1. REST API endpoint refactoring
     Refactored /api/v1 endpoints, added pagination support.
  2. Auth middleware integration  ✔ derived
     Integrated JWT auth middleware, configured CORS.
  3. Load testing and optimization
     Set up k6 load tests, optimized DB queries.
============================================================

  [r] Resume full session
  [1-3] Resume by topic
  [t] Edit tags
  [v] Save    [d] Delete
  [x] Delete topic
  [f] Feedback (re-analyze topics)
  [i] Index   ← only when not LLM enriched
  [b] Back    [q] Quit
```

With cleanup marker:
```
Session: test  ⚠ Suggested for cleanup (stale, 95d, 1 turn)
```

Or fully derived:
```
Session: API Gateway Migration  📦 Fully derived (3/3 topics)
```

### Search results

fast mode (FTS5 with snippets):
```
612381ac  API Gateway Migration  (4d, docs)
  ...configured nginx reverse proxy for docker containers...

5d32ae37  Debug production logs  (2d, project-x)
  ...nginx error 502 bad gateway in production...
```

smart mode (LLM with explanations):
```
612381ac  API Gateway Migration  (4d, docs)
  Discussed Docker container networking and nginx reverse proxy setup.

5d32ae37  Debug production logs  (2d, project-x)
  Debugged nginx 502 errors related to container networking issues.
```

### Cleanup

Startup reminder:
```
⚠ 3 session(s) suggested for cleanup. Run: kiro-session cleanup
```

`kiro-session cleanup`:
```
🗑 Stale sessions (>90d, ≤2 turns):
  e4efb70f  test  (95d ago, 1 turn)
  93fb0554  hi  (102d ago, 1 turn)

🗑 Empty sessions (0 turns):
  a1b2c3d4  (empty)  (3d ago)

📦 Fully derived sources (>30d, all topics have derived sessions):
  612381ac  API Gateway Migration  (45d ago, 3/3 topics derived)

Delete all suggested? [y/N] or enter IDs to select:
```

## Privacy

### Exclude from indexing

```yaml
# ~/.kiro/session-config.yml
privacy:
  exclude_dirs:                  # sessions in these dirs are not indexed
    - /home/user/personal
  exclude_sessions: []           # specific session IDs to skip
```

Layer 0 skips matching sessions during scan.

### Delete sensitive data from index

```bash
kiro-session redact <session-id> --turn 5    # remove specific turn from index only
```

Removes the turn's content from our index (fts_content, turns, files_used, commands). Does not modify kiro DB. FTS5 `optimize` is run after redaction to physically purge deleted content from disk.

### Private sessions

```bash
kiro-session private          # start private session
kiro-session private -a       # with all tools trusted
```

Runs kiro-cli in a sandboxed directory (`~/.kiro/skills/session-manager/private/`). Two-layer cleanup:

1. **Normal exit**: `cmd_private` wrapper deletes session from kiro DB immediately after kiro-cli exits
2. **Abnormal exit** (window close, crash): `ensure_index_fresh` calls `_cleanup_private_dir()` before scanning, deleting any leftover private sessions

Only deletes local data. Content sent to LLM provider may be retained server-side.

### Purge all index data

```bash
kiro-session config privacy.purge
```

Deletes the entire `session-index.db`. kiro DB is unaffected. Next startup rebuilds from scratch.

### Delete topic (privacy-motivated split)

When a session contains a sensitive topic that needs to be permanently removed from kiro DB:

```bash
kiro-session delete-topic <session-id> --topic <N>
```

Process:
1. Show topic breakdown with turn assignments
2. Auto-generate new sessions for all OTHER topics (resume by topic for each)
3. User confirms: clearly lists which turns will be permanently deleted
4. Wait for user to `/chat load` each generated session
5. After confirmation, delete original session via `kiro-cli chat --delete-session`

```
kiro-session delete-topic 612381ac --topic 2

Topic to delete:
  2. "Auth middleware" (turns: 5,6,7,8,9)

Topics to preserve as new sessions:
  1. "REST API refactoring" (turns: 0,1,2,3,4)
  3. "Load testing" (turns: 10,11,12,13)

⚠ Turns 5-9 will be permanently deleted from kiro DB.
  This requires loading 2 new sessions and deleting the original.

  Step 1: Load preserved topics into kiro-cli:
    cd /home/user/docs && kiro-cli chat
    /chat load ~/.kiro/tmp/612381ac-topic-1.json
    /chat load ~/.kiro/tmp/612381ac-topic-3.json

  Step 2: Confirm deletion of original session.

Proceed? [y/N]
```

⚠ If topic assignments are inaccurate, some turns may be lost. User should review topic breakdown before confirming.

## Delete Behavior

All deletes require user confirmation.

**Deleting any session** (source or derived):
1. Call `kiro-cli chat --delete-session <id>` (public CLI interface, deletes from kiro DB)
2. Remove from our index: sessions, turns, fts_content, files_used, commands, topics
3. Clean derivations table (remove rows where this session is `derived_session_id`)
4. If source session: keep derivation records with source marked as deleted

Derived sessions are fully independent — deleting source does not affect derived, and vice versa.

**External deletion detection**: Layer 0 scan compares our index IDs against kiro DB. Sessions in our index but not in kiro DB are automatically cleaned from our index.

## Module Structure

```
scripts/
├── kiro_session.py      # CLI entry + command routing
├── extractor.py         # Extract structured data from kiro DB (read-only)
├── index_store.py       # Index SQLite read/write (WAL mode)
├── searcher.py          # Search engine (fast FTS5 + smart LLM)
├── llm_provider.py      # LLM abstraction (auto-detect + fallback)
├── splitter.py          # Topic analysis + resume by topic
├── ui.py                # Interactive UI (picker, detail, preview)
└── config.py            # Configuration management
```

Dependencies: `pick` (interactive picker), `orjson` (fast JSON parsing), `pyyaml` (config).

## Skill / In-Chat Integration

When triggered as a skill inside a kiro-cli conversation, use `--json` output mode for clean structured output without ANSI codes, progress indicators, or decorative formatting:

```bash
python3 scripts/kiro_session.py search "keyword" --json
python3 scripts/kiro_session.py list --json --recent 7d
python3 scripts/kiro_session.py list --json --file Dockerfile
```

Output example:
```json
{"results": [{"id": "612381ac", "name": "API Gateway Migration", "dir": "docs", "updated": "4d ago", "turns": 58, "snippet": "..."}]}
```

The LLM in the active conversation parses the JSON and presents results in natural language to the user. Interactive operations (browse, split, tag) are not available in skill mode — the skill guides the user to run `kiro-session` in their terminal.

## TODO

### Provider 抽象层（Future Architecture）

当前架构对 kiro-cli 有硬耦合。如果要支持其他 AI CLI 工具（Claude Code、Cursor、Copilot Chat 等），需要抽象 provider 接口。

耦合点分析：

| 耦合点 | 当前实现 | 抽象后 |
|--------|---------|--------|
| 数据读取 | 直接读 kiro SQLite + JSONL | `provider.list_sessions()` / `provider.read_session(id)` |
| 数据删除 | `kiro-cli chat --delete-session` | `provider.delete_session(id)` |
| LLM 调用 | `kiro-cli chat --no-interactive` | `llm_provider.query()` (已部分抽象) |
| Resume | 生成 kiro 格式 JSON + `/chat load` | `provider.generate_resume(id, turns)` |
| Private | 依赖 kiro cwd-based 存储 | `provider.start_private()` / `provider.cleanup_private()` |

目标接口：

```python
class SessionProvider(ABC):
    """Abstract interface for AI CLI session storage."""
    def list_sessions(self) -> list[dict]:
        """Return [{id, directory, updated_at, ...}]"""
    def read_session(self, sid: str) -> dict | None:
        """Return {history: [...], ...} in normalized format"""
    def delete_session(self, sid: str) -> bool:
        """Delete session from tool's storage"""
    def generate_resume_file(self, sid: str, turn_indices: list[int] | None = None) -> Path | None:
        """Generate loadable file for the tool. None = all turns."""
    def resume_instructions(self, sid: str, path: Path) -> str:
        """Return human-readable resume command for the tool"""
```

当前 `extractor.py` 是事实上的 KiroProvider，`llm_provider.py` 已经有 provider 抽象。重构时只需：
1. 从 `extractor.py` 提取 `KiroProvider` 实现
2. index_store / searcher / splitter / ui 只依赖 `SessionProvider` 接口
3. 新工具只需实现 `SessionProvider`，其余代码复用

暂不实施 — 等真正需要支持第二个工具时再抽象，避免提前优化。

### Picker 内搜索

当前 picker 只支持上下导航，不支持搜索。计划加两种：

1. **`/` 本地过滤** — substring match 当前 picker 列表（session 名称、tags、目录），即时过滤，类似 vim `/`
2. **`s` FTS 全文搜索** — 调用 `searcher.search_fast()`，搜索所有 session 的完整对话内容（user prompt + assistant response），返回新的结果列表替换 picker

FTS 已索引全部对话全文（当前 5000+ 条），零 token 消耗。

### 垃圾 session 清理

`--no-interactive` headless 模式（LLM enrichment 调用）产生的垃圾 session 全部存在 v1 SQLite 里。当前 `_cleanup_garbage` 只匹配最近 5 个 session，历史积累的垃圾没清。需要：

1. 启动时扫描所有 v1 session，识别 headless 垃圾（prompt 包含 `Analyze this conversation` / `Merge these topic groups` / `return ONLY a JSON` 等 marker）
2. 自动删除，不需要用户确认（这些 session 对用户无价值）
3. 或者改进 `_cleanup_garbage` 使其在每次 LLM 调用后更可靠地清理（当前依赖 prompt 前 80 字符匹配，多窗口场景可能漏删）

## Key Changes from v0.3.0

| Aspect | v0.3.0 | v2 |
|--------|--------|-----|
| kiro DB access | Read + write (insert splits, delete garbage) | Read-only (delete via kiro-cli CLI) |
| Delete | Only index records | kiro-cli `--delete-session` + index cleanup |
| Index storage | JSON file | SQLite (FTS5 + structured tables, WAL mode) |
| Search | Brute-force string match | FTS5 fast + LLM smart |
| Search results | Session list only | Snippets (fast) / LLM explanations (smart) |
| Filters | `--search`, `--dir`, `--recent` | `--file`, `--cmd`, `--dir`, `--recent` |
| Tags | None | Auto-tags (Layer 0/1) + user manual tags |
| Split | Write new sessions to kiro DB | Topic mapping in index, temp file for resume |
| Derivation | parent/children in kiro DB | `_kiro_session_source` marker, auto-detected |
| LLM calls | kiro-cli headless only | Provider abstraction (kiro/ollama/API/none) |
| LLM concurrency | Lock file + waiting/active states | `llm_enriched` flag, no locks needed |
| Garbage cleanup | Marker mechanism | Not needed (no DB writes) |
| Crash recovery | pending_splits rollback | Not needed (no DB writes) |
| Session name | First prompt or LLM | Keywords + first prompt (Layer 0) or LLM (Layer 1) |
| Cleanup markers | Not shown | Detail page markers (⚠ stale, 📦 fully derived) |
| Config | None | `~/.kiro/session-config.yml` + `config` command |
| Restore | Write to kiro DB | Via `/chat load`, auto-indexed on next scan |
| Privacy | None | Exclude dirs/sessions, redact turns, purge index, delete-topic |
| Re-index | Manual only | Auto on session change (reset llm_enriched) |
| Derivation tree | Flat parent/children | root_session_id for multi-hop chains |
