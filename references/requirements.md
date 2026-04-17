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

**问题**：当前恢复流程需要 3 步手动操作（cd → kiro-cli chat → /chat load）。

**需求**：选中 session 后一步进入 kiro-cli 继续对话。

**方案探索**：

| 方案 | 可行性 | 问题 |
|------|--------|------|
| `kiro-cli chat --resume` | 只恢复最近一个，限当前目录 | 不通用 |
| 复制 session 到目标目录 | v2 可以，v1 需要写 DB | 违反 read-only |
| `/chat load` 自动化 | kiro-cli 不支持 CLI 参数传 slash command | 需要模拟 stdin |
| `/context add` + 新 session | 只加 context 不加 history | 不是真正恢复 |
| stdin pipe 模拟 | `echo "/chat load <path>" \| kiro-cli chat` | 需实测 |

**当前状态**：⚠️ 部分实现（生成 temp file + 打印指令，用户手动执行）

**待探索**：
- stdin pipe 是否可行
- kiro-cli 是否有未公开的 load 参数
- `/context add` 作为轻量级"恢复"的可行性（不恢复 history，但带入之前的结论）

### R3. 全局搜索

**问题**：kiro-cli 没有搜索功能。用户记得聊过某个话题但找不到在哪个 session。

**需求**：
- 全文搜索所有 session 的完整对话内容
- 支持过滤器：目录、时间、文件、命令
- 搜索结果带上下文 snippet
- （可选）LLM 增强的语义搜索

**当前状态**：✅ FTS5 全文搜索已实现，过滤器已实现

**待优化**：
- picker 内实时搜索（`/` 本地过滤 + `s` FTS 搜索）
- LLM 搜索的垃圾 session 问题（headless 写 v1 SQLite）
- 中文分词精度（FTS5 unicode61 按字符分词）

### R4. 会话恢复（Full + Topic）

**问题**：长 session 里讨论了多个 topic，用户只想继续其中一个。

**需求**：
- 恢复整个 session
- 只恢复某个 topic 相关的 turns（cherry-pick）

**当前状态**：✅ 已实现
- Full resume：导出完整 ConversationState JSON
- Topic resume：LLM 识别 topic → cherry-pick turns → 生成 temp file
- Topic resume 依赖 LLM enrichment

**注意**：生成的文件必须保留完整 ConversationState 结构（`next_message`, `valid_history_range`, `tools` 等），否则 `/chat load` 反序列化失败。

### R5. Session 详情

**问题**：kiro-cli 只显示 session ID 和一行摘要，看不出聊了什么。

**需求**：
- 显示 session 名称、目录、时间、turn 数
- 显示 topic 列表和摘要
- 显示 tags

**当前状态**：✅ 已实现
- Layer 0（无 LLM）：自动生成名称（关键词 + first prompt）、auto-tags、turn 数
- Layer 1（LLM）：更好的名称、topic 分组 + 摘要、smart tags

### R6. 过期清理

**问题**：session 越来越多，旧的占空间且干扰浏览。

**需求**：
- 提醒用户清理过期 session
- 识别空 session（0 turns）、stale session（老 + 少 turns）
- 所有删除需要用户确认

**当前状态**：✅ 已实现（`kiro-session cleanup`）

**待优化**：
- 可选 `--auto` 自动删除符合条件的
- 垃圾 session（headless LLM 调用产生的）自动清理

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

## 补充需求

### R9. Session 重命名

**问题**：自动生成的名字不够好，用户想自定义。

**需求**：`kiro-session rename <id> "new name"`

**当前状态**：❌ 未实现

### R10. 导出为 Markdown

**问题**：JSON 导出不可读，用户想分享或存档对话。

**需求**：导出为格式化的 Markdown 文档（user/assistant 分段，代码块保留）。

**当前状态**：❌ 未实现

### R11. 跨 Session 上下文引用

**问题**：用户在 session A 讨论过某个结论，想在 session B 中引用。

**需求**：从 session A 提取结论/摘要，通过 `/context add` 注入到当前对话。

**当前状态**：❌ 未实现
**可行性**：高。结合 LLM 摘要 + `/context add` 可以实现。

### R12. 批量操作

**需求**：批量删除、批量 tag、批量导出。

**当前状态**：❌ 未实现

## 技术约束

1. **Read-only on kiro DB**：不写 kiro 的 SQLite/JSONL，删除通过 `kiro-cli chat --delete-session`
2. **Headless 写 v1**：`--no-interactive` 产生的 session 存 v1 SQLite，产生垃圾 session
3. **`/chat load` 格式**：必须是完整 ConversationState，不能精简
4. **`/compact` 改变 history**：压缩后 turn 数变化，影响 topic 索引
5. **`/context add` 是 session-only**：不持久化，每次新 session 需要重新添加

## 优先级

| 优先级 | 需求 | 状态 |
|--------|------|------|
| P0 | R1 全局列表 | ✅ |
| P0 | R3 全局搜索 | ✅ |
| P0 | R4 会话恢复 | ✅ |
| P0 | R5 Session 详情 | ✅ |
| P1 | R2 无感恢复 | ⚠️ 待探索 |
| P1 | R6 过期清理 | ✅ |
| P1 | R7 隐私会话 | ✅ |
| P1 | R8 Skill 集成 | ✅ |
| P2 | R9 重命名 | ❌ |
| P2 | R10 Markdown 导出 | ❌ |
| P2 | R11 跨 Session 上下文 | ❌ |
| P3 | R12 批量操作 | ❌ |
