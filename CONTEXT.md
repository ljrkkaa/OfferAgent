# OfferAgent

OfferAgent 是服务于单个用户的面试学习助手，围绕一个 Obsidian Vault 维护真实、可追溯的学习状态。Vault 中的内容始终是学习事实的权威来源。

## Language

**Vault**:
承载面试材料、每日记录和学习进度的 Obsidian 项目，也是 OfferAgent 判断学习状态的权威来源。
_Avoid_: 知识库服务器、同步副本、数据仓库

**Study Evidence**:
能够证明实际学习已经发生的明确记录；计划、候选主题和未勾选任务不属于 Study Evidence。
_Avoid_: 学习计划、可能完成、推测进度

**Learning State**:
面经或面试题在真实学习过程中的当前阶段，只能依据 Study Evidence 保守推进。
_Avoid_: 文件存在状态、计划状态、模型推断状态

**Vault Action**:
OfferAgent 根据明确来源提出、但尚未应用到 Vault 的内容变更。
_Avoid_: 自动写入、模型已经修改、后台静默修改

**Vault Change Batch**:
一个 Agent Run 为完成同一逻辑任务而提出的一组 Vault Action；用户只能全部应用或全部拒绝。应用前任一源文件版本不再匹配时，整个批次失效。
_Avoid_: 可部分勾选的修改列表、后台任务队列、多个无关任务的合并

**Vault Permission Mode**:
Obsidian 插件为单个 Vault 保存和执行的写入授权策略。默认模式为 Trusted Vault；Agent 和 Runtime 都不能自行扩大或修改该策略。
_Avoid_: 模型自行授权、Runtime 绕过插件、全文件系统权限

**Git Checkpoint**:
Vault Change Batch 应用前，由恢复模块使用临时 Git index 为本批目标文件创建的隐藏恢复引用；它不切换分支、不修改现有暂存区，也不等同于用户提交。
_Avoid_: 自动提交当前分支、`git add .`、完整 Vault 备份

**Study-Maintenance Agent**:
检查 Study Evidence、提出 Learning State 变化并推荐下一学习主题的 OfferAgent 角色；它不把计划或已有材料当成完成证据。
_Avoid_: 内容生成器、自动完成器

## Conversations and Runs

**Conversation**:
用户与 OfferAgent 之间可跨多次启动持续存在的交互历史，其中可以包含多个 Agent Run。
_Avoid_: 单次请求、单次模型调用、Agent Run

**Agent Run**:
OfferAgent 为处理一条用户请求而进行的一次执行，直到完成、失败、取消或中断。
_Avoid_: Conversation、模型请求、聊天消息

**Interrupted Run**:
在完成前因插件或 Runtime 断开而停止、但仍保留可恢复进度的 Agent Run。
_Avoid_: 失败运行、新运行、重新提问

**Run Checkpoint**:
Agent Run 中已经完整提交的恢复边界；恢复时不得重复已经提交的副作用。
_Avoid_: 部分流式文本、临时 UI 状态、自动重跑

**Runtime State**:
为保存 Conversation、Agent Run 和恢复进度而产生的本地内部状态；它不构成学习事实，也不能用于推进 Learning State。
_Avoid_: Study Evidence、Vault 内容、学习记录

**Evidence Snapshot**:
Agent Run 实际使用过的有限 Vault 文本片段及其来源标识；源文件发生变化后，它不能继续作为当前证据。
_Avoid_: 完整文件副本、Vault 缓存、永久知识副本

**Agent Sidebar**:
Obsidian 右侧栏中承载 Conversation、Agent Run 状态和用户输入的唯一首版工作区；辅助信息只在当前操作需要时出现。
_Avoid_: 任务中心、仪表盘、多 Agent 控制台、常驻调试面板

## Agent Instructions

**Agent Contract**:
Vault 根目录的 `agent.md`，定义 OfferAgent 在该 Vault 中必须遵守的长期行为、证据规则和写入约束。它高于 Local Skill 和模型默认行为。
_Avoid_: 普通笔记、Codex 系统提示、可选参考资料

**Local Skill**:
Vault 内 `.codex/skills/*/SKILL.md` 描述的按需工作流。Local Skill 是单个 OfferAgent 可读取的指令包，不是子 Agent，也不能授予未开放的工具权限。
_Avoid_: 子 Agent、插件命令、额外权限

**Vault Tool Adapter**:
Obsidian 插件中通过官方 Obsidian TypeScript API 实现 Vault 工具协议的模块。它执行 `vault_search`、`vault_read` 和已授权的 Vault Change Batch，不通过 Shell 或 Obsidian CLI 间接访问 Vault。
_Avoid_: CLI 包装器、Runtime 直接文件访问、模型执行 Shell

**Provider Capability**:
模型 Provider 在当前认证方式和后端上经过实际探测后可用的托管能力，例如 Codex Hosted Web Search。未知或探测失败的能力不得被 Agent 当成已提供的工具。
_Avoid_: 根据模型名称猜测能力、把内部后端当成公开稳定协议

## Migration Requirement

在新的 TypeScript Agent 接管目标 Vault 前，必须重新审查并修改目标 Vault 根目录的 `agent.md`，使其中的角色名称、工具名称、运行边界、确认流程和恢复语义与新 Agent 一致。旧 `agent.md` 不得未经适配直接作为新 Agent 的正式 Agent Contract。

目标 Vault 的 `obsidian-cli` Skill 也必须改写为 OfferAgent Vault 工具说明，不能继续要求 Agent 执行本机不存在且未授权的 `obsidian ...` Shell 命令。`skill_read` 必须支持读取对应 Skill 目录内被 `SKILL.md` 直接引用的资源文件，同时拒绝目录越界。
