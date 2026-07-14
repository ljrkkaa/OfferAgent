# OfferAgent Windows Local Runtime

本仓库正在把旧的服务器型 OfferAgent 重构为 Windows 完全本地化产品。完整架构与不可变红线以
外层工作区的 [`task.md`](../task.md) 为权威；当前交付目标已经收敛为项目所有者的 Windows x64
个人本机插件，完成判定见
[`docs/architecture/personal-local-scope.md`](docs/architecture/personal-local-scope.md)。完整公开发行
DoD 继续保留作长期蓝图，但不再阻塞个人版本。

## 产品边界

- 一座 Vault 只有一个 Windows Worker、一个 `HarnessService`、一份 SQLite 和一个活动 Run 所有权边界。
- Obsidian 插件通过当前 Windows SID 专属 Named Pipe 连接 Worker；本地 Web UI 由同一 Worker 在随机 Loopback 端口提供。
- 唯一 Agent Loop、Planner、Composer、Tool Kernel、Policy、Approval、Memory、Skills、Hooks、Shell 与 Subagent 全部在本机 Runtime 中运行。
- Codex、OpenAI 和本地模型只通过 `ModelGateway` 提供推理，不能执行工具、访问 Vault、管理 Session 或拥有第二套 Agent Runtime。
- 正式运行路径和发行依赖不得包含 Django、PostgreSQL、Khoj Server、远程 Conversation/Workspace Store、内容检索服务或内容同步。
- 除用户选择的模型 Provider 与可关闭的签名更新检查外，Runtime 不允许产生网络出口。

## 目录入口

```text
packages/offeragent-harness/       Windows 本地 Agent Core、Worker、Host、协议与发行工具
src/interface/obsidian/            Obsidian 本地客户端源码
docs/architecture/                 新 Runtime 的架构与发行说明
```

主分支不保存旧 Khoj Server、服务端 Web UI 或第二套 Agent Loop。旧实现只通过 Git
对照分支 `archive/khoj-server-baseline-20260713` 和外层只读 snapshot 追溯；一次性数据导出使用独立的
`packages/offeragent-harness/scripts/legacy_exporter`，它不 import 或启动旧服务器。

真实开发与测试边界不在本仓库目录内混用：

- 代码根目录：`E:\Projects\offeragent\repo`
- 真实 Vault：`E:\面试胜利！`
- 插件测试部署目录：`E:\面试胜利！\.obsidian\plugins\offeragent-obsidian-plugin`
- Runtime 状态：`%LOCALAPPDATA%\OfferAgent\workspaces\<workspace-instance-id>`

真实 Vault 默认只读。写入测试前必须阅读 `E:\面试胜利！\AGENTS.md`，不得修改或删除已有 `notes/` 内容，也不得读取插件 `data.json` 中的凭据。

## Harness 开发

```powershell
cd packages/offeragent-harness
uv sync --extra dev --locked --python 3.12
uv run pytest -q
uv run ruff check src tests scripts
uv run ruff format --check src tests scripts
uv run mypy src tests
uv run lint-imports --config .importlinter --no-cache
uv run python -m offeragent_harness.protocol.schemas check
uv run python scripts/audit_repository_closure.py
uv run python scripts/check_architecture.py
uv run python scripts/check_forbidden_dependencies.py
uv run semgrep --config semgrep.yml --no-git-ignore --exclude .venv --exclude .mypy_cache --exclude ../../src/interface/obsidian/node_modules src scripts ../../src/interface/obsidian/src
uv run python scripts/build_web_assets.py check
uv build
```

架构检查要求生产包中只有一个标记的 Agent Loop，并禁止旧框架依赖或测试 Fake 进入生产代码。协议 Schema、示例、插件协议身份和本地 Web 资源必须在同一次变更中重新生成并校验。

遵守真实 Vault 的 `AGENTS.md` 后，可执行隐私安全的 Worker 侧零写入验收：

```powershell
uv run python scripts/validate_real_vault_readonly.py `
  --vault-root 'E:\面试胜利！' `
  --expected-vault-root 'E:\面试胜利！' `
  --query "验证本地文件读取"
```

该命令不输出笔记路径或内容，并在前后核对可见 Markdown 的身份、hash 和 mtime。活动文件、选区与
MetadataCache revision 属于真实 Obsidian E2E，不能由无插件的命令行验收代替。

## Obsidian 插件开发

```powershell
cd src/interface/obsidian
corepack yarn install --frozen-lockfile
corepack yarn test
corepack yarn build
```

长期修改必须落在这里的 TypeScript 源码中；真实 Vault 下的 `main.js` 是安装产物，不能作为目标源码直接维护。插件不得直接调用模型、执行 Shell，或实现 `while model -> tool -> model`。

## Windows 发行

个人本机版本与正式发行严格分开：个人版本允许使用明确标识、按 hash 固定的本地开发安装路径；
它只能用于当前机器，不能被命名、打包或展示为签名发行物。个人安装流程见交付范围文档和后续
本机安装脚本。

正式发行由 `packages/offeragent-harness/scripts/build_windows_release.py --architecture x64|arm64` 在对应原生 Windows 机器上统一生成签名的 PyInstaller onedir Runtime、架构专属完整离线 ZIP、插件 payload、SBOM 和 Setup 安装器。构建脚本拒绝跨架构伪装且没有无签名生产模式：缺少 Ed25519 私钥、Authenticode 证书、SignTool 或 Inno Setup 时必须失败，不能产出降级包。

发行前还必须完成干净 Windows VM 上的断网安装、升级、回滚、恢复、卸载、进程树清理、抓包与长期压力 E2E。详细流程见 [`docs/architecture/windows-runtime-release.md`](docs/architecture/windows-runtime-release.md)。

## 迁移规则

旧服务器源码不在主分支正式树中。行为对照从只读 Git 归档/snapshot 进行；会话、Memory
和 VaultAction audit 只能先导成规范 JSONL，再由本地 importer 读取。Runtime、插件与 Web
均不得连接旧服务器或提供远程降级。服务器默认只读；任何服务器写操作仍需用户明确授权。
