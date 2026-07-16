# OfferAgent 个人本机版本完成证据

> 审核日期：2026-07-15。对标依据只取 Claude Code 官方公开文档与可观察行为；本文不声称复刻其未公开内部实现。

## 结论

当前代码已收敛为一个 Worker 拥有 Agent Loop、文件工具、权限、审计、恢复和 Vault 写边界的本地执行链。Obsidian 插件只负责配置、命令提交和有序事件展示。知识上下文只从已授权 Workspace/Vault 经 Glob、Grep（ripgrep）和 Read 获取；项目中没有 RAG、全文索引、Embedding、向量检索、文档切块或相关性路由链。

## 模块审核对照表

| 范围 | Claude Code 公开原则 | OfferAgent 当前实现 | 2026-07-15 证据 |
| --- | --- | --- | --- |
| 多轮会话与上下文 | 会话连续、按需取上下文、上下文有界 | Session/Turn/Run 为持久化权威状态；事件按 Run sequence 回放；运行快照只注入明确时间、权限、指令和已激活 Skill 上下文 | Session lifecycle、typed replay、ChatStore hydration 测试通过 |
| 本轮消息交互 | 用户提交立即可见，运行阶段和工具事实可观察 | 插件先加入 transient user submission，再以精确 Turn id 与持久化事件对账；规划、工具、完成/失败均来自协议事件 | 插件消息与 timeline 测试通过 |
| 会话记忆 | 历史按边界读取，不隐式发明事实 | 历史由 Session/Turn 事件读取；没有自动总结、评分、语义召回或隐式记忆写入 | 上下文和 Session 测试通过 |
| 长期记忆 | 显式、可见、可编辑的文件 | 唯一固定入口是 `.offeragent/memory/MEMORY.md`，受字节数、行数、UTF-8、内容 hash 和配置开关约束；其他文件只可经文件工具读取 | 功能实现并有测试；目标 Vault 当前没有该文件，因此实际未注入 |
| 本地知识 | 搜索文件后读取必要范围 | 仅 Glob、Grep（独立 `rg.exe`）和 Read；路径 containment、结果上限、来源 hash 与行范围进入工具结果 | File Tool、真实 ripgrep、Vault integration 测试通过 |
| Tool Registry | 单一权威注册与执行入口 | ToolRegistry 提供定义；ToolKernel 是权限、预检、执行、结果和审计的唯一入口；写操作逐个 JIT 预检并串行提交 | Tool Kernel contract、transaction integration 通过 |
| 权限与执行边界 | 显式信任、最小权限、危险操作可控 | Workspace trust 与 permission mode 独立；当前 Vault 配置为 `bypass` 且显式信任，Vault 写自动批准；工具声明不能扩张 Run authority | 权限、审批重连、策略审计测试通过 |
| Skills | 发现元数据，正文按需加载，工具受当前权限约束 | `.claude/skills/*/SKILL.md` 自动发现；信任后正文由 Skill 工具按需读取并注入本 Run；同一 Skill 每 Run 只激活一次，激活后必须重新规划 | 单元、集成及真实 DeepSeek Run 均通过 |
| `CLAUDE.md` 与规则 | 项目指令、路径作用域规则、按需加载 | 自动加载 `CLAUDE.md`/`CLAUDE.local.md` 与路径链；`.claude/rules` 支持带 `paths` frontmatter 的作用域；`AGENTS.md` 不会自动识别，只能由 Claude 指令文件显式 `@` 导入 | 作用域、显式导入和 fail-closed 测试通过；目标 Vault 只有根 `CLAUDE.md` |
| 子 Agent | 隔离上下文、权限缩减、生命周期与结果回收 | 子 Run 只从父 Run 派生收缩权限和预算；有独立 lease、Artifact、恢复与父 Run 事件回收；不能拥有第二套 Runtime | Subagent contract、预算、恢复测试通过；当前插件配置启用 |
| Hooks | 配置可见、执行可审计、不能绕过权限 | Hook 定义与确认状态持久化；命令经 Process Supervisor 和 Tool 生命周期执行；取消会终止真实子进程树 | SQLite Hook、production Hook、Windows process tree 测试通过；当前插件配置启用 |
| Provider | 模型边界不拥有工具和业务状态 | Provider 只解析传输流和严格 JSON object；AgentStepCatalog 唯一验证业务 Schema；每轮输出工具调用或最终响应，不再存在第二个 Composer 模型链 | DeepSeek/OpenAI/Ollama、Agent Loop 和网络审计测试通过 |
| 恢复与并发 | 单一权威状态、可恢复、冲突可解释 | 每 Session 一个 active root Run；UI mutation single-flight；SQLite CAS、Journal、deadline、预算 checkpoint 和 crash recovery 统一处理 | Runtime、Worker crash、Vault durable recovery 测试通过 |

