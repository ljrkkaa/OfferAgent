# OfferAgent Harness

`offeragent-harness` 是 OfferAgent 的 Windows 本地、Provider-neutral Agent Core 与 Runtime。它不是 Khoj Server 的便携打包，也不启动 Codex Agent Runtime。

核心所有权：

- 模型只能通过 `ModelGateway` 参与规划和生成。
- 唯一 Agent Loop、工具调度、权限、审批、Session/Event、Vault、Memory、Shell 和 Subagent 均归本包及其本地 Adapter。
- Obsidian 与本地 Web UI 是同一 Vault Worker 的客户端，不拥有第二套 Agent 状态机。
- 生产依赖不得包含 Khoj、Django、PostgreSQL、LangChain 或远程 Conversation/Workspace Store。
- 个人本机默认 DeepSeek 通过固定 Chat Completions Adapter 推理；Provider 原生工具和原始思维链不进入
  Harness，JSON 输出在本机按 canonical Schema 再校验。

开发命令：

```powershell
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
uv run python scripts/build_web_assets.py check
uv build
```

## 执行入口

本包不提供独立的模型/工具循环。所有 Agent 执行必须通过 Worker transport 进入同一正式运行时，
从而统一经过能力策略、审批、审计、UoW 持久化和恢复边界。

读取工作区前须先遵守其 `CLAUDE.md`（如需复用 `AGENTS.md`，在其中显式使用 `@AGENTS.md`）。Worker 的本地上下文严格遵循 Claude Code 的工作方式：用 `Glob` 定位文件、用 `Grep`（`ripgrep`）搜索内容、用 `Read` 读取确认过的文件；`Shell/PowerShell` 用于在受权限和工作区边界约束的情况下执行本地命令。运行时不维护本地检索索引、不进行网络页面检索或读取，也不暴露外部工具协议面。

```powershell
uv run pytest -q tests/unit/workspace/test_code_tools.py
```

该测试覆盖受限根目录内的文件发现、基于 `rg --json --no-config` 的内容搜索和分段读取；PowerShell 工具同样受工作区根目录、超时、输出上限与审批策略约束。

仓库级 closure audit 同时验证：旧服务器路径和根旧入口不存在；可执行 Python 不导入、动态加载
或启动 Khoj/Django/PostgreSQL；发布 metadata/lock/SBOM 不含旧依赖；生产源码只有一个规范
`@agent_loop_entrypoint`。历史文档可以保留迁移说明，但其中的命令不能进入任何可执行入口。

完整产品范围与 Definition of Done 以仓库外层工作区的 `task.md` 为准；阶段目录只表示依赖顺序，不代表功能裁剪。
