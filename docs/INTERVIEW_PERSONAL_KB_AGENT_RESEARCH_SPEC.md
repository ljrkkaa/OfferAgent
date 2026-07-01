# Interview Personal KB Agent Research Spec

> 目标：在当前 Khoj 面试精简分支上，做一个面试个人知识库 Agent。原则是沿用 Khoj 已经能用的 `/api/chat`、Agent、Research loop、Memory 和 Obsidian 插件，只做必要修改；不重写 Agent 框架，不引入新的检索框架。

## 0. 当前完成度与下一步

更新时间：2026-07-01。基于最新阶段提交 `0474cab8 feat(agent): add file-first local kb workspace`、`12643a0d feat(agent): add interview writeback runtime`、`1b731764 fix(agent): require explicit vault write intent` 和 `2f1d4ed4 feat(qqbot): add thin interview chat adapter`；当前工作区已落地 Phase 5 OpenKB Knowledge Harness、旧向量检索硬删除、本地 embedded DB 重建验证、Notes 主 tool loop 的本地 SKILL.md 绑定，以及本轮 OpenAI-level review 修复的真实 UI/API/Agent 断点，正在按功能分阶段提交。最新真实 HTTP E2E 已验证：Notes agent 能读取嵌套官方 Obsidian skill、按 vault 规则写入 daily note、根据本地 Redis 笔记生成面试八股问答，且 Obsidian 上传/搜索/删除和 chat/session/agent options smoke 通过。新的 Agent Harness worker / observability 设计已写入独立 spec；2026-07-01 根据真实 “Agent 评估八股 + 补充到项目里” 失败链路，已选择 Claude-style unified agent runtime 方案并写入中文实施计划 `docs/superpowers/plans/2026-07-01-unified-claude-style-agent-runtime-zh.md`，英文版保留在 `docs/superpowers/plans/2026-07-01-unified-claude-style-agent-runtime.md`。第一版代码已新增 `src/khoj/processor/conversation/agent_tool_loop.py` 并接入 default chat：默认聊天删除浅 source router 依赖，而是由单主 tool loop 统一调度 web/local-KB/OpenKB/writeback，并把 web 工具结果传给 Notes 写入 verifier；旧 `aget_data_sources_and_output_format()` 和 `pick_relevant_tools` prompt 已删除。Codex `json_object` payload 已修复，避免 Responses API 因 input 缺少 json hint 报 400。Interview Card Maintainer 已作为项目级本地 skill 第一版落地，按当前知识库项目设定读取目录、索引、命名和写入边界，不绑定具体库名或项目名。下一步：用真实模型和真实浏览器/Obsidian 复测 “根据网络资料补充 agent 评估八股内容 → 补充到项目里”。

2026-07-01 分阶段提交进度：第一阶段 `ab72ad9d refactor(search): remove legacy vector search stack` 已提交，范围为旧 vector/embedding 检索栈删除、数据库迁移压缩和词法索引测试更新。验证：迁移 `replaces=114 deleted=114`；`makemigrations --check --dry-run` 无变更；`showmigrations --plan` 中 `database.0001_initial` 在 `admin.0001_initial` 前；`ruff check` 相关后端目录通过；`pytest --create-db tests/test_text_search.py tests/test_client.py::test_search_with_valid_content_type tests/test_agents.py::test_create_or_update_agent_with_knowledge_base_and_search -q` 为 `14 passed, 1 skipped`。第二阶段 `6a8e1aef feat(kb): add notes and openkb tool primitives` 已提交，范围为 local KB 文件工具、OpenKB wiki harness、Notes tool loop 和对应工具层测试。验证：`ruff check` 本阶段文件通过；`git diff --cached --check` 通过；`pytest tests/test_local_kb.py tests/test_notes_tool_loop.py tests/test_openkb_harness.py -q` 为 `54 passed`。第三阶段 `d9098e11 feat(api): wire kb evidence into backend routes` 已提交，范围为 `/api/chat`、`/api/search`、Research/helper 文档工具、后端 API 边界修复和对应真实 route 测试。验证：`ruff check` 本阶段文件通过；`git diff --cached --check` 通过；`pytest --create-db tests/test_api_chat_file_kb.py tests/test_local_kb_summary.py tests/test_local_kb_fallback.py tests/test_local_vault_references.py tests/test_research_document_tools.py tests/test_api_automation.py tests/test_memory_settings.py tests/test_helpers.py tests/test_online_chat_actors.py tests/test_agents.py tests/test_client.py tests/test_multiple_users.py -q` 为 `172 passed, 9 skipped`。第四阶段 `d7c54926 fix(web): harden client response handling` 已提交，范围为 Web 端响应 shape 校验、HTTP 失败回滚、附件提交快照、hook 依赖和静态资源修正。验证：`npx prettier --check app components`、两个 SVG 的 `prettier --parser html`、`npx tsc --noEmit`、`npx next lint`、`timeout 180s npx next build` 均通过。第五阶段 `6fb271a8 feat(clients): update obsidian and qqbot adapters` 已提交，范围为 Obsidian 插件 HTTP/会话/搜索交互修复和 QQBot 薄适配器。验证：`ruff check src/khoj/integrations/qqbot tests/test_qqbot_adapter.py` 通过；`pytest tests/test_qqbot_adapter.py -q` 为 `5 passed`；`cd src/interface/obsidian && npm run build` 通过。第六阶段 `c5cdf892 docs: update kb evidence setup notes` 已提交，范围为 README、Postgres/docker compose 和 ignore 说明收尾。验证：`docker compose -f docker-compose.yml config` 通过。下一步：跑最终整体验证并准备交付。

| 阶段 | 状态 | 依据 | 下一步 |
| --- | --- | --- | --- |
| Phase A：Codex 模型后端 | 已完成 | `a1e2eef1 feat(chat): add codex conversation runtime`；`docs/superpowers/specs/2026-06-29-phase-a-codex-design.md` | 无；仅按需修 bug |
| Stage B：本地只读知识库工具 | 已完成 | `7500d8db feat(agent): add interview local kb runtime`；`docs/superpowers/specs/stage-b-local-knowledge-base-spec.md` | 保持 read-only；Notes 不回退旧文档检索 |
| Stage C：File-first Agent Workspace | 已完成 | `0474cab8 feat(agent): add file-first local kb workspace`；`docs/superpowers/specs/stage-c-file-first-agent-workspace-spec.md`；实现 `kb_list`、literal-first `kb_grep`、`kb_headings`、`kb_resolve_link`、编号 `kb_read`、`collect_notes_evidence_with_tools()`；验证 `uv run pytest tests/test_local_kb.py tests/test_notes_tool_loop.py tests/test_api_chat_file_kb.py tests/test_local_kb_summary.py tests/test_research_document_tools.py tests/test_local_kb_fallback.py tests/test_grep_files.py -q` 通过 | 保持 file-first 为 Notes 主路径 |
| Vault 写回 / Interview Agent persona | 已完成，已并入主 tool loop | `12643a0d feat(agent): add interview writeback runtime`；`1b731764 fix(agent): require explicit vault write intent`；面试表达由 `Agent.personality` / 用户指令驱动；写回由主 `/api/chat` Notes tool loop 的 `append_note` / `propose_edit` 工具触发，后端继续执行 root jail、client 和 `KHOJ_ALLOW_VAULT_WRITE` 校验；验证 `uv run pytest tests/test_local_kb.py tests/test_notes_tool_loop.py tests/test_api_chat_file_kb.py tests/test_local_kb_summary.py tests/test_research_document_tools.py tests/test_local_kb_fallback.py tests/test_grep_files.py -q` 通过 | 进入 Phase 4：QQBot 薄入口和 gold set |
| QQBot / gold set eval | 已完成 | `2f1d4ed4 feat(qqbot): add thin interview chat adapter`；实现 `src/khoj/integrations/qqbot/adapter.py`、入站归一化、默认拒绝空 allowlist、回复分片、注入式 chat bridge、离线 interview gold set；验证 `uv run pytest tests/test_qqbot_adapter.py tests/test_notes_tool_loop.py -q` 和 64 个主线回归测试通过 | 如需上线，再接官方 QQ Gateway 或 OneBot/NapCat，并按服务器端口转发策略暴露本地服务 |
| Phase 5：OpenKB Knowledge Harness / 旧向量检索硬删除 | 已分阶段提交，待最终整体验证 | `ab72ad9d refactor(search): remove legacy vector search stack`；`6a8e1aef feat(kb): add notes and openkb tool primitives`；`d9098e11 feat(api): wire kb evidence into backend routes`；`d7c54926 fix(web): harden client response handling`；`6fb271a8 feat(clients): update obsidian and qqbot adapters`；`c5cdf892 docs: update kb evidence setup notes`；`docs/superpowers/specs/2026-06-30-openkb-knowledge-harness-design.md`；`docs/superpowers/plans/2026-06-30-openkb-knowledge-harness.md`；实现 `src/khoj/utils/openkb.py` 的 `wiki_search_documents()`、PageIndex JSON 页码读取、显式 `save_exploration()`；`/api/chat` 按 `KHOJ_KB_ENGINE=file_first/openkb/hybrid` 把 local KB / OpenKB / write 工具交给主聊天模型选择，OpenKB 只输出 `compiled_references`，最终回答仍由主链路生成；删除 `/api/chat` legacy document-search fallback、旧运行时开关、research 旧语义工具面，以及旧本地 KB / 面试硬解码设计；`/api/search` 保持非 agentic direct endpoint：配置 local KB 时只查本地 evidence，显式 OpenKB 时查 compiled wiki，否则查权限过滤后的 `text_search.query()` 词法索引；Memory search 也为权限过滤后的词法扫描；删除旧检索模型配置、旧检索管理命令、Entry/UserMemory 向量字段和日期旁表，并把 database migration 历史压成新的无旧检索初始迁移；ponytail cleanup 删除旧 local KB wrappers、孤立 vector placeholder、重复 query-term helpers、旧 API 参数和旧 prompt/doc 残词；已删除旧 embedded DB 目录并用新迁移重建 `/data/ljr/my_project/khoj/pgserver_data`，验证旧表/旧列不存在；Notes tool loop 已按 OpenKB runtime 方式扫描 bound vault 的 `.codex/skills/*/SKILL.md` 和 `skills/*/SKILL.md`，向主 agent 注入 skill catalog 且只保留 `read_skill` 工具，并把根目录 `agents.md` 等项目说明作为规划上下文注入；真实用户场景验收后补强了中文写入/更新工具规划、append_note 新建文件说明、首轮无工具调用二次规划、planner 瞬断重试，以及写入结果对最终回答的强 grounding；同日 review 后删除重复 `list_skills` 工具、收窄 retry 代码、把写入结果上下文改成短 JSON，并把 prompt 文案测试改为真实创建行为测试；OpenAI-level review 继续修复：发现类工具 grep/list/headings 后未 `view_file` 会导致 `/api/chat` 证据不足，现强制二次规划精读；`read_skill` 文件消失不再打崩 Notes loop；Codex runtime 下不再用传统 `KHOJ_DEFAULT_CHAT_MODEL` 误报默认模型错误；Research/helper grep 的本地 KB 参数名回归已修复；继续补测真实 HTTP 后修复自动任务执行语义丢失（已触发任务不再重新询问何时提醒）和非法 `conversation_id` 触发 UUID ValidationError 500；本轮真实浏览器 review 继续修复 `/search` 因 `/api/content/computer` 返回对象而崩溃、缺失文件接口 500、Next static export `.txt` 404、SVG `height=auto` 控制台错误、浏览器直连 ipapi CORS 错误、Automations 在只有浏览器时区 fallback 时显示 `undefined, undefined`，并删除 Web CSP / legacy template 中已不再需要的 `ipapi` connect-src 残留；浏览器 CSP 下同源 WebSocket 已真实打开；2026-07-01 继续修复首页/分享页/聊天页附件提交快照，确保同一轮消息的 images/files 不被 React 批量状态刷新或 localStorage effect 重跑丢失，并修复空白 query 的 websocket telemetry `conversation_commands` 未初始化错误 | 下一步：跑最终整体验证并准备交付 |
| Phase 6：Claude-style unified agent runtime / observability | 第一版已实现，待真实模型/浏览器复测 | `docs/superpowers/specs/2026-06-30-agent-harness-workers-observability-design.md`；中文计划 `docs/superpowers/plans/2026-07-01-unified-claude-style-agent-runtime-zh.md`；英文计划 `docs/superpowers/plans/2026-07-01-unified-claude-style-agent-runtime.md`；新增 `src/khoj/processor/conversation/agent_tool_loop.py`，default chat 已删除 `aget_data_sources_and_output_format()` 浅 source routing，进入单主 planner，统一调度 `web_search` / `read_webpage`、local-KB/OpenKB Notes 工具、`append_note` / `propose_edit` 和 write grounding verifier；旧 `pick_relevant_tools` prompt、旧 source/output mode 常量和旧 source-router 测试已删除；Codex `json_object` payload 已加入 input JSON hint；显式 slash commands 保持现有专用路径；验证：`uv run pytest tests/test_agent_tool_loop.py tests/test_codex_conversation_adapter.py tests/test_notes_tool_loop.py tests/test_api_chat_file_kb.py tests/test_research_document_tools.py -q` 为 `79 passed`，`uv run ruff check src/khoj tests` 通过，`uv run pytest -q` 为 `385 passed, 12 skipped` | 下一步：真实模型 + 浏览器/Obsidian 跑 “根据网络资料补充 agent 评估八股内容 → 补充到项目里”，确认真实 web evidence、assistant artifact 和落盘写入都符合预期 |
| Phase 7：Interview Card Maintainer / 项目事实卡 grounding | 第一版已实现，待真实面经跑通；本轮不提交 | 已创建项目级本地 skill `skills/interview-card-maintainer/SKILL.md`，自然语言手动触发，不安装到全局 `/data/ljr/.codex/skills/`，不新增按钮或 API；skill 启动时先读取用户指定设置、根目录 `agents.md` / `AGENTS.md` / `agent.md`，再按设置派生面经目录、面试卡目录、项目事实卡目录、索引、进度文件、命名规则和回答风格；fallback 目录只作为设置缺失时的兜底，不绑定具体知识库名或项目名；已新增通用 `templates/面试知识库项目设定模板.md` 和 `templates/面试项目事实卡模板.md`，并保留一张项目事实卡实例用于把 RAG、MCP、评估和可观测性题卡 grounded 到真实项目事实；验证：`quick_validate.py` 通过，`uv run pytest tests/test_notes_tool_loop.py::test_notes_tool_loop_loads_nested_official_obsidian_skill -q` 通过 `1 passed`，skill/templates 硬编码库名/项目名和占位符扫描无输出；原三次 docs commit 已按用户要求 reset 到 `c5cdf892` 后 | 下一步：用一篇现有面经手动触发 skill，检查能否创建/更新设置定义的面试题卡、补 `## 结合我的项目`、更新索引和进度 |

2026-07-01 Agent 语义判断准则：不要用词表、关键词命中、token overlap 比例这类伪智能规则判断用户意图或内容是否 grounded。代码只负责协议完整性、权限、路径、数据形状、安全边界和预算控制；语义选择、写入意图、内容 grounding 交给模型通过结构化 JSON 协议裁决。Notes 写入链路已按这个准则重修：`append_note` 增加 `write_intent` 和 `source_refs`；执行端不再用指代词表判断，而是按工具参数关系判断：如果 `content` 不是用户当前请求里明确给出的原文，就必须带 `source_refs`，否则返回 `source_refs_required`。第一版曾用 stop tokens 和 token overlap 比例判断来源一致性，真实浏览器复测证明这会误杀学习日记模板化改写；现已删除该启发式。当前边界改为：先确定性拦截写入内容中新冒出的文件名/路径，再用一次隔离上下文的结构化 grounding verifier 判断候选写入是否只来自声明来源；不通过就返回 `source_mismatch`，让 planner 带着错误状态重试。主 chat 的 data source 选择也删除了英文关键词 override，不再用 `latest/weather/image/my` 等词强行覆盖模型已返回的结构化选择，只保留 allowlist 校验。验证：先红后绿覆盖“携程 AI 应用开发面经学习日记写入时混入 raw/Windows 同步问题”、“没有文件名但混入 Windows 同步问题”、“面经内容模板化写入日记”和“data source 选择尊重模型裁决、不被关键词 override”的回归；`uv run pytest tests/test_notes_tool_loop.py -q` 为 `21 passed`；`uv run pytest tests/test_research_document_tools.py::test_data_source_selection_respects_model_verdict_without_keyword_override tests/test_research_document_tools.py::test_local_kb_makes_notes_source_available_without_entries -q` 为 `2 passed`；`uv run pytest tests/test_api_chat_file_kb.py::test_chat_notes_local_kb_write_request_appends_and_reports_tool_result tests/test_api_chat_file_kb.py::test_chat_notes_qqbot_write_reports_blocked_tool_result -q` 为 `2 passed`；`uv run ruff check src/khoj/processor/conversation/notes_tool_loop.py src/khoj/routers/helpers.py tests/test_notes_tool_loop.py tests/test_research_document_tools.py` 通过。下一步：在真实浏览器/Obsidian 对同一条“根据 experiences/... 规划今日学习日记，然后把这个写入学习日记”做人工复测。

2026-06-30 最新 review 补充：自动任务 API 已修复输入边界。`post_automation` / `edit_job` 现在先规范化并验证 cron，malformed cron 返回 400；`schedule_automation` / `aschedule_automation` / `edit_job` 共享 timezone UTC fallback，坏 timezone 不再导致 500。前端 review 继续收敛真实浏览器路径：Phosphor `Image` 图标避免被 ESLint 当成 `<img>`，Automations toast hook 依赖补齐，`/api/agents` 最近会话排序 cutoff 改成 timezone-aware UTC，`/chat` 页面不再触发 Conversation `updated_at` naive datetime warning。2026-07-01 继续补齐 `/chat` 流式消息处理和 interrupt effect 的 stale closure 依赖，避免切换会话后标题生成或中断消息使用旧闭包；`/search` 上传弹窗的上传结束 effect 已订阅 `warning/error`，避免上传失败提示刚出现就被旧闭包自动关掉。

2026-07-01 最新 review 补充：聊天附件提交链路已从“父组件 effect 读取可变 images/uploadedFiles”改成“提交瞬间快照”。`ChatInputArea.sendMessage` 会把本次上传的 image data 一起传给首页、分享页和聊天页；`/chat` 用 `PendingChatRequest` 同时驱动 placeholder 和 websocket body，并用 `sentRequestRef` 防止 socket/locationData 变化重复发送；从 localStorage 恢复消息时先读取 images/files，再立刻移除 message，避免 callback identity 变化导致 effect 重跑并覆盖成无图片请求。真实 WebSocket payload 验证显示 images/files 均随同一轮请求发出。空白 query 分支同时修复了 `conversation_commands` 未初始化导致 telemetry NameError 的后端错误。

2026-07-01 最新前端 review 补充：继续清理高风险 React hook 闭包问题。`/search` 的文件过滤弹窗现在订阅最新 `allFiles/inputText/onClose`，并删除重复首屏 fetch；`/agents` 的表单重置和 mutate effect 订阅真实依赖；Agent 文件选择器把上传回填改成稳定 callback 和函数式去重，避免旧 `allFileOptions` 覆盖新文件；`ChatHistory` 的滚动、翻页和更多历史消息读取逻辑改成 `useCallback`，IntersectionObserver 和 ResizeObserver 不再持有旧 conversation/message 闭包。本轮未扩大处理其它页面历史 warning。

2026-07-01 最新前端 lint 收敛补充：`app` 和 `components` 下 ESLint 已清到零输出。修复范围保持最小：`ModelSelector` 不再把整包 `props` 带进 effect；`ChatMessage` 在 code context / Excalidraw / Mermaid 数据变化时重新渲染，MutationObserver 不再依赖 `ref.current`；`ChatSidebar` 的 agent 初始化改成稳定 callback，并修复 `sort()` 原地修改 state 的问题；`ExcalidrawWrapper` 改用 `window.addEventListener` 注册 Escape 快捷键，不再覆盖全局 `onkeydown`；`LoginPrompt` 的 Google 登录回调用 `useCallback`，metadata 未加载前不初始化 SDK，受控登录 GIF 改为 `next/image`；动态 base64/favicons/tool frame 图片保留原生 `<img>` 并显式关闭不适用的 Next Image lint。

2026-07-01 Settings Memory API review 补充：Settings 里的 “Your Memories” UI 依赖 `/api/memories` list/update/delete。API 已挂载，但 `PUT /api/memories/{id}` 原先通过 delete + recreate 更新文本，会改变 memory id 并丢失 agent 归属；现在改为原地更新 `raw`，保留 id、created_at 和 agent 关系，并补了真实 FastAPI route + DB 测试覆盖 list -> update -> delete 链路。

2026-07-01 Obsidian 插件补测：插件生产构建通过。按插件实际 HTTP 形状验证了匿名本地 server 的 Obsidian sync/search 链路：`DELETE /api/content/type/markdown?client=obsidian`、`PATCH /api/content?client=obsidian` 上传 `interview/redis.md`、`GET /api/content/computer?client=obsidian`、带空 `Authorization: Bearer ` 的 `GET /api/search?...&client=obsidian` 均真实通过，搜索命中上传笔记。另确认当前项目 `.env` 配置了 `KHOJ_LOCAL_KB_PATH=/data/ljr/my_project/面试胜利！` 时，`/api/search` 会按 Phase 5 设计优先走本地 vault file-first evidence，而不是插件上传的 DB index；因此后续在 Obsidian 客户端内验收时要区分“本地 vault 直读模式”和“插件同步索引模式”。

