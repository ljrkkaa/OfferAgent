# Agent Harness Workers and Observability Spec

日期：2026-06-30

状态：第一版统一 runtime 已实现，待真实模型/浏览器复测。

## 2026-07-01 Update: Default Chat Uses Unified Runtime

调试真实 “Agent 评估八股 + 补充到项目里” 流程后，本阶段架构从“保留浅 source router，再接窄 worker”调整为更接近 Claude Code 的单主运行时：默认聊天不再先被 `aget_data_sources_and_output_format()` 分流成 `notes` / `online` / `general`，而是进入一个统一 agent tool loop，由同一个 planner 在一轮任务内调用 web、local KB、OpenKB、writeback 和 verifier。显式 slash commands（例如 `/notes`、`/online`、`/webpage`、`/research`）保留当前专用路径，降低迁移风险。

中文实施计划见 `docs/superpowers/plans/2026-07-01-unified-claude-style-agent-runtime-zh.md`，英文版见 `docs/superpowers/plans/2026-07-01-unified-claude-style-agent-runtime.md`。第一版实现已落到 `src/khoj/processor/conversation/agent_tool_loop.py`：默认聊天删除浅 source router 依赖，进入统一 planner，可在同一轮内调用 `web_search` / `read_webpage`、local KB / OpenKB Notes 工具、`append_note` / `propose_edit`，并把 write result 绑定到最终回答上下文；旧 `aget_data_sources_and_output_format()`、`pick_relevant_tools` prompt 和旧 source/output mode 选择测试已删除。Codex `json_object` 请求也已修复，避免 system prompt 被抽到 `instructions` 后触发 Responses API 400。验证覆盖 `tests/test_agent_tool_loop.py`、`tests/test_api_chat_file_kb.py`、`tests/test_notes_tool_loop.py`、`tests/test_codex_conversation_adapter.py` 和 `tests/test_research_document_tools.py` 的相关路径；`uv run ruff check src/khoj tests` 通过，`uv run pytest -q` 为 `385 passed, 12 skipped`。下一步是用真实模型和真实浏览器/Obsidian 流程复测 “根据网络资料补充 agent 评估八股内容 → 补充到项目里”。

## 1. 目标

本阶段目标是在现有 Khoj `/api/chat` 主链路上，借鉴 Claude Code 的 Agent Harness 思路，但只落地当前面试个人知识库真正需要的部分：

- 主 Agent Loop：继续由 `/api/chat` 承担用户对话、persona、memory、tool loop、最终回答和写回。
- 工具 Schema：保留 schema 作为模型说明、参数解析、日志和测试依据，默认信任工具调用，不新增复杂权限确认。
- 子智能体：增加少量 worker，用于本地知识库检索、OpenKB 检索和答案校验。
- 记忆边界：只存用户偏好、学习状态和明确决策，不复制知识库原文。
- 可观测性：记录每轮 agent turn、worker、tool、reference 和 writeback 事件，方便排查假成功、漏读资料和工具失败。

一句话验收：用户从 Web / Obsidian / QQBot 进入 `/api/chat` 后，主 Agent 可以调度窄 worker 找证据、生成最终回答，并留下结构化运行日志；系统不新增第二套聊天框架。

## 2. 非目标

本阶段明确不做：

- 不替换 `/api/chat`。
- 不新增 LangChain、AutoGen、CrewAI 或其他 Agent 框架。
- 不重做上下文压缩；先继续使用现有上下文和 evidence caps。
- 不做 Plan 模式或用户确认 gate。
- 不做复杂权限管线；部署层和现有 root jail 足够当前本地场景。
- 不做插件市场或通用 worker marketplace。
- 不允许 worker 递归创建 worker。
- 不让 worker 直接面向用户输出最终答案。
- 不让 worker 直接写文件；写回仍由主 Agent 通过现有工具触发。
- 不把 OpenKB 查询结果作为最终答案透传。

## 3. 总体架构

采用单主循环加少量 worker 的结构：

