import { randomUUID } from "node:crypto";
import { execFile, spawn, type ChildProcessByStdio } from "node:child_process";
import { access } from "node:fs/promises";
import { request } from "node:http";
import path from "node:path";
import { delimiter } from "node:path";
import { once } from "node:events";
import type { Readable } from "node:stream";
import WebSocket from "ws";
import {
  PROTOCOL_VERSION,
  type AgentRunCancel,
  type AgentRunEvent,
  type AgentRunRecord,
  type AgentRunResume,
  type AgentRunStart,
  type ConversationCommand,
  type ConversationEvent,
  type ConversationMessage,
  type ConversationSummary,
  type DurableEventAck,
  type LocalToolResultPayload,
  type ModelDescriptor,
  type ProviderErrorCode,
  type RunAttachmentReference,
  type RuntimeError,
  type RuntimeHandshake,
  type RuntimeHealth,
  type RuntimeHostedWebSearchCapability,
  type RuntimeModels,
  type RuntimeShutdown,
  type StagedRunAttachment,
  type ToolCallRecord,
  type ToolResultCommand,
  type VaultChangeApplyingRequest,
  type VaultChangeCommand,
  type VaultChangeEvent,
  type VaultChangeJournalRecord,
  type VaultChangeStateRequest,
  type VaultChangeTransactionState,
} from "@offeragent/protocol";

const NODE_DIAGNOSTIC =
  "OfferAgent could not find Node.js 20 or newer. Install Node.js and restart Obsidian, or set OFFERAGENT_NODE_PATH to node.exe.";

function unrefTimer(timer: unknown): void {
  const candidate = timer as { unref?: () => void } | undefined;
  if (typeof candidate?.unref === "function") candidate.unref();
}

export interface RuntimeSupervisorOptions {
  loadUserProxyEnvironment?: () => Promise<NodeJS.ProcessEnv>;
  nodeCandidates?: string[];
  parentPid?: number;
  provider?: "codex" | "fake";
  runtimePath: string;
  statePath?: string;
  startupTimeoutMs?: number;
  toolExecutor?: LocalToolExecutor;
  onCancelAgentRun?: (agentRunId: string) => void;
}

export interface LocalToolExecutor {
  execute(
    call: Extract<AgentRunEvent, { type: "tool_call.requested" }>,
  ): Promise<LocalToolResultPayload>;
}

export interface AgentRunRequest {
  agentRunId: string;
  attachments?: RunAttachmentReference[];
  conversationId: string;
  fastMode?: boolean;
  input: string;
  model: string;
}

export interface AgentRunResumeRequest extends Pick<AgentRunRequest, "agentRunId" | "conversationId"> {
  recoveredToolResult?: AgentRunResume["recoveredToolResult"];
}

export interface ConversationSnapshot {
  agentRuns: AgentRunRecord[];
  conversation: ConversationSummary;
  messages: ConversationMessage[];
  toolCalls: ToolCallRecord[];
}

export interface RuntimeClient {
  cancelAgentRun(request: Pick<AgentRunRequest, "agentRunId" | "conversationId">): void;
  createConversation(conversation: ConversationSummary): Promise<ConversationSummary>;
  deleteConversation(conversationId: string): Promise<void>;
  discardAttachment(request: {
    agentRunId: string;
    attachmentId: string;
    conversationId: string;
  }): Promise<void>;
  listModels(): Promise<ModelDescriptor[]>;
  listConversations(): Promise<ConversationSummary[]>;
  onUnavailable(subscriber: UnavailableSubscriber): () => void;
  openConversation(conversationId: string): Promise<ConversationSnapshot>;
  resumeAgentRun(
    request: AgentRunResumeRequest,
  ): AsyncIterable<AgentRunEvent>;
  runAgent(request: AgentRunRequest): AsyncIterable<AgentRunEvent>;
  start(): Promise<RuntimeHandshake>;
  stageAttachment(request: {
    agentRunId: string;
    bytes: Uint8Array;
    conversationId: string;
    fileName: string;
    mediaType: string;
  }): Promise<StagedRunAttachment>;
  stop(): Promise<void>;
  updateConversationModel(conversationId: string, modelId: string): Promise<ConversationSummary>;
}

type UnavailableSubscriber = (message: string) => void;

interface RuntimeConnection extends RuntimeHandshake {
  token: string;
}

type RuntimeChild = ChildProcessByStdio<null, Readable, Readable>;

interface RunChannel {
  allowReplaySequenceGaps: boolean;
  awaitingResumeBoundary: boolean;
  conversationId: string;
  events: AgentRunEvent[];
  expectedSequence: number;
  failure?: Error;
  processedEventIds: Set<string>;
  seenEventIds: Set<string>;
  command: AgentRunResume | AgentRunStart;
  wake?: () => void;
}

interface ConversationChannel {
  command: ConversationCommand;
  conversationId: string;
  expectedSequence: number;
  reject: (error: Error) => void;
  requestEventId: string;
  resolve: (event: ConversationEvent) => void;
  timeout: NodeJS.Timeout;
}

interface VaultChangeChannel {
  command: VaultChangeCommand;
  conversationId: string;
  expectedSequence: number;
  reject: (error: Error) => void;
  requestEventId: string;
  resolve: (event: VaultChangeEvent) => void;
  timeout: NodeJS.Timeout;
}

interface ExpectedConversationEvent {
  conversationId: string;
  expectedSequence: number;
  requestEventId: string;
  requestId: string;
}

const CONVERSATION_EVENT_TYPES = new Set([
  "conversation.created",
  "conversation.deleted",
  "conversation.error",
  "conversation.list",
  "conversation.snapshot",
  "conversation.updated",
]);

const VAULT_CHANGE_EVENT_TYPES = new Set([
  "vault_changes.applying_stored",
  "vault_changes.error",
  "vault_changes.listed",
  "vault_changes.state_stored",
]);

const MAX_RECENT_PROTOCOL_EVENT_IDS = 4_096;

function rememberEventId(eventIds: Set<string>, eventId: string): void {
  eventIds.delete(eventId);
  eventIds.add(eventId);
  if (eventIds.size <= MAX_RECENT_PROTOCOL_EVENT_IDS) return;
  const oldest = eventIds.values().next().value as string | undefined;
  if (oldest) eventIds.delete(oldest);
}

