---
status: accepted
---

# Vault change batches are atomic

`vault_propose_changes` 将同一个 Agent Run 为完成一项逻辑任务所需的一个或多个 Vault Action 组成一个待确认批次。Obsidian 插件展示整个批次的文件差异，用户只能全部应用或全部拒绝，不提供部分勾选。

应用前，插件必须验证每个目标文件的路径、操作类型和读取时版本。只有全部验证通过时才通过 Obsidian Vault API 应用全部修改；任一验证失败时不应用任何修改，并将整个批次标记为失效，由 Agent 重新读取证据和规划。

## Consequences

- 跨 `daily/`、`experiences/` 和 `interview/` 的学习状态更新不会只完成一部分。
- 首版需要为多文件写入实现预验证和失败回滚，不能依赖逐文件写入的偶然成功。
- 用户若只需要其中部分修改，应拒绝整个批次并要求 Agent 生成新的、更小批次。
- 首版批次只允许创建、追加和精确替换，不允许删除或移动文件。
- 批次等待用户决定时，Agent Run 进入 `waiting_for_confirmation`；用户应用或拒绝后，决定作为工具结果返回，同一个 Agent Run 自动继续。
- 若等待期间 Obsidian 关闭，重启后恢复待确认批次，但不得自动替用户作出决定。
