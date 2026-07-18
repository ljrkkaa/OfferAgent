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
Obsidian 插件通过专用 `daily_note.context` 工具为指定日期解析 Daily Notes 配置；未指定日期时按插件所在本地时区确定“今天”。它返回解析后的日期、目标路径、文件是否存在及其版本、模板路径与模板正文及其版本、日期格式。它不创建文件、不选择学习内容，也不向普通 Vault 读取开放 `.obsidian/**`。
_Avoid_: agent.md 硬编码模板路径、vault_read 读取 Obsidian 配置、插件替模型规划内容

**Interview Experience**:
OfferAgent 从用户分享或自主检索的内容中整理出的结构化面经笔记；它保存可学习的面试上下文与问题，不把原始图片、网页正文或聊天原文作为独立知识层重复归档。每次入库都在同一个 Vault Change Batch 中同步相关 Interview Question。
_Avoid_: 原始素材库、网页镜像、聊天记录副本

**Interview Submission**:
用户在一条消息中提供的文本、URL 和有序 Run Attachment 集合；多张图片默认共同描述一篇 Interview Experience，Agent 应综合全部内容后再提取，只有用户明确说明时才拆成多篇。
_Avoid_: 每张截图一篇面经、忽略附件顺序、未征得用户意图自动拆分

**Interview Experience Identity**:
一篇 Interview Experience 所代表的单次来源事件；相同 URL、相同截图内容或明显搬运内容属于同一事件并合并，来自不同求职者、时间或轮次的经历仍是不同事件，即使部分问题重合。
_Avoid_: 按公司岗位合并全部面经、搬运内容重复建档、按题目重合误删不同经历

**Source Metadata**:
Interview Experience 中用于最低限度追溯的信息，例如来源类型、可用时的来源 URL、采集日期、公司、岗位和面试轮次；它不包含原始素材正文。
_Avoid_: 完整来源存档、无来源标记

**Interview Question**:
从一个或多个 Interview Experience 中提取并合并的可复用学习单元；语义重复的问题更新既有条目及其出现上下文和出现频率，只有尚不存在的问题才创建新条目。新题可以先入库，再依据 Answer State 独立完成研究与答案建设。
_Avoid_: 每篇面经复制一套题目、同义问题重复建档、与面经入库分离的手工同步

**Answer State**:
Interview Question 的答案成熟度，依次为 `needs-research`、`draft` 和 `verified`；它描述答案是否经过研究与验证，不表示用户是否已经学习或掌握该题。
_Avoid_: Learning State、用文件存在表示答案成熟、把即时生成内容标为已验证

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

**Study Planning Priority**:
Daily Study Plan 选择学习内容与研究方向时采用的语义优先级：当前明确的公司、岗位、面试日期或目标优先，其次参考最近六个月匹配面经中的高频问题、答案或学习尚未成熟的题目、与用户项目简历高度相关的方向，并避免近期计划的无意义重复。
_Avoid_: 固定分数公式、忽略当前用户目标、只按热度排序、把计划优先级当成完成证据

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
新 Agent Run 可见的同一 Conversation 中有限、按序的用户与 Agent 消息及其 Conversation Attachment；它不继承旧工具结果或 Evidence Snapshot，接近模型上限时从最旧的完整对话轮次开始裁剪。
_Avoid_: 完整运行日志、旧证据缓存、单轮输入

**Agent Run**:
用户为一个目标明确启动、由 OfferAgent 自主选择和调度可用工具的一次执行，直到完成、失败、取消或中断；首版不在用户未启动目标时创建后台研究运行。
_Avoid_: Conversation、模型请求、聊天消息、无人触发的后台任务、固定 Workflow 实例

**Stopped Run**:
用户在完成前主动停止的 Agent Run；已经显示的部分回答可以留在 Conversation 中供参考，但不进入 Conversation Context，也不构成可恢复进度。用户可以取回原提示词、修改后启动新的 Agent Run。
_Avoid_: Paused Run、Interrupted Run、继续同一次运行

**Interrupted Run**:
在完成前因插件或 Runtime 断开而停止、但仍保留可恢复进度的 Agent Run。
_Avoid_: 失败运行、新运行、重新提问

**Run Checkpoint**:
Agent Run 中已经完整提交的恢复边界；恢复时不得重复已经提交的副作用。
_Avoid_: 部分流式文本、临时 UI 状态、自动重跑

**Runtime State**:
为保存 Conversation、Agent Run 和恢复进度而产生的本地内部状态；它不构成学习事实，也不能用于推进 Learning State。
_Avoid_: Study Evidence、Vault 内容、学习记录

