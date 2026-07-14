# OfferAgent: Interview Knowledge Intake, Research, Planning, and Project Training

Status: ready-for-agent specification.

## Problem Statement

OfferAgent 已经能够由一个 Agent 自主调用工具来创建每日计划、读取知识库和提议变更，但用户的完整面试准备过程仍然被割裂在聊天、截图、网页和项目源码之间。用户分享的面经图片不能直接理解和入库；寻找目标公司的近期面经需要手工浏览和筛选；学习计划不能系统利用近期高频题、答案缺口和项目相关性；项目面试训练也无法根据真实 Project Evidence 进行追问和纠偏。

用户需要的是一个持续工作的面试准备 Agent：用户只负责提出目标或提供材料，Agent 自己选择合适工具、判断是否重复、整理知识、安排学习与研究，并围绕个人项目进行逐题训练。它不能退化为需要用户逐步填写表单的 Workflow，也不能在用户没有启动目标时自行扩张研究范围或执行后台任务。

## Solution

扩展现有单 Agent 工具循环，使其能够接收由文本、URL 和多张图片组成的 Interview Submission，并使用 Codex Vision Capability 直接理解图片。Agent 在写入前通过 Interview Catalog 查找相似 Interview Experience 与 Interview Question，自主完成语义判断，然后用一个原子 Vault Change Batch 创建或更新面经、题目和索引。知识库只保留结构化结果、最小 Source Metadata 和单向 Source Fingerprint，不保存原始截图、网页正文或聊天原文。

当用户要求寻找面经时，Agent 优先使用现有公开网页能力，并在动态页面或登录态必需时使用隔离、只读的 Research Browser。默认检索最近六个月、岗位最相关的少量结果，按匹配程度排序，且不重复导入已有面经。Agent 进一步结合用户目标、近期题目频率、Answer State、Learning State、项目相关性和近期计划重复度生成学习与研究安排。

对于 Project Interview Training，Agent 只读取 Project Registry 中登记的个人项目，通过受限的 Project Evidence 工具读取必要源码和文档，每次提出一个问题，根据用户回答与真实证据继续追问，给出分维度但不量化总分的反馈，并只在用户确认后把精炼 Training Outcome 写入知识库。

确定性 Module 负责附件、权限、候选发现、版本验证、数据约束和恢复；意图理解、工具调度、语义去重、资料选择、题目选择和回答生成继续由主 Agent 负责。

## User Stories

