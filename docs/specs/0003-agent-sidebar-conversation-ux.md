# OfferAgent: Agent Sidebar Conversation UX

Status: ready-for-agent specification.

## Problem Statement

OfferAgent 已经拥有可工作的 Conversation、流式 Agent Run、Vault 工具、变更确认和图片输入，但当前 Agent Sidebar 仍像调试界面而不是可长期使用的聊天产品。视图在每个流式增量到达时整体清空重建，导致滚动位置、输入焦点和用户阅读状态不稳定；历史 Conversation 缺少可区分标题；上下文只显示模糊的 `Vault context` 标签，无法看到用户固定了什么或 Agent 实际读取了什么；输入、附件和运行控制也没有形成统一、克制的交互。

其中部分问题虽然表现于前端，却不能只靠 CSS 修复。会话级图片历史、Conversation 重命名与归档、Evidence Snapshot 来源展示、停止输出的历史状态和旧标题回填，都需要插件、协议、Runtime 与持久化共同支持。

## Solution

把 Agent Sidebar 重构为一个 Obsidian 原生、默认中文、Conversation-first 的单栏聊天界面。产品借鉴 ChatGPT、Codex 与 Claudian 的交互层级，但使用 Obsidian 主题变量和组件语言，不做像素级复制。侧栏由极简标题区、稳定的滚动消息流、按需出现的活动与来源面板，以及底部悬浮 Composer 组成。

用户可以直接粘贴图片、通过 `@` 或 `+` 固定 Vault 文档、按 Enter 发送、停止当前 Agent Run 并修改提示词。流式输出只在用户仍位于底部时自动跟随；用户上拉后阅读位置保持不动。每条回答显示其实际使用的 Evidence Snapshot 来源，而不是把搜索候选或整个 Vault 冒充为上下文。Conversation 使用本地生成且可编辑的标题，并通过侧栏内抽屉进行搜索、切换、归档和删除。

## User Stories

1. 作为用户，我想直接把剪贴板图片粘贴进 Composer，以便无需先保存文件再查找路径。
2. 作为用户，我想拖入或通过一个 `+` 添加图片，以便在不同输入习惯下都能快速附加材料。
3. 作为用户，我想在发送前预览、删除和拖动调整多张图片的顺序，以便提交内容准确。
4. 作为用户，我想在发送后的消息中继续看到图片，以便 Conversation 历史保持完整。
5. 作为用户，我想在后续消息中再次引用同一 Conversation 的较早图片，以便不必重复上传。
6. 作为用户，我想删除 Conversation 时同时删除其本地图片，以便附件生命周期清楚可控。
7. 作为用户，我想在模型流式回答时自动看到最新内容，以便不必持续手动滚动。
8. 作为正在阅读旧内容的用户，我希望上拉后位置保持不动，以便新输出不会打断阅读。
9. 作为用户，我想通过“新内容”按钮恢复跟随，以便随时回到最新回答。
10. 作为用户，我想看到自己为本轮固定的 Vault 文档，以便发送前能检查上下文。
11. 作为用户，我想输入 `@` 搜索 Vault 文档，以便通过键盘快速固定来源。
12. 作为用户，我想通过 `+` 添加当前笔记、选择 Vault 文档或添加图片，以便常用入口集中但不拥挤。
13. 作为从笔记选区启动 OfferAgent 的用户，我希望该选区自动成为显式 Pinned Context，以便 Agent 知道我正在讨论哪段内容。
14. 作为用户，我不希望仅仅切换当前笔记就静默改变上下文，以便模型可见内容保持可预测。
15. 作为用户，我想在每条 Agent 回答下看到实际使用了哪些文档，以便判断回答依据。
16. 作为用户，我想点击来源打开对应 Vault 文档和行范围，以便快速核查原文。
17. 作为用户，我想把某个已使用来源一键固定到下一轮，以便继续围绕同一文档追问。
18. 作为用户，我想让 Pinned Context 只是优先候选而不是读取白名单，以便 Agent 仍能发现遗漏的相关资料。
19. 作为用户，我想让历史 Conversation 自动获得可区分的标题，以便快速找到旧对话。
20. 作为已有用户，我希望现存 `New Conversation` 标题被安全回填，以便升级后旧历史也可用。
21. 作为用户，我想搜索、重命名、归档和删除 Conversation，以便长期管理历史。
22. 作为用户，我想用侧栏内抽屉查看历史，以便聊天界面保持单一而简洁。
23. 作为用户，我想按 Enter 发送、按 Shift+Enter 换行，以便输入行为符合聊天产品习惯。
24. 作为中文输入法用户，我希望确认候选词时不会误发送，以便中文输入可靠。
25. 作为用户，我想在 Agent 输出期间继续编辑草稿，以便提前准备修正或下一条消息。
26. 作为用户，我不希望运行中的消息被自动排队，以便不会意外启动额外 Agent Run。
27. 作为用户，我想停止当前回答并保留已经看到的部分内容，以便及时纠正方向而不丢失参考。
28. 作为用户，我想把最近的提示词重新放入 Composer 修改后发送，以便快速重试。
29. 作为用户，我希望修改后的提示启动新的 Agent Run，而不是伪装成继续同一次运行，以便状态语义清楚。
30. 作为用户，我想阅读正确渲染的 Markdown、代码、表格和链接，以便回答具有可读性。
31. 作为用户，我想复制代码块或整条回答，以便复用输出。
32. 作为用户，我希望常规工具活动合并为一条摘要，以便消息流不会被执行细节切碎。
33. 作为用户，我希望确认、冲突、失败和中断继续直接可见，以便关键操作不会被隐藏。
34. 作为用户，我希望健康状态下不显示冗余的 `Connected` 文本，以便界面安静。
35. 作为用户，我希望模型、权限和发送/停止仍可从 Composer 直接访问，以便简洁不以牺牲控制为代价。
36. 作为用户，我希望界面默认使用中文并适配 Obsidian 明暗主题，以便与日常 Vault 环境一致。

