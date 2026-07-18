# Codex Subscription request-instructions contract

Status: research note (not a product specification)
Checked: 2026-07-19
Scope: Codex Responses instruction placement for ChatGPT-account access;
official OpenAI/Codex sources, the installed Codex CLI, and a redacted structural
inspection of the local model cache

## Executive conclusion

Codex does not treat its ChatGPT-account Responses request as an ordinary
application-defined system prompt. It first resolves an authoritative model
baseline from the Codex model catalog, then projects that baseline according to
the catalog's request dialect:

- for a normal Responses model (`use_responses_lite = false`), the resolved
  baseline is the top-level `instructions` string;
- for a Responses Lite model (`use_responses_lite = true`), top-level
  `instructions` is empty and the resolved baseline is prepended to `input` as
  a `developer` message, alongside a developer `AdditionalTools` item.

The resolved baseline is not always the raw catalog `base_instructions` value.
When `model_messages.instructions_template` exists, official Codex substitutes
the selected/default personality variable into that template and uses the
result; raw `base_instructions` is the fallback. The same behavior exists in
the installed 0.143.0 source tag and current upstream source.
[Codex 0.143.0 catalog model and resolver](https://github.com/openai/codex/blob/rust-v0.143.0/codex-rs/protocol/src/openai_models.rs#L352-L488),
[Codex 0.143.0 session resolution](https://github.com/openai/codex/blob/rust-v0.143.0/codex-rs/core/src/session/mod.rs#L595-L612),
[Codex 0.143.0 request projection](https://github.com/openai/codex/blob/rust-v0.143.0/codex-rs/core/src/client.rs#L829-L914),
[current upstream resolver](https://github.com/openai/codex/blob/312caf176a8fd3a5897a3d1fd3ed0a283bd1b5ac/codex-rs/protocol/src/openai_models.rs#L368-L500),
[current upstream request projection](https://github.com/openai/codex/blob/312caf176a8fd3a5897a3d1fd3ed0a283bd1b5ac/codex-rs/core/src/client.rs#L825-L908)

Application-owned instructions are a separate layer. Official Codex encodes
configured developer instructions and its application instruction blocks as
`developer` messages in `input`; it encodes discovered `AGENTS.md` content as
contextual `user` messages. It does not replace the catalog baseline with
either layer.
[developer/user message builders](https://github.com/openai/codex/blob/rust-v0.143.0/codex-rs/core/src/context_manager/updates.rs#L185-L228),
[configured developer-instruction aggregation](https://github.com/openai/codex/blob/rust-v0.143.0/codex-rs/core/src/session/mod.rs#L3225-L3240),
[Apps instruction role](https://github.com/openai/codex/blob/rust-v0.143.0/codex-rs/core/src/context/apps_instructions.rs#L7-L25),
[`AGENTS.md` contextual user role](https://github.com/openai/codex/blob/rust-v0.143.0/codex-rs/core/src/context/user_instructions.rs#L3-L29)

For ChatGPT-account authentication, replacing the catalog baseline with a
custom prompt is explicitly unsupported. An OpenAI Codex maintainer states that
custom instructions are supported with API-key authentication, while
ChatGPT-account users should augment the prompt through `AGENTS.md`; another
maintainer response calls the resulting `Instructions are not valid` error
expected. Thus an entirely missing baseline is not an official ChatGPT-account
request shape either. The only legitimate empty top-level `instructions` case
found is Responses Lite, where the same resolved baseline is still present as
a developer input message.
[maintainer answer on ChatGPT-account augmentation](https://github.com/openai/codex/issues/4433#issuecomment-3354172191),
[maintainer answer on API-key custom prompts](https://github.com/openai/codex/issues/3376#issuecomment-3273010764),
[official authentication-mode distinction](https://developers.openai.com/codex/auth#openai-authentication),
[official `AGENTS.md` guidance](https://developers.openai.com/codex/guides/agents-md)

Consequently, the OfferAgent fixed point that discarded catalog instruction
fields and sent only OfferAgent-authored `system` input was not merely missing
an optional prompt enhancement. It omitted part of the ChatGPT-account wire
contract and also collapsed two separate authority layers. A correct adapter
must preserve the catalog-resolved baseline and its standard/lite placement,
then carry OfferAgent application rules separately as developer input. Public
Responses accepts both `system` and `developer` roles, but that general API
syntax does not override the stricter ChatGPT-account behavior established by
Codex's own client and maintainer guidance.
[public Responses instruction semantics](https://developers.openai.com/api/reference/resources/responses/methods/create#responses-create-instructions),
[OfferAgent fixed-point request encoder](https://github.com/ljrkkaa/OfferAgent/blob/2353d9a2506e587386dff69fc6c352b3d0d596ba/packages/offeragent-harness/src/offeragent_harness/providers/openai_responses.py#L1315-L1327),
[OfferAgent fixed-point catalog decoder](https://github.com/ljrkkaa/OfferAgent/blob/2353d9a2506e587386dff69fc6c352b3d0d596ba/packages/offeragent-harness/src/offeragent_harness/providers/codex_subscription.py#L517-L554),
[OfferAgent system-rule projection](https://github.com/ljrkkaa/OfferAgent/blob/2353d9a2506e587386dff69fc6c352b3d0d596ba/packages/offeragent-harness/src/offeragent_harness/agent/context_manager.py#L492-L510)

## 1. Where top-level `instructions` comes from

The backend `/models` response is authoritative model metadata. Its
`ModelInfo` includes at least these instruction/dialect fields:

- `base_instructions`;
- optional `model_messages`, including `instructions_template` and personality
  variables;
- `use_responses_lite`.

Official Codex resolves one session baseline in this order: explicit config
override, resumed conversation metadata, then the selected model's catalog
instructions. In the ordinary no-override path, `get_model_instructions`
selects the catalog template when present and substitutes the requested or
default personality; otherwise it returns `base_instructions`.
[catalog response type](https://github.com/openai/codex/blob/rust-v0.143.0/codex-rs/protocol/src/openai_models.rs#L352-L434),
[template/default-personality resolution](https://github.com/openai/codex/blob/rust-v0.143.0/codex-rs/protocol/src/openai_models.rs#L454-L537),
[session resolution priority](https://github.com/openai/codex/blob/rust-v0.143.0/codex-rs/core/src/session/mod.rs#L595-L645)

The request projection then branches:

```text
standard Responses:
  instructions = resolved catalog baseline
  input        = conversation/application input
  tools        = normal Responses tools

Responses Lite:
  instructions = ""
  input        = [developer AdditionalTools,
                  developer resolved catalog baseline,
                  conversation/application input]
  tools        = absent
```

This means the precise answer to “does top-level `instructions` come from
`base_instructions`?” is conditional:

1. **Standard dialect:** yes, it contains the catalog-resolved model baseline,
   with raw `base_instructions` as fallback rather than an unconditional
   byte-for-byte source.
2. **Lite dialect:** no; top-level `instructions` is deliberately empty, but
   the catalog-resolved baseline is still mandatory in a developer input item.

[official standard/lite branch](https://github.com/openai/codex/blob/rust-v0.143.0/codex-rs/core/src/client.rs#L839-L868),
[final request construction](https://github.com/openai/codex/blob/rust-v0.143.0/codex-rs/core/src/client.rs#L890-L914)

## 2. How application and developer instructions are encoded

Official Codex maintains three relevant layers:

| Layer | Responses representation | Examples |
| --- | --- | --- |
| Model baseline | Standard: top-level `instructions`; Lite: leading `developer` input | Catalog model behavior and tool-use baseline |
| Application/developer | `input` message with role `developer` | `developer_instructions`, permissions, collaboration mode, Apps blocks |
| Project/user context | `input` message with role `user` | Global/project/nested `AGENTS.md`, environment context |

The implementation builds ordinary `ResponseItem::Message` values and assigns
`developer` or `user` explicitly. Public Responses documents a string
`instructions` value as equivalent to a developer-role text input and gives
both `developer` and `system` precedence over `user`; nevertheless, Codex's
ChatGPT-account client consistently uses `developer` for its application-owned
blocks and `user` for project guidance.
[message construction](https://github.com/openai/codex/blob/rust-v0.143.0/codex-rs/core/src/context_manager/updates.rs#L185-L228),
[public Responses roles](https://developers.openai.com/api/reference/resources/responses/methods/create#responses-create-input),
[public top-level instructions definition](https://developers.openai.com/api/reference/resources/responses/methods/create#responses-create-instructions)

The distinction is architectural, not cosmetic: custom application behavior
augments the model baseline; it is not a substitute for that baseline. Project
instructions sit at user authority. The official `AGENTS.md` guide likewise
describes Codex as loading global and project guidance into a precedence-ordered
chain rather than replacing model instructions.
[official `AGENTS.md` discovery and precedence](https://developers.openai.com/codex/guides/agents-md#how-codex-discovers-guidance)

## 3. ChatGPT-account restrictions on custom or missing baselines

OpenAI documents ChatGPT sign-in as subscription access and API-key sign-in as
usage-based access. The distinction matters here because the ChatGPT Codex
backend validates model instructions more narrowly than the public API-key
Responses endpoint.
[Codex authentication](https://developers.openai.com/codex/auth#openai-authentication)

The strongest first-party evidence is the closed Codex issue about custom
instruction files. A maintainer says the behavior is “by design”: custom
instructions are supported with an API key, while a ChatGPT account should use
`AGENTS.md` to augment instructions. An earlier issue received the same
maintainer answer after a custom baseline produced `400 Instructions are not
valid`.
[issue #4433 maintainer answer](https://github.com/openai/codex/issues/4433#issuecomment-3354172191),
[issue #3376 maintainer answer](https://github.com/openai/codex/issues/3376#issuecomment-3273010764)

These sources directly establish that an arbitrary replacement baseline is
unsupported for ChatGPT-account access. They do not publish a complete backend
validator specification. The conclusion about an entirely missing baseline is
therefore an inference, but a strong one:

- official Codex always resolves a session baseline before constructing a
  request;
- standard requests send it at top level;
- Lite requests move it into developer input rather than discard it;
- maintainers direct ChatGPT-account customization to additive `AGENTS.md`
  guidance, not baseline replacement.

Accordingly, “omit the baseline and put equivalent/custom text in `system`
input” is not an evidenced supported variant. It may fail validation, fail in
backend processing, or silently change model behavior; the official sources do
not promise which failure mode occurs.

## 4. Local model-cache structural evidence

The official cache implementation stores `models_cache.json` under
`CODEX_HOME`; the serialized snapshot contains `fetched_at`, optional `etag`,
optional `client_version`, and a vector of the same `ModelInfo` catalog values.
[cache path](https://github.com/openai/codex/blob/rust-v0.143.0/codex-rs/models-manager/src/manager.rs#L25-L27),
[cache construction](https://github.com/openai/codex/blob/rust-v0.143.0/codex-rs/models-manager/src/manager.rs#L230-L240),
[serialized cache schema](https://github.com/openai/codex/blob/rust-v0.143.0/codex-rs/models-manager/src/cache.rs#L160-L169)

A read-only structural inspection of the local `~/.codex/models_cache.json`
observed the following on 2026-07-19:

- top-level keys are `client_version`, `etag`, `fetched_at`, and `models`;
- the cache contains eight model entries;
- all eight have non-empty `base_instructions` and an
  `instructions_template`;
- three templates are byte-identical to `base_instructions`; five differ;
- all templates expose only the documented default/friendly/pragmatic
  personality-variable keys;
- five models use standard Responses and three use Responses Lite;
- the currently investigated `gpt-5.6-terra` entry uses Responses Lite and its
  template is byte-identical to its non-empty `base_instructions`;
- the standalone `codex` command reports 0.143.0, while the cache's non-secret
  `client_version` field reports 0.145.0, so the cache may have been refreshed
  by a newer Codex client sharing the same `CODEX_HOME`.

No prompt text, model instruction excerpt, token, account identifier, auth
field, request body, or full cache document was printed or persisted during
this inspection. These observations are local evidence, not a stable model
catalog guarantee; all of the values may change on the next catalog refresh.

## 5. Meaning for OfferAgent

At fixed point `2353d9a`, OfferAgent decoded model capabilities but did not
retain `base_instructions`, `model_messages`, or `use_responses_lite`; its
request encoder omitted top-level `instructions`; and its context manager put
OfferAgent rules and the run snapshot into `system` messages. That composition
had three separate contract gaps:

1. **Missing model baseline.** Neither the standard nor Lite catalog baseline
   survived to the request.
2. **Missing dialect selection.** A Lite model was encoded as a standard
   Responses model, including ordinary top-level tools rather than the official
   Lite prefix.
3. **Collapsed authority layers.** OfferAgent application rules occupied
   `system` input instead of the `developer` application layer used by Codex,
   while no distinct catalog model layer remained.

[fixed-point catalog projection](https://github.com/ljrkkaa/OfferAgent/blob/2353d9a2506e587386dff69fc6c352b3d0d596ba/packages/offeragent-harness/src/offeragent_harness/providers/codex_subscription.py#L517-L554),
[fixed-point Responses request](https://github.com/ljrkkaa/OfferAgent/blob/2353d9a2506e587386dff69fc6c352b3d0d596ba/packages/offeragent-harness/src/offeragent_harness/providers/openai_responses.py#L1315-L1327),
[fixed-point system inputs](https://github.com/ljrkkaa/OfferAgent/blob/2353d9a2506e587386dff69fc6c352b3d0d596ba/packages/offeragent-harness/src/offeragent_harness/agent/context_manager.py#L492-L510)

The minimum contract-preserving direction is therefore:

1. freeze the instruction and dialect fields with the same immutable catalog
   binding as the other model capabilities;
2. resolve the effective baseline from template/default personality exactly as
   official Codex does, with raw `base_instructions` only as fallback;
3. project that baseline according to `use_responses_lite` rather than applying
   one universal request shape;
4. encode trusted OfferAgent application rules as separate developer input and
   keep user/workspace data at user authority;
5. fail locally when a selected model lacks the catalog material needed for
   its dialect instead of sending a guessed or incomplete request.

This research does not claim that matching instruction placement alone proves
the complete private backend dialect. A real OfferAgent product run must still
qualify tool representation, structured output, streaming, authentication,
and model behavior. It does establish that discarding the catalog baseline or
ignoring `use_responses_lite` cannot be considered faithful to the official
Codex ChatGPT-account client.