1. 作为准备面试的用户，我想把文字面经直接发给 Agent，以便无需手工整理就能进入知识库。
2. 作为准备面试的用户，我想把一个面经网址发给 Agent，以便 Agent 能读取、总结并结构化保存其有效内容。
3. 作为准备面试的用户，我想粘贴、拖入或选择面经截图，以便 Agent 能直接理解图片而不要求我转写文字。
4. 作为准备面试的用户，我想一次提交多张有顺序的截图，以便长面经可以作为同一份 Interview Experience 被理解。
5. 作为准备面试的用户，我想明确说明一次提交包含多份面经，以便 Agent 能按语义将它们分开处理。
6. 作为准备面试的用户，我想在发送前查看、移除和调整图片顺序，以便输入内容准确完整。
7. 作为准备面试的用户，我想在图片不受支持或过大时保留草稿，以便修正附件后无需重新输入消息。
8. 作为注重隐私的用户，我希望原始图片只短暂存在于本机运行附件区，以便知识库不会积累不需要的原始材料。
9. 作为注重隐私的用户，我希望图片、网页正文和聊天原文不会被复制进知识库，以便只保留对学习有用的结构化结果。
10. 作为准备面试的用户，我希望 Agent 能从面经中识别公司、岗位、轮次、时间和问题，以便生成可检索的 Interview Experience。
11. 作为准备面试的用户，我希望未知元数据保持为空而不是被猜测，以便知识库中的事实可信。
12. 作为准备面试的用户，我希望同一个网址、截图或转载内容不会生成重复面经，以便统计和学习计划不被污染。
13. 作为准备面试的用户，我希望不同候选人、日期或轮次的经历仍可分别保存，以便保留真实差异。
14. 作为准备面试的用户，我希望相同底层问题的不同表述合并到同一个 Interview Question，以便形成统一知识条目。
15. 作为准备面试的用户，我希望同一问题在不同面经中的出现记录被累积，以便看到近期频率和出现上下文。
16. 作为准备面试的用户，我希望重复提交不会再次增加问题频率，以便统计结果准确。
17. 作为准备面试的用户，我希望新题可以先以 needs-research 保存，以便导入面经无需等待所有答案研究完成。
18. 作为准备面试的用户，我希望 Answer State 与 Learning State 相互独立，以便资料成熟度不会被误当成我的学习进度。
19. 作为准备面试的用户，我希望一份面经及其题目、频率和索引要么一起写入、要么全部不写，以便不会留下半完成状态。
20. 作为准备面试的用户，我希望在写入前看到一个连贯的变更提议，以便权限模式仍能控制知识库修改。
21. 作为准备面试的用户，我希望可以撤销一次成功的面经导入，以便误操作能够安全恢复。
22. 作为准备面试的用户，我希望中断的图片 Run 可以恢复，以便临时关闭或异常不会迫使我重新上传材料。
23. 作为准备面试的用户，我希望 Agent 明确说明 Vision Capability 不可用，而不是忽略图片或假装读过，以便我能选择可用模型。
24. 作为求职用户，我想让 Agent 寻找某家公司和岗位的面经，以便减少手工检索时间。
25. 作为求职用户，我希望未指定时间时默认查看最近六个月，以便结果反映当前招聘重点。
26. 作为求职用户，我希望 Agent 优先岗位、公司和技术方向最匹配的结果，以便少量结果仍有较高价值。
27. 作为求职用户，我希望 Agent 只返回和整理最匹配的几条，而不是给每条打复杂可信度等级，以便快速进入准备阶段。
28. 作为求职用户，我希望 Agent 不会自行扩大岗位或时间范围，以便搜索仍符合我的需求。
29. 作为求职用户，我希望搜索结果在导入前与已有 Interview Experience 去重，以便网上研究不会制造重复内容。
30. 作为求职用户，我希望公开网页可直接读取，只有动态页面或登录态必需时才启用 Research Browser，以便检索高效且边界清楚。
31. 作为求职用户，我希望用独立浏览器配置手动完成登录和安全验证，以便不暴露日常 Chrome Profile。
32. 作为注重账号安全的用户，我希望 Research Browser 只能读取和导航，不能发帖、点赞、收藏、关注、评论或私信，以便 Agent 不产生社交写操作。
33. 作为求职用户，我希望登录失效或结果不足时 Agent 明确报告，以便它不会暗中放宽范围或编造材料。
34. 作为学习者，我希望学习计划优先考虑当前公司、岗位、面试日期和目标，以便每天的任务服务于最近目标。
35. 作为学习者，我希望近期匹配面经中的高频问题获得更高优先级，以便时间投入更符合面试概率。
36. 作为学习者，我希望计划区分“需要学习的现有内容”和“需要研究补全的答案”，以便采取不同动作。
37. 作为学习者，我希望计划考虑 needs-research、draft 和 verified 等 Answer State，以便优先补齐真正的知识缺口。
38. 作为学习者，我希望计划仍只把 Daily Note 中的明确完成证据当作 Study Evidence，以便不会虚报学习进度。
39. 作为学习者，我希望计划减少近期无意义重复，同时允许重要高频题复习，以便兼顾覆盖率和巩固。
40. 作为项目作者，我想在 Project Registry 中登记自己的项目，以便 Agent 能区分第一人称项目证据与普通研究材料。
41. 作为项目作者，我希望 Agent 只读项目源码、配置和文档，不执行或修改代码，以便训练不会影响项目。
42. 作为项目作者，我希望密钥、环境文件、依赖、构建产物和二进制文件默认排除，以便 Project Evidence 工具不会泄露敏感或无关内容。
43. 作为项目作者，我想指定一道项目面试题进行训练，以便针对当前薄弱点练习。
44. 作为项目作者，我希望未指定题目时 Agent 能根据目标岗位、近期面经、项目风险和历史反馈选题，以便训练无需手工编排。
45. 作为项目作者，我希望训练一次只问一个问题，并等待我回答后再追问，以便过程接近真实面试。
46. 作为项目作者，我希望追问和纠错引用真实 Project Evidence，以便回答不会脱离项目实现。
47. 作为项目作者，我希望反馈覆盖设计、取舍、指标、失败场景和实现细节，但不产生武断的总分，以便获得可行动建议。
48. 作为项目作者，我希望项目默认按个人独立完成来表达，以便不必反复回答协作分工问题。
49. 作为项目作者，我希望每个项目拥有独立的 Interview Profile 和按题拆分的 Project Answer，以便内容可维护且不会形成巨型文件。
50. 作为项目作者，我希望完整训练对话留在 Conversation，只保存经确认的精炼 Training Outcome，以便知识库保持简洁。
51. 作为 OfferAgent 用户，我希望始终由同一个 Agent 根据目标自主选择工具，以便体验是 agentic 的而不是固定 Workflow。
52. 作为 OfferAgent 用户，我希望只有我启动目标时 Agent 才执行研究、计划或训练，以便系统不会产生未经请求的后台行为。

