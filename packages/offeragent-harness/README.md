# OfferAgent Harness

`offeragent-harness` 是 OfferAgent 的 Windows 本地、Provider-neutral Agent Core 与 Runtime。它不是 Khoj Server 的便携打包，也不启动 Codex Agent Runtime。

核心所有权：

- 模型只能通过 `ModelGateway` 参与规划和生成。
- 唯一 Agent Loop、工具调度、权限、审批、Session/Event、Vault/RAG、Memory、MCP、Shell 和 Subagent 均归本包及其本地 Adapter。
- Obsidian 与本地 Web UI 是同一 Vault Worker 的客户端，不拥有第二套 Agent 状态机。
- 生产依赖不得包含 Khoj、Django、PostgreSQL、LangChain 或远程 Conversation/Workspace Store。

开发命令：

```powershell
uv sync --extra dev --locked --python 3.12
uv run pytest -q
uv run ruff check src tests
uv run ruff format --check src tests
uv run mypy src tests
uv run lint-imports --config .importlinter --no-cache
uv run python -m offeragent_harness.protocol.schemas check
uv run python scripts/check_architecture.py
uv run python scripts/check_forbidden_dependencies.py
uv build
```

完整产品范围与 Definition of Done 以仓库外层工作区的 `task.md` 为准；阶段目录只表示依赖顺序，不代表功能裁剪。

