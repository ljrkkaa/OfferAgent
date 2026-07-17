---
status: accepted
---

# Expose bounded read-only project source tools

OfferAgent 通过 Obsidian 插件提供 `project.list`、`project.search` 和 `project.read`，使 Agent 只能从 `projects/index.md` 直接链接、且描述文件中 `project-id` 与绝对 `project-root` 都有效的项目取得 Project Evidence，而不是仅依据 README 推测项目实现。工具沿用有界输出、版本与 Hash、Evidence Snapshot 和恢复失效规则，并以 canonical root containment 拒绝目录逃逸和别名；同时排除 `.git/**`、`.env*`、密钥、依赖目录、构建产物和二进制文件。它们不允许写入项目源码、执行代码或调用 Shell。这个受限入口扩展 ADR 0012 的 Vault `.md`/`.txt` 普通笔记读取范围，但不改变插件拥有证据适配能力、Runtime 无直接项目文件权限的架构。
