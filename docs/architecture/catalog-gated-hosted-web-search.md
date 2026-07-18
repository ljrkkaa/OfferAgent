---
status: accepted
issue: 75
---

# Catalog-gated Hosted Web Search specification

## Outcome

OfferAgent gives the Python Agent Loop one provider-hosted public research capability when, and only when, the
selected model in the current account-bound Codex catalog declares `supports_search_tool: true`. The immutable
`CodexRunBinding` is the capability authority for the whole root/child Run tree. A stale display catalog, a
disappeared model, an account change, a model-name guess, or a synthetic probe cannot enable search.

The capability remains distinct from both existing controlled research paths:

- plugin `web_read` reads a user/model-selected public URL into bounded, content-hashed evidence;
- the isolated Research Browser performs an explicit browser research workflow;
- Hosted Web Search is executed inside one Codex Responses request and yields provider-attested citations, not a
  captured page body.

## Decision pressure test

The old `obsidan` branch used a probe cache and retried without Hosted Web Search when the backend rejected the
tool. That design was rejected because it creates an extra network request, makes cached probe state a second
capability authority, and can replace a partially executed research request with an uncited silent downgrade.
The current catalog already carries the exact account, model, revision, and search declaration required by the
Run boundary.

Two integration shapes were considered:

1. A provider-specific flag plus raw event dictionaries would make the Planner, Agent Loop, and UI understand
   Responses wire details.
2. A provider-neutral hosted-tool request and typed search/citation events keep the Responses request, SSE action
   union, annotation format, and limits inside the Model Provider.

The second shape is selected. It is the deeper module: callers choose only a hosted tool and consume bounded
semantic events; they never parse `response.web_search_call.*`, output items, or annotations.

The OpenAI reference documents `web_search` requests, lifecycle events, output actions, sources, and URL
annotations. It does not explicitly guarantee that Web Search and JSON Schema Structured Outputs can be combined
in one request, nor that annotations are retained around a structured JSON payload. OfferAgent therefore keeps
its existing strict JSON AgentStep request, adds Hosted Web Search to that request, and treats this combination as
an integration and live-acceptance obligation rather than an assumed platform guarantee. A rejection is a
visible Provider failure; it never triggers a request without search. See
[`docs/research/openai-responses-hosted-web-search.md`](../research/openai-responses-hosted-web-search.md).

## Capability and request contract

- `CodexRunBinding.model.supports_hosted_search` is derived only from the required live catalog field
  `supports_search_tool`. `web_search_tool_type` remains bounded catalog metadata in the durable binding; it is
  not copied into the Responses `type` field and is not a separate authorization source.
- The production composition maps a supporting binding to the provider-neutral `WEB_SEARCH` hosted tool on every
  Planner request in that Run. A non-supporting binding produces an empty hosted-tool set before the Provider is
  called. Child Runs inherit the root binding exactly.
- The Responses adapter encodes the tool as `{ "type": "web_search" }`, uses automatic tool selection, keeps
  `store: false`, and requests `web_search_call.action.sources` in `include`.
- No health request, minimal prompt, capability probe, cached capability status, model-name allowlist, or retry
  without the tool is permitted.
- Local Function Tools remain Harness-owned AgentStep calls. A Provider function call is still a protocol error;
  enabling Hosted Web Search does not give the Provider a Vault or local executor.

## Streaming contract

The Responses adapter owns one bounded state machine per `web_search_call` item:

1. `response.output_item.added` with a `web_search_call` starts a unique call and fixes its output index.
2. `response.web_search_call.in_progress` and `response.web_search_call.searching` advance the call without
   inventing a result.
3. `response.web_search_call.completed` marks provider completion.
4. `response.output_item.done`, or the terminal response's equivalent output snapshot, validates the completed
   `WebSearchCall`, its action union, and any included sources.
5. `response.output_text.annotation.added` and final `output_text.annotations` produce bounded URL citations;
   duplicate representations of the same annotation are emitted once.
6. `response.completed`, `response.incomplete`, `response.failed`, or `error` is the only terminal provider event.
   A successful terminal requires every started search call to have completed and to be reconciled by its done item
   or the terminal output snapshot.

