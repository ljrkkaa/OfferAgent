# ADR-0003：Vault 写入是 Loop 内 Client Tool 事务

状态：Accepted

## 决策

所有写入统一表示为 `vault.transaction`，依次通过 Schema、Policy、preflight、Diff、Approval、expectedHash 重验、真实执行、Journal 和结果回填。需要编辑器/metadata 语义时通过双向 Named Pipe 调用 Obsidian Client Tool；基线可靠时 Worker 可执行本地原子事务。

## 后果

`prepared` 不解除写完成义务。ACK 丢失通过 invocation journal 查询结果，不得重放 append。插件仅返回真实 before/after hash 与 typed outcome。

