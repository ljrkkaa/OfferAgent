# ADR-0005：Subagent 是同一 Worker 的 child AgentRun

状态：Accepted

## 决策

Subagent 复用同一个 Loop、ModelGateway、Tool Kernel、Policy、EventStore 与 Unit of Work，通过 `parentRunId/rootRunId` 形成树。有效权限为 system、parent、profile 与 requested scope 的交集。

## 后果

不使用 Codex Subagent 或第二 Runtime。父子预算、取消、审批 lineage、文件锁和恢复都由 Harness 管理；子 Agent 默认只读且不能扩大权限。

