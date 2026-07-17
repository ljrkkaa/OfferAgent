# OfferAgent Windows Local Runtime

OfferAgent 是面向项目所有者个人使用的 Windows x64 Obsidian 本地 Agent。工程约束以
[`docs/GENERAL_ENGINEERING_REFACTOR_CONSTRAINTS.md`](docs/GENERAL_ENGINEERING_REFACTOR_CONSTRAINTS.md)
为准，产品范围和完成判定以
[`docs/architecture/personal-local-scope.md`](docs/architecture/personal-local-scope.md) 为准。

## 唯一运行架构

- 每个 Obsidian 插件实例直接启动一个 `offeragent-worker.exe` 子进程。这个 Worker 拥有该
  Workspace 的 `HarnessService`、Agent Loop、SQLite、Session/Run、工具、权限、审批和恢复状态。
- 同一交互式 Windows 会话和用户下，相同 canonical Vault root 的 Worker 共享一个命名互斥锁；第二个实例在接触
  `state.sqlite` 或恢复事务前即 fail closed，不同 Vault 仍可并行。
- 插件与 Worker 的唯一 IPC 是继承 stdin/stdout 上的 framed JSON-RPC。stderr 只承载受限诊断码。
- 显式停止和设置重启会先请求 shutdown；断线自动重连会关闭旧 transport。两类路径都会等待当前子
  Worker 实际退出，超时才强制终止，旧进程完成回收前不会由同一插件实例启动替代 Worker。
- Obsidian 的 `onunload` 不等待 Promise；卸载、禁用、热重载或 Obsidian 退出时，插件会在回调返回前
  同步关闭 stdio 并发起 Worker 回收，后台继续等待实际进程退出。同一交互式 Windows 会话和用户内，
  后续同 Vault Worker 在旧进程释放互斥锁前不能进入 Runtime 校验、SQLite 或恢复。
- 没有常驻 Host、进程发现服务、Named Pipe、后台 Worker 或跨插件实例复用通道。
- 本地 Web UI 是同一 Worker 的可选 Loopback 客户端，不拥有第二套 Agent Runtime。
- Shell、Hook 等进程工具由 Worker 通过短生命周期 `offeragent-process-host.exe` 执行；进程受固定
  hash/catalog、Job Object、按策略启用的 AppContainer，以及用户注册可执行文件的可选离线
  Authenticode 校验约束。
- 模型只能通过 `ModelGateway` 推理，不能执行工具、访问 Vault、拥有 Session 或建立第二套循环。

当前仓库只支持个人 Windows x64 本机构建。它不包含正式签名发布、Setup 安装器、自动更新通道、
ARM64 构建矩阵，也不保留这些已删除路径的兼容入口。除用户主动选择的模型 Provider 外，Runtime
没有网络出口；本地 Loopback 不属于外部出口。

## 目录入口

```text
packages/offeragent-harness/       Agent Core、Worker、协议、进程隔离与个人构建脚本
src/interface/obsidian/            Obsidian 客户端源码
docs/architecture/                 当前架构、范围和迁移约束
```

主分支不保存旧 Khoj Server、Django/PostgreSQL 运行入口、远程 Store、旧 HTTP/sync 插件路径或
第二套 Agent Loop。历史实现只从 Git 对照分支 `archive/khoj-server-baseline-20260713` 追溯。

## Harness 门禁

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
uv run python scripts/build_web_assets.py check
uv build
```

这些门禁验证唯一 Agent Loop、direct stdio transport、依赖方向、协议生成物、Web 资源和生产依赖
闭包。测试结果以命令的实时退出状态为准，文档不硬编码测试数量或临时 schema hash。

## Obsidian 门禁

在 `src/interface/obsidian` 中执行：

```powershell
corepack yarn install --frozen-lockfile
corepack yarn protocol:check
corepack yarn typecheck
corepack yarn test
```

插件唯一可生成 bundle 的脚本是 `build:local`，并由个人构建脚本注入 Runtime manifest 锚点后调用；
不要使用或恢复无锚点的 production `build`/`dev` 入口，也不要直接维护真实 Vault 中的 `main.js`。

## 个人构建与更新

完整本地插件只能在 Windows x64 上由以下脚本构建：

```powershell
cd packages/offeragent-harness
uv run python scripts/build_local_windows_plugin.py `
  --output E:\Projects\offeragent\artifacts\offeragent-obsidian-plugin `
  --ripgrep-executable C:\path\to\rg.exe
```

更新现有 Vault 插件只使用以下脚本；执行前先停止 Runtime 并完全退出 Obsidian：

```powershell
uv run python scripts/update_local_windows_plugin.py `
  --vault-root 'E:\面试胜利！' `
  --ripgrep-executable C:\path\to\rg.exe
```

详细的 manifest、原子切换和 `data.json` 保护规则见
[`docs/personal-local-plugin-install.md`](docs/personal-local-plugin-install.md)。

## 真实 Vault 验收

真实 Vault 读取验收只能走 Obsidian 插件 → stdio Worker → Tool Kernel。验收前须遵守目标 Vault 的
`CLAUDE.md` 及其显式导入规则，保持 Workspace 未信任或 `read-only`，并核对验收前后 Markdown
文件身份、hash 和 mtime。活动文件、选区和 MetadataCache revision 必须由真实 Obsidian E2E 验证；
仓库不提供另一条 headless Agent 入口。真实 Vault 写入需要用户另行明确授权。