## Implementation Decisions

### Sidebar shell

- 保持 ADR-0014 的单一 Agent Sidebar，不增加多标签聊天、任务中心或独立运行管理页。
- 使用 Obsidian CSS 变量、字体、焦点样式和明暗主题。借鉴 ChatGPT/Codex 的信息层级，不复制其品牌色、头像或页面框架。
- 顶部常驻历史入口、当前 Conversation 标题、新建按钮和一个溢出菜单。设置移入溢出菜单。
- Runtime 健康时不显示文字状态；断开、鉴权失败或其他需要操作的问题才显示诊断。

### Conversation history

- 历史入口在当前侧栏内打开覆盖式抽屉，不永久占用一列宽度。
- 抽屉提供搜索，并按“今天 / 昨天 / 更早”分组；每项显示标题和最后更新时间。
- 每项的溢出菜单提供重命名、归档和删除。删除必须确认；归档只从默认列表隐藏，不删除 Conversation Attachment。
- 选中 Conversation 后抽屉关闭，消息流定位到最新消息。
- 新 Conversation 在首条消息发送前可以短暂显示“新对话”；发送后立即用本地算法生成标题。Runtime 使用显式标题来源区分占位、自动和手动标题，不把本地化显示文字当作状态。
- 标题算法清理多余空白、代码围栏和无意义礼貌前缀，优先取第一个有效句子或分句，并限制为约 28 个中文字符或 56 个拉丁字符。无法安全改写时直接截断原句。
- 纯图片 Conversation 优先使用首张图片文件名；无有效文件名时使用“图片分析 · M月D日”。标题始终允许手动修改。
- 升级迁移只回填标题精确等于 `New Conversation` 且包含有效消息的记录。自定义标题和空 Conversation 不变；迁移必须幂等。

### Transcript and streaming

- 用户消息使用右侧紧凑气泡；Agent 回答左侧全宽且无大边框。默认不显示头像、说话者名称和时间。
- Agent 回答使用 Obsidian MarkdownRenderer。流式阶段按短时间窗口或块边界节流更新；完成后执行一次完整渲染。
- 代码块显示语言并提供复制按钮；表格和代码块在窄侧栏中横向滚动；内部链接用 Obsidian 导航，外部链接使用安全的新窗口行为。
- Transcript 必须保留稳定 DOM 和滚动容器。不得在每个 `agent_run.delta` 上清空并重建整个 Sidebar。
- 当用户距离底部不超过实现定义的小阈值时保持自动跟随；用户主动向上滚动越过阈值后进入冻结状态。
- 冻结状态下新内容不得改变 `scrollTop`，并显示不抢焦点的“新内容”按钮。点击按钮或手动回到底部后恢复跟随。
- 常规 Tool Call 按 Agent Run 聚合成一条折叠摘要；展开后按时间显示详情。Vault Change Batch、冲突、失败和 Interrupted Run 仍在消息流中直接显示。

### Composer

