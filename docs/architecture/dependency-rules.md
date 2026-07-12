# OfferAgent Harness 依赖与所有权规则

## 权威入口

每个 Vault 只有一个 Worker；Worker 内只有一个带 `@agent_loop_entrypoint` 标记的 Agent Loop 实现。`HarnessService` 是所有 UI Command 的应用入口。Named Pipe、Loopback Web、测试 Direct Adapter 都必须调用这一个服务。

## 依赖方向

```text
app / cli composition root
  -> adapters
    -> runtime application services
      -> agent / tool / policy domain
        -> ports
```

`protocol` 是独立 wire DTO 岛。Core 领域对象不复用 Pydantic wire DTO；只有 Adapter mapper 可以连接二者。

## 禁止依赖

- 新 Runtime 不得 import `khoj`、Django、PostgreSQL、Router、LangChain 或旧远程 Store。
- Model Provider 不得 import Tool Kernel、Vault、Storage、ProcessSupervisor，也不得使用文件或进程 API。
- 插件和本地 Web UI 不得 import 模型 SDK、工具执行器或实现 Planner/Tool Loop。
- 生产模块不得 import `offeragent_harness.testing`。
- Core 不读取全局 Vault/Model 环境变量；Workspace 与 RunConfig 通过不可变快照注入。

## 状态与副作用

- 状态、事件、预算、审批与 invocation journal 通过同一个 Unit of Work 原子提交。
- Artifact blob 先以内容寻址方式幂等 stage，随后在 Unit of Work 中提交 metadata/link；未链接 blob 由 GC 清理。
- Event Store 是 UI 恢复事实来源，EventSink 断线不能回滚已提交事件。
- 未声明安全属性的工具构造失败；不得根据工具名推断并发、幂等或风险。

