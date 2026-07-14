# Windows 子进程网络与文件系统隔离

## 结论

`ProcessSupervisor` 的 `allow_network=False` 生产路径使用当前用户下、按 Workspace 稳定命名的
AppContainer，并通过 `PROC_THREAD_ATTRIBUTE_SECURITY_CAPABILITIES` 创建零 capability 子进程。
零 capability 是网络拒绝边界；Job Object 只负责进程树归属、取消、崩溃回收和资源约束，**不提供网络隔离**。

禁止用进程名、环境变量、代理设置或“进程已在 Job 中”推断网络已被隔离。子进程在恢复运行前必须验证：

- `TokenIsAppContainer` 与本次网络策略一致；
- `TokenAppContainerSid` 等于本 Workspace 的 Package SID；
- `TokenCapabilities` 数量为零；
- 镜像仍是已验证并保持打开的固定镜像；
- 子进程同时属于 Worker Job 和本次调用的 Job。

任一条件不成立时，在 `CREATE_SUSPENDED` 状态终止进程并失败关闭，不降级为普通进程。

## Workspace 身份与进程创建

AppContainer profile 名为 `OfferAgent.NoNetwork.<workspace-instance-id>`。它是当前 Windows 用户拥有的稳定
profile，同一 Workspace 重启后派生出相同 Package SID；不同 Workspace 使用不同 SID。正常 Worker 关闭只释放
SID 句柄，不删除 profile。Workspace 注销或完整卸载时，在 Host/Worker 静默后删除 profile。

一次创建使用同一个 `STARTUPINFOEX` 属性表组合：

- `PROC_THREAD_ATTRIBUTE_SECURITY_CAPABILITIES`：Package SID，capability 列表为空；
- `PROC_THREAD_ATTRIBUTE_JOB_LIST`：Worker Job（如有）与 invocation Job；
- `PROC_THREAD_ATTRIBUTE_HANDLE_LIST`：仅 stdin/stdout/stderr 三个继承句柄。

因此 Shell 和 Hook 可使用匿名管道；匿名管道是父进程预先创建并显式继承的 handle，不需要把 Vault
或 Named Pipe ACL 扩大给 AppContainer。子进程的后代默认继承 AppContainer token，并继续受 Job 进程树
约束。Worker 的 SID 专属 Named Pipe 仍只服务插件 IPC，不授予 AppContainer 主动连接权限。

## 文件系统能力

AppContainer token 对普通用户可访问的路径也不会自动获得访问权。发行内置 profile 必须随签名发布明确的文件系统
声明；Workspace 用户 profile 必须经下节的两阶段固定流程登记同样的声明。运行时只把声明解析为真实目录：

- 可执行文件固定安装根：`read_execute`；
- 精确工作目录或资源子树：`read` 或 `read_write`；
- Workspace/Vault 声明必须是非空、无 `.`/`..`、非绝对路径的窄相对目录；
- Workspace 资源禁止 `read_execute`；
- cwd 必须位于固定安装根或某个已声明资源下；
- 目标目录与 ACL 状态目录都不得是 reparse point。

禁止声明空路径，因而不能把整个 Vault 作为隐式资源。Vault 读写仍应优先走 Worker 的 Vault Tool/Policy/
Approval/Journal；只有签名 profile 明确需要的窄目录才临时加入 AppContainer DACL。

## Workspace 用户注册

发行内置 profile 继续由签名 `process-catalog.v1.json` 授权。用户额外选择的程序不修改发行目录，也不能热插入
正在运行的 `ProcessSupervisor`；它们使用当前 Workspace 的 SQLite 聚合，并只在下一次 Worker 启动前合并成一次
不可变快照。管理面仅经当前 SID 的 Named Pipe 暴露，Loopback Web 不返回本机绝对路径。

注册必须经过同一 Pipe 连接上的 `probe -> confirm`：probe 有效期五分钟且只可消费一次；confirm 再次读取文件身份
和 SHA-256 后，与 catalog revision、record revision/contentHash 及幂等 `clientRequestId` 在同一事务提交。断线丢失
ACK 后可由无路径的 durable receipt 重放。运行时变更只返回 `restartRequired=true`，不会创建第二个进程执行器。