2026-07-01 Content API 边界 review 补充：Settings 的 disconnect source 和 Obsidian regenerate sync 都会调用内容删除 API。`DELETE /api/content/type/{content_type}` 和 `DELETE /api/content/source/{content_source}` 原先对非法客户端输入抛 `ValueError`，会变成 500；现在改为标准 400 `HTTPException`，并补充真实 API route 测试和临时服务 HTTP 验证。

2026-07-01 Obsidian Chat Agent review 补充：Obsidian 插件有两条建会话路径：点新会话按钮用 query `agent_slug`，但用户第一条消息自动建会话时把 `agent_slug` 放在 JSON body。后端 `/api/chat/sessions` 原先只读 query，导致用户选了自定义 agent 后，首轮消息创建的会话会静默落到默认 Agent。现在 `/api/chat/sessions` 同时接受 query/body，query 优先；补充真实 FastAPI route + DB 测试覆盖 body、query、空 body 默认会话三条路径。额外发现服务器默认 Web build 会在大量 Next worker 下完成导出后不自然退出；代码构建本身通过，后续服务器/CI 建议固定 worker cap。

2026-07-01 QQBot 写入安全 review 补充：QQBot 默认禁写是正确边界，但 Notes tool loop 之前只把“QQBot client writes are disabled”返回给内部 planner，没有作为 write reference 交给 `/api/chat` 主回答层；纯 `/notes` 写入请求会被吞成泛化的“找不到 Notes evidence”。现在 QQBot 禁写和写入工具的 `LocalKBError` 都会生成 `append_note/propose_edit` 的 blocked/failed write reference，主回答上下文只要求报告真实状态，只有 `written` 才禁止说不可写。

2026-07-01 Search 文件详情 review 补充：搜索页文件卡片读取内容和删除文件时直接把 `file_name` 拼进 query string，文件名包含 `#`、`&`、`?` 时浏览器会截断或拆分参数，导致真实 Obsidian/本地笔记打不开或删不掉。现在 Search 页读取和删除都用 `encodeURIComponent(fileName)`；后端真实 route + DB 测试覆盖编码后的特殊文件名读删。

2026-07-01 会话标题 review 补充：Web `AllConversations` 和 Obsidian 插件重命名会话时把用户输入标题直接拼到 `/api/chat/title?...&title=...`，标题包含 `&`、`#`、`?` 会被浏览器截断，导致保存标题和用户输入不一致。现在 Web/Obsidian 标题更新都编码 `conversation_id` 和 `title`；生成标题路径也编码 `conversation_id`；后端真实 route + DB 测试覆盖编码后的特殊标题完整保存。

2026-07-01 Agent slug review 补充：Agent 首次保存时原先只把空格替换成 `-`，名称里的 `/`、`&`、`#`、`?` 和非 ASCII 字符会直接进入 slug，导致 `/api/agents/{slug}` 详情/删除路径、分享链接和前端跳转不稳定。现在生成 slug 使用 Django `slugify()` 并保留随机后缀，空结果回退为 `agent-xxxxxx`；后端真实 route + DB 测试覆盖 `R&D / 面试 #1?` 生成 URL-safe slug 并可通过详情接口读取。

2026-07-01 Agent 写权限 review 补充：Agent API 原先把“可访问 public/protected agent”混同为“可修改 agent”。非创建者对 public agent 发 `PATCH /api/agents` 会得到 200 并悄悄创建一个同名意图的新个人 agent；`DELETE /api/agents/{slug}` 会返回删除成功但实际未删除。现在写操作明确要求 `creator_id == user.id`，非 owner 返回 404；只读 `GET /api/agents/{slug}` 仍允许访问 public/protected agent。真实 route + DB 测试覆盖非 owner 可读、不可改、不可删三条路径。

2026-07-01 Agent 模型配置 review 补充：创建/更新普通和 hidden agent 的四条路径原先各自用 friendly name 查 `ChatModel` 后直接访问 `price_tier/name`，当前端模型选项过期或请求传入不存在的 `chat_model` 时会抛 `AttributeError` 变成 500。现在统一通过 `_resolve_agent_chat_model()` 解析：空值沿用原有默认模型 fallback，明确不存在的模型返回 400 `Unknown chat model`；真实 API 测试覆盖创建 agent 时传入不存在模型不再 500。

2026-07-01 Hidden Agent 写权限 review 补充：`PATCH /api/agents/hidden` 原先只确认 slug 可访问，未确认它真的是当前用户的 hidden agent；用户自己的普通 agent 可被该接口改成 hidden agent，覆盖 persona、privacy 和工具配置。现在 hidden update 必须同时满足 `creator_id == user.id` 且 `is_hidden=true`，普通 agent 返回 400 且不被修改；真实 route + DB 测试覆盖普通 agent 不会被 hidden update 覆盖。

2026-07-01 模型选择 API review 补充：传统 DB 模型运行时的 `POST /api/model/chat?id=...` 原先直接 `int(id)`，非数字 id 会抛 `ValueError` 变成 500；Codex runtime 的未知 option 仍按原逻辑返回 404。现在传统分支先解析 `chat_model_id`，非法 id 返回 400 `Invalid chat model id`，并复用解析后的整数更新用户模型配置；真实 API 测试覆盖非数字 id。

2026-07-01 模型配置空状态 review 补充：传统 DB 模型运行时的 `GET /api/model/chat` 原先在没有用户配置且没有默认 `ChatModel` 时继续访问 `chat_model.id`，空模型表会 500。现在返回 404 `Chat model not found`；真实 API 测试覆盖无 ChatModel 配置时不再崩溃。

2026-07-01 分页 API review 补充：`GET /api/content/files?page=-1` 原先把负页码直接变成 Django 负 slice，触发 `Negative indexing is not supported` 500；现在用 FastAPI `Query(ge=0)` 在路由层拦截。`GET /api/chat/export?page=N` 原先把 `page` 当 slice 起点而不是页码，前端导出 `page=0,1,2...` 会产生大量重复并漏导会话；现在 adapter 用 `page * 10` 计算 offset，并按 `created_at,id` 稳定排序。真实 route + DB 测试覆盖负页码和 12 条会话两页导出无重叠。

2026-07-01 公开会话分享 API review 补充：`POST /api/chat/share`、`POST /api/chat/share/fork` 和 `DELETE /api/chat/share` 原先在 conversation id 或 public slug 不存在时继续把 `None` 传进复制/删除逻辑，导致用户传错链接或删除已失效分享时返回 500。现在分享、fork、删除三条入口都把缺失记录转成 404 `Conversation not found`；`PublicConversationAdapters.delete_public_conversation_by_slug()` 对空记录返回 `False`，不再对 `None` 调 `.delete()`。真实 route + DB 测试覆盖三条缺失记录路径，先红后绿。

2026-07-01 聊天会话 API 边界 review 补充：`POST /api/chat/title` 原先先访问 `conversation.title` 再判断会话是否存在，非法或缺失 conversation id 会 500；`PATCH /api/chat/title` 和 `DELETE /api/chat/history` 原先把非法 UUID 直接传给 Django UUIDField lookup，也会 500；`DELETE /api/chat/conversation/message` 原先只要会话存在就返回成功，即使 turnId 不存在且没有删除任何消息。现在标题生成先判空并返回 404；标题设置和清空历史在 adapter 层识别非法 UUID 并按原有 no-op 语义返回；单条消息删除只有实际移除 turnId 才返回 200，否则返回 404。真实 route + DB 测试覆盖四条缺失/非法路径，先红后绿；另补正向测试确认同一 turnId 的消息被删除、其他 turn 保留。

2026-07-01 同步 Agent 会话 adapter review 补充：`ConversationAdapters.create_conversation_session()` 的同步 agent_slug 分支原先调用异步 `AgentAdapters.aget_readonly_agent_by_slug()` 且没有 await，会把 coroutine 当成 `Conversation.agent` 外键写入并抛 `ValueError`。虽然当前 HTTP 建会话走 async 分支、automation 同步分支只传 title，这个 public adapter 一旦被同步调用就会炸。现在新增同步 `AgentAdapters.get_readonly_agent_by_slug()` 并让同步建会话使用它；真实 DB adapter 测试覆盖 private agent slug 创建会话，先红后绿。

2026-07-01 Agent 删除 adapter review 补充：`AgentAdapters.adelete_agent_by_slug()` 原先假设 `aget_agent_by_slug()` 一定返回 Agent，缺失 slug 会访问 `None.creator` 触发 500。虽然 HTTP 删除路由已先做 404 判空，adapter 直接调用仍不稳。现在缺失或非 owner 都返回 `False`；真实 async adapter 测试覆盖缺失 slug，先红后绿。

2026-07-01 Agent 只读权限 review 补充：`AgentAdapters.get_readonly_agent_by_slug()` / `aget_readonly_agent_by_slug()` 原先在 `user=None` 时仍拼入 `Q(creator=user)`，会把 `creator=NULL` 的 admin-managed private agent 当成可读对象，未登录用户可通过 `/api/agents/{slug}` 读到私有 agent 配置。现在只有有真实 user 时才允许 owner 分支，未登录只允许 public/protected；真实 route 测试覆盖未登录不可读 admin private agent，并确认非 owner 仍可读 public agent。

2026-07-01 Agent async 查询权限 review 补充：`AgentAdapters.aget_agent_by_slug()` / `aget_agent_by_name()` 和同步 `get_agent_by_slug()` 语义不一致，匿名 `user=None` 时仍会拼出 `Q(creator=None)`，让 creator 为空的 private admin agent 被 async adapter 返回。现在 async slug/name 查询都先构造 public-only filter，只有真实 user 才加入 owner 分支；新增 async adapter 红绿测试覆盖匿名 slug/name 查询均不能读到 admin private agent。

2026-07-01 Agent 付费模型选择 review 补充：`POST/PATCH /api/agents` 选择一个存在但当前账号无权使用的 paid chat model 时，`_resolve_agent_chat_model()` 原先返回 `(None, None)`，`AgentAdapters.aupdate_agent()` 随后把 `None` 当作“使用默认模型”，导致请求 200 成功但 agent 实际不是用户选择的模型。现在无权限模型返回 403，阻止静默降级和错误配置；真实 route 测试在 billing 开启且订阅过期账号下先红后绿，确认不会创建 agent。

2026-07-01 Agent choice 输入校验 review 补充：`ModifyAgentBody` 原先把 `privacy_level`、style、input/output tools 都作为裸字符串/列表接收，Django model `choices` 不会在 `save()` 时自动校验；非法 `privacy_level` 可通过 `/api/agents` 返回 200 并落库，后续权限判断只认固定枚举，造成不可预期 agent 状态。现在 create/update/hidden agent 入口统一校验 Agent choices，非法值返回 400；真实 route 测试覆盖非法 privacy 先红后绿。

2026-07-01 Notes/OpenKB 权限边界 review 补充：`collect_notes_evidence_with_tools()` 原先即使 `allow_local_kb=False`，只要 `KHOJ_LOCAL_KB_PATH` 存在，仍会扫描并注入 vault 根目录 profile（如 `agents.md`）和本地 `.codex/skills/*/SKILL.md` catalog，OpenKB-only 模式也可能读到本地 vault 指令。现在只有 local KB 真正允许时才扫描 local skills、注入 local profile、暴露 `read_skill`；真实红绿测试覆盖本地 KB 禁用时 system prompt 不含私有 profile/skill，且 `read_skill` 返回 unavailable。

2026-07-01 OpenKB root jail review 补充：`wiki_search_documents()` 的候选文件由 `wiki/{summaries,concepts,entities,explorations}/*.md` 直接 glob 后读取，绕过 `resolve_openkb_wiki_path()`，wiki 内 symlink 指向外部文件时会读取/抛出外部路径；`save_exploration()` 在 `wiki/explorations` 是外部 symlink 目录时会把 exploration 写出 wiki root。现在新增共享 `_is_safe_wiki_child()`，搜索候选和 exploration 写入目录都必须解析后仍在 wiki root 内；真实红绿测试覆盖 symlink 逃逸搜索不泄露、symlink exploration 目录不写出 root。

2026-07-01 OpenKB/local KB fallback 写入边界 review 补充：`save_exploration()` 在 OpenKB 未启用时会 fallback 到本地 KB 的 `review/explorations`，但原先直接拼 `local_root / "review" / "explorations"` 后 `mkdir/write`，如果 `review` 是指向 vault 外的 symlink，会把 exploration 写出本地 KB root。现在 fallback 目录也复用 `resolve_local_kb_path("review/explorations")`，本地 KB root jail 统一拦截 symlink/越界路径，并把 `LocalKBError` 转成 `OpenKBError`；真实红绿测试覆盖 symlink review 目录不会写出 root。

2026-07-01 Local KB 目录遍历边界 review 补充：`kb_list()` 原先只对文件候选调用 `_allowed_file()`，目录候选只看 `item.is_dir()`；vault 内的目录 symlink 指向 root 外时会进入 candidates，排序阶段调用 `local_kb_relative_path()` 抛 `ValueError`，上层 list 工具/API 可能 500。现在新增 `_allowed_directory()`，目录和文件一样必须解析后仍在 root 内且非隐藏；真实红绿测试覆盖外部目录 symlink 被跳过且不会崩溃。

2026-07-01 Local KB link resolve 未配置边界 review 补充：`kb_resolve_link()` 原先在本地 KB 未配置时捕获 `resolve_local_kb_path()` 的 `LocalKBError` 后把 `from_file` 设成 `None`，随后访问 `from_file.is_file()` 抛内部 `AttributeError`。这会让 Notes 工具层无法拿到统一的 `not_configured` 错误，用户看到的可能是脏 500/内部异常。现在需要本地 root 的解析分支入口直接抛 `LocalKBError(kind="not_configured")`；真实红绿测试覆盖未配置 root，且相邻 Obsidian/Markdown link resolve 正常。

2026-07-01 Notes local skill 读取边界 review 补充：`collect_notes_evidence_with_tools()` 扫描本地 `.codex/skills` / `skills` 时会校验 `SKILL.md` 在 vault root 内，但 `read_skill` 执行阶段原先直接读取扫描时保存的原始路径；如果文件在扫描后被替换成指向 vault 外的 symlink，下一轮 planner prompt 会收到库外内容。现在 `_read_skill()` 在每次读取前重新 resolve 当前 KB root、skill 目录和 `SKILL.md`，要求文件仍在 KB root 内且父目录仍是扫描到的 skill 目录，否则返回 `Local skill is not readable`，不把内容注入 planner。验证：先红后绿 `uv run pytest tests/test_notes_tool_loop.py::test_notes_tool_loop_revalidates_skill_path_before_read -q`；局部 `uv run pytest tests/test_notes_tool_loop.py -q` 通过 17 项；`uv run ruff check src/khoj/processor/conversation/notes_tool_loop.py tests/test_notes_tool_loop.py` 和 `uv run ruff format --check ...` 通过；全量 `uv run pytest -q` 通过 `341 passed, 12 skipped`。

2026-07-01 Search 页文件列表路径 review 补充：`src/interface/web/app/search/page.tsx` 的文件列表分页请求原先使用 `fetch("api/content/files?...")`，在真实 `/search` 页面会解析成 `/search/api/content/files`，导致文件列表加载失败，而不是请求后端 `/api/content/files`。现在改为绝对路径 `fetch("/api/content/files?...")`；全局扫描确认没有剩余 `fetch("api/...")` / `url = \`api/...\`` 写法。验证：`npx prettier --check app/search/page.tsx`、`npx next lint`、`npx tsc --noEmit`、`npx next build` 全部通过。当前环境的 `/snap/bin/bun` 因项目位于 `/data` 被 snap home 限制拒绝访问，因此验证改用本地 `node_modules` / `npx`。

2026-07-01 Content sync 参数边界 review 补充：`PUT/PATCH /api/content?t=...` 原先把 `t` 声明为 `Union[SearchType, str]`，非法内容类型会绕过 FastAPI 枚举校验进入 `configure_content()`，随后被 indexer 包成 500。现在路由和 indexer 的 `t` 都收窄为 `state.SearchType`，非法值在请求边界返回 422，`github` 等现有枚举值仍保持原行为。验证：先红后绿 `uv run pytest tests/test_client.py::test_index_update_with_invalid_content_type -q`；相邻 `github`/有效类型测试通过；`uv run pytest tests/test_client.py -q` 通过 `68 passed, 1 skipped`；全量 `uv run pytest -q` 通过 `342 passed, 12 skipped`。

2026-07-01 Search 页文件筛选交互 review 补充：文件筛选下拉选中某个文件后，页面原先先 `setSearchQuery(newQuery)` 再立刻调用 `search()`，但 `search()` 读取的是旧 React state，导致第一次请求仍使用未加 `file:"..."` 的旧 query；同时搜索请求失败时只 `console.error`，`searchResultsLoading` 不会复位。现在 `search(query = searchQuery)` 接受本次 query 快照，debounce、回车和文件建议选择都传入当前值，失败路径用 `finally` 复位 loading。验证：`npx prettier --check app/search/page.tsx`、`npx next lint`、`npx tsc --noEmit`、`npx next build` 全部通过。

2026-07-01 URLSearchParams double-decode review 补充：`useSearchParams().get()` / `URLSearchParams.get()` 返回值已由浏览器解码，首页 `q` 参数和 Automations 分享链接入口又调用 `decodeURIComponent()`，当用户内容包含字面 `%`（例如 `100% ready` 或 `drop > 5%`）时会抛 `URIError: URI malformed`，导致页面渲染失败。现在这两处直接使用 `searchParams.get()` 返回值，保留创建分享链接时的 `encodeURIComponent()`。验证：Node 复现确认旧逻辑会对 `%` 抛错；`npx prettier --check app/automations/page.tsx app/page.tsx`、`npx next lint`、`npx tsc --noEmit`、`npx next build` 全部通过。

2026-07-01 Settings 姓名保存编码 review 补充：Settings 页 `saveName()` 原先把用户输入直接拼到 `/api/user/name?name=${name}`，姓名包含 `&`、`#`、`?` 时浏览器会截断 query，例如 `A&B` 只会传到后端为 `A`。现在前端用 `encodeURIComponent(name)`；后端真实 route 测试覆盖编码后的 `A&B` 会完整写入 `KhojUser.first_name`。验证：`uv run pytest tests/test_client.py::test_set_user_name_accepts_encoded_special_characters -q`、`uv run pytest tests/test_client.py -q` 通过 `69 passed, 1 skipped`；`npx prettier --check app/settings/page.tsx`、`npx next lint`、`npx tsc --noEmit`、`npx next build` 通过；全量 `uv run pytest -q` 通过 `343 passed, 12 skipped`。

2026-07-01 ModelSelector 初始化副作用 review 补充：`ModelSelector` 原先在 `selectedModel` 初始化后通过 effect 调用 `onSelect(selectedModel)`。在 ChatSidebar 默认 Agent 场景，`onSelect` 会 POST `/api/model/chat`，导致用户仅打开模型侧栏就自动提交一次“切换当前模型”的请求，污染 telemetry，并在 Codex runtime 下重复写环境模型选择。现在 `onSelect(model)` 只在用户实际点击选择模型时触发；非默认 agent 的初始模型仍由 ChatSidebar 的 `setupAgentData()` 管理。验证：源码扫描确认不再存在 `onSelect(selectedModel)` 初始化回调；`npx prettier --check app/common/modelSelector.tsx`、`npx next lint`、`npx tsc --noEmit`、`npx next build` 全部通过。

2026-07-01 Local KB profile cap 配置 review 补充：`load_local_kb_profile_references()` 原先直接 `int()` 解析 `KHOJ_LOCAL_KB_PROFILE_MAX_FILES/CHARS` 和旧 `KHOJ_LOCAL_VAULT_*` 环境变量；`.env` 写成非数字时会在 agent prompt/profile 注入前抛 `ValueError`，导致 Notes/Obsidian agent 启动证据阶段失败。现在只读取新的 `KHOJ_LOCAL_KB_PROFILE_*` cap，非法值回退到函数默认值，旧 `KHOJ_LOCAL_VAULT_*` cap 不再生效；真实红绿测试覆盖无效 env 不崩溃、仍读取 `agents.md`，以及旧 cap 不会限制 profile 文件数。

2026-07-01 Notes source availability review 补充：`/api/chat` 在 `KHOJ_KB_ENGINE=openkb` 但 OpenKB 未 ready、同时机器上还配置了 `KHOJ_LOCAL_KB_PATH` 时，原先不会启用任何 Notes evidence 工具，但 no-evidence 判断只看“有没有 local root”，导致 `/notes` 命令被移除并退回普通聊天生成。现在 `/api/chat` 先计算 `notes_local_source_available` / `notes_openkb_source_available`，工具执行和 no-evidence 分支复用同一判断；Notes-only 请求在没有可用 evidence source 时直接返回 no-entries 提示，不调用最终通用生成。验证：新增红绿测试 `test_chat_notes_openkb_not_ready_with_local_root_does_not_fall_back_to_general`，相邻 file-first miss、file-first evidence 和 OpenKB evidence 测试通过；`uv run pytest tests/test_api_chat_file_kb.py -q` 返回 `9 passed`。

