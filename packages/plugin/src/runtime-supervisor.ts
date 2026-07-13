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
  type AgentRunStart,
  type ModelDescriptor,
  type ProviderErrorCode,
  type RuntimeError,
  type RuntimeHandshake,
  type RuntimeHealth,
  type RuntimeModels,
  type RuntimeShutdown,
} from "@offeragent/protocol";

const NODE_DIAGNOSTIC =
  "OfferAgent could not find Node.js 20 or newer. Install Node.js and restart Obsidian, or set OFFERAGENT_NODE_PATH to node.exe.";

export interface RuntimeSupervisorOptions {
  nodeCandidates?: string[];
  parentPid?: number;
  provider?: "codex" | "fake";
  runtimePath: string;
  startupTimeoutMs?: number;
}

export interface AgentRunRequest {
  agentRunId: string;
  conversationId: string;
  input: string;
  model: string;
}

export interface RuntimeClient {
  listModels(): Promise<ModelDescriptor[]>;
  onUnavailable(subscriber: UnavailableSubscriber): () => void;
  runAgent(request: AgentRunRequest): AsyncIterable<AgentRunEvent>;
  start(): Promise<RuntimeHandshake>;
  stop(): Promise<void>;
}

type UnavailableSubscriber = (message: string) => void;

interface RuntimeConnection extends RuntimeHandshake {
  token: string;
}

type RuntimeChild = ChildProcessByStdio<null, Readable, Readable>;

interface RunChannel {
  events: AgentRunEvent[];
  failure?: Error;
  wake?: () => void;
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
): Promise<T> {
  return new Promise((resolve, reject) => {
    const outgoing = request(
      {
        host: "127.0.0.1",
        port: connection.port,
        method,
        path: pathname,
        headers: { authorization: `Bearer ${connection.token}` },
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
    outgoing.end();
  });
}

export class RuntimeSupervisor implements RuntimeClient {
  readonly #options: Required<
    Pick<RuntimeSupervisorOptions, "parentPid" | "provider" | "runtimePath" | "startupTimeoutMs">
  > & { nodeCandidates: string[] };
  readonly #unavailableSubscribers = new Set<UnavailableSubscriber>();
  #child?: RuntimeChild;
  #connection?: RuntimeConnection;
  #eventSocket?: WebSocket;
  #healthCheckInFlight = false;
  #healthTimer?: NodeJS.Timeout;
  readonly #runChannels = new Map<string, RunChannel>();
  #stopping = false;

  constructor(options: RuntimeSupervisorOptions) {
    this.#options = {
      nodeCandidates: options.nodeCandidates ?? defaultNodeCandidates(process.env),
      parentPid: options.parentPid ?? process.pid,
      provider:
        options.provider ??
        (process.env.OFFERAGENT_RUNTIME_PROVIDER === "fake" ? "fake" : "codex"),
      runtimePath: options.runtimePath,
      startupTimeoutMs: options.startupTimeoutMs ?? 10_000,
    };
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
    const child = spawn(
      executable,
      [
        this.#options.runtimePath,
        "--port",
        "0",
        "--token",
        token,
        "--parent-pid",
        `${this.#options.parentPid}`,
        "--provider",
        this.#options.provider,
      ],
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

  async *runAgent(request: AgentRunRequest): AsyncIterable<AgentRunEvent> {
    this.#requiredConnection();
    const socket = this.#eventSocket;
    if (!socket || socket.readyState !== WebSocket.OPEN) {
      throw new Error("OfferAgent Runtime event connection is unavailable.");
    }
    if (this.#runChannels.has(request.agentRunId)) {
      throw new Error(`Agent Run '${request.agentRunId}' is already active.`);
    }
    const channel: RunChannel = { events: [] };
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
        yield event;
        if (event.type === "agent_run.completed" || event.type === "agent_run.failed") {
          terminal = true;
          return;
        }
      }
    } finally {
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

    this.#eventSocket = socket;
    socket.on("message", (data) => {
      let event: AgentRunEvent;
      try {
        event = JSON.parse(data.toString("utf8")) as AgentRunEvent;
        if (typeof event.agentRunId !== "string") throw new Error("Agent Run id is missing.");
      } catch (error) {
        this.#handleEventSocketFailure(
          child,
          socket,
          new Error("OfferAgent Runtime returned an invalid Agent Run event.", { cause: error }),
        );
        return;
      }
      const channel = this.#runChannels.get(event.agentRunId);
      if (!channel) return;
      channel.events.push(event);
      channel.wake?.();
      channel.wake = undefined;
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