- Composer 固定在侧栏底部并使用 Obsidian 原生视觉。常驻控件只有 `+`、模型选择、权限图标和发送/停止主按钮。
- `+` 菜单依次提供“当前笔记”“选择 Vault 文档”“添加图片”。输入 `@` 打开 Vault 文档搜索。
- `Enter` 发送，`Shift+Enter` 换行。`KeyboardEvent.isComposing` 或等价 IME 状态为真时不得发送。
- Agent Run 运行时 Composer 保持可编辑，但发送动作不可用且不创建队列；主按钮切换为停止。
- 最近用户消息在悬浮或键盘聚焦时提供“放入输入框”。若 Composer 已有草稿，覆盖前必须确认。
- 停止运行后，部分 Agent 输出作为 Stopped Run 内容留在历史中并标记“已停止”，但不进入之后的 Conversation Context，也不构成 Run Checkpoint。修改后的提示词启动新的 Agent Run。

### Attachments

- 支持 PNG、JPEG、WEBP 和非动画 GIF；保留现有每条消息最多 20 张、单张最多 10 MiB、合计最多 50 MiB 的输入限制。
- 粘贴、拖入和文件选择使用同一原子导入路径。混合剪贴板同时包含文字和图片时，两者都必须保留。
- 草稿中的图片显示为横向缩略图条，支持拖动排序和单独删除，不再显示常驻 `Up` / `Down` 文本按钮。
- 已发送图片显示在对应用户消息中。原始字节作为 Conversation Attachment 存放于 Vault 外，不写入 SQLite、事件日志或 Vault。
- Conversation Attachment 可在同一 Conversation 的后续 Agent Run 中再次成为 Run Attachment。Conversation Context 裁剪掉对应消息轮次后，附件也不再自动注入模型，但历史仍可显示。
- 删除 Conversation 时原子删除其附件。归档不删除附件。建议初始容量上限为每个 Conversation 250 MiB、全局 2 GiB；达到限制时阻止新增并提供清理入口，不静默淘汰。

### Pinned Context and used sources

- 普通切换当前笔记不改变 Pinned Context。用户通过 `@`、`+` 菜单或选区调用显式添加。
- Composer 上方只显示本轮 Pinned Context 胶囊。胶囊可打开来源或移除；它们是优先候选，不限制 Agent 使用其他 Vault 工具。
- Search Result 不是 Evidence Snapshot，不得显示为“实际使用”。只有 Agent 通过有界读取形成的 Evidence Snapshot 才进入回答来源。
- 每条 Agent 回答底部显示“使用了 N 份文档”。展开面板列出路径、实际行范围和短片段，并支持打开原文及固定到下一轮。
- 来源按 Agent Run 归属，不显示成整个 Conversation 的全局 `Vault context` 状态。

### Protocol and persistence

- Conversation Summary 增加可更新标题、标题来源、最后活动时间和归档状态；协议提供重命名与归档命令，Runtime 状态存储负责幂等迁移。
- Conversation Message 保留有序 Conversation Attachment 元数据，并能区分完成回答与 Stopped Run 的非上下文输出。
- Stopped Run 的部分输出需要持久化以便重启后仍可见，但组装 Conversation Context 时必须明确排除。
- Conversation Snapshot 或按 Run 查询的等价协议必须向插件提供回答实际使用的 Evidence Snapshot 来源元数据；不得传输无关完整文档。
- Conversation Attachment 原始字节继续使用带认证的本机 HTTP 通道，事件与元数据继续使用可重放 WebSocket。附件所有权从 Agent Run 提升为 Conversation，Run 只持有有序引用。
- 旧附件表与文件布局的迁移必须可恢复。无法确定 Conversation 所有权的孤儿附件不得猜测绑定，应进入有界清理流程并报告诊断。
- 本规格允许同时修改插件、共享协议、本地 Runtime、持久化和测试；不得把端到端语义伪装成只改前端的临时状态。

## Testing Decisions

- 自动化测试只断言公开协议、Controller presentation 和用户可见行为，不锁定私有辅助函数、DOM 拼装细节、CSS 选择器或流式刷新次数。
- 主要自动化接缝是现有 Sidebar Controller / presentation view-model：向 Controller 提供确定性的 Runtime 行为和类型化事件，验证可见消息、Composer 动作、Conversation 管理、Pinned Context、附件和活动摘要。
- 第二个自动化接缝是通过真实认证 HTTP、WebSocket 和 SQLite 驱动的 Runtime 黑盒：验证持久化、重启、迁移、Conversation Attachment 所有权、Stopped Run 排除规则和 Evidence Snapshot 来源。
- 仓库目前没有通用 DOM/Electron UI 测试框架。本规格不为此单独引入 jsdom；滚动位置、焦点、Obsidian Markdown、主题和窄侧栏在真实目标 Vault 中验收。