**Legacy State Migration**:
将旧 TypeScript Runtime 的只读 schema-v19 快照转换为 Python Harness 权威状态的一次性离线操作；dry-run 与执行共享同一个封闭计划，只导入完成的 Conversation Turn、已保留的有序附件和映射后的插件权限设置，并以 source hash receipt、只读备份、staging 与可回滚 authority switch 保证幂等。活动或中断 Run、工具检查点、Vault Change Batch、凭据、Token、Secret 与未知设置只进入不含值的排除清单。迁移同时安装与 Python 工具语言一致的 `agent.md` 和兼容名 `obsidian-cli` Skill；自定义控制文件冲突时拒绝覆盖。
_Avoid_: 机械数据库复制、恢复旧 Run、重放旧副作用、导入秘密、静默覆盖自定义 Contract

**Python Harness**:
由 Obsidian 插件为当前 Vault 启动的唯一隐藏 Python Worker；它拥有 Agent Loop、Tool Kernel、模型、Run Event、Conversation、恢复和 Runtime State，但不拥有 Vault API 能力。
_Avoid_: TypeScript Agent Runtime、常驻 Host、第二个 Agent Loop、Vault 文件系统权威

**Plugin Tool Invocation**:
Python Harness 先持久化 `executorLocation=plugin` 的 `tool.started` Event，Vault Tool Adapter 再执行并通过同一 stdio 连接回传严格绑定结果的一次工具调用；重复回执只在绑定与结果完全一致时可安全重放。
_Avoid_: 反向 RPC、Runtime 直接 Vault I/O、无绑定工具结果、断线后盲目重试写入

**Evidence Snapshot**:
Agent Run 实际使用过的有限 Vault 文本片段及其来源标识；源文件发生变化后，它不能继续作为当前证据。
_Avoid_: 完整文件副本、Vault 缓存、永久知识副本

**Pinned Context**:
用户为下一次 Agent Run 明确指定、希望 Agent 优先考虑的 Vault 文档；它不限制 Agent 搜索其他文档，也只有在实际读取后才形成 Evidence Snapshot。
_Avoid_: 上下文白名单、仅限所选文档、Evidence Snapshot

**Project Evidence**:
Agent 通过 `projects/index.md` 明确登记的项目描述文件，从其外部项目根中的文档、文本源码和配置里精确读取的有界证据，用于核对项目事实并生成特化面试回答；它不包含密钥、依赖、构建产物或版本控制内部文件，也不授权执行或修改项目代码。
_Avoid_: README 推测、完整项目副本、Shell 输出、代码执行结果、用户已掌握的证明

**Project Interview Training**:
基于 Project Evidence 的交互式模拟面试；Agent 一次提出一道与目标岗位相关的问题，在用户回答后继续真实性与技术深挖、给出反馈和改进建议，训练结束后才沉淀经确认的项目特化内容。
_Avoid_: 直接代写答案、一次展示整套问题、脱离项目事实的通用问答、未训练先标记掌握

**Project Answer**:
Interview Question 针对一个具体个人项目形成的回答版本，包含用户实际职责、实现依据、取舍、指标、失败案例和追问准备；它与通用标准答案分开保存并关联对应 Project Evidence。
_Avoid_: 覆盖通用标准答案、把模型推测写成个人经历、无项目证据的包装

**Project Interview Profile**:
一个项目的特化面试知识集合；它以独立目录组织稳定项目事实、训练索引和按问题拆分的 Project Answer，只为实际训练过的问题创建答案内容。
_Avoid_: 单个巨型项目问答文件、未训练问题的空文件、把项目源码复制进回答目录

**Project Ownership**:
纳入 Project Interview Profile 的个人项目默认由用户独立完成，Agent 可以依据 Project Evidence 使用第一人称描述其设计与实现，不需要逐题确认个人贡献；未注册的外部项目副本或临时实验不自动获得该归属。
_Avoid_: 每道题重复确认作者身份、把任意 projects 目录内容都包装成个人成果、虚构源码中不存在的实现

**Project Registry**:
`projects/index.md` 中列出的当前项目，是可以建立 Project Interview Profile 并按 Project Ownership 生成第一人称回答的个人面试项目集合；其他项目目录只能作为研究参考，除非通过正常 Vault Change Batch 加入注册表。
_Avoid_: 扫描目录即认定个人项目、隐藏项目名单、Agent 绕过变更批次自行授权

**Training Outcome**:
Project Interview Training 结束后写入 Vault 的精炼成果，包括用户确认的 Project Answer、Project Evidence 引用、薄弱点、可能追问和下一次复训题目；完整逐轮问答只保留在 Conversation，不复制进知识库。
_Avoid_: 完整训练转录、模型未确认的临场回答、只保存评分不保存改进内容

**Training Feedback**:
Agent 对用户回答给出的维度化改进意见，覆盖项目事实、个人职责、技术取舍、指标结果、追问准备和表达时长；每个维度只说明已覆盖或需要补充，并提供具体证据与改进方向，不计算数字总分。
_Avoid_: 伪精确总分、脱离 Project Evidence 的主观评价、只判好坏不给改法

