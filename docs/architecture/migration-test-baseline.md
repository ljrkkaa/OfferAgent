# 旧行为到 Windows Harness 测试迁移矩阵

| 旧测试/行为 | 必须保留的不变量 | 新测试层 | 状态 |
|---|---|---|---|
| `test_tool_protocol.py` | canonical ToolPlan、严格 Schema、未知字段拒绝 | Core unit/schema | 迁移中 |
| `test_agent_tool_loop.py` | 只读并发、写串行、顺序稳定、兄弟失败隔离 | Fake Model + real Tool Kernel | 迁移中 |
| `test_conversation_turn.py` | terminal exactly-once、重复 turn 幂等、interrupted 恢复 | UoW/SQLite integration | 待迁移 |
| `test_vault_actions.py` | Diff、expectedHash、冲突、幂等、ACK 丢失、回滚/人工复核 | Vault transaction integration | 待迁移 |
| `test_local_kb.py` | root containment、范围/限额、来源 hash、链接解析 | VaultFS property/unit | 待迁移 |
| `test_knowledge_workspace.py` | 来源绑定、read-before-edit、Skill 路径安全 | RAG/Skills integration | 待迁移 |
| `test_codex_conversation_adapter.py` | Provider payload/normalization、真流式、取消、usage | ModelGateway contract | 迁移中 |
| 旧 HTTP/Router tests | 相同用户场景，不保留 Router/HTTP 所有权 | HarnessService + Pipe/Loopback conformance | 待重写 |
| 插件 19 项 tests | 流拆包、stale event、写前检查等目标语义 | Generated protocol/EventReducer/Client Tool | 待重写 |

明确反转的旧断言：`action_prepared` 不是写完成；Abort fetch 不是 Run 取消；环境变量不是 Workspace identity；Django/PostgreSQL 锁不是目标实现；远程内容同步不是兼容路径。

每删除一项旧运行入口前，必须先在新测试层建立同等或更强的目标不变量证据。