## Implementation Decisions

- 保留 Runtime 所有的单 Agent 原生工具调用循环。不得新增关键词意图路由、固定 Workflow 状态机或子 Agent 调度层。
- Interview Submission 由有序文本和图片内容组成。多张图片默认属于一份 Interview Experience，除非用户明确说明需要拆分。
- 初始图片格式为 PNG、JPEG、WEBP 和非动画 GIF。单次提交最多 20 张、每张最多 10 MiB、总计最多 50 MiB；这些是产品验证限制，不是模型能力声明。
- 插件通过带认证的本机 HTTP 端点上传二进制附件，Agent Run 命令只引用不透明 Attachment ID。Conversation、工具事件和恢复事件继续使用可重放 WebSocket 通道。
- Run Attachment Module 负责签名和类型验证、原子暂存、所有权绑定、读取、终态清理、Conversation 清理及 TTL 清扫。原始字节只存在于本机临时附件区，不进入 Runtime State、Conversation 历史或事件日志。
- Interrupted Run 保留附件以支持 Resume；completed、failed、cancelled、Conversation 删除和超时孤儿清理均删除原始字节。Attachment ID 不能跨 Conversation 或 Agent Run 使用。
- Multimodal Input Module 在 Provider 调用前才把有效附件物化为内存中的 Responses API 图片输入，保持文本与图片顺序。Provider 不支持图片时必须返回可操作错误，不能静默忽略或回退到 OCR。
- Vision Capability 按 backend 和 model 保存为 unknown、available 或 unavailable。unknown 在首次图片 Run 上进行有界探测；不可用状态不能影响之后的纯文本 Run。
- 知识库不保存原始来源，只保留最小 Source Metadata、可选规范化 URL 和单向 Source Fingerprint。未知公司、岗位、轮次或日期不得推断。
- Experience Identity 由规范化 URL、Source Fingerprint、截图内容或明显转载正文支持。不同候选人、日期或轮次保持不同 Experience；重复 Experience 不创建新笔记，也不增加问题频率。
- Interview Catalog Module 是一个深、只读的本地接口，负责解析现有 Experience 和 Question 元数据、精确 URL/指纹查找、有界候选生成、索引版本及规范化路径建议。它不做语义决定，也不直接写入。
- 语义去重由 Agent 完成：Module 返回候选，Agent 阅读精确证据并决定是否为相同 Experience Identity 或相同底层 Interview Question。歧义时优先不合并。
- 一次面经导入使用一个 Vault Change Batch 同步 Experience、Question、出现上下文、频率和索引。验证失败、版本过期或动作失败时不得部分写入。
- 新 Interview Question 默认可进入 needs-research。Answer State 采用 needs-research、draft、verified，且独立于 Learning State。
- verified 表示答案已经根据当前可用的合适资料检查，不表示用户已经掌握。只有明确 Daily Note 完成证据才能推进学习状态。
- Hosted Web Search 与直接网页读取是公开页面的快速路径。Research Browser 只在动态渲染或独立登录态必要时使用。
- Research Browser 使用独立本机 Profile，登录和安全挑战由用户手动完成。Agent 只获得打开、读取、列出导航结果、跟随、翻页或滚动、返回等面向导航的能力。
- Research Browser 不提供任意脚本、任意表单提交、上传、下载或通用点击能力，并禁止任何社交写操作。网页内容始终视为不可信数据。
- Interview Research Scope 由用户请求决定；未指定时使用最近六个月。排序依次考虑公司、岗位、技术方向、时间和问题具体性，输出少量最匹配结果，不使用严格可信度等级。
- Agent 不得自主扩大岗位或时间范围。结果不足时报告不足，并在入库前使用同一 Interview Catalog 和原子导入路径去重。
- Daily Study Plan 的语义选择继续由 Agent 完成，输入优先级包括明确目标、近期匹配面经频率、Answer State、Learning State、项目相关性和近期计划重复度。
- Project Registry 是第一人称项目事实的权威入口。登记项目默认是用户独立完成；未登记目录只能作为研究材料，不能形成第一人称项目主张。
- Project Evidence Module 提供有界的项目列表、搜索和读取能力，只允许必要的 UTF-8 源码、配置与文档，排除版本库内部文件、依赖、虚拟环境、构建产物、缓存、二进制、密钥和常见秘密文件。
- Project Evidence 接口沿用 Evidence Snapshot 和过期失效语义。Runtime 不直接任意打开项目文件，系统不增加项目写入、Shell 或代码执行工具。
- Project Interview Training 每次只处理一个问题。用户可选题；否则 Agent 根据目标岗位、近期 Experience、项目设计风险、历史 Training Feedback 和待复训项自主选择。
- 训练先读取当前问题所需的最小 Project Evidence，再提问、等待回答、进行证据化追问、给出分维度反馈，并在用户确认后提议精炼 Project Answer。
- 每个登记项目拥有单独的 Project Interview Profile、索引和按题拆分的答案；只为实际训练过的问题创建答案，不创建空文件。
- 完整训练记录保留在 Conversation，知识库只保存已确认 Training Outcome、稳定项目事实、证据链接、薄弱点和待复训项。
- Read Only、Ask Every Time、Trusted Vault、Git Checkpoint、guarded undo、expected-version validation 和控制文件保护规则继续适用。
- 初始交付分四个阶段：首先打通图片或文本分享至原子知识入库；其次增加 Agent 主导的面经研究；然后增加 Answer State 驱动的研究和学习规划；最后增加 Project Evidence 与交互式训练。后续阶段复用第一阶段的 Interview Catalog 和入库路径，不建立第二套导入流程。

