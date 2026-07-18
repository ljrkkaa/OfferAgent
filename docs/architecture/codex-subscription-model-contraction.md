# Codex Subscription model contraction

## Purpose

This is the contract phase of the model-module migration. The earlier vertical
slices made the live, account-bound Codex catalog authoritative for model
selection, image input and Hosted Web Search. This phase removes every retired
choice and probe that can still create a second model or transport authority.
It introduces no new Agent behavior.

The resulting production graph has one model path:

```text
Obsidian catalog selection
        |
        v
Python Run preparation -- exact CodexRunBinding --> Codex Subscription adapter
        |                                              |
        +-- image modality / detail -------------------+
        +-- hosted-search declaration ----------------+
```

The Obsidian plugin remains the UI and Vault Tool Adapter. The Python Harness
remains the only Agent Loop. They communicate only over the generated direct
stdio protocol.

## Public model boundary

The persistent model settings are deliberately small:

- `model` and `account_binding` form one atomic, catalog-verifiable selection;
- `reasoning_effort` is an execution preference;
- `proxy_url` is an optional, literal-loopback transport route frozen at Worker
  activation.

There is no configurable Provider, wire API, model endpoint, credential handle,
organization/project header, service tier, temperature, or remote-HTTPS switch.
The selected model is not free text: a fresh catalog supplies it and a fresh
`CodexRunBinding` revalidates it before a new Run.

The generated Run protocol carries the exact model and execution settings, not
a Provider choice. `models/list` has no Provider filter or unavailable-model
switch. Every returned model is a visible Codex catalog record. Provider ID
remains an internal observability and provider-attested citation identity; it
is not a client decision.

`models/health` is deleted. It sent a model request and created a second notion
of capability health. Refreshing `models/list` already reports catalog
freshness, authentication/account failure and model disappearance without an
inference probe. Image and Hosted Search eligibility come only from the exact
immutable Run binding.

## Deep Codex Subscription adapter

The production composition exposes one constructor for Codex Subscription
inference. It accepts the account-bound credential broker, fixed network enable
decision, optional literal-loopback proxy, test transport, and observability
ports. Its backend URL, Provider ID, schema projection, request restrictions and
credential mode are internal constants.

The strict Responses request/SSE state machine remains valuable implementation
mechanism, but it can no longer be instantiated as OpenAI API,
OpenAI-compatible, DeepSeek, Ollama or local inference. The retired factories,
provider subclasses, endpoint selection, SecretHandle credential path and their
dedicated tests are deleted. Tests exercise the one Codex adapter through the
provider-neutral `ModelGateway` port or the shared scripted/fake gateway seam.

## Configuration and Secret migration

Current configuration receives a new schema version whose model patch contains
only the four live fields above and whose UI patch has no loopback-Web lease.
Legacy file and SQLite layers are projected through an explicit compatibility
reader:

1. validate the bounded legacy shape without constructing a current config;
2. report only retired field names and Provider identities, never their values;
3. discard Provider, wire, endpoint, credential/API-key references, headers,
   free-text model selection, service tier, sampling and loopback-Web fields;
4. require a new live-catalog model selection;
5. write the current schema atomically, with the existing backup/rollback rules.

SQLite migration writes one `config_migration_reports` entity per migrated
layer before deletion. The entity contains only the migration name, owner,
retired field names and canonical Provider identities; endpoint, credential and
other retired values are never copied into it.

The model Secret retirement scans only regular SecretStore envelopes under the
current Workspace scope. It removes records whose declared kind is
`model-provider`, records only their canonical Provider identities and counts,
and leaves every other Secret envelope byte-for-byte untouched. A repeated run
is a no-op. Corrupt or ambiguous envelopes are never guessed to be model
credentials and are reported without content.

The offline TypeScript-state migration remains closed: only completed Turns,
valid ordered attachments and mapped permission settings cross. Runs,
checkpoints, pending batches, tokens and Secret material do not.

## Retired control surfaces

ADR 0025 already chose direct stdio and plugin-owned Vault tools. The remaining
loopback HTTP/WebSocket listener, `web/launch` command, Web capability/config,
static control UI and Worker-owned Vault transaction fixture are incompatible
production leftovers and are deleted. This does not remove the isolated
Research Browser or provider Hosted Web Search; neither is a Runtime control
transport.

The architecture audit is strengthened to reject:

- retired model adapter modules or imports;
- runtime model/capability probe vocabulary and `models/health`;
- TypeScript Agent Loop, Planner or model-network ownership;
- Runtime HTTP/WebSocket control modules or commands;
- imports from the Python Vault implementation into Worker Runtime code.

## Ordered TDD slices

1. **Contract characterization** -- add failing architecture and protocol tests
   for the exact retired files, imports, commands and fields.
2. **Configuration and Secret retirement** -- add failing v5/file and v4/SQLite
   migrations plus idempotent, redacted model-Secret purge tests; then contract
   the current schemas.
3. **Single adapter** -- drive a fixed Codex Subscription constructor through
   text, image, Hosted Search, error and cancellation tests; delete all other
   Provider implementations, branches and dedicated tests.
4. **Protocol and UI contraction** -- remove Provider/run/descriptor fields,
   health probes, model Secret RPC, loopback Web and the Worker Vault fixture;
   regenerate protocol bindings and preserve catalog/model-selection behavior.
5. **Architecture closure** -- update the domain model and ADRs, run the full
   Python and Obsidian suites, formatting, type checks, import contracts,
   protocol/assets/build audits, then perform Standards and Spec review.

## Completion evidence

Completion requires no production reference to a retired Provider, runtime
capability probe, HTTP/WebSocket control plane or Worker Vault implementation;
reproducible generated protocol artifacts; all automated gates; and a final
review against issue #76. Live model/image qualification belongs to #77 and the
built-product acceptance belongs to #78, so neither can be substituted by this
contraction's mocks.