export function isExpectedConversationEvent(
  value: unknown,
  expected: ExpectedConversationEvent,
): value is ConversationEvent {
  if (!value || typeof value !== "object") return false;
  const event = value as Partial<ConversationEvent>;
  return (
    typeof event.type === "string" &&
    CONVERSATION_EVENT_TYPES.has(event.type) &&
    event.protocolVersion === PROTOCOL_VERSION &&
    typeof event.eventId === "string" &&
    event.eventId.length > 0 &&
    event.eventId !== expected.requestEventId &&
    event.conversationId === expected.conversationId &&
    event.agentRunId === expected.requestId &&
    event.sequence === expected.expectedSequence
  );
}

export function isExpectedVaultChangeEvent(
  value: unknown,
  expected: ExpectedConversationEvent & { command: VaultChangeCommand },
): value is VaultChangeEvent {
  if (!value || typeof value !== "object") return false;
  const event = value as Partial<VaultChangeEvent>;
  const validEnvelope = (
    typeof event.type === "string" &&
    VAULT_CHANGE_EVENT_TYPES.has(event.type) &&
    event.protocolVersion === PROTOCOL_VERSION &&
    typeof event.eventId === "string" &&
    event.eventId.length > 0 &&
    event.eventId !== expected.requestEventId &&
    event.conversationId === expected.conversationId &&
    event.agentRunId === expected.requestId &&
    event.sequence === expected.expectedSequence
  );
  if (!validEnvelope) return false;
  if (event.type === "vault_changes.listed") {
    return expected.command.type === "vault_changes.list" &&
      Array.isArray(event.batches) &&
      event.batches.every(
        (batch) =>
          typeof batch?.batchId === "string" &&
          typeof batch.checkpointRef === "string" &&
          typeof batch.state === "string" &&
          Array.isArray(batch.targets) &&
          batch.targets.every(
            (target) =>
              typeof target?.path === "string" &&
              typeof target.beforeHash === "string" &&
              typeof target.afterHash === "string",
          ),
      );
  }
  if (event.type === "vault_changes.applying_stored") {
    return expected.command.type === "vault_changes.applying" &&
      event.batchId === expected.command.batchId;
  }
  if (event.type === "vault_changes.state_stored") {
    return expected.command.type === "vault_changes.state" &&
      event.batchId === expected.command.batchId &&
      event.state === expected.command.state;
  }
  return event.type === "vault_changes.error" &&
    event.error?.code === "storage_error" &&
    typeof event.error.message === "string";
}

function isAgentRunEventEnvelope(value: unknown): value is AgentRunEvent {
  if (!value || typeof value !== "object") return false;
  const event = value as Partial<AgentRunEvent>;
  return (
    typeof event.type === "string" &&
    [
      "agent_run.started",
      "agent_run.resumed",
      "agent_run.delta",
      "agent_run.cancelled",
      "agent_run.interrupted",
      "agent_run.completed",
      "agent_run.failed",
      "hosted_web_search.completed",
      "tool_call.requested",
      "tool_call.completed",
    ].includes(event.type) &&
    event.protocolVersion === PROTOCOL_VERSION &&
    typeof event.eventId === "string" &&
    event.eventId.length > 0 &&
    typeof event.conversationId === "string" &&
    typeof event.agentRunId === "string" &&
    typeof event.sequence === "number"
  );
}

function isDurableAgentRunEvent(event: AgentRunEvent): boolean {
  return event.type !== "agent_run.delta";
}

function isTerminalAgentRunEvent(event: AgentRunEvent): boolean {
  return event.type === "agent_run.cancelled" ||
    event.type === "agent_run.completed" ||
    event.type === "agent_run.failed" ||
    event.type === "agent_run.interrupted";
}

export function isAcceptableAgentRunSequence(
  event: AgentRunEvent,
  expectedSequence: number,
  allowReplaySequenceGaps: boolean,
): boolean {
  if (event.sequence === expectedSequence) return true;
  if (event.sequence < expectedSequence) return false;
  return allowReplaySequenceGaps && isDurableAgentRunEvent(event);
}

export class RuntimeRequestError extends Error {
  readonly code: ProviderErrorCode;

  constructor(code: ProviderErrorCode, message: string) {
    super(message);
    this.name = "RuntimeRequestError";
    this.code = code;
  }
}

function defaultNodeCandidates(environment: NodeJS.ProcessEnv): string[] {
  const executable = process.platform === "win32" ? "node.exe" : "node";
  const candidates = [environment.OFFERAGENT_NODE_PATH];

  for (const directory of (environment.PATH ?? "").split(delimiter)) {
    if (directory) candidates.push(path.join(directory, executable));
  }

  if (process.platform === "win32") {
    candidates.push(
      environment.ProgramFiles
        ? path.join(environment.ProgramFiles, "nodejs", executable)
        : undefined,
      environment.LOCALAPPDATA
        ? path.join(environment.LOCALAPPDATA, "Programs", "nodejs", executable)
        : undefined,
    );
  } else {
    candidates.push("/usr/local/bin/node", "/usr/bin/node");
  }

  return [...new Set(candidates.filter((candidate): candidate is string => Boolean(candidate)))];
}

const PROXY_ENVIRONMENT_NAMES = new Set([
  "ALL_PROXY",
  "HTTP_PROXY",
  "HTTPS_PROXY",
  "NO_PROXY",
]);

async function loadWindowsUserProxyEnvironment(): Promise<NodeJS.ProcessEnv> {
  if (process.platform !== "win32") return {};
  return new Promise((resolve) => {
    execFile(
      "reg.exe",
      ["query", "HKCU\\Environment"],
      { encoding: "utf8", timeout: 3_000, windowsHide: true },
      (error, stdout) => {
        if (error) {
          resolve({});
          return;
        }
        const environment: NodeJS.ProcessEnv = {};
        for (const line of stdout.split(/\r?\n/u)) {
          const match = /^\s*([^\s]+)\s+REG_(?:EXPAND_)?SZ\s+(.+?)\s*$/u.exec(line);
          const name = match?.[1]?.toUpperCase();
          if (name && PROXY_ENVIRONMENT_NAMES.has(name)) environment[name] = match?.[2];
        }
        resolve(environment);
      },
    );
  });
}

