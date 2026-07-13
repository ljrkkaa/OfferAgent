---
status: accepted
---

# Store Runtime State in local SQLite outside the Vault

本地 Runtime 将 Conversation、Agent Run、Run Checkpoint、工具记录、Provider 标识、待审核 Vault Action 和幂等标识保存在 `%LOCALAPPDATA%\OfferAgent\state.db`。Vault 只承载面试材料和真实学习状态；把高频内部运行数据放入 Vault 会污染用户知识空间并缺少可靠事务，而保留 PostgreSQL 对单用户本地产品又过于复杂。

## Consequences

- Runtime 是 SQLite 的唯一所有者，插件只能通过版本化协议访问运行状态。
- 数据库 schema 必须有显式版本和迁移策略。
- 删除 Conversation 时必须连带清理相关 Agent Run、Run Checkpoint 和待审核 Vault Action。
- API Key 和 Codex 登录凭据不得写入该数据库。
