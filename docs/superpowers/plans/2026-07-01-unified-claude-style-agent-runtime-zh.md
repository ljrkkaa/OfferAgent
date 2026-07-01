# Claude 风格统一 Agent Runtime 实施计划

> **给 agent 执行者：** 逐个 checkbox 执行。每个任务先写最小回归测试，再改最少代码。不要新增 Agent 框架、队列、权限 UI 或规则词表。

**目标：** 把默认聊天从“先猜数据源再走固定链路”改成一个统一 tool loop，让同一轮任务可以连续使用 web、local KB、OpenKB 和写回工具，并给出 grounded 的最终回答。

**架构：** 参考 Claude Code 的形状：一个主 agent 负责用户回合，项目说明和记忆作为上下文，工具通过 schema 暴露，少量窄 worker 只做内部检索/校验，生命周期事件用于排查。显式命令 `/notes`、`/online`、`/webpage`、`/research` 先保持原路径，第一版只迁移 `ConversationCommand.Default`。

**技术栈：** Python 3.10-3.12、FastAPI streaming route、现有 `ToolDefinition`、Codex/OpenAI JSON responses、`notes_tool_loop.py`、`local_kb.py`、`online_search.py`、pytest。

---

## 1. 当前问题

真实失败链路是：

```text
用户：根据网络资料补充一些 agent 评估八股内容
系统：默认聊天先由 aget_data_sources_and_output_format() 做 source 选择
结果：可能只产出普通回答，web evidence / assistant artifact / writeback 没进入同一个 planner

用户：补充到项目里
系统：Notes loop 才开始规划写入
结果：它看不到上一轮在线资料的可靠 artifact，且 Codex json_object 请求还可能 400
```

坏点不是 Redis，也不是少一组关键词规则。坏点是架构把“找网络资料、整理回答、写入项目”拆在不同链路里，默认聊天没有一个统一 planner 能跨工具完成任务。

## 2. 设计原则

- 不加关键词规则：语义选择交给模型的结构化 tool call，代码只管协议、权限、路径、安全和预算。
- 不引入新 Agent 框架：第一版用普通 Python 函数和现有工具。
- 不重写 `/notes`：复用已有 Notes 工具、写入安全、grounding verifier。
- 不让 worker 写文件：写入只走主 runtime 的 `append_note` / `propose_edit`。
- 不动显式 slash command：降低迁移风险。

## 3. 参考 Claude 设计如何落到 Khoj

| Claude 设计 | Khoj 落点 |
| --- | --- |
| Project memory / `CLAUDE.md` / `AGENTS.md` | `agents.md`、vault profile、local skills、UserMemory 作为 runtime context |
| MCP / tools schema | 继续用 `ToolDefinition`，把 web/local KB/writeback 放进同一个 registry |
| Subagents | 第一版只做窄 worker 概念：`kb-researcher`、`web-researcher`、`answer-verifier`，可先是函数 |
| Hooks / permissions | 不做 UI 确认；保留 root jail、`KHOJ_ALLOW_VAULT_WRITE`、QQBot 禁写、source grounding |
| 单主 agent | `ConversationCommand.Default` 进入 `agent_tool_loop.py`，由同一个 planner 决定工具顺序 |

参考链接：

- https://docs.anthropic.com/en/docs/claude-code/overview
- https://docs.anthropic.com/en/docs/claude-code/memory
- https://docs.anthropic.com/en/docs/claude-code/sub-agents
- https://docs.anthropic.com/en/docs/claude-code/mcp
- https://docs.anthropic.com/en/docs/claude-code/hooks
- https://docs.anthropic.com/en/docs/claude-code/security

## 4. 文件结构

- 新增 `src/khoj/processor/conversation/agent_tool_loop.py`
  - 统一 runtime 的深模块边界。
  - 输出 `AgentToolLoopResult`，不直接生成最终回答。
  - 负责 tool registry、planner prompt、执行循环、transcript、references、write result context。

