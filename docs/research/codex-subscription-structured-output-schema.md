# Codex subscription structured-output schema constraints

Status: research note (not a product specification)
Checked: 2026-07-19
Scope: strict JSON Schema output in the Responses API and the official Codex
standard/Responses Lite request dialects; official OpenAI documentation,
official Codex source, and first-party maintainer statements only

## Executive conclusion

OpenAI's public Responses documentation does **not** publish a small serialized
byte ceiling for `text.format.schema`. The currently documented structural
limits are much larger: up to 5,000 object properties, 10 nesting levels,
120,000 characters across property names, definition names, enum values, and
const values, and 1,000 enum values in total. A single string enum containing
more than 250 values has an additional 15,000-character aggregate limit.
[Structured Outputs limits](https://developers.openai.com/api/docs/guides/structured-outputs#objects-have-limitations-on-nesting-depth-and-size)

Those size limits are not the whole contract. A strict schema must have an
object at its root, every field must be required (nullable unions emulate
optional values), every object must set `additionalProperties: false`, and the
schema must stay inside OpenAI's supported JSON Schema subset. Unsupported
composition keywords include `allOf`, `not`, `dependentRequired`,
`dependentSchemas`, `if`, `then`, and `else`; with `strict: true`, an
unsupported schema is documented to produce an error.
[root and required-field rules](https://developers.openai.com/api/docs/guides/structured-outputs#root-objects-must-not-be-anyof-and-must-be-an-object),
[`additionalProperties` and unsupported keywords](https://developers.openai.com/api/docs/guides/structured-outputs#additionalproperties-false-must-always-be-set-in-objects)

The official Codex client sends output schemas through the same
`text.format = {type: "json_schema", name, strict, schema}` representation for
both standard Responses and Responses Lite. The dialect branch changes where
the model baseline and tools are placed, but the client constructs `text`
*after* that branch from the same prompt output schema and strictness flag.
Codex's prompt default sets schema strictness to `true`.
[Codex prompt default](https://github.com/openai/codex/blob/312caf176a8fd3a5897a3d1fd3ed0a283bd1b5ac/codex-rs/core/src/client_common.rs#L22-L48),
[standard/Lite branch and shared text construction](https://github.com/openai/codex/blob/312caf176a8fd3a5897a3d1fd3ed0a283bd1b5ac/codex-rs/core/src/client.rs#L825-L908),
[Codex `TextFormat` encoder](https://github.com/openai/codex/blob/312caf176a8fd3a5897a3d1fd3ed0a283bd1b5ac/codex-rs/codex-api/src/common.rs#L165-L227),
[schema-to-text conversion](https://github.com/openai/codex/blob/312caf176a8fd3a5897a3d1fd3ed0a283bd1b5ac/codex-rs/codex-api/src/common.rs#L325-L342)

Therefore, a real ChatGPT-account request that succeeds with a small strict
schema but returns `server_error` with a larger schema well below the public
limits is not evidence of a documented OpenAI size limit. It is evidence of an
undocumented endpoint/model interaction until a controlled differential proves
whether the trigger is serialized size, property count, a keyword, a union
shape, or some interaction among them. The private subscription backend has no
published schema-limit contract located by this research, and the public API
limits must not be assumed to apply unchanged to it.

For OfferAgent, a compact provider-facing AgentStep envelope is a defensible
compatibility control, provided the full tool contract remains authoritative
locally. Moving tool arguments into a bounded JSON string can substantially
shrink the strict output schema, but OpenAI then constrains only the string;
the application must parse it with a strict JSON decoder and validate the
decoded object against the exact selected tool schema before authorization or
execution. That last requirement is an architectural inference, not an
OpenAI-documented guarantee.

## 1. Public Responses strict-schema contract

The Responses API enables structured output with `text.format.type =
"json_schema"`. The format has a name (maximum 64 characters, restricted to
letters, digits, underscores, and dashes), a JSON Schema object, and an
optional `strict` boolean. The API reference says that `strict: true` makes the
model follow the exact supplied schema, subject to the supported subset.
[Responses `text.format` reference](https://developers.openai.com/api/reference/resources/responses/methods/create#responses-create-text-format)

The current public limits, as checked on 2026-07-19, are:

| Constraint | Published limit or rule |
| --- | --- |
| Root | Must be an object; root-level `anyOf` is not allowed |
| Fields | Every field/function parameter must be in `required` |
| Optional values | Represent with a union including `null` |
| Extra keys | Every object must use `additionalProperties: false` |
| Object properties | At most 5,000 in total |
| Nesting | At most 10 levels |
| Counted schema strings | At most 120,000 characters total for property names, definition names, enum values, and const values |
| Enum values | At most 1,000 across all enum properties |
| One large string enum | If it has more than 250 values, their combined length is at most 15,000 characters |
| Unsupported composition | `allOf`, `not`, `dependentRequired`, `dependentSchemas`, `if`, `then`, `else` |

[supported schema subset](https://developers.openai.com/api/docs/guides/structured-outputs#supported-schemas),
[required fields](https://developers.openai.com/api/docs/guides/structured-outputs#all-fields-must-be-required),
[size and enum limits](https://developers.openai.com/api/docs/guides/structured-outputs#objects-have-limitations-on-nesting-depth-and-size),
[unsupported keywords](https://developers.openai.com/api/docs/guides/structured-outputs#some-type-specific-keywords-are-not-yet-supported)

The 120,000-character rule is not a total JSON-byte limit. Its wording counts
specific names and values; it does not say that every byte in descriptions,
punctuation, or schema serialization counts toward that number. Neither the
Structured Outputs guide nor the Responses create reference reviewed here
states a separate maximum serialized schema byte length. This is a bounded
statement about the current official pages, not proof that no backend limit
exists.

OpenAI also distinguishes two uses of schemas:

- function calling is recommended when the model is connected to application
  tools or data;
- `text.format` structured output is recommended when the assistant's own
  response needs a structured shape.

[function calling versus `text.format`](https://developers.openai.com/api/docs/guides/structured-outputs#when-to-use-structured-outputs-via-function-calling-vs-via-textformat)

OfferAgent's AgentStep is a mixed application protocol: it can represent a
tool request or a final response. Using `text.format` for that envelope is not
forbidden by the public documentation, but OpenAI's recommendation means this
is an application protocol choice rather than the canonical public function-
calling shape.

## 2. Official Codex request behavior

The current official Codex source at commit
`312caf176a8fd3a5897a3d1fd3ed0a283bd1b5ac` establishes four relevant facts.

### 2.1 Output schemas are strict by default

`Prompt::default()` sets `output_schema_strict` to `true`. When an output
schema exists, `create_text_param_for_request` emits a `TextFormat` with:

```text
type   = json_schema
name   = codex_output_schema
strict = prompt.output_schema_strict
schema = prompt.output_schema
```

[prompt fields and default](https://github.com/openai/codex/blob/312caf176a8fd3a5897a3d1fd3ed0a283bd1b5ac/codex-rs/core/src/client_common.rs#L22-L48),
[text format construction](https://github.com/openai/codex/blob/312caf176a8fd3a5897a3d1fd3ed0a283bd1b5ac/codex-rs/codex-api/src/common.rs#L325-L342)

### 2.2 Standard and Lite share schema placement

The request builder first branches on `model_info.use_responses_lite`:

```text
standard:
  instructions = model baseline
  tools        = top-level tools

Responses Lite:
  input prefix = developer AdditionalTools + developer model baseline
  instructions = empty
  tools        = omitted at the top level
```

After that branch, Codex calls the shared text-format encoder and assigns the
result to the same top-level `text` request field. Thus the source does not
support a hypothesis that Lite requires the output schema to be moved into a
developer message or into `AdditionalTools`.
[request builder](https://github.com/openai/codex/blob/312caf176a8fd3a5897a3d1fd3ed0a283bd1b5ac/codex-rs/core/src/client.rs#L825-L908)

Responses Lite additionally carries an internal dialect marker; the official
client adds `x-openai-internal-codex-responses-lite: true` for Lite HTTP
requests. This marker selects the request dialect but does not change the
location of `text.format` in the request object.
[Lite header](https://github.com/openai/codex/blob/312caf176a8fd3a5897a3d1fd3ed0a283bd1b5ac/codex-rs/core/src/client.rs#L1888-L1896)

### 2.3 Codex does not publish its own smaller schema limit

In the reviewed encoder, the schema is a generic `serde_json::Value` cloned
into the request. No Codex-side byte ceiling or schema compaction is applied at
this boundary. This describes the reviewed client source only; it says nothing
about validation or limits implemented by the server.
[Codex `TextFormat`](https://github.com/openai/codex/blob/312caf176a8fd3a5897a3d1fd3ed0a283bd1b5ac/codex-rs/codex-api/src/common.rs#L165-L189),
[encoder](https://github.com/openai/codex/blob/312caf176a8fd3a5897a3d1fd3ed0a283bd1b5ac/codex-rs/codex-api/src/common.rs#L325-L342)

### 2.4 Structured output applies to each model response in the loop

A Codex maintainer confirmed that applying the final output schema only to the
last assistant message is not currently something the harness can implement:
it is a limitation of the Responses server endpoint. This is useful evidence
that the server's structured-output behavior has endpoint-level constraints
that are not captured by the public JSON Schema subset alone. It does **not**
document a schema-size limit.
[maintainer response on the endpoint limitation](https://github.com/openai/codex/issues/19816#issuecomment-4331988890)

## 3. What is documented, observed, and inferred

### Documented facts

1. Public strict Structured Outputs has the structural and size limits listed
   in section 1.
2. Unsupported strict schemas are documented to receive an error.
3. Official Codex defaults output-schema requests to strict mode.
4. Official Codex places the schema in top-level `text.format` for both
   standard and Responses Lite requests.
5. Standard/Lite differences concern instructions, tools, parallel tool calls,
   reasoning context, and the internal Lite marker—not schema placement.

### Qualification observation supplied to this research

The separate OfferAgent qualification reported successful responses for
schemas of approximately 818 and 2,731 serialized bytes, followed by
`server_error` for schemas of approximately 6,909 and 16,130 bytes. This note
did not repeat those authenticated requests and does not persist their prompts,
credentials, or schema contents.

### Inferences

1. If the failing schemas also satisfy the supported subset, the observed
   threshold is far below every relevant published public size limit. Calling
   it the public Responses schema limit would therefore be inaccurate.
2. The monotonic result makes serialized size or schema complexity a plausible
   trigger, but does not distinguish size from the first keyword/shape that
   appears only in the larger cases.
3. Because Lite and standard Codex use the same `text.format` field, changing
   schema placement is not an evidence-backed fix.
4. A compact provider envelope reduces exposure to an undocumented backend
   boundary. It should be treated as a compatibility design, not as proof of a
   4 KB, 6 KB, or any other stable server contract.
5. If exact tool arguments are transported as a JSON string, the model-facing
   schema no longer proves their internal shape. Exact local decoding and
   validation must remain the execution gate.

## 4. Recommended engineering consequences

The evidence supports the following boundaries for OfferAgent:

1. **Keep two schemas.** Maintain an exact internal AgentStep/tool schema for
   local authority and a deliberately compact provider-facing envelope for
   Codex structured output.
2. **Keep the provider envelope structurally stable.** Its property count and
   nesting should not grow linearly with the number or complexity of installed
   tools. Tool names may be an enum if bounded, while argument payloads can be
   represented by a bounded string.
3. **Carry tool contracts as trusted context.** Names, versions,
   descriptions, and argument schemas may be supplied in trusted application
   instructions so the model can construct the payload, but prompt text is not
   an authorization mechanism.
4. **Decode before authority.** Parse argument JSON with duplicate-key and
   non-finite-number rejection, require an object, select the exact
   name/version contract, then validate the object before authorization or
   execution. A decode or validation failure should enter the existing invalid-
   output/repair path.
5. **Do not weaken local validation to fit the provider.** The compact schema
   is a wire projection; it must not replace the exact catalog schema used for
   tool authorization, fingerprints, replay, or audit.
6. **Qualify both dimensions.** Automated tests should cover schema-subset
   validity and compact-envelope decoding locally. A real ChatGPT-account
   qualification should prove at least one full tool round and final response
   using the built product and selected Lite model.
7. **Record metrics, not secrets.** Diagnostics may safely record schema byte
   length, property count, nesting depth, enum count, dialect, response status,
   and error code. They should not record model baselines, credentials, or
   proprietary prompt contents.

## 5. Unknowns that remain

No reviewed official source answers these questions:

- whether the ChatGPT-account Responses endpoint has a serialized schema byte
  ceiling below the public API limits;
- whether that ceiling, if any, differs by model or changes with
  `use_responses_lite`;
- whether the observed failure is caused by total bytes, property count,
  nesting, enum complexity, or a particular schema feature;
- whether `server_error` is a stable error classification for this condition;
- whether public API-key Responses and ChatGPT-account Responses compile the
  same schema through the same backend path.

These are qualification questions, not contracts to infer from absence of
documentation. The durable design should remain correct even if the private
limit changes: keep the model-facing envelope small, keep exact validation
local, and prove the real subscription path end to end.
