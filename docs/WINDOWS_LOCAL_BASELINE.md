# OfferAgent Windows 本地化迁移基线

记录时间：2026-07-12（Asia/Shanghai）

## 源码来源

- 服务器：`10.106.17.252:/data/ljr/my_project/khoj`
- 服务器分支：`dev`
- 服务器 HEAD：`ac55173f97a05caf7c8bb5f5d09f18215f2743b3`
- 服务器工作树：42 个 tracked 路径变化（35 M、1 A、6 D），无 untracked 文件
- binary diff：669 additions、3535 deletions
- binary diff SHA-256：`6C7269547A2F9ECF20816996CACD28E484EEE5D3B989465CAB275D7A3A118315`
- 本地迁移分支：`codex/windows-local-harness`
- 本地迁移基线提交：`b3ab685b`（`chore: preserve audited server working tree baseline`）

迁移时的一次性服务器快照已于 2026-07-16 从外层工作区清理；可追溯代码基线保留在本地迁移基线提交 `b3ab685b` 和归档分支 `archive/khoj-server-baseline-20260713`。审计时，本地应用后的 `git diff HEAD` 与服务器 binary diff 逐字节相同；本地与服务器 `git status --porcelain=v2 -z` 也具有相同 SHA-256。

## 本地工具链

- Windows 11 x64，中文系统，版本 `10.0.26200`
- Git `2.47.1.windows.2`
- OpenSSH `9.5p2`
- uv `0.11.17`
- 锁定测试解释器：uv-managed CPython `3.12.13`
- Node.js `24.15.0`
- Corepack/Yarn `1.22.22`

根项目声明 Python `>=3.10,<3.13`。本机默认 `python` 是 3.13，因此测试必须通过 `uv` 使用 3.12，不能依赖 PATH 中的启发式解释器选择。

## 旧 Python 测试基线

依赖安装：

```powershell
uv python install 3.12
uv sync --extra dev --python 3.12 --frozen
```

### 旧全局测试夹具问题

命令：

```powershell
$env:USE_EMBEDDED_DB='true'
$env:PGSERVER_DATA_DIR='E:\Projects\offeragent\test-state\legacy-pg'
uv run --frozen pytest tests/test_tool_protocol.py -q --disable-warnings
```

结果：15 errors，全部发生在测试数据库 setup，测试函数没有执行。原因是 `tests/conftest.py` 对所有测试强制请求 Django `db` fixture；旧 `pgserver` 配置在 Windows 上仍把目录当作 PostgreSQL Unix socket，连接被拒绝。这是旧测试/Core 与 Django/PostgreSQL 反向耦合的迁移证据，不是 Tool Protocol 逻辑失败。

隔离旧全局 conftest 后：

```powershell
uv run --frozen pytest --noconftest tests/test_tool_protocol.py -q -o addopts='' --disable-warnings
```

结果：`15 passed`。

其余优先迁移测试：

```powershell
uv run --frozen pytest --noconftest `
  tests/test_agent_tool_loop.py `
  tests/test_knowledge_workspace.py `
  tests/test_conversation_turn.py `
  tests/test_vault_actions.py `
  tests/test_codex_conversation_adapter.py `
  tests/test_local_kb.py `
  tests/test_local_kb_fallback.py `
  -q -o addopts='' --disable-warnings
```

结果：`98 passed, 2 skipped, 6 failed, 23 errors`。

- 23 errors：需要 Django/PostgreSQL 的 Conversation/VaultAction 持久化与并发测试，Windows 本机没有旧 PostgreSQL Server。
- 3 个 symlink 相关失败：Windows 当前用户没有创建符号链接的权限；目标实现需要用可控 reparse-point fixture 和权限感知测试覆盖，而不是跳过路径逃逸不变量。
- 2 个 VaultAction 原子操作失败：旧实现直接调用 Linux `renameat2`/`ctypes.CDLL(None)`，不能在 Windows 工作。
- 1 个超长路径 fixture 失败：当前临时目录组合超过 Windows 路径限制；目标测试需要同时覆盖长路径启用和 fail-closed 行为。

这些失败必须由新的 Windows-native SQLite、PathPolicy 和事务执行器测试替代，不能通过删除测试或放宽安全语义处理。

## 旧 Obsidian 插件基线

```powershell
corepack yarn install --frozen-lockfile --non-interactive
corepack yarn test
corepack yarn build
```

结果：

- Node tests：`19 passed, 0 failed`
- TypeScript 检查和 production esbuild：通过
- 生成 `main.js`：约 129.3 KiB

现有测试主要证明旧 HTTP Server Adapter、流式帧解析和回答后 VaultAction Apply 的行为。目标插件会删除 HTTP Agent/sync 路径，因此这些测试只能作为迁移行为基线；必须新增 Named Pipe RPC、Event replay、Worker-local Tool Journal、Runtime bootstrap 和同一 Worker identity 契约测试。

## 环境与安全状态

- 未读取或复制真实插件 `data.json`、Codex auth、SSH key、Token、Cookie 或数据库密码。
- 未读取或写入真实 Vault 内容。
- 服务器只执行 Git/容量只读命令，状态变化为 0。
- Python `.venv`、Node `node_modules` 和插件构建产物均被忽略，未进入 Git 状态。
- 旧服务器 `src/khoj/pgserver_data` 报告文件系统错误；它是明确禁止迁移的数据库状态目录。
