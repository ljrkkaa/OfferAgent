---
status: accepted
---

# Persist bounded evidence snapshots instead of complete Vault files

为了恢复 Agent Run，Runtime 只持久化模型实际使用过的有限 Evidence Snapshot，并同时记录文件路径、行范围和 checksum；它不得把完整文件或未读取的 Vault 内容复制到 SQLite。恢复时如果源文件 checksum 已变化，相关 Run Checkpoint 必须失效并重新规划，不能静默使用旧证据。

## Consequences

- 每个工具结果和每个 Agent Run 都必须有明确的证据大小上限。
- 文件搜索结果应先返回定位信息，只有后续明确读取的片段才形成 Evidence Snapshot。
- 重新规划可以复用仍然有效的步骤，但不能复用来自已变化来源的推理结论。
- 删除 Conversation 时必须删除相关 Evidence Snapshot。