async function findNodeExecutable(candidates: string[]): Promise<string> {
  for (const candidate of candidates) {
    try {
      await access(candidate);
      const version = await new Promise<string>((resolve, reject) => {
        execFile(
          candidate,
          ["--version"],
          { timeout: 3_000, windowsHide: true },
          (error, stdout) => {
            if (error) reject(error);
            else resolve(stdout.trim());
          },
        );
      });
      const majorVersion = Number(/^v(\d+)\./.exec(version)?.[1]);
      if (Number.isInteger(majorVersion) && majorVersion >= 20) return candidate;
    } catch {
      // Continue until an installed executable is found.
    }
  }
  throw new Error(NODE_DIAGNOSTIC);
}

function readHandshake(
  child: RuntimeChild,
  timeoutMs: number,
): Promise<RuntimeHandshake> {
  return new Promise((resolve, reject) => {
    let stdout = "";
    let stderr = "";
    const timeout = setTimeout(() => {
      cleanup();
      reject(new Error("OfferAgent Runtime did not become ready before the startup timeout."));
    }, timeoutMs);

    const cleanup = (): void => {
      clearTimeout(timeout);
      child.stdout.off("data", onStdout);
      child.stderr.off("data", onStderr);
      child.off("error", onError);
      child.off("exit", onExit);
    };

    const onStderr = (chunk: Buffer): void => {
      stderr = `${stderr}${chunk.toString("utf8")}`.slice(-2_000);
    };
    const onError = (error: Error): void => {
      cleanup();
      reject(new Error(`OfferAgent Runtime could not start: ${error.message}`));
    };
    const onExit = (code: number | null): void => {
      cleanup();
      const detail = stderr.trim() || `exit code ${code ?? "unknown"}`;
      reject(new Error(`OfferAgent Runtime exited during startup: ${detail}`));
    };
    const onStdout = (chunk: Buffer): void => {
      stdout += chunk.toString("utf8");
      const newline = stdout.indexOf("\n");
      if (newline === -1) return;

      cleanup();
      try {
        const handshake = JSON.parse(stdout.slice(0, newline)) as RuntimeHandshake;
        if (handshake.protocolVersion !== PROTOCOL_VERSION) {
          reject(
            new Error(
              `OfferAgent protocol mismatch: plugin ${PROTOCOL_VERSION}, Runtime ${handshake.protocolVersion}.`,
            ),
          );
          return;
        }
        resolve(handshake);
      } catch (error) {
        const message = error instanceof Error ? error.message : String(error);
        reject(new Error(`OfferAgent Runtime returned an invalid handshake: ${message}`));
      }
    };

    child.stdout.on("data", onStdout);
    child.stderr.on("data", onStderr);
    child.once("error", onError);
    child.once("exit", onExit);
  });
}

function callRuntime<T>(
  connection: RuntimeConnection,
  method: "GET" | "POST",
  pathname: string,
  body?: unknown,
): Promise<T> {
  return new Promise((resolve, reject) => {
    const encodedBody = body === undefined ? undefined : JSON.stringify(body);
    const outgoing = request(
      {
        host: "127.0.0.1",
        port: connection.port,
        method,
        path: pathname,
        headers: {
          authorization: `Bearer ${connection.token}`,
          ...(encodedBody === undefined
            ? {}
            : {
                "content-type": "application/json; charset=utf-8",
                "content-length": Buffer.byteLength(encodedBody, "utf8"),
              }),
        },
      },
      (response) => {
        let body = "";
        response.setEncoding("utf8");
        response.on("data", (chunk) => {
          body += chunk;
        });
        response.on("end", () => {
          if (!response.statusCode || response.statusCode >= 400) {
            try {
              const error = JSON.parse(body) as RuntimeError;
              if (
                error.code === "auth_required" ||
                error.code === "instruction_error" ||
                error.code === "model_unavailable" ||
                error.code === "provider_error" ||
                error.code === "transport_error"
              ) {
                reject(new RuntimeRequestError(error.code, error.message));
              } else {
                reject(new Error(error.message));
              }
            } catch {
              reject(new Error(`OfferAgent Runtime request failed with status ${response.statusCode}.`));
            }
            return;
          }
          try {
            resolve(JSON.parse(body) as T);
          } catch (error) {
            reject(error);
          }
        });
      },
    );
    outgoing.once("error", reject);
    outgoing.setTimeout(2_000, () => {
      outgoing.destroy(new Error("OfferAgent Runtime request timed out."));
    });
    outgoing.end(encodedBody);
  });
}

export class RuntimeSupervisor implements RuntimeClient {
  readonly #options: Required<
    Pick<
      RuntimeSupervisorOptions,
      "loadUserProxyEnvironment" | "parentPid" | "provider" | "runtimePath" | "startupTimeoutMs"
    >
  > & { nodeCandidates: string[]; statePath?: string };
  readonly #toolExecutor?: LocalToolExecutor;
  readonly #onCancelAgentRun?: (agentRunId: string) => void;
  readonly #unavailableSubscribers = new Set<UnavailableSubscriber>();
  #child?: RuntimeChild;
  #connection?: RuntimeConnection;
  #eventSocket?: WebSocket;
  #healthCheckInFlight = false;
  #healthTimer?: NodeJS.Timeout;
  #reconnectPromise?: Promise<void>;
  readonly #conversationChannels = new Map<string, ConversationChannel>();
  readonly #vaultChangeChannels = new Map<string, VaultChangeChannel>();
  readonly #pendingReplayEvents = new Map<string, AgentRunEvent>();
  readonly #processedEventIds = new Set<string>();
  readonly #reconciledConversations = new Set<string>();
  readonly #runChannels = new Map<string, RunChannel>();
  readonly #seenEventIds = new Set<string>();
  #stopping = false;

