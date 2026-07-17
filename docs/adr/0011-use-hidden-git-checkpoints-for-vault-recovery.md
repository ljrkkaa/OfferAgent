---
status: accepted
---

# Use hidden Git checkpoints for Vault recovery

目标 Vault 已是 Git 仓库。每个自动应用或用户确认应用的 Vault Change Batch 在写入前，恢复模块使用独立的临时 Git index 保存本批目标文件的当前版本，并创建 `refs/offeragent/checkpoints/<batch-id>` 隐藏引用。该过程不得切换分支、修改现有 Git index 或向当前分支创建提交。

在插件执行边界内，耐久 Journal 只保存批次绑定、目标路径、读取时与应用后 Hash、Git Checkpoint 引用和事务状态；Python Runtime 的 invocation journal 独立保存本次 Tool 调用结果。文件恢复内容由 Git 对象保存，不在任一 Journal 中复制完整文件或保存长期反向文本副本。此所有权划分由 ADR 0025 收窄：Python Runtime 不执行 Vault 文件恢复，插件也不拥有 Agent Run 状态。

## Recovery protocol

1. 验证全部目标路径、操作和读取时版本。
2. 创建并确认 Git Checkpoint 可读取。
3. 将事务状态持久化为 `applying`。
4. 插件通过 Obsidian Vault API 应用全部修改，并在每个目标后原子推进插件 Journal。
5. 全部成功后记录应用后 Hash，并将事务标记为 `applied`。
6. 若重启时仍存在 `applying` 事务，根据 Checkpoint 和当前文件 Hash 恢复到全部应用或全部未应用的稳定状态。

## Consequences

- Vault 原有未提交修改、当前分支和暂存区不会被 OfferAgent 自动提交或清理。
- 一键撤销只能在当前文件仍匹配应用后 Hash 时直接执行；否则展示冲突 Diff，不覆盖后续修改。
- Checkpoint 默认保留 30 天或最近 100 个批次，以先达到的限制为准。
- Git 仅是内部恢复模块，不作为模型可调用工具暴露。
- Git Checkpoint 负责恢复内容，SQLite 事务日志负责恢复决策；两者缺一不可。
