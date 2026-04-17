# Kiro CLI Reference (v1.29.6)

kiro-session 的上游依赖。本文档记录 kiro-cli 的存储格式、CLI 接口、slash 命令和已知行为。

## CLI 命令

### 核心命令

| 命令 | 说明 |
|------|------|
| `kiro-cli chat [INPUT]` | 交互式对话 |
| `kiro-cli chat --no-interactive` | Headless 模式（**写 v1 SQLite，不写 v2 JSONL**） |
| `kiro-cli chat --tui` | TUI 模式 |
| `kiro-cli chat --legacy-ui` | 经典 UI |
| `kiro-cli chat -r` / `--resume` | 恢复最近 session |
| `kiro-cli chat --resume-picker` | 交互式选择 session 恢复 |
| `kiro-cli chat -l` / `--list-sessions` | 列出当前目录的 session |
| `kiro-cli chat -d <ID>` / `--delete-session` | 删除 session |
| `kiro-cli chat --session-source <v1\|v2>` | 指定删除的存储源（默认 both） |
| `kiro-cli chat --trust-all-tools` / `-a` | 信任所有工具 |
| `kiro-cli chat --trust-tools=<TOOLS>` | 信任指定工具 |
| `kiro-cli chat --agent <NAME>` | 使用指定 agent |
| `kiro-cli chat --model <MODEL>` | 使用指定模型 |
| `kiro-cli chat --list-models` | 列出可用模型 |
| `kiro-cli chat -f json` | 列表输出为 JSON |

### 其他命令

| 命令 | 说明 |
|------|------|
| `kiro-cli agent list\|create\|edit\|validate\|set-default` | Agent 管理 |
| `kiro-cli mcp add\|remove\|list\|import\|status` | MCP 服务器管理 |
| `kiro-cli settings [key] [value]` | 设置管理 |
| `kiro-cli settings list` | 列出所有设置 |
| `kiro-cli translate <INPUT>` | 自然语言转 shell 命令 |
| `kiro-cli inline enable\|disable\|status` | 行内补全 |
| `kiro-cli acp` | Agent Client Protocol 模式 |
| `kiro-cli update` | 更新 kiro-cli |
| `kiro-cli doctor` | 诊断修复 |
| `kiro-cli diagnostic` | 运行诊断测试 |
| `kiro-cli dashboard` | 打开 dashboard |
| `kiro-cli login\|logout\|whoami\|profile` | 账户管理 |

## Slash 命令（交互式对话内）

| 命令 | 说明 |
|------|------|
| `/chat` | 列出并加载历史 session（交互式 picker） |
| `/chat new [prompt]` | 不重启 CLI 开新 session |
| `/chat save [path]` | 保存当前对话到文件（ConversationState 格式） |
| `/chat load <path>` | 从文件加载对话 |
| `/chat save-via-script <SCRIPT>` | 导出简化 JSON 到自定义脚本 |
| `/context` | 显示 context window token 使用情况 |
| `/context show [--expand]` | 显示 context 规则和匹配文件 |
| `/context add <paths...> [--force]` | 添加临时 context 文件（session-only） |
| `/context remove <paths...>` | 移除 context 规则 |
| `/context clear` | 清除所有 session context |
| `/compact` | 压缩历史释放 context 空间 |
| `/knowledge show\|add\|remove\|update\|clear\|cancel` | 知识库管理（实验性） |
| `/tools` | 查看工具和权限 |
| `/model` | 切换当前 session 的模型 |
| `/todos` | 查看和管理 todo list |
| `/checkpoint` | 工作区快照（与 tangent mode 互斥） |
| `/plan` | 切换到 Plan agent（也可用 Shift+Tab） |
| `/editor` | 打开 $EDITOR 编写 prompt |
| `/reply` | 打开 $EDITOR 引用最近回复 |
| `/clear` | 清除对话历史 |
| `/quit` | 退出 |

## 实验性功能

通过 `/experiment` 交互式切换，或 `kiro-cli settings` 设置：

| 功能 | 设置键 | 说明 |
|------|--------|------|
| Knowledge | `chat.enableKnowledge` | 跨 session 持久化知识库 |
| Thinking | `chat.enableThinking` | 扩展推理工具 |
| Tangent Mode | `chat.enableTangentMode` | 对话分支探索（Ctrl+T） |
| Todo Lists | `chat.enableTodoList` | 任务跟踪 |
| Checkpoint | `chat.enableCheckpoint` | 工作区快照（与 tangent 互斥） |
| Context Usage | `chat.enableContextUsageIndicator` | 显示 context 使用百分比 |
| Delegate | `chat.enableDelegate` | 后台异步 agent 执行 |

## Session 存储

### v1: SQLite DB

位置：`~/.local/share/kiro-cli/data.sqlite3`

**写入场景：`--no-interactive` headless 模式**

表 `conversations_v2`：

