---
status: accepted
---

# Expose bounded read-only project source tools

OfferAgent 通过 Obsidian 插件提供 `project_list`、`project_search` 和 `project_read`，使 Agent 能从 `projects/` 下的项目文档、文本源码和配置中取得 Project Evidence，而不是仅依据 README 推测项目实现。工具沿用 Vault 路径约束、有界输出、版本与 Hash、Evidence Snapshot 和恢复失效规则，但排除 `.git/**`、`.env*`、密钥、依赖目录、构建产物和二进制文件；它们不允许写入项目源码、执行代码或调用 Shell。这个受限入口扩展 ADR 0012 的 `.md`/`.txt` 普通笔记读取范围，但不改变插件拥有 Vault 能力、Runtime 无直接文件权限的架构。