- 修改 `src/khoj/processor/conversation/codex/utils.py`
  - 修 Codex Responses `json_object` 请求：即使 system prompt 被抽到 `instructions`，`input` 里也要有 JSON hint。

- 修改 `src/khoj/routers/api_chat.py`
  - 只把 `ConversationCommand.Default` 接到统一 runtime。
  - 显式 `/notes`、`/online`、`/research` 保持当前逻辑。

- 小改 `src/khoj/processor/conversation/notes_tool_loop.py`
  - 保持现有行为。
  - 必要时导出已有工具定义，避免在新 runtime 里重复写 schema。

- 小改 `src/khoj/processor/conversation/utils.py`
  - 让 assistant artifact 能携带 online source metadata，方便下一轮“补充到项目里”引用上一轮结果。

- 新增/修改测试：
  - `tests/test_codex_conversation_adapter.py`
  - `tests/test_agent_tool_loop.py`
  - `tests/test_api_chat_file_kb.py`

## 5. 任务 1：先修 Codex `json_object` 400

**原因：** 当前 `build_codex_response_kwargs()` 会把 system message 放进 `instructions`，Responses API 的 `json_object` 检查可能只看 `input`，所以即使 system prompt 写了 JSON，后端仍报 “input messages must contain json”。

**文件：**

- 修改：`src/khoj/processor/conversation/codex/utils.py`
- 修改：`tests/test_codex_conversation_adapter.py`

- [ ] **Step 1：写失败测试**

在 `tests/test_codex_conversation_adapter.py` 增加：

```python
def test_json_object_payload_keeps_json_hint_in_input_when_system_prompt_is_extracted():
    kwargs = build_codex_response_kwargs(
        [
            ChatMessage(role="system", content="Return a machine readable object."),
            ChatMessage(role="user", content="plan the tool call"),
        ],
        model="gpt-test",
        response_type="json_object",
    )

    assert kwargs["text"] == {"format": {"type": "json_object"}}
    assert "instructions" in kwargs
    assert "json" in json.dumps(kwargs["input"]).lower()
```

- [ ] **Step 2：确认测试先失败**

```bash
uv run pytest tests/test_codex_conversation_adapter.py::test_json_object_payload_keeps_json_hint_in_input_when_system_prompt_is_extracted -q
```

预期：失败，`kwargs["input"]` 里没有 `json`。

- [ ] **Step 3：最小实现**

在 `src/khoj/processor/conversation/codex/utils.py` 增加一个小 helper：

```python
JSON_OBJECT_SENTINEL = "Return a JSON object."


def _ensure_json_object_hint(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    if "json" in json.dumps(messages).lower():
        return messages
    return [{"role": "user", "content": JSON_OBJECT_SENTINEL}, *messages]
```

然后在 `response_type == "json_object"` 分支前调用：

```python
if response_type == "json_object" and not response_schema:
    kwargs["input"] = _ensure_json_object_hint(kwargs["input"])
```

- [ ] **Step 4：跑测试**

```bash
uv run pytest tests/test_codex_conversation_adapter.py -q
```

预期：Codex adapter 测试通过。

## 6. 任务 2：新增统一 runtime 返回结构

**文件：**

- 新增：`src/khoj/processor/conversation/agent_tool_loop.py`
- 新增：`tests/test_agent_tool_loop.py`

- [ ] **Step 1：写最小类型测试**

```python
from khoj.processor.conversation.agent_tool_loop import AgentToolLoopResult


def test_agent_tool_loop_result_defaults_are_empty():
    result = AgentToolLoopResult()

    assert result.references == []
    assert result.inferred_queries == []
    assert result.online_results == {}
    assert result.program_context == []
    assert result.searched == []
    assert result.errors == []
```

- [ ] **Step 2：新增最小 dataclass**

```python
from dataclasses import dataclass, field
from typing import Any


@dataclass
class AgentToolLoopResult:
    references: list[dict[str, Any]] = field(default_factory=list)
    inferred_queries: list[str] = field(default_factory=list)
    online_results: dict[str, Any] = field(default_factory=dict)
    program_context: list[str] = field(default_factory=list)
    searched: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
```