## 删除与重建

- 删除独立 Composer/ModelComposer 及其测试、事件和预算阶段；模型每轮只产生一个 canonical AgentStep。
- 删除前端独立 Memory 管理后台与不存在的协议能力；长期记忆回到一个固定可见文件。
- 删除由 Obsidian 反向拥有 Vault 写授权的链路；Worker 本地以路径授权、CAS、Journal 和审计提交。
- 删除旧周计划的第二条路由；`daily-study-workflow` 同时处理单日、日期范围和自然周，`weekly-review-workflow` 只基于既有 Daily 事实做复盘。
- Provider 不再重复验证 Agent 业务 Schema；工具调用与最终文本由同一个 AgentStepCatalog 验证，并只允许一次明确的格式修复。
- ToolKernel 对 effectful batch 逐项即时预检、串行执行且前项失败后取消余项，避免陈旧 CAS 预检和部分继续执行。
- 插件的发送、重试、分叉、压缩和其他 Session mutation 统一 single-flight；用户消息即时显示并与持久化 Turn 对账。

## 当前实际启用能力

必须区分“代码存在”“配置启用”和“真实运行成功”：

| 能力 | 代码存在 | 目标 Vault 配置 | 真实运行 |
| --- | --- | --- | --- |
| DeepSeek Chat Completions | 是 | `deepseek-v4-flash / medium` | 已完成真实 API Run |
| Glob/Grep/Read | 是 | Workspace 已信任 | 真实 Run 已使用 |
| Vault 本地写入 | 是 | `bypass`、自动批准写入 | 真实 Run 已提交 7 个文件 |
| Skills | 是 | 自动发现，不需要逐个设置 | 真实 Run 已激活 Daily Skill |
| Shell | 是 | 已启用 | 自动化测试通过；本次真实计划 Run 未调用 |
| Subagents | 是 | 已启用 | 自动化生命周期/恢复通过；本次真实计划 Run 未调用 |
| Hooks | 是 | 已启用 | 真实进程树集成测试通过；本次真实计划 Run 未调用 |
| 固定长期记忆文件 | 是 | 目标 Vault 文件不存在 | 未注入，不声称可用内容 |
| `.claude/rules` | 是 | 目标 Vault 目录不存在 | 作用域自动化测试通过，目标 Vault 本次无规则可加载 |

## Skills 真实端到端验收

目标 Vault `E:\面试胜利！` 当前有三个 Skill：

- `daily-study-workflow`：单日、日期范围和当前自然周学习计划；模板内置于 Skill asset。
- `weekly-review-workflow`：只从已有 Daily 实际记录生成周复盘；模板内置于 Skill asset。
- `prepare-job-interview`：根据岗位要求和 Vault 内真实材料制定面试应对计划；模板内置于 Skill asset。

真实验收输入为 `请制定这周的学习计划`，使用生产 Worker、DPAPI SecretStore 和真实 DeepSeek `deepseek-v4-flash`：

