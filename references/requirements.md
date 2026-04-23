# kiro-session 需求文档

## 定位

kiro-cli 的 session 管理增强工具。以 Kiro CLI Skill 形式提供，同时支持独立 CLI 使用。

## 核心需求

### R1. 全局 Session 列表

**问题**：kiro-cli `--list-sessions` 只显示当前目录的 session，用户无法看到所有项目的会话。

**需求**：一个命令列出当前机器上所有 session，跨目录、跨存储（v1 SQLite + v2 JSONL）。

**方案决策**：直接读 kiro 底层存储（非调用 `--list-sessions`）。
- 优点：一次读取所有 session，不依赖目录记录，冷启动友好
- 缺点：依赖 kiro 内部存储格式，格式变更需要适配
- 替代方案（记录目录 + 逐目录 list）已否决：慢、不可靠、冷启动差

**当前状态**：✅ 已实现

### R2. 无感恢复

**问题**：恢复流程需要 3 步手动操作（cd → kiro-cli chat → /chat load）。

**需求**：选中 session 后一步进入 kiro-cli 继续对话。

**方案决策**：`--resume-picker` + PTY 自动选择。
1. Full resume：touch `updated_at` 使目标 session 排到最前，然后 `--resume-picker` 启动 picker
2. PTY 自动化：检测到 `"Select a chat session"` 后，计算目标 session 在 picker 中的位置，发送方向键 + Enter 自动选中
3. Topic resume：生成 cherry-picked ConversationState JSON，写入 kiro DB 为新 session，再通过 `--resume-picker` 选中
4. 无 TTY 环境（skill 调用等）自动 fallback 到打印指令

已否决的方案：
- `--resume`：限当前目录，只恢复最近一个
- `/chat load` + PTY 注入：早期方案，已替换为 `--resume-picker`
- 复制 session 到目标目录：v1 需要写 DB，违反 read-only
- stdin pipe：kiro-cli 处理完 EOF 就退出，无法继续交互

**当前状态**：✅ 已实现
- `kiro-session resume <id>` — 直接启动 kiro-cli
- `kiro-session resume <id> --topic N` — 只恢复某个 topic
- detail page `[r]` / `[1-N]` — 直接启动
- 无 TTY 自动 fallback 到打印指令

### R3. 全局搜索

**问题**：kiro-cli 没有搜索功能。用户记得聊过某个话题但找不到在哪个 session。

**需求**：
- 全文搜索所有 session 的完整对话内容（user prompt + assistant response）
- 支持过滤器：目录、时间、文件、命令
- 搜索结果带上下文 snippet
- picker 内实时搜索

**当前状态**：✅ 已实现
- `kiro-session search <query>` — Hybrid search（FTS5 关键词 + bge-small-zh embedding 语义 + RRF 合并排序）
- 过滤器：`--dir`, `--recent`, `--file`, `--cmd`
- picker 内：`/` 本地过滤（名称、tags、目录），`s` hybrid search（替换列表为搜索结果）
- 跨语言搜索：搜"容器部署"能找到只写了"docker"的 session

**待优化**：
- （无）

### R4. 会话恢复（Full + Topic）

**问题**：长 session 里讨论了多个 topic，用户只想继续其中一个。

**需求**：
- 恢复整个 session
- 只恢复某个 topic 相关的 turns（cherry-pick）

**当前状态**：✅ 已实现
- Full resume：导出完整 ConversationState JSON → PTY 注入 `/chat load`
- Topic resume：LLM 识别 topic → cherry-pick turns → 生成 temp file → PTY 注入
- Topic resume 依赖 LLM enrichment

**注意**：生成的文件必须保留完整 ConversationState 结构（`next_message`, `valid_history_range`, `tools` 等），否则 `/chat load` 反序列化失败。

### R5. Session 详情

**问题**：kiro-cli 只显示 session ID 和一行摘要，看不出聊了什么。

**需求**：
- 显示 session 名称、目录、时间、turn 数
- 显示 topic 列表和摘要
- 显示 tags
- 支持重命名

**当前状态**：✅ 已实现
- Layer 0（无 LLM）：自动生成名称（关键词 + first prompt）、auto-tags、turn 数
- Layer 1（LLM）：更好的名称、topic 分组 + 摘要、smart tags
- detail page 操作：`[r]` resume、`[1-N]` topic resume、`[n]` rename、`[t]` tags、`[v]` save、`[d]` delete、`[e]` enrich、`[f]` feedback

