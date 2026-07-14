# 旧行为到 Windows Harness 测试迁移矩阵

| 旧测试/行为 | 必须保留的不变量 | 新测试层 | 状态 |
|---|---|---|---|
| `test_tool_protocol.py` | canonical ToolPlan、严格 Schema、未知字段拒绝 | `unit/tools`、`schema`、Tool Kernel contract | 已迁移 |
| `test_agent_tool_loop.py` | 只读并发、写串行、顺序稳定、兄弟失败隔离 | `unit/agent`、`unit/tools`、Tool Kernel contract | 已迁移 |
| `test_conversation_turn.py` | terminal exactly-once、重复 turn 幂等、interrupted 恢复 | Session lifecycle、SQLite UoW、startup recovery | 已迁移 |
| `test_vault_actions.py` | Diff、expectedHash、冲突、幂等、ACK 丢失、回滚/人工复核 | Vault transaction、Client Tool、recovery integration | 已迁移 |
| `test_local_kb.py` | root containment、范围/限额、来源 hash、链接解析 | Workspace path/filesystem、Vault read、Markdown file-tool tests | 已迁移 |
| `test_knowledge_workspace.py` | 来源绑定、read-before-edit、Skill 路径安全 | File Tool Kernel、Skill trust/state/tool tests | 已迁移 |
| `test_codex_conversation_adapter.py` | Provider payload/normalization、真流式、取消、usage | OpenAI/Ollama ModelGateway、Planner/Composer contracts | 已迁移 |
| 旧 HTTP/Router tests | 相同用户场景，不保留 Router/HTTP 所有权 | 真实 Named Pipe + Loopback identity/conformance | 已重写 |
| 插件旧 tests | 流拆包、stale event、写前检查等目标语义 | Generated protocol、EventReducer、Client Tool/Journal | 已重写 |

明确反转的旧断言：`action_prepared` 不是写完成；Abort fetch 不是 Run 取消；环境变量不是 Workspace identity；Django/PostgreSQL 锁不是目标实现；远程内容同步不是兼容路径。

每删除一项旧运行入口前，必须先在新测试层建立同等或更强的目标不变量证据。

2026-07-13 已完成该门槛并从主分支删除根 Khoj distribution/lock/tests、`src/khoj`、旧服务端
Web、Docker/Gunicorn/Router 入口和插件 HTTP/sync 残留。旧实现只允许出现在 Git 对照分支
`archive/khoj-server-baseline-20260713` 与外层 snapshot；主分支保留的
`packages/offeragent-harness/scripts/legacy_exporter` 只消费调用方提供的
只读 iterable 或离线 JSONL，不能 import、配置或启动 Django/Khoj Server。