- [ ] **Step 3：跑测试**

```bash
uv run pytest tests/test_agent_tool_loop.py::test_agent_tool_loop_result_defaults_are_empty -q
```

预期：通过。

## 7. 任务 3：实现 planner 协议，不写关键词规则

**文件：**

- 修改：`src/khoj/processor/conversation/agent_tool_loop.py`
- 修改：`tests/test_agent_tool_loop.py`

- [ ] **Step 1：测试 JSON tool call parser**

```python
from khoj.processor.conversation.agent_tool_loop import parse_agent_tool_calls


def test_parse_agent_tool_calls_accepts_json_object():
    calls = parse_agent_tool_calls(
        '{"calls":[{"name":"web_search","args":{"query":"Agent evaluation benchmarks"},"id":"1"}]}'
    )

    assert calls[0].name == "web_search"
    assert calls[0].args == {"query": "Agent evaluation benchmarks"}
    assert calls[0].id == "1"


def test_parse_agent_tool_calls_rejects_plain_text():
    assert parse_agent_tool_calls("I can answer directly.") == []
```

- [ ] **Step 2：实现 parser**

```python
import json

from khoj.processor.conversation.utils import ToolCall, load_complex_json


def parse_agent_tool_calls(raw: str) -> list[ToolCall]:
    try:
        payload = load_complex_json(raw)
    except Exception:
        return []
    calls = payload.get("calls") if isinstance(payload, dict) else payload
    if not isinstance(calls, list):
        return []
    parsed = []
    for index, call in enumerate(calls):
        if not isinstance(call, dict) or not call.get("name"):
            continue
        parsed.append(ToolCall(name=call["name"], args=call.get("args") or {}, id=call.get("id") or str(index + 1)))
    return parsed
```

- [ ] **Step 3：新增 planner prompt**

```python
AGENT_TOOL_SYSTEM_PROMPT = """
You are the tool planner for the main Khoj chat answer.
Return only a json object: {"calls":[{"name":"...", "args":{...}, "id":"1"}]}.
Use tools when the user asks for current web information, personal knowledge base evidence, or writeback.
Do not decide intent with keywords. Choose tools from task meaning and available context.
When enough evidence is collected, return {"calls":[]}.
""".strip()
```

- [ ] **Step 4：跑测试**

```bash
uv run pytest tests/test_agent_tool_loop.py -q
```

预期：通过。

## 8. 任务 4：把现有工具放进一个 registry

**文件：**

- 修改：`src/khoj/processor/conversation/agent_tool_loop.py`
- 可选小改：`src/khoj/processor/conversation/notes_tool_loop.py`

- [ ] **Step 1：写 registry 测试**

```python
from khoj.processor.conversation.agent_tool_loop import build_agent_tool_registry


def test_agent_tool_registry_exposes_web_kb_and_write_tools():
    tools = build_agent_tool_registry(allow_local_kb=True, allow_openkb=True, allow_web=True)

    assert {"web_search", "view_file", "regex_search_files", "append_note", "propose_edit"} <= set(tools)
```

- [ ] **Step 2：最小 registry**

```python
from dataclasses import dataclass
from typing import Awaitable, Callable

from khoj.processor.conversation.notes_tool_loop import APPEND_NOTE_TOOL, OPENKB_TOOL, PROPOSE_EDIT_TOOL
from khoj.utils.helpers import ConversationCommand, ToolDefinition, tools_for_research_llm


@dataclass(frozen=True)
class AgentRuntimeTool:
    definition: ToolDefinition
    execute: Callable[..., Awaitable[dict]]


def build_agent_tool_registry(*, allow_local_kb: bool, allow_openkb: bool, allow_web: bool) -> dict[str, ToolDefinition]:
    registry: dict[str, ToolDefinition] = {}
    if allow_web:
        registry["web_search"] = tools_for_research_llm[ConversationCommand.SearchWeb]
    if allow_local_kb:
        for command in (
            ConversationCommand.ViewFile,
            ConversationCommand.ListFiles,
            ConversationCommand.KbHeadings,
            ConversationCommand.KbResolveLink,
            ConversationCommand.RegexSearchFiles,
        ):
            tool = tools_for_research_llm[command]
            registry[tool.name] = tool
        registry[APPEND_NOTE_TOOL.name] = APPEND_NOTE_TOOL
        registry[PROPOSE_EDIT_TOOL.name] = PROPOSE_EDIT_TOOL
    if allow_openkb:
        registry[OPENKB_TOOL.name] = OPENKB_TOOL
    return registry
```

