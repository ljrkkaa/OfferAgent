import { randomUUID } from "node:crypto";
import { createServer, type IncomingMessage, type ServerResponse } from "node:http";
import { homedir } from "node:os";
import type { AddressInfo } from "node:net";
import path from "node:path";
import { WebSocketServer, type WebSocket } from "ws";
import {
  PROTOCOL_VERSION,
  type AgentRunCancel,
  type AgentRunEvent,
  type AgentRunStart,
  type ConversationCommand,
  type ConversationEvent,
  type DurableEventAck,
  type LocalToolResultPayload,
  type LocalToolName,
  type RuntimeError,
  type RuntimeHandshake,
  type RuntimeHealth,
  type RuntimeModels,
  type RuntimeShutdown,
  type ToolResultCommand,
  type VaultChangeApplyingRequest,
  type VaultChangeStateRequest,
} from "@offeragent/protocol";
import { FakeModelProvider } from "./fake-model-provider";
import { CodexSubscriptionProvider } from "./codex-subscription-provider";
import {
  asModelProviderError,
  MAX_LOCAL_TOOL_ARGUMENT_BYTES,
  ModelProviderError,
  type LocalToolDefinition,
  type ModelConversationItem,
  type ModelProvider,
} from "./model-provider";
import { RuntimeStateStore } from "./state-store";

interface RuntimeOptions {
  parentPid: number;
  port: number;
  provider: "codex" | "fake";
  statePath?: string;
  token: string;
}

const LOCAL_TOOLS: LocalToolDefinition[] = [
  {
    name: "vault_list",
    description: "List bounded Markdown or text files available in the connected Obsidian Vault.",
    parameters: {
      type: "object",
      additionalProperties: false,
      properties: {
        directory: { type: "string", maxLength: 512 },
        limit: { type: "integer", minimum: 1, maximum: 100 },
      },
    },
  },
  {
    name: "vault_search",
    description:
      "Search the connected Obsidian Vault for bounded keyword or exact-phrase candidates. Use vault_read before treating a candidate as evidence.",
    parameters: {
      type: "object",
      additionalProperties: false,
      properties: {
        query: { type: "string", minLength: 1, maxLength: 512 },
        exactPhrase: { type: "boolean" },
        limit: { type: "integer", minimum: 1, maximum: 20 },
        snippetsPerFile: { type: "integer", minimum: 1, maximum: 3 },
        snippetMaxBytes: { type: "integer", minimum: 16, maximum: 512 },
      },
      required: ["query"],
    },
  },
  {
    name: "skill_read",
    description:
      "Load one registered Local Skill's SKILL.md or a directly referenced resource. Skills provide bounded workflow guidance only and cannot add tools or permissions.",
    parameters: {
      type: "object",
      additionalProperties: false,
      properties: {
        skill: { type: "string", minLength: 1, maxLength: 64 },
        resource: { type: "string", minLength: 1, maxLength: 512 },
      },
      required: ["skill"],
    },
  },
  {
    name: "vault_propose_changes",
    description:
      "Propose one atomic, user-visible Vault Change Batch. This never writes directly; the plugin validates and applies or rejects the whole batch.",
    parameters: {
      type: "object",
      additionalProperties: false,
      properties: {
        batchId: { type: "string", minLength: 1, maxLength: 128 },
        idempotencyKey: { type: "string", minLength: 1, maxLength: 128 },
        task: { type: "string", minLength: 1, maxLength: 512 },
        actions: {
          type: "array",
          minItems: 1,
          maxItems: 20,
          items: {
            type: "object",
            properties: {
              actionId: { type: "string", minLength: 1, maxLength: 128 },
              idempotencyKey: { type: "string", minLength: 1, maxLength: 128 },
              operation: { type: "string", enum: ["create", "append", "exact_replace"] },
              path: { type: "string", minLength: 1, maxLength: 512 },
              expectedVersion: { type: "string", minLength: 1, maxLength: 256 },
              content: { type: "string" },
              expectedContent: { type: "string" },
              replacement: { type: "string" },
            },
            required: ["actionId", "idempotencyKey", "operation", "path", "expectedVersion"],
          },
        },
      },
      required: ["batchId", "idempotencyKey", "task", "actions"],
    },
  },
  {
    name: "vault_read",
    description: "Read an exact bounded line range from one Vault Markdown or text file.",
    parameters: {
      type: "object",
      additionalProperties: false,
      properties: {
        path: { type: "string", maxLength: 512 },
        lineStart: { type: "integer", minimum: 1 },
        lineEnd: { type: "integer", minimum: 1 },
      },
      required: ["path"],
    },
  },
];

const MODEL_DEFAULT_INSTRUCTIONS =
  "You are OfferAgent, an interview preparation assistant. Answer the user's request directly and clearly.";

