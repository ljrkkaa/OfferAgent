# OpenAI Responses Hosted Web Search protocol

Status: research note (not a product specification)
Checked: 2026-07-18
Scope: public OpenAI Responses API behavior only

## Executive conclusion

For a new Responses API integration, the public request tool is
`{"type":"web_search"}`. `web_search_preview` remains a legacy option, but the
current guide recommends `web_search` and reserves newer controls such as
filters, live-access control, and returned-token-budget control for it. The
model may decline to search when `tool_choice` is `auto`; the guide says to use
`required` or a specific web-search choice when a search must occur.
[Web search guide](https://developers.openai.com/api/docs/guides/tools-web-search)

The documented stream has three dedicated search lifecycle events, but those
events carry identity and ordering data only. Search action details and the
optional complete source list are carried by a `web_search_call` output item,
not by `response.web_search_call.completed`. A parser therefore needs both the
lifecycle-event path and the output-item/terminal-response path.
[Streaming-event reference](https://developers.openai.com/api/reference/resources/responses/streaming-events#response.web_search_call.in_progress)

The public documentation does **not** establish that Hosted Web Search and
JSON-Schema Structured Outputs are supported together in one request. It also
does not establish that `url_citation` annotations or search `sources` survive
when the final output is constrained to structured JSON. The create schema
exposes `tools`, `include`, and `text.format` as top-level request fields, but
that syntactic shape is not a compatibility guarantee. Treat both behaviors as
undocumented until a real request against the selected backend/model proves
them. [Create response reference](https://developers.openai.com/api/reference/resources/responses/methods/create),
[Structured Outputs guide](https://developers.openai.com/api/docs/guides/structured-outputs),
[Web search output contract](https://developers.openai.com/api/docs/guides/tools-web-search#output-and-citations)

## Request contract

The minimal streaming request shape is:

```json
{
  "model": "<selected-model>",
  "input": "<user input>",
  "tools": [{ "type": "web_search" }],
  "include": ["web_search_call.action.sources"],
  "stream": true
}
```

`include` is not required to perform a search. It is required when the client
wants the complete URL list consulted by search rather than only the URLs that
the model chose to cite inline.
[Sources](https://developers.openai.com/api/docs/guides/tools-web-search#sources)

The current public tool type recommended for a new integration is
`web_search`; the public request schema does not define a field named
`web_search_tool_type`. Any similarly named Codex model catalog field is
backend/catalog metadata and must not be copied into the public tool `type`
without an independently documented mapping.
[Web search request examples](https://developers.openai.com/api/docs/guides/tools-web-search)

Documented optional controls include:

- `search_context_size`: `low`, `medium`, or `high`. It does not promise an
  exact number of tokens, sources, or citations.
  [Search context size](https://developers.openai.com/api/docs/guides/tools-web-search#search-context-size)
- `filters.allowed_domains` and `filters.blocked_domains`: at most 100 entries
  in each list; domains omit the URL scheme and include subdomains.
  [Domain filtering](https://developers.openai.com/api/docs/guides/tools-web-search#domain-filtering)
- `external_web_access`: defaults to `true`; `false` limits the current
  `web_search` tool to cached/indexed results. The legacy preview tool ignores
  this control.
  [Live internet access](https://developers.openai.com/api/docs/guides/tools-web-search#live-internet-access)
- `return_token_budget`: only `default` and `unlimited` are accepted, and only
  for hosted Responses `web_search` with GPT-5+ reasoning web search. It does
  not apply to `web_search_preview`.
  [Longer web research](https://developers.openai.com/api/docs/guides/tools-web-search#run-longer-web-research)
- `user_location`: an approximate location with free-text city/region, a
  two-letter country code, and/or an IANA timezone. The guide says deep
  research models do not support it.
  [User location](https://developers.openai.com/api/docs/guides/tools-web-search#user-location)
- Responses web search has a 128k search-context limit even when the selected
  model's general context window is larger.
  [Limitations](https://developers.openai.com/api/docs/guides/tools-web-search#limitations)

The Responses request also has `max_tool_calls`, which bounds the total number
of built-in tool calls across the response rather than per tool. The public
reference does not publish a numeric maximum for this field.
[Create response reference](https://developers.openai.com/api/reference/resources/responses/methods/create)

## Streaming protocol

All documented search lifecycle events have the same four required fields:

| Event `type` | Meaning | Required payload |
| --- | --- | --- |
| `response.web_search_call.in_progress` | call initiated | `item_id`, `output_index`, `sequence_number`, `type` |
| `response.web_search_call.searching` | call executing | `item_id`, `output_index`, `sequence_number`, `type` |
| `response.web_search_call.completed` | call completed | `item_id`, `output_index`, `sequence_number`, `type` |

Sources: [in progress](https://developers.openai.com/api/reference/resources/responses/streaming-events#response.web_search_call.in_progress),
[searching](https://developers.openai.com/api/reference/resources/responses/streaming-events#response.web_search_call.searching),
[completed](https://developers.openai.com/api/reference/resources/responses/streaming-events#response.web_search_call.completed).

Important: `response.web_search_call.completed` does **not** carry `action`,
queries, sources, or citation annotations. `response.output_item.added` and
`response.output_item.done` each carry an `item` plus `output_index` and
`sequence_number`; the done item (or the terminal response's `output`) is where
the completed `web_search_call` snapshot can be read.
[Output item added](https://developers.openai.com/api/reference/resources/responses/streaming-events#response.output_item.added),
[output item done](https://developers.openai.com/api/reference/resources/responses/streaming-events#response.output_item.done)

The other event families relevant to a complete parser are:

- `response.output_text.delta` and `response.output_text.done`, keyed by
  `item_id`, `output_index`, and `content_index`.
  [Text delta](https://developers.openai.com/api/reference/resources/responses/streaming-events#response.output_text.delta),
  [text done](https://developers.openai.com/api/reference/resources/responses/streaming-events#response.output_text.done)
- `response.output_text.annotation.added`, which adds `annotation_index` and
  an annotation object to those same item/content coordinates. The streaming
  event declares the annotation as an open object, so consumers must
  discriminate its `type` before decoding it as a URL citation.
  [Annotation event](https://developers.openai.com/api/reference/resources/responses/streaming-events#response.output_text.annotation.added)
- `response.reasoning_summary_part.added`/`.done` and
  `response.reasoning_summary_text.delta`/`.done`, keyed by reasoning item and
  summary index. These are separate from visible output text.
  [Reasoning summary part](https://developers.openai.com/api/reference/resources/responses/streaming-events#response.reasoning_summary_part.added),
  [reasoning summary text](https://developers.openai.com/api/reference/resources/responses/streaming-events#response.reasoning_summary_text.delta)

Each published event carries an integer `sequence_number`. The documentation
does not publish a maximum event count, maximum sequence number, maximum text
delta size, maximum annotation count, or maximum sources count.

## Final output, actions, citations, and sources

A successful search response has at least a `web_search_call` output item and a
`message` output item. The search item has this logical shape:

```text
WebSearchCall {
  id: string
  type: "web_search_call"
  status: string
  action:
    | { type: "search", queries?: string[], query?: string, sources?: Source[] }
    | { type: "open_page", url?: string | null }
    | { type: "find_in_page", url: string, pattern: string }
}

Source { type: "url", url: string }
```

`query` is deprecated in the current schema in favor of optional `queries`.
The guide warns that a search action usually, but not always, includes the
queries. Therefore neither field is safe to require. `sources` is optional and
is requested with `include: ["web_search_call.action.sources"]`.
[Output and actions](https://developers.openai.com/api/docs/guides/tools-web-search#output-and-citations),
[complete sources](https://developers.openai.com/api/docs/guides/tools-web-search#sources),
[response schema](https://developers.openai.com/api/reference/resources/responses/streaming-events#response.completed)

The assistant message contains `output_text`. A URL citation annotation has:

```json
{
  "type": "url_citation",
  "start_index": 0,
  "end_index": 10,
  "url": "https://example.com/article",
  "title": "Example article"
}
```

The indices locate the cited span in that output text. Inline citations must be
visible and clickable when web-derived results are shown to users. Citations
are the model-selected references; `sources` is the broader list of URLs
consulted and may be larger.
[Output and citations](https://developers.openai.com/api/docs/guides/tools-web-search#output-and-citations),
[sources distinction](https://developers.openai.com/api/docs/guides/tools-web-search#sources)

The public schema does not document a content hash, retrieval timestamp, page
body, title, or citation offsets on `Source`; it contains only `type: "url"`
and `url`. A consumer must not invent page-content provenance from this object.

## Usage

Token usage is part of the terminal Response object and is optional in the
published schema:

```text
usage {
  input_tokens: integer
  input_tokens_details: { cached_tokens: integer, cache_write_tokens?: integer }
  output_tokens: integer
  output_tokens_details: { reasoning_tokens: integer }
  total_tokens: integer
}
```

Web search calls also have separate tool-call pricing. The token `usage` object
does not expose a search-call count field; count `web_search_call` output items
when the application needs a per-response call count.
[Completed response schema](https://developers.openai.com/api/reference/resources/responses/streaming-events#response.completed),
[web search pricing note](https://developers.openai.com/api/docs/guides/tools-web-search#output-and-citations)

## Terminal, failure, and cancellation semantics

The documented streaming terminal events are:

- `response.completed`: includes the completed Response object.
- `response.failed`: includes the failed Response object; inspect
  `response.error` (`code` and `message`).
- `response.incomplete`: includes the incomplete Response object; documented
  `incomplete_details.reason` values are `max_output_tokens` and
  `content_filter`.
- `error`: a stream error with nullable `code`, required `message`, nullable
  `param`, and `sequence_number`.

Sources: [completed](https://developers.openai.com/api/reference/resources/responses/streaming-events#response.completed),
[failed](https://developers.openai.com/api/reference/resources/responses/streaming-events#response.failed),
[incomplete](https://developers.openai.com/api/reference/resources/responses/streaming-events#response.incomplete),
[error](https://developers.openai.com/api/reference/resources/responses/streaming-events#error).

The public streaming-event reference does not document a
`response.cancelled` SSE event. The Cancel Response endpoint only applies to
responses created with `background: true`; it returns a Response whose
`status` is `cancelled`, and its example has `usage: null`. For an ordinary
foreground stream, client cancellation/connection closure must therefore be a
local cancellation path rather than a parser state that waits for a
`response.cancelled` event.
[Cancel response reference](https://developers.openai.com/api/reference/resources/responses/methods/cancel),
[background cancellation guide](https://developers.openai.com/api/docs/guides/background#cancelling-a-background-response)

The official Responses streaming documentation does not promise a `[DONE]`
sentinel. A robust terminal decision should be based on a typed terminal event,
an explicit local cancellation, or an error/EOF policy, not on an undocumented
sentinel.
[Streaming Responses](https://developers.openai.com/api/docs/guides/streaming-responses)

## Structured Outputs compatibility: undocumented

The Structured Outputs guide documents `text.format` with
`type: "json_schema"`; the Web Search guide independently documents
`tools: [{"type":"web_search"}]`. Neither guide shows both features in one
request or says that the combination is supported. The API reference accepts
both fields at the request-schema level, but it does not state cross-feature
compatibility for a particular model or backend.
[Structured Outputs](https://developers.openai.com/api/docs/guides/structured-outputs),
[Web Search](https://developers.openai.com/api/docs/guides/tools-web-search),
[Create response](https://developers.openai.com/api/reference/resources/responses/methods/create)

Likewise, the Web Search citation contract is described for an assistant
`output_text` response with inline citations. There is no official statement
or example guaranteeing `url_citation` annotations when that output text is a
JSON string constrained by JSON Schema. Complete `sources` are attached to the
separate search call item and are requested through `include`, but their
presence in a JSON-Schema request is still not explicitly guaranteed.

Consequently, an implementation that requires structured planning output and
hosted search should not assume this combination from public docs alone. It
needs either a backend-specific documented contract or an end-to-end
qualification test. A portable alternative is a two-response design: perform
the citation-bearing hosted search response first, then transform the captured
result into structured data in a second request. That is an architectural
option, not an OpenAI-documented requirement.

## Parser implications derived from the contract

These are engineering consequences, not additional claims about server
behavior:

1. Correlate by `item_id`; use `output_index`, `content_index`, and
   `annotation_index` as bounds-checked coordinates, and retain
   `sequence_number` for ordering diagnostics.
2. Treat malformed JSON or a malformed payload for a known event as a protocol
   error. Unknown future event types can be preserved/ignored for forward
   compatibility, but they must not count as a successful terminal event.
3. Do not mark a search successful merely because
   `response.web_search_call.completed` arrived. Reconcile it with the done or
   terminal `web_search_call` item and its status/action.
4. Decode `url_citation` only after checking the annotation discriminator and
   validate `0 <= start_index <= end_index <= len(text)` locally; the public
   schema does not publish a maximum or server-side offset guarantee.
5. Bound frame bytes, cumulative text, item count, citation count, source count,
   and open search calls locally. Except for domain filters and the 128k search
   context limit, the public docs do not provide safe parser-memory limits.
6. Require exactly one accepted terminal outcome in application state. EOF
   before a typed terminal event is truncated transport unless a local
   cancellation already owns the outcome.

## Public/OpenAI API boundary

These sources describe `api.openai.com/v1/responses`. They do not document the
private ChatGPT Codex subscription endpoint, its model-directory fields, or its
entitlement/account rules. OfferAgent must use the live Codex catalog as the
authority for whether to expose Hosted Web Search on that backend; this note
only defines the public event and output shapes to parse after the capability
has been authorized.
