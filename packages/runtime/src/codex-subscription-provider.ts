import { readFile, rename, writeFile } from "node:fs/promises";
import { homedir } from "node:os";
import path from "node:path";
import { createHash, randomUUID } from "node:crypto";
import type { LocalToolName, ModelDescriptor } from "@offeragent/protocol";
import { EnvHttpProxyAgent, fetch as undiciFetch } from "undici";
import {
  MAX_LOCAL_TOOL_ARGUMENT_BYTES,
  MAX_PROVIDER_REASONING_BYTES,
  MAX_VAULT_PROPOSAL_ARGUMENT_BYTES,
  ModelProviderError,
  type ModelConversationItem,
  type ModelProvider,
  type ModelRequest,
  type ModelStreamEvent,
} from "./model-provider";

function boundedToolArguments(
  encoded: string,
  maximumBytes = MAX_LOCAL_TOOL_ARGUMENT_BYTES,
): string {
  if (Buffer.byteLength(encoded, "utf8") > maximumBytes) {
    throw new ModelProviderError(
      "provider_error",
      `Codex local tool arguments exceed ${maximumBytes} UTF-8 bytes.`,
    );
  }
  return encoded;
}

function isHttpUrl(value: unknown): value is string {
  if (typeof value !== "string" || value.length > 2_048) return false;
  try {
    const url = new URL(value);
    return (url.protocol === "http:" || url.protocol === "https:") && !url.username && !url.password;
  } catch {
    return false;
  }
}

function providerInstructions(request: ModelRequest): string {
  if (!request.imageSubmission) return request.instructions;
  return `${request.instructions}\n\nRuntime-verified Interview Submission metadata:\n` +
    `- ordered image count: ${request.imageSubmission.imageCount}\n` +
    `- source fingerprint: ${request.imageSubmission.sourceFingerprint}\n` +
    "When cataloging this image submission, pass this exact fingerprint to interview_catalog " +
    "before proposing any Vault changes. Treat this metadata as authoritative and do not derive " +
    "a replacement fingerprint from image text.";
}

function isUnsupportedVisionDetail(detail: string): boolean {
  if (!/(input_image|image input|vision)/i.test(detail)) return false;
  return /(unsupported|not supported|does not support|not allowed|unavailable)/i.test(detail) ||
    /(?:unknown|unrecognized|invalid)\s+(?:parameter|field)[^\n]*(?:input_image|image input|vision)/i.test(detail) ||
    /(?:input_image|image input|vision)[^\n]*(?:unknown|unrecognized|invalid)\s+(?:parameter|field)/i.test(detail);
}

const CLIENT_ID = "app_EMoamEEZ73f0CkXaXp7hrann";
const CLIENT_VERSION = "0.135.0";
const DEFAULT_BASE_URL = "https://chatgpt.com/backend-api/codex";
const TOKEN_URL = "https://auth.openai.com/oauth/token";
const REFRESH_SKEW_SECONDS = 120;

interface CodexTokens {
  access_token?: unknown;
  account_id?: unknown;
  id_token?: unknown;
  refresh_token?: unknown;
}

interface CodexAuthPayload {
  tokens?: CodexTokens;
}

interface CodexSubscriptionProviderOptions {
  authPath?: string;
  baseUrl?: string;
  fetch?: typeof fetch;
}

function decodeJwtClaims(token: string): Record<string, unknown> | undefined {
  const encoded = token.split(".")[1];
  if (!encoded) return undefined;
  try {
    return JSON.parse(Buffer.from(encoded, "base64url").toString("utf8")) as Record<string, unknown>;
  } catch {
    return undefined;
  }
}

