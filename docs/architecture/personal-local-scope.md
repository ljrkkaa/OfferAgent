# OfferAgent 个人本机插件交付范围

## 支持范围

当前产品只交付给项目所有者本人，在一台 Windows x64 电脑上通过 Obsidian 使用。它是未签名的
个人本地开发安装，不是公开安装包。

唯一支持的运行拓扑是：每个 Obsidian 插件实例直接创建一个 `offeragent-worker.exe` 子进程，插件
只通过继承 stdin/stdout 上的 framed JSON-RPC 与它通信。Worker 拥有唯一 `HarnessService`、Agent
Loop、SQLite、Session/Run、文件工具和副作用边界。同一交互式 Windows 会话和用户、同一 canonical Vault root 的命名
互斥锁在 SQLite 打开前排除第二个 Worker。显式停止和插件内部重连会等待 Worker 及其子进程树退出；
`onunload` 则在同步回调返回前关闭 stdio 并发起后台 join。

仓库没有常驻 Host、discovery、Named Pipe、后台 Worker、正式签名发布、Setup、自动更新通道或
ARM64 运行路径。这些不是隐藏开关、兼容模式或等待重新启用的发行设计。

## 必须满足的产品能力

1. **真实插件可用**：插件能部署到目标 Vault，保留既有 `data.json`，并能加载、聊天、显示状态和
   停止当前 Runtime。
2. **Runtime 身份唯一**：一个插件实例只有一个直接子 Worker；同一交互式 Windows 会话和用户内，
   同一 Vault 同时最多一个 Worker 能持锁并进入 Runtime 校验、SQLite、恢复和运行期；
   命令只能经 stdio JSON-RPC 进入同一 `HarnessService`，本地 Web 也只连接同一个 Worker。
3. **模型边界明确**：至少一个用户配置的 `ModelGateway` Provider 能完成真实对话；Provider 不得
   取得工具、Vault、Session、Memory、审批或 Subagent 所有权。
4. **真实工作区读取可用**：在只读权限下验证 Glob、Grep、Read、引用和 wikilink/backlink；项目源码、
   构建物和测试报告不得意外进入工具可见范围。
5. **安全写闭环可用**：模型面对的写入口只允许单文件 create/append/replace/patch，并覆盖 Diff、
   审批、`expectedHash`、原子提交、冲突、取消、ACK 丢失、硬崩溃和幂等恢复。真实 Vault 写 smoke
   必须另获授权并限制到专属测试目录。
6. **进程工具受控**：Shell/Hook 等通过短生命周期 `offeragent-process-host.exe` 执行；镜像和 catalog
   固定 hash，进程树归入 Job Object，无网络策略使用 AppContainer，用户注册的可执行文件可要求
   离线 Authenticode 并始终固定文件身份。
7. **生命周期可靠**：显式停止、断线和设置重启必须先回收旧 Worker，再由同一插件实例启动替代
   Worker；`onunload` 必须在同步回调返回前关闭 stdio 并发起后台 join，后续同会话同 Vault Worker 在旧
   进程释放互斥锁前不得访问状态。任何路径都不得重复写、伪报成功或遗留 Shell、Hook 或 Subagent
   进程。
8. **可复现构建**：Harness、协议、架构、插件测试与类型检查通过；本机插件只由
   `scripts/build_local_windows_plugin.py` 构建，只由 `scripts/update_local_windows_plugin.py` 更新。

## 保留的非阻塞能力

以下能力继续接受安全性和正确性修复，但不作为个人版本的 UI 完成阻塞项：

- 同一 Worker 提供的本地 Loopback Web UI；
- Memory、Skills、Hooks、Shell 和 Subagent 的高级配置；
- 复杂 Run tree 和多 Vault 同时打开时的体验优化；
- 多文件批次、rename/trash 的 durable journal 扩展；
- 大规模压力、性能和视觉一致性优化。

## 明确不属于当前产品

- 面向第三方的公开分发、安装器、商店上架或后台更新服务；
- Authenticode 发布证书、SmartScreen 信誉或 Ed25519 发布签名体系；
- Windows ARM64 或多架构构建矩阵；
- 常驻进程协调器、跨插件实例发现或 IPC 复用；
- 旧服务器、远程 Conversation/Workspace Store、内容同步或远程降级。

这些能力在当前仓库中没有受支持实现，也不属于当前路线图的保留入口。产品范围发生变化时，应先
建立新的架构决策和威胁模型，而不是恢复已删除代码。

## 完成判定

- 真实 Obsidian 插件能直接启动并通过 stdio 使用本地 x64 Worker。
- 真实 Vault 只读检索通过；临时 Vault 的单文件事务与故障恢复通过。
- 一个真实模型 Provider 可聊天；Session 可恢复；停止后本轮进程树退出。
- Python、协议、依赖、文档、Web asset、插件测试和类型检查门禁实时通过。
- 从干净输出目录执行个人构建，并由更新脚本原子切换到目标 Vault。
- 没有影响个人主路径或数据安全的已知 P0/P1。

构建和更新说明见
[`docs/personal-local-plugin-install.md`](../personal-local-plugin-install.md)。
