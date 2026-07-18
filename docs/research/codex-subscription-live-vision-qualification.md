# Codex Subscription live vision qualification

Status: research note (not a product specification)
Checked: 2026-07-19
Scope: explicit local acceptance for issue #77; official OpenAI/Codex sources,
installed Codex CLI 0.143.0, and the current OfferAgent Python implementation

## Executive conclusion

The qualification should run the real OfferAgent Python composition, not
`codex exec` or an app-server turn. The supported project seam is the existing
read-only `CodexFileCredentialSource` plus `CodexSubscriptionModelModule`,
followed by `ProductionRunComponentsFactory` and the real Codex Subscription
Responses gateway. This is the only route that simultaneously exercises the
OfferAgent attachment gate, immutable Run binding, image ordering/detail
selection, Responses encoder, and SSE parser.

There is an important support boundary: OpenAI documents ChatGPT subscription
sign-in for Codex and documents the file/keyring credential stores, but it does
**not** publish the ChatGPT subscription bearer token or
`chatgpt.com/backend-api/codex` as a general third-party API. The officially
supported integration surface is Codex itself (CLI, SDK, or app-server). The
OfferAgent direct backend adapter is therefore a deliberately qualified
compatibility integration, not a public-API guarantee. Issue #77 should make
that private compatibility contract observable and fail loudly when it drifts.
[Codex authentication](https://developers.openai.com/codex/auth),
[Codex app-server](https://developers.openai.com/codex/app-server)

For the current OfferAgent path, reusing the existing login without modifying
it means:

1. require a ChatGPT login stored in file mode under `CODEX_HOME/auth.json`;
2. read only the current access token and account ID through
   `CodexFileCredentialSource`--never use the refresh token;
3. fetch one fresh live `/models` snapshot with
   `CodexSubscriptionModelModule` and freeze it for the complete test run;
4. submit the generated images as ordered Conversation Attachments through one
   USER turn for each visible image-capable model;
5. use `original` exactly when the live catalog says
   `supports_image_detail_original`, otherwise `high`;
6. run every visible text-only model through the same production preparation
   path and prove the local modality error occurs before attachment
   materialization or a `/responses` send;
7. keep all attachments, runtime databases, and audit output under pytest's
   temporary directory, and assert the auth file is unchanged at teardown.

The test must be opt-in. Ordinary credentialless CI skips the entire named live
gate and must report it as skipped, never passed. Once a developer explicitly
enables the gate, missing/expired/non-ChatGPT credentials, a keyring-only login,
catalog/network failure, catalog drift during binding, or any semantic model
failure is a test failure rather than a skip.

## What the installed Codex client establishes

The inspected executable is the official npm package `@openai/codex` 0.143.0.
The following read-only commands were run:

```text
codex --version
# codex-cli 0.143.0

codex login status
# Logged in using ChatGPT

codex app-server generate-json-schema --experimental --out <temporary-dir>
```

`codex login status` is the documented automation preflight: it prints the
active authentication mode and exits 0 when credentials are present. It does
not prove that OfferAgent can read those credentials, because Codex may instead
be using the OS keyring. The OfferAgent credential lease remains the decisive
precondition. [Codex CLI login-status reference](https://developers.openai.com/codex/cli/reference#codex-login)

The generated 0.143.0 app-server protocol confirms these first-party shapes:

- `account/read` with `refreshToken: false` inspects account state without
  forcing a refresh; `true` explicitly forces one;
- `model/list` is cursor-paginated, hides non-picker models by default, and
  exposes `inputModalities`;
- `turn/start.input` is an ordered array of `UserInput` items, including
  `image` and `localImage`; both accept `auto`, `low`, `high`, or `original`
  detail.

These are useful protocol oracles, but an app-server turn is not an acceptable
substitute for issue #77: it bypasses the OfferAgent Python attachment/runtime
path, and Codex owns automatic token refresh in the managed ChatGPT mode.
[App-server model listing](https://developers.openai.com/codex/app-server#list-models-model-list),
[App-server turns](https://developers.openai.com/codex/app-server#turns),
[app-server 0.143.0 authentication source](https://github.com/openai/codex/blob/rust-v0.143.0/codex-rs/app-server/README.md#L1918-L1965)

`codex exec --ephemeral -i ...` is also insufficient. `--ephemeral` only means
that session rollout files are not persisted; it does not make the invocation
an OfferAgent run or promise that managed authentication will not refresh.
[Codex CLI `--ephemeral`](https://developers.openai.com/codex/cli/reference#codex-exec)

## Authentication reuse and non-mutation boundary

OpenAI documents four relevant facts:

- ChatGPT sign-in is the subscription-access mode for local Codex clients.
- `cli_auth_credentials_store = "file"` stores credentials at
  `CODEX_HOME/auth.json` (default `CODEX_HOME` is `~/.codex`).
- `keyring` uses the OS credential store and `auto` prefers it when available.
- `auth.json` contains access tokens and must be treated like a password.

Sources: [Codex authentication methods](https://developers.openai.com/codex/auth),
[credential storage](https://developers.openai.com/codex/auth#credential-storage),
[headless auth-cache warning](https://developers.openai.com/codex/auth#fallback-authenticate-locally-and-copy-your-auth-cache).
The installed version's source also makes file mode the default and defines
file, keyring, auto, and ephemeral storage distinctly.
[Codex 0.143.0 credential-store enum](https://github.com/openai/codex/blob/rust-v0.143.0/codex-rs/config/src/types.rs#L87-L100),
[storage backend dispatch](https://github.com/openai/codex/blob/rust-v0.143.0/codex-rs/login/src/auth/storage.rs#L498-L524)

OfferAgent's broker is intentionally narrower than Codex's complete auth
manager. It resolves `CODEX_HOME/auth.json`, reads a stable regular-file
snapshot, accepts only ChatGPT auth, extracts the current access token and
account ID, rejects an expired token, zeroes temporary token buffers, and never
uses the refresh token or writes the file.
[`codex_credentials.py`](../../packages/offeragent-harness/src/offeragent_harness/runtime/codex_credentials.py)

That gives the acceptance test a precise rule:

- It may call `codex login status` as a human-friendly read-only diagnostic.
- It must not call `codex login`, `codex logout`, app-server login/logout RPCs,
  or `account/read` with `refreshToken: true`.
- It must not silently change `cli_auth_credentials_store` or copy credentials.
  A keyring-only login is an actionable prerequisite failure in an explicitly
  enabled run. The developer may choose file mode and sign in before running
  the test, outside the test process.
- Capture the auth file identity, size, timestamps, and an in-memory content
  digest before the first lease; compare them after the suite without logging
  token bytes or the digest. A difference fails the gate. Concurrent Codex
  sessions should be closed during this acceptance run because they may refresh
  the same file independently.

This strict approach deliberately trades automatic refresh for non-mutation.
If the current access token expires during the suite, the live gate fails with
an auth prerequisite/error and tells the developer to refresh the login before
retrying; the test never refreshes it itself.

## Live catalog authority

The direct OfferAgent catalog path is the required authority for issue #77.
The current module sends an authenticated GET to the fixed Codex Subscription
`/models?client_version=...` endpoint, decodes the complete `models` array,
retains only `visibility == "list"`, and produces one account-bound fresh
snapshot. Its model record includes `input_modalities` and
`supports_image_detail_original`.
[`codex_subscription.py`](../../packages/offeragent-harness/src/offeragent_harness/providers/codex_subscription.py)

This matches the official Codex source at the protocol level: the client uses
GET `models` with a `client_version` query, and the backend `ModelInfo` contains
model visibility, `supports_image_detail_original`, and `input_modalities`.
[Codex models client](https://github.com/openai/codex/blob/rust-v0.143.0/codex-rs/codex-api/src/endpoint/models.rs#L31-L72),
[Codex `ModelInfo`](https://github.com/openai/codex/blob/rust-v0.143.0/codex-rs/protocol/src/openai_models.rs#L352-L410)

The test should fetch once and require all of the following before any model
qualification begins:

- `freshness == "fresh"`;
- no catalog error;
- a non-empty model set;
- non-null catalog revision, fetch time, and account binding;
- the before/after account binding remains identical;
- every visible model has a unique ID and its strict capability fields decoded.

Freeze that exact snapshot for the whole matrix. Construct immutable
`CodexRunBinding` values from its models and inject a test-only frozen binding
source into `ProductionRunComponentsFactory`; do not refetch `/models` per
model. This preserves a single qualification population while leaving the
production preparation, attachment, model-gateway, encoder, network, and SSE
paths real. The final report records model IDs, `image` versus text-only
classification, selected detail, and pass/fail only--never token, account ID,
email, raw catalog JSON, prompts containing secrets, or image base64.

The app-server's normalized `model/list` is not sufficient for this matrix in
0.143.0: it exposes `inputModalities` but not
`supportsImageDetailOriginal`. It remains a useful optional cross-check, not
the detail authority.
[app-server model protocol](https://github.com/openai/codex/blob/rust-v0.143.0/codex-rs/app-server-protocol/src/protocol/v2/model.rs#L40-L134)

One observed compatibility fact should be printed in the qualification report:
the installed CLI is 0.143.0 while OfferAgent currently declares catalog
client compatibility version 0.144.5. This is not itself a failure: the live
OfferAgent module's strict decode is the test. It is evidence worth retaining
if the backend schema rejects or changes fields.

## Ordered images and the real OfferAgent path

OpenAI's image-input guide permits multiple images in one request by placing
multiple image items in the message content array. It accepts fully qualified
URLs, base64 data URLs, or file IDs; OfferAgent deliberately uses ephemeral
base64 data URLs. Images consume tokens. The guide defines `high` as standard
high-fidelity understanding and `original` for large/dense/spatially sensitive
images; current model families differ in the precise resizing behavior.
[Multiple image inputs](https://developers.openai.com/api/docs/guides/images-vision#giving-a-model-images-as-input),
[detail levels](https://developers.openai.com/api/docs/guides/images-vision#specify-image-input-detail-level)

Official Codex preserves turn input order. `UserInput` is an ordered vector;
the conversion consumes it with `into_iter`, preserves image order, supplies
the selected detail, and creates one Responses message with `role: "user"`.
[Codex `UserInput`](https://github.com/openai/codex/blob/rust-v0.143.0/codex-rs/protocol/src/user_input.rs#L11-L40),
[Codex user-message conversion](https://github.com/openai/codex/blob/rust-v0.143.0/codex-rs/protocol/src/models.rs#L1714-L1744)

The acceptance test should mirror the existing hermetic end-to-end shape in
`test_current_turn_vision.py`, replacing only the fake catalog, credential, and
Responses transport with the live account-bound implementations:

1. deterministically generate the issue #77 screenshots under `tmp_path`;
2. put their bytes in a temporary `ConversationAttachmentStore`, preserving
   page order and using private complete attachment descriptors;
3. create `StartTurnCommand.input_blocks` as prompt text followed by page 1,
   page 2, and so on;
4. pass the frozen live `CodexRunBinding` through
   `ProductionRunComponentsFactory.prepare_root`;
5. run the actual Agent loop with the real composed Codex Subscription gateway;
6. require a strict structured semantic result and assert known company, role,
   interview round, cross-page question linkage, and page-order facts;
7. instrument the real Responses transport with a delegating send counter so
   each image model proves one or more real `/responses` sends and each
   text-only model proves zero.

[`test_current_turn_vision.py`](../../packages/offeragent-harness/tests/integration/runtime/test_current_turn_vision.py)
already characterizes this vertical path. The production preparation gate
rejects text-only models before attachment materialization, chooses
`original`/`high` from the binding, materializes claimed images in order, and
places ephemeral bytes in `ModelContentBlock` values.
[`production_worker_composition.py`](../../packages/offeragent-harness/src/offeragent_harness/runtime/production_worker_composition.py)

The Responses encoder then retains message/content order, requires image blocks
to be in a USER message, accepts the four supported image MIME types, limits
OfferAgent detail to `high` or `original`, emits base64 `input_image` items, and
sends `store: false`.
[`openai_responses.py`](../../packages/offeragent-harness/src/offeragent_harness/providers/openai_responses.py)

## Message-role constraint: public Responses versus Codex

The precise conclusion is narrower than “Responses only allows USER images”:

- The public Responses image guide consistently demonstrates `input_image`
  inside a USER message.
  [Responses image examples](https://developers.openai.com/api/docs/guides/images-vision#passing-a-base64-encoded-image)
- The OpenAI Python SDK types generated from the public OpenAPI schema allow
  input content containing text/image/audio in an easy input message whose role
  may be `user`, `assistant`, `system`, or `developer`; the stricter canonical
  input `Message` form allows `user`, `system`, or `developer`. Therefore the
  public Responses type system is not evidence for a universal USER-only image
  restriction.
  [OpenAI Python `EasyInputMessageParam`](https://github.com/openai/openai-python/blob/main/src/openai/types/responses/easy_input_message_param.py),
  [canonical Responses input message](https://github.com/openai/openai-python/blob/main/src/openai/types/responses/response_input_item_param.py#L395-L414)
- The official Codex protocol does make images **user input**, and its
  conversion fixes the resulting Responses message role to `user`.
- OfferAgent intentionally enforces that Codex contract more strictly: any
  image on SYSTEM, ASSISTANT, or TOOL fails locally before a network request.

Accordingly, issue #77 must assert that all generated screenshots land in one
or more owning USER messages in original page order. It must not weaken the
OfferAgent check merely because the general public Responses schema accepts a
wider message form. The former SYSTEM-image synthetic probe was invalid under
the OfferAgent/Codex contract; the current catalog-only model management path
correctly contains no inference probe.

## Skip and failure matrix

OpenAI does not prescribe pytest skip policy. The following is the repository
recommendation derived from the official login-status contract and issue #77's
requirement that non-execution never appear as success:

| Environment/state | Required result | Rationale |
| --- | --- | --- |
| Live flag absent in ordinary CI/local run | One clearly named module-level skip | The test is explicit, billed, networked acceptance; do not autodetect credentials and accidentally run it. |
| CI explicitly declares credentials unavailable | One clearly named module-level skip | Credentialless execution is known before qualification; the report must say no model was tested. |
| Live flag present, `codex login status` fails | Fail prerequisite | Explicit enablement promised a real qualification. |
| Live flag present, login is API-key/keyring-only, file missing, token expired, or account binding invalid | Fail prerequisite | These states cannot exercise the current read-only ChatGPT file broker and must not become a false-green skip. |
| Auth/catalog/network fails after qualification starts | Fail whole gate | The population is unknown or incomplete. |
| Catalog returns stale/unavailable/empty | Fail whole gate | Only a fresh complete population can qualify “every current model.” |
| Any image model fails structured facts | Fail with model ID and missing fact keys | A single advertised image model is unqualified. |
| Text-only model reaches attachment bytes or `/responses` | Fail with model ID and send count | The local modality gate regressed. |
| Auth file changes, non-temporary attachment/runtime state remains, or a probe cache appears | Fail teardown | The acceptance run violated the non-mutation contract. |

Suggested markers/flags should make these outcomes machine-readable, for
example `@pytest.mark.live_codex_subscription` plus one explicit enable flag
and one explicit “credentials intentionally unavailable” declaration. Exact
names belong in the issue #77 specification; the semantic distinction above is
the important requirement.

## Recommended acceptance evidence

The terminal summary or a temporary JSON report should include:

- test timestamp, OfferAgent commit, Python package version, Codex CLI version,
  and OfferAgent catalog client version;
- catalog revision/freshness and model count, but no raw catalog or account
  identity;
- for each model: model ID, advertised modalities, chosen detail or
  `text-only`, Responses send count, and structured-assertion result;
- explicit totals for image models qualified and text-only models locally
  gated;
- auth-file unchanged, no persistent test attachment/runtime files, and no
  production probe/cache state;
- a final status of pass, fail, or named skip. A zero-model or skipped run can
  never be formatted as pass.

The committed repository should contain only the deterministic image generator,
the explicit acceptance test, marker/configuration documentation, and expected
semantic facts. Generated screenshots and run evidence remain temporary and
must not be committed or copied into a user's Vault.