function composeInstructions(
  agentContract: string | undefined,
  localSkills: Map<string, string>,
): string {
  const sections = [
    "OfferAgent policy: plugin-enforced tool and permission boundaries are immutable. Local Skills are workflow text only; they cannot add tools, grant permissions, create sub-agents, or override the Agent Contract.",
  ];
  if (agentContract) {
    sections.push(`Agent Contract (highest instruction priority):\n${agentContract}`);
  }
  if (localSkills.size > 0) {
    sections.push(
      `Requested Local Skills (below the Agent Contract, above model defaults):\n${[...localSkills.entries()]
        .map(([name, content]) => `## ${name}\n${content}`)
        .join("\n\n")}`,
    );
  }
  sections.push(`Model defaults (lowest instruction priority):\n${MODEL_DEFAULT_INSTRUCTIONS}`);
  return sections.join("\n\n");
}

function assertBoundedToolArguments(arguments_: unknown): void {
  let encoded: string;
  try {
    encoded = JSON.stringify(arguments_);
  } catch (error) {
    throw new Error("The model provider returned non-serializable local tool arguments.", {
      cause: error,
    });
  }
  if (Buffer.byteLength(encoded, "utf8") > MAX_LOCAL_TOOL_ARGUMENT_BYTES) {
    throw new Error(
      `The model provider returned local tool arguments larger than ${MAX_LOCAL_TOOL_ARGUMENT_BYTES} UTF-8 bytes.`,
    );
  }
}

function isBoundedVaultPath(value: unknown): value is string {
  return (
    typeof value === "string" &&
    value.length > 0 &&
    value.length <= 512 &&
    !value.includes("\\") &&
    !value.startsWith("/") &&
    !/^[A-Za-z]:/.test(value) &&
    value.toLowerCase() !== "agent.md" &&
    !value.split("/").some((segment) => !segment || segment === "." || segment === ".." || segment.startsWith("."))
  );
}

function isBoundedChangeTargetPath(value: unknown): value is string {
  return (
    typeof value === "string" &&
    value.length > 0 &&
    value.length <= 512 &&
    !value.includes("\\") &&
    !value.startsWith("/") &&
    !/^[A-Za-z]:/.test(value) &&
    !value.split("/").some((segment) => !segment || segment === "." || segment === "..")
  );
}

function isLocalToolResultPayload(value: unknown): value is LocalToolResultPayload {
  if (!value || typeof value !== "object") return false;
  const result = value as Partial<LocalToolResultPayload>;
  if (result.ok === false) {
    return Boolean(
      result.error &&
      ["invalid_change", "invalid_path", "malformed_control_file", "not_found", "plugin_disconnected", "request_too_large", "stale_evidence", "tool_error", "undo_conflict"].includes(
        result.error.code as string,
      ) &&
      typeof result.error.message === "string" &&
      Buffer.byteLength(result.error.message, "utf8") <= 2_048,
    );
  }
  if (result.ok !== true || !result.value || typeof result.value !== "object") return false;
  if (result.value.type === "agent_contract_read") {
    return (
      result.value.path === "agent.md" &&
      typeof result.value.content === "string" &&
      result.value.content.trim().length > 0 &&
      Buffer.byteLength(result.value.content, "utf8") <= 32_768 &&
      typeof result.value.modifiedVersion === "string" &&
      result.value.modifiedVersion.length <= 128 &&
      typeof result.value.contentHash === "string" &&
      result.value.contentHash.length <= 128
    );
  }
  if (result.value.type === "skill_read") {
    const expectedPath = `.codex/skills/${result.value.skill}/${result.value.resource}`;
    return (
      typeof result.value.skill === "string" &&
      /^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$/.test(result.value.skill) &&
      typeof result.value.resource === "string" &&
      result.value.resource.length > 0 &&
      result.value.resource.length <= 512 &&
      result.value.path === expectedPath &&
      typeof result.value.content === "string" &&
      result.value.content.trim().length > 0 &&
      Buffer.byteLength(result.value.content, "utf8") <= 32_768 &&
      typeof result.value.modifiedVersion === "string" &&
      result.value.modifiedVersion.length <= 128 &&
      typeof result.value.contentHash === "string" &&
      result.value.contentHash.length <= 128
    );
  }
  if (result.value.type === "vault_propose_changes") {
    return (
      typeof result.value.batchId === "string" &&
      result.value.batchId.length > 0 &&
      result.value.batchId.length <= 128 &&
      (result.value.decision === "applied" || result.value.decision === "rejected") &&
      (result.value.decision === "applied"
        ? result.value.checkpointRef ===
          `refs/offeragent/checkpoints/${result.value.batchId}`
        : result.value.checkpointRef === undefined) &&
      Array.isArray(result.value.targets) &&
      result.value.targets.length > 0 &&
      result.value.targets.length <= 20 &&
      result.value.targets.every(
        (target) =>
          target &&
          typeof target.path === "string" &&
          target.path.length <= 512 &&
          typeof target.beforeHash === "string" &&
          target.beforeHash.length <= 128 &&
          typeof target.afterHash === "string" &&
          target.afterHash.length <= 128,
      )
    );
  }
  if (result.value.type === "vault_list") {
    return (
      typeof result.value.truncated === "boolean" &&
      Array.isArray(result.value.entries) &&
      result.value.entries.length <= 100 &&
      result.value.entries.every(
        (entry) =>
          entry !== null &&
          typeof entry === "object" &&
          isBoundedVaultPath(entry.path) &&
          typeof entry.modifiedVersion === "string" &&
          entry.modifiedVersion.length <= 128 &&
          typeof entry.contentHash === "string" &&
          entry.contentHash.length <= 128,
      )
    );
  }
  if (result.value.type === "vault_search") {
    return (
      typeof result.value.truncated === "boolean" &&
      Array.isArray(result.value.entries) &&
      result.value.entries.length <= 20 &&
      result.value.entries.every(
        (entry) =>
          entry !== null &&
          typeof entry === "object" &&
          isBoundedVaultPath(entry.path) &&
          ["path", "metadata", "body"].includes(entry.matchTier) &&
          typeof entry.modifiedVersion === "string" &&
          entry.modifiedVersion.length <= 128 &&
          typeof entry.contentHash === "string" &&
          entry.contentHash.length <= 128 &&
          Array.isArray(entry.snippets) &&
          entry.snippets.length <= 3 &&
          entry.snippets.every(
            (snippet) =>
              snippet !== null &&
              typeof snippet === "object" &&
              typeof snippet.content === "string" &&
              Buffer.byteLength(snippet.content, "utf8") <= 512 &&
              Number.isInteger(snippet.lineStart) &&
              Number.isInteger(snippet.lineEnd) &&
              snippet.lineStart >= 1 &&
              snippet.lineEnd >= snippet.lineStart &&
              typeof snippet.truncated === "boolean",
          ),
      )
    );
  }
  return (
    result.value.type === "vault_read" &&
    isBoundedVaultPath(result.value.path) &&
    typeof result.value.content === "string" &&
    Buffer.byteLength(result.value.content, "utf8") <= 32_768 &&
    Number.isInteger(result.value.lineStart) &&
    Number.isInteger(result.value.lineEnd) &&
    result.value.lineStart >= 1 &&
    result.value.lineEnd >= result.value.lineStart &&
    result.value.lineEnd - result.value.lineStart + 1 <= 200 &&
    typeof result.value.modifiedVersion === "string" &&
    result.value.modifiedVersion.length <= 128 &&
    typeof result.value.contentHash === "string" &&
    result.value.contentHash.length <= 128 &&
    typeof result.value.truncated === "boolean"
  );
}

function readOption(name: string): string {
  const index = process.argv.indexOf(name);
  const value = index === -1 ? undefined : process.argv[index + 1];
  if (!value || value.startsWith("--")) {
    throw new Error(`Missing required Runtime option: ${name}`);
  }
  return value;
}

function parseIntegerOption(name: string, allowZero = false): number {
  const value = Number(readOption(name));
  const minimum = allowZero ? 0 : 1;
  if (!Number.isInteger(value) || value < minimum) {
    throw new Error(`Runtime option ${name} must be an integer >= ${minimum}`);
  }
  return value;
}

function readOptions(): RuntimeOptions {
  const provider = process.argv.includes("--provider") ? readOption("--provider") : "codex";
  if (provider !== "codex" && provider !== "fake") {
    throw new Error(`Unsupported Runtime Provider: ${provider}`);
  }
  const statePathIndex = process.argv.indexOf("--state-path");
  const statePath = statePathIndex === -1 ? undefined : readOption("--state-path");
  return {
    parentPid: parseIntegerOption("--parent-pid"),
    port: parseIntegerOption("--port", true),
    provider,
    statePath:
      statePath ??
      (provider === "fake"
        ? undefined
        : path.join(
            process.env.LOCALAPPDATA || path.join(homedir(), ".local", "share"),
            "OfferAgent",
            "state.db",
          )),
    token: readOption("--token"),
  };
}

function sendJson(
  response: ServerResponse,
  statusCode: number,
  body: unknown,
): void {
  response.writeHead(statusCode, {
    "content-type": "application/json; charset=utf-8",
    "cache-control": "no-store",
  });
  response.end(JSON.stringify(body));
}

function readJsonBody(request: IncomingMessage): Promise<unknown> {
  return new Promise((resolve, reject) => {
    let body = "";
    request.setEncoding("utf8");
    request.on("data", (chunk: string) => {
      body += chunk;
      if (Buffer.byteLength(body, "utf8") > 64 * 1024) {
        reject(new Error("Runtime journal request exceeds its size limit."));
        request.destroy();
      }
    });
    request.on("end", () => {
      try {
        resolve(JSON.parse(body) as unknown);
      } catch {
        reject(new Error("Runtime journal request must contain valid JSON."));
      }
    });
    request.on("error", reject);
  });
}

function isVaultChangeApplyingRequest(value: unknown): value is VaultChangeApplyingRequest {
  if (!value || typeof value !== "object") return false;
  const request = value as Partial<VaultChangeApplyingRequest>;
  return (
    typeof request.batchId === "string" &&
    request.batchId.length > 0 &&
    request.batchId.length <= 128 &&
    request.checkpointRef === `refs/offeragent/checkpoints/${request.batchId}` &&
    Array.isArray(request.targets) &&
    request.targets.length > 0 &&
    request.targets.length <= 20 &&
    request.targets.every(
      (target) =>
        isBoundedChangeTargetPath(target?.path) &&
        typeof target.beforeHash === "string" &&
        target.beforeHash.length <= 128 &&
        typeof target.afterHash === "string" &&
        target.afterHash.length <= 128,
    )
  );
}

function isVaultChangeStateRequest(value: unknown): value is VaultChangeStateRequest {
  if (!value || typeof value !== "object") return false;
  const request = value as Partial<VaultChangeStateRequest>;
  return (
    typeof request.batchId === "string" &&
    request.batchId.length > 0 &&
    request.batchId.length <= 128 &&
    ["applied", "expired", "recovery_failed", "rolled_back", "undone"].includes(
      request.state as string,
    )
  );
}

function parentExists(parentPid: number): boolean {
  try {
    process.kill(parentPid, 0);
    return true;
  } catch {
    return false;
  }
}

function createProvider(provider: RuntimeOptions["provider"]): ModelProvider {
  if (provider === "codex") return new CodexSubscriptionProvider();
  if (provider === "fake") return new FakeModelProvider();
  throw new Error(`Unsupported Runtime Provider: ${provider satisfies never}`);
}

function sendEvent(socket: WebSocket, event: AgentRunEvent): void {
  if (socket.readyState !== 1) return;
  socket.send(JSON.stringify(event));
}

function isAgentRunStart(value: unknown): value is AgentRunStart {
  if (!value || typeof value !== "object") return false;
  const message = value as Partial<AgentRunStart>;
  return (
    message.type === "agent_run.start" &&
    message.protocolVersion === PROTOCOL_VERSION &&
    message.sequence === 0 &&
    typeof message.eventId === "string" &&
    typeof message.conversationId === "string" &&
    typeof message.agentRunId === "string" &&
    typeof message.model === "string" &&
    message.input?.role === "user" &&
    typeof message.input.text === "string"
  );
}

function isAgentRunCancel(value: unknown): value is AgentRunCancel {
  if (!value || typeof value !== "object") return false;
  const message = value as Partial<AgentRunCancel>;
  return (
    message.type === "agent_run.cancel" &&
    message.protocolVersion === PROTOCOL_VERSION &&
    typeof message.eventId === "string" &&
    typeof message.conversationId === "string" &&
    typeof message.agentRunId === "string" &&
    typeof message.sequence === "number"
  );
}

function isDurableEventAck(value: unknown): value is DurableEventAck {
  if (!value || typeof value !== "object") return false;
  const message = value as Partial<DurableEventAck>;
  return (
    message.type === "event.ack" &&
    message.protocolVersion === PROTOCOL_VERSION &&
    typeof message.eventId === "string" &&
    typeof message.acknowledgedEventId === "string" &&
    typeof message.conversationId === "string" &&
    typeof message.agentRunId === "string" &&
    typeof message.sequence === "number"
  );
}

function isToolResultCommand(value: unknown): value is ToolResultCommand {
  if (!value || typeof value !== "object") return false;
  const message = value as Partial<ToolResultCommand>;
  return (
    message.type === "tool_result" &&
    message.protocolVersion === PROTOCOL_VERSION &&
    typeof message.eventId === "string" &&
    typeof message.conversationId === "string" &&
    typeof message.agentRunId === "string" &&
    typeof message.toolCallId === "string" &&
    typeof message.sequence === "number" &&
    isLocalToolResultPayload(message.result)
  );
}

function isConversationCommand(value: unknown): value is ConversationCommand {
  if (!value || typeof value !== "object") return false;
  const message = value as Partial<ConversationCommand>;
  if (
    message.protocolVersion !== PROTOCOL_VERSION ||
    typeof message.eventId !== "string" ||
    typeof message.conversationId !== "string" ||
    typeof message.agentRunId !== "string" ||
    typeof message.sequence !== "number"
  ) {
    return false;
  }
  if (message.type === "conversation.create") {
    return typeof message.title === "string" && typeof message.model === "string";
  }
  if (message.type === "conversation.update") return typeof message.model === "string";
  return (
    message.type === "conversation.delete" ||
    message.type === "conversation.list" ||
    message.type === "conversation.open"
  );
}

function sendConversationEvent(socket: WebSocket, event: ConversationEvent): void {
  if (socket.readyState === 1) socket.send(JSON.stringify(event));
}

async function handleConversationCommand(
  socket: WebSocket,
  command: ConversationCommand,
  store: RuntimeStateStore,
): Promise<void> {
  const base = {
    protocolVersion: PROTOCOL_VERSION,
    eventId: randomUUID(),
    conversationId: command.conversationId,
    agentRunId: command.agentRunId,
    sequence: command.sequence + 1,
  };
  if (command.type === "conversation.create") {
    const conversation = await store.createConversation({
      id: command.conversationId,
      title: command.title,
      modelId: command.model,
    });
    sendConversationEvent(socket, { ...base, type: "conversation.created", conversation });
    return;
  }
  if (command.type === "conversation.open") {
    const snapshot = await store.getConversation(command.conversationId);
    sendConversationEvent(socket, { ...base, type: "conversation.snapshot", ...snapshot });
    return;
  }
  if (command.type === "conversation.list") {
    const conversations = await store.listConversations();
    sendConversationEvent(socket, { ...base, type: "conversation.list", conversations });
    return;
  }
  if (command.type === "conversation.update") {
    const conversation = await store.updateConversationModel(
      command.conversationId,
      command.model,
    );
    sendConversationEvent(socket, { ...base, type: "conversation.updated", conversation });
    return;
  }
  await store.deleteConversation(command.conversationId);
  sendConversationEvent(socket, { ...base, type: "conversation.deleted" });
}

async function startRuntime({
  parentPid,
  port,
  provider: providerName,
  statePath,
  token,
}: RuntimeOptions): Promise<void> {
  const instanceId = randomUUID();
  const provider = createProvider(providerName);
  const store = await RuntimeStateStore.open(statePath);
  const activeRuns = new Map<
    string,
    {
      cancelRequested: boolean;
      controller: AbortController;
      conversationId: string;
      socket: WebSocket;
    }
  >();
  const pendingToolResults = new Map<
    string,
    {
      agentRunId: string;
      conversationId: string;
      reject: (error: Error) => void;
      resolve: (result: LocalToolResultPayload) => void;
      sequence: number;
      socket: WebSocket;
    }
  >();
  const completedToolResults = new Set<string>();
  let exiting = false;

  const server = createServer((request, response) => {
    if (request.headers.authorization !== `Bearer ${token}`) {
      sendJson(response, 401, {
        code: "unauthorized",
        message: "A valid one-time Runtime token is required.",
      });
      return;
    }

    const requestUrl = new URL(request.url ?? "/", "http://127.0.0.1");

    if (request.method === "GET" && requestUrl.pathname === "/vault-changes") {
      const states = (requestUrl.searchParams.get("states") ?? "")
        .split(",")
        .filter(Boolean);
      const allowed = new Set([
        "pending", "applying", "applied", "rejected", "failed", "rolled_back",
        "recovery_failed", "undone", "expired",
      ]);
      if (states.length === 0 || states.some((state) => !allowed.has(state))) {
        sendJson(response, 400, { code: "not_found", message: "Vault Change states are invalid." });
        return;
      }
      void store.listVaultChangeBatches(states as never).then(
        (batches) => sendJson(response, 200, { batches }),
        (error: unknown) => sendJson(response, 500, {
          code: "storage_error",
          message: error instanceof Error ? error.message : "Vault Change journal read failed.",
        }),
      );
      return;
    }

    if (request.method === "POST" && requestUrl.pathname === "/vault-changes/applying") {
      void readJsonBody(request).then(async (body) => {
        if (!isVaultChangeApplyingRequest(body)) {
          sendJson(response, 400, { code: "not_found", message: "Applying metadata is invalid." });
          return;
        }
        await store.markVaultChangeApplying(body.batchId, body.checkpointRef, body.targets);
        sendJson(response, 200, { status: "applying" });
      }).catch((error: unknown) => {
        sendJson(response, 409, {
          code: "storage_error",
          message: error instanceof Error ? error.message : "Applying metadata could not be stored.",
        });
      });
      return;
    }

    if (request.method === "POST" && requestUrl.pathname === "/vault-changes/state") {
      void readJsonBody(request).then(async (body) => {
        if (!isVaultChangeStateRequest(body)) {
          sendJson(response, 400, { code: "not_found", message: "Vault Change state is invalid." });
          return;
        }
        await store.markVaultChangeState(body.batchId, body.state);
        sendJson(response, 200, { status: body.state });
      }).catch((error: unknown) => {
        sendJson(response, 409, {
          code: "storage_error",
          message: error instanceof Error ? error.message : "Vault Change state could not be stored.",
        });
      });
      return;
    }

    if (request.method === "GET" && request.url === "/health") {
      sendJson(response, 200, {
        status: "healthy",
        instanceId,
        protocolVersion: PROTOCOL_VERSION,
      });
      return;
    }

    if (request.method === "GET" && request.url === "/models") {
      void provider.listModels().then(
        (models) => sendJson(response, 200, { models }),
        (error: unknown) => {
          const providerError = asModelProviderError(error);
          sendJson(response, providerError.code === "auth_required" ? 401 : 502, {
            code: providerError.code,
            message: providerError.message,
          });
        },
      );
      return;
    }

    if (request.method === "POST" && request.url === "/shutdown") {
      sendJson(response, 202, { status: "shutting_down" });
      shutdown();
      return;
    }

    sendJson(response, 404, {
      code: "not_found",
      message: "The requested Runtime management endpoint does not exist.",
    });
  });
  const sockets = new Set<WebSocket>();
  const webSockets = new WebSocketServer({ noServer: true });

  server.on("upgrade", (request, socket, head) => {
    if (
      request.url !== "/events" ||
      request.headers.authorization !== `Bearer ${token}`
    ) {
      socket.write("HTTP/1.1 401 Unauthorized\r\nConnection: close\r\n\r\n");
      socket.destroy();
      return;
    }
    webSockets.handleUpgrade(request, socket, head, (webSocket) => {
      webSockets.emit("connection", webSocket, request);
    });
  });

  webSockets.on("connection", (socket) => {
    sockets.add(socket);
    void store.listUnacknowledgedEvents().then((events) => {
      for (const event of events) sendEvent(socket, event);
    });
    socket.once("close", () => {
      sockets.delete(socket);
      for (const [toolCallId, pending] of pendingToolResults) {
        if (pending.socket !== socket) continue;
        pendingToolResults.delete(toolCallId);
        completedToolResults.add(toolCallId);
        pending.resolve({
          ok: false,
          error: {
            code: "plugin_disconnected",
            message: "The Obsidian plugin disconnected during the Vault tool call.",
          },
        });
      }
      for (const run of activeRuns.values()) {
        if (run.socket === socket) run.controller.abort();
      }
    });
    socket.on("message", (data) => {
      let message: unknown;
      try {
        message = JSON.parse(data.toString("utf8"));
      } catch {
        socket.close(1003, "Messages must be JSON.");
        return;
      }
      if (isToolResultCommand(message)) {
        const pending = pendingToolResults.get(message.toolCallId);
        if (!pending && completedToolResults.has(message.toolCallId)) return;
        if (
          !pending ||
          pending.socket !== socket ||
          pending.agentRunId !== message.agentRunId ||
          pending.conversationId !== message.conversationId ||
          pending.sequence !== message.sequence
        ) {
          socket.close(1008, "Unexpected or out-of-sequence Tool Result.");
          return;
        }
        pendingToolResults.delete(message.toolCallId);
        completedToolResults.add(message.toolCallId);
        pending.resolve(message.result);
        return;
      }
      if (isDurableEventAck(message)) {
        void store.acknowledgeDurableEvent(
          message.acknowledgedEventId,
          message.conversationId,
          message.agentRunId,
        );
        return;
      }
      if (isAgentRunCancel(message)) {
        const run = activeRuns.get(message.agentRunId);
        if (
          run &&
          run.socket === socket &&
          run.conversationId === message.conversationId
        ) {
          run.cancelRequested = true;
          run.controller.abort();
        }
        return;
      }
      if (isConversationCommand(message)) {
        void handleConversationCommand(socket, message, store).catch((error: unknown) => {
          sendConversationEvent(socket, {
            type: "conversation.error",
            protocolVersion: PROTOCOL_VERSION,
            eventId: randomUUID(),
            conversationId: message.conversationId,
            agentRunId: message.agentRunId,
            sequence: message.sequence + 1,
            error: {
              code: "storage_error",
              message: error instanceof Error ? error.message : "Runtime State operation failed.",
            },
          });
        });
        return;
      }
      if (!isAgentRunStart(message)) {
        socket.close(1008, "Unsupported protocol message.");
        return;
      }
      if (activeRuns.has(message.agentRunId)) {
        socket.close(1008, "Agent Run identifiers must be unique.");
        return;
      }
      const controller = new AbortController();
      activeRuns.set(message.agentRunId, {
        cancelRequested: false,
        controller,
        conversationId: message.conversationId,
        socket,
      });
      void (async () => {
        let sequence = 1;
        let output = "";
        const base = {
          protocolVersion: PROTOCOL_VERSION,
          conversationId: message.conversationId,
          agentRunId: message.agentRunId,
        };
        const startedEvent: AgentRunEvent = {
          ...base,
          type: "agent_run.started",
          eventId: randomUUID(),
          sequence,
          model: message.model,
        };
        try {
          await store.beginAgentRun(
            message.conversationId,
            message.agentRunId,
            message.model,
            message.input.text,
            startedEvent,
          );
        } catch (error) {
          const providerError = asModelProviderError(error);
          sendEvent(socket, {
            ...base,
            type: "agent_run.failed",
            eventId: randomUUID(),
            sequence,
            error: { code: providerError.code, message: providerError.message },
          });
          activeRuns.delete(message.agentRunId);
          return;
        }
        sendEvent(socket, startedEvent);
        sequence += 1;
        try {
          let input: ModelConversationItem[] = [
            { type: "user_message", text: message.input.text },
          ];
          const requiredRereads = new Set<string>();
          const canonicalReadPaths = new Map<string, string>();
          let agentContract: string | undefined;
          const localSkills = new Map<string, string>();
          const executeLocalTool = async (
            name: LocalToolName,
            arguments_: unknown,
          ): Promise<{ result: LocalToolResultPayload; stalePaths: string[] }> => {
            assertBoundedToolArguments(arguments_);
            const toolCallId = randomUUID();
            const requestedSequence = sequence;
            const requestedEvent: Extract<AgentRunEvent, { type: "tool_call.requested" }> = {
              ...base,
              type: "tool_call.requested",
              eventId: randomUUID(),
              sequence: requestedSequence,
              toolCallId,
              tool: { kind: "local", name, arguments: arguments_ },
            };
            await store.requestToolCall(message.agentRunId, requestedEvent);
            const resultPromise = new Promise<LocalToolResultPayload>((resolve, reject) => {
              pendingToolResults.set(toolCallId, {
                agentRunId: message.agentRunId,
                conversationId: message.conversationId,
                reject,
                resolve,
                sequence: requestedSequence,
                socket,
              });
            });
            const abortToolCall = (): void => {
              const pending = pendingToolResults.get(toolCallId);
              if (!pending) return;
              pendingToolResults.delete(toolCallId);
              completedToolResults.add(toolCallId);
              pending.reject(new Error("The Agent Run was interrupted during a local tool call."));
            };
            controller.signal.addEventListener("abort", abortToolCall, { once: true });
            sendEvent(socket, requestedEvent);
            sequence += 1;
            let result: LocalToolResultPayload;
            try {
              if (controller.signal.aborted) abortToolCall();
              result = await resultPromise;
            } finally {
              pendingToolResults.delete(toolCallId);
              controller.signal.removeEventListener("abort", abortToolCall);
            }
            const completedEvent: Extract<AgentRunEvent, { type: "tool_call.completed" }> = {
              ...base,
              type: "tool_call.completed",
              eventId: randomUUID(),
              sequence,
              toolCallId,
              tool: { kind: "local", name },
              status: result.ok ? "completed" : "failed",
              ...(result.ok ? {} : { error: result.error }),
            };
            const stalePaths = await store.completeToolCall(
              message.agentRunId,
              result,
              completedEvent,
            );
            sendEvent(socket, completedEvent);
            sequence += 1;
            return { result, stalePaths };
          };
          const loadedContract = await executeLocalTool("agent_contract_read", {});
          if (!loadedContract.result.ok) {
            throw new ModelProviderError(
              "instruction_error",
              `Agent Contract could not be loaded: ${loadedContract.result.error.message}`,
            );
          }
          if (loadedContract.result.value.type !== "agent_contract_read") {
            throw new ModelProviderError(
              "instruction_error",
              "The plugin returned an invalid Agent Contract result.",
            );
          }
          agentContract = loadedContract.result.value.content;
          let finished = false;
          for (let step = 0; step < 8; step += 1) {
            output = "";
            let requestedTool = false;
            for await (const providerEvent of provider.stream({
              model: message.model,
              input,
              instructions: composeInstructions(agentContract, localSkills),
              signal: controller.signal,
              tools: LOCAL_TOOLS,
            })) {
              if (providerEvent.type === "output_text.delta") {
                output += providerEvent.delta;
                await store.advanceAgentRunSequence(message.agentRunId, sequence);
                sendEvent(socket, {
                  ...base,
                  type: "agent_run.delta",
                  eventId: randomUUID(),
                  sequence,
                  delta: providerEvent.delta,
                });
                sequence += 1;
                continue;
              }

              requestedTool = true;
              const skillRequest =
                providerEvent.name === "skill_read" &&
                providerEvent.arguments &&
                typeof providerEvent.arguments === "object" &&
                !Array.isArray(providerEvent.arguments) &&
                typeof (providerEvent.arguments as { skill?: unknown }).skill === "string"
                  ? (providerEvent.arguments as { resource?: unknown; skill: string })
                  : undefined;
              const skillWasLoaded = skillRequest
                ? localSkills.has(skillRequest.skill)
                : false;
              if (
                skillRequest &&
                !skillWasLoaded &&
                typeof skillRequest.resource === "string" &&
                skillRequest.resource !== "SKILL.md"
              ) {
                const loadedSkill = await executeLocalTool("skill_read", {
                  skill: skillRequest.skill,
                });
                if (!loadedSkill.result.ok) {
                  input.push(providerEvent, {
                    type: "local_tool_result",
                    callId: providerEvent.callId,
                    result: loadedSkill.result,
                  });
                  break;
                }
                if (
                  loadedSkill.result.value.type !== "skill_read" ||
                  loadedSkill.result.value.skill !== skillRequest.skill ||
                  loadedSkill.result.value.resource !== "SKILL.md"
                ) {
                  throw new ModelProviderError(
                    "instruction_error",
                    "The plugin returned invalid Local Skill instructions.",
                  );
                }
                localSkills.set(skillRequest.skill, loadedSkill.result.value.content);
                break;
              }
              assertBoundedToolArguments(providerEvent.arguments);
              input.push(providerEvent);
              const { result, stalePaths } = await executeLocalTool(
                providerEvent.name,
                providerEvent.arguments,
              );
              if (
                result.ok &&
                result.value.type === "skill_read" &&
                result.value.resource === "SKILL.md"
              ) {
                localSkills.set(result.value.skill, result.value.content);
              }
              const currentReadPath =
                result.ok && result.value.type === "vault_read" ? result.value.path : undefined;
              if (currentReadPath) canonicalReadPaths.set(providerEvent.callId, currentReadPath);
              if (stalePaths.length > 0) {
                const stalePathSet = new Set(stalePaths);
                const staleCallIds = new Set(
                  input
                    .filter(
                      (item): item is Extract<ModelConversationItem, { type: "local_tool_call" }> =>
                        item.type === "local_tool_call" &&
                        item.callId !== providerEvent.callId &&
                        item.name === "vault_read" &&
                        stalePathSet.has(canonicalReadPaths.get(item.callId) ?? ""),
                    )
                    .map((item) => item.callId),
                );
                input = input.filter(
                  (item) =>
                    !(
                      (item.type === "local_tool_call" || item.type === "local_tool_result") &&
                      staleCallIds.has(item.callId)
                    ),
                );
                for (const staleCallId of staleCallIds) canonicalReadPaths.delete(staleCallId);
                for (const stalePath of stalePaths) requiredRereads.add(stalePath);
              }
              if (currentReadPath) requiredRereads.delete(currentReadPath);
              const providerResult: LocalToolResultPayload =
                stalePaths.length > 0 &&
                !currentReadPath &&
                !(result.ok && result.value.type === "vault_propose_changes")
                  ? {
                      ok: false,
                      error: {
                        code: "stale_evidence",
                        message: `Vault evidence changed for ${stalePaths.join(", ")}. Call vault_read for each changed path before continuing.`,
                      },
                    }
                  : result;
              input.push({
                type: "local_tool_result",
                callId: providerEvent.callId,
                result: providerResult,
              });
              if (providerEvent.name === "skill_read" && !skillWasLoaded) break;
            }
            if (requestedTool) continue;

            if (requiredRereads.size > 0) {
              throw new ModelProviderError(
                "provider_error",
                `Vault evidence changed. Reread ${[...requiredRereads].join(", ")} before continuing.`,
              );
            }

            const completedEvent: Extract<AgentRunEvent, { type: "agent_run.completed" }> = {
              ...base,
              type: "agent_run.completed",
              eventId: randomUUID(),
              sequence,
              output: { role: "assistant", text: output },
            };
            await store.completeAgentRun(message.agentRunId, output, completedEvent);
            sendEvent(socket, completedEvent);
            finished = true;
            break;
          }
          if (!finished) {
            throw new Error("The Agent Run exceeded the maximum of 8 Provider steps.");
          }
        } catch (error) {
          const run = activeRuns.get(message.agentRunId);
          if (controller.signal.aborted) {
            const cancelled = run?.cancelRequested === true;
            const terminalEvent: AgentRunEvent = {
              ...base,
              type: cancelled ? "agent_run.cancelled" : "agent_run.interrupted",
              eventId: randomUUID(),
              sequence,
            };
            const transitioned = cancelled
              ? await store.cancelAgentRun(message.agentRunId, terminalEvent as Extract<AgentRunEvent, { type: "agent_run.cancelled" }>)
              : await store.interruptAgentRun(message.agentRunId, terminalEvent as Extract<AgentRunEvent, { type: "agent_run.interrupted" }>);
            if (transitioned) sendEvent(socket, terminalEvent);
            return;
          }
          const providerError = asModelProviderError(error);
          const failedEvent: AgentRunEvent = {
            ...base,
            type: "agent_run.failed",
            eventId: randomUUID(),
            sequence,
            error: { code: providerError.code, message: providerError.message },
          };
          await store.failAgentRun(
            message.agentRunId,
            providerError.code,
            providerError.message,
            failedEvent,
          );
          sendEvent(socket, failedEvent);
        } finally {
          activeRuns.delete(message.agentRunId);
        }
      })();
    });
  });

  const parentWatch = setInterval(() => {
    if (!parentExists(parentPid)) shutdown();
  }, 500);
  parentWatch.unref();

  const forceExit = (): void => {
    clearInterval(parentWatch);
    process.exit(0);
  };

  const shutdown = (): void => {
    if (exiting) return;
    exiting = true;
    clearInterval(parentWatch);
    for (const run of activeRuns.values()) run.controller.abort();
    for (const socket of sockets) socket.close(1001, "Runtime is shutting down.");
    webSockets.close();
    server.close(() => {
      void store
        .interruptActiveRuns()
        .then(() => store.close())
        .finally(forceExit);
    });
    setTimeout(forceExit, 2_000).unref();
  };

  process.once("SIGINT", shutdown);
  process.once("SIGTERM", shutdown);
  process.once("disconnect", shutdown);

  server.on("error", (error) => {
    process.stderr.write(`OfferAgent Runtime failed: ${error.message}\n`);
    process.exitCode = 1;
  });

  server.listen(port, "127.0.0.1", () => {
    const address = server.address() as AddressInfo;
    const handshake: RuntimeHandshake = {
      instanceId,
      pid: process.pid,
      port: address.port,
      protocolVersion: PROTOCOL_VERSION,
    };
    process.stdout.write(`${JSON.stringify(handshake)}\n`);
  });
}

try {
  void startRuntime(readOptions()).catch((error: unknown) => {
    const message = error instanceof Error ? error.message : String(error);
    process.stderr.write(`OfferAgent Runtime failed: ${message}\n`);
    process.exitCode = 1;
  });
} catch (error) {
  const message = error instanceof Error ? error.message : String(error);
  process.stderr.write(`OfferAgent Runtime failed: ${message}\n`);
  process.exitCode = 1;
}
