# ADR-0004：SQLite Unit of Work 是权威本地状态边界

状态：Accepted

## 决策

Session、Turn、Run、Event、预算、审批、租约和 invocation journal 由同一 SQLite Unit of Work 原子提交。Artifact 内容先幂等 stage，事务中只提交 metadata 与 link。

## 后果

插件与本地 Web 不拥有数据库；它们通过同一 Worker 重放 Event。数据库成功而客户端断线由 outbox/replay 解决，不能回滚已发生副作用或伪造完成事件。