function accountId(tokens: CodexTokens, accessToken: string): string | undefined {
  if (typeof tokens.account_id === "string" && tokens.account_id) return tokens.account_id;
  for (const candidate of [tokens.id_token, accessToken]) {
    if (typeof candidate !== "string") continue;
    const claims = decodeJwtClaims(candidate);
    const direct = claims?.chatgpt_account_id;
    if (typeof direct === "string" && direct) return direct;
    const namespaced = claims?.["https://api.openai.com/auth"];
    if (namespaced && typeof namespaced === "object") {
      const nested = (namespaced as Record<string, unknown>).chatgpt_account_id;
      if (typeof nested === "string" && nested) return nested;
    }
    const legacy = claims?.["https://api.openai.com/auth.chatgpt_account_id"];
    if (typeof legacy === "string" && legacy) return legacy;
  }
  return undefined;
}

function isExpiring(accessToken: string): boolean {
  const expiresAt = decodeJwtClaims(accessToken)?.exp;
  return (
    typeof expiresAt === "number" &&
    expiresAt <= Math.floor(Date.now() / 1_000) + REFRESH_SKEW_SECONDS
  );
}

function authRequired(options?: ErrorOptions): ModelProviderError {
  return new ModelProviderError(
    "auth_required",
    "Codex sign-in is unavailable. Open Codex, sign in with ChatGPT, and retry.",
    options,
  );
}

export class CodexSubscriptionProvider implements ModelProvider {
  readonly #authPath: string;
  readonly #baseUrl: string;
  readonly #fetch: typeof fetch;

  get backendId(): string {
    return `codex:${createHash("sha256").update(this.#baseUrl).digest("hex").slice(0, 24)}`;
  }