- [ ] **Step 3：跑测试**

```bash
uv run pytest tests/test_agent_tool_loop.py::test_agent_tool_registry_exposes_web_kb_and_write_tools -q
```

预期：通过。

## 9. 任务 5：接入 web 搜索工具

**文件：**

- 修改：`src/khoj/processor/conversation/agent_tool_loop.py`
- 修改：`tests/test_agent_tool_loop.py`

- [ ] **Step 1：测试 web result 会进入 `online_results`**

```python
import pytest

from khoj.processor.conversation.agent_tool_loop import AgentToolLoopResult, run_web_search_tool


@pytest.mark.asyncio
async def test_run_web_search_tool_records_online_results(monkeypatch):
    async def fake_search_online(**kwargs):
        yield {"Agent eval": {"organic": [{"title": "WebArena", "link": "https://example.com"}]}}

    monkeypatch.setattr("khoj.processor.conversation.agent_tool_loop.search_online", fake_search_online)

    result = AgentToolLoopResult()
    await run_web_search_tool({"query": "Agent eval"}, result=result, user=object(), conversation_history=[])

    assert "Agent eval" in result.online_results
    assert result.searched == ["web_search: Agent eval"]
```

- [ ] **Step 2：最小 wrapper**

```python
from khoj.processor.tools.online_search import search_online


async def run_web_search_tool(args: dict, *, result: AgentToolLoopResult, user, conversation_history, **kwargs) -> None:
    query = str(args.get("query") or "").strip()
    if not query:
        result.errors.append("web_search requires query")
        return
    async for response in search_online(query=query, conversation_history=conversation_history, user=user, **kwargs):
        if response:
            result.online_results.update(response)
    result.searched.append(f"web_search: {query}")
```

- [ ] **Step 3：跑测试**

```bash
uv run pytest tests/test_agent_tool_loop.py::test_run_web_search_tool_records_online_results -q
```

预期：通过。

## 10. 任务 6：复用 Notes 工具执行写回

**文件：**

- 修改：`src/khoj/processor/conversation/agent_tool_loop.py`
- 小改：`src/khoj/processor/conversation/notes_tool_loop.py`
- 修改：`tests/test_agent_tool_loop.py`

- [ ] **Step 1：抽出或复用 Notes 单工具执行**

最小做法：不要重写安全逻辑。优先从 `notes_tool_loop.py` 导出一个小函数：

```python
async def execute_notes_tool_call_for_agent_runtime(...):
    ...
```

这个函数内部继续调用现有：

```python
append_local_kb_note(...)
propose_local_kb_edit(...)
kb_read(...)
kb_grep(...)
kb_list(...)
kb_headings(...)
kb_resolve_link(...)
wiki_search_documents(...)
```

- [ ] **Step 2：测试写入结果转成 program context**

```python
def test_write_reference_becomes_program_context():
    from khoj.processor.conversation.agent_tool_loop import add_write_reference_context, AgentToolLoopResult

    result = AgentToolLoopResult()
    add_write_reference_context(
        result,
        {"action": "append_note", "status": "written", "file": "agent.md", "changed": True, "compiled": "Appended 5 lines"},
    )

    assert "append_note" in result.program_context[0]
    assert "written" in result.program_context[0]
```

