# ADR-0001：每个 Vault Worker 只有一个 Agent Loop

状态：Accepted

## 决策

Agent Loop 只存在于 Windows Worker 的 Core 中，并以唯一 `@agent_loop_entrypoint` 标记。Obsidian direct stdio 和测试 Adapter 都调用同一个 `HarnessService`，不得定义第二套 Planner/Tool 循环或 HTTP/WebSocket 控制面。

## 后果

多 Session 可以在统一调度器和预算下并发，但共享同一实现、Store、Policy 和 Worker 所有权。CI 通过 AST 检查确保生产源码恰好一个入口。

