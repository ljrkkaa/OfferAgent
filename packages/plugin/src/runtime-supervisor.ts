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
  type AgentRunStart,
  type ConversationCommand,
  type ConversationEvent,
  type ConversationMessage,
  type ConversationSummary,
  type DurableEventAck,
  type LocalToolResultPayload,
  type ModelDescriptor,
  type ProviderErrorCode,
  type RuntimeError,
  type RuntimeHandshake,
  type RuntimeHealth,
  type RuntimeHostedWebSearchCapability,
  type RuntimeModels,
  type RuntimeShutdown,
  type ToolCallRecord,
  type ToolResultCommand,
  type VaultChangeApplyingRequest,
  type VaultChangeJournalRecord,
  type VaultChangeStateRequest,
  type VaultChangeTransactionState,
  type RuntimeVaultChangeBatches,
} from "@offeragent/protocol";

const NODE_DIAGNOSTIC =
  "OfferAgent could not find Node.js 20 or newer. Install Node.js and restart Obsidian, or set OFFERAGENT_NODE_PATH to node.exe.";

export interface RuntimeSupervisorOptions {
  nodeCandidates?: string[];
  parentPid?: number;
  provider?: "codex" | "fake";
  runtimePath: string;
  statePath?: string;
  startupTimeoutMs?: number;
  toolExecutor?: LocalToolExecutor;
}

export interface LocalToolExecutor {
  execute(
    call: Extract<AgentRunEvent, { type: "tool_call.requested" }>,
  ): Promise<LocalToolResultPayload>;
}

export interface AgentRunRequest {
  agentRunId: string;
  conversationId: string;
  input: string;
  model: string;
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
  listModels(): Promise<ModelDescriptor[]>;
  listConversations(): Promise<ConversationSummary[]>;
  onUnavailable(subscriber: UnavailableSubscriber): () => void;
  openConversation(conversationId: string): Promise<ConversationSnapshot>;
  runAgent(request: AgentRunRequest): AsyncIterable<AgentRunEvent>;
  start(): Promise<RuntimeHandshake>;
  stop(): Promise<void>;
  updateConversationModel(conversationId: string, modelId: string): Promise<ConversationSummary>;
}

type UnavailableSubscriber = (message: string) => void;

interface RuntimeConnection extends RuntimeHandshake {
  token: string;
}

type RuntimeChild = ChildProcessByStdio<null, Readable, Readable>;

interface RunChannel {
  conversationId: string;
  events: AgentRunEvent[];
  expectedSequence: number;
  failure?: Error;
  wake?: () => void;
}