| 列 | 类型 | 说明 |
|----|------|------|
| key | TEXT (PK1) | 工作目录路径 |
| conversation_id | TEXT (PK2) | UUID |
| value | TEXT | 完整 ConversationState JSON |
| created_at | INTEGER | Unix 时间戳 ms |
| updated_at | INTEGER | Unix 时间戳 ms |

ConversationState JSON 结构：

```json
{
  "conversation_id": "uuid",
  "next_message": null,
  "history": [...],
  "valid_history_range": {...},
  "transcript": [...],
  "tools": {...},
  "context_manager": {...},
  "context_message_length": 0,
  "latest_summary": null,
  "model_info": {...},
  "file_line_tracker": {...},
  "mcp_enabled": false,
  "user_turn_metadata": [...]
}
```

History entry 格式：

```json
{
  "user": {
    "additional_context": "",
    "env_context": {"env_state": {"operating_system": "linux", "current_working_directory": "..."}},
    "content": {
      "Prompt": {"prompt": "user text"}
    },
    "timestamp": "ISO8601",
    "images": []
  },
  "assistant": {
    "Response": {"message_id": "uuid", "content": "text"}
  },
  "request_metadata": {"model_id": "...", "context_usage_percentage": 0.0}
}
```

User content 变体：
- `content.Prompt.prompt` — 用户输入
- `content.ToolUseResults` — 工具返回（非用户输入）

Assistant 变体：
- `assistant.Response` — 纯文本回复
- `assistant.ToolUse` — 包含 tool_uses 的回复

### v2: JSON + JSONL 文件

位置：`~/.kiro/sessions/cli/`

**写入场景：交互式对话（`kiro-cli chat`，无论 TUI 还是 legacy UI）**

每个 session 包含：
- `<id>.json` — 元数据
- `<id>.jsonl` — 对话事件流
- `<id>/tasks/` — todo list 持久化（可选）

元数据格式：

```json
{
  "session_id": "uuid",
  "cwd": "/path/to/project",
  "created_at": "ISO8601",
  "updated_at": "ISO8601",
  "title": "session title",
  "session_state": {
    "version": "v1",
    "conversation_metadata": {
      "user_turn_metadatas": [...]
    }
  }
}
```

JSONL 事件格式：

```jsonl
{"version": "v1", "kind": "Prompt", "data": {"message_id": "uuid", "content": [{"kind": "text", "data": "user text"}], "meta": {"timestamp": 1234567890}}}
{"version": "v1", "kind": "AssistantMessage", "data": {"message_id": "uuid", "content": [{"kind": "text", "data": "response"}, {"kind": "toolUse", "data": {...}}]}}
{"version": "v1", "kind": "ToolResults", "data": {"message_id": "uuid", "content": [{"kind": "toolResult", "data": {...}}]}}
```

### /chat save 格式

`/chat save` 导出的是 ConversationState（与 v1 SQLite `value` 列相同格式）。`/chat load` 期望相同格式。

**关键：我们生成的 resume/topic 文件必须保留完整 ConversationState 结构，不能只保留 history。**

### /chat save-via-script 格式

简化格式，不含内部状态：

```json
{
  "conversation_id": "uuid",
  "created_at": "timestamp",
  "messages": [
    {"role": "user|assistant", "content": "text", "timestamp": "timestamp"}
  ],
  "metadata": {"agent": "agent_name", "model": "model_name"}
}
```

## 其他存储位置

| 路径 | 内容 |
|------|------|
| `~/.kiro/agents/` | Agent 配置（JSON） |
| `~/.kiro/settings/` | 用户设置（JSON） |
| `~/.kiro/skills/` | Skill 定义 |
| `~/.kiro/kiro-cli.db` | 空（未使用） |
| `~/.local/share/kiro-cli/knowledge_bases/` | 知识库数据 |
| `~/.local/share/kiro-cli/todo-lists/` | Todo list 持久化 |
| `~/.local/share/kiro-cli/webcontexts/` | Dashboard 数据 |
| `~/.local/share/kiro-cli/cli-checkouts/` | 版本切换 |

## 已知行为和注意事项

1. **headless 写 v1**：`--no-interactive` 始终写 SQLite，不写 JSONL。这是确认的行为，不是 bug。
2. **`/compact` 改变 history**：压缩后 history 条目减少，重新索引时 topic 可能变化。
3. **`/context add` 是 session-only**：不持久化，不影响存储。
4. **`--list-sessions` 按目录隔离**：只显示当前 cwd 下的 session。
5. **`--delete-session` 默认删两边**：同时删 v1 和 v2，可用 `--session-source` 指定。
6. **session 可能同时存在于 v1 和 v2**：迁移场景，应以 updated_at 更新的为准。
7. **tangent mode 和 checkpoint 互斥**：启用一个会禁用另一个。
8. **`/chat save-via-script`**：可用作 hook，每次 save 时触发自定义脚本。
