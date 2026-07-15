# ADR-0003：Vault 文件工具由 Worker 单一执行

状态：Accepted（取代插件执行模型）

## 决策

`Glob`、`Grep`、`Read` 与 `vault.transaction` 只在 Worker 的 Tool Registry 中注册，并且只通过 Tool Kernel 执行。Obsidian 和本地 Web 都是协议客户端：发送会话命令、消费可重放事件，不注册执行器、不代理文件工具、不保存调用影子状态。

读工具受 Workspace 根目录、隐藏路径白名单、结果字节上限和当前 Run 权限约束。`Grep` 使用受控的 ripgrep 进程；不存在索引、文档分块、评分、语义检索或查询改写。

写工具先生成显式操作计划和 diff，经统一权限决策后执行基于 `expectedHash` 的本地 CAS。事务结果由 Worker 在同一文件系统边界内校验并写入耐久 Journal；未知提交结果不得自动重放非幂等写入。

## 后果

插件断开不改变已经开始的 Worker Run，也不会切换文件执行路径。工具是否存在、是否对当前 Run 可见、是否获准执行以及实际结果分别记录，UI 仅渲染这些权威事件。