2026-07-01 Automation legacy metadata review 补充：`AutomationAdapters.get_automation_metadata()` 和 `PUT /api/automation` 原先都假设 APScheduler job `name` 一定是合法 JSON；历史任务、外部写入或损坏的 job name 会让 `/api/automation` 列表和编辑直接抛 `JSONDecodeError` 500，且删除也可能因为取 metadata 失败而无法清理。现在新增共享 `AutomationAdapters.parse_automation_metadata()`，合法 JSON 优先，缺字段回退到 job kwargs，最后才回退到 job name/id；列表不再崩，编辑会把 metadata 修回合法 JSON。真实 route 测试覆盖 legacy plain name 的 list/edit 两条链路，先红后绿。

2026-07-01 Automation create rollback review 补充：`POST /api/automation` 原先先创建 “Automation: …” conversation，再调用 APScheduler 创建任务；如果 scheduler 因重复任务、存储异常或运行时不可用抛错，接口返回 500，但刚创建的 conversation 会留在用户会话列表里，形成失败操作的残留状态。现在 schedule 创建失败时立即删除本次创建的 conversation，再返回原 500 文案。验证：新增红绿测试 `test_create_automation_cleans_conversation_when_schedule_fails`，并运行 `uv run pytest tests/test_api_automation.py -q` 返回 `7 passed, 5 skipped`；`uv run ruff check src/khoj/routers/api_automation.py tests/test_api_automation.py` 和 `git diff --check` 通过。

2026-07-01 Content API 清空残留 review 补充：`DELETE /api/content/type/all` 原先只删除 `Entry` 索引，不删除 `FileObject` 原文对象；设置页/插件执行“清空全部内容”后，`/api/content/files` 和 `/api/content/file` 仍可能看到 stale 文件内容。现在 `all` 分支同步删除用户全部 FileObject，再删除 Entry；真实 route 测试覆盖清空前文件可见、清空后文件列表为空，先红后绿。

2026-07-01 Content convert 附件编码 review 补充：聊天输入区上传附件会调用 `/api/content/convert`。原先 `text/plain` 未显式归类为 plaintext，短文本或非 UTF-8 文本会依赖 Magika 误判成 unsupported，返回空转换结果；即使被识别为文本，无 charset 的坏 UTF-8 字节也可能在解码时失败。现在 `get_file_type()` 明确支持 `text/plain`，文本上传统一用 UTF-8 replacement fallback 解码，PDF 仍保留原始 bytes 进入 PDF parser；真实 multipart route 测试覆盖 Latin-1 风格坏字节文本转换为 `caf�`，先红后绿。

2026-07-01 上传 MIME 缺失边界 review 补充：`get_file_type()` 原先类型签名和实现都假设 `UploadFile.content_type` 一定是字符串，直接执行 `";" in file_type`；异常 multipart、插件客户端或测试构造传入 `None` 时会在内容推断前抛 `TypeError`，让上传/索引链路 500。现在缺失 MIME 先归一为空字符串，继续走 Magika 内容推断；真实 helper 红绿测试覆盖 `None` MIME 文本推断为 plaintext，并用 `/api/content/convert` 上传用例确认 route 层仍正常。

2026-07-01 Automations 页面权限 review 补充：`/automations` 是用户私有自动任务页面，但原先没有 `@requires(["authenticated"])`，非 anonymous 且未登录时会直接返回 200 页面壳，随后页面内 API 再失败，造成私有功能入口和登录流程不一致。现在 `/automations` 与 `/settings` 一样要求认证，未登录时 303 到 `/login`；真实 route 测试覆盖非 anonymous 未登录访问先红后绿。

2026-07-01 Home static 非文件边界 review 补充：`/home/{file_path}` 原先只做 root jail，不确认目标存在且是普通文件；请求 landing page 静态目录或缺失资源会把目录/不存在路径交给 Starlette `FileResponse`，在响应发送阶段抛 `RuntimeError`，表现为 500/测试异常。现在路由入口统一拒绝解析异常、越界路径、目录和缺失文件，返回 404；真实 route 测试覆盖正常文件仍可读取、目录返回 404、缺失文件返回 404，先红后绿。

2026-07-01 Search API 参数 review 补充：`GET /api/search?n=-1` 原先会把负数 limit 传进 evidence/索引结果切片，导致返回数量语义不可靠；`n=0` 还会被 `n or 5` 悄悄改成默认 5。现在 `n` 使用 FastAPI `Query(5, ge=1)` 在入口层拦截，负数和 0 均返回 422；真实 route 测试覆盖负数 limit，先红后绿。

2026-07-01 OpenAI-level Agent E2E review 补充：真实服务测试发现三处会直接影响用户预期的问题。第一，官方 Obsidian skill 常见路径是 `skills/note-taking/obsidian/SKILL.md`，旧 scanner 只扫一层 `skills/*/SKILL.md`，导致 agent 看不到官方 Obsidian 规则，写日记/文档时缺 frontmatter、wikilink 和 daily note 约束；现在扫描 `.codex/skills` 与 `skills` 下最多三层 `SKILL.md`，并保留 root jail、隐藏目录和 symlink 逃逸拦截。第二，Notes planner 原来只把工具 schema 写进 prompt 文本，且一度尝试 Codex native tools；当前 Codex Responses 对 native tools 返回 `output=[]/output_text=''`，会让 `/notes` 写日记直接失败。现在 Notes planner 明确使用 `json_object` 文本协议：模型仍负责选择 `read_skill/view_file/append_note/propose_edit` 和生成写入内容，但必须返回 `{"calls":[...]}`，后端只负责执行工具和权限边界，不硬编码日记内容。第三，非流式 `/api/chat` 对非法 `conversation_id` 原先把 “Conversation ... not found” 塞进 200 body，客户端会误判成功；现在受控 not-found 响应返回 HTTP 404。另修复 WebSocket thought debounce 双 flush 导致 thought 内容丢失的问题。

2026-07-01 Streaming Chat API review 补充：继续补测 HTTP streaming 后发现同一个 conversation 缺失契约在 `stream=true` 下仍会返回 200，因为 `StreamingResponse` 会先建立响应，再由 generator 把 “Conversation ... not found” 当普通消息吐出。现在 `/api/chat` route 在创建 stream/non-stream iterator 前统一预检 `conversation_id`，缺失或非法会话直接返回同形状 JSON 404；后续 generator 仍保留防御性处理，但正常客户端不再把缺失会话当成功流。

2026-07-01 WebSocket message buffer review 补充：上一轮只覆盖了 thought debounce，继续审阅普通 message debounce 时发现 `delayed_flush()` 定义缩进在 `if message_buffer.timeout` 内；第一条短文本 chunk 进入 WebSocket 时没有旧 timeout，随后 `asyncio.create_task(delayed_flush())` 会触发未定义变量并走 internal error。现在把 `delayed_flush()` 定义提升到 timeout cancel 之后，首个短消息也能正常 debounce flush。

2026-07-01 Chat event stream predicate review 补充：`send_event()` 原先用 `event_type == ChatEvent.REFERENCES or ChatEvent.METADATA or stream` 判断结构化事件，Python 会把它解析成永真条件；非流式聚合路径因此也会写入所有 status/start/end 控制事件，只是当前 `read_chat_stream()` 恰好忽略它们。现在改成显式 `_should_emit_structured_event()`：流式继续发送全部控制事件；非流式只保留聚合器需要的 references、generated assets、metadata 和 usage，避免协议噪音继续靠偶然忽略兜底。

2026-07-01 Chat file-filter API review 补充：`GET /api/chat/conversation/file-filters/{id}` 对缺失会话返回 404，但 add/remove file filter 四条写接口原先由 adapter 把“会话不存在”和“筛选器为空”都返回 `[]`，前端会把不存在会话上的保存/删除误判为成功。现在 `ConversationAdapters.add_files_to_filter()` / `remove_files_from_filter()` 用 `None` 表示 missing conversation，四条写接口统一返回 404 `Conversation not found`，真实空筛选器仍返回 `[]`。

2026-07-01 Search / memory empty-query review 补充：词法搜索和 memory 搜索在 `query_terms()` 为空时原先返回固定距离 `1.0`，导致 `/api/search?q=`、空白搜索或空白 memory lookup 会返回用户内容/记忆的前几条。Web/Obsidian 前端虽有空输入拦截，但 API 和内部检索边界不能依赖前端。现在 `/api/search` 对空白 query 直接返回 `[]`，`text_search` 和 `UserMemoryAdapters.search_memories()` 的空 terms 不再命中任何记录，避免把“没有查询”误当作“查询全部”。

2026-07-01 Codex Agent sidebar review 补充：真实 Codex runtime 空 DB 下没有传统 DB `ChatModel`，`create_default_agent()` 会跳过创建默认 Agent；新会话再调用 `/api/agents/conversation` 时，conversation 没有关联 agent，默认 agent 也不存在，接口返回 404，聊天侧边栏无法加载当前 agent 配置。现在仅在 Codex runtime 下返回虚拟默认 `khoj` agent packet，`chat_model` 来自当前 Codex model；传统 DB 模型运行时仍保留缺配置报错，不静默掩盖配置问题。

2026-07-01 Agent list missing-default review 补充：继续审阅 Agent API 时发现 `/api/agents` 假设默认 `khoj` agent 一定存在，直接访问 `default_agent.slug`；在默认 agent 被删除、创建失败或迁移后只剩用户 agent 的库里，Agent 列表会 500，前端 Agents 页无法打开。现在列表序列化只在默认 agent 存在时置顶它，并统一通过 `_agent_chat_model_name()` 渲染 chat model 名称，避免同类 `.friendly_name` 空引用散落在多个 create/update/read route 中。

2026-07-01 Codex hidden agent save review 补充：侧边栏从虚拟默认 `khoj` agent 保存自定义 prompt/tools 时会走 `POST /api/agents/hidden`；前端默认不会传 `chat_model`，后端原先 fallback 到传统 DB default `ChatModel`，Codex 空 DB 下会把 `None` 传入 Agent 外键并 500。现在持久化 Agent 的 create/update/hidden 路径在 Codex runtime 且未提供 DB chat model 时直接返回 400 `Agent editing requires a configured database chat model.`；这不影响虚拟默认 agent 读取，也不假装已保存一个无法落库的 agent。

2026-07-01 Notes tool argument parsing review 补充：真实 LLM 工具协议常把 `arguments` 作为 JSON 字符串返回，旧 `_parse_tool_calls()` 会把字符串原样塞进 `ToolCall.args`，执行 `view_file` / `append_note` 时对字符串调用 `.get()` 直接崩溃。现在在工具调用解析入口把字符串 `args/arguments` 解析成 dict，解析失败或非对象时降为空 dict；所有 Notes 工具共享这一处修复。

2026-07-01 Hidden agent default model fallback review 补充：传统 DB runtime 下 `POST /api/agents/hidden` 允许不传 `chat_model` 并回退默认模型，但 `AgentAdapters.aupdate_agent()` 原先把 `aget_default_chat_model()` 返回的 `ChatModel` 对象再按 `name` 查询一次；当 `friendly_name` 与 `name` 不同时查询为空，随后创建 agent 时 `chat_model_id=null` 触发 500。现在有显式 `chat_model` 才按 name 查；没有显式模型时直接使用默认 `ChatModel` 对象。

2026-07-01 OpenKB local fallback wikilink review 补充：`save_exploration()` 在 OpenKB 不可用时会回退写入本地 vault 的 `review/explorations/`，但旧代码仍用 OpenKB wiki root 扫已存在 wikilink 目标。结果本地 vault 里真实存在的 `[[redis]]` 会被当作 ghost link 剥成纯文本，影响 Obsidian 复盘笔记质量。现在保存到 OpenKB 时按 wiki root 判定链接，回退本地 vault 时按 local KB root 判定链接，只剥离实际不存在的 wikilink。

2026-07-01 OpenKB exploration save failure review 补充：`/api/chat` 在显式要求保存 OpenKB exploration 时，`save_exploration()` 的安全错误会直接冒泡出 streaming generator；用户可能已经收到回答正文，但聊天流随后断开，没有结束事件，也无法知道写入失败。现在路由层只捕获受控 `OpenKBError`，把失败转成 `status` 事件并继续发送 `end_response`，OpenKB/root jail 安全校验保持强硬拒绝。

2026-07-01 Local KB nested note creation review 补充：`append_note` 允许创建新笔记文件，但旧实现要求父目录预先存在；真实 Obsidian 日记路径如 `daily/2026-07-01.md` 在新 vault 或新文件夹里会直接失败，导致 agent 声称能写日记却无法落盘。现在安全路径、后缀、hidden/root jail 校验保持不变，写入开启时会为新笔记创建缺失父目录；写入关闭时仍不创建任何目录或文件。

2026-07-01 Automation stale conversation trigger review 补充：手动触发 automation 时，旧代码拿到 job 后直接开后台线程并返回 200；如果 job metadata 里的 `conversation_id` 已失效或不是 UUID，后台 `scheduled_chat()` 会失败/删除任务，用户端却看到“Automation triggered”的假成功。现在 trigger 入口先同步解析 metadata、校验 conversation；坏任务会被删除并返回 404。共享 `ConversationAdapters.get_conversation_by_id()` 也改成非法 UUID 返回 None，避免同类校验点抛 500。

2026-07-01 ChatSidebar save failure review 补充：后端明确返回 400 后，前端侧栏保存逻辑原先仍会继续 `mutate` 并把 `hasModified` 清掉，且 `res.json()` 没有 return，用户会看到“保存成功”的假象，实际 prompt/tools 没有落库。现在 `ChatSidebar.handleSave()` 先解析响应体，只有 `res.ok` 时才刷新 agent 数据并清除 dirty 状态；失败时保留用户改动、展示错误，并统一结束 saving 状态。

2026-07-01 ChatSidebar create agent failure review 补充：侧栏里的 “Create Agent” modal 原先只检查响应 JSON 是否有 `detail`，不检查 HTTP status，也不识别后端当前常用的 `{error: ...}`；创建失败时会进入半成功状态，隐藏 Create 按钮但不展示错误。现在创建 agent 也按 `res.ok` 处理，读取 `detail/error` 后展示错误并恢复按钮，不再把 400 当成功。

2026-07-01 AgentCard delete failure review 补充：Agents 页面卡片菜单里的 Delete 原先完全不检查 `DELETE /api/agents/{slug}` 返回值；如果后端返回 404/403，前端仍会触发刷新，用户得不到失败原因。现在删除 agent 时检查 `response.ok`，读取后端 `error` 并 alert；同处 catch 也改成展示 `Error.message`，避免把异常对象渲染成 `[object Object]`。

2026-07-01 Chat stream JSON-like text review 补充：Web chat 流式渲染里，如果模型输出普通文本刚好以 `{` 开头并以 `}` 结尾但不是合法 JSON，旧 fallback 会追加 `JSON.stringify(chunkData)`，导致用户看到额外引号和转义字符。现在 JSON 解析失败时保留原始 chunk 文本，只把真正合法的 JSON response/image payload 当结构化消息处理。

2026-07-01 Settings 状态刷新 review 补充：Settings 页保存用户名和清空知识库后原先直接原地修改 `userConfig` 再 `setUserConfig(同一对象)`，React 可能跳过重渲染，造成 Save 按钮、知识库连接状态和 Clear All 按钮显示旧状态。现在改为函数式不可变更新。聊天导出进度也按实际已收到 conversation 数量更新，避免 12 条会话第二页显示 20/12 和超过 100% 的进度；导出 stats/page HTTP 失败时直接进入失败 toast，不再继续打包坏响应。

2026-07-01 分享/会话 query review 补充：Web 新建 agent 会话、分享页 unshare/fork/history、首页/侧栏/ChatHistory/AgentCard 跳转或分享 Agent 管理页时仍有裸 slug 拼 query string 的路径；历史 agent slug 或外部分享 slug 含 `&/#/?` 会被浏览器截断成错误参数。现在这些入口统一编码 slug，侧栏 agent-conversation URL 也编码 conversation id。分享 fork 还补了 HTTP 失败和缺失 `conversation_id` 的拦截，避免 404 后继续写入 `conversationId=undefined` 并跳到坏会话；分享历史读取也先检查 HTTP status 和 response body，再渲染标题/消息。

2026-07-01 会话列表 action review 补充：会话列表里的 rename/share/delete 原先不检查 HTTP 状态，rename 还会在后端返回 `success:false` 时先改本地标题；share 失败会打开空分享链接弹窗，delete 失败也可能刷新列表或跳首页。现在三个 action 都只在真实成功后更新 UI；conversation id 也编码进 query。失败时保留当前界面并提示错误，不再制造假成功。

2026-07-01 Automations 前端 action review 补充：Automations 页保存自动任务原先不检查 HTTP 状态，后端 400/403/500 文本响应会进入 JSON 解析失败分支并关闭编辑弹窗；删除自动任务原先强制 `response.json()`，204 空响应或失败响应会被静默吞掉。现在保存只在 2xx + JSON 成功后关闭并更新卡片，失败时保留表单和 Save 按钮；删除/手动触发都编码 automation id，删除失败会 toast，不再假成功。

2026-07-01 Chat retry/delete review 补充：Chat 的 retry 按钮原先先在本地删掉旧 turn，再异步调用 `DELETE /api/chat/conversation/message`，且不检查 HTTP 状态；后端返回 404 时仍会重发原问题，导致历史里旧 turn 未删、新 turn 又新增，用户界面还误以为旧消息已删除。现在 retry 先等待删除 API 成功，只有 2xx 才从本地历史移除并重发；404/失败会中止 retry 并保留原消息。

2026-07-01 Chat feedback/direct delete review 补充：消息点赞/点踩原先 fire-and-forget 调 `/api/chat/feedback`，接口失败时 UI 仍会切成成功状态；消息菜单直接删除虽然检查了 `response.ok`，但网络错误只进控制台，用户没有失败反馈。现在反馈和直接删除都等待真实 HTTP 成功后再更新本地状态，失败时保留原 UI 并提示错误；后端新增真实 `/api/chat/feedback` route 测试，验证认证用户、请求解析和反馈发送参数，不触发外部邮件副作用。

2026-07-01 Settings Memory UI review 补充：Memory 子组件原先在调用父级 `onUpdate` / `onDelete` 后立刻关闭编辑态并弹成功 toast；如果 `/api/memories/{id}` 返回失败，父级会弹错误 toast，但用户同时看到成功提示。现在父级 handler 返回真实成败，子组件只在 `true` 后更新 UI 和展示成功；父级 memory 列表更新改成函数式 state，避免并发操作使用旧快照。

2026-07-01 Search response-shape review 补充：Search 页初始化文件建议和执行搜索时原先直接 `response.json()` 并把结果塞进 `string[]` / `SearchResult[]` state；认证失败、后端 4xx/5xx 或错误 JSON 对象会被传给 `.map()`，导致页面崩或显示异常。现在这两个 fetch 边界都先检查 `response.ok` 和 `Array.isArray()`，失败时保留空文件建议/空搜索结果，不把错误对象当列表渲染。

2026-07-01 Chat session entrypoint review 补充：侧边栏 New Chat 和 Agent 卡片 Chat 原先各自手写 `/api/chat/sessions` fetch，并在检查 HTTP 状态前先 `response.json()`；如果后端返回 401/403 以外的非 JSON 错误，点击入口会直接抛异常且无提示。现在两个入口都复用 `createNewConversation()`，共享 `response.ok`、conversation id 校验和 agent slug 编码；401/403 仍跳登录页，其它失败展示启动会话失败。

2026-07-01 Agents page response-boundary review 补充：Agents 页原先的 SWR fetcher 会吞掉 `/api/agents` 网络/HTTP 错误，导致页面停留在 loading；如果后端 200 返回非数组，也会在 `data.filter()` 处崩。现在 `/api/agents` fetcher 检查 HTTP 状态和数组形状，失败进入现有 Error loading agents UI；创建 Agent 的失败响应也统一读取 `error/detail` 或 fallback 文案，网络异常不再把 Error 对象塞进 string state；URL 中 protected agent slug 读取时也做编码和 `response.ok` 检查。

2026-07-01 ChatSidebar response-boundary review 补充：聊天侧栏的 SWR fetcher 原先不检查 HTTP 状态，`/api/agents/conversation` 返回 404/500 JSON 时会被当成 `AgentData` 进入 prompt/model/tools 初始化，造成空白或错误配置状态。现在侧栏 fetcher 对非 2xx 抛错，`agentDataError` 会显示 “Failed to load chat options.”，不再把错误对象当作 agent 配置；同轮验证了 Codex 虚拟默认 agent、hidden agent 创建失败和默认模型 fallback 三条后端路径。

2026-07-01 Auth fetcher review 补充：全局 `useAuthenticatedData()` / `useUserConfig()` 共用的 fetcher 原先 catch 所有异常并返回 `undefined`，SWR 不会进入 error 分支；网络错误、500 或非 JSON 错误会被误判成“没数据/未登录”。现在 fetcher 保留 403 `Forbidden` 的匿名语义，其它非 2xx、网络和坏响应都会抛错交给 SWR error，避免全站把服务端故障吞成空配置。

2026-07-01 Automations list response-boundary review 补充：Automations 页自己的 `/api/automation` SWR fetcher 原先把 HTTP、网络和 JSON 错误 `console.log` 后吞掉，SWR 会拿到 `undefined`，页面可能停在 loading 或把服务端故障误表现为无任务。现在 fetcher 对非 2xx 抛错，并拒绝非数组响应，复用页面已有错误分支；同轮跑通 automation API 真实后端测试、web 类型检查、lint 和 production build。

