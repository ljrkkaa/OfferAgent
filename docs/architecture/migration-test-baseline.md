# 旧行为到 Windows x64 Harness 的测试迁移矩阵

| 旧测试/行为 | 必须保留的不变量 | 当前证据层 |
|---|---|---|
| Tool protocol | canonical AgentStep、严格 Schema、未知字段拒绝 | `unit/tools`、`schema`、Tool Kernel contract |
| Agent tool loop | 只读并发、写串行、顺序稳定、兄弟失败隔离 | `unit/agent`、`unit/tools`、Agent Loop contract |
| Conversation/Turn | terminal exactly-once、重复 Turn 幂等、interrupted 恢复 | Session lifecycle、SQLite UoW、startup recovery |
| Vault actions | Diff、expectedHash、冲突、幂等、ACK 丢失、崩溃恢复 | Worker-local transaction、durable manifest integration |
| 本地知识检索 | root containment、范围/限额、来源 hash、链接解析 | Glob/Grep/Read、Workspace path/filesystem tests |
| Knowledge Workspace | 来源绑定、read-before-edit、Skill 路径安全 | File Tool Kernel、Skill trust/state/tool tests |
| Provider adapter | payload/normalization、流式、取消、usage | ModelGateway、AgentStep catalog、network audit |
| 旧 HTTP/Router | 用户场景保留，不保留 Server/Router 所有权 | direct stdio RPC、Loopback、identity/conformance |
| 旧插件事件 | 流拆包、stale event、写前检查、重连恢复 | Generated protocol、EventReducer、stdio client tests |
| 旧进程执行 | 进程树取消、路径固定、无网络、输出限额 | Process Host、Job Object、AppContainer、hash/Authenticode tests |

明确反转的旧断言：`action_prepared` 不是写完成；Abort fetch 不是 Run 取消；环境变量不是 Workspace
identity；Django/PostgreSQL 锁不是目标实现；远程内容同步不是兼容路径；断开插件也不是把 Worker
转交给后台服务。

主分支已经删除 Khoj distribution、`src/khoj`、旧服务端 Web、Docker/Gunicorn/Router、远程 Store、
插件 HTTP/sync，以及常驻协调 Host/Named Pipe/签名发行/Setup/ARM64 运行闭包。历史实现只允许从 Git 对照
分支 `archive/khoj-server-baseline-20260713` 查看，不能成为 fallback。

验证不记录固定测试数量。每次候选都必须重新执行 Harness pytest、schema check、Import Linter、
repository closure、architecture/forbidden-dependency checks，以及插件 protocol check、typecheck 和 tests；
只有当次命令退出状态是证据。