```text
Web / Obsidian / QQBot
  -> /api/chat
     -> Agent persona / local instructions / memory
     -> Tool Registry with schema
     -> Worker Dispatcher
        -> kb-researcher
        -> openkb-researcher
        -> answer-verifier
     -> Evidence Binder
     -> Final Answer Generator
     -> optional append_note / propose_edit / save_exploration
     -> Observability Event Log
```

主 Agent 是唯一的用户对话角色。Worker 是内部执行单元，只返回结构化结果。

第一版 dispatcher 可以是普通 Python 函数，不需要队列、进程池或新的 runtime。只要能把任务、工具集、上下文和日志串起来即可。

## 4. 主 Agent Loop

主链路继续复用现有 `/api/chat`、`collect_notes_evidence_with_tools()`、local KB 工具、OpenKB 工具和写回工具。

每次请求的逻辑顺序：

```text
1. 接收用户问题。
2. 绑定 Agent.personality、UserMemory、vault agents.md / index.md、local skills。
3. 判断是否需要本地 KB worker、OpenKB worker、答案校验 worker。
4. Worker 收集证据，返回 references / notes / errors。
5. Evidence Binder 合并、去重、裁剪 references。
6. 主 Agent 生成最终中文回答。
7. 如果用户明确要求保存或写入，主 Agent 调用现有写回工具。
8. 输出 SSE / HTTP 响应。
9. 写结构化 observability events。
```

当前 Notes 主 tool loop 已经接近这个形态。本阶段优先把隐含流程命名清楚，并在代码上补一个薄 dispatcher，而不是拆出新框架。

## 5. 工具 Schema 和权限

工具继续用 `ToolDefinition` schema：

```text
schema 用途：
- 告诉模型工具名、参数和语义。
- 解析 JSON tool call。
- 生成稳定测试输入。
- 写入 observability event。
```

默认信任工具调用：

```text
allow by default
no confirmation prompt
no per-tool approval UI
no Plan mode gate
```

仍然保留已有硬边界：

- 本地文件路径必须留在 local KB / vault root 内。
- OpenKB 路径必须留在 `wiki/` root 内。
- QQBot 默认可以继续禁写，避免聊天入口误写。
- malformed args 返回工具错误，不让主 Agent 声称成功。

这些是运行安全边界，不是交互式权限系统。

## 6. Memory 规则

只写入无法从资料重新推导的长期信息：

- 用户偏好：中文、面试风格、回答长度、不要过度格式化。
- 长期学习状态：薄弱点、最近复盘、准备方向。
- 明确决策：项目经历采用哪个叙事版本、某类问题的固定回答策略。

不写入：

- vault 原文。
- OpenKB wiki 原文。
- 一次性工具搜索结果。
- 可通过文件路径和 references 重新读取的事实。

Worker 可以读取 memory 上下文，但第一版只有主 Agent 能触发 memory 写入。

## 7. Worker 设计

第一版只需要三个 worker。

### 7.1 kb-researcher

职责：

- 用 `list_files`、`regex_search_files`、`kb_headings`、`view_file`、`kb_resolve_link` 读取本地 vault / local KB。
- 返回 line-based references。
- 不生成最终回答。
- 不写文件。

输出示例：

```json
{
  "worker": "kb-researcher",
  "status": "ok",
  "references": [
    {
      "uri": "local-kb://agents.md#L1-L30",
      "file": "agents.md",
      "compiled": "# agents.md L1-L30\n..."
    }
  ],
  "notes": "找到用户画像和 vault 写作规则。",
  "errors": []
}
```

### 7.2 openkb-researcher

职责：

- 仅在 `KHOJ_ENABLE_OPENKB=true` 且 `KHOJ_KB_ENGINE=openkb|hybrid` 时启用。
- 调用 `wiki_search_documents()`。
- 返回 compiled wiki references 和 PageIndex evidence。
- 不把 OpenKB 答案透传给用户。

### 7.3 answer-verifier

职责：

- 在主 Agent 生成最终答案前或后做轻量校验。
- 检查答案是否依赖了不存在的 evidence。
- 检查是否出现“已写入”但写回工具失败。
- 检查面试回答是否偏离用户问题。

第一版 verifier 可以只返回文字 notes 和 blocking errors。不要做复杂评分系统。

