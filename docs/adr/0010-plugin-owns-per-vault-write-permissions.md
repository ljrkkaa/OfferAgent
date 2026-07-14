---
status: accepted
---

# Plugin owns per-Vault write permissions

Obsidian 插件按 Vault 保存并执行写入权限，Agent 与 Runtime 只能请求 Vault Action，不能修改、扩大或绕过权限。首版提供 `trusted_vault`、`ask_every_time` 和 `read_only` 三种模式，当前单用户产品默认使用 `trusted_vault`。

在 `trusted_vault` 模式下，绑定 Vault 内普通内容的创建、追加和精确替换批次通过路径、版本和大小验证后自动应用。权限不扩展到 Vault 外文件、Shell、任意代码执行、删除或移动。

`agent.md`、`.codex/**` 和 `.obsidian/**` 属于控制文件；任何模式下修改这些路径都必须由用户显式确认，不能自动应用。

## Consequences

- 自动应用仍保留批次 Diff、执行结果和可恢复记录。
- 任一文件版本不匹配时，整个批次失效并重新规划，不允许部分落盘。
- 插件设置页允许用户随时切换权限模式；设置属于具体 Vault，而不是 Conversation 或 Agent Run。
- 权限判断必须在插件中实现，不能只依赖 Prompt、`agent.md` 或模型自律。
