---
status: accepted
---

# Runtime owns agent execution and the plugin owns Vault capabilities

本地 Runtime 负责模型调用、Agent loop、工具选择和运行状态，Obsidian 插件负责通过 Obsidian API 提供 Vault 读取能力、展示 Vault Action 并取得用户确认。这样既能把模型编排和 Provider 差异集中在 Runtime，又能确保交互式运行中的 Vault 权限由用户正在使用的 Obsidian 客户端执行，而不是让 Runtime 绕过插件直接操作文件。

## Consequences

- 插件与 Runtime 之间需要支持 `tool_call` 和 `tool_result` 的双向、版本化协议。
- Runtime 不得把模型返回的写入意图当成已经应用的修改。
- Obsidian 未运行时如何提供 Vault 读取能力需要单独决策，不能隐含地绕过插件所有权。