- [ ] **Step 3：实现 context helper**

```python
import json


def add_write_reference_context(result: AgentToolLoopResult, reference: dict) -> None:
    if reference.get("action") not in {"append_note", "propose_edit"}:
        return
    payload = {
        "action": reference.get("action"),
        "status": reference.get("status"),
        "file": reference.get("file"),
        "changed": reference.get("changed"),
        "result": reference.get("compiled", ""),
    }
    instruction = "Final answer must report this exact write tool result."
    if reference.get("status") == "written":
        instruction += " Do not say writing is unavailable."
    result.program_context.append(json.dumps(payload, ensure_ascii=False) + "\n" + instruction)
```

- [ ] **Step 4：跑测试**

```bash
uv run pytest tests/test_agent_tool_loop.py::test_write_reference_becomes_program_context -q
```

预期：通过。

## 11. 任务 7：实现统一 loop 主函数

**文件：**

- 修改：`src/khoj/processor/conversation/agent_tool_loop.py`
- 修改：`tests/test_agent_tool_loop.py`

- [ ] **Step 1：测试 planner 可以先 web 后写入**

```python
import json
import pytest

from khoj.processor.conversation.agent_tool_loop import collect_agent_context_and_actions


@pytest.mark.asyncio
async def test_default_agent_loop_can_search_web_then_write(monkeypatch):
    responses = iter(
        [
            json.dumps({"calls": [{"name": "web_search", "args": {"query": "agent evaluation"}, "id": "1"}]}),
            json.dumps({"calls": [{"name": "append_note", "args": {"path": "agent.md", "content": "Agent eval"}, "id": "2"}]}),
            json.dumps({"calls": []}),
        ]
    )

    async def fake_send_message(**kwargs):
        return next(responses)

    async def fake_web(args, *, result, **kwargs):
        result.online_results["agent evaluation"] = {"organic": [{"title": "source"}]}
        result.searched.append("web_search: agent evaluation")

    async def fake_notes_call(call, *, result, **kwargs):
        result.references.append({"action": "append_note", "status": "written", "file": "agent.md", "changed": True})

    monkeypatch.setattr("khoj.processor.conversation.agent_tool_loop.run_web_search_tool", fake_web)
    monkeypatch.setattr("khoj.processor.conversation.agent_tool_loop.run_notes_tool_call", fake_notes_call)

    result = await collect_agent_context_and_actions(
        "根据网络资料补充 agent 评估八股并写入项目",
        [],
        user=object(),
        agent=None,
        send_message=fake_send_message,
        allow_local_kb=True,
        allow_openkb=False,
        allow_web=True,
    )

    assert "agent evaluation" in result.online_results
    assert result.references[-1]["status"] == "written"
```

- [ ] **Step 2：实现主循环**

```python
async def collect_agent_context_and_actions(
    query: str,
    chat_history: list,
    *,
    user,
    agent,
    send_message,
    allow_local_kb: bool,
    allow_openkb: bool,
    allow_web: bool,
    max_iterations: int = 4,
    **kwargs,
) -> AgentToolLoopResult:
    result = AgentToolLoopResult()
    registry = build_agent_tool_registry(allow_local_kb=allow_local_kb, allow_openkb=allow_openkb, allow_web=allow_web)
    transcript: list[dict[str, object]] = []

    for _ in range(max_iterations):
        raw = await send_message(
            query=_build_planner_query(query, registry, transcript),
            response_type="json_object",
            agent=agent,
        )
        calls = parse_agent_tool_calls(getattr(raw, "text", raw))
        if not calls:
            break
        for call in calls:
            if call.name == "web_search":
                await run_web_search_tool(call.args, result=result, user=user, conversation_history=chat_history, **kwargs)
            else:
                await run_notes_tool_call(call, result=result, user=user, conversation_history=chat_history, **kwargs)
            transcript.append({"tool": call.name, "args": call.args})

    return result
```

- [ ] **Step 3：跑测试**