2026-07-01 Home response-boundary review 补充：首页 `/api/agents` fetcher 原先吞掉错误并把任意 JSON 当 agent 列表，`/api/chat/options` 失败时还不会 `setLoading(false)`，导致首页可能永久停在 Loading。现在 agents 读取检查 HTTP 状态和数组形状；chat options 读取无论成功失败都会结束 loading，失败只禁用相关 options，不阻塞首页基础输入体验。同轮跑通 agent/session 后端路径、web 类型检查、lint 和 production build。

2026-07-01 Conversation sidebar response-boundary review 补充：会话侧栏的 `/api/chat/sessions` fetcher 原先不检查 HTTP 状态或数组形状，认证/服务端错误 JSON 会进入 `chatSessions.forEach()` 直接崩；文件菜单的两个列表接口也只检查 HTTP，不验证返回类型。现在这个组件的列表 fetcher 统一要求 2xx + array，会话读取失败显示 “Failed to load conversations”；新增真实后端测试先创建会话再验证 `/api/chat/sessions` 与 `/api/chat/conversation/file-filters/{id}` 都返回列表。

2026-07-01 Chat options response-boundary review 补充：首页、Chat 页和 Share Chat 页都直接读取 `/api/chat/options`；Chat/Share 原先在接口失败时只 `console.error`，不会 `setLoading(false)`，用户会永久停在 Loading。现在三处共用 `fetchChatOptions()`，统一检查 HTTP 状态；页面读取失败仍会退出 loading，只让 chat options 为空，不阻断基础页面渲染。新增真实 `/api/chat/options` shape 测试并跑通 web 类型检查、lint 和 production build。

2026-07-01 Login prompt auth-flow review 补充：登录弹窗的 magic-link 发送路径原先成功后强制 `res.json()`，但后端 `POST /auth/magic` 成功返回 200 空 body；429/404 分支也没有 return 内层 JSON promise，错误可能变成未处理 promise。OTP 校验遇到 500/网络错误只打日志，不给用户反馈。现在 magic-link 发送和 OTP 校验改成 async/await，成功路径不解析空 body，失败路径统一展示用户可见错误；新增真实 auth route 测试覆盖 OAuth metadata、发送 magic code、有效 code 重定向和无效 code 401。

2026-07-01 Settings API key review 补充：Settings API key 管理原先生成 token 时不检查 HTTP 状态和返回形状，服务端错误 JSON 可能被塞进 `apiKeys` 并在 `key.token.slice()` 崩页面；复制/删除按钮也会先弹成功 toast，再执行真实操作，导致剪贴板失败或删除失败时误报成功。现在生成/列表读取要求 2xx 和 `{token,name}` shape，删除编码 token 并只在成功后更新 UI / toast，复制失败会显示错误。新增真实 `/auth/token` 生成、列表、删除端到端测试。

2026-07-01 Settings memory response-shape review 补充：Settings 的 Browse Memories 原先只检查 `/api/memories` HTTP 状态，不验证返回数组和 memory 对象形状；服务端错误 JSON 或坏 payload 会进入 `memories` state，并在弹窗 `memories.map()` / `UserMemory` 渲染时崩。更新单条 memory 也未校验返回对象。现在列表读取要求 `UserMemorySchema[]`，坏响应会清空列表并显示错误 toast；单条更新要求 `{id,raw,created_at}` shape 后才更新本地 state。同轮跑通真实 memory API list/update/delete 测试、web 类型检查、lint 和 production build。

2026-07-01 Settings chat export response-shape review 补充：Settings 的 Export Chats 原先只检查 `/api/chat/stats` 和 `/api/chat/export` HTTP 状态，不验证 `num_conversations` 和每页导出数组；坏 payload 可能生成错误 zip，或在 `conversations.push(...data)` 才抛运行时异常。现在 stats 必须是非负整数，每页必须是 conversation export 记录数组，坏响应会中止导出并 toast 失败；真实后端测试补充验证 `/api/chat/stats` 与两页 `/api/chat/export` 分页不重叠且总数正确。

2026-07-01 Settings model switch review 补充：模型下拉框原先先把 UI 选中值改掉，再调用 `/api/model/chat`；如果后端返回 400/403/404，或者返回 200 但 body 是 `{status:"error"}`，toast 会提示失败但下拉框仍显示成已切换。现在模型切换 callback 返回成功/失败，下拉框失败时回滚到旧选项；成功时同步 `userConfig.selected_chat_model_config`，并把 200/error body 当失败处理。新增真实后端测试覆盖 invalid id、缺失配置和 free model 成功切换后 GET 确认。

2026-07-01 Settings account deletion review 补充：Settings 的 Delete Account 前端已经检查 `DELETE /api/self` 的 HTTP 状态，后端实现保持极简 `request.user.object.delete()`；本轮未发现需要扩实现的 bug。为防止未来 cascade/auth 回归，新增真实 FastAPI route + DB 测试：创建当前用户私有 agent 和 conversation，调用 `/api/self` 后确认当前用户、API token、conversation、agent 均删除，另一个用户仍存在，且旧 token 再访问 `/api/search` 返回 403。下一步继续审 destructive/状态写入类路径，优先找会造成假成功或跨用户影响的缺口。

2026-07-01 Search file response-shape review 补充：Search 页文件分页和文件详情读取原先只检查 HTTP 状态，不校验 `/api/content/files` 的 `{files,num_pages}` 形状和 `/api/content/file` 的 `raw_text` 类型；坏响应会把旧数据或异常对象带进文件列表/预览。现在文件分页要求 `files` 为 `FileObject[]`、`num_pages` 为非负整数，文件详情要求 `raw_text` 为字符串，失败时走现有错误状态。验证：Search 相关真实后端 route 测试、`npx prettier --check app/search/page.tsx`、`npx tsc --noEmit`、`npx next lint`、`timeout 120s npx next build` 均通过。

2026-07-01 Chat attachment indexing review 补充：聊天附件上传的 `uploadDataForIndexing()` 原先调用 `/api/content?client=web` 后不检查 HTTP 状态，后端 413/422/500 也会把文件标记为 uploaded，并可能加入 conversation file filter，让 Agent 以为附件可检索但实际没有入库。空 MIME 的 `.docx` 还会被跳过 FormData append 却同样标记成功。现在只把真正 append 到 FormData 的文件名作为候选，补上 docx MIME fallback，上传接口必须 `response.ok` 后才更新 uploaded files 和 file filter。验证：内容上传相关真实 route 测试、`npx prettier --check app/common/chatFunctions.ts app/search/page.tsx`、`npx tsc --noEmit`、`npx next lint`、`timeout 120s npx next build` 均通过。

2026-07-01 Chat input convert review 补充：聊天输入区附件预览调用 `/api/content/convert` 时，网络错误会跳过 `setUploading(false)`，让输入区一直显示上传状态；200 响应的 body 也没有校验，坏 payload 会被当成 `AttachedFileText[]` 写入下一轮消息的 files。现在转换请求在 `finally` 清理 loading，并要求返回数组内每项都有 `name/content/file_type/size`。验证：转换相关真实后端 route 测试、`npx prettier --check app/components/chatInputArea/chatInputArea.tsx app/common/chatFunctions.ts`、`npx tsc --noEmit`、`npx next lint`、`timeout 120s npx next build` 均通过。

2026-07-01 Chat history response-shape review 补充：ChatHistory 读取普通/分享会话历史时原先只检查 HTTP 状态和 `response` 是否存在，未校验 `chat/slug/conversation_id/is_owner/agent` 的基本形状；公开分享里私有 agent 被后端剥离时 `agent` 可为 null，但前端类型写成必有对象，可能传出 null agent 或生成 `undefined-500` 颜色类。现在 `ChatHistoryData.agent` 与后端对齐为 nullable，历史响应要求 `status:"ok"` 和最小字段形状，只有 agent 对象存在时才更新父级 agent state，并给缺失 agent 的图标/颜色提供默认值。验证：分享/历史相关真实后端 route 测试、前端格式、类型、lint 和 production build 均通过。

2026-07-01 Content API OpenAPI schema review 补充：`GET /api/content/files` 原先声明 `response_model=Dict[str,str]`，但真实返回 `{files: FileObject[], num_pages: number}`；`GET /api/content/file` 声明 `Dict[str,str]`，但真实返回 numeric `id`。运行时因为手写 `Response` 没被校验，但 OpenAPI 和客户端生成会被误导。现在新增 `FilesResponse`、`FileObjectResponse`、`FileDetailResponse`，让 schema 与真实 JSON 对齐；相关真实 route 测试和 `ruff` 均通过。

2026-07-01 Auth test isolation review 补充：综合跑 `tests/test_client.py` 时发现 magic-link 测试依赖全局 `state.billing_enabled` 的历史状态；当前面测试打开 billing 且非 debug 时，新邮箱会走 deliverability 校验，导致 `login-flow@example.com` 返回 404。生产逻辑保持不变，测试显式关闭该分支，只验证 magic link code 发送和 redirect 行为。复跑单条测试通过，随后 `uv run pytest tests/test_client.py -q` 通过 `78 passed, 1 skipped`。

2026-07-01 Model API response contract review 补充：`POST /api/model/chat` 的 adapter 保存失败分支原先返回 200 + `{status:"error"}`，迫使前端额外解析 body 才能知道失败；`GET /api/model/chat/options` 的 response model 也声明成 dict，但真实返回模型列表。现在保存失败返回 404 `Model not found`，并新增 `ChatModelOptionResponse[]` schema 对齐真实列表；新增真实 route 测试用 monkeypatch 模拟 adapter 返回 None，确认不再 200 假成功。

2026-07-01 Chat file preview response-shape review 补充：聊天消息内文件预览的 `useFileContent()` 已检查 HTTP 状态，但没有校验 `/api/content/file` 返回的 `raw_text` 类型；坏 payload 会被当成空文件内容渲染，用户看不到真实错误。现在要求 `raw_text` 为字符串，否则进入现有 error 状态。验证：文件详情/缺失真实 route 测试、前端格式和 `npx tsc --noEmit` 均通过。

2026-07-01 AgentCard delete URL review 补充：AgentCard 删除按钮仍把 `props.data.slug` 直接拼进 `/api/agents/{slug}`；新 slug 已 URL-safe，但历史或外部数据里含 `&/#/?` 时删除请求会截断或落到错误路径。现在删除路径使用 `encodeURIComponent(props.data.slug)`；相关 agent 删除权限/adapter 测试、前端格式和 `npx tsc --noEmit` 通过。

2026-07-01 ChatSidebar model switch review 补充：共享 `ModelSelector` 原先在调用 `onSelect` 前就更新自己的选中状态，ChatSidebar 默认 Agent 模型切换失败时只 `console.error`，侧栏会显示成已切换。现在 `ModelSelector` 允许 `onSelect` 返回 `false` 并只在成功后更新选中项；ChatSidebar 默认模型切换只有 `/api/model/chat` 2xx 后才更新本地 selected model，失败给用户可见提示。Fast mode 切换失败也不再只打 console。补充真实 route 测试覆盖非 Codex runtime 下 `/api/model/chat/fast` 返回 400。

2026-07-01 Shared chat functions response-shape review 补充：`modifyFileFilterForConversation()` 原先只检查 HTTP 状态，坏 JSON 可直接写入会话文件过滤 state；`createNewConversation()` 只检查 `conversation_id` truthy，非字符串 payload 也会被用于跳转。现在文件过滤响应必须是 `string[]`，新会话响应必须给字符串 `conversation_id`。验证：会话创建、侧栏文件过滤和缺失会话真实 route 测试、前端格式和 `npx tsc --noEmit` 均通过。

2026-07-01 Login metadata response-shape review 补充：登录弹窗的 `/auth/oauth/metadata` SWR fetcher 原先不检查 HTTP 状态，也不校验 Google provider 的 `client_id/redirect_uri` 类型；服务端错误 JSON 会被当作 metadata，按钮只表现为不可点。现在 metadata fetcher 要求 2xx 且 Google 字段为字符串，坏响应进入 SWR error；OAuth metadata 和 magic-link 真实 route 测试、前端格式和 `npx tsc --noEmit` 均通过。

2026-07-01 Agent form submit failure review 补充：AgentCard 的创建/更新表单失败分支原先嵌套 `response.json().then(...)` 且不 return，非 JSON 错误不会进入 catch，用户可能看不到保存失败原因。现在表单提交使用 `async/await`，只在 `response.ok` 后关闭弹窗和刷新，失败统一读取 `error/detail` 并展示。验证：未知模型、付费模型、非法 privacy、非创建者更新 public agent 等真实 route 测试、前端格式和 `npx tsc --noEmit` 通过。

2026-07-01 Agents page agent-shape review 补充：Agents 页的 `/api/agents` fetcher 已检查 HTTP 状态和顶层数组，但仍把数组元素和 URL 直链 `/api/agents/{slug}` 详情响应直接断言为 `AgentData`；服务端坏 payload 可能进入 `data.filter()`、卡片渲染或 protected agent 注入，表现为空白页或错误配置。现在列表和详情共用 zod `agentDataSchema`，要求 slug/persona/style/model/tools 等最小字段类型正确；直链详情失败会记录错误并保留主列表，不再产生未处理 Promise。验证：`uv run pytest tests/test_agents.py::test_non_creator_can_read_public_agent tests/test_agents.py::test_unauthenticated_user_cannot_read_admin_private_agent tests/test_agents.py::test_create_agent_rejects_invalid_privacy_level -q` 通过 3 项；`npx prettier --check app/agents/page.tsx`、`npx tsc --noEmit`、`npx next lint`、`timeout 120s npx next build` 均通过。

2026-07-01 Obsidian chat entrypoint response-shape review 补充：Obsidian 插件的建会话、会话列表和 Agent 列表请求原先只看 `response.ok`，不校验 `conversation_id`、sessions 数组或 agent `{name,slug}`；坏响应会把 `undefined` 写入 `dataset.conversationId`、让会话列表静默空白，或把错误对象塞进 Agent selector。现在插件本地用最小 type guard 校验 `conversation_id`、`{conversation_id,slug}[]` 和 `{name,slug}[]`，失败时显示/记录失败并清空 agents，不继续假装成功。验证：`npm run build` 在 `src/interface/obsidian` 通过；`uv run pytest tests/test_client.py::test_create_chat_session_accepts_agent_slug_in_json_body tests/test_client.py::test_sidebar_chat_session_endpoints_return_lists -q` 通过 2 项。

2026-07-01 Obsidian search response-shape review 补充：Obsidian 普通搜索 modal 和 Similar Documents view 原先把 `/api/search` 的任意 JSON 当数组处理，并直接访问 `result.additional.file`；服务端坏 payload 或协议漂移会让插件 UI 在 filter/map 阶段崩溃。现在两处都要求搜索响应是 `{entry:string, additional:{file:string}}[]`，并在相似笔记模式下先稳定读取当前文件路径再过滤自身。验证：`npm run build` 在 `src/interface/obsidian` 通过；`uv run pytest tests/test_client.py::test_notes_search tests/test_client.py::test_search_empty_query_returns_empty tests/test_client.py::test_search_rejects_negative_limit -q` 通过 4 项。

2026-07-01 Obsidian settings/model response-shape review 补充：Obsidian 设置页的模型列表只检查 `/api/model/chat/options` 顶层是否数组，元素缺 `id/name` 时会在 `toString()` 或下拉渲染阶段崩；`/api/settings?detailed=true` 也被直接 cast 成 `ServerUserConfig`。现在模型列表必须由 `{id:string|number,name:string}` 组成，server settings 只保留合法 numeric `selected_chat_model_config`，切换模型时也编码 `id`。验证：`npm run build` 在 `src/interface/obsidian` 通过；`uv run pytest tests/test_client.py::test_update_chat_model_rejects_invalid_id tests/test_client.py::test_get_chat_model_handles_missing_config tests/test_client.py::test_update_chat_model_accepts_free_model tests/test_client.py::test_update_chat_model_reports_adapter_save_failure -q` 通过 4 项。

2026-07-01 Obsidian sync lastSync review 补充：Obsidian 同步成功后原先用 `response.includes(file.path)` 判断哪些文件上传成功；如果 `a.md` 和 `notes/a.md` 这类路径互相包含，会误更新 lastSync，导致后续增量同步跳过实际未成功的文件。现在插件把 `/api/content` 返回的逗号文件名解析成 `Set` 后做精确路径匹配；清空内容类型的 URL path 也编码。验证：`npm run build` 在 `src/interface/obsidian` 通过；`uv run pytest tests/test_client.py::test_index_update tests/test_client.py::test_index_update_fails_if_more_than_1000_files tests/test_client.py::test_delete_all_content_removes_file_objects -q` 通过 3 项。

2026-07-01 Obsidian chat history response-shape review 补充：Obsidian 插件恢复会话时原先在检查 HTTP 状态前解析 JSON，并把 `responseJson.conversation_id` 直接写入 DOM dataset；404/500 或坏 payload 会让插件带着 `undefined` 会话继续渲染。现在 `getChatHistory()` 先检查 `response.ok`，再要求 `{status:"ok", response:{conversation_id:string, chat:[{by,message}]}}` 的最小形状；会话恢复、删除历史的 `conversation_id` 都做 URL 编码。新增真实 route 测试创建 Obsidian client 会话、写入 conversation_log、再 GET `/api/chat/history` 验证插件依赖字段。验证：`npm run build` 在 `src/interface/obsidian` 通过；`uv run pytest tests/test_client.py::test_chat_history_returns_obsidian_session_shape tests/test_client.py::test_sidebar_chat_session_endpoints_return_lists tests/test_client.py::test_create_chat_session_accepts_agent_slug_in_json_body -q` 通过 3 项。

2026-07-01 Obsidian streaming chat review 补充：Obsidian 插件发送 `/api/chat?client=obsidian` 时原先把 `fetch()` 放在 `try` 外，网络失败不会进入 UI 错误提示；非 2xx 响应也会被当作正常 stream 读取，404/403 JSON 可能被渲染成普通 assistant 消息。`readChatStream()` 还没有 await async `processMessageChunk()`，chunk 处理和 edit retry/apply 可能乱序。现在 fetch、HTTP 状态检查和 stream body 检查都在同一个 try 内，非 2xx 走错误 UI；stream 事件按顺序 await 处理。验证：`npm run build` 在 `src/interface/obsidian` 通过；`uv run pytest tests/test_client.py::test_streaming_chat_invalid_conversation_id_returns_not_found tests/test_client.py::test_chat_invalid_conversation_id_returns_not_found tests/test_client.py::test_chat_event_structured_streaming_predicate -q` 通过 3 项。

2026-07-01 Obsidian edit writeback review 补充：Obsidian 插件的 SEARCH/REPLACE 应用路径原先把 diff preview (`==新增==` / `~~删除~~`) 拼进 `newContent` 后写入 vault，用户点击 Apply 时再全文件删除 `==` / `~~` 标记；这会先污染真实文件，并且会破坏用户本来就有的 Obsidian 高亮/删除线语法。现在 `processSingleEdit()` 分离 preview 和写入内容：preview 仍用于展示，`newContent` 只写纯 replacement；Apply 按钮只确认当前写入，Cancel 仍用备份回滚；同时删除不再使用的 normalize 匹配，遵守 SEARCH 必须精确匹配源文本的规则。验证：`npm run build` 在 `src/interface/obsidian` 通过；对构建后的 `main.js` 用最小 fake Obsidian VM 直接调用 `FileInteractions.processSingleEdit()`，确认写入内容保留智能引号和原有 `==highlight==`，且不会把人工 diff 标记写入 `newContent`。

2026-07-01 Obsidian edit target matching review 补充：Obsidian 插件的编辑 prompt 要求模型返回 FULL file path，但实际上下文只给 `# file: basename.md`，执行端还用 Levenshtein 模糊匹配最近文件；如果 vault 里有同名笔记或模型文件名略有拼错，可能把 edit block 写进错误文件。现在上下文暴露真实 `file.path`，执行端只接受精确 path，或唯一的 exact name/basename；basename 歧义和拼写近似都会拒绝写入，同时删除未再使用的 Levenshtein helper。验证：`npm run build` 在 `src/interface/obsidian` 通过；对构建后的 `main.js` 用 fake Obsidian VM 断言 full path 出现在 prompt、`raw/Redis.md` 精确命中、`Rdis.md` 不再模糊命中、两个 `Note.md` 候选会拒绝 basename 歧义。

2026-07-01 Obsidian edit new-file review 补充：Obsidian 插件的 prompt 已告诉模型“空 SEARCH + 新路径”可创建新文件，但执行端仍要求目标必须匹配最近打开文件，导致真实用户让 agent 新建日记/文档时 UI 看似支持、落盘却失败。现在 `parseEditBlocks()` 接受模型常见的空 SEARCH 格式，`applyEditBlocks()` 对安全相对 `.md` 新路径支持创建缺失父目录，并返回 `createdFiles` 供 Cancel 删除；危险路径、非 markdown、已存在但未打开的文件仍强硬拒绝，避免覆盖用户笔记。顺手修正 edit prompt 里的畸形 `<khoj_edit>` 示例，减少模型输出坏 XML 的概率。验证：`npm run build` 在 `src/interface/obsidian` 通过；对构建后的 `main.js` 用 fake Obsidian VM 覆盖从原始 edit block 文本解析、新建 `daily/2026-07-01.md`、已有文件纯写入、多段编辑同一新文件、危险路径拒绝、未打开既有文件拒绝；`uv run ruff check src/khoj tests`、`uv run pytest -q`、`NEXT_TELEMETRY_DISABLED=1 npm run build`、`git diff --check` 均通过。额外临时真实 HTTP E2E 在 `0.0.0.0:12842` 启动匿名 server + 临时 vault，验证 health、chat options、agent options、session 创建、local KB search、Obsidian 上传/列表/读取/删除，以及 `/api/chat /notes` 根据 Redis 笔记生成面试题，输出 `E2E_REAL_HTTP_OK`；服务和临时目录已清理。当前证据仍在工作区待提交；下一步是在真实 Obsidian 客户端里复测同样的“写日记/新建 raw 文档/取消回滚”交互。