  constructor(options: RuntimeSupervisorOptions) {
    this.#options = {
      loadUserProxyEnvironment:
        options.loadUserProxyEnvironment ?? loadWindowsUserProxyEnvironment,
      nodeCandidates: options.nodeCandidates ?? defaultNodeCandidates(process.env),
      parentPid: options.parentPid ?? process.pid,
      provider:
        options.provider ??
        (process.env.OFFERAGENT_RUNTIME_PROVIDER === "fake" ? "fake" : "codex"),
      runtimePath: options.runtimePath,
      statePath: options.statePath,
      startupTimeoutMs: options.startupTimeoutMs ?? 10_000,
    };
    this.#toolExecutor = options.toolExecutor;
    this.#onCancelAgentRun = options.onCancelAgentRun;
  }

  onUnavailable(subscriber: UnavailableSubscriber): () => void {
    this.#unavailableSubscribers.add(subscriber);
    return () => this.#unavailableSubscribers.delete(subscriber);
  }

  async start(): Promise<RuntimeHandshake> {
    if (this.#connection && this.#child?.exitCode === null) {
      const { token: _token, ...handshake } = this.#connection;
      return handshake;
    }

    const executable = await findNodeExecutable(this.#options.nodeCandidates);
    const token = randomUUID();
    const arguments_ = [
      this.#options.runtimePath,
      "--port",
      "0",
      "--token",
      token,
      "--parent-pid",
      `${this.#options.parentPid}`,
      "--provider",
      this.#options.provider,
    ];
    if (this.#options.statePath) arguments_.push("--state-path", this.#options.statePath);
    const userProxyEnvironment = await this.#options.loadUserProxyEnvironment();
    const child = spawn(
      executable,
      arguments_,
      {
        env: { ...userProxyEnvironment, ...process.env },
        stdio: ["ignore", "pipe", "pipe"],
        windowsHide: true,
      },
    );
    this.#child = child;
    child.once("exit", (code, signal) => {
      this.#handleExit(child, code, signal);
    });

    try {
      const handshake = await readHandshake(child, this.#options.startupTimeoutMs);
      const connection = { ...handshake, token };
      const health = await callRuntime<RuntimeHealth>(connection, "GET", "/health");
      if (
        health.status !== "healthy" ||
        health.instanceId !== handshake.instanceId ||
        health.protocolVersion !== PROTOCOL_VERSION
      ) {
        throw new Error("OfferAgent Runtime health response did not match its startup handshake.");
      }
      this.#pendingReplayEvents.clear();
      this.#processedEventIds.clear();
      this.#reconciledConversations.clear();
      this.#seenEventIds.clear();
      await this.#openEventSocket(child, connection);
      this.#connection = connection;
      this.#startHealthMonitor(child, connection);
      return handshake;
    } catch (error) {
      this.#closeEventSocket(new Error("OfferAgent Runtime startup did not complete."));
      if (child.exitCode === null) child.kill();
      this.#child = undefined;
      throw error;
    }
  }

  async stop(): Promise<void> {
    const child = this.#child;
    const connection = this.#connection;
    this.#stopping = true;
    this.#child = undefined;
    this.#connection = undefined;
    this.#clearHealthMonitor();
    this.#closeEventSocket(new Error("OfferAgent Runtime is shutting down."));
    if (!child || child.exitCode !== null) {
      this.#stopping = false;
      return;
    }

    const exited = once(child, "exit");
    if (connection) {
      try {
        await callRuntime<RuntimeShutdown>(connection, "POST", "/shutdown");
      } catch {
        child.kill();
      }
    } else {
      child.kill();
    }

    try {
      await Promise.race([
        exited,
        new Promise<void>((resolve) => {
          const timeout = setTimeout(() => {
            if (child.exitCode === null) child.kill();
            resolve();
          }, 3_000);
          unrefTimer(timeout);
        }),
      ]);
    } finally {
      this.#stopping = false;
    }
  }

  async listModels(): Promise<ModelDescriptor[]> {
    const connection = this.#requiredConnection();
    const response = await callRuntime<RuntimeModels>(connection, "GET", "/models");
    return response.models;
  }

  async stageAttachment(input: {
    agentRunId: string;
    bytes: Uint8Array;
    conversationId: string;
    fileName: string;
    mediaType: string;
  }): Promise<StagedRunAttachment> {
    const connection = this.#requiredConnection();
    return new Promise((resolve, reject) => {
      const bytes = Buffer.from(input.bytes);
      const outgoing = request(
        {
          host: "127.0.0.1",
          port: connection.port,
          method: "POST",
          path: "/attachments",
          headers: {
            authorization: `Bearer ${connection.token}`,
            "content-length": bytes.byteLength,
            "content-type": input.mediaType,
            "x-offeragent-agent-run-id": input.agentRunId,
            "x-offeragent-conversation-id": input.conversationId,
            "x-offeragent-file-name": encodeURIComponent(input.fileName),
          },
        },
        (response) => {
          let body = "";
          response.setEncoding("utf8");
          response.on("data", (chunk) => {
            body += chunk;
          });
          response.on("end", () => {
            if (!response.statusCode || response.statusCode >= 400) {
              try {
                const error = JSON.parse(body) as RuntimeError;
                reject(new Error(error.message));
              } catch {
                reject(new Error(`Run Attachment upload failed with status ${response.statusCode}.`));
              }
              return;
            }
            try {
              resolve(JSON.parse(body) as StagedRunAttachment);
            } catch (error) {
              reject(error);
            }
          });
        },
      );
      outgoing.once("error", reject);
      outgoing.setTimeout(5_000, () => {
        outgoing.destroy(new Error("Run Attachment upload timed out."));
      });
      outgoing.end(bytes);
    });
  }

  async discardAttachment(input: {
    agentRunId: string;
    attachmentId: string;
    conversationId: string;
  }): Promise<void> {
    const connection = this.#requiredConnection();
    return new Promise((resolve, reject) => {
      const outgoing = request(
        {
          host: "127.0.0.1",
          port: connection.port,
          method: "DELETE",
          path: `/attachments/${encodeURIComponent(input.attachmentId)}`,
          headers: {
            authorization: `Bearer ${connection.token}`,
            "x-offeragent-agent-run-id": input.agentRunId,
            "x-offeragent-conversation-id": input.conversationId,
          },
        },
        (response) => {
          let body = "";
          response.setEncoding("utf8");
          response.on("data", (chunk) => {
            body += chunk;
          });
          response.on("end", () => {
            if (!response.statusCode || response.statusCode >= 400) {
              try {
                reject(new Error((JSON.parse(body) as RuntimeError).message));
              } catch {
                reject(new Error(`Run Attachment discard failed with status ${response.statusCode}.`));
              }
              return;
            }
            resolve();
          });
        },
      );
      outgoing.once("error", reject);
      outgoing.setTimeout(5_000, () => {
        outgoing.destroy(new Error("Run Attachment discard timed out."));
      });
      outgoing.end();
    });
  }

  async getHostedWebSearchCapability(modelId: string): Promise<RuntimeHostedWebSearchCapability> {
    return callRuntime<RuntimeHostedWebSearchCapability>(
      this.#requiredConnection(),
      "GET",
      `/capabilities/web-search?model=${encodeURIComponent(modelId)}`,
    );
  }

  async reprobeHostedWebSearch(modelId: string): Promise<RuntimeHostedWebSearchCapability> {
    return callRuntime<RuntimeHostedWebSearchCapability>(
      this.#requiredConnection(),
      "POST",
      `/capabilities/web-search?model=${encodeURIComponent(modelId)}`,
    );
  }

  async listVaultChangeBatches(
    states: VaultChangeTransactionState[],
  ): Promise<VaultChangeJournalRecord[]> {
    const event = await this.#requestVaultChange({ type: "vault_changes.list", states });
    if (event.type !== "vault_changes.listed") throw new Error("Unexpected Vault Change response.");
    return event.batches;
  }

  async markVaultChangeApplying(
    batchId: string,
    checkpointRef: string,
    targets: VaultChangeApplyingRequest["targets"],
  ): Promise<void> {
    const request: VaultChangeApplyingRequest = { batchId, checkpointRef, targets };
    const event = await this.#requestVaultChange({ type: "vault_changes.applying", ...request });
    if (event.type !== "vault_changes.applying_stored") {
      throw new Error("Unexpected Vault Change response.");
    }
  }

  async markVaultChangeState(
    batchId: string,
    state: VaultChangeStateRequest["state"],
  ): Promise<void> {
    const request: VaultChangeStateRequest = { batchId, state };
    const event = await this.#requestVaultChange({ type: "vault_changes.state", ...request });
    if (event.type !== "vault_changes.state_stored") {
      throw new Error("Unexpected Vault Change response.");
    }
  }

  async listConversations(): Promise<ConversationSummary[]> {
    const event = await this.#requestConversation({
      type: "conversation.list",
      conversationId: "conversation-management",
    });
    if (event.type !== "conversation.list") throw new Error("Unexpected Conversation response.");
    return event.conversations;
  }

  async createConversation(conversation: ConversationSummary): Promise<ConversationSummary> {
    const event = await this.#requestConversation({
      type: "conversation.create",
      conversationId: conversation.id,
      title: conversation.title,
      model: conversation.modelId,
    });
    if (event.type !== "conversation.created") throw new Error("Unexpected Conversation response.");
    return event.conversation;
  }

  async openConversation(conversationId: string): Promise<ConversationSnapshot> {
    const event = await this.#requestConversation({ type: "conversation.open", conversationId });
    if (event.type !== "conversation.snapshot") throw new Error("Unexpected Conversation response.");
    const snapshot = {
      conversation: event.conversation,
      messages: event.messages,
      agentRuns: event.agentRuns,
      toolCalls: event.toolCalls,
    };
    this.#reconcileConversationEvents(conversationId);
    return snapshot;
  }

  async deleteConversation(conversationId: string): Promise<void> {
    const event = await this.#requestConversation({ type: "conversation.delete", conversationId });
    if (event.type !== "conversation.deleted") throw new Error("Unexpected Conversation response.");
  }

  async updateConversationModel(
    conversationId: string,
    modelId: string,
  ): Promise<ConversationSummary> {
    const event = await this.#requestConversation({
      type: "conversation.update",
      conversationId,
      model: modelId,
    });
    if (event.type !== "conversation.updated") throw new Error("Unexpected Conversation response.");
    return event.conversation;
  }

  cancelAgentRun(request: Pick<AgentRunRequest, "agentRunId" | "conversationId">): void {
    this.#onCancelAgentRun?.(request.agentRunId);
    const socket = this.#eventSocket;
    if (!socket || socket.readyState !== WebSocket.OPEN) return;
    const cancel: AgentRunCancel = {
      type: "agent_run.cancel",
      protocolVersion: PROTOCOL_VERSION,
      eventId: randomUUID(),
      conversationId: request.conversationId,
      agentRunId: request.agentRunId,
      sequence: 1,
    };
    socket.send(JSON.stringify(cancel));
  }

  async *runAgent(request: AgentRunRequest): AsyncIterable<AgentRunEvent> {
    const command: AgentRunStart = {
      type: "agent_run.start",
      protocolVersion: PROTOCOL_VERSION,
      eventId: randomUUID(),
      conversationId: request.conversationId,
      agentRunId: request.agentRunId,
      sequence: 0,
      model: request.model,
      ...(request.fastMode ? { fastMode: true } : {}),
      input: {
        role: "user",
        text: request.input,
        ...(request.attachments?.length ? { attachments: request.attachments } : {}),
      },
    };
    yield* this.#runAgentCommand(request, command);
  }

  async *resumeAgentRun(
    request: AgentRunResumeRequest,
  ): AsyncIterable<AgentRunEvent> {
    const command: AgentRunResume = {
      type: "agent_run.resume",
      protocolVersion: PROTOCOL_VERSION,
      eventId: randomUUID(),
      conversationId: request.conversationId,
      agentRunId: request.agentRunId,
      sequence: 0,
      ...(request.recoveredToolResult
        ? { recoveredToolResult: request.recoveredToolResult }
        : {}),
    };
    yield* this.#runAgentCommand({ ...request, input: "", model: "" }, command);
  }

  async *#runAgentCommand(
    request: AgentRunRequest,
    command: AgentRunResume | AgentRunStart,
  ): AsyncIterable<AgentRunEvent> {
    this.#requiredConnection();
    const socket = this.#eventSocket;
    if (!socket || socket.readyState !== WebSocket.OPEN) {
      throw new Error("OfferAgent Runtime event connection is unavailable.");
    }
    if (this.#runChannels.has(request.agentRunId)) {
      throw new Error(`Agent Run '${request.agentRunId}' is already active.`);
    }
    const channel: RunChannel = {
      allowReplaySequenceGaps: false,
      awaitingResumeBoundary: command.type === "agent_run.resume",
      conversationId: request.conversationId,
      events: [],
      expectedSequence: 1,
      processedEventIds: new Set<string>(),
      seenEventIds: new Set<string>(),
      command,
    };
    this.#runChannels.set(request.agentRunId, channel);
    try {
      socket.send(JSON.stringify(command));
    } catch (error) {
      this.#runChannels.delete(request.agentRunId);
      throw error;
    }

    let expectedSequence = 1;
    let lastDeliveredEvent: AgentRunEvent | undefined;
    let terminal = false;
    try {
      while (true) {
        if (channel.failure) throw channel.failure;
        const event = channel.events.shift();
        if (!event) {
          await new Promise<void>((resolve) => {
            channel.wake = resolve;
            if (channel.events.length > 0 || channel.failure) {
              channel.wake = undefined;
              resolve();
            }
          });
          continue;
        }
        if (
          event.protocolVersion !== PROTOCOL_VERSION ||
          typeof event.eventId !== "string" ||
          event.conversationId !== request.conversationId ||
          event.agentRunId !== request.agentRunId ||
          !isAcceptableAgentRunSequence(event, expectedSequence, true)
        ) {
          throw new Error(
            `OfferAgent Runtime sequence gap: expected ${expectedSequence}, received ${event.sequence}.`,
          );
        }
        expectedSequence = event.sequence + 1;
        lastDeliveredEvent = event;
        yield event;
        channel.processedEventIds.add(event.eventId);
        this.#acknowledgeEvent(this.#eventSocket ?? socket, event);
        lastDeliveredEvent = undefined;
        if (
          event.type === "agent_run.cancelled" ||
          event.type === "agent_run.completed" ||
          event.type === "agent_run.failed" ||
          event.type === "agent_run.interrupted"
        ) {
          terminal = true;
          return;
        }
      }
    } finally {
      if (
        lastDeliveredEvent &&
        (lastDeliveredEvent.type === "agent_run.cancelled" ||
          lastDeliveredEvent.type === "agent_run.completed" ||
          lastDeliveredEvent.type === "agent_run.failed" ||
          lastDeliveredEvent.type === "agent_run.interrupted")
      ) {
        channel.processedEventIds.add(lastDeliveredEvent.eventId);
        this.#acknowledgeEvent(this.#eventSocket ?? socket, lastDeliveredEvent);
        terminal = true;
      }
      for (const eventId of channel.seenEventIds) {
        rememberEventId(this.#seenEventIds, eventId);
      }
      for (const eventId of channel.processedEventIds) {
        rememberEventId(this.#processedEventIds, eventId);
      }
      this.#runChannels.delete(request.agentRunId);
      if (
        !terminal &&
        this.#eventSocket?.readyState === WebSocket.OPEN
      ) {
        const cancel: AgentRunCancel = {
          type: "agent_run.cancel",
          protocolVersion: PROTOCOL_VERSION,
          eventId: randomUUID(),
          conversationId: request.conversationId,
          agentRunId: request.agentRunId,
          sequence: expectedSequence,
        };
        this.#eventSocket.send(JSON.stringify(cancel));
      }
    }
  }

  async #openEventSocket(
    child: RuntimeChild,
    connection: RuntimeConnection,
  ): Promise<void> {
    const socket = new WebSocket(`ws://127.0.0.1:${connection.port}/events`, {
      headers: { authorization: `Bearer ${connection.token}` },
    });
    await new Promise<void>((resolve, reject) => {
      const cleanup = (): void => {
        socket.off("open", onOpen);
        socket.off("error", onError);
        socket.off("close", onClose);
      };
      const onOpen = (): void => {
        cleanup();
        resolve();
      };
      const onError = (error: Error): void => {
        cleanup();
        reject(new Error("OfferAgent Runtime event connection failed during startup.", { cause: error }));
      };
      const onClose = (): void => {
        cleanup();
        reject(new Error("OfferAgent Runtime event connection closed during startup."));
      };
      socket.once("open", onOpen);
      socket.once("error", onError);
      socket.once("close", onClose);
    });

    this.#eventSocket = socket;
    socket.on("message", (data) => {
      let event: unknown;
      try {
        event = JSON.parse(data.toString("utf8")) as unknown;
        if (
          !event ||
          typeof event !== "object" ||
          typeof (event as { agentRunId?: unknown }).agentRunId !== "string"
        ) {
          throw new Error("Agent Run id is missing.");
        }
      } catch (error) {
        this.#handleEventSocketFailure(
          child,
          socket,
          new Error("OfferAgent Runtime returned an invalid Agent Run event.", { cause: error }),
        );
        return;
      }
      if ((event as { protocolVersion?: unknown }).protocolVersion !== PROTOCOL_VERSION) {
        this.#handleEventSocketFailure(
          child,
          socket,
          new Error(
            `OfferAgent protocol version mismatch: plugin ${PROTOCOL_VERSION}, Runtime event ${(event as { protocolVersion?: unknown }).protocolVersion ?? "missing"}.`,
          ),
        );
        return;
      }
      const eventRecord = event as { agentRunId: string; type?: unknown };
      if (typeof eventRecord.type === "string" && eventRecord.type.startsWith("vault_changes.")) {
        const channel = this.#vaultChangeChannels.get(eventRecord.agentRunId);
        if (!channel) return;
        if (
          !isExpectedVaultChangeEvent(event, {
            command: channel.command,
            conversationId: channel.conversationId,
            expectedSequence: channel.expectedSequence,
            requestEventId: channel.requestEventId,
            requestId: eventRecord.agentRunId,
          }) ||
          this.#seenEventIds.has(event.eventId)
        ) {
          this.#handleEventSocketFailure(
            child,
            socket,
            new Error("OfferAgent Runtime returned an invalid Vault Change event."),
          );
          return;
        }
        rememberEventId(this.#seenEventIds, event.eventId);
        clearTimeout(channel.timeout);
        this.#vaultChangeChannels.delete(event.agentRunId);
        if (event.type === "vault_changes.error") channel.reject(new Error(event.error.message));
        else channel.resolve(event);
        return;
      }
      if (typeof eventRecord.type === "string" && eventRecord.type.startsWith("conversation.")) {
        const channel = this.#conversationChannels.get(eventRecord.agentRunId);
        if (!channel) return;
        if (
          !isExpectedConversationEvent(event, {
            conversationId: channel.conversationId,
            expectedSequence: channel.expectedSequence,
            requestEventId: channel.requestEventId,
            requestId: eventRecord.agentRunId,
          }) ||
          this.#seenEventIds.has(event.eventId)
        ) {
          this.#handleEventSocketFailure(
            child,
            socket,
            new Error("OfferAgent Runtime returned an invalid Conversation event."),
          );
          return;
        }
        rememberEventId(this.#seenEventIds, event.eventId);
        clearTimeout(channel.timeout);
        this.#conversationChannels.delete(event.agentRunId);
        if (event.type === "conversation.error") channel.reject(new Error(event.error.message));
        else channel.resolve(event);
        return;
      }
      if (!isAgentRunEventEnvelope(event)) {
        this.#handleEventSocketFailure(
          child,
          socket,
          new Error("OfferAgent Runtime returned an invalid Agent Run event."),
        );
        return;
      }
      const channel = this.#runChannels.get(event.agentRunId);
      const seenEventIds = channel?.seenEventIds ?? this.#seenEventIds;
      const processedEventIds = channel?.processedEventIds ?? this.#processedEventIds;
      if (seenEventIds.has(event.eventId)) {
        if (processedEventIds.has(event.eventId)) this.#acknowledgeEvent(socket, event);
        return;
      }
      if (!channel) {
        this.#pendingReplayEvents.set(event.eventId, event);
        if (this.#reconciledConversations.has(event.conversationId)) {
          rememberEventId(this.#processedEventIds, event.eventId);
          rememberEventId(this.#seenEventIds, event.eventId);
          this.#pendingReplayEvents.delete(event.eventId);
          this.#acknowledgeEvent(socket, event);
        }
        return;
      }
      if (
        event.conversationId !== channel.conversationId ||
        !isAcceptableAgentRunSequence(
          event,
          channel.expectedSequence,
          channel.allowReplaySequenceGaps || channel.awaitingResumeBoundary,
        )
      ) {
        this.#handleEventSocketFailure(
          child,
          socket,
          new Error(
            `OfferAgent Runtime sequence gap: expected ${channel.expectedSequence}, received ${event.sequence}.`,
          ),
        );
        return;
      }
      channel.expectedSequence = event.sequence + 1;
      if (event.type === "agent_run.resumed") channel.awaitingResumeBoundary = false;
      channel.seenEventIds.add(event.eventId);
      channel.events.push(event);
      channel.wake?.();
      channel.wake = undefined;
      if (event.type === "tool_call.requested" && event.tool.kind === "local") {
        void this.#executeLocalTool(socket, event);
      }
    });
    socket.once("error", (error) => {
      this.#handleEventSocketFailure(
        child,
        socket,
        new Error("The connection to OfferAgent Runtime failed.", { cause: error }),
      );
    });
    socket.once("close", () => {
      this.#handleEventSocketFailure(
        child,
        socket,
        new Error("OfferAgent Runtime event connection closed unexpectedly."),
      );
    });
    for (const channel of this.#conversationChannels.values()) {
      socket.send(JSON.stringify(channel.command));
    }
    for (const channel of this.#vaultChangeChannels.values()) {
      socket.send(JSON.stringify(channel.command));
    }
    for (const channel of this.#runChannels.values()) {
      socket.send(JSON.stringify(channel.command));
    }
  }

  #acknowledgeEvent(socket: WebSocket, event: AgentRunEvent): void {
    if (!isDurableAgentRunEvent(event) || socket.readyState !== WebSocket.OPEN) return;
    const acknowledgement: DurableEventAck = {
      type: "event.ack",
      protocolVersion: PROTOCOL_VERSION,
      eventId: randomUUID(),
      acknowledgedEventId: event.eventId,
      conversationId: event.conversationId,
      agentRunId: event.agentRunId,
      sequence: event.sequence,
    };
    socket.send(JSON.stringify(acknowledgement));
  }

  async #executeLocalTool(
    socket: WebSocket,
    event: Extract<AgentRunEvent, { type: "tool_call.requested" }>,
  ): Promise<void> {
    let result: LocalToolResultPayload;
    try {
      result = this.#toolExecutor
        ? await this.#toolExecutor.execute(event)
        : {
            ok: false,
            error: {
              code: "plugin_disconnected",
              message: "The connected Obsidian plugin has no Vault tool executor.",
            },
          };
    } catch (error) {
      result = {
        ok: false,
        error: {
          code: "tool_error",
          message: error instanceof Error ? error.message : "The Vault tool failed.",
        },
      };
    }
    if (this.#eventSocket !== socket || socket.readyState !== WebSocket.OPEN) return;
    const command: ToolResultCommand = {
      type: "tool_result",
      protocolVersion: PROTOCOL_VERSION,
      eventId: randomUUID(),
      conversationId: event.conversationId,
      agentRunId: event.agentRunId,
      sequence: event.sequence,
      toolCallId: event.toolCallId,
      result,
    };
    socket.send(JSON.stringify(command));
  }

  #reconcileConversationEvents(conversationId: string): void {
    this.#reconciledConversations.add(conversationId);
    const socket = this.#eventSocket;
    if (!socket) return;
    for (const [eventId, event] of this.#pendingReplayEvents) {
      if (event.conversationId !== conversationId) continue;
      this.#pendingReplayEvents.delete(eventId);
      rememberEventId(this.#seenEventIds, eventId);
      rememberEventId(this.#processedEventIds, eventId);
      this.#acknowledgeEvent(socket, event);
    }
  }

  #handleEventSocketFailure(child: RuntimeChild, socket: WebSocket, error: Error): void {
    if (this.#eventSocket !== socket) return;
    this.#eventSocket = undefined;
    for (const channel of this.#runChannels.values()) {
      channel.allowReplaySequenceGaps = true;
    }
    if (socket.readyState === WebSocket.OPEN || socket.readyState === WebSocket.CONNECTING) {
      socket.close();
    }
    if (this.#stopping || this.#reconnectPromise) return;
    const connection = this.#connection;
    if (!connection || this.#child !== child) {
      this.#failRunChannels(error);
      return;
    }
    this.#reconnectPromise = (async () => {
      let lastError = error;
      for (let attempt = 1; attempt <= 5; attempt += 1) {
        await new Promise<void>((resolve) => {
          const timeout = setTimeout(resolve, attempt * 100);
          unrefTimer(timeout);
        });
        if (this.#stopping || this.#child !== child) return;
        try {
          await this.#openEventSocket(child, connection);
          return;
        } catch (reconnectError) {
          lastError = reconnectError instanceof Error ? reconnectError : lastError;
        }
      }
      this.#failRunChannels(lastError);
      this.#markUnavailable(
        child,
        "OfferAgent Runtime event connection could not be re-established after five attempts.",
      );
    })().finally(() => {
      this.#reconnectPromise = undefined;
    });
  }

  #closeEventSocket(error: Error): void {
    const socket = this.#eventSocket;
    this.#eventSocket = undefined;
    this.#failRunChannels(error);
    if (
      socket &&
      (socket.readyState === WebSocket.OPEN || socket.readyState === WebSocket.CONNECTING)
    ) {
      socket.close();
    }
  }

  #failRunChannels(error: Error): void {
    for (const channel of this.#runChannels.values()) {
      channel.failure = error;
      channel.wake?.();
      channel.wake = undefined;
    }
    for (const [requestId, channel] of this.#conversationChannels) {
      clearTimeout(channel.timeout);
      channel.reject(error);
      this.#conversationChannels.delete(requestId);
    }
    for (const [requestId, channel] of this.#vaultChangeChannels) {
      clearTimeout(channel.timeout);
      channel.reject(error);
      this.#vaultChangeChannels.delete(requestId);
    }
  }

  #requestVaultChange(
    input:
      | { type: "vault_changes.list"; states: VaultChangeTransactionState[] }
      | ({ type: "vault_changes.applying" } & VaultChangeApplyingRequest)
      | ({ type: "vault_changes.state" } & VaultChangeStateRequest),
  ): Promise<VaultChangeEvent> {
    const socket = this.#eventSocket;
    if (!socket || socket.readyState !== WebSocket.OPEN) {
      return Promise.reject(new Error("OfferAgent Runtime event connection is unavailable."));
    }
    const requestId = randomUUID();
    const command = {
      ...input,
      protocolVersion: PROTOCOL_VERSION,
      eventId: randomUUID(),
      conversationId: "vault-change-management",
      agentRunId: requestId,
      sequence: 0,
    } as VaultChangeCommand;
    return new Promise<VaultChangeEvent>((resolve, reject) => {
      const timeout = setTimeout(() => {
        this.#vaultChangeChannels.delete(requestId);
        reject(new Error("OfferAgent Runtime Vault Change request timed out."));
      }, 5_000);
      unrefTimer(timeout);
      this.#vaultChangeChannels.set(requestId, {
        command,
        conversationId: command.conversationId,
        expectedSequence: command.sequence + 1,
        reject,
        requestEventId: command.eventId,
        resolve,
        timeout,
      });
      try {
        socket.send(JSON.stringify(command));
      } catch (error) {
        clearTimeout(timeout);
        this.#vaultChangeChannels.delete(requestId);
        reject(error);
      }
    });
  }

  #requestConversation(
    input:
      | { type: "conversation.create"; conversationId: string; title: string; model: string }
      | { type: "conversation.update"; conversationId: string; model: string }
      | { type: "conversation.delete" | "conversation.list" | "conversation.open"; conversationId: string },
  ): Promise<ConversationEvent> {
    const socket = this.#eventSocket;
    if (!socket || socket.readyState !== WebSocket.OPEN) {
      return Promise.reject(new Error("OfferAgent Runtime event connection is unavailable."));
    }
    const requestId = randomUUID();
    const command = {
      ...input,
      protocolVersion: PROTOCOL_VERSION,
      eventId: randomUUID(),
      agentRunId: requestId,
      sequence: 0,
    } as ConversationCommand;
    return new Promise<ConversationEvent>((resolve, reject) => {
      const timeout = setTimeout(() => {
        this.#conversationChannels.delete(requestId);
        reject(new Error("OfferAgent Runtime Conversation request timed out."));
      }, 5_000);
      unrefTimer(timeout);
      this.#conversationChannels.set(requestId, {
        command,
        conversationId: command.conversationId,
        expectedSequence: command.sequence + 1,
        reject,
        requestEventId: command.eventId,
        resolve,
        timeout,
      });
      try {
        socket.send(JSON.stringify(command));
      } catch (error) {
        clearTimeout(timeout);
        this.#conversationChannels.delete(requestId);
        reject(error);
      }
    });
  }

  #requiredConnection(): RuntimeConnection {
    if (!this.#connection) throw new Error("OfferAgent Runtime is not connected.");
    return this.#connection;
  }

  #startHealthMonitor(child: RuntimeChild, connection: RuntimeConnection): void {
    this.#clearHealthMonitor();
    this.#healthTimer = setInterval(() => {
      if (this.#healthCheckInFlight || this.#child !== child) return;
      this.#healthCheckInFlight = true;
      void callRuntime<RuntimeHealth>(connection, "GET", "/health")
        .then((health) => {
          if (
            health.status !== "healthy" ||
            health.instanceId !== connection.instanceId ||
            health.protocolVersion !== PROTOCOL_VERSION
          ) {
            throw new Error("Runtime health response changed unexpectedly.");
          }
        })
        .catch(() => {
          this.#markUnavailable(
            child,
            "OfferAgent Runtime health checks failed. Restart the plugin to reconnect.",
          );
        })
        .finally(() => {
          this.#healthCheckInFlight = false;
        });
    }, 1_000);
    unrefTimer(this.#healthTimer);
  }

  #handleExit(
    child: RuntimeChild,
    code: number | null,
    signal: NodeJS.Signals | null,
  ): void {
    if (this.#child !== child) return;
    const wasConnected = Boolean(this.#connection);
    this.#child = undefined;
    this.#connection = undefined;
    this.#clearHealthMonitor();
    this.#closeEventSocket(new Error("OfferAgent Runtime stopped unexpectedly."));
    if (!wasConnected || this.#stopping) return;

    const reason = signal ? `signal ${signal}` : `exit code ${code ?? "unknown"}`;
    this.#notifyUnavailable(`OfferAgent Runtime stopped unexpectedly (${reason}).`);
  }

  #markUnavailable(child: RuntimeChild, message: string): void {
    if (this.#child !== child) return;
    this.#child = undefined;
    this.#connection = undefined;
    this.#clearHealthMonitor();
    this.#closeEventSocket(new Error(message));
    if (child.exitCode === null) child.kill();
    this.#notifyUnavailable(message);
  }

  #clearHealthMonitor(): void {
    if (this.#healthTimer) clearInterval(this.#healthTimer);
    this.#healthTimer = undefined;
    this.#healthCheckInFlight = false;
  }

  #notifyUnavailable(message: string): void {
    for (const subscriber of this.#unavailableSubscribers) subscriber(message);
  }
}