## Testing Decisions

- 测试只验证外部行为和 Module 合约，不断言内部辅助函数、搜索遍数或提示词排版。
- 最高层主测试接缝是完整 Agent Run：给定用户输入、Agent Contract、工具结果和权限状态，验证工具选择、证据读取、单一原子提议、最终响应和可恢复状态。
- Run Attachment Module 通过内存存储适配器测试类型验证、所有权、跨 Run 拒绝、终态清理、Conversation 清理、Interrupted Resume 和 TTL 清扫。
- Multimodal Input Module 通过 Fake Provider 测试有序文本、多图片、图片物化时机、能力探测、unsupported 状态和纯文本兼容性。
- Interview Catalog Module 通过内存 Vault 适配器测试精确 URL/指纹命中、转载候选、同公司不同 Experience、语义问题候选、损坏元数据、有界输出和索引版本。
- 面经导入通过确定性 Agent Run 测试一个完整纵向路径：多图片输入、Catalog 候选、精确读取、创建 Experience、合并旧 Question、创建 needs-research Question、更新索引和原子提交。
- 重复导入测试必须验证不创建 Experience、不增加频率，并可在有证据时只补全缺失 Source Metadata。
- 原子失败测试必须覆盖版本过期、动作数超限、任一写入失败、权限拒绝和 undo，确保不会留下部分文件。
- Research Browser Module 使用内存页面适配器测试导航、动态结果、翻页、登录暂停、范围保持、禁止写操作和不可信页面指令隔离。
- 研究端到端测试覆盖默认最近六个月、用户覆盖时间、岗位相关排序、少量结果选择、结果不足、不自主扩域和已有 Experience 排除。
- Daily Study Plan 测试复用现有真实 Agent Loop 与无虚假进度用例，并新增频率、Answer State、项目相关性和近期重复输入。
- Project Evidence Module 通过临时项目树测试 Registry 限制、秘密排除、二进制排除、超大文件限制、过期快照、只读性和无执行能力。
- Project Interview Training 通过多轮 Conversation 测试一次一题、用户指定或 Agent 选题、证据化追问、维度反馈、无数字总分以及确认后才写入 Training Outcome。
- 保留一个显式启用的真实 Codex 图片验收和一个安装后的真实目标 Vault 验收；它们不替代确定性测试。
- 秘密卫生检查扩展至附件字节、图片数据 URL、浏览器 Profile 元数据、网页内容和项目文件，确保这些内容不进入持久事件、日志或知识库。
- 恢复测试覆盖 uploaded、bound、Run started、Provider step committed、interrupted、resumed、completed、failed、cancelled、Conversation deleted 和 TTL sweep 等持久状态边界。

