---
status: accepted
---

# Replay stateless model-turn continuations

## Context

The Codex Subscription adapter sends Responses requests with `store: false`
and no `previous_response_id`. The Agent Loop originally reconstructed every
planning request from application context plus accumulated local ToolResults.
It parsed the previous assistant JSON into an internal AgentStep but discarded
the Provider's terminal `response.output`. Consequently the next request could
not see the assistant step that caused those results, including its encrypted
reasoning and assistant `phase`. Real built-product runs repeatedly selected
already completed tools until their input-token budgets expired.

OpenAI's stateless Responses contract requires callers to append all previous
`response.output` items, including encrypted reasoning, before the next input.
Responses ordinary message roles do not include `tool`. OfferAgent's compact
JSON AgentStep is not a native `function_call` or `custom_tool_call`, so its
Harness-created ToolCall identity cannot legally become a Provider call ID.
The source-backed facts and the limits of the published contract are recorded
in the
[stateless multi-round research note](../research/codex-subscription-multiround-context.md).

## Decision

The Python Harness owns a durable `ModelTurn` seam:

- The Responses adapter requests `reasoning.encrypted_content`, validates each
  successful terminal output item, and emits a bounded opaque
  `ModelContinuation` bound to Provider, model, local request ID and canonical
  content hash.
- `ModelPlanner` preserves that continuation when it validates the compact
  AgentStep. Python creates the AgentStep identity and every ToolCall identity;
  the model still cannot create execution authority.
- The Agent Loop atomically accepts the continuation, AgentStep identity and
  exact ToolCalls before execution. SQLite schema v6 persists that relationship
  and database migration v8 refuses to resume an active legacy Run whose
  continuation authority is unavailable. Terminal legacy Runs receive an empty
  history only because they will never sample again.
- `ContextManager` replays each completed turn as one indivisible ordered group:
  first the exact assistant continuation, then each matching named and versioned
  local result. The group obeys the strictest result sensitivity and context
  budget; it is omitted as a whole rather than split.
- The Responses adapter strips only non-stored top-level item IDs on resend.
  It preserves item order, encrypted reasoning, assistant `phase` and all other
  validated fields. Internal `ModelRole.TOOL` remains a provider-neutral local
  observation role; this adapter projects it to a delimited USER `input_text`
  data envelope because no authentic Provider call ID exists.

Continuation blocks are accepted only as the sole content of an assistant
message and only by the same Provider and model. Standard and Lite request
dialects share this replay rule; their existing instruction prefixes remain
independent.

## Consequences

- Multi-round planning and recovery retain the causal model/tool transcript
  required by the stateless backend instead of relying on model guesswork.
- The continuation is intentionally opaque outside its owning Provider adapter;
  Agent orchestration can persist and order it but cannot reinterpret its wire
  items.
- Native Responses tool calls remain a separate future vertical slice. The
  Harness must never synthesize `function_call_output` or
  `custom_tool_call_output` for a compact JSON AgentStep.
- Continuation bytes consume the existing context budget. Raising cumulative
  input capacity before restoring causal replay, publishing the enforced tool
  contract, adding per-tool behavioral fences, or replaying only assistant text
  would hide a protocol defect and are rejected alternatives. A diagnostic
  sealed run used 373,051 cumulative input tokens before exposing that the
  model-facing `vault.changes.apply` directory omitted the plugin's mandatory
  Interview Submission Markdown contract. The contract is now published at
  that call seam, and the root cumulative ceiling remains 400,000 while
  model-round, tool-call, per-request context and deadline limits remain
  unchanged.
- Tests must prove exact wire replay, named USER result projection, atomic
  context grouping, durable round-trip, migration interruption, recovery and a
  real sealed-product multi-round run.