## 8. Worker Dispatcher

Dispatcher 输入：

```json
{
  "query": "...",
  "conversation_id": "...",
  "client_app": "web",
  "agent": "...",
  "memory": "...",
  "allow_local_kb": true,
  "allow_openkb": false
}
```

Dispatcher 输出：

```json
{
  "references": [],
  "searched": [],
  "worker_results": [],
  "errors": []
}
```

调度规则保持简单：

- 有 local KB root 时启用 `kb-researcher`。
- OpenKB ready 且 engine 允许时启用 `openkb-researcher`。
- 有 references 或写回意图时启用 `answer-verifier`。
- Worker 失败时记录错误，主 Agent 可以继续，但必须在最终回答中避免假装证据完整。

第一版 worker 顺序执行即可。并发等真实延迟变成问题后再加。

## 9. Observability

增加结构化事件日志，先用现有 Python logger，不引入 tracing 依赖。

事件类型：

```text
agent_turn_started
agent_turn_completed
agent_turn_failed
worker_started
worker_completed
worker_failed
tool_called
tool_completed
tool_failed
reference_bound
write_attempted
write_completed
write_failed
```

事件字段：

```json
{
  "event": "tool_completed",
  "conversation_id": "c1",
  "client_app": "web",
  "worker": "kb-researcher",
  "tool": "view_file",
  "status": "ok",
  "duration_ms": 42,
  "reference_count": 1,
  "error": ""
}
```

日志要求：

- 不记录 access token、API key、完整用户隐私原文。
- 可以记录相对文件路径、工具名、reference 数量和错误类型。
- 写入失败必须有 `write_failed`。
- Worker 异常必须有 `worker_failed`。

先满足本地 debug；后续需要 UI 时再读这些事件做 dashboard。

## 10. 错误处理

最小规则：

- Worker 失败：主 Agent 继续生成回答，但不能声称已完整检索。
- 所有 evidence 为空：回答应明确资料不足，不编造 vault 内容。
- 写入失败：停止生成“已写入”，返回失败原因。
- 工具参数错误：记录 `tool_failed`，把错误交回主 loop。
- Verifier 发现 hard error：最终回答改成错误说明或请求用户补充资料。

## 11. 测试计划

只补最小覆盖：

- `tests/test_agent_workers.py`
  - dispatcher 会按 local KB / OpenKB 可用性选择 worker。
  - worker 不允许递归调用 worker。
  - worker 不直接写文件。

- `tests/test_notes_tool_loop.py`
  - worker references 会进入最终 `compiled_references`。
  - worker 失败不会产生假成功回答。
  - OpenKB worker 只返回 evidence，不返回最终答案。

- `tests/test_agent_observability.py`
  - tool call / worker / writeback 会产生日志事件。
  - 写入失败会记录 `write_failed`。

文档-only 本阶段不要求跑 pytest。实现阶段至少跑新增测试和现有 Notes/OpenKB 回归。

## 12. 实施顺序

推荐最短路径：

1. 新增轻量 worker result dataclass / typed dict。
2. 把现有 Notes evidence collection 包成 `kb-researcher`。
3. 把 `wiki_search_documents()` 包成 `openkb-researcher`。
4. 增加 `log_agent_event()` 小函数，统一结构化日志字段。
5. 在 `/api/chat` Notes path 接入 dispatcher。
6. 增加 answer verifier 的最小 hard-error 检查。
7. 补最小测试。

跳过独立服务、后台队列、权限 UI、dashboard 和上下文压缩。等 worker 数量或日志查询需求真的起来再加。

## 13. 验收标准

- `/api/chat` 仍是唯一聊天入口。
- 本地 KB 和 OpenKB evidence 仍进入现有 `compiled_references`。
- Worker 不直接回复用户，不直接写文件，不递归创建 worker。
- 工具 schema 仍可被模型和测试使用。
- 权限默认开放，不出现每次确认。
- Memory 只记录长期偏好、状态和决策。
- 每次 worker/tool/writeback 成功或失败都有结构化日志。
- 资料不足、工具失败、写入失败时不会假成功。
