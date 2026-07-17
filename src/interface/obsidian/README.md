# OfferAgent for Obsidian

这是 OfferAgent 个人 Windows x64 Runtime 的 Obsidian 客户端。每个插件实例验证本地 Runtime manifest
后，直接启动一个 `offeragent-worker.exe` 子进程，并渲染该 Worker 的 Session、Run、工具、审批和
诊断事件。

## 边界

- 插件与 Worker 的唯一 IPC 是继承 stdin/stdout 上的 framed JSON-RPC；stderr 只解析稳定诊断码。
- 没有常驻协调 Host、插件 IPC 中间 Host、discovery、Named Pipe、listener、后台 Worker 或常驻运行模式。
- 插件不连接 OfferAgent/Khoj Server，不上传或同步 Vault 内容，不实现第二套 Agent Loop。
- 插件不执行文件写、模型调用、Shell 或 Hook。磁盘事务、工具调用和状态恢复都由 Worker 拥有。
- “打开本地 Web”只访问同一 Worker 的随机 Loopback 端口。
- Shell/Hook 等进程工具由 Worker 通过短生命周期 `offeragent-process-host.exe` 执行；插件不直接
  启动或代理这些进程。

Provider secret 输入只作为一次性命令经当前 stdio 通道写入 Workspace 绑定的 Windows DPAPI
SecretStore。插件配置只保存 opaque handle，不保存 API key 明文；插件自身不包含 Provider 端点逻辑。

## 生命周期

插件热重载、禁用、Obsidian 退出或 `onunload` 会在回调返回前同步关闭 stdio、发起当前子 Worker
回收并取消其中的活动 Run；Obsidian 不等待 `onunload` 返回的 Promise，因此实际进程 join 在后台继续。
命令面板的“停止当前 Vault 的 OfferAgent Runtime”会等待同一清理操作完成，并清理该 Worker 本轮创建的
Shell、Hook 与 Subagent 进程树。显式停止和自动重连都会等待旧 Worker 退出；并发停止共享同一个清理
操作，停止期间的新启动也必须等待。同一交互式 Windows 会话和用户内，后续同 Vault Worker 在旧进程
释放互斥锁前不能访问 Runtime 状态。它不影响其他 Vault，也不删除 Vault 笔记或本地 Runtime 状态。

## 开发门禁

在 `src/interface/obsidian` 中执行：

```powershell
corepack yarn install --frozen-lockfile
corepack yarn protocol:check
corepack yarn typecheck
corepack yarn test
```

插件构建面只保留：

- `protocol:check`：以非修改模式核对 Harness schema 与生成的 identity/types；
- `typecheck`：先执行协议检查，再运行 TypeScript `--noEmit`；
- `build:local`：由 Harness 个人构建脚本调用，必须注入 Runtime manifest SHA-256 和输出路径。

不要恢复无 manifest 锚点的 `build`/`dev`，也不要直接调用 esbuild 生成可安装 bundle。完整插件只能由
`packages/offeragent-harness/scripts/build_local_windows_plugin.py` 构建，并由
`packages/offeragent-harness/scripts/update_local_windows_plugin.py` 更新。

生成的 `main.js` 是安装产物，不能作为长期维护源码。不要读取、复制或打印目标 Vault 的 `data.json`；
旧 Server URL、API key 和同步字段也不能进入新配置。
