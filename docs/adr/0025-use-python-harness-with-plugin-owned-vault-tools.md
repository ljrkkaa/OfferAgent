---
status: accepted
---

# Use the Python Harness with plugin-owned Vault tools over direct stdio

OfferAgent 以 `codex/windows-local-harness` 的 Python Harness 为唯一 Agent Loop、Tool Kernel、模型、Run Event、Conversation、恢复和本地 SQLite Runtime State 的权威实现。Obsidian 插件保留唯一的 Vault Tool Adapter，通过官方 Obsidian TypeScript API 执行 Vault 读取、MetadataCache 查询、Daily Notes、Project Evidence、Vault Change Batch、Git Checkpoint 与撤销；Python Worker 不为这些能力直接读取或写入 Vault 文件系统。

插件只拥有一个隐藏 Python Worker 子进程。双方只使用一条长度前缀 JSON-RPC stdio 流：Worker 先持久化带完整 Workspace、Run、Tool、定义指纹、参数 Hash 和幂等键绑定的 `tool.started` Event；插件执行 `executorLocation=plugin` 的调用，并用 `plugin-tools/complete` Application Command 回传同一绑定的结果。完全相同的回执可重放，绑定冲突必须拒绝。读取在断线时视为 interrupted；无法证明是否提交的写入视为 unknown outcome，不能自动重试。

Conversation Attachment 的二进制不进入 Run Event 历史。插件通过同一 stdio 连接使用有界 begin/chunk/commit/read Application Command 传输，Event 只保存 Attachment ID、Hash 和元数据。

本 ADR supersede ADR 0007 和 ADR 0021，并 narrow ADR 0003、0008 与 0012。历史 TypeScript Runtime 和协议不作为第二套运行时移植；产品功能按垂直切片接到 Python Harness 的生成协议上。

## Consequences

- Harness 架构和运行主题保持为 Python 代码，TypeScript 只承载 Obsidian UI 与 Vault Tool Adapter。
- 不存在 HTTP、WebSocket、Named Pipe、反向 RPC、常驻 Host 或第二个 Agent Loop。
- Vault 权限、版本校验、批次原子性与恢复语义不能绕过插件边界。
- 稳定旧数据只能通过显式、可 dry-run、先备份且幂等的导入器迁移；pending writes、Interrupted Run、旧 Checkpoint 与 Secret 不迁移。

## Alternatives considered

- 机械合并两个 Runtime 会产生双 Agent Loop、双协议和双 Vault 权威，因此拒绝。
- 让 Python Worker 直接操作 Vault 会绕过 Obsidian API、MetadataCache 与 per-Vault 权限，因此拒绝。
- 保留 HTTP/WebSocket 会增加第二套生命周期、认证和重连面，且对单个插件子进程没有必要，因此拒绝。
