# Phase A Codex Runtime Design

## Goal

Add a Codex Responses backend to Khoj's existing chat and research model calls, selected by `KHOJ_CONVERSATION_RUNTIME=codex` by default. Keep the existing OpenAI, Gemini, and Anthropic path unchanged when `KHOJ_CONVERSATION_RUNTIME=api`.

## Architecture

The runtime switch lives in the shared model sending layer:

- `send_message_to_model(...)` routes research/tool-planning calls to Codex before reading the configured API key.
- `agenerate_chat_response(...)` routes normal streamed chat to Codex after Khoj has built the same context messages it already builds for every provider.
- `ConversationAdapters.aget_valid_chat_model(...)` is left unchanged. The existing `ChatModel` still supplies context-window and vision settings; Codex does not get a database enum or migration.

## Components

- `src/khoj/processor/conversation/codex/auth.py`: read Codex CLI or Hermes-style auth JSON, refresh expiring JWT access tokens, write refreshed tokens back to the same file shape, and build Codex headers.
- `src/khoj/processor/conversation/codex/utils.py`: build Responses payloads and normalize text, reasoning summaries, and function tool calls into `ResponseWithThought`.
- `src/khoj/processor/conversation/codex/gpt.py`: create OpenAI SDK clients for the Codex backend and expose `codex_send_message_to_model(...)` plus `converse_codex(...)`.
- `tests/test_codex_conversation_adapter.py`: unit tests with fake auth files and fake SDK clients; no live network.

## Data Flow

Khoj builds `ChatMessage` objects exactly as it does today. The Codex adapter formats them with the existing OpenAI helper, moves the first system message into `instructions`, converts tools with the existing schema helper, and calls `responses.create(...)` against `KHOJ_CODEX_BASE_URL`.

Tool calls return as JSON text containing existing `ToolCall` dictionaries, so the current research loop can execute tools and send tool results back on the next model call.

## Error Handling

Auth errors use explicit codes from the Phase A spec, including missing auth, invalid token shape, missing access/refresh token, relogin-required refresh failures, and Cloudflare challenge responses. Codex runtime does not automatically fall back to API runtime.

If `reasoning.summary` is rejected, the adapter retries the same request with only `reasoning.effort`. Empty responses raise `ValueError("Empty response returned by Codex backend")`.

## Testing

Targeted tests cover auth shape handling, refresh write-back, malformed JWT tolerance, account ID headers, payload omission of empty tools, tool conversion, tool-call normalization, empty-response failures, and the `api` runtime bypass.

The final verification is:

```bash
pytest tests/test_codex_conversation_adapter.py -q
pytest tests/test_online_chat_director.py tests/test_conversation_utils.py -q
```
