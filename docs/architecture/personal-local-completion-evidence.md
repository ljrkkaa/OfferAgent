# OfferAgent 个人本机版本完成证据

本文定义当前 Windows x64 候选如何被证明，而不是缓存某次运行的测试数量、耗时、bundle hash 或
schema hash。任何源码、协议、构建脚本或生成物变化后，都必须重新执行对应门禁。

## 当前架构事实

- 每个 Obsidian 插件实例直接创建一个 `offeragent-worker.exe` 子进程。
- 当前交互式 Windows 会话中的用户按 canonical Vault root 持有命名互斥锁；锁覆盖 Runtime 校验、SQLite、恢复、运行期和
  event-loop shutdown，第二个同 Vault Worker 在状态访问前被拒绝。
- 唯一插件 IPC 是继承 stdin/stdout 上的 framed JSON-RPC；stderr 只返回受限诊断码。
- Worker 内只有一个 `HarnessService` 和一个带 `@agent_loop_entrypoint` 的 Agent Loop；SQLite、
  Session/Run、Tool Kernel、Policy、Approval、Memory、Skills、Hooks、Shell 和 Subagent 均归它所有。
- 显式停止和设置重启会先请求 shutdown，断线重连会关闭旧 transport；两类路径都会 join 当前 Worker，
  超时强杀后仍等待 process exit，同一插件实例的替代 Worker 只能在回收完成后启动。Obsidian 不等待 `onunload` Promise；卸载、禁用、
  热重载或退出会在同步回调内关闭 stdio 并发起后台 join，同会话同 Vault Worker 在旧进程释放互斥锁
  前不能访问 Runtime 状态。
- Shell/Hook 等进程工具由短生命周期 `offeragent-process-host.exe` 执行，并受固定 hash/catalog、
  Job Object、AppContainer 和可选离线 Authenticode 约束。
- 仓库仅支持个人 Windows x64；没有常驻 Host、discovery、Named Pipe、正式签名发布、Setup、自动
  更新通道或 ARM64 路径。

## 实现证据映射

| 不变量 | 权威实现/门禁 |
|---|---|
| 单一 Agent Loop | `agent/loop.py` 标记检查、`scripts/check_architecture.py` |
| 模型只推理 | Provider import contract、ModelGateway tests、network audit |
| direct stdio | `runtime/duplex_json_rpc.py`、`runtime/worker_entrypoint.py`、插件 stdio client tests |
| 单一 Worker composition | `runtime/production_worker_composition.py`、Vault-scoped `ProcessLock`、repository closure audit |
| 协议无漂移 | Python schema check、插件 `protocol:check`、generated protocol tests |
| 本地状态和恢复 | SQLite UoW、startup/recovery、invocation journal tests |
| Vault 写安全 | transaction schema、CAS、durable manifest、fault-injection tests |
| 进程安全 | process catalog、Job Object、AppContainer、process tree tests |
| 插件不拥有工具 | TypeScript import/closure tests、HarnessClient/EventReducer tests |
| 生产闭包纯净 | Import Linter、forbidden dependency、SBOM/repository closure checks |

知识上下文只从已授权 Workspace/Vault 经 Glob、Grep（独立 `rg.exe`）和 Read 获取。当前 Runtime 不
维护 RAG、Embedding、向量索引、文档切块或相关性路由链。长期记忆是显式、可见、可编辑的
`.offeragent/memory/MEMORY.md`，不存在隐式语义记忆写入。

## 自动化门禁

在 `packages/offeragent-harness` 中执行：

```powershell
uv sync --extra dev --locked --python 3.12
uv run pytest -q
uv run ruff check src tests scripts
uv run ruff format --check src tests scripts
uv run mypy src tests
uv run lint-imports --config .importlinter --no-cache
uv run python -m offeragent_harness.protocol.schemas check
uv run python scripts/audit_repository_closure.py
uv run python scripts/check_documentation.py
uv run python scripts/check_architecture.py
uv run python scripts/check_forbidden_dependencies.py
uv build
```

在 `src/interface/obsidian` 中执行：