```bash
uv run pytest tests/test_agent_tool_loop.py::test_default_agent_loop_can_search_web_then_write -q
```

预期：通过。

## 12. 任务 8：把 default chat 接到统一 runtime

**文件：**

- 修改：`src/khoj/routers/api_chat.py`
- 修改：`tests/test_api_chat_file_kb.py`

- [ ] **Step 1：写 route 级回归测试**

测试目标：默认聊天不是先被 source router 卡住，而是会调用统一 runtime。

```python
def test_default_chat_uses_unified_agent_runtime(monkeypatch):
    called = {}

    async def fake_collect_agent_context_and_actions(*args, **kwargs):
        called["used"] = True
        from khoj.processor.conversation.agent_tool_loop import AgentToolLoopResult

        return AgentToolLoopResult(
            references=[{"query": "append_note", "status": "written", "file": "agent.md"}],
            program_context=["Notes write tool result: written"],
        )

    monkeypatch.setattr("khoj.routers.api_chat.collect_agent_context_and_actions", fake_collect_agent_context_and_actions)
```

按现有 `test_api_chat_file_kb.py` 的 client fixture 补完整 HTTP 调用，断言 `called["used"] is True`。

- [ ] **Step 2：最小接线**

在 `api_chat.py` 中：

```python
if conversation_commands == [ConversationCommand.Default]:
    agent_result = await collect_agent_context_and_actions(
        q,
        chat_history,
        user=user,
        agent=agent,
        send_message=send_message_to_model_wrapper,
        allow_local_kb=notes_local_source_available,
        allow_openkb=notes_openkb_source_available,
        allow_web=is_web_search_enabled(),
        conversation_id=conversation_id,
        client_app=user_scope.client_app,
        query_images=uploaded_images,
        query_files=attached_file_context,
        relevant_memories=relevant_memories,
        tracer=tracer,
    )
    compiled_references.extend(agent_result.references)
    inferred_queries.extend(agent_result.inferred_queries)
    online_results.update(agent_result.online_results)
    program_execution_context.extend(agent_result.program_context)
    conversation_commands = [ConversationCommand.General]
```

保留显式命令的原逻辑。

- [ ] **Step 3：删除 default path 对 `aget_data_sources_and_output_format()` 的依赖**

只改 default path。保留 helper 给其它调用方和现有测试使用。

- [ ] **Step 4：跑 focused 测试**

```bash
uv run pytest tests/test_api_chat_file_kb.py tests/test_agent_tool_loop.py -q
```

预期：相关测试通过。

## 13. 任务 9：让上一轮 assistant artifact 能被写入工具引用

**文件：**

- 修改：`src/khoj/processor/conversation/utils.py`
- 修改：`tests/test_notes_tool_loop.py`

- [ ] **Step 1：补测试**

目标：上一轮在线整理出的 assistant artifact，在下一轮 “补充到项目里” 时能通过 `artifact_id` 被 `append_note` 使用。

```python
async def test_notes_tool_loop_appends_previous_online_artifact(tmp_path, monkeypatch):
    ...
```

复用已有 `test_notes_tool_loop_appends_artifact_content_without_recopied_chat` 的结构，只把 artifact context 扩展成包含 online source metadata。

- [ ] **Step 2：小改 artifact payload**

在 `_assistant_response_artifact(...)` 里保留：

```python
{
    "id": f"assistant:{turn_id}",
    "content": chat_response,
    "references": compiled_references,
}
```

如果已有同等字段，不新增新格式，只确保 online references 不被过滤掉。

- [ ] **Step 3：跑测试**

```bash
uv run pytest tests/test_notes_tool_loop.py::test_notes_tool_loop_appends_artifact_content_without_recopied_chat -q
```

预期：通过。

## 14. 任务 10：加最小可观测性

**文件：**

- 修改：`src/khoj/processor/conversation/agent_tool_loop.py`

- [ ] **Step 1：只用 logger，不建表**

第一版事件写日志即可：

