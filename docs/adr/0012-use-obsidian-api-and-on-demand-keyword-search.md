---
status: accepted
---

# Use Obsidian API and on-demand keyword search

OfferAgent 不依赖 Obsidian CLI。Vault Tool Adapter 在 Obsidian 插件进程中通过官方 TypeScript API 实现文件枚举、读取、元数据访问和写入；Runtime 只能通过版本化工具协议请求这些能力，不能直接访问 Vault 文件系统或执行 Shell。

首版 `vault_search` 仅提供关键词和完整短语检索，不提供 Embedding、向量数据库或语义检索。目标 Vault 当前文本规模约为 418 个 Markdown/TXT 文件、7.56 MB，因此每次搜索可以在插件内枚举候选 Markdown 文件，使用 `Vault.cachedRead()` 读取正文，并结合 `MetadataCache` 的标题、Frontmatter、Tags 和链接信息进行排序。

## Search result contract

- 文件名和路径命中权重最高，其次为标题、Frontmatter 和 Tags，正文命中权重较低。
- 默认返回 10 个文件，最多 20 个；每个文件最多返回 3 个带行号的有限上下文片段。
- 搜索结果用于定位候选内容；Agent 必须使用 `vault_read` 读取确切行后，才能把内容作为正式 Evidence Snapshot。
- 返回结果包含文件路径、修改时间和内容 Hash；不得把全部候选文件正文发送给 Runtime。
- 不持久化正文索引。Obsidian 关闭后不保留搜索缓存，下次搜索直接使用当前 Vault 内容。
- 普通搜索默认排除 `.git/**`、`.obsidian/**`、`.codex/**`、`node_modules/**` 和其他隐藏控制目录；根 `agent.md` 与 Local Skill 分别通过专用加载流程读取。

## Skill consequences

- `obsidian-markdown`、`obsidian-bases` 和 `json-canvas` 继续作为格式和验证指令使用，不产生额外 Agent 或权限。
- 当前 `obsidian-cli` Skill 必须改写为 OfferAgent 工具语义，生产 Agent 不执行其中的 CLI 命令。
- `defuddle` 的网页读取职责由 Runtime 的 `web_read` 工具承担，不作为 Vault 工具或 Shell 命令执行。
- `skill_read` 接受受限的相对资源路径，以便读取 Skill 直接引用的 `references/*` 文件，但必须确保解析后的路径仍位于对应 Skill 目录中。