2026-07-01 Obsidian edit rollback review 补充：继续审阅新文件写回发现两个原子性边界。第一，新建嵌套文件会创建父目录，但 Cancel 和异常回滚只删除文件，留下本轮创建的空目录；第二，应用阶段先成功写入一块、后续块失败时会回滚文件，但 `editResults` 仍保留早先 success，UI 可能显示“部分成功”。现在 `ensureParentFolders()` 返回本轮创建目录，父路径若是文件会直接拒绝；异常回滚和 Cancel 都只删除仍为空的本轮新建目录；应用阶段失败后清空旧 success，按 atomic 语义把全部 edit block 标为失败。验证：`npm run build` 在 `src/interface/obsidian` 通过；构建后的 `main.js` fake Obsidian VM 覆盖嵌套目录创建记录、中途 create 失败后文件和空目录全部回滚、父路径为文件时拒绝且不写入。

2026-07-01 Obsidian delete-message review 补充：Obsidian 插件删除聊天消息时原先先从 DOM 移除消息，再异步请求 `/api/chat/conversation/message`；如果后端返回 404/500 或网络失败，用户界面已经消失，形成“本地假删除、服务端仍存在”的错觉。现在有 `turnId` 且需要同步后端时，先等待后端删除成功，再按原动画移除消息；失败会撤掉 deleting 状态并提示错误。验证：`npm run build` 在 `src/interface/obsidian` 通过；构建后的 `main.js` fake Obsidian VM 覆盖后端 500 时消息和配对消息仍保留、后端 200 时两条消息才移除。

2026-07-01 Obsidian edit-message review 补充：继续审阅发现 Edit Message 按钮仍走旧假删除：先把当前消息之后的所有 DOM 消息移除，再后台逐条调用删除；同一 turn 的 user/khoj 两条消息还会重复请求同一个 `turn_id`，第二次可能 404。现在编辑旧消息会先按唯一 `turnId` 顺序删除后端历史，全部成功后才移除 UI 并把原问题填回输入框；任一后端删除失败则保留原消息和输入框状态。验证：`npm run build` 在 `src/interface/obsidian` 通过；构建后的 `main.js` fake Obsidian VM 覆盖后端失败时不移除消息/不改输入、后端成功时只请求 `turn-1`/`turn-2` 各一次并删除后续消息。

2026-07-01 Obsidian rename-conversation review 补充：Obsidian 会话重命名原先先替换 UI 标题和菜单，再等待 `PATCH /api/chat/title`；后端失败时用户会看到已改名，刷新后又恢复旧标题。现在 Save 按钮先等待后端 2xx，成功后才替换标题、菜单和点击恢复会话的 `dataset`；失败时保留输入框并提示错误。验证：`npm run build` 在 `src/interface/obsidian` 通过；构建后的 `main.js` fake DOM 覆盖后端 500 时输入框仍保留且不创建新标题、后端 200 时才显示新标题，并确认 conversation id 和 title 都按 URL 编码。

2026-07-01 Obsidian delete-conversation review 补充：Obsidian 会话列表删除按钮原先不区分当前会话和列表里的其他会话，删除任意历史会话成功后都会清空当前聊天窗口；后端删除失败也被静默吞掉。现在删除请求先检查后端 2xx，只有被删的是当前会话时才清空聊天 DOM 和 dataset；删除非当前会话只刷新会话列表；失败会保留 UI 并提示 `Failed to delete conversation`。验证：`npm run build` 在 `src/interface/obsidian` 通过；构建后的 `main.js` fake DOM 覆盖删除非当前会话不清空当前聊天、删除当前会话才清空、后端 500 保留 UI 且提示错误。

2026-07-01 Chat history delete false-success review 补充：`DELETE /api/chat/history?conversation_id=...` 原先忽略 `ConversationAdapters.adelete_conversation_by_user()` 的删除计数；非法 UUID 或不存在的会话都会返回 200 `Conversation history cleared`，前端会误以为删除成功并移除 UI。继续审阅发现 `conversation_id=` 空字符串还会被 adapter 当成“不传 id”，误触发清空全部历史。现在仅在完全不传 `conversation_id` 时清空全部；传了具体 id 但非法、缺失或为空都返回 404 `Conversation not found`，且空 id 不会删除任何会话。验证：新增红绿测试 `test_delete_chat_history_rejects_invalid_conversation_id`、`test_delete_missing_chat_history_returns_not_found`、`test_delete_empty_chat_history_id_does_not_clear_all`，`uv run pytest tests/test_client.py -q` 返回 `83 passed, 1 skipped`。

2026-07-01 Conversation id empty-string root-cause review 补充：继续沿删除契约审查发现 `ConversationAdapters.get_conversation_by_user()` / `aget_conversation_by_user()` / `save_conversation()` 等共享入口使用 truthy 判断 conversation id；`conversation_id=""` 会被当成“未指定会话”，从而读取/创建最近会话，普通 `/api/chat` 会绕过缺失会话预检，消息删除也可能在最近会话里按相同 `turnId` 删除内容。现在 conversation id 只要不是 `None` 就必须通过 UUID 校验，空字符串返回 not found；`/api/chat` 预检同样按 `is not None` 判断。验证：新增 `test_chat_empty_conversation_id_returns_not_found` 和 `test_delete_message_empty_conversation_id_does_not_delete_latest`，相邻聊天/消息删除回归通过；`uv run pytest tests/test_client.py -q` 返回 `85 passed, 1 skipped`。

2026-07-01 Content/file contract spot review 补充：复查 `/api/content/file`、`/api/content/files`、`/api/content/type/source` 和 Web/Obsidian 对应调用后，确认缺失文件读取已返回 404 且前端显示失败；删除缺失文件保持幂等 ok，未发现会导致误删、假成功 UI 清空或 500 的新阻断项。本轮不扩大前端改动，验证沿用最新全量 pytest、ruff、migration dry-run 和 diff check。

2026-07-01 Automation edit rollback review 补充：继续收尾审阅自动化链路时发现 `PUT /api/automation` 编辑旧 job 且缺少 `conversation_id` 时，会先创建新的 Automation conversation，再调用 scheduler `modify/reschedule`；如果 scheduler 更新失败，接口直接抛 500 且新 conversation 留在库里。现在编辑失败会返回受控 500，并删除本次刚创建的 conversation，避免自动化脏会话残留。验证：新增红绿测试 `test_edit_automation_cleans_created_conversation_when_modify_fails`，`uv run pytest --create-db tests/test_api_automation.py -q` 返回 `8 passed, 5 skipped`。

2026-07-01 final real-chain closure 补充：按真实用户链路重新启动隔离 Khoj server（临时 embedded DB、临时 vault `/tmp/khoj-final-e2e-yc3FNm/vault`、`client=obsidian`、真实 `/api/chat` 模型调用），完整验证 health、chat options、agents/options、model options、Obsidian content 上传/列举/读取/删除、local KB search、空搜索、会话创建/列表/历史/重命名、file filters 增删查、`/notes` 根据 `raw/redis.md` 生成 3 道 Redis 面试八股互问题并带 `local-kb://raw/redis.md#Lx-Ly` 引用、`/notes` 读取 Obsidian skill 后创建 `daily/2026-07-01.md`、日记含 YAML frontmatter、`# Redis 面试复盘`、`[[raw/redis|Redis Notes]]`、缓存穿透、布隆过滤器和空值 TTL、`/notes` 修改 `raw/index.md` 素材清单、消息删除 404、非法/缺失会话删除 404、正常会话删除成功，输出 `E2E_REAL_HTTP_OK`（daily note 1224 bytes，quiz 1875 chars）；临时 server 和 vault 已清理。本轮收尾未发现新的前端阻断项，因此没有新增单独 frontend issue 文件。

2026-07-01 end-to-end closure verification 补充：按真实用户链路启动隔离 Khoj server（临时 embedded DB、临时 vault `/tmp/khoj-e2e-WQvTbu/vault`、`client=obsidian`、真实 `/api/chat` 模型调用），完整验证 health、chat options、agent options、agents list、model options、Obsidian content 上传/列举/读取、local KB search、空搜索、会话创建/列表/历史、会话重命名、file filters 增删查、缺失会话 404、`/notes` 根据 `raw/redis.md` 生成 Redis 面试题并引用 `local-kb://raw/redis.md`、`/notes` 写入 `daily/2026-07-01.md` 日记并落盘、消息删除 404、content 删除和会话删除，输出 `E2E_REAL_HTTP_OK`；临时 server 已停止。插件侧补充 VM 验证新建 markdown 文件、取消后清空本轮创建目录、已有文件写回不污染 Obsidian `==highlight==` 语法、会话删除状态分支。最终验证：`npm run build` in `src/interface/obsidian` 通过；Obsidian VM 断言输出 `obsidian vm assertions passed`；`uv run pytest -q` 返回 `355 passed, 12 skipped, 10 warnings`；`uv run ruff check src/khoj tests` 返回 `All checks passed!`；`uv run python src/khoj/manage.py makemigrations --check --dry-run` 返回 `No changes detected`；`NEXT_TELEMETRY_DISABLED=1 npm run build` in `src/interface/web` 成功；`git diff --check` 通过。本轮收尾未发现新的未修前端阻断项，因此没有新增单独的 frontend issue 文件；若后续真实 Windows/Obsidian 客户端手测发现前端专属问题，只记录到文档，不再直接扩大前端改动。

当前总体完成度：本 spec 的核心实现已完成。模型后端、本地只读文件工具、Stage C file-first Notes 主路径、Interview Agent persona、受控 vault 追加写入、edit proposal、QQBot 薄入口、离线 gold set，以及 Phase 5 OpenKB-style Wiki Navigator / exploration save / PageIndex evidence / `/api/chat` 与 `/api/search` 旧文档检索替换都已落地。OpenKB 不改变默认 `file_first` 主路径，只有显式 `KHOJ_ENABLE_OPENKB=true` 且 `KHOJ_KB_ENGINE=openkb|hybrid` 时参与在线 evidence pass。2026-06-30 最新调整：Notes evidence 和写回已改成主 `/api/chat` LLM tool loop，面试风格回到 `Agent.personality` / 用户指令，后端只保留工具执行、权限和 evidence caps；旧本地 KB / 面试硬解码模块及其测试已删除。Notes loop 对 Codex 使用 JSON 文本工具协议，避免 Codex native tools 返回空 output 时工具完全不执行；如果 Notes 工具失败，`/api/chat` 会停止并明确报告未读取/修改本地知识库，不再继续生成假成功回复。`/api/search` 不调用 LLM，不复活旧 subagent，只作为直接查询端点返回 local KB / OpenKB / 词法索引结果。Phase 5H 已完成代码侧硬删除：文档导入只写 Entry 文本字段；文档和 Memory 查询走词法扫描；server 启动不加载旧检索模型；默认依赖无旧向量检索栈；database migration 历史已压成新的无旧检索初始迁移；本地 embedded DB 已按新 schema 重建。2026-06-30 ponytail cleanup 已额外清掉旧 wrappers、孤立 helper、旧 API 参数、旧 prompt/doc 命名，并用 `rg` 验证旧符号族不再出现在 `src/khoj`、`tests`、`docs`、`README.md`、`pyproject.toml`。同日最新补充：Notes planner 已按 OpenKB 的 skill runtime 模式支持本地 SKILL.md catalog，绑定 vault 里的 Obsidian skills（如 `obsidian-markdown`、`obsidian-cli`、`obsidian-bases`），并注入根目录 `agents.md` 项目规则，避免写日记/笔记时只凭硬编码猜格式。真实用户模拟发现并修复：写入工具已执行但最终回答声称无法写入、新建 raw 笔记不敢调用 append_note、更新索引首轮不调用工具、以及 Codex planner 偶发断流直接失败。OpenAI-level review 进一步用无 stub 端到端测试发现并修复了 grep 只发现不精读导致的 `/api/chat` 证据不足、`read_skill` 文件消失打崩 Notes loop、Codex runtime 启动误报传统默认模型、helper grep 参数名回归、自动任务 trigger 后实际 chat 反问提醒时间、非法 `conversation_id` 触发 500，以及 web shell 的搜索页、静态 RSC、logo SVG、IP/时区和 Automations 展示问题。Phase 6 已完成 Agent Harness worker / observability 设计：继续保留单主 `/api/chat`，只新增窄 worker 和结构化事件日志，不做 Plan、压缩、权限确认或新 Agent 框架。

当前验证：