1. 自动发现并读取 `daily-study-workflow` 元数据，信任后只激活一次正文。
2. 读取根 `CLAUDE.md`、Skill 内置模板、面经索引、面试进度和实际面经文件。
3. Grep 命中 7 个带目标状态的实际材料；再 Read 选定文件，没有语义检索或评分。
4. 精确 Glob 检查 7 个目标路径。
5. 提交 7 个独立单文件 `vault.transaction`；Kernel 串行执行并全部 commit。
6. 生成 `daily/2026-07-13.md` 至 `daily/2026-07-19.md` 后才返回最终响应。

Run `run_312fd8871e76a4989230163fe2f904eb` 完成于 165.5 秒，共 6 个模型轮次、23 个工具调用；没有测试替身、手工中间介入或插件反向写桥。

## 质量门禁

2026-07-15 在 Windows x64 本机执行：

- Python 全量：`1117 passed, 6 skipped`，145.35 秒；skip 仅由 Windows 文件共享语义或当前账户无 symlink 权限造成。
- 单元：`819 passed, 3 skipped`；集成：`233 passed, 3 skipped`；协议/Schema：63 项通过。
- Runtime 集成：`82 passed`，包含真实 Worker 崩溃恢复、Named Pipe、Job Object、SecretStore 与启动恢复。
- Obsidian 插件：`87 passed, 1 skipped`；TypeScript `--noEmit` 与本地锚定构建通过。
- Ruff 检查通过；Ruff format 检查 381 个文件通过；Mypy 359 个源文件 0 issues。
- Import Linter 分析 283 个文件、2321 条依赖，11/11 contracts kept。
- 协议 Schema 可复现：`sha256:bf5d2343d754e26b916bd410ce027f2a76f72e849b5ef43bd41a26a2990b1916`。
- repository closure、forbidden dependencies、architecture check、Web assets check 全通过。
- `uv build` 成功生成 sdist 与 wheel。

## 当前插件包与安装

- 构建包：`E:\Projects\offeragent\artifacts\offeragent-obsidian-plugin-20260716-state-v6-profile`
- 安装目录：`E:\面试胜利！\.obsidian\plugins\offeragent-obsidian-plugin`
- 安装后的 `main.js` 与构建包 SHA-256 一致：`5AF0049550C5DE43C8667720E8EBFA2C79A9436EECBD8C2C8B163EA49E6C78DF`
- 插件目录只有这一份 OfferAgent；原子安装没有保留旧插件目录。
- `data.json` 仅保存 schema、非敏感设置和本地聊天页签；API key 由 Windows DPAPI SecretStore 管理，不进入插件包、Vault 笔记或 Git。

本次工作树保持未提交状态；只有用户明确允许后才能创建 Git commit。

## 2026-07-16 Runtime 断线修复

真实安装曾在启动恢复扫描中因旧 ToolResult 缺少 `contextActivations` 而退出。修复没有放宽解码器或加入 fallback：SQLite Schema v6 原子地把终态历史记录转换为 RunState codec v5；无法安全推断 Skill 激活状态的旧活动 Run 快照会被移除并由既有恢复协议明确中断。真实数据库已迁移至 v6，8 个 RunState 和 59 个 Journal ToolResult 均可由新严格解码器读取。

第二个启动故障来自净化后的插件进程环境没有 `USERPROFILE`，而 Worker 曾调用 `Path.home()` 定位用户级 Skill。当前 Worker 改用 Windows Profile Known Folder API，不再依赖父进程环境；Codex 本地凭据的默认根目录也使用同一可信 Profile 边界。最终冻结 Worker 在与插件相同的最小环境中持续存活 20 秒并进入 JSON-RPC 等待阶段。

插件现在只从受限 Worker stderr 中解析稳定错误码，并显示 `runtime_disconnected · <causeCode>`；任意异常正文、路径或秘密不会进入 UI。