  constructor(options: CodexSubscriptionProviderOptions = {}) {
    this.#authPath =
      options.authPath ??
      process.env.OFFERAGENT_CODEX_AUTH_FILE ??
      path.join(process.env.CODEX_HOME || path.join(homedir(), ".codex"), "auth.json");
    this.#baseUrl = (
      options.baseUrl ?? process.env.OFFERAGENT_CODEX_BASE_URL ?? DEFAULT_BASE_URL
    ).replace(/\/$/, "");
    if (options.fetch) {
      this.#fetch = options.fetch;
    } else {
      const dispatcher = new EnvHttpProxyAgent();
      this.#fetch = ((input: URL | RequestInfo, init?: RequestInit) =>
        undiciFetch(input as string, {
          ...(init as Parameters<typeof undiciFetch>[1]),
          dispatcher,
        }) as unknown as Promise<Response>) as typeof fetch;
    }
  }

  async listModels(): Promise<ModelDescriptor[]> {
    const response = await this.#request(
      `${this.#baseUrl}/models?client_version=${encodeURIComponent(CLIENT_VERSION)}`,
      { headers: { accept: "application/json" } },
      "catalog",
    );
    let payload: unknown;
    try {
      payload = await response.json();
    } catch (error) {
      throw new ModelProviderError(
        "provider_error",
        "Codex returned an invalid model catalog.",
        { cause: error },
      );
    }
    const rows =
      payload && typeof payload === "object" && Array.isArray((payload as { models?: unknown }).models)
        ? (payload as { models: unknown[] }).models
        : [];
    const models = rows.flatMap((row): ModelDescriptor[] => {
      if (!row || typeof row !== "object") return [];
      const model = row as Record<string, unknown>;
      if (model.visibility === "hide") return [];
      const id = typeof model.slug === "string" ? model.slug : model.id;
      if (typeof id !== "string" || !id) return [];
      const label =
        typeof model.display_name === "string"
          ? model.display_name
          : typeof model.label === "string"
            ? model.label
            : id;
      const supportsFastMode = model.supports_fast_mode === true;
      return [{ id, label, ...(supportsFastMode ? { supportsFastMode } : {}) }];
    });
    if (models.length === 0) {
      throw new ModelProviderError(
        "provider_error",
        "Codex did not report any available subscription models.",
      );
    }
    return models;
  }

  async *stream(request: ModelRequest): AsyncIterable<ModelStreamEvent> {
    const response = await this.#request(
      `${this.#baseUrl}/responses`,
      {
        method: "POST",
        headers: {
          accept: "text/event-stream",
          "content-type": "application/json",
          session_id: randomUUID(),
        },
        signal: request.signal,
        body: JSON.stringify({
          model: request.model,
          ...(request.fastMode ? { service_tier: "priority" } : {}),
          instructions: providerInstructions(request),
          input: request.input.map((item) => encodeConversationItem(item, request.imageInputs)),
          tools: request.tools.map((tool) =>
            tool.kind === "hosted"
              ? { type: "web_search" }
              : {
                  type: "function",
                  name: tool.name,
                  description: tool.description,
                  parameters: tool.parameters,
                  strict: false,
                },
          ),
          tool_choice: "auto",
          reasoning: { effort: "low", summary: "auto" },
          include: ["reasoning.encrypted_content"],
          store: false,
          stream: true,
        }),
      },
      "agent_run",
    );
    if (!response.body) {
      throw new ModelProviderError("transport_error", "Codex returned no response stream.");
    }
    const contentType = response.headers.get("content-type")?.toLowerCase() ?? "missing";
    // The Codex subscription backend currently omits this header on some successful SSE responses.
    if (contentType !== "missing" && !contentType.includes("text/event-stream")) {
      throw new ModelProviderError(
        "provider_error",
        `Codex returned an invalid streaming response (${contentType}).`,
      );
    }

    const decoder = new TextDecoder();
    let buffer = "";
    let completed = false;
    let emittedOutputText = false;
    const functionArguments = new Map<string, string>();
    const pendingOutputEvents: Array<{
      arrivalOrder: number;
      events: ModelStreamEvent[];
      outputIndex: number;
    }> = [];
    let outputItemArrivalOrder = 0;
    try {
      for await (const chunk of response.body) {
        buffer += decoder.decode(chunk, { stream: true });
        let boundary = /\r?\n\r?\n/.exec(buffer);
        while (boundary?.index !== undefined) {
          const frame = buffer.slice(0, boundary.index);
          buffer = buffer.slice(boundary.index + boundary[0].length);
          for (const line of frame.split(/\r?\n/)) {
            if (!line.startsWith("data:")) continue;
            const data = line.slice(5).trim();
            if (!data || data === "[DONE]") continue;
            let event: Record<string, unknown>;
            try {
              event = JSON.parse(data) as Record<string, unknown>;
            } catch (error) {
              throw new ModelProviderError(
                "provider_error",
                "Codex returned an invalid streaming event.",
                { cause: error },
              );
            }
            if (event.type === "response.output_text.delta" && typeof event.delta === "string") {
              if (event.delta.length > 0) emittedOutputText = true;
              yield { type: "output_text.delta", delta: event.delta };
            } else if (
              event.type === "response.function_call_arguments.delta" &&
              typeof event.item_id === "string" &&
              typeof event.delta === "string"
            ) {
              functionArguments.set(
                event.item_id,
                boundedToolArguments(
                  `${functionArguments.get(event.item_id) ?? ""}${event.delta}`,
                  MAX_VAULT_PROPOSAL_ARGUMENT_BYTES,
                ),
              );
            } else if (event.type === "response.output_item.done") {
              const item = event.item;
              const itemEvents: ModelStreamEvent[] = [];
              if (item && typeof item === "object") {
                const call = item as Record<string, unknown>;
                if (
                  call.type === "reasoning" &&
                  typeof call.id === "string" &&
                  Array.isArray(call.content) &&
                  typeof call.encrypted_content === "string" &&
                  Array.isArray(call.summary)
                ) {
                  const reasoningItem = {
                    type: "reasoning" as const,
                    id: call.id,
                    content: call.content,
                    encrypted_content: call.encrypted_content,
                    summary: call.summary,
                  };
                  if (
                    Buffer.byteLength(JSON.stringify(reasoningItem), "utf8") >
                    MAX_PROVIDER_REASONING_BYTES
                  ) {
                    throw new ModelProviderError(
                      "provider_error",
                      `Codex reasoning context exceeds ${MAX_PROVIDER_REASONING_BYTES} UTF-8 bytes.`,
                    );
                  }
                  itemEvents.push({ type: "provider_reasoning", item: reasoningItem });
                } else if (call.type === "web_search_call" && typeof call.id === "string") {
                  const action = call.action && typeof call.action === "object"
                    ? call.action as Record<string, unknown>
                    : {};
                  const sources = Array.isArray(action.sources)
                    ? action.sources.slice(0, 20).flatMap((candidate) => {
                        if (!candidate || typeof candidate !== "object") return [];
                        const source = candidate as Record<string, unknown>;
                        if (!isHttpUrl(source.url)) return [];
                        return [{
                          url: source.url,
                          ...(typeof source.title === "string" &&
                          Buffer.byteLength(source.title, "utf8") <= 512
                            ? { title: source.title }
                            : {}),
                        }];
                      })
                    : [];
                  itemEvents.push({ type: "hosted_web_search_call", callId: call.id, sources });
                } else if (call.type === "message" && Array.isArray(call.content)) {
                  const completedText: string[] = [];
                  for (const content of call.content) {
                    if (!content || typeof content !== "object") continue;
                    const output = content as Record<string, unknown>;
                    if (output.type === "output_text" && typeof output.text === "string") {
                      completedText.push(output.text);
                    }
                    if (!Array.isArray(output.annotations)) continue;
                    for (const candidate of output.annotations) {
                      if (!candidate || typeof candidate !== "object") continue;
                      const annotation = candidate as Record<string, unknown>;
                      if (
                        annotation.type === "url_citation" &&
                        isHttpUrl(annotation.url) &&
                        typeof annotation.title === "string" &&
                        Buffer.byteLength(annotation.title, "utf8") <= 512 &&
                        typeof annotation.start_index === "number" &&
                        typeof annotation.end_index === "number" &&
                        annotation.start_index >= 0 &&
                        annotation.end_index >= annotation.start_index
                      ) {
                        itemEvents.push({
                          type: "url_citation",
                          citation: {
                            url: annotation.url,
                            title: annotation.title,
                            startIndex: annotation.start_index,
                            endIndex: annotation.end_index,
                          },
                        });
                      }
                    }
                  }
                  if (!emittedOutputText && completedText.length > 0) {
                    const text = completedText.join("");
                    if (text.length > 0) {
                      emittedOutputText = true;
                      itemEvents.push({ type: "output_text.delta", delta: text });
                    }
                  }
                } else if (
                  call.type === "function_call" &&
                  typeof call.call_id === "string" &&
                  isLocalToolName(call.name)
                ) {
                  const encodedArguments = boundedToolArguments(
                    typeof call.arguments === "string"
                      ? call.arguments
                      : typeof call.id === "string"
                        ? functionArguments.get(call.id) ?? "{}"
                        : "{}",
                    call.name === "vault_propose_changes"
                      ? MAX_VAULT_PROPOSAL_ARGUMENT_BYTES
                      : MAX_LOCAL_TOOL_ARGUMENT_BYTES,
                  );
                  let arguments_: unknown;
                  try {
                    arguments_ = JSON.parse(encodedArguments);
                  } catch (error) {
                    throw new ModelProviderError(
                      "provider_error",
                      "Codex returned invalid local tool arguments.",
                      { cause: error },
                    );
                  }
                  itemEvents.push({
                    type: "local_tool_call",
                    callId: call.call_id,
                    name: call.name,
                    arguments: arguments_,
                    ...(typeof call.id === "string" ? { providerItemId: call.id } : {}),
                    ...(call.status === "completed" || call.status === "in_progress"
                      ? { providerStatus: call.status }
                      : {}),
                  });
                }
              }
              if (itemEvents.length > 0) {
                const arrivalOrder = outputItemArrivalOrder;
                outputItemArrivalOrder += 1;
                pendingOutputEvents.push({
                  arrivalOrder,
                  events: itemEvents,
                  outputIndex:
                    typeof event.output_index === "number" &&
                    Number.isSafeInteger(event.output_index) &&
                    event.output_index >= 0
                      ? event.output_index
                      : Number.MAX_SAFE_INTEGER,
                });
              }
            } else if (event.type === "response.completed") {
              pendingOutputEvents.sort(
                (left, right) =>
                  left.outputIndex - right.outputIndex || left.arrivalOrder - right.arrivalOrder,
              );
              for (const pending of pendingOutputEvents) {
                for (const pendingEvent of pending.events) yield pendingEvent;
              }
              pendingOutputEvents.length = 0;
              completed = true;
            } else if (
              event.type === "response.incomplete" ||
              event.type === "response.failed" ||
              event.type === "error"
            ) {
              const detail = JSON.stringify(event);
              if (/web_search/i.test(detail) && /(unsupported|unknown|invalid|parameter|tool)/i.test(detail)) {
                throw new ModelProviderError(
                  "unsupported_capability",
                  "This Codex backend does not support hosted Web Search.",
                  { capability: "hosted_web_search" },
                );
              }
              if (isUnsupportedVisionDetail(detail)) {
                throw new ModelProviderError(
                  "unsupported_capability",
                  "This Codex backend or model does not support image input.",
                  { capability: "vision" },
                );
              }
              throw new ModelProviderError(
                "provider_error",
                "Codex could not complete this Agent Run.",
              );
            }
          }
          boundary = /\r?\n\r?\n/.exec(buffer);
        }
      }
    } catch (error) {
      if (error instanceof ModelProviderError) throw error;
      throw new ModelProviderError(
        "transport_error",
        request.signal.aborted
          ? "The Codex Agent Run was interrupted."
          : "The Codex response stream was interrupted.",
        { cause: error },
      );
    }
    if (!completed) {
      throw new ModelProviderError(
        "transport_error",
        "The Codex response stream ended before completion.",
      );
    }
  }

  async #loadAuth(): Promise<{ accessToken: string; accountId?: string }> {
    let payload: CodexAuthPayload;
    try {
      payload = JSON.parse(await readFile(this.#authPath, "utf8")) as CodexAuthPayload;
    } catch (error) {
      throw authRequired({ cause: error });
    }
    const tokens = payload.tokens;
    if (!tokens || typeof tokens.access_token !== "string" || !tokens.access_token) {
      throw authRequired();
    }
    let accessToken = tokens.access_token;
    if (isExpiring(accessToken)) {
      accessToken = await this.#refresh(payload, tokens);
    }
    return { accessToken, accountId: accountId(tokens, accessToken) };
  }

  async #refresh(payload: CodexAuthPayload, tokens: CodexTokens): Promise<string> {
    if (typeof tokens.refresh_token !== "string" || !tokens.refresh_token) throw authRequired();
    let response: Response;
    try {
      response = await this.#fetch(TOKEN_URL, {
        method: "POST",
        headers: { "content-type": "application/x-www-form-urlencoded", accept: "application/json" },
        body: new URLSearchParams({
          grant_type: "refresh_token",
          refresh_token: tokens.refresh_token,
          client_id: CLIENT_ID,
        }),
      });
    } catch (error) {
      throw authRequired({ cause: error });
    }
    if (!response.ok) throw authRequired();
    const refreshed = (await response.json()) as CodexTokens;
    if (typeof refreshed.access_token !== "string" || !refreshed.access_token) throw authRequired();
    payload.tokens = {
      ...tokens,
      ...refreshed,
      refresh_token:
        typeof refreshed.refresh_token === "string" ? refreshed.refresh_token : tokens.refresh_token,
    };
    const temporaryPath = `${this.#authPath}.${process.pid}.tmp`;
    try {
      await writeFile(temporaryPath, JSON.stringify(payload), { encoding: "utf8", mode: 0o600 });
      await rename(temporaryPath, this.#authPath);
    } catch (error) {
      throw authRequired({ cause: error });
    }
    return refreshed.access_token;
  }

  async #request(
    url: string,
    init: RequestInit,
    context: "agent_run" | "catalog",
  ): Promise<Response> {
    const auth = await this.#loadAuth();
    let response: Response;
    try {
      response = await this.#fetch(url, {
        ...init,
        headers: {
          ...init.headers,
          authorization: `Bearer ${auth.accessToken}`,
          originator: "codex_cli_rs",
          "user-agent": `codex_cli_rs/${CLIENT_VERSION} (OfferAgent)`,
          version: CLIENT_VERSION,
          ...(auth.accountId ? { "ChatGPT-Account-ID": auth.accountId } : {}),
        },
      });
    } catch (error) {
      throw new ModelProviderError(
        "transport_error",
        "Codex could not be reached. Check your network connection and retry.",
        { cause: error },
      );
    }
    if (response.ok) return response;
    if (response.status === 401 || response.status === 403) throw authRequired();
    let detail = "";
    try {
      detail = await response.clone().text();
    } catch {
      // Classification falls back to a generic Provider error.
    }
    if (
      context === "agent_run" &&
      (response.status === 400 || response.status === 404) &&
      /web_search/i.test(detail) &&
      /(unsupported|unknown|invalid|not allowed|unrecognized|parameter|tool)/i.test(detail)
    ) {
      throw new ModelProviderError(
        "unsupported_capability",
        "This Codex backend does not support hosted Web Search.",
        { capability: "hosted_web_search" },
      );
    }
    if (
      context === "agent_run" &&
      (response.status === 400 || response.status === 404) &&
      isUnsupportedVisionDetail(detail)
    ) {
      throw new ModelProviderError(
        "unsupported_capability",
        "This Codex backend or model does not support image input.",
        { capability: "vision" },
      );
    }
    if (
      context === "agent_run" &&
      (response.status === 400 || response.status === 404) &&
      /model/i.test(detail) &&
      /(not supported|unavailable|unknown|does not exist|invalid)/i.test(detail)
    ) {
      throw new ModelProviderError(
        "model_unavailable",
        "The selected Codex model is unavailable for this subscription.",
      );
    }
    throw new ModelProviderError(
      "provider_error",
      `Codex could not complete the request (HTTP ${response.status}).`,
    );
  }
}