- 2026-07-01 final real-chain E2E：临时 embedded DB + 临时 vault + 真实 `/api/chat` 模型调用输出 `E2E_REAL_HTTP_OK`，覆盖 Obsidian content 上传/读取/删除、local KB search、session/history/title/file filters、Redis 面试八股问答、Obsidian skill 日记创建、`raw/index.md` 修改、消息删除 404、非法/缺失会话删除 404 和正常会话删除。
- `uv run pytest --create-db tests/test_api_automation.py -q` 返回 `8 passed, 5 skipped`，覆盖自动化创建失败回滚、编辑旧 job 修复、编辑失败清理新建 conversation、手动触发缺失 conversation 清理。
- `uv run pytest tests/test_client.py -q` 返回 `85 passed, 1 skipped`，覆盖本轮 chat history/delete/message 空 id 回归和主客户端 route 覆盖。
- `uv run pytest -q` 返回 `363 passed, 12 skipped, 10 warnings`，后端全量 pytest 当前无回归。
- `uv run ruff check src/khoj tests` 返回 `All checks passed!`；`uv run python src/khoj/manage.py makemigrations --check --dry-run` 返回 `No changes detected`；`git diff --check` 返回 0。
- `npm run build` in `src/interface/obsidian` 通过；`NEXT_TELEMETRY_DISABLED=1 npm run build` in `src/interface/web` 通过。本轮未发现新的前端阻断项，没有新增 frontend issue 文件。
- `uv run pytest tests/test_notes_tool_loop.py::test_notes_tool_loop_loads_nested_official_obsidian_skill -q` 先红：`skills/note-taking/obsidian/SKILL.md` 未进入 skill catalog；修复后通过，并确认 planner 使用 `json_object` 文本工具协议而不是 Codex native tools。
- `uv run pytest tests/test_client.py::test_websocket_chat_flushes_debounced_thought -q` 先红：debounced thought 被第一次 flush 清空，WebSocket 收到空字符串；修复后通过。
- `uv run pytest tests/test_client.py::test_websocket_chat_flushes_first_debounced_message -q` 修复后通过：首个普通文本 chunk 会发送 `hello` 和 END_EVENT，不再因 `delayed_flush` 未定义进入 internal error。
- `uv run pytest tests/test_client.py::test_chat_event_structured_streaming_predicate -q` 先红：缺少显式 predicate 且旧条件永真；修复后返回 `1 passed`。
- `uv run pytest tests/test_client.py::test_file_filter_updates_missing_conversation_return_not_found -q` 先红：四条 file-filter 写接口对缺失会话返回 200；修复后返回 `4 passed`。
- `uv run pytest tests/test_client.py::test_search_empty_query_returns_empty tests/test_text_search.py::test_text_search_empty_query_returns_no_hits tests/test_memory_settings.py::test_memory_search_empty_query_returns_no_hits -q` 先红：空白搜索/记忆 lookup 返回已有内容；修复后返回 `4 passed`。
- `uv run pytest tests/test_client.py tests/test_text_search.py tests/test_memory_settings.py -q` 返回 `105 passed, 2 skipped`。
- `uv run pytest tests/test_client.py -q` 返回 `65 passed, 1 skipped`，确认 HTTP/WebSocket/content route 主覆盖未回归。
- `uv run pytest tests/test_notes_tool_loop.py::test_notes_tool_loop_accepts_json_string_arguments -q` 先红：字符串 `arguments` 会导致 Notes 工具执行崩溃；修复后返回 `1 passed`。`uv run pytest tests/test_notes_tool_loop.py -q` 返回 `16 passed`。
- `uv run pytest tests/test_agents.py::test_hidden_agent_create_without_chat_model_uses_default_model -q` 先红：传统 DB runtime 下缺省 `chat_model` 的 hidden agent 创建触发 `chat_model_id` 空外键 500；修复后返回 `1 passed`。
- `uv run pytest tests/test_agents.py::test_agents_endpoint_handles_missing_default_agent -q` 返回 `1 passed`；`uv run pytest tests/test_agents.py tests/test_notes_tool_loop.py -q` 返回 `43 passed`，覆盖 agent 列表、创建/更新、隐藏 agent、agent KB 搜索和 Notes tool loop 链路。
- `uv run pytest tests/test_openkb_harness.py::test_save_exploration_falls_back_to_local_kb -q` 先红：本地已有 `redis.md` 时 fallback 保存仍把 `[[redis]]` 剥成纯文本；修复后返回 `1 passed`。`uv run pytest tests/test_openkb_harness.py -q` 返回 `13 passed`；`uv run pytest tests/test_api_chat_file_kb.py::test_chat_explicit_save_writes_openkb_exploration -q` 返回 `1 passed`。
- `uv run pytest tests/test_api_chat_file_kb.py::test_chat_explicit_save_reports_openkb_save_failure -q` 先红：OpenKB exploration 目录 symlink 逃逸触发 `OpenKBError` 后 streaming chat 直接中断；修复后返回 `1 passed`，并确认返回失败 status、`end_response` 事件且外部目录未写入。
- `uv run pytest tests/test_local_kb.py::LocalKBTest::test_append_note_creates_missing_parent_directories -q` 先红：`daily/2026-07-01.md` 父目录不存在时 `append_note` 拒绝创建新笔记；修复后返回 `1 passed`。`uv run pytest tests/test_local_kb.py -q` 返回 `23 passed`，覆盖 root jail、hidden path、symlink escape、grep/read/link resolve 和写入禁用。
- `uv run pytest tests/test_api_automation.py::test_trigger_automation_deletes_job_with_missing_conversation -q` 先红：conversation 已失效的 automation 手动 trigger 返回 200；修复后返回 `1 passed`，并确认返回 404 且坏 job 被删除。`uv run pytest --create-db tests/test_api_automation.py -q` 返回 `6 passed, 5 skipped`；conversation invalid-id client 回归 4 条返回 `4 passed`。
- `uv run ruff check src/khoj/routers/api_automation.py src/khoj/database/adapters/__init__.py tests/test_api_automation.py` 返回 `All checks passed!`。
- `cd src/interface/web && ./node_modules/.bin/prettier --check app/components/chatSidebar/chatSidebar.tsx`、`./node_modules/.bin/eslint app/components/chatSidebar/chatSidebar.tsx`、`./node_modules/.bin/tsc --noEmit` 均通过；`NEXT_TELEMETRY_DISABLED=1 ./node_modules/.bin/next build` 返回成功，确认 ChatSidebar create-agent 失败处理没有引入格式、lint、类型或生产构建问题。
- `cd src/interface/web && ./node_modules/.bin/prettier --check app/components/agentCard/agentCard.tsx`、`./node_modules/.bin/eslint app/components/agentCard/agentCard.tsx app/components/chatSidebar/chatSidebar.tsx app/agents/page.tsx`、`./node_modules/.bin/tsc --noEmit` 均通过；`NEXT_TELEMETRY_DISABLED=1 ./node_modules/.bin/next build` 返回成功，确认 AgentCard 删除失败处理没有引入格式、lint、类型或生产构建问题。
- `cd src/interface/web && node -e ...` 源码回归确认 `chatFunctions.ts` 已移除 `JSON.stringify(chunkData)` fallback 且保留 raw chunk；`./node_modules/.bin/prettier --check app/common/chatFunctions.ts`、`./node_modules/.bin/eslint app/common/chatFunctions.ts`、`./node_modules/.bin/tsc --noEmit` 均通过；`NEXT_TELEMETRY_DISABLED=1 ./node_modules/.bin/next build` 返回成功。
- `uv run python src/khoj/manage.py makemigrations --check --dry-run` 返回 `No changes detected`，确认压缩后的新初始迁移与当前模型一致。
- `uv run pytest -q` 返回 `340 passed, 12 skipped`，后端全量 pytest 当前无回归。
- `uv run ruff check src/khoj/utils/openkb.py tests/test_openkb_harness.py tests/test_api_chat_file_kb.py` 返回 `All checks passed!`；`git diff --check` 返回 0。
- `cd src/interface/web && npm run build` 返回 0，Next production build、lint/type check、static export 通过；`cd src/interface/obsidian && npm run build` 返回 0，Obsidian TypeScript check 和 esbuild production bundle 通过。
- `uv run pytest tests/test_agents.py::test_agent_conversation_returns_virtual_default_agent_for_codex_runtime -q` 修复后通过；真实临时 Codex server 创建 chat session 后 `GET /api/agents/conversation?conversation_id=...` 返回 200，body 为虚拟 `khoj` agent，`chat_model` 为当前 Codex model。
- `uv run pytest tests/test_agents.py::test_hidden_agent_create_requires_database_chat_model_in_codex_runtime -q` 修复后通过；真实临时 Codex server 新建 session 后 `POST /api/agents/hidden` 不再 500，返回明确 400。
- `cd src/interface/web && ./node_modules/.bin/eslint app/components/chatSidebar/chatSidebar.tsx && ./node_modules/.bin/tsc --noEmit` 返回 0；`cd src/interface/web && npm run build` 返回 0，确认 ChatSidebar 保存失败处理没有引入类型、lint 或生产构建问题。额外同源 HTTP 验证：临时 `0.0.0.0:18084` 真实 Codex backend + `0.0.0.0:18085` 当前 `out` 静态包反代，`GET /chat?conversationId=...` 返回 200，`POST /api/agents/hidden?...` 通过同源反代真实返回 400 `Unknown chat model: gpt-test-codex`。
- `uv run pytest tests/test_client.py::test_chat_invalid_conversation_id_returns_not_found -q` 先红：真实 HTTP 非流式 chat 对缺失 conversation 返回 200；修复后返回 404。
- `uv run pytest tests/test_client.py::test_streaming_chat_invalid_conversation_id_returns_not_found -q` 修复后返回 `1 passed`；真实临时 server `POST /api/chat {"stream": true, "conversation_id": "not-a-real-conversation"}` 返回 404 JSON，而不是 200 text stream。
- 真实服务 E2E（临时 embedded DB + 临时 vault + `KHOJ_LOCAL_KB_PATH` + `KHOJ_ALLOW_VAULT_WRITE=true` + `http://127.0.0.1:18081`）通过：`/notes` 读取 `interview/redis.md` 和嵌套 `obsidian` skill 后创建 `daily/2026-07-01.md`，文件含 YAML frontmatter、`Redis 面试复盘` 标题、中文小标题、`[[interview/redis|Redis Interview Notes]]` wikilink、缓存穿透/布隆过滤器/空值短 TTL 内容；另用 `/notes` 根据 Redis 笔记生成 3 道面试八股互问题，答案含本地 KB 行号引用和追问点。
- 真实 HTTP smoke 通过：`GET /api/health`、`GET /`、`GET /api/chat/options`、`GET/POST /api/chat/sessions`、非法 `/api/chat` conversation 404、`GET /api/search?q=布隆过滤器` 命中 `local_kb`、`/home/../../etc/passwd` 404、Obsidian 形状 `PATCH /api/content?client=obsidian&t=markdown` 上传、`GET /api/content/files`、`GET /api/content/file`、`GET /api/content/computer`、`DELETE /api/content/file`、删除后 404、`GET /api/agents/options`。
- 真实 Automation CRUD E2E 通过：`POST /api/automation` 用中文 “Redis 复习提醒” 真实调用模型生成 `/automated_task ...`，`GET /api/automation` 列出任务，`PUT /api/automation` 更新为 “Redis 八股题提醒”，`DELETE /api/automation` 删除同一 job。
- `cd src/interface/obsidian && yarn build` 通过；`cd src/interface/web && npm run build` 通过。`bun run build` 在当前服务器 Snap 环境因 home outside `/home` 限制失败，非代码错误。
- `git diff --check`、`uv run ruff check src/khoj tests`、`USE_EMBEDDED_DB=true PGSERVER_DATA_DIR=/data/ljr/my_project/khoj/pgserver_data uv run python src/khoj/manage.py check` 均通过。
- `uv run pytest tests/test_notes_tool_loop.py tests/test_client.py::test_chat_invalid_conversation_id_returns_not_found tests/test_client.py::test_websocket_chat_flushes_debounced_thought -q` 返回 `17 passed`。
- `uv run pytest tests/test_client.py -q` 返回 `60 passed, 1 skipped`。
- `uv run pytest tests/test_agents.py -q` 返回 `25 passed`。
- `uv run pytest -q` 返回 `321 passed, 12 skipped, 10 warnings`。
- `uv run pytest tests/test_agents.py::test_anonymous_async_agent_lookup_ignores_admin_private_agent -q` 先红：匿名 async lookup 返回 `<Agent: Async Admin Private Agent>`。
- `uv run pytest tests/test_agents.py::test_anonymous_async_agent_lookup_ignores_admin_private_agent tests/test_agents.py::test_unauthenticated_user_cannot_read_admin_private_agent tests/test_agents.py::test_non_creator_can_read_public_agent -q` 返回 `3 passed`。
- `uv run pytest tests/test_agents.py::test_create_agent_with_unavailable_paid_chat_model_returns_forbidden -q` 先红：过期订阅账号选择 paid model 创建 agent 返回 200，并静默降级到默认模型。
- `uv run pytest tests/test_agents.py::test_create_agent_with_unavailable_paid_chat_model_returns_forbidden tests/test_agents.py::test_create_agent_with_unknown_chat_model_returns_bad_request tests/test_agents.py::test_non_creator_cannot_update_public_agent -q` 修复后返回 `3 passed`。
- `uv run pytest tests/test_agents.py::test_create_agent_rejects_invalid_privacy_level -q` 先红：非法 `privacy_level` 创建 agent 返回 200 并落库。
- `uv run pytest tests/test_agents.py::test_create_agent_rejects_invalid_privacy_level tests/test_agents.py::test_create_agent_with_unavailable_paid_chat_model_returns_forbidden tests/test_agents.py::test_create_agent_with_unknown_chat_model_returns_bad_request tests/test_agents.py::test_hidden_agent_update_rejects_regular_agent -q` 修复后返回 `4 passed`。
- `uv run pytest tests/test_agents.py -q` 返回 `23 passed`。
- `uv run pytest tests/test_notes_tool_loop.py::test_notes_tool_loop_hides_local_profile_and_skills_when_local_kb_disabled -q` 先红：OpenKB-only/local KB 禁用时 prompt 仍含 `private vault profile`。
- `uv run pytest tests/test_notes_tool_loop.py::test_notes_tool_loop_hides_local_profile_and_skills_when_local_kb_disabled -q` 修复后返回 `1 passed`。
- `uv run pytest tests/test_notes_tool_loop.py tests/test_api_chat_file_kb.py -q` 返回 `21 passed`。
- `uv run ruff check src/khoj/processor/conversation/notes_tool_loop.py tests/test_notes_tool_loop.py` 返回 `All checks passed!`。
- `uv run pytest tests/test_openkb_harness.py::test_wiki_search_skips_symlink_escape tests/test_openkb_harness.py::test_save_exploration_rejects_symlink_escape -q` 先红：搜索 symlink 逃逸触发外部路径，exploration symlink 目录未拒绝。
- `uv run pytest tests/test_openkb_harness.py::test_wiki_search_skips_symlink_escape tests/test_openkb_harness.py::test_save_exploration_rejects_symlink_escape -q` 修复后返回 `2 passed`。
- `uv run pytest tests/test_openkb_harness.py::test_save_exploration_local_kb_fallback_rejects_symlink_escape -q` 先红：OpenKB fallback 到 local KB 时未拒绝 symlink review 目录，未抛 `OpenKBError`。
- `uv run pytest tests/test_openkb_harness.py::test_save_exploration_local_kb_fallback_rejects_symlink_escape -q` 修复后返回 `1 passed`。
- `uv run pytest tests/test_openkb_harness.py -q` 返回 `13 passed`。
- `uv run ruff check src/khoj/utils/openkb.py tests/test_openkb_harness.py` 返回 `All checks passed!`。
- `uv run pytest tests/test_local_kb.py::LocalKBTest::test_list_skips_symlink_directories_outside_root -q` 先红：外部目录 symlink 进入 `kb_list()` candidates，排序时 `local_kb_relative_path()` 抛 `ValueError`。
- `uv run pytest tests/test_local_kb.py::LocalKBTest::test_list_skips_symlink_directories_outside_root -q` 修复后返回 `1 passed`。
- `uv run pytest tests/test_local_kb.py::LocalKBTest::test_kb_resolve_link_requires_configured_root -q` 先红：未配置本地 KB 时 `kb_resolve_link()` 抛 `AttributeError: 'NoneType' object has no attribute 'is_file'`。
- `uv run pytest tests/test_local_kb.py::LocalKBTest::test_kb_resolve_link_requires_configured_root tests/test_local_kb.py::LocalKBTest::test_kb_resolve_link_supports_obsidian_and_markdown_links tests/test_local_kb.py::LocalKBTest::test_kb_resolve_link_returns_ambiguous_for_duplicate_basenames -q` 修复后返回 `3 passed`。
- `uv run pytest tests/test_local_kb.py::LocalKBTest::test_profile_references_ignore_invalid_caps -q` 先红：无效 profile cap env 触发 `ValueError: invalid literal for int()`。
- `uv run pytest tests/test_local_kb.py::LocalKBTest::test_profile_references_ignore_invalid_caps tests/test_local_kb.py::LocalKBTest::test_profile_references_read_agents_indexes_and_entry_links -q` 修复后返回 `2 passed`。
- `uv run pytest tests/test_local_kb.py -q` 返回 `22 passed`。
- `uv run pytest tests/test_api_automation.py::test_get_automations_handles_legacy_plain_name tests/test_api_automation.py::test_edit_automation_repairs_legacy_plain_name -q` 先红：legacy plain job name 让列表和编辑分别触发 `JSONDecodeError`。
- `uv run pytest tests/test_api_automation.py::test_get_automations_handles_legacy_plain_name tests/test_api_automation.py::test_edit_automation_repairs_legacy_plain_name -q` 修复后返回 `2 passed`。
- `uv run pytest tests/test_api_automation.py -q` 返回 `5 passed, 5 skipped`。
- `uv run pytest tests/test_client.py::test_delete_all_content_removes_file_objects -q` 先红：`DELETE /api/content/type/all` 后 `/api/content/files` 仍返回 stale FileObject。
- `uv run pytest tests/test_client.py::test_delete_all_content_removes_file_objects tests/test_client.py::test_delete_invalid_content_type_returns_bad_request tests/test_client.py::test_delete_invalid_content_source_returns_bad_request tests/test_client.py::test_content_file_routes_accept_encoded_special_file_names -q` 修复后返回 `4 passed`。
- `uv run pytest tests/test_client.py::test_convert_text_file_replaces_invalid_utf8 -q` 先红：`text/plain` 短文本被跳过为 unsupported，返回空转换列表。
- `uv run pytest tests/test_client.py::test_convert_text_file_replaces_invalid_utf8 tests/test_client.py::test_delete_all_content_removes_file_objects tests/test_client.py::test_content_file_routes_accept_encoded_special_file_names -q` 修复后返回 `3 passed`。
- `uv run pytest tests/test_helpers.py::test_get_file_type_allows_missing_mime_type -q` 先红：`get_file_type(None, b"...")` 触发 `TypeError: argument of type 'NoneType' is not iterable`。
- `uv run pytest tests/test_helpers.py::test_get_from_null_dict tests/test_helpers.py::test_get_file_type_allows_missing_mime_type tests/test_client.py::test_convert_text_file_without_content_type tests/test_client.py::test_convert_text_file_replaces_invalid_utf8 -q` 修复后返回 `4 passed`。
- `uv run pytest tests/test_client.py::test_automations_page_requires_auth -q` 先红：非 anonymous 未登录访问 `/automations` 返回 200。
- `uv run pytest tests/test_client.py::test_automations_page_requires_auth tests/test_client.py::test_next_export_text_files_are_served tests/test_client.py::test_get_configured_types_with_no_content_config -q` 修复后返回 `3 passed`。
- `uv run pytest tests/test_client.py::test_home_static_directory_returns_not_found -q` 先红：`GET /home/assets` 把目录交给 `FileResponse`，抛 `RuntimeError: ... is not a file`。
- `uv run pytest tests/test_client.py::test_home_static_directory_returns_not_found -q` 修复后返回 `1 passed`。
- `uv run pytest tests/test_client.py::test_home_static_directory_returns_not_found tests/test_client.py::test_next_export_text_files_are_served -q` 返回 `2 passed`。
- `uv run pytest -q` 返回 `319 passed, 12 skipped, 10 warnings`。
- `uv run ruff check src/khoj tests` 返回 `All checks passed!`。
- `git diff --check` 无输出，退出码 0。
- `USE_EMBEDDED_DB=true PGSERVER_DATA_DIR=/data/ljr/my_project/khoj/pgserver_data uv run python src/khoj/manage.py check` 返回 `System check identified no issues`。
- `USE_EMBEDDED_DB=true PGSERVER_DATA_DIR=/data/ljr/my_project/khoj/pgserver_data uv run python src/khoj/manage.py makemigrations --check --dry-run` 返回 `No changes detected`。
- 临时 embedded Postgres 数据目录执行 `uv run python src/khoj/manage.py migrate --noinput`，`database.0001_initial`、Django 内置表和 sessions 全部 `OK`。
- `CI=1 NEXT_TELEMETRY_DISABLED=1 CIRCLE_NODE_TOTAL=2 timeout 180s npm run build` 在 `src/interface/web` 返回成功，Next 静态导出完成。
- `yarn build` 在 `src/interface/obsidian` 返回成功，生成 `main.js 249.2kb`。
- `USE_EMBEDDED_DB=true ... .venv/bin/khoj --host 0.0.0.0 --port 12831 --anonymous-mode --non-interactive` 真实启动成功；`curl /api/health` 返回 200，`curl /api/agents` 返回 200，`curl '/api/search?q=redis&n=-1'` 返回 422，`curl /api/model/chat` 返回 200；测试后已停止 12831 服务。
- `uv run ruff check src/khoj/processor/conversation/notes_tool_loop.py src/khoj/routers/api_chat.py src/khoj/routers/api.py src/khoj/routers/helpers.py tests/test_notes_tool_loop.py tests/test_api_chat_file_kb.py tests/test_client.py tests/test_local_kb.py tests/test_openkb_harness.py tests/test_research_document_tools.py tests/test_memory_settings.py tests/test_text_search.py`
- `uv run pytest --create-db tests/test_client.py::test_search_with_no_auth_key tests/test_client.py::test_search_with_invalid_auth_key tests/test_client.py::test_search_with_invalid_content_type tests/test_client.py::test_search_with_valid_content_type tests/test_client.py::test_notes_search tests/test_client.py::test_notes_search_no_results tests/test_client.py::test_notes_search_with_only_filters tests/test_client.py::test_notes_search_with_include_filter tests/test_client.py::test_notes_search_with_exclude_filter tests/test_client.py::test_notes_search_requires_parent_context tests/test_multiple_users.py tests/test_local_kb.py tests/test_local_vault_references.py tests/test_notes_tool_loop.py tests/test_openkb_harness.py tests/test_api_chat_file_kb.py tests/test_memory_settings.py tests/test_text_search.py tests/test_research_document_tools.py tests/test_qqbot_adapter.py -q`
- `USE_EMBEDDED_DB=true PGSERVER_DATA_DIR=/data/ljr/my_project/khoj/pgserver_data uv run python src/khoj/manage.py check`
- `KHOJ_HOST=0.0.0.0 KHOJ_PORT=12805 USE_EMBEDDED_DB=true PGSERVER_DATA_DIR=/data/ljr/my_project/khoj/pgserver_data bash scripts/run_local.sh`
- `curl -sS -i http://127.0.0.1:12805/api/health` 返回 `HTTP/1.1 200 OK` 和 `{"email": "default@example.com"}`
- `uv run pytest tests/test_notes_tool_loop.py tests/test_api_chat_file_kb.py -q` 返回 `17 passed`
- `uv run ruff check src/khoj/processor/conversation/notes_tool_loop.py src/khoj/routers/api_chat.py tests/test_notes_tool_loop.py tests/test_api_chat_file_kb.py` 返回 `All checks passed!`
- 真实 Codex 只读 smoke：`collect_notes_evidence_with_tools("读取一下 agents.md 的开头")` 返回 `list_files`、`view_file` 和 `local-kb://agents.md#L1-L30`
- `uv run pytest tests/test_notes_tool_loop.py -q` 返回 `8 passed`
- `uv run ruff check src/khoj/processor/conversation/notes_tool_loop.py tests/test_notes_tool_loop.py` 返回 `All checks passed!`
- `uv run pytest tests/test_api_chat_file_kb.py -q` 返回 `6 passed`
- 真实用户模拟：自然语言 Agent 工具系统出题读取 `raw/claude-code-book/00-前言.md` 并生成主问题+追问；日记追加写入 `daily/2026-06-30.md` 后最终回答正确报告 `append_note written`；新建 `raw/agent-e2e-2026-06-30.md` 带 Obsidian frontmatter；`raw/index.md` 的“素材清单”追加 `[[agent-e2e-2026-06-30]]`
- `uv run pytest -q` 返回 `261 passed, 12 skipped`
- `USE_EMBEDDED_DB=true PGSERVER_DATA_DIR=/data/ljr/my_project/khoj/pgserver_data uv run python src/khoj/manage.py check` 返回 `System check identified no issues`
- `uv run ruff check src/khoj/database/models/__init__.py src/khoj/database/adapters/__init__.py src/khoj/routers/api.py src/khoj/routers/api_chat.py src/khoj/routers/api_agents.py src/khoj/routers/api_model.py src/khoj/routers/research.py src/khoj/routers/helpers.py src/khoj/utils/local_kb.py src/khoj/utils/openkb.py src/khoj/utils/lexical.py src/khoj/utils/initialization.py src/khoj/processor/conversation/notes_tool_loop.py src/khoj/integrations/qqbot/adapter.py tests/test_notes_tool_loop.py tests/test_api_chat_file_kb.py tests/test_local_kb.py tests/test_openkb_harness.py tests/test_qqbot_adapter.py` 返回 `All checks passed!`
- 隔离 vault/OpenKB 无 stub E2E：在 `0.0.0.0:12806` 启动真实服务，验证 `/api/health`、Codex model options、`/api/search` 本地 KB、`/api/search` OpenKB、`/api/chat /notes` 本地精读、`/api/chat /notes` OpenKB 精读、`/api/chat /notes` 读取 `obsidian-markdown` skill 后真实写入 `raw/e2e-skill-note.md`、QQBot adapter 透传真实 `/api/chat`，脚本输出 `E2E_OK`
- `cd src/interface/obsidian && yarn build` 返回成功，生成 `main.js`
- `cd src/interface/web && NEXT_TELEMETRY_DISABLED=1 timeout 180s ./node_modules/.bin/next build` 返回成功；存在既有 ESLint/Prettier/React hook warnings，未阻塞构建
- 额外无 stub HTTP E2E：`0.0.0.0:12807` 匿名临时服务验证 content convert/index/list/get/types/size/delete、本地 KB search、OpenKB search、automation create/list/edit/trigger/delete、WebSocket interrupt/missing-q 校验，脚本输出 `E2E_ANON_HTTP_OK` 和 `E2E_WS_OK`
- 额外非匿名 HTTP E2E：`0.0.0.0:12808` 临时服务用真实 seeded Bearer token 验证 `/auth/token` 未认证 303、已认证 create/list/delete，脚本输出 `E2E_AUTH_TOKEN_OK`
- 自动任务质量 E2E：真实 `/api/chat` 输入 `scheduled_chat` 修复后的 `/automated_task` payload，返回 `Reminder: review the edited e2e token.`，脚本输出 `E2E_AUTOMATION_CONTEXT_CHAT_OK`
- 非法会话 ID E2E：真实 `/api/chat` 传 `conversation_id=not-a-uuid` 返回 `Conversation not-a-uuid not found`，脚本输出 `E2E_INVALID_CONVERSATION_ID_OK`
- `uv run pytest -q` 返回 `263 passed, 12 skipped`
- `uv run ruff check src/khoj tests` 返回 `All checks passed!`
- `cd src/interface/web && NEXT_TELEMETRY_DISABLED=1 ./node_modules/.bin/next build` 返回成功；仍有既有 React hook、Prettier、a11y warnings
- `0.0.0.0:12809` 临时服务 API smoke：`/static/_next/static/css/d7302a65d1fe2961.css`、`/index.txt`、`/settings.txt`、`/search.txt`、`/api/ip`、`/api/content/computer` 返回 200，`/api/content/file?file_name=missing.md` 返回 404 `{"error":"File not found"}`
- Playwright 真实浏览器打开 `http://127.0.0.1:12809/`、`/search`、`/settings`、`/automations`、`/agents`、`/chat` 均为 200；无 page error、console error、request failure、HTTP 4xx/5xx；未出现 `Application error`、`allFiles.map is not a function`、`undefined, undefined`
- 强制清理 `.next` / `out` 后重跑 `cd src/interface/web && NEXT_TELEMETRY_DISABLED=1 ./node_modules/.bin/next build` 返回成功；`src/interface/web/out`、`src/khoj/interface/built` 和 `src/khoj/static/_next` 不再包含前端直连 `ipapi.co/json`
- Playwright 真实浏览器在 `http://127.0.0.1:12810/chat` 的 CSP 环境里执行 `new WebSocket("ws://127.0.0.1:12810/api/chat/ws?client=web")`，返回 `status: open`，无 console error / page error；服务端日志显示 WebSocket accepted/open/closed
- 自动任务边界真实 HTTP E2E：`0.0.0.0:12811` 临时服务验证 malformed crontime 返回 400 `Invalid crontime`，create -> edit(`timezone=Not/AZone` fallback UTC) -> delete 全部返回 200
- `uv run pytest tests/test_api_automation.py -q` 返回 `3 passed, 5 skipped`
- `uv run pytest tests/test_client.py::test_ip_location_uses_forwarded_public_ip tests/test_client.py::test_ip_location_skips_private_ip tests/test_client.py::test_next_export_text_files_are_served tests/test_client.py::test_get_content_source_files_for_search_page tests/test_client.py::test_get_missing_content_file_returns_not_found -q` 返回 `5 passed`
- `uv run pytest tests/test_agents.py::test_recent_conversation_cutoff_is_timezone_aware tests/test_agents.py::test_agents_endpoint_uses_timezone_aware_recent_conversation_cutoff -q` 返回 `2 passed`
- `0.0.0.0:12813` 临时服务真实浏览器 smoke：Playwright Chromium 打开 `/automations`、`/chat`，无 page error、console error、非 aborted request failure、HTTP 4xx/5xx；服务端日志 `rg "naive datetime|RuntimeWarning"` 无命中
- `0.0.0.0:12814` 临时服务真实浏览器 smoke：Playwright Chromium 打开 `/chat`，无 page error、console error、非 aborted request failure、HTTP 4xx/5xx
- `0.0.0.0:12815` 临时服务真实浏览器 smoke：Playwright Chromium 打开 `/search`，无 page error、console error、非 aborted request failure、HTTP 4xx/5xx
- `uv run pytest -q` 返回 `272 passed, 12 skipped`
- `uv run ruff check src/khoj tests` 返回 `All checks passed!`
- `cd src/interface/web && NEXT_TELEMETRY_DISABLED=1 ./node_modules/.bin/next build` 返回成功；本轮已清掉 `app/common/iconUtils.tsx` 的误判 alt warning、`app/automations/page.tsx` 的 toast hook warning、`app/chat/page.tsx` 中流式消息 effect / interrupt effect 的 stale closure warnings，以及 `app/search/page.tsx` 上传弹窗 `warning/error` stale closure warning，仍有既有 React hook、Prettier、真实 `<img>` warnings
- `cd src/interface/obsidian && yarn build` 返回成功，生成 `main.js`
- `cd src/interface/web && ./node_modules/.bin/eslint app/chat/page.tsx app/page.tsx app/share/chat/page.tsx app/components/chatInputArea/chatInputArea.tsx` 返回 0；本轮触及的首页/聊天页/分享页/输入框无 ESLint warning
- `cd src/interface/web && ./node_modules/.bin/tsc --noEmit` 返回 0
- `cd src/interface/web && timeout 300s ./node_modules/.bin/next build` 返回 0；本轮触及页面无新增 warning，仍保留其它页面历史 warnings
- `uv run pytest -q` 返回 `272 passed, 12 skipped, 10 warnings`
- `uv run ruff check src/khoj tests` 返回 `All checks passed!`
- `127.0.0.1:12816` 临时服务真实浏览器 smoke：Playwright Chromium 分别打开 `/`、`/chat?conversationId=<real id>`、`/search`、`/automations`、`/agents`、`/settings`，全部 HTTP 200，无 page error、console error、非 aborted request failure、HTTP 5xx
- `127.0.0.1:12816` 真实 WebSocket payload 验证：从 localStorage 恢复 `message`、`images`、`uploadedFiles` 后，Playwright 捕获真实 `framesent`，确认 `q`、1 张 encoded image 和 `smoke.md` 文件同时发往 `/api/chat/ws?client=web`；后端返回 `Please ask your query to get started.`，无 console/page error
- `cd src/interface/web && ./node_modules/.bin/eslint app/search/page.tsx app/agents/page.tsx app/components/agentCard/agentCard.tsx app/components/chatHistory/chatHistory.tsx` 返回 0；本轮触及的 Search / Agents / AgentCard / ChatHistory 无 ESLint warning
- `cd src/interface/web && ./node_modules/.bin/tsc --noEmit` 返回 0
- `cd src/interface/web && timeout 300s ./node_modules/.bin/next build` 返回 0；本轮触及文件不再出现在 build warning 中，仍有其它页面历史 React hook / Prettier / a11y warning
- `uv run ruff check src/khoj tests` 返回 `All checks passed!`
- `127.0.0.1:12817` 临时服务真实浏览器 smoke：先通过 `/api/chat/sessions?client=web&agent_slug=khoj` 创建真实会话，再打开 `/chat?conversationId=<real id>`、`/search`、`/agents`，全部 HTTP 200，无 page error、console error、request failure、HTTP 4xx/5xx
- `uv run pytest -q` 返回 `272 passed, 12 skipped, 10 warnings`
- `cd src/interface/web && ./node_modules/.bin/eslint app components` 返回 0；当前前端 app/components 无 ESLint warning
- `cd src/interface/web && ./node_modules/.bin/tsc --noEmit` 返回 0
- `cd src/interface/web && timeout 300s ./node_modules/.bin/next build` 返回 0；编译、lint、类型检查、静态导出全部通过
- `uv run python src/khoj/manage.py collectstatic --noinput` 返回成功，复制 241 个静态文件，170 个未修改
- `127.0.0.1:12818` 临时服务真实浏览器 smoke：先通过 `/api/chat/sessions?client=web&agent_slug=khoj` 创建真实会话，再打开 `/`、`/chat?conversationId=<real id>`、`/search`、`/agents`、`/settings`，全部 HTTP 200，无 page error、console error、request failure、HTTP 4xx/5xx
- `uv run ruff check src/khoj tests` 返回 `All checks passed!`
- `uv run pytest -q` 返回 `272 passed, 12 skipped, 10 warnings`
- `uv run pytest tests/test_memory_settings.py::test_memories_api_list_update_delete_preserves_memory_identity -q` 返回 `1 passed`
- `uv run pytest tests/test_memory_settings.py -q` 返回 `25 passed`
- `uv run ruff check src/khoj tests` 返回 `All checks passed!`
- `uv run pytest -q` 返回 `273 passed, 12 skipped, 10 warnings`
- `cd src/interface/obsidian && yarn build` 返回成功，生成 `main.js 249.1kb`
- `127.0.0.1:12821` 无 local KB 临时服务 Obsidian sync/search E2E：插件同款请求删除 markdown index、上传 `interview/redis.md`、列出 content files、带空 Bearer 搜索 `Redis sorted sets`，返回 `additional.file == "interview/redis.md"`；临时服务和 `/tmp/khoj-obsidian-e2e-12821` 已清理
- `uv run pytest tests/test_client.py::test_delete_invalid_content_type_returns_bad_request tests/test_client.py::test_delete_invalid_content_source_returns_bad_request -q` 返回 `2 passed`
- `uv run ruff check src/khoj tests` 返回 `All checks passed!`
- `uv run pytest -q` 返回 `275 passed, 12 skipped, 10 warnings`
- `127.0.0.1:12822` 临时服务真实 HTTP 验证：`DELETE /api/content/type/not-a-type` 和 `DELETE /api/content/source/not-a-source` 均返回 400 和明确 detail；临时服务和 `/tmp/khoj-content-boundary-12822` 已清理
- `uv run pytest tests/test_client.py::test_create_chat_session_accepts_agent_slug_in_json_body -q` 返回 `1 passed`；覆盖 Obsidian 首轮自动建会话 JSON body、旧 query 参数、Web 空 body 默认会话
- `uv run pytest tests/test_client.py -q` 返回 `36 passed, 1 skipped, 9 warnings`
- `git diff --check` 返回 0；`uv run ruff check src/khoj tests` 返回 `All checks passed!`
- `uv run pytest -q` 返回 `276 passed, 12 skipped, 10 warnings`
- `cd src/interface/obsidian && yarn build` 返回成功，生成 `main.js 249.1kb`
- `cd src/interface/web && bun run build` 在当前服务器因 snap `bun` 拒绝 `/data/...` home 外路径失败；改用 `CI=1 NEXT_TELEMETRY_DISABLED=1 CIRCLE_NODE_TOTAL=2 timeout 180s npm run build` 跑同一 `next build`，返回成功
- `uv run pytest tests/test_api_chat_file_kb.py::test_chat_notes_qqbot_write_reports_blocked_tool_result -q` 返回 `1 passed`
- `uv run pytest tests/test_notes_tool_loop.py::test_notes_tool_loop_blocks_qqbot_write -q` 返回 `1 passed`
- `uv run pytest tests/test_notes_tool_loop.py tests/test_api_chat_file_kb.py -q` 返回 `20 passed`
- `git diff --check` 返回 0；`uv run ruff check src/khoj tests` 返回 `All checks passed!`
- `uv run pytest -q` 返回 `277 passed, 12 skipped, 10 warnings`
- `uv run pytest tests/test_client.py::test_content_file_routes_accept_encoded_special_file_names tests/test_client.py::test_get_missing_content_file_returns_not_found -q` 返回 `2 passed`
- `cd src/interface/web && ./node_modules/.bin/eslint app/search/page.tsx && ./node_modules/.bin/tsc --noEmit` 返回 0
- `git diff --check` 返回 0；`uv run ruff check src/khoj tests` 返回 `All checks passed!`
- `uv run pytest -q` 返回 `278 passed, 12 skipped, 10 warnings`
- `cd src/interface/web && CI=1 NEXT_TELEMETRY_DISABLED=1 CIRCLE_NODE_TOTAL=2 timeout 180s npm run build` 返回成功
- `uv run pytest tests/test_client.py::test_set_conversation_title_accepts_encoded_special_title -q` 返回 `1 passed`
- `cd src/interface/web && ./node_modules/.bin/eslint app/components/allConversations/allConversations.tsx app/common/chatFunctions.ts && ./node_modules/.bin/tsc --noEmit` 返回 0
- `cd src/interface/obsidian && yarn build` 返回成功，生成 `main.js 249.2kb`
- `git diff --check` 返回 0；`uv run ruff check src/khoj tests` 返回 `All checks passed!`
- `uv run pytest -q` 返回 `279 passed, 12 skipped, 10 warnings`
- `cd src/interface/web && CI=1 NEXT_TELEMETRY_DISABLED=1 CIRCLE_NODE_TOTAL=2 timeout 180s npm run build` 返回成功
- `uv run pytest tests/test_client.py::test_share_missing_conversation_returns_not_found tests/test_client.py::test_fork_missing_public_conversation_returns_not_found tests/test_client.py::test_delete_missing_public_conversation_returns_not_found -q` 先红后绿；修复后返回 `3 passed`
- `uv run pytest -q` 返回 `292 passed, 12 skipped, 10 warnings`
- `uv run ruff check src/khoj tests` 返回 `All checks passed!`
- `git diff --check` 返回 0
- `USE_EMBEDDED_DB=true PGSERVER_DATA_DIR=/data/ljr/my_project/khoj/pgserver_data uv run python src/khoj/manage.py check` 返回 `System check identified no issues`
- `cd src/interface/web && CI=1 NEXT_TELEMETRY_DISABLED=1 CIRCLE_NODE_TOTAL=2 timeout 180s npm run build` 返回成功
- `cd src/interface/obsidian && yarn build` 返回成功，生成 `main.js 249.2kb`
- `uv run pytest tests/test_client.py::test_generate_chat_title_missing_conversation_returns_not_found tests/test_client.py::test_set_conversation_title_rejects_invalid_conversation_id tests/test_client.py::test_delete_chat_history_rejects_invalid_conversation_id tests/test_client.py::test_delete_missing_message_turn_returns_not_found -q` 先红后绿；修复后返回 `4 passed`
- `uv run pytest tests/test_client.py::test_delete_message_turn_removes_matching_messages -q` 返回 `1 passed`
- `uv run pytest tests/test_agents.py::test_sync_create_conversation_session_accepts_agent_slug -q` 先红后绿；修复后返回 `1 passed`
- `uv run pytest tests/test_agents.py::test_delete_missing_agent_by_slug_returns_false -q` 先红后绿；修复后返回 `1 passed`
- `uv run pytest tests/test_agents.py::test_unauthenticated_user_cannot_read_admin_private_agent tests/test_agents.py::test_non_creator_can_read_public_agent -q` 先红后绿；修复后返回 `2 passed`
- `uv run pytest tests/test_client.py::test_search_rejects_negative_limit -q` 先红后绿；修复后返回 `1 passed`
- `uv run pytest -q` 返回 `297 passed, 12 skipped, 10 warnings`
- `uv run ruff check src/khoj tests` 返回 `All checks passed!`
- `git diff --check` 返回 0
- `USE_EMBEDDED_DB=true PGSERVER_DATA_DIR=/data/ljr/my_project/khoj/pgserver_data uv run python src/khoj/manage.py check` 返回 `System check identified no issues`
- `cd src/interface/web && CI=1 NEXT_TELEMETRY_DISABLED=1 CIRCLE_NODE_TOTAL=2 timeout 180s npm run build` 返回成功
- `cd src/interface/obsidian && yarn build` 返回成功，生成 `main.js 249.2kb`
- `uv run pytest -q` 返回 `300 passed, 12 skipped, 10 warnings`
- `uv run ruff check src/khoj tests` 返回 `All checks passed!`
- `git diff --check` 返回 0
- `USE_EMBEDDED_DB=true PGSERVER_DATA_DIR=/data/ljr/my_project/khoj/pgserver_data uv run python src/khoj/manage.py check` 返回 `System check identified no issues`
- `cd src/interface/web && CI=1 NEXT_TELEMETRY_DISABLED=1 CIRCLE_NODE_TOTAL=2 timeout 180s npm run build` 返回成功
- `cd src/interface/obsidian && yarn build` 返回成功，生成 `main.js 249.2kb`
- `uv run pytest -q` 返回 `301 passed, 12 skipped, 10 warnings`
- `uv run ruff check src/khoj tests` 返回 `All checks passed!`
- `git diff --check` 返回 0
- `USE_EMBEDDED_DB=true PGSERVER_DATA_DIR=/data/ljr/my_project/khoj/pgserver_data uv run python src/khoj/manage.py check` 返回 `System check identified no issues`
- `cd src/interface/web && CI=1 NEXT_TELEMETRY_DISABLED=1 CIRCLE_NODE_TOTAL=2 timeout 180s npm run build` 返回成功
- `cd src/interface/obsidian && yarn build` 返回成功，生成 `main.js 249.2kb`

