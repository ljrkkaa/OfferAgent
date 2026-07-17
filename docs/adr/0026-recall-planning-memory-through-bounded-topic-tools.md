---
status: accepted
---

# Recall Planning Memory through bounded topic tools

OfferAgent 通过插件拥有的 `planning_memory.list` 只读取 `memory/{user,feedback,project,study}/*.md` 的
`name`、`description`、`type` 与版本元数据，再由唯一的 Python Agent Loop 根据当前请求和 Conversation
Context 语义选择最多五个主题，并通过 `planning_memory.read` 精读。`memory/MEMORY.md` 只作为用户可见索引，
不常驻模型上下文；Python Worker 不直接读取 Vault Memory，也不建立隐藏副本、向量索引或第二套 Memory Loop。

Memory Capture 和 Consolidation 仍使用插件拥有的 `vault.changes.apply`。明确写入与回答前兜底写入在同一 Run
不得重复修改同一路径；主题删除必须与索引同步并逐次确认。Daily Study Plan 产生的跨天 Study Memory 与 Daily
修改必须属于同一个 Vault Change Batch。

## Consequences

- 元数据发现不会把主题正文或完整索引注入上下文，精读数量和总字节数在插件边界受限。
- Feedback Memory 与其他相关 Memory 的优先级由 Agent Contract 和 Python 系统规则表达，不进入 Provider adapter。
- Planning Memory 仍是用户可见 Vault 内容，不是 Runtime State、Study Evidence 或模型供应商的隐藏记忆。
- no-op、陈旧版本、越界路径、重复路径、超量选择和不一致读取在创建 Vault journal 或模型使用前失败。

## Alternatives considered

- 自动加载完整 `MEMORY.md` 会把索引当正文并注入无关主题，因此拒绝。
- 在 Worker 中扫描 Vault 或建立向量数据库会绕过 Obsidian 权威并产生隐藏副本，因此拒绝。
- 把 Recall/Capture 实现为 TypeScript Agent 子循环会产生第二个规划循环，因此拒绝。