function isLocalToolName(value: unknown): value is LocalToolName {
  return (
    value === "daily_note_context" ||
    value === "interview_catalog" ||
    value === "skill_read" ||
    value === "hosted_web_search_probe" ||
    value === "vault_list" ||
    value === "vault_propose_changes" ||
    value === "vault_read" ||
    value === "vault_search" ||
    value === "web_read"
  );
}

function encodeConversationItem(
  item: ModelConversationItem,
  imageInputs: ModelRequest["imageInputs"],
): Record<string, unknown> {
  if (item.type === "user_message") {
    const images = [...(item.attachments ?? [])]
      .sort((left, right) => left.order - right.order)
      .flatMap((attachment) => {
        const image = imageInputs?.find(
          ({ attachmentId }) => attachmentId === attachment.attachmentId,
        );
        return image ? [{ type: "input_image", image_url: image.dataUrl }] : [];
      });
    return {
      role: "user",
      content: [{ type: "input_text", text: item.text }, ...images],
    };
  }
  if (item.type === "assistant_message") {
    return {
      role: "assistant",
      content: [{ type: "output_text", text: item.text }],
    };
  }
  if (item.type === "provider_reasoning") return { ...item.item };
  if (item.type === "local_tool_call") {
    return {
      type: "function_call",
      ...(item.providerItemId ? { id: item.providerItemId } : {}),
      call_id: item.callId,
      name: item.name,
      arguments: JSON.stringify(item.arguments),
      ...(item.providerStatus ? { status: item.providerStatus } : {}),
    };
  }
  return {
    type: "function_call_output",
    call_id: item.callId,
    output: JSON.stringify(item.result),
  };
}