## 1. 总体结论

当前项目已经具备可复用的 Agent 外壳：

- `/api/chat`：统一聊天入口。
- `Agent` 模型：persona、工具权限、模型配置。
- `Research loop`：多轮 tool call / tool result 循环。
- `Obsidian plugin`：Obsidian 客户端入口、vault 同步、edit block 确认式写回。
- `UserMemory`：长期偏好、薄弱点、学习状态。
- 现有文件工具：`list_files`、`view_file`、`regex_search_files`、`kb_headings`、`kb_resolve_link`。
- 现有 OpenAI/Gemini/Anthropic adapter：保留为显式 fallback。

Hermes 的参考价值不是“搬一个新 Agent”，而是三块实现经验：

- Codex OAuth/Responses API provider：把 Codex 订阅账号作为模型后端使用。
- Gateway platform adapter：QQBot 的连接、鉴权、入站消息归一化和出站分片。
- Memory/Skills 边界：能力放在边缘，核心 loop 保持窄。

因此本项目的目标架构是：

```text
Web / Obsidian / QQBot
  -> Khoj /api/chat
  -> Khoj Agent + Memory + Research loop
  -> CodexConversationAdapter 或显式 API fallback
  -> File-first 本地文件库 / Obsidian vault workspace
  -> OpenKB compiled wiki evidence
  -> 旧文档检索已硬删除
  -> 面试回答 / 追问 / 复盘 / 可确认写回
```

默认模型后端使用 Codex Responses backend。Codex 后端只负责模型推理和 tool_call 决策；工具执行、上下文组织、记忆和写回仍由 Khoj 完成。

## 2. 非目标

- 不替换 `/api/chat`。
- 不直接改写 OpenAI/Gemini/Anthropic adapter。
- 不引入 Hermes 的完整 `AIAgent`、`conversation_loop` 或 gateway。
- 不默认走 `codex app-server`，不让 Codex CLI 接管工具执行。
- 不新增 LangChain、向量库或新的 Agent 框架。
- 不把本地 Obsidian vault 上传成云端知识库作为主路径。
- 不把 Obsidian 插件做成第二套 Agent；插件只是 Khoj server 的客户端和可选同步入口。
- 不把 QQBot 做成第二套业务系统。
- 不默认开放 Code / Operator / 高风险写入工具。
- 不根据固定目录结构写死逻辑；vault 结构从本地说明文件读取。

## 3. Hermes 参考映射

| 需求 | Hermes 可参考点 | 本项目采用方式 |
| --- | --- | --- |
| Codex 订阅额度 | `hermes_cli/auth.py` 的 `openai-codex` OAuth token 读取和 refresh | 新增轻量 `CodexAuthResolver` |
| Codex 请求头 | `originator: codex_cli_rs`、`ChatGPT-Account-ID`、Codex-like `User-Agent` | 在 Codex adapter 的 OpenAI client headers 中设置 |
| Responses API 适配 | `agent/transports/codex.py`、`agent/codex_responses_adapter.py` | 只移植 messages/tools/response 转换思路 |
| Streaming 稳定性 | `responses.create(stream=True)` 低层事件消费 | 第一版优先支持非流式和最小流式，避免依赖 SDK 高层 shape |
| `codex app-server` | Hermes 可把整 turn 交给 Codex CLI | 本项目不采用，避免 Codex 接管工具和文件写入 |
| QQBot | `gateway/platforms/qqbot/adapter.py` | 参考鉴权、白名单、分片、重试；只做薄入口 |
| Memory provider | `MemoryProvider` / `MemoryManager` 生命周期 | 不新增 manager，复用 Khoj `UserMemory`，只参考入口/写入边界 |
| Skills | `skill_bundles` / skill 文件注入 | 用 Obsidian `agents.md` / `index.md` 作为本地 skill-like 指令 |

## 4. 必要修改清单

### A. CodexConversationAdapter

新增一个模型 adapter，而不是改原 adapter。

职责：

- 接收 Khoj 已组装好的 messages、system prompt、tools。
- 读取本机 Codex/Hermes-style auth。
- 调用 `https://chatgpt.com/backend-api/codex/responses`。
- 将 Khoj tool schema 转成 Responses tools。
- 将 Codex 返回的文本、reasoning 摘要、tool_call 转回 Khoj 可消费格式。
- tool result 继续由 Khoj 执行并回传给 Codex backend。
- quota / auth / backend 错误返回清晰错误。

默认配置：

```text
KHOJ_CONVERSATION_RUNTIME=codex
KHOJ_CODEX_AUTH_FILE=~/.hermes/auth.json
KHOJ_CODEX_BASE_URL=https://chatgpt.com/backend-api/codex
KHOJ_CODEX_ORIGINATOR=codex_cli_rs
KHOJ_CODEX_USER_AGENT="codex_cli_rs/0.0.0 (Khoj Interview Agent)"
```

Auth 规则：

- 优先读 `KHOJ_CODEX_AUTH_FILE`。
- 如果没有 Hermes-style auth，可只读检测 `CODEX_HOME/auth.json` 或 `~/.codex/auth.json`。
- 不写 `~/.codex/auth.json`，避免和 Codex CLI / IDE 抢 refresh token。
- 如果需要保存 refresh 后 token，写回 `KHOJ_CODEX_AUTH_FILE`。
- access token 中能解析 `chatgpt_account_id` 时，设置 `ChatGPT-Account-ID`。

Fallback：

```text
KHOJ_CONVERSATION_RUNTIME=api
```

只有显式配置为 `api` 时，才走原 OpenAI/Gemini/Anthropic adapter。这个 fallback 是保留现有能力，不是主路径。

明确不做：

- 不把 Codex 订阅账号伪装成 `OPENAI_API_KEY`。
- 不启动 `codex app-server`。
- 不让 Codex CLI 执行 shell、patch、MCP 或文件工具。

### B. 本地文件库 / Obsidian Vault Profile

本阶段已完成。后端支持两种本地知识库接入模式，第一版都走同一套 file tools 和 root jail。

1. 本地文件库模式：Khoj server 直接读取本机目录，适合自托管和 demo。
2. Obsidian 插件模式：Obsidian Community Plugin 连接 Khoj server，负责插件侧聊天、搜索、相似笔记和可选 vault 同步。

默认先支持本地文件库模式；Obsidian vault 只是一个本地文件库的常见形态。

```text
KHOJ_LOCAL_KB_PATH=/data/ljr/my_project/面试胜利！
KHOJ_OBSIDIAN_VAULT_PATH=/data/ljr/my_project/面试胜利！
KHOJ_ALLOW_VAULT_WRITE=false
```

路径规则：

- `KHOJ_LOCAL_KB_PATH` 是通用本地文件库根目录。
- `KHOJ_OBSIDIAN_VAULT_PATH` 是兼容旧命名的 Obsidian vault 根目录。
- 两者都配置时，优先使用 `KHOJ_LOCAL_KB_PATH`。
- 两者都未配置时，保留原 DB/索引文件工具行为，不启用本地直读。

Obsidian 插件模式：

- 插件设置里的 `Khoj URL` 指向本地 server，例如 `http://127.0.0.1:42110`。
- server 匿名模式运行时，插件不需要配置 API key；非匿名模式继续使用 Khoj API key。
- 插件的 Chat/Search/Find Similar Notes 仍走 Khoj 现有 API，不绕过 `/api/chat`。
- 插件同步 vault 后，本地文件库直读是面试场景主路径；Phase 5 已删除旧文档检索路径。

不要写死目录结构。绑定后先读取 vault 自描述文件：