interface ConversationChannel {
  conversationId: string;
  expectedSequence: number;
  reject: (error: Error) => void;
  requestEventId: string;
  resolve: (event: ConversationEvent) => void;
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

function isAgentRunEventEnvelope(value: unknown): value is AgentRunEvent {
  if (!value || typeof value !== "object") return false;
  const event = value as Partial<AgentRunEvent>;
  return (
    typeof event.type === "string" &&
    [
      "agent_run.started",
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
    Pick<RuntimeSupervisorOptions, "parentPid" | "provider" | "runtimePath" | "startupTimeoutMs">
  > & { nodeCandidates: string[]; statePath?: string };
  readonly #toolExecutor?: LocalToolExecutor;
  readonly #unavailableSubscribers = new Set<UnavailableSubscriber>();
  #child?: RuntimeChild;
  #connection?: RuntimeConnection;
  #eventSocket?: WebSocket;
  #healthCheckInFlight = false;
  #healthTimer?: NodeJS.Timeout;
  readonly #conversationChannels = new Map<string, ConversationChannel>();
  readonly #pendingReplayEvents = new Map<string, AgentRunEvent>();
  readonly #processedEventIds = new Set<string>();
  readonly #reconciledConversations = new Set<string>();
  readonly #runChannels = new Map<string, RunChannel>();
  readonly #seenEventIds = new Set<string>();
  #stopping = false;

  constructor(options: RuntimeSupervisorOptions) {
    this.#options = {
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
    const child = spawn(
      executable,
      arguments_,
      {
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
          setTimeout(() => {
            if (child.exitCode === null) child.kill();
            resolve();
          }, 3_000).unref();
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
    const connection = this.#requiredConnection();
    const response = await callRuntime<RuntimeVaultChangeBatches>(
      connection,
      "GET",
      `/vault-changes?states=${encodeURIComponent(states.join(","))}`,
    );
    return response.batches;
  }

  async markVaultChangeApplying(
    batchId: string,
    checkpointRef: string,
    targets: VaultChangeApplyingRequest["targets"],
  ): Promise<void> {
    const connection = this.#requiredConnection();
    const request: VaultChangeApplyingRequest = { batchId, checkpointRef, targets };
    await callRuntime(connection, "POST", "/vault-changes/applying", request);
  }

  async markVaultChangeState(
    batchId: string,
    state: VaultChangeStateRequest["state"],
  ): Promise<void> {
    const connection = this.#requiredConnection();
    const request: VaultChangeStateRequest = { batchId, state };
    await callRuntime(connection, "POST", "/vault-changes/state", request);
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
    this.#requiredConnection();
    const socket = this.#eventSocket;
    if (!socket || socket.readyState !== WebSocket.OPEN) {
      throw new Error("OfferAgent Runtime event connection is unavailable.");
    }
    if (this.#runChannels.has(request.agentRunId)) {
      throw new Error(`Agent Run '${request.agentRunId}' is already active.`);
    }
    const channel: RunChannel = {
      conversationId: request.conversationId,
      events: [],
      expectedSequence: 1,
    };
    this.#runChannels.set(request.agentRunId, channel);

    const start: AgentRunStart = {
      type: "agent_run.start",
      protocolVersion: PROTOCOL_VERSION,
      eventId: randomUUID(),
      conversationId: request.conversationId,
      agentRunId: request.agentRunId,
      sequence: 0,
      model: request.model,
      input: { role: "user", text: request.input },
    };
    try {
      socket.send(JSON.stringify(start));
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
          event.sequence !== expectedSequence
        ) {
          throw new Error("OfferAgent Runtime returned an out-of-sequence Agent Run event.");
        }
        expectedSequence += 1;
        lastDeliveredEvent = event;
        yield event;
        this.#processedEventIds.add(event.eventId);
        this.#acknowledgeEvent(socket, event);
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
        this.#processedEventIds.add(lastDeliveredEvent.eventId);
        this.#acknowledgeEvent(socket, lastDeliveredEvent);
        terminal = true;
      }
      this.#runChannels.delete(request.agentRunId);
      if (
        !terminal &&
        this.#eventSocket === socket &&
        socket.readyState === WebSocket.OPEN
      ) {
        const cancel: AgentRunCancel = {
          type: "agent_run.cancel",
          protocolVersion: PROTOCOL_VERSION,
          eventId: randomUUID(),
          conversationId: request.conversationId,
          agentRunId: request.agentRunId,
          sequence: expectedSequence,
        };
        socket.send(JSON.stringify(cancel));
      }
    }
  }

  async #openEventSocket(child: RuntimeChild, connection: RuntimeConnection): Promise<void> {
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

    this.#pendingReplayEvents.clear();
    this.#processedEventIds.clear();
    this.#reconciledConversations.clear();
    this.#seenEventIds.clear();
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
      const eventRecord = event as { agentRunId: string; type?: unknown };
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
        this.#seenEventIds.add(event.eventId);
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
      if (this.#seenEventIds.has(event.eventId)) {
        if (this.#processedEventIds.has(event.eventId)) this.#acknowledgeEvent(socket, event);
        return;
      }
      if (!channel) {
        this.#pendingReplayEvents.set(event.eventId, event);
        if (this.#reconciledConversations.has(event.conversationId)) {
          this.#processedEventIds.add(event.eventId);
          this.#seenEventIds.add(event.eventId);
          this.#pendingReplayEvents.delete(event.eventId);
          this.#acknowledgeEvent(socket, event);
        }
        return;
      }
      if (
        event.conversationId !== channel.conversationId ||
        event.sequence !== channel.expectedSequence
      ) {
        this.#handleEventSocketFailure(
          child,
          socket,
          new Error("OfferAgent Runtime returned an out-of-sequence Agent Run event."),
        );
        return;
      }
      channel.expectedSequence += 1;
      this.#seenEventIds.add(event.eventId);
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
  }

  #acknowledgeEvent(socket: WebSocket, event: AgentRunEvent): void {
    if (socket.readyState !== WebSocket.OPEN) return;
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
      this.#seenEventIds.add(eventId);
      this.#processedEventIds.add(eventId);
      this.#acknowledgeEvent(socket, event);
    }
  }

  #handleEventSocketFailure(child: RuntimeChild, socket: WebSocket, error: Error): void {
    if (this.#eventSocket !== socket) return;
    this.#eventSocket = undefined;
    this.#failRunChannels(error);
    if (socket.readyState === WebSocket.OPEN || socket.readyState === WebSocket.CONNECTING) {
      socket.close();
    }
    if (!this.#stopping) {
      this.#markUnavailable(
        child,
        "OfferAgent Runtime event connection failed. Restart the plugin to reconnect.",
      );
    }
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
      timeout.unref();
      this.#conversationChannels.set(requestId, {
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
    this.#healthTimer.unref();
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