```python
logger.info(
    "agent_runtime_tool_call",
    extra={
        "tool": call.name,
        "conversation_id": str(conversation_id) if conversation_id else None,
        "status": "ok",
    },
)
```

- [ ] **Step 2：错误也记录**

```python
logger.warning(
    "agent_runtime_tool_error",
    extra={
        "tool": call.name,
        "error": error_message,
        "conversation_id": str(conversation_id) if conversation_id else None,
    },
)
```

不新增数据库表。需要查询长期统计时再建表。

## 15. 任务 11：真实链路回归

**目标问题：**

```text
根据网络资料补充一些 agent评估的八股内容
补充到项目里
```

- [ ] **Step 1：单测覆盖默认聊天**

```bash
uv run pytest tests/test_agent_tool_loop.py tests/test_api_chat_file_kb.py tests/test_notes_tool_loop.py -q
```

预期：通过。

- [ ] **Step 2：真实 HTTP smoke**

用临时 vault 和临时 DB 启动本地服务，按项目习惯跑真实 `/api/chat`。服务器在远端时记得转发 localhost 端口给 Windows 访问。

验收标准：

- 第一轮回答含网络来源或 online context。
- 第二轮写入时不声称“无法写入”。
- 写入结果必须来自 `append_note` / `propose_edit` reference。
- 文件落在 local KB root 内。
- 最终回答报告真实写入状态。

## 16. 任务 12：更新文档进度

**文件：**

- 修改：`docs/INTERVIEW_PERSONAL_KB_AGENT_RESEARCH_SPEC.md`
- 修改：`docs/superpowers/specs/2026-06-30-agent-harness-workers-observability-design.md`

- [ ] **Step 1：Phase 6 状态更新**

把 Phase 6 写成：

```markdown
| Phase 6：Claude-style unified agent runtime / observability | 已计划，待实现 | `docs/superpowers/plans/2026-07-01-unified-claude-style-agent-runtime-zh.md`；默认聊天改为单主 agent tool loop，统一调度 web/local-KB/OpenKB/writeback/verifier；显式 slash commands 保持现有专用路径 | 下一步：先修 Codex `json_object` payload，再新增 `agent_tool_loop.py` 并接入 default chat |
```

- [ ] **Step 2：自检**

```bash
pattern="$(printf '%s|%s|%s|%s' 'TO''DO' 'TB''D' 'fill'' in' 'implement'' later')"
rg -n "$pattern" docs/superpowers/plans/2026-07-01-unified-claude-style-agent-runtime-zh.md docs/INTERVIEW_PERSONAL_KB_AGENT_RESEARCH_SPEC.md docs/superpowers/specs/2026-06-30-agent-harness-workers-observability-design.md
```

预期：无输出。

## 17. 最终验证命令

```bash
uv run pytest tests/test_codex_conversation_adapter.py tests/test_agent_tool_loop.py tests/test_notes_tool_loop.py tests/test_api_chat_file_kb.py -q
uv run ruff check src/khoj/processor/conversation/codex/utils.py src/khoj/processor/conversation/agent_tool_loop.py src/khoj/processor/conversation/notes_tool_loop.py src/khoj/routers/api_chat.py tests/test_codex_conversation_adapter.py tests/test_agent_tool_loop.py tests/test_notes_tool_loop.py tests/test_api_chat_file_kb.py
git diff --check
```

## 18. 回滚方案

- 如果 unified runtime 线上表现不稳，只回滚 `api_chat.py` 的 default 接线，让 `ConversationCommand.Default` 回到旧 source router。
- 保留 Codex `json_object` 修复，因为它是独立 runtime bug。
- 保留 `agent_tool_loop.py` 和测试文件不影响显式 slash command。

## 19. 第一版明确不做

- 不新增 LangChain / CrewAI / AutoGen。
- 不新增数据库事件表。
- 不新增用户确认 UI。
- 不做 worker 递归。
- 不把关键词、词表、token overlap 当语义判断。
- 不改 `/notes`、`/online` 等显式命令的用户可见行为。