1. `AGENTS.md` / `agents.md` / `agent.md`
2. `index.md` / `README.md`
3. `*/index.md` / `*/README.md`
4. 入口文件里的 wiki link / markdown link 指向的少量代表文件

当前 vault 已观察到：

- `agents.md`：用户画像、vault 结构、命名规范、行为规则。
- `agent.md`：学习状态维护 Agent。
- `index.md`：总入口。
- `interview/index.md`：面试题入口。
- `experiences/index.md`：面经入口和学习状态。
- `mocs/index.md`：主题路线图。
- `raw/index.md`：原始素材来源。
- `projects/index.md`：项目面试材料。

运行时生成 `local_kb_profile`，兼容字段名 `vault_profile`，不写回：

```text
owner_preferences: 中文、秋招面试、不要过度格式化
primary_dirs: interview, experiences, mocs, raw, projects, notes, daily, weekly
write_rules: 不删已有内容；notes/ 未确认不改主体；新增内容更新对应 index.md
evidence_rules: raw/ 是来源材料；面经来源和标准答案分开保存
progress_files: interview/面试八股学习进度.md, experiences/index.md
```

### C. 本地文件工具

Stage B 已完成现有工具的本地优先实现。Stage C 的目标是在普通 chat Notes 主路径中让模型主动使用文件工具找证据，而不是只预加载 profile 文件。

默认工具：

| Tool | 复用/修改 |
| --- | --- |
| `list_files` | 优先读本地文件库；无本地根目录时保留 DB 行为 |
| `view_file` / `read_file` | 复用现有 view file 语义，限制行数 |
| `regex_search_files` | 优先 grep 本地文件库 |
| `kb_headings` | Stage C 新增，先看 Markdown 结构再读局部 |
| `kb_resolve_link` | Stage C 新增，解析 Obsidian / Markdown 链接 |
| `compiled_references` | `collect_notes_evidence_with_tools()` 只引用实际 `read` 的文件行 |
| `append_note` | 后续写回阶段新增，默认关闭 |
| `propose_edit` | 后续写回阶段复用 Obsidian edit block 确认流程 |

边界：

- 所有 path resolve 后必须在本地知识库根目录内。
- 默认只读 `.md`、`.txt`。
- 单次 read 最多 80 行。
- grep 最多 1000 行结果。
- 写入必须由用户明确触发，并遵守 `agents.md` / `agent.md`。
- 旧语义搜索工具已从个人知识库 Agent 工具面删除。

#### C1. 写入控制机制

当前 demo 的本地文件库 / vault 能被读入 `/api/chat` 上下文，但还没有把写入工具暴露给模型。因此用户说“写进相关文档里”时，模型只能生成待复制内容，不能真正修改文件。这不是文件系统权限问题，而是 Agent runtime 没有可调用的写工具。

2026-06-29 demo 实测结论：

- `KHOJ_OBSIDIAN_VAULT_PATH` 已能让 `/api/chat` 读取 demo vault。
- `KHOJ_ALLOW_VAULT_WRITE=true` 已加入 `.env` 并重启服务。
- 用户在 Web UI 里要求“写进相关文档里”时，模型仍只返回可复制内容，没有产生文件 diff。
- 结论：当前缺口不是部署权限，也不是本地文件系统权限，而是写工具尚未注册到 `/api/chat` 使用的 Agent/tool loop。

因此 `KHOJ_ALLOW_VAULT_WRITE=true` 只能视为写入能力的总开关，不能视为写入能力本身。实现前，Agent 不允许声称“已写入”。

要让 Agent 真正写入，必须同时满足三层控制：

1. 配置层允许写入：

```text
KHOJ_ALLOW_VAULT_WRITE=true
```

默认仍为 `false`。没有打开时，模型只能回答“我可以给出建议补丁”，不能声称已经写入。

2. Tool registry 暴露写工具：

```text
append_note(path, content, heading?)
propose_edit(path, find, replace, reason)
```

工具由 Khoj 执行，不由 LLM 直接访问磁盘。所有 path 仍必须通过 root jail，resolve 后位于本地知识库根目录内。

必要实现点：

- 新增本地文件库写入 helper：负责 root jail、文件类型限制、原子写入和 diff 摘要。
- 将 `append_note` / `propose_edit` 注册到当前 `/api/chat` 实际使用的工具列表，而不是只写 prompt。
- 工具 schema 必须进入模型请求，模型返回 tool call 后由 Khoj 执行，再把 tool result 回传给模型。
- 写入结果要进入 conversation log，便于审计“谁在什么时候改了哪个文件”。

3. Prompt / policy 强制 tool call：

当用户明确说“写入 / 记录 / 更新 / 追加到某文档”时，Agent 必须选择写工具或说明写入被禁用；不能只输出“你可以复制粘贴”。如果写入目标不明确，先追问目标文件；如果内容会覆盖或删除已有文本，必须走 `propose_edit` 的确认式流程。

写入行为矩阵：

| 用户意图 | `KHOJ_ALLOW_VAULT_WRITE=false` | `KHOJ_ALLOW_VAULT_WRITE=true` |
| --- | --- | --- |
| “写进相关文档里” | 返回建议补丁并说明未写入 | 选择目标文件，调用 `append_note` 或 `propose_edit` |
| “记录错题/复盘” | 返回待写入内容 | 追加到约定错题/复盘文件，并更新相关 index |
| “修改原文/删除内容” | 拒绝直接修改，给出 edit proposal | 走 `propose_edit`，等待用户确认 |
| QQ 群聊请求写入 | 默认拒绝 | 仍默认拒绝，除非显式白名单和单聊确认 |

验收方式：

- 写入关闭时，`/api/chat` 不会修改任何 vault 文件，并明确说明写入未执行。
- 写入打开时，用户说“写进 `java_basics.md`”会产生真实文件 diff。
- WebSocket / HTTP 流中能看到 tool call / tool result，而不是纯文本假装完成。
- 每次写入在 conversation log 中保存 tool 名、目标路径、摘要和结果。

### D. Interview Agent 行为

新建 Interview Agent persona，不改核心 Agent 模型。

默认回答格式：

```text
1. 30 秒版
2. 核心要点
3. 高频追问
4. 参考文件路径
5. 是否建议写入复盘/学习状态
```

默认上下文：

- 当前问题和入口：web / obsidian / qqbot。
- 最近会话。
- local kb / vault profile。
- 文件工具读取到的证据片段。
- UserMemory 中的偏好、薄弱点、目标岗位。
- 写入/群聊安全约束。

写入规则：

- “记一下 / 记录错题 / 更新笔记 / 更新学习状态”才写 vault。
- 删除和覆盖必须走 edit block。
- QQ 群聊默认不写 UserMemory，不写 vault。
- 当前 vault 中 `notes/` 主体内容未经确认不改。

### E. QQBot 薄入口

QQBot 只负责入口和回复，不实现 planner、不直接读 vault。

可选实现形态：

- 官方 QQ Bot API：参考 Hermes `QQAdapter`，使用 `QQ_APP_ID` / `QQ_CLIENT_SECRET`、WebSocket Gateway、REST 发送。
- OneBot / NapCat：如果部署目标是个人 QQ 或已有 OneBot 环境，可作为后续 adapter，仍然走同一个薄入口协议。

统一处理：

```text
QQ inbound message
  -> normalize text / chat_id / user_id / group_id
  -> allowlist / rate limit
  -> POST /api/chat?client=qqbot
  -> split long response
  -> send back to QQ
```

## 5. 分阶段实施

### Phase 1：Codex 模型后端最小切换（已完成）

目标：只加 Codex adapter，不碰 `/api/chat` 和旧 adapter。

改动：

- 新增 `CodexConversationAdapter`。
- 新增 `CodexAuthResolver`。
- 新增 `KHOJ_CONVERSATION_RUNTIME=codex|api`。
- 在模型选择层按配置路由。
- 保留 OpenAI/Gemini/Anthropic adapter。
- 启动时检查 Codex auth 文件。
- 可选只读导入 `~/.codex/auth.json`，但不写回该文件。

验收：

- `/api/chat` 仍可用。
- 默认配置走 Codex backend。
- 显式 `api` 配置仍可走原 adapter。
- tool_call 由 Khoj 执行，不由 Codex CLI 执行。
- auth 缺失、过期、quota 用明确错误提示返回。

### Phase 2：本地文件库和 Obsidian 插件读取闭环（已完成）

目标：不依赖旧文档检索，先能基于本地文件库回答；同时保留 Obsidian 插件作为客户端和可选同步入口。

改动：

- 新增本地知识库根目录配置：`KHOJ_LOCAL_KB_PATH`，并兼容 `KHOJ_OBSIDIAN_VAULT_PATH`。
- 新增 local kb / vault profile 读取 helper。
- `list_files` / `view_file` / `regex_search_files` 优先读本地文件库。
- 加 root jail、行数限制、文件类型限制。
- 保留 Obsidian 插件接入：插件配置 `Khoj URL=http://127.0.0.1:42110` 后，Chat/Search/Find Similar Notes 继续走 Khoj server。
- 插件同步得到的 DB 内容不替代本地文件库直读主路径。

验收：

- 能读取 `/data/ljr/my_project/面试胜利！/agents.md` 和各目录 `index.md`。
- 能 grep / read `interview/`、`experiences/`、`mocs/`。
- 越权路径被拒绝。
- 未配置本地文件库时不破坏原 DB 文件工具。
- Obsidian 插件能连本地 Khoj URL 发起聊天；匿名模式下不要求 API key。
- 插件 Force Sync 后，原 Search/Similar Notes 能继续使用；面试问答仍优先引用本地文件路径。

### Stage C：File-first Agent Workspace（已完成，当前工作区待提交）

目标：把普通 chat 的 Notes 主路径从“预加载 profile + 旧文档检索 fallback”升级为“agent 主动 list / grep / headings / read / resolve_link，并只引用实际读过的文件行”。

改动：

- 新增或升级文件工具：`kb_headings`、literal-first `kb_grep`、编号 `kb_read`、`kb_resolve_link`。
- `collect_notes_evidence_with_tools()` 让主聊天模型选择 Notes 工具，并把真实读取过的文件行转换成现有 `compiled_references` 和 UI references。
- 新增 `collect_notes_evidence_with_tools()`，普通 chat Notes 按 `KHOJ_KB_ENGINE` 提供本地 KB / OpenKB 工具。
- 旧 fallback 开关已删除；无文件/OpenKB 证据时不再调用旧文档检索入口。
- summary/file filters 在本地 KB 模式下改用相对路径和 `kb_read`，不依赖 DB file object。

当前工作区实现：

- `src/khoj/utils/local_kb.py`：新增结构化 `kb_list`、literal-first `kb_grep`、编号 `kb_read`、`kb_headings`、`kb_resolve_link`。
- `src/khoj/processor/conversation/notes_tool_loop.py`：新增 `collect_notes_evidence_with_tools()`，由主聊天模型通过工具调用选择 list/grep/headings/read/link/OpenKB/write/propose 操作，并把精确行证据转成 references。
- `src/khoj/routers/api_chat.py`：Notes 默认走主 agent tool loop；OpenKB 显式开启时作为可选工具交给同一个 loop；不再调用 legacy document-search fallback。
- `src/khoj/routers/helpers.py` / `src/khoj/routers/research.py` / `src/khoj/utils/helpers.py`：research 文档工具复用同一套本地 toolkit，并暴露 `kb_headings`、`kb_resolve_link`。
- summary/file filters 在本地 KB root 下使用 `kb_read`，不直接依赖 DB `FileObject.raw_text`。

验证：

- `uv run ruff check src/khoj/utils/local_kb.py src/khoj/processor/conversation/notes_tool_loop.py src/khoj/routers/helpers.py src/khoj/routers/research.py src/khoj/routers/api_chat.py tests/test_local_kb.py tests/test_notes_tool_loop.py tests/test_research_document_tools.py tests/test_api_chat_file_kb.py tests/test_local_kb_summary.py`
- `uv run pytest tests/test_local_kb.py tests/test_notes_tool_loop.py tests/test_api_chat_file_kb.py tests/test_local_kb_summary.py tests/test_research_document_tools.py tests/test_local_kb_fallback.py tests/test_grep_files.py -q`
- Direct check against `/data/ljr/my_project/面试胜利！/` covered Redis/cache exact miss, vault structure, route-map link following, and no-evidence query behavior.

验收：

- 无 DB entries、无旧检索模型时，Notes 仍能通过本地文件工具回答。
- 最终引用只来自 `kb_read` 证据。
- 没找到证据时说明搜索过什么，而不是硬答。
- research 文档工具和 chat Notes 共用同一套 file toolkit。

### Phase 3：Interview Agent 和安全写回（已完成）

目标：形成面试问答、追问、复盘的可用体验。

改动：

- 面试回答策略放回 Agent persona / 用户自然语言指令，不再由后端词表注入。
- 中文面试/写入意图不再用后端词表强制走 Notes；主数据源选择和显式 `/notes` 决定是否进入 Notes tool loop。
- `collect_notes_evidence_with_tools()` 把本地 KB 工具交给主聊天模型规划；后端只执行工具、限额和整理 references。
- 新增 `append_local_kb_note()`，默认由 `KHOJ_ALLOW_VAULT_WRITE=false` 关闭；打开后仍走 root jail、文本后缀限制和原子写入。
- 新增 `propose_local_kb_edit()`，只产出 diff，不修改文件，覆盖/删除类请求不直接落盘。
- `/api/chat` 只在模型明确调用 `append_note` / `propose_edit` 时执行受控本地写入工具，并把结果放入 references 和 conversation log 上下文。
- QQBot client 写入默认禁用；Memory 写入仍沿用现有 `/api/chat` 权限边界。

设计修正：

- 当前普通 chat 主路径不是完整 LLM tool-call loop，真实工具循环主要在 research。Phase 3 不新增第二套 agent loop，不强行让模型直接拿工具 schema 写磁盘；采用更小的受控写入执行器。这样用户明确要求“写进某文档”时能产生真实文件 diff，禁用或不明确时也会把未执行原因写入上下文，避免假装完成。

验收：

- 面试问题能输出“短答 -> 要点 -> 追问 -> 引用”。
- 引用路径来自本地文件库 / vault。
- 写入不会静默修改 notes 主体。
- 写入关闭时不会输出“已经写入”；写入开启时能产生真实文件 diff。
- 覆盖/删除类请求只生成 edit proposal，不直接改原文。
- QQ 群聊默认禁写。

### Phase 4：QQBot 薄适配和评估（已完成）

目标：增加入口，不改 Agent 核心。

改动：

- 新增 `src/khoj/integrations/qqbot/adapter.py` 和包入口。
- `normalize_inbound()` 支持官方 QQ Gateway 风格 payload：`content`、`author.id`、`channel_id`、`guild_id`。
- `is_allowed()` 默认空 allowlist 拒绝，允许 user/channel/group 任一命中。
- `split_response()` 对长回复做本地分片。
- `handle_message()` 只归一化并调用注入的 `chat_client(text, client="qqbot", user_id, chat_id, group_id)`；不直接访问 vault、不实现 planner。
- 新增 `tests/test_notes_tool_loop.py`，离线覆盖 LLM 选择读文件、LLM 选择写入、QQ 写入默认禁用和 OpenKB 工具调用。

设计边界：

- 本阶段不接真实 QQ Gateway，不保存 QQ 凭据，不启动后台 websocket。真实部署只需要把官方 QQ 或 OneBot/NapCat 事件转成 `handle_message()` payload，再由 chat client 调 `/api/chat?client=qqbot`。

验收：

- QQBot 不直接访问 vault。
- QQBot 不实现 planner。
- 所有业务仍走 `/api/chat`。
- gold set 能跑出引用命中、是否读对文件、是否胡编、写入是否安全。

### Phase 5：会话级 Artifact 写入协议（已完成）

目标：解决多轮对话中“上一轮规划正确、下一轮写入时内容漂移”的工作记忆问题。

改动：

- `ChatMessageModel` 保留 `artifacts` 字段，避免 conversation 保存时丢失可引用工作状态。
- `save_to_conversation_log()` 为每轮 assistant 最终回答生成 `assistant:{turnId}` artifact，并保留最小 `source_refs`。
- Notes planner prompt 注入最近 conversation artifacts catalog。
- `append_note` 支持 `artifact_id`；无 `content` 时直接写入 artifact 原文，有 `content` 时作为 artifact 改写走 grounding verifier。
- `source_refs` 支持 `{type: "artifact", id: "assistant:<turnId>"}`。

验收：

- `uv run pytest tests/test_notes_tool_loop.py -q`
- `uv run pytest tests/test_api_chat_file_kb.py -q`
- `uv run pytest tests/test_client.py::test_chat_history_returns_obsidian_session_shape tests/test_client.py::test_delete_chat_history_rejects_invalid_conversation_id tests/test_client.py::test_delete_missing_chat_history_returns_not_found tests/test_client.py::test_delete_empty_chat_history_id_does_not_clear_all -q`
- `uv run ruff check src/khoj/processor/conversation/notes_tool_loop.py src/khoj/processor/conversation/utils.py src/khoj/database/models/__init__.py tests/test_notes_tool_loop.py`

下一步：用真实 UI 手测“规划学习日记 → 写入学习日记”，确认模型会优先选择 `artifact_id`。

## 6. 主要代码触点

| 文件 | 必要改动 |
| --- | --- |
| `src/khoj/processor/conversation/codex/` | 新增 Codex auth、Responses payload、response normalize |
| `src/khoj/processor/conversation/codex/gpt.py` | 新增 `CodexConversationAdapter` 或同等入口 |
| `src/khoj/database/adapters/__init__.py` | 模型选择层按 `KHOJ_CONVERSATION_RUNTIME` 路由 |
| `src/khoj/routers/helpers.py` | 本地文件库 file tools、chat runtime 接入点 |
| `src/khoj/utils/helpers.py` 或 `src/khoj/utils/vault.py` | local kb / vault profile 读取和 root jail |
| `src/khoj/routers/research.py` | 复用 tool loop，默认只暴露 file-first 文档工具 |
| `src/khoj/processor/conversation/prompts.py` | Interview Agent persona 和回答格式 |
| `src/interface/obsidian/src/settings.ts` | 默认本地 URL，减少 Cloud 文案；保留 API key/匿名模式配置 |
| `src/interface/obsidian/src/chat_view.ts` | 面试快捷命令 |
| `src/interface/obsidian/src/interact_with_files.ts` | 继续复用确认写回和插件侧文件交互 |
| `src/khoj/integrations/qqbot/` | 新增 QQBot 薄 adapter |
| `tests/test_codex_conversation_adapter.py` | auth、headers、payload、tool_call normalize |
| `tests/test_interview_vault_tools.py` | local kb / vault profile、root jail、read/grep/append |
| `tests/test_qqbot_adapter.py` | 入站归一化、白名单、分片、调用 `/api/chat` |

## 7. 测试策略

最小测试顺序：

1. Codex adapter unit tests：不打真实网络，验证 auth 解析、headers、payload、tool call normalize。
2. Codex adapter smoke：在本机已有 Codex auth 时，真实调用一次 Codex backend。
3. Local KB file tools：用临时目录测 root jail、read、grep、写入禁用；同一组测试覆盖 Obsidian vault 目录。
4. Write tools：写入关闭时不改文件；写入开启时 `append_note` 改临时 vault；`propose_edit` 只产出待确认 patch。
5. Obsidian plugin integration：插件指向本地 Khoj URL，验证 chat 请求、匿名/API key 两种配置、Force Sync 不破坏本地直读路径。
6. `/api/chat` integration：client 分别为 `web`、`obsidian`、`qqbot`，验证上下文权限。
7. Gold set：八股单点、多跳追问、项目经历、无答案拒答、写入确认。
8. 全量 pytest：确认不破坏现有 Khoj 行为。

## 8. 推迟项

- 完整 Hermes gateway：不做。
- `codex app-server` runtime：不做，除非未来明确要让 Codex CLI 接管整 turn。
- PDF 深度解析：交给 Phase 5D 的 OpenKB PageIndex。
- 旧检索保留评测：不做。后续只做 file-first / OpenKB 覆盖率、引用准确率和长文命中率评测。
- Operator 默认开放：不做。
- 大规模迁移数据库模型：不做。
- 重命名整个项目为 Hermes：不做。

## 9. 面试讲法

核心口径：

> 我没有重写 Khoj，而是复用了它已有的 `/api/chat`、Agent、Research loop、Memory 和 Obsidian 插件。我的改造点是：模型层新增 CodexConversationAdapter，本地文件库工具优先读本机目录或 Obsidian vault，Interview Agent 加面试场景 persona 和安全写回规则，QQBot 作为薄入口接入 `/api/chat`。Phase 5 用 OpenKB 做 compiled wiki evidence，并硬删除旧文档检索栈。

如果问为什么要删除旧文档检索：

> 这个 vault 已经有 `agents.md`、`index.md`、`interview/index.md`、`experiences/index.md`、MOC 等结构化入口。file-first 能直接引用真实文件行，OpenKB 能把多文档资料编译成 summaries、concepts、entities 和 PageIndex 页码证据。两者合起来可以覆盖原先旧文档检索的职责，而且引用更可审计、知识更可维护，所以旧链路已删除。

如果问 Codex 订阅：

> 不是把订阅账号伪装成 OpenAI API key，也不是让 Codex CLI 接管工具。做法参考 Hermes：读取本机 ChatGPT/Codex OAuth token 调 Codex backend，Codex 负责模型推理和 tool_call 决策，Khoj 负责上下文、工具执行、记忆和最终保存。
