---
status: accepted
---

# Vault-dependent runs require a connected Obsidian plugin

第一版不要求在 Obsidian 关闭后继续执行依赖 Vault 的 Agent Run；当插件断开时，Runtime 必须暂停或中断相关运行，而不能通过另一套文件适配器绕过插件直接读取 Vault。这个边界保持了单一 Vault 权限入口，并避免为了后台调度引入第二套读取和一致性模型。

## Consequences

- Obsidian 重新连接后可以恢复已持久化的运行状态。
- Runtime 可以独立保存运行检查点，但不能在缺少插件工具执行器时继续 Vault 工具调用。
- 未来若增加关闭 Obsidian 后的自动化，需要作为新的权限与一致性设计单独决策。
