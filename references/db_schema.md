# Kiro CLI Database Schema

## Location

- Linux: `~/.local/share/kiro-cli/data.sqlite3`
- macOS: `~/Library/Application Support/kiro-cli/data.sqlite3`

## Table: conversations_v2

| Column | Type | Description |
|--------|------|-------------|
| key | TEXT (PK1) | Directory path where session was created |
| conversation_id | TEXT (PK2) | UUID session identifier |
| value | TEXT | Full conversation JSON |
| created_at | INTEGER | Unix timestamp ms |
| updated_at | INTEGER | Unix timestamp ms |

## Conversation JSON Structure

```json
{
  "conversation_id": "uuid",
  "history": [
    {
      "user": {
        "content": {
          "Prompt": { "prompt": "user text" }       // or
          "ToolUseResults": { "tool_use_results": [] }
        },
        "timestamp": "ISO8601"
      },
      "assistant": {
        "Response": { "message_id": "uuid", "content": "text" }  // or
        "ToolUse": { "message_id": "uuid", "content": "text", "tool_uses": [] }
      },
      "request_metadata": {
        "model_id": "string",
        "context_usage_percentage": 0.0
      }
    }
  ],
  "transcript": ["simplified text entries..."],
  "latest_summary": "string or null",
  "model_info": {},
  "context_manager": {},
  "tools": {}
}
```

### Extracting User Prompts

Only `history[n].user.content.Prompt.prompt` contains actual user input.
`ToolUseResults` entries are tool responses and should be skipped when summarizing.

### Extracting Assistant Responses

- `assistant.Response.content` — text-only responses
- `assistant.ToolUse.content` — responses that include tool calls
