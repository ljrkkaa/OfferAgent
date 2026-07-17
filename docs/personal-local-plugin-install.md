# OfferAgent 个人 Windows x64 构建与更新

这是仓库唯一支持的插件交付路径，仅用于项目所有者当前的 Windows x64 电脑。产物是未签名的
PyInstaller onedir 本地开发 Runtime；仓库没有正式签名发布、Setup、自动更新服务或 ARM64 产物。

## 信任和完整性边界

- `development-runtime-manifest.json` 是 canonical JSON，带 `developmentOnly: true`，并固定完整文件集、
  长度、SHA-256、x64 PE 架构、协议/schema、Git commit、源码树摘要和构建身份。
- 构建脚本把 manifest 的期望 SHA-256 编译进 `main.js`。插件初始化和每次启动 Worker 前都会重新
  校验整棵 Runtime；校验失败不会创建 Worker client。
- `offeragent-worker.exe` 和短生命周期 `offeragent-process-host.exe` 还会在冻结进程内校验自身、
  process catalog、内置进程和 Skill 资产。
- PyInstaller 使用显式模块白名单并审计冻结闭包，拒绝 testing/fakes、pytest、旧服务器和已删除
  发行模块进入产物。
- 哈希锚点用于发现损坏、局部替换和混合版本，不提供发布者身份保证；不要把该产物描述成签名包。

## 一次性准备

在 `packages/offeragent-harness` 中安装锁定依赖：

```powershell
uv sync --extra dev --locked --python 3.12
```

在 `src/interface/obsidian` 中安装插件依赖：

```powershell
corepack yarn install --frozen-lockfile
```

构建机必须是 Windows x64，并显式提供一个真实、非 reparse point、非硬链接的 `rg.exe`。脚本不会
从 PATH、网络或其他机器猜测该文件。

## 构建

输出目录必须不存在；唯一支持的完整构建命令是：

```powershell
cd E:\Projects\offeragent\repo\packages\offeragent-harness
uv run python scripts/build_local_windows_plugin.py `
  --output E:\Projects\offeragent\artifacts\offeragent-obsidian-plugin `
  --ripgrep-executable C:\path\to\rg.exe
```

脚本会执行静态门禁，构建 Worker/Process Host，调用插件内部 `build:local`，生成并复验 manifest，
然后写入一个新的输出目录。不要直接调用 esbuild，也不要恢复无 manifest 锚点的 `build`/`dev`。

构建完成后，可在临时 Vault 对该目录执行确定性的融合产品烟测：

```powershell
cd E:\Projects\offeragent\repo\src\interface\obsidian
corepack yarn smoke:fused E:\Projects\offeragent\artifacts\offeragent-obsidian-plugin
```

烟测使用真实冻结 Worker、stdio 协议和本地 Responses Gateway，完成模型配置重启、来源绑定的
`agent_contract.read`/`vault.search`/`vault.read` 回合、Worker 审批、插件确认的 Vault Change Batch、
重启恢复、冲突保护的撤销、持久事件回放及进程树清理。它只操作自动创建并删除的临时 Vault，不能
代替目标 Vault 上另行授权的真实写入验收。

产物结构：

```text
offeragent-obsidian-plugin\
  main.js
  manifest.json
  styles.css
  local-development-build.json
  runtime\windows-x64\local-development\
    development-runtime-manifest.json
    offeragent-worker.exe
    offeragent-process-host.exe
    ...完整 onedir 依赖、rg.exe 和内置资产
```

## 安装或更新

先在插件中执行“停止当前 Vault 的 OfferAgent Runtime”，再完全退出 Obsidian。唯一支持的更新命令是：

```powershell
cd E:\Projects\offeragent\repo\packages\offeragent-harness
uv run python scripts/update_local_windows_plugin.py `
  --vault-root 'E:\面试胜利！' `
  --ripgrep-executable C:\path\to\rg.exe
```

更新脚本会拒绝在 `Obsidian.exe`、`offeragent-worker.exe` 或 `offeragent-process-host.exe` 仍运行时继续。
它从源码在临时目录执行同一构建和验证，再原子切换
`<Vault>\.obsidian\plugins\offeragent-obsidian-plugin`。

现有 `data.json` 只作为不透明文件移动；脚本不会打开、解码、打印或把其中内容复制到构建目录。
失败时旧插件与原设置会回滚；若回滚移动本身失败，承载唯一 `data.json` 的恢复目录会保留并在错误中
报告，清理逻辑不得删除最后一份设置。不要绕过更新脚本手工覆盖插件目录。

## 运行拓扑

每个 Obsidian 插件实例验证本地 manifest 后，直接启动一个 `offeragent-worker.exe` 子进程。唯一插件
IPC 是继承 stdin/stdout 上的 framed JSON-RPC；没有常驻协调 Host、插件 IPC 中间 Host、discovery、
Named Pipe、后台 Worker 或常驻运行模式。
同一交互式 Windows 会话和用户、同一 canonical Vault root 的命名互斥锁在 SQLite 打开和恢复前排除第二个 Worker。插件
显式停止会先请求 shutdown，断线重连会直接关闭旧 transport；两者最终都会关闭 stdin 并等待旧
Worker。stdin 关闭后 15 秒仍未退出才强制终止，且同一插件实例在实际进程退出前不启动替代 Worker。
Obsidian 不等待 `onunload` Promise；卸载回调会同步关闭 stdio、
发起回收并在后台继续 join，同会话同 Vault Worker 在旧进程释放互斥锁前不能访问 Runtime 状态。

Shell、Hook 等进程工具由 Worker 通过短生命周期 `offeragent-process-host.exe` 执行。相关进程受
固定 hash/catalog、Job Object、AppContainer 文件和网络策略约束；用户注册的外部可执行文件可以
额外要求离线 Authenticode，但无论签名状态如何都必须固定文件身份。

删除自动更新功能后，既有 Runtime 配置仍有一次窄兼容迁移：文件 v3 原子迁移为 v4 并保留备份，
SQLite 配置层 v2 可读取并在下次写入时使用 v3。迁移只剥离旧 `update` 和
`network.update_network_enabled`；其他未知或畸形字段仍 fail closed。这不是自动更新代码路径。

## 更新后验收

1. 启动 Obsidian 并启用 OfferAgent，状态应经过“定位 → 校验 → 启动 Worker → 协议握手 → ready”。
2. 打开聊天，确认当前 Vault 的 stdio Worker、文件工具状态和模型配置可见。
3. Provider 凭据只能通过插件到当前 Worker 的 stdio 命令写入 Windows DPAPI SecretStore；不要写入
   `data.json`、Vault、环境变量或命令行。
4. 新 Workspace 默认未信任，实际权限应保持只读；提升信任不等于绕过 Diff 和审批。
5. 首次验收先做真实 Vault 只读检索；真实写入需另行明确授权。
6. 源码或协议变化后必须从不存在的输出目录重新构建，旧 artifact 不能冒充当前候选。
7. 执行 Harness 和插件的实时测试、协议、依赖、文档及类型检查门禁；不要用文档中的历史计数代替。
