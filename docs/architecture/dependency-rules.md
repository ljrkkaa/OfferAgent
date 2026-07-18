# OfferAgent Harness 依赖与所有权规则

## 权威入口

每个 Obsidian 插件实例直接创建一个 Worker；Worker 内只有一个带 `@agent_loop_entrypoint` 标记的 Agent Loop 实现。
`HarnessService` 是所有 UI Command 的应用入口。当前插件 direct stdio 与测试 Direct Adapter
都只能调用这一个服务。插件 IPC 只有继承 stdin/stdout 上的 framed JSON-RPC；不存在第二个
transport composition root 或 HTTP/WebSocket 控制面。

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
- 插件不得 import 模型 SDK、工具执行器或实现 Planner/Tool Loop。
- 生产模块不得 import `offeragent_harness.testing`。
- Core 不读取全局 Vault/Model 环境变量；Workspace 与 RunConfig 通过不可变快照注入。
- `runtime.duplex_json_rpc` 只负责 direct stdio framing、取消、背压和应用命令分发，不得拥有 Agent、
  Tool、Vault、Storage 或 `HarnessService` 实现。
- `local_process_host`、process supervisor 和 Windows process backend 只负责短生命周期进程基础设施，
  不得拥有 Agent、Session、Vault 或持久化实现。上层 Worker composition 负责把它们接入 Tool Kernel。

## 主分支闭包

- 主分支不得存在 `src/khoj`、旧服务端 Web、根 Khoj distribution/lock/test、Docker/Gunicorn
  启动器或旧插件 HTTP/sync 文件；它们只能从对照分支
  `archive/khoj-server-baseline-20260713` 与只读 snapshot 查看。
- `scripts/audit_repository_closure.py` 对正式路径、Python AST、动态 import、子进程命令、
  console entry point、`uv.lock`、Runtime SBOM 和 UI 边界做 fail-closed 检查。
- 迁移文档可以描述旧类型和命令；任何可执行 Python/TypeScript、package script、CI 或发行脚本
  都不能 import、动态加载、启动或打包这些历史入口。

## 状态与副作用

- 状态、事件、预算、审批与 invocation journal 通过同一个 Unit of Work 原子提交。
- Artifact blob 先以内容寻址方式幂等 stage，随后在 Unit of Work 中提交 metadata/link；未链接 blob 由 GC 清理。
- Event Store 是 UI 恢复事实来源，EventSink 断线不能回滚已提交事件。
- 未声明安全属性的工具构造失败；不得根据工具名推断并发、幂等或风险。
