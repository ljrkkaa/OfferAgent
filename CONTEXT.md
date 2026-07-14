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

**Daily Study Plan**:
OfferAgent 根据用户的学习安排意图，使用 Vault 已配置的 Daily Notes 模板创建并填充 `daily/YYYY-MM-DD.md`；生成前召回相关 Study Memory 与 Project Memory，并读取历史 daily、项目库和待学习队列。目标缺失时实例化模板并创建；目标已存在时保留 frontmatter、已有记录和勾选状态，优先填充空占位或按用户意图追加；只有用户明确要求重新安排或重写时才替换已有计划。若本次计划形成跨天学习主线、主题顺序或暂缓方向，则在同一 Vault Change Batch 中更新 Study Memory；不把当天完整清单复制进记忆，且计划本身不是 Study Evidence。
_Avoid_: 学习日记、关键词路由、Study-State Synchronization

**Daily Note Context**:
Obsidian 插件通过专用 `daily_note_context` 工具为指定日期解析 Daily Notes 配置；未指定日期时按插件所在本地时区确定“今天”。它返回解析后的日期、目标路径、文件是否存在及其版本、模板路径与模板正文及其版本、日期格式。它不创建文件、不选择学习内容，也不向普通 Vault 读取开放 `.obsidian/**`。
_Avoid_: agent.md 硬编码模板路径、vault_read 读取 Obsidian 配置、插件替模型规划内容

**Planning Memory**:
Vault 中用户可见、可编辑并跨 Conversation 保留的 User Memory、Feedback Memory、Project Memory 和 Study Memory；它默认启用并允许 Agent 直接写入，不提供独立的启用开关或权限配置。它存放在 Vault 根目录 `memory/`：`MEMORY.md` 只保存简短索引，实际记忆按语义主题分别存放在 `user/`、`feedback/`、`project/` 和 `study/` 中，并由 Agent 根据当前请求按需读取。Daily Study Plan 可以使用它，但它不是 Study Evidence 或 Runtime State。
_Avoid_: 隐藏模型记忆、大类汇总文件、按时间堆积、可选功能开关、独立权限配置、Conversation Context、运行日志

**Memory Topic**:
Planning Memory 中可独立召回和更新的 Markdown 文件；它使用 `name`、`description` 和 `type` 元数据说明语义，并在 `MEMORY.md` 中保留一条简短链接。一个主题可以容纳多条彼此相关的事实，但不按消息逐条建文件。
_Avoid_: 一条消息一个文件、完整对话副本、MEMORY.md 内嵌正文

**Memory Capture**:
Planning Memory 的双通道写入机制：主 Agent 在明确请求记忆或语义上识别到长期有效信息时立即写入；若本轮没有发生记忆写入，则在回答结束后对本轮新增用户与 Agent 消息执行一次受限的语义提取。兜底提取只能读写 `memory/**`，成功后向用户显示实际更新的 Memory Topic。
_Avoid_: 关键词触发、两条通道重复写入、扫描完整历史、后台修改非 memory 文件

**Memory Recall**:
每个 Agent Run 根据当前请求与 Conversation Context 扫描 Memory Topic 的 `name`、`description` 和 `type`，由模型语义选择最多五个相关主题，并把选中正文加入本轮上下文；用户明确要求检查、回忆或使用记忆时必须执行召回。`MEMORY.md` 只用于用户浏览和记忆整理，不常驻模型上下文。v1 不计算记忆年龄、不设置过期阈值，也不显示新鲜度提醒。
_Avoid_: 关键词匹配、无条件加载全部记忆、MEMORY.md 常驻 Prompt、重复注入同一主题、新鲜度评分

**Memory Consolidation**:
Memory Capture 写入前对相关 Memory Topic 做语义合并：更新已有主题、删除被纠正或不再成立的内容、消除重复，并同步 `MEMORY.md` 索引。记忆文件只表达当前有效理解，变更历史由 Vault Change Batch 的 Git 检查点与撤销机制承担。
_Avoid_: 追加式事件流水、保留失效正文、为同一事实重复建主题

**User Memory**:
用户稳定的身份、知识背景和长期偏好；它不包含一次性任务要求。
_Avoid_: 临时提示、当前消息、Feedback Memory

**Feedback Memory**:
用户对 OfferAgent 行为、表达或工作流的持久纠正；它描述未来应该如何协作，而不是当前任务要做什么。v1 不把 Feedback Memory 晋升或建议晋升到 Agent Contract。
_Avoid_: 普通补充信息、一次性修改、User Memory、自动修改 agent.md

**Project Memory**:
单个项目的目标、进展、关键决策、约束和截止日期；它引用项目权威来源而不复制项目内容或运行日志。
_Avoid_: Study Memory、源码副本、项目日志

**Study Memory**:
跨 Daily Study Plan 延续的当前学习主线、暂缓方向、主题顺序和安排偏好；已完成学习事实仍由 Study Evidence 决定。
_Avoid_: Project Memory、Study Evidence、完成状态

**Memory Precedence**:
记忆参与决策时遵循 Agent Contract > 当前用户明确要求 > Feedback Memory > 与任务相关的 User Memory、Project Memory 或 Study Memory > 模型默认行为；同层冲突采用更具体且更新的内容。当前请求可以覆盖本次行为，但除非表达了持久纠正或稳定事实，否则不改写 Planning Memory。
_Avoid_: 旧记忆覆盖当前要求、单次要求自动永久化、所有记忆无条件拼接

**Study-State Synchronization**:
OfferAgent 从已有 daily 中的 Study Evidence 保守推进 Learning State 并推荐下一学习主题；daily 缺失时结束而不创建日记。
_Avoid_: Daily Study Plan、计划生成、推测完成

## Conversations and Runs

**Conversation**:
用户与 OfferAgent 之间可跨多次启动持续存在的交互历史，其中可以包含多个 Agent Run。
_Avoid_: 单次请求、单次模型调用、Agent Run

**Conversation Context**:
新 Agent Run 可见的同一 Conversation 中有限、按序的用户与 Agent 消息；它不继承旧工具结果或 Evidence Snapshot，接近模型上限时从最旧的完整对话轮次开始裁剪。
_Avoid_: 完整运行日志、旧证据缓存、单轮输入

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

目标 Vault 当前 `agent.md` 及仓库内对应部署模板必须从单一“学习状态维护”契约改为按用户意图工作的 OfferAgent 契约：创建和改写是覆盖整个 Vault 普通 `.md`、`.txt` 文件的通用能力。用户要求创建内容时，应在确认目标缺失后直接创建；用户要求整体或局部改写时，应先读取当前版本再执行。用户未要求重写时仍保留无关内容。不得用“daily 缺失”“默认文件范围”或固定状态检查输出阻止明确的创建、填充或重写请求；控制文件确认、插件权限、版本校验和 Git Checkpoint 仍保持不变。Daily Study Plan 与 Study-State Synchronization 继续按各自证据边界独立工作。

目标 Vault 的 `obsidian-cli` Skill 也必须改写为 OfferAgent Vault 工具说明，不能继续要求 Agent 执行本机不存在且未授权的 `obsidian ...` Shell 命令。`skill_read` 必须支持读取对应 Skill 目录内被 `SKILL.md` 直接引用的资源文件，同时拒绝目录越界。
