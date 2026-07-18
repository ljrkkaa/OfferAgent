---
status: accepted
---

# Project AgentSteps through a compact model envelope

## Context

The Harness originally sent the complete execution-time AgentStep schema as the
Codex structured-output schema. That document embeds every plugin tool input
schema as a separate variant. A real Responses Lite qualification completed
with one and four plugin tools, but the same request failed with a provider
`server_error` at eight and fourteen tools. The failing provider schema grew
from 2,731 bytes to 6,909 and 16,130 bytes, while equivalent requests with the
same model baseline, application rules, hosted-search declaration and a small
strict schema completed. This is evidence of a backend structured-output
schema compatibility ceiling or defect, not evidence for a stable public byte
limit.

The execution boundary still needs the full catalog. Removing tool constraints,
guessing a smaller per-turn tool subset, retrying under a different request
dialect or treating provider validation as authorization would make model
output rather than Python the capability authority. Encoding all tools as
provider-native calls would also couple the Agent Loop to an internal Codex
dialect and bypass its existing single AgentStep planning contract.

Official Responses documentation defines strict JSON-schema output as a way to
shape model output; it does not make that schema a local authorization system.
The Codex request-placement facts are recorded in
[the request-instructions research note](../research/codex-subscription-request-instructions.md),
and the structured-output evidence is recorded in
[the structured-output research note](../research/codex-subscription-structured-output-schema.md).

## Decision

`AgentStepCatalog` is one deep Python module with three related surfaces:

- `schema` remains the complete execution-time AgentStep contract with exact
  name/version pairs and the original tool input schemas.
- `model_schema` is a bounded provider-facing projection. Each call contains
  only `name`, `version`, `argumentsJson` and `reason`; `argumentsJson` is a
  bounded string rather than an embedded tool-specific schema.
- `model_instruction` is a canonical, Harness-owned tool directory containing
  each exact name, version, description and input schema. It is included in the
  trusted application context and therefore participates in context budgeting
  and projection identity.

After a model response, the same catalog validates the compact envelope,
strictly decodes each `argumentsJson` object with duplicate-key, non-finite,
non-object and non-canonical-number rejection, validates it against the selected
tool's exact input schema, reconstructs the complete internal AgentStep, and
then reapplies cross-call and Skill planning invariants. Only that normalized
internal value may become a `ToolCall`; Python still creates every call ID,
hash, deadline, lineage, sensitivity and idempotency identity.

The provider adapter remains unaware of AgentStep semantics. It only projects
the already bounded `model_schema` into the Codex strict subset. Schema repair
uses the same compact schema, catalog directory and local decoder, so invalid
encoded arguments get at most the existing single explicit repair attempt.

## Consequences

- Provider schema size is structurally bounded by the small envelope and tool
  names rather than by every tool input schema.
- The ordinary trusted prompt grows with the complete tool directory and is
  charged against the existing context budget; there is no hidden side channel.
- A provider can return values accepted by the compact schema but execution
  still fails closed unless the exact local catalog accepts the decoded call.
- Tests must separately prove bounded projection, strict decoding, unchanged
  full-schema authority, the production two-round plugin loop and the real
  built-product AgentStep.
- Future providers may choose a different wire projection without changing the
  Agent Loop's exact internal contract.