The provider-neutral stream exposes search phase and citation events alongside text, reasoning summary, usage,
error, cancellation, and completion. The structured Planner collector rejects search or citation events when the
request did not declare Hosted Web Search, phase regression/duplication, citation before a completed search,
events after terminal, missing usage, and incomplete successful streams.

The adapter applies explicit ceilings: at most 16 search calls, 64 included sources per call, 256 unique citation
annotations, 2,048 characters per public URL, 512 UTF-8 bytes per title, and 4,096 UTF-8 bytes per query/find
pattern. URLs must be credential-free HTTP(S). Invalid action variants, indices, identifiers, source objects,
annotations, event order, or limit excess fail as a non-secret Provider protocol error.

Local cancellation closes the HTTP response and stops the producer. The public Responses protocol does not
define a normal `response.cancelled` streaming event for this foreground request shape, so OfferAgent does not
invent one; the existing cancellation token remains the authority.

## Citation provenance

`WebSourceRef` continues to mean a page body captured by a bounded research tool and therefore still requires its
actual `contentHash`. Hosted search annotations do not contain page bytes, so hashing the URL or title would create
false evidence.

OfferAgent adds a discriminated `HostedWebSourceRef` containing the safe URL, title, Provider ID, model ID, and
model request ID. It is explicitly provider-attested and has no content hash. Citation offsets are validated in
the Provider envelope but are not copied onto the extracted `finalResponse`, because their coordinate space is
the serialized AgentStep JSON rather than the extracted Markdown string.

Only URL annotations become final response references. The broader `action.sources` list is validated but is not
shown as “evidence actually used.” Immediately before `assistant.completed`, the Agent Loop publishes the unique
Hosted Web references through the existing `references.updated` event and attaches the same references to the
assistant and terminal content blocks. Obsidian and the local Web view render both captured Web sources and
Hosted Web citations as safe clickable HTTP(S) links.

## Failure and compatibility behavior

- A model without catalog search support can still perform an ordinary Agent Run, but its Model Request contains
  no Hosted Web Search tool. There is no network probe and no hidden fallback.
- A catalog/backend mismatch after a supporting binding is a visible Provider error. The Run does not retry
  without search and does not relabel an uncited response as successful research.
- New Runs already require a fresh account-bound catalog and exact selected model. Recovery reuses the immutable
  durable binding, so it cannot silently adopt capabilities from another account or a newer model entry.
- Existing plugin `web_read`, Research Browser permissions/network isolation, and their content-hashed evidence
  remain unchanged. Hosted search introduces no new API key, crawler, browser profile, or Vault authority.
- Queries and complete source action payloads are not added to durable Run events. This keeps Provider protocol
  detail and potentially sensitive query expansions out of the public event schema; the final cited URLs remain
  auditable.

## Implementation tickets

These are ordered TDD slices owned by GitHub issue #75.

1. **Provider-neutral contract and catalog gate**
   - Add hosted-tool, hosted-search phase, and citation model types.
   - Prove exact supporting/non-supporting `CodexRunBinding` values produce, or omit, the hosted tool with zero
     probe requests.
2. **Responses request and SSE parser**
   - Encode `web_search` plus the source include and implement the strict search/action/annotation state machine.
   - Cover lifecycle, text, reasoning, usage, terminal, cancellation, malformed order, invalid URLs, duplicate
     events, unrequested calls, and all count/size ceilings.
3. **Planner and provenance**
   - Consume hosted events through the public Model interface, preserve unique citations in the final PlanningStep,
     and reject undeclared or incomplete search streams.
   - Add `HostedWebSourceRef`, publish `references.updated`, and render safe links without weakening `WebSourceRef`.
4. **Production research slice**
   - Run a fresh catalog-bound production Agent Run through a MockTransport search response and prove one cited
     final answer, usage, correct terminal state, and exactly one search-bearing inference with no probe/fallback.
   - Prove a non-supporting catalog produces an ordinary request with no hosted tool and no probe/fallback call.
5. **Closure**
   - Keep `web_read` and Research Browser suites green, regenerate protocol artifacts, run Python/TypeScript type
     and architecture gates, and complete Standards + Spec review before the local commit and issue closure.
