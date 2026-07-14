---
status: accepted
---

# Runtime is a plugin-owned child process

第一版本地 Runtime 由 Obsidian 插件启动和管理，并随 Obsidian 会话启停，而不是安装成 Windows Service 或长期驻留进程。插件使用随机端口和一次性令牌启动 Runtime；正常卸载或父进程消失时，Runtime 先持久化 Agent Run 状态再退出，下一次启动由新的 Runtime 实例从 SQLite 恢复。

## Consequences

- Obsidian 插件必须声明为桌面专用。
- Runtime 启动握手必须返回端口、进程标识、实例标识和协议版本。
- 插件与 Runtime 必须有心跳、优雅关闭和孤儿进程退出机制。
- 第一版不创建 Windows Service、开机启动项或系统定时任务。
