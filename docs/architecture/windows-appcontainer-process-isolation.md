# Windows 进程工具隔离

## 结论

Shell、Hook 等进程工具由 Worker 通过短生命周期 `offeragent-process-host.exe` 执行。可执行镜像和
process catalog 固定 SHA-256；进程从 suspended 状态创建，验证通过后才恢复，并同时受 Job Object、
文件系统 grant、输出/超时限额和权限策略约束。

`ProcessSupervisor` 的 `allow_network=False` 路径使用当前用户下按 Workspace 稳定命名的
AppContainer，并通过 `PROC_THREAD_ATTRIBUTE_SECURITY_CAPABILITIES` 创建零 capability 子进程。
零 capability 是网络拒绝边界；Job Object 只负责进程树归属、取消、崩溃回收和资源约束，不能替代
网络隔离。

禁止用进程名、环境变量、代理设置或“已经在 Job 中”推断隔离成功。进程恢复前必须验证：

- `TokenIsAppContainer` 与本次网络策略一致；
- `TokenAppContainerSid` 等于当前 Workspace 的 Package SID；
- `TokenCapabilities` 数量为零；
- 镜像仍是已验证并保持打开的固定文件；
- 子进程同时属于 Worker Job 和本次 invocation Job。

任一条件不成立都在 `CREATE_SUSPENDED` 状态终止进程并失败关闭，不降级成普通进程。

## 与插件 IPC 的边界

Obsidian 插件实例直接启动 `offeragent-worker.exe`。插件与 Worker 的唯一 IPC 是继承 stdin/stdout
上的 framed JSON-RPC；没有常驻协调 Host、插件 IPC 中间 Host、discovery、Named Pipe 或 listener。

Worker 启动 Process Host 或其他受监督子进程时，使用同一个 `STARTUPINFOEX` 属性表组合：

- `PROC_THREAD_ATTRIBUTE_SECURITY_CAPABILITIES`：Package SID，capability 列表为空；
- `PROC_THREAD_ATTRIBUTE_JOB_LIST`：Worker Job 与 invocation Job；
- `PROC_THREAD_ATTRIBUTE_HANDLE_LIST`：只继承 stdin/stdout/stderr 三个句柄。

这里的匿名管道是父进程预先创建并显式继承的进程句柄，不是可发现 IPC 服务，也不需要修改 Vault
或任何 listener ACL。子进程后代继承 AppContainer token，并继续受 Job 进程树约束。

## Workspace 身份

AppContainer profile 名为 `OfferAgent.NoNetwork.<workspace-instance-id>`。它由当前 Windows 用户拥有：
同一 Workspace 重启后派生相同 Package SID，不同 Workspace 使用不同 SID。正常 Worker 关闭只释放
SID 句柄，不在进程退出时猜测性删除 profile。

显式 Workspace 清理逻辑必须先恢复由 OfferAgent 精确拥有的文件系统 ACE，再删除对应 profile；
该逻辑不是 Setup、后台更新器或常驻服务。

## 文件系统能力

AppContainer token 不会自动继承普通用户对文件的访问权。内置进程能力由本地开发 Runtime manifest
固定的 `process-catalog.v1.json` 声明；用户注册的程序也必须产生不可变的启动快照。声明只允许：

- Runtime 内固定可执行根：`read_execute`；
- 精确工作目录或资源子树：`read` 或 `read_write`；
- Workspace/Vault grant 必须是非空、无 `.`/`..`、非绝对路径的窄相对目录；
- Workspace 资源禁止 `read_execute`；
- cwd 必须位于 Runtime 固定根或已声明资源中；
- 目标目录和 ACL 状态目录都不得是 reparse point。

空相对路径被拒绝，因而不能隐式授权整个 Vault。Vault 读写仍优先走 Worker 的 Tool、Policy、Approval
和 Journal；只有进程 profile 明确需要的窄目录才临时加入 AppContainer DACL。

## 用户程序注册

用户选择的程序不修改 Runtime 目录，也不能热插入正在执行的 `ProcessSupervisor`。注册状态由当前
Workspace 的 SQLite 持久化，只在下一次 Worker 启动前合并成不可变快照。管理命令只经当前插件实例
到 Worker 的 stdio JSON-RPC 执行，协议结果不返回本机绝对路径。

