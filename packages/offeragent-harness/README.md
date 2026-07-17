# OfferAgent Harness

`offeragent-harness` 是 OfferAgent 的 Windows x64 本地、Provider-neutral Agent Core 与 Runtime。它不是
Khoj Server 的便携打包，也不启动另一个模型厂商 Agent Runtime。

## 所有权

- 每个 Obsidian 插件实例直接启动一个 `offeragent-worker.exe`；Worker 内只有一个
  `HarnessService` 和一个 canonical Agent Loop。
- 同一交互式 Windows 会话和用户、同一 canonical Vault root 的 Worker 由命名互斥锁排他；锁早于 SQLite 打开和
  恢复，且一直持有到事件循环完全退出。
- 插件唯一 IPC 是继承 stdin/stdout 上的 framed JSON-RPC。没有常驻 Host、discovery、Named Pipe
  或后台 Worker。
- 工具调度、权限、审批、Session/Event、SQLite、Vault、Memory、Shell、Hooks 和 Subagent 均归
  Worker 所有；插件和本地 Web 不拥有第二套状态机。
- 模型只能通过 `ModelGateway` 参与规划和生成，不能访问文件、执行进程或管理业务状态。
- Shell/Hook 等进程工具经短生命周期 `offeragent-process-host.exe` 执行，并受固定 hash/catalog、
  Job Object、AppContainer 和可选离线 Authenticode 约束。
- 生产依赖不得包含 Khoj、Django、PostgreSQL、LangChain 或远程 Conversation/Workspace Store。

当前包只支持个人 Windows x64 本地构建，不包含正式签名发布、Setup、自动更新通道或 ARM64 路径。

## 开发门禁

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
uv run python scripts/build_web_assets.py check
uv build
```

结果以当前命令的退出状态为准；README 不保存测试数量或临时 schema hash。

## 执行和文件工具

所有 Agent 请求都从 direct stdio 或同一 Worker 的 Loopback adapter 进入应用命令边界，再统一经过
能力策略、审批、审计、UoW 和恢复。`runtime.duplex_json_rpc` 只负责 framing、取消和背压，不拥有
Agent、Tool、Vault 或 Storage 实现。

读取工作区前须遵守其 `CLAUDE.md`；需要使用 `AGENTS.md` 时由指令文件显式导入。Worker 使用 Glob
定位文件、Grep（固定的 `rg.exe`）搜索内容、Read 读取确认范围。Runtime 不维护本地检索索引、RAG、
Embedding 或网络页面检索。

生产 closure audit 验证旧服务器路径和入口不存在、生产代码不导入测试 fake、依赖图符合
`.importlinter`，且冻结 bundle 不含已删除的常驻协调 Host、发行或更新模块。

## 个人构建

完整插件只能由：

```powershell
uv run python scripts/build_local_windows_plugin.py `
  --output E:\Projects\offeragent\artifacts\offeragent-obsidian-plugin `
  --ripgrep-executable C:\path\to\rg.exe
```

更新目标 Vault 只能由：

```powershell
uv run python scripts/update_local_windows_plugin.py `
  --vault-root 'E:\面试胜利！' `
  --ripgrep-executable C:\path\to\rg.exe
```

检测到标准旧插件目录和只读 schema-v19 State 时，更新器会在插件切换内自动执行一次 source-hash 幂等迁移。需要先审查迁移范围或使用非标准旧路径时，可显式运行同一个 Python 计划：

```powershell
uv run python -m offeragent_harness.migration `
  --source-state "$env:LOCALAPPDATA\OfferAgent\state.db" `
  --source-attachments 'C:\path\to\legacy-attachments' `
  --source-plugin-data 'E:\Vault\.obsidian\plugins\offeragent\data.json' `
  --target-state-directory 'C:\path\to\OfferAgent\workspaces\wsi_...' `
  --target-plugin-data 'E:\Vault\.obsidian\plugins\offeragent-obsidian-plugin\data.json' `
  --vault-root 'E:\Vault' `
  --target-templates 'E:\artifact\migration\target-vault' `
  --workspace-id 'ws_...' `
  --dry-run
```

去掉 `--dry-run` 才会执行。迁移拒绝未知 schema、活动 SQLite sidecar、损坏或超容量图片、身份碰撞和未识别的控制文件；旧来源始终只读，旧 Run、工具/Vault 副作用和秘密不会进入新 State。

工程约束见仓库的 `docs/GENERAL_ENGINEERING_REFACTOR_CONSTRAINTS.md`，当前范围见
`docs/architecture/personal-local-scope.md`。