## Out of Scope

- 保存原始截图、完整网页正文或完整聊天记录到知识库。
- 本地 OCR、向量数据库、批量后台爬虫或无人值守定时研究。
- 复用用户日常 Chrome Profile 或直接接管任意浏览器操作。
- 发帖、评论、点赞、收藏、关注、私信、上传、下载或其他站点写操作。
- 为研究结果建立严格的高、中、低可靠度等级或数字评分。
- 在用户未指定时自动扩大公司、岗位、技术方向或时间范围。
- 在用户没有启动目标时自动创建研究、计划或训练 Run。
- 使用子 Agent、关键词路由或固定 Workflow 替代主 Agent 的工具调度。
- 执行、编译、测试或修改登记项目中的代码。
- 从未登记项目生成第一人称项目经历或多人协作叙事。
- 给项目训练生成总分、排名或自动声称用户已经掌握。
- 在本规格中实现简历生成、投递管理、招聘沟通或面试日程管理。

## Further Notes

- 第一阶段是唯一立即实施的范围：Interview Submission 经 Vision 或文本理解，完成 Experience 去重、Question 合并和原子知识入库。
- 后续 tickets 应采用 tracer-bullet 纵向切片，每张票在一个新上下文窗口内可独立验证；不应按协议、Runtime、UI 等横向层拆票。
- 现有公开接口优先于新增测试接缝。新增的五个深 Module 接口分别是 Run Attachment、Multimodal Input、Interview Catalog、Research Browser 和 Project Evidence；完整 Agent Run 是它们组合后的最高验收接缝。
- 附件传输对既有 WebSocket ADR 是窄化修订：大体积、不可重放的二进制使用认证本机 HTTP，Conversation 与 Agent Run 事件仍由 WebSocket 承载。
- 原始来源不是知识库资产；Source Fingerprint 仅用于去重，不能被扩展为隐式原文归档。