**Training Question Selection**:
Project Interview Training 的逐题选择规则；用户可以明确点题，否则 Agent 根据当前目标公司与岗位、最近匹配面经中的高频问题、项目关键设计与风险、既有 Training Feedback 和复训项自主选择下一题，并降低已成熟且无新证据题目的优先级。
_Avoid_: 一次展示整套题、固定题序、忽略用户点题、无意义重复训练

**Conversation Attachment**:
用户在一条 Conversation 消息中提供的图片等非文本输入；它在 Vault 外随 Conversation 保留，可在后续 Agent Run 中再次引用，并在 Conversation 删除时一并删除。归档 Conversation 不会删除附件，容量不足时必须提示用户处理而不是静默清理。
_Avoid_: Vault 附件、知识库原始素材、运行结束即删除的临时文件、Evidence Snapshot

**Run Attachment**:
当前 Agent Run 按顺序引用的一组 Conversation Attachment；它可以来自本轮新消息或同一 Conversation 的较早消息，不复制底层附件，也不进入 Vault 或 Interview Experience 正文。
_Avoid_: Conversation Attachment 副本、知识库原始素材、Planning Memory

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
Obsidian 插件中通过官方 Obsidian TypeScript API 实现 Vault 工具协议的模块。它执行 `vault.search`、`vault.read`、受 Registry 约束的 `project.*` 和已授权的 Vault Change Batch，不通过 Shell 或 Obsidian CLI 间接访问 Vault。
_Avoid_: CLI 包装器、Runtime 直接文件访问、模型执行 Shell

**Provider Capability**:
模型 Provider 在当前认证方式和后端上经过实际探测后可用的托管能力，例如 Codex Hosted Web Search。未知或探测失败的能力不得被 Agent 当成已提供的工具。
_Avoid_: 根据模型名称猜测能力、把内部后端当成公开稳定协议

**Vision Capability**:
当前账户绑定的 Provider 模型目录为所选模型声明的原生图片理解能力；它只适用于该目录修订中的精确模型选择，并允许读取用户明确提供的 Run Attachment。目录未知或未声明图片输入时不得推断支持，也不依赖本地 OCR。
_Avoid_: Synthetic Vision Probe、Vision Cache、OCR Pipeline、按模型名称假定支持、把附件永久上传为知识库素材

**Research Browser**:
OfferAgent 为用户发起的研究任务使用的隔离登录浏览器；它拥有独立于日常 Chrome 的 Profile，可根据研究目标自主跨站搜索、跳转、翻页和读取，但不发布内容或进行社交互动。
_Avoid_: 用户日常 Chrome 会话、通用电脑控制、后台爬虫、发帖点赞私信

**Interview Research Scope**:
用户请求限定的公司、岗位或技术方向、时间范围和期望数量；未明确时间时默认最近六个月，并在范围内选择岗位相关且信息具体的少量 Interview Experience。Agent 不得为凑数量自行放宽岗位或时间范围，合格结果不足时应明确报告。
_Avoid_: 自动扩大岗位、自动纳入更早面经、固定抓取篇数、用低质量结果填满配额

**Interview Research Ranking**:
Agent 根据当前用户需求对 Interview Research Scope 内的候选结果进行语义排序，优先公司与岗位更匹配、时间更新且问题更具体的内容；它不生成伪精确的可信度等级，并在入库前排除已经保存的重复 Interview Experience。
_Avoid_: high/medium/low 可信度打分、固定站点权重、忽略用户需求的热度排序、重复入库

## Migration Requirement

在新的 Python Harness Agent Loop 接管目标 Vault 前，必须重新审查并修改目标 Vault 根目录的 `agent.md`，使其中的角色名称、工具名称、运行边界、确认流程和恢复语义与新 Agent 一致。旧 `agent.md` 不得未经适配直接作为新 Agent 的正式 Agent Contract。

目标 Vault 当前 `agent.md` 及仓库内对应部署模板必须从单一“学习状态维护”契约改为按用户意图工作的 OfferAgent 契约：创建和改写是覆盖整个 Vault 普通 `.md`、`.txt` 文件的通用能力。用户要求创建内容时，应在确认目标缺失后直接创建；用户要求整体或局部改写时，应先读取当前版本再执行。用户未要求重写时仍保留无关内容。不得用“daily 缺失”“默认文件范围”或固定状态检查输出阻止明确的创建、填充或重写请求；控制文件确认、插件权限、版本校验和 Git Checkpoint 仍保持不变。Daily Study Plan 与 Study-State Synchronization 继续按各自证据边界独立工作。

目标 Vault 的 `obsidian-cli` Skill 也必须改写为 OfferAgent Vault 工具说明，不能继续要求 Agent 执行本机不存在且未授权的 `obsidian ...` Shell 命令。`skill.read` 必须支持读取对应 Skill 目录内被 `SKILL.md` 直接引用的资源文件，同时拒绝目录越界。
