import { readFile, rename, writeFile } from "node:fs/promises";
import { homedir } from "node:os";
import path from "node:path";
import { randomUUID } from "node:crypto";
import type { ModelDescriptor } from "@offeragent/protocol";
import { EnvHttpProxyAgent, fetch as undiciFetch } from "undici";
import { ModelProviderError, type ModelProvider, type ModelRequest } from "./model-provider";

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
      return [{ id, label }];
    });
    if (models.length === 0) {
      throw new ModelProviderError(
        "provider_error",
        "Codex did not report any available subscription models.",
      );
    }
    return models;
  }

  async *stream(request: ModelRequest): AsyncIterable<string> {
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
          instructions:
            "You are OfferAgent, an interview preparation assistant. Answer the user's request directly and clearly.",
          input: [
            {
              role: "user",
              content: [{ type: "input_text", text: request.input }],
            },
          ],
          reasoning: { effort: "low", summary: "auto" },
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
              yield event.delta;
            } else if (event.type === "response.completed") {
              completed = true;
            } else if (
              event.type === "response.incomplete" ||
              event.type === "response.failed" ||
              event.type === "error"
            ) {
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
