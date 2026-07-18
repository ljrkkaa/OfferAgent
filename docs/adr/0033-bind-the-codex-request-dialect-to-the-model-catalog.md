---
status: accepted
---

# Bind the Codex request dialect to the model catalog

## Context

ADR 0032 made one fresh `CodexRunBinding` authoritative for model execution,
but it did not define the instruction material or wire dialect needed by the
ChatGPT-account Responses backend. The production adapter consequently dropped
the catalog model baseline, ignored `use_responses_lite`, and encoded trusted
OfferAgent application rules as `system` input. A real `gpt-5.6-terra` AgentStep
could fail before producing a usable response even though catalog, attachment,
schema and SSE tests passed independently.

Official Codex resolves the selected catalog entry's effective model
instructions before a request. An `instructions_template` populated with the
default personality takes precedence over raw `base_instructions`. Standard
Responses sends that resolved baseline as top-level `instructions`; Responses
Lite instead prepends developer `AdditionalTools` and the resolved baseline to
`input`, omits top-level tools and instructions, adds the Lite request header,
and requests all-turn reasoning context. Application rules remain a distinct
developer layer. See the source-backed
[research note](../research/codex-subscription-request-instructions.md).

## Decision

The Codex catalog adapter owns both normalization steps: it resolves effective
model instructions from the catalog template and default personality, and it
decodes `use_responses_lite` as a required typed capability. Raw templates and
personality variants do not escape the adapter. The resulting
`model_instructions` and request dialect are immutable parts of the exact
`CodexRunBinding`, its catalog revision and its schema-versioned durable recovery
snapshot. Missing or malformed material invalidates the catalog before a Run or
network request.

The Responses adapter supports exactly two internal projections:

- Standard uses top-level `instructions` and `tools`.
- Lite prepends developer `AdditionalTools` and developer model instructions to
  `input`, omits those top-level fields, sends
  `x-openai-internal-codex-responses-lite: true`, and sets reasoning context to
  `all_turns`.

In both projections, trusted Harness `SYSTEM` messages are encoded as Codex
application `developer` input, USER messages remain `user`, and only USER
messages may carry image blocks. There is no model-name inference, retry under a
different projection, synthetic probe, or prompt replacement. The model
instructions may be persisted only inside the private Run binding required for
deterministic recovery; they are excluded from representations, public RPC,
telemetry, qualification reports and logs.

## Consequences

- A model-catalog refresh can change the effective instructions or request
  dialect, so either change produces a new catalog revision and affects only new
  Runs.
- Recovery reuses the exact historical projection without requiring live auth
  or silently adopting a newer baseline.
- Provider encoding stays shallow: it consumes one resolved baseline and one
  typed dialect flag instead of understanding catalog templates.
- Contract tests must cover both projections, the complete production
  composition, local failure for incomplete catalog data and a real built
  Windows-product AgentStep.
- Matching instruction placement is necessary but not sufficient evidence for
  subscription compatibility; billed qualification still owns the end-to-end
  claim.
