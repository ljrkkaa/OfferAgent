---
status: accepted
supersedes: 0018
---

# Contract model execution to Codex Subscription

## Context

ADR 0018 correctly moved image input to the owning USER message and rejected a
synthetic vision cache. Later slices made the live Codex catalog authoritative
for visible models, exact Run binding, image input detail and Hosted Web Search.
The repository nevertheless retained configurable Provider schemas, DeepSeek,
OpenAI API, OpenAI-compatible and Ollama implementations, a generic Responses
factory, model Secret RPC, `models/health` inference probes, and an old
HTTP/WebSocket control surface. Those dormant choices contradicted the one-loop
and direct-stdio architecture and could become a second capability authority.

## Decision

Production model execution has exactly one external adapter: Codex Subscription.
Its backend identity, Responses endpoint, read-only Codex credential broker,
schema projection and request restrictions are internal. Persistent settings
contain only the atomic catalog selection (`model` plus `account_binding`),
reasoning effort and an optional literal-loopback proxy. Run and model-list
protocols expose no Provider choice, endpoint, API key, free-text model,
`requireVision`, capability status or probe command.

`CodexRunBinding` is the sole execution capability authority. Its selected
model's `input_modalities` is the Model Input Modality gate for real USER image
attachments, `supports_image_detail_original` chooses image detail, and its
Hosted Search declaration controls the provider-neutral search tool. There is
no synthetic image, health prompt, probe cache, model-name guess or retry after
removing a capability. A stale/unavailable catalog prevents a new Run while
remaining usable as display-only history.

Provider identity remains internal telemetry and provider-attested citation
provenance, not configuration. The strict Responses encoder/SSE parser is an
internal Codex adapter mechanism and cannot be constructed for another endpoint
or credential mode.

Legacy configuration is projected once into the contracted schema. Retired
model fields and model-provider Secret records are deleted with value-free,
idempotent reporting; unrelated Secret envelopes are unchanged. The existing
offline TypeScript-state migration remains limited to completed Turns, valid
ordered attachments and mapped permissions.

ADR 0031's Hosted Search decision is preserved and narrowed: the exact Run
binding, never `models/health`, is its only source. ADR 0030's forced confirmation
for Interview Submission remains the deliberate exception to Trusted Workspace
auto-apply; contracting model and transport surfaces does not weaken that user
decision boundary.

ADR 0025 is enforced in code by deleting the residual HTTP/WebSocket listener,
`web/launch` and Worker-owned Vault transaction fixture. Direct generated stdio
is the only Runtime control transport, and the Obsidian plugin remains the only
Vault Tool Adapter.

## Consequences

- New model integrations require a new architectural decision; they cannot be
  enabled by config or endpoint text.
- Catalog refresh replaces inference health/capability probes and fails closed
  before a new Run.
- Text, real USER images and Hosted Search share one exact model binding.
- Codex login material never enters OfferAgent's model SecretStore.
- Historical Provider choices can be audited by identity but cannot be resumed
  as execution paths.
- Live screenshot support is proven only by the explicit, billed acceptance gate
  that freezes one fresh catalog and drives every declared image model through
  real USER attachments, production Run preparation, the canonical Agent Loop
  and the Codex Subscription Responses gateway. Text-only entries must fail the
  same production preparation path before attachment materialization or network
  send. Built Windows-product proof remains separate downstream acceptance work;
  neither claim is inferred from unit tests.

See
[`codex-subscription-model-contraction.md`](../architecture/codex-subscription-model-contraction.md).
