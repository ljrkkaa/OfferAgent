---
status: accepted
supersedes: 0013
---

# Bind Hosted Web Search to the Codex catalog

## Context

ADR 0013 treated the ChatGPT Codex backend as an unversioned capability surface. It required a runtime probe,
cached the result, and retried once without search. The fused Python Harness now obtains an account-bound Codex
model catalog before every new Run and durably binds the selected model plus its declared capabilities. Keeping a
probe cache would create a second capability authority and a silent downgrade path.

Hosted search citations also differ from `web_read` evidence. Responses URL annotations identify a public page,
but they do not supply the page bytes needed for the existing content hash.

## Decision

The immutable `CodexRunBinding` is the only production authority for Hosted Web Search. When its selected model's
required `supports_search_tool` field is true, the Planner declares one provider-neutral Hosted Web Search tool;
the Responses adapter encodes `{ "type": "web_search" }`. Otherwise no hosted tool is present before the model
network call. There are no runtime probes, capability caches, model-name guesses, or retry-without-search paths.

The Provider owns the bounded Responses request and SSE state machine. It emits provider-neutral search phases
and URL citations to the Planner. Provider-attested citations use a distinct `HostedWebSourceRef` with URL, title,
Provider/model identity, and model request ID, but no fabricated content hash. Captured `web_read` evidence keeps
using `WebSourceRef` with an actual page-content hash.

`web_search_tool_type` remains preserved catalog metadata and is not used as the Responses wire tool type. The
documented request tool type is `web_search`.

## Consequences

- Catalog freshness, model disappearance, and account changes fail closed through the existing Run-binding path.
- A supporting catalog followed by backend rejection is a visible Provider failure, not an uncited fallback.
- The Python Harness remains the only Agent Loop; Hosted Search is not a local ToolCall and gains no Vault access.
- Plugin `web_read` and the isolated Research Browser retain their existing permissions, network boundaries, and
  evidence semantics.
- OpenAI's public reference does not explicitly guarantee Web Search combined with JSON Schema output. This exact
  composition is therefore covered by deterministic integration and the final live Codex acceptance gate; no
  product guarantee is inferred solely from documentation.

See [`catalog-gated-hosted-web-search.md`](../architecture/catalog-gated-hosted-web-search.md) and
[`openai-responses-hosted-web-search.md`](../research/openai-responses-hosted-web-search.md).
