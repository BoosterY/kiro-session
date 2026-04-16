# Kiro CLI Database Schema

kiro-cli stores sessions in two backends. kiro-session reads both (never writes).

## v1: SQLite DB

Location: `~/.local/share/kiro-cli/data.sqlite3`

### Table: conversations_v2

| Column | Type | Description |
|--------|------|-------------|
| key | TEXT (PK1) | Directory path where session was created |
| conversation_id | TEXT (PK2) | UUID session identifier |
| value | TEXT | Full conversation JSON blob |
| created_at | INTEGER | Unix timestamp ms |
| updated_at | INTEGER | Unix timestamp ms |

### Conversation JSON (value column)

```json
{
  "conversation_id": "uuid",
  "history": [
    {
      "user": {
        "content": {
          "Prompt": { "prompt": "user text" }
        },
        "timestamp": "ISO8601"
      },
      "assistant": {
        "Response": { "message_id": "uuid", "content": "text" }
      }
    }
  ],
  "transcript": ["simplified text entries..."]
}
```

User content variants:
- `content.Prompt.prompt` — actual user input
- `content.ToolUseResults` — tool responses (skip when summarizing)

Assistant content variants:
- `assistant.Response.content` — text-only response
- `assistant.ToolUse.content` + `tool_uses` — response with tool calls

## v2: JSON + JSONL files

Location: `~/.kiro/sessions/cli/`

Each session has two files:

### Metadata: `<session-id>.json`

```json
{
  "session_id": "uuid",
  "cwd": "/path/to/project",
  "created_at": "ISO8601",
  "updated_at": "ISO8601",
  "title": "session title",
  "session_state": {}
}
```

### Conversation: `<session-id>.jsonl`

One event per line:

```jsonl
{"kind": "Prompt", "data": {"content": [{"type": "text", "data": "user text"}]}}
{"kind": "AssistantMessage", "data": {"content": "assistant response"}}
{"kind": "ToolResults", "data": {"results": [...]}}
```

## kiro-session Index

Location: `~/.kiro/session-index.db` (SQLite, WAL mode)

See [v2-design.md](v2-design.md) for full schema definition.