注册采用 `probe -> confirm`：probe 有限时效且只可消费一次；confirm 再次读取文件 identity 和
SHA-256，并把 catalog revision、record revision/contentHash 与幂等 `clientRequestId` 在同一事务
提交。ACK 丢失可由无路径 durable receipt 重放。成功结果返回 `restartRequired=true`，不会创建
第二个进程执行器。

用户程序必须是本地绝对 `.exe`，拒绝 UNC/device path、reparse point、硬链接和命令解释器。记录
包含 canonical path、固定安装根、文件 identity、SHA-256、argv schema、stdin/cwd/environment 能力
和窄 AppContainer grant；`allowNetwork` 恒为 `false`。

离线 Authenticode 是用户注册可执行文件的可选附加信号，不替代 hash 固定。验证通过仍固定文件身份；
未通过时 UI 显示“未通过 Authenticode，仅固定当前 SHA-256”。文件、签名或 profile fingerprint 漂移
都会使注册项在下次启动快照中不可用，不能降级执行。普通环境变量与 Secret 名分开登记，`PATH`、
代理变量和危险 loader/runtime 变量均拒绝。

## ACL 所有权与恢复

每个授权以 `(canonical path, Package SID, access mask, inheritance flags)` 为精确键。若 Package SID
已经由外部 ACL 获得所需权限，OfferAgent 不取得所有权，也不会在清理时删除。确需新增时：

1. 将路径、SID、mask、flags、原 DACL 摘要和 `pending_add` 原子写入 ACL journal 并 `fsync`；
2. 在规范位置插入一个 Package-SID allow ACE；
3. 记录新增后的 DACL 摘要和 `active`；
4. 同键并发调用只增加引用计数，最后一个 lease 关闭时记录 `pending_remove`；
5. 只删除一个完全匹配的自有 ACE，不回写整份旧 DACL，保留并发外部修改。

Worker 启动时恢复残留 journal。journal 或目录 identity 无法验证时失败关闭，不能在失去所有权证据后
猜测性修改 ACL。

## 不采用的方案

Windows 11 `CreateProcessInSandbox` 仍是实验性 API，不能同时满足继承句柄和原子 Job 归属；当前不
采用。防火墙规则会引入管理员权限、规则生命周期和路径竞态，也不作为普通用户默认方案。

## 验证要求

当前 Windows x64 候选至少覆盖：

- 普通子进程可连接测试 listener，而零 capability AppContainer 被拒绝；
- 已声明目录可按权限访问，相邻未声明目录不可访问；
- Worker/invocation Job 归属和三个标准句柄继承；
- 固定 hash、文件 identity 与可选 Authenticode 漂移均 fail closed；
- 优雅结束后 invocation journal 和精确 ACE 清理；
- 模拟 Worker 崩溃后恢复自有 ACE，并保留外部 ACL；
- 空相对路径和整个 Vault grant 被拒绝。

结果以当前 process/AppContainer 测试、Import Linter、architecture 和 repository closure 门禁为准，
不在文档中硬编码测试数量。

## Microsoft 依据

- [Implementing an AppContainer](https://learn.microsoft.com/en-us/windows/win32/secauthz/implementing-an-appcontainer)
- [UpdateProcThreadAttribute](https://learn.microsoft.com/en-us/windows/win32/api/processthreadsapi/nf-processthreadsapi-updateprocthreadattribute)
- [CreateAppContainerProfile](https://learn.microsoft.com/en-us/windows/win32/api/userenv/nf-userenv-createappcontainerprofile)
- [DeleteAppContainerProfile](https://learn.microsoft.com/en-us/windows/win32/api/userenv/nf-userenv-deleteappcontainerprofile)
- [SetNamedSecurityInfoW](https://learn.microsoft.com/en-us/windows/win32/api/aclapi/nf-aclapi-setnamedsecurityinfow)
- [CreateProcessInSandbox（实验性）](https://learn.microsoft.com/en-us/windows/win32/secauthz/createprocessinsandbox)
