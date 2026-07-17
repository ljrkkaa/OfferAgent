# OfferAgent Windows 本地化迁移基线

记录起点：2026-07-12（Asia/Shanghai）。本文件保存迁移来源和不变量，不保存会随代码变化失真的
测试通过数量、构建大小或临时 schema hash。

## 可追溯来源

- 旧服务器仓库：`10.106.17.252:/data/ljr/my_project/khoj`
- 旧分支/HEAD：`dev@ac55173f97a05caf7c8bb5f5d09f18215f2743b3`
- 本地迁移基线提交：`b3ab685b`
- 只读历史分支：`archive/khoj-server-baseline-20260713`
- 迁移时服务器 binary diff SHA-256：
  `6C7269547A2F9ECF20816996CACD28E484EEE5D3B989465CAB275D7A3A118315`

一次性服务器 snapshot 已从工作区清理。旧实现只用于行为追溯，不能被主分支 import、启动、打包或
用作远程降级。

## 本地工具链基线

- Windows 11 x64
- uv 管理的 CPython 3.12；不得依赖 PATH 中可能不兼容的默认 Python
- Node.js 与 Corepack/Yarn 1.22 锁定插件依赖
- Git 和显式提供的本地 `rg.exe`

实际版本由锁文件、CI 和运行命令输出决定；本文件不把某次开发机版本写成永久要求。

## 从旧实现继承的不变量

旧测试受 Django 全局 fixture、PostgreSQL、Linux `renameat2`、symlink 权限和 Windows 长路径差异影响。
迁移不是删除失败测试，而是把下列语义迁入 Windows-native 层：

- AgentStep schema、工具调用顺序、取消和 exactly-once terminal；
- SQLite UoW、Session/Run 恢复、invocation journal 和 ACK 丢失重放；
- Vault containment、expectedHash、原子替换、冲突和崩溃恢复；
- Glob/Grep/Read 的范围、限额、来源 hash 和链接解析；
- Provider 只推理，工具、状态和权限仍由本地 Harness 拥有；
- 插件事件顺序、stale event 拒绝和用户操作 single-flight；
- Windows Job Object、AppContainer、固定可执行文件身份和子进程树清理。

旧 HTTP Server Adapter、Router、远程 Store、内容同步和 PostgreSQL 锁不属于兼容目标。

## 当前运行基线

- 仅个人 Windows x64。
- 每个 Obsidian 插件实例直接启动一个 `offeragent-worker.exe`。
- 同一交互式 Windows 会话和用户、同一 canonical Vault root 由命名互斥锁保证最多一个 Worker 能持锁
  并进入 Runtime 状态访问；互斥早于 SQLite 和恢复。
- 唯一插件 IPC 是继承 stdin/stdout 上的 framed JSON-RPC。
- 显式停止或重连会等待旧 Worker 优雅退出，超时强杀后仍等待进程退出；`onunload` 会同步关闭 stdio
  并发起回收，后台继续 join，互斥锁在旧进程退出前阻止同会话同 Vault Worker 访问状态。没有常驻
  Host、discovery、Named Pipe 或后台运行模式。
- 进程工具使用短生命周期 `offeragent-process-host.exe`，受 Job/AppContainer/hash 以及可选离线
  Authenticode 约束。
- 没有正式签名发布、Setup、自动更新通道或 ARM64 产物。

## 可复现门禁

Harness 候选在 `packages/offeragent-harness` 执行：

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

插件候选在 `src/interface/obsidian` 执行：

```powershell
corepack yarn install --frozen-lockfile
corepack yarn protocol:check
corepack yarn typecheck
corepack yarn test
```

完整插件 bundle 只能由 `scripts/build_local_windows_plugin.py` 产生，更新只能由
`scripts/update_local_windows_plugin.py` 执行。测试数量、skip 数量、bundle 字节数和 schema hash 必须
从当次命令及生成 manifest 获取，不能从本文件复制。

## 数据安全基线

- 不读取、复制或打印真实插件 `data.json`、Codex auth、SSH key、Token、Cookie 或数据库密码。
- 真实 Vault 默认只读；写入必须获得明确授权并限制范围。
- `.venv`、`node_modules`、插件 bundle 和临时 Runtime 不进入 Git。
- 旧 PostgreSQL 数据目录和服务器工作树永远不进入当前 Runtime 状态。