### R6. 过期清理

**问题**：session 越来越多，旧的占空间且干扰浏览。

**需求**：
- 提醒用户清理过期 session
- 识别空 session（0 turns）、stale session（老 + 少 turns）
- 所有删除需要用户确认

**当前状态**：✅ 已实现（`kiro-session cleanup`）

### R7. 隐私会话

**问题**：用户想问隐私问题，不希望对话被本地存储。

**需求**：
- 提供隐私沙箱目录
- 对话结束后自动删除本地数据
- 异常退出也能清理

**当前状态**：✅ 已实现（`kiro-session private`）
- 两层防护：exit hook + scan-time cleanup
- 声明：只删本地数据，服务端可能保留

### R8. Skill 集成

**问题**：用户在 kiro 对话中想查找/管理 session，不想切到终端。

**需求**：
- 在 kiro 对话中通过自然语言触发 session 管理
- 支持搜索、列表、详情查看
- 交互式操作（resume、delete）引导用户到终端

**当前状态**：✅ 已实现
- SKILL.md 定义触发词
- `--json` 输出供 skill 解析
- 交互操作引导到终端
- 隐私请求引导到 `kiro-session private`

### R9. Session 重命名

**问题**：自动生成的名字不够好，用户想自定义。

**当前状态**：✅ 已实现
- `kiro-session rename <id> "new name"` — CLI 命令
- detail page `[n]` — 交互式重命名
- `user_name` 字段优先于自动/LLM 生成的名字，重新索引不会覆盖

### R10. 导出为 Markdown

**问题**：JSON 导出不可读，用户想分享或存档对话。

**当前状态**：✅ 已实现
- `kiro-session export <id> [path]` — 单个导出
- `kiro-session export <id1> <id2> ... --dir <dir>` — 批量导出
- `kiro-session export --all --dir <dir>` — 全量导出
- 支持 v1 和 v2 格式，保留代码块，标注工具使用

### R11. 跨 Session 上下文引用

**问题**：用户在 session A 讨论过某个结论，想在 session B 中引用。

**当前状态**：✅ 已实现（基础版）
- `kiro-session context <id> [--topic N]` — 生成摘要 Markdown
- 输出 `/context add <path>` 命令供用户粘贴

**评估**：价值有限。用户可以直接让 LLM 看原始文件达到类似效果。降低优先级。

### R12. 批量操作

**需求**：批量删除、批量 tag、批量导出。

**当前状态**：✅ 已实现
- `kiro-session delete <id1> <id2> ...` — 批量删除，一次确认
- `kiro-session tag <id1> <id2> ... <tags> --batch` — 批量打 tag
- `kiro-session export` — 批量/全量导出（见 R10）

## 技术约束

1. **Read-only on kiro DB**：不写 kiro 的 SQLite/JSONL，删除通过 `kiro-cli chat --delete-session`
2. **Headless 写 v1**：`--no-interactive` 产生的 session 存 v1 SQLite，产生垃圾 session
3. **`/chat load` 格式**：必须是完整 ConversationState，不能精简
4. **`/compact` 改变 history**：压缩后 turn 数变化，影响 topic 索引
5. **`/context add` 是 session-only**：不持久化，每次新 session 需要重新添加
6. **PTY 注入依赖 TTY**：无交互终端时（skill 调用、pipe）自动 fallback 到打印指令

## 优先级与状态

| 优先级 | 需求 | 状态 |
|--------|------|------|
| P0 | R1 全局列表 | ✅ |
| P0 | R2 无感恢复 | ✅ |
| P0 | R3 全局搜索 | ✅ |
| P0 | R4 会话恢复 (Full + Topic) | ✅ |
| P0 | R5 Session 详情 | ✅ |
| P1 | R6 过期清理 | ✅ |
| P1 | R7 隐私会话 | ✅ |
| P1 | R8 Skill 集成 | ✅ |
| P2 | R9 重命名 | ✅ |
| P2 | R10 Markdown 导出 | ✅ |
| P2 | R11 跨 Session 上下文 | ✅ (低价值) |
| P2 | R12 批量操作 | ✅ |

## 待优化项

- ~~垃圾 session 可靠清理~~ — 已实现（`_is_llm_garbage` 检测 + isolated cwd）
- ~~中文分词精度~~ — embedding 语义搜索已补充，优先级降低
- ~~LLM 搜索优化~~ — 已用本地 embedding 替代 LLM 搜索