用户程序必须是本地绝对 `.exe`，并拒绝 UNC/device path、reparse point、硬链接和命令解释器。注册保存 canonical
path、固定安装根、文件 identity、SHA-256、argv schema、stdin/cwd/environment 能力及窄 AppContainer grant；
`allowNetwork` 恒为 `false`。离线 Authenticode 通过时仍固定文件身份；未通过时 UI 必须明确显示“未通过
Authenticode，仅固定当前 SHA-256”。任一文件、签名或 profile fingerprint 漂移都会使注册在启动快照中不可用，
不会降级执行。普通环境变量和 Secret 变量名分开登记，`PATH`、代理变量及危险 loader/runtime 变量均拒绝。

## ACL 所有权、并发与崩溃恢复

每个授权以 `(canonical path, Package SID, access mask, inheritance flags)` 为精确键。管理器先判断 Package SID、
All Application Packages 等 AppContainer principal 是否已经具备所需访问；已有权限不归 OfferAgent 所有，也不会
在清理时删除。确需新增时：

1. 把路径、SID、mask、flags、原 DACL 摘要和 `pending_add` 状态原子写入
   `process-sandbox/appcontainer-acl-journal.json`，并 `fsync`；
2. 在显式 ACE 与继承 ACE 的规范边界插入一个 Package-SID allow ACE；
3. 记录新增后 DACL 摘要和 `active`；
4. 同键并发调用只增加引用计数；最后一个 lease 关闭时记录 `pending_remove`；
5. 基于当前 DACL 仅删除一个完全匹配的自有 ACE，不回写旧的整份 DACL，因此并发的外部 ACL 修改会保留。

Worker 启动时先恢复残留 journal。完整卸载在删除 `%LOCALAPPDATA%\OfferAgent\workspaces` 前扫描合法
`wsi_<uuid>` 状态目录，恢复精确 ACE 后删除对应 profile；清理可重复执行。journal 无法验证时失败关闭，避免在
失去所有权证据后猜测性修改 ACL。

## 不采用的方案

Windows 11 的 `CreateProcessInSandbox` 目前是实验性 API、没有公开 SDK header，且其契约不支持继承 handle 和
进程/线程属性。它无法同时满足现有的原子 Job 归属和受控进程管道，因此当前不作为生产路径。防火墙规则还会
引入管理员权限、规则生命周期和可执行路径竞态，也不作为普通用户默认方案。

## 验证要求

Windows 真机测试必须至少覆盖：

- 同一个 `127.0.0.1` listener：普通子进程连接成功，零 capability AppContainer 连接失败；
- 已声明目录文件可读、可写，未声明的相邻目录文件不可读；
- Worker/invocation Job 归属与 stdout/stderr/stdin handle 继承；
- 优雅结束后 journal 与精确 ACE 清空；
- 模拟 Worker 崩溃后恢复精确 ACE，并保留原有/外部 ACL；
- 完整卸载在状态目录删除前恢复 ACE，重复清理幂等；
- 空相对路径（整个 Vault）声明被拒绝。

## Microsoft 依据

- [Implementing an AppContainer](https://learn.microsoft.com/en-us/windows/win32/secauthz/implementing-an-appcontainer)
- [UpdateProcThreadAttribute](https://learn.microsoft.com/en-us/windows/win32/api/processthreadsapi/nf-processthreadsapi-updateprocthreadattribute)
- [CreateAppContainerProfile](https://learn.microsoft.com/en-us/windows/win32/api/userenv/nf-userenv-createappcontainerprofile)
- [DeleteAppContainerProfile](https://learn.microsoft.com/en-us/windows/win32/api/userenv/nf-userenv-deleteappcontainerprofile)
- [SetNamedSecurityInfoW](https://learn.microsoft.com/en-us/windows/win32/api/aclapi/nf-aclapi-setnamedsecurityinfow)
- [CreateProcessInSandbox（实验性）](https://learn.microsoft.com/en-us/windows/win32/secauthz/createprocessinsandbox)