```powershell
corepack yarn install --frozen-lockfile
corepack yarn protocol:check
corepack yarn typecheck
corepack yarn test
```

完成证据是这些命令在当前 checkout 上全部成功，以及生成后再次以 check 模式得到零漂移。测试框架
报告的实时数量和 skip 原因应保留在当次日志中，不写入本文件。

## 构建证据

完整插件必须从不存在的输出目录执行 `scripts/build_local_windows_plugin.py`。候选需要证明：

- 构建机和 PE 均为 Windows x64；
- manifest 的源码摘要、Git identity、协议范围、schema hash 和逐文件记录互相一致；
- `main.js` 内嵌的 manifest 锚点与 Runtime manifest 一致；
- Worker、Process Host、`rg.exe` 和所有冻结依赖均在 manifest 中；
- 冻结闭包不含测试 fake、旧服务器、已删除的常驻协调 Host、发行或更新模块；
- 插件只能由内部 `build:local` 生成，不存在无锚点的 production bundle。

更新必须由 `scripts/update_local_windows_plugin.py` 执行。它应在 Obsidian 和 Runtime 进程退出后重新
构建、复验并原子切换插件目录，同时把 `data.json` 当作不可读取的不透明文件保护和回滚。

完整构建还必须生成独立的资格驱动目录。最终证据由
`scripts/qualify_final_windows_product.py` 这一条 Python 命令从干净 checkout 依次执行全部门禁、构建、离线启动、
迁移、安装和真实 Codex 阶段；`qualify_built_windows_product.py` 只保留为开发中的快速离线 smoke。
Python 主控先验证插件精确布局、完整 Runtime manifest、bundle anchor，以及资格驱动对同一 build receipt、
Runtime manifest、Git commit 和源码树摘要的绑定。TypeScript 驱动只在构建时编译；执行时仅加载密封 CJS、
manifest 固定的真实冻结 Worker 和 direct stdio，不解析仓库源码。基础 smoke 在 ownership marker 约束的一次性
Vault 中完成 initialize/shutdown，主动观测网络 socket 与子进程，并证明 Worker/Process Host 零泄漏。版本化
canonical JSON 记录实际命令/退出、命名 skip、来源头、local commits、迁移、安装、认证、临时 Vault 和进程证据；
真实模型、Vault Change 和恢复阶段复用同一 Python 资格入口，不给 Worker 注入隐藏模型后门。

## 已安装插件 E2E

源码门禁和构建成功不能替代真实安装验收。当前候选还必须在目标 Vault 验证：

1. 插件状态经过定位、完整性校验、Worker 启动、协议握手并进入 ready。
2. 进程父子关系是 Obsidian → `offeragent-worker.exe`；不存在常驻协调进程或 listener。
3. 插件热重载、禁用、退出、断线重连和停止命令都会开始回收当前 Worker；同一交互式 Windows 会话
   和用户内，同一 Vault 同时最多一个 Worker 能持锁并访问 Runtime 状态。
4. 会话发送、取消、重试、分叉、压缩、审批和事件回放使用同一 Worker 状态。
5. 设置只保留账户绑定的 Codex 目录选择，不包含 Provider、端点、API Key 或模型 SecretHandle。

## 真实模型和 Vault 验收

必须区分“代码存在”“配置启用”和“当前候选真实运行成功”。至少复验：

- 当前 Codex 订阅账户目录中的真实模型完成多轮会话；
- 未信任 Workspace 的实际权限保持只读；
- 真实 Vault 只读 Glob/Grep/Read 返回可核对的来源 hash 和行范围，且前后文件身份不变；
- 临时 Vault 完成单文件 Diff、审批、提交、冲突、取消和硬崩溃恢复；
- Skills、Hooks、Shell 或 Subagent 只有在配置启用且本次确实执行时，才可声明真实可用；
- 真实 Vault 写入必须另获明确授权，历史候选的写入结果不能自动转移到当前候选。

任何早于当前 stdio/x64-only 架构的真实 Run、安装目录或 artifact hash 只属于历史候选，不能作为
本次完成证据。完成结论应附当次命令日志、manifest 和 E2E 记录，而不是复制旧计数。