### Deterministic controller and protocol tests

- Controller 流式测试连续发送多个 delta，断言消息身份和草稿状态稳定，并验证 presentation 能区分跟随、冻结和恢复三种滚动意图。Transcript 是否局部更新及 Composer 焦点是否稳定由目标 Vault 验收。
- 键盘测试覆盖 Enter、Shift+Enter、空消息、图片-only 消息和中文 IME composing 状态。
- 停止测试断言部分输出在重启后仍可见、标记为已停止、不进入下一次 Provider 输入，且“放入输入框”启动新的 Agent Run。
- 并发测试断言运行时可以编辑草稿，但不能发送或排队第二个 Agent Run。
- 标题测试覆盖中文、英文、Markdown、URL、长文本、纯图片、手动标题和幂等旧数据回填。
- Conversation 测试覆盖搜索、重命名、归档、恢复归档、删除确认及删除后的附件清理。
- Context 测试区分 Pinned Context、搜索候选和真正 Evidence Snapshot，验证来源严格按 Agent Run 归属。
- 附件测试覆盖粘贴、拖入、选择、混合文字、排序、预览、跨 Run 再引用、重启恢复、容量限制、跨 Conversation 拒绝和删除清理。

### Rendering and accessibility acceptance

- Markdown 验收覆盖标题、列表、引用、内部链接、外部链接、代码块、复制、宽表格和超长无空格文本。
- 明暗主题下不得使用硬编码背景或文字颜色；在 320 px 和 480 px 侧栏宽度下不得出现页面级水平溢出。
- 所有图标按钮具有中文 `aria-label`、可见焦点和键盘操作。历史抽屉打开时管理焦点，关闭后回到触发按钮。
- 流式渲染必须节流，且不因每个 token 创建完整 Sidebar DOM。长 Conversation 的增量更新只影响发生变化的消息、活动摘要和必要状态。

### Manual target-Vault acceptance

1. 在目标 Vault 中打开 OfferAgent，确认界面默认中文，并分别检查明暗主题与窄侧栏。
2. 用 Windows 截图工具复制图片，在 Composer 中按 Ctrl+V，确认无需保存文件即可看到预览并发送。
3. 重启 Obsidian 后打开同一 Conversation，确认图片和消息仍在，并能在新消息中引用较早图片。
4. 发送产生持续流式输出的问题，先保持底部观察自动跟随，再上拉确认位置固定，最后点击“新内容”恢复。
5. 通过 `@` 固定一份文档，通过 `+` 固定当前笔记，确认两者清楚显示且可以移除。
6. 让 Agent 搜索并读取其他 Vault 文档，确认回答来源只列出实际读取文档及行范围，并可点击打开。
7. 在流式输出中输入新草稿，确认不会自动排队；停止后把原提示放入 Composer、修改并重新发送。
8. 创建多个 Conversation，确认新旧会话均有可区分标题，抽屉搜索、重命名、归档和删除有效。
9. 验证普通工具活动默认合并，Vault 变更确认、冲突和错误仍然清楚可见。
10. 删除含图片的 Conversation，确认对应本地附件被清理且其他 Conversation 不受影响。

## Out of Scope

- 像素级复制 ChatGPT、Codex 或 Claudian 的品牌视觉。
- 多列永久历史栏、多标签 Conversation、任务中心、Dashboard 或独立 Run 管理页。
- 自动把当前笔记静默加入上下文。
- 默认将 Pinned Context 作为严格读取白名单；如未来需要，可单独设计“仅使用所选文档”模式。
- 同一 Conversation 中自动排队多个 Agent Run。
- 真正冻结并继续同一个用户暂停 Run；用户操作是 Stop and Revise，Interrupted Run 的恢复语义保持独立。
- 把 Conversation Attachment 复制进 Vault、Interview Experience 正文或 Planning Memory。
- 首轮完整国际化体系；界面默认中文，模型名、文档名和必要的原始错误保持原样。
- Obsidian 移动端支持。当前 Runtime 与插件架构继续以 Windows Obsidian Desktop 为验收目标。

## Further Notes

- 本规格细化并扩展既有本地 Obsidian 插件规格中的 Agent Sidebar 范围。
- ADR-0022 定义 Stop and Revise；ADR-0023 定义 additive Pinned Context 与实际 Evidence 展示；ADR-0024 取代 ADR-0017 的运行终态附件清理规则。
- 实现应按用户可验收的纵向切片拆分，而不是先分别重写协议、Runtime 和 UI。每个切片需要同时包含必要的状态迁移、测试和目标 Vault 可见结果。
