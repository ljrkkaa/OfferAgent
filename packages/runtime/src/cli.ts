import { randomUUID } from "node:crypto";
import { createServer, type ServerResponse } from "node:http";
import type { AddressInfo } from "node:net";
import { WebSocketServer, type WebSocket } from "ws";
import {
  PROTOCOL_VERSION,
  type AgentRunCancel,
  type AgentRunEvent,
  type AgentRunStart,
  type RuntimeError,
  type RuntimeHandshake,
  type RuntimeHealth,
  type RuntimeModels,
  type RuntimeShutdown,
} from "@offeragent/protocol";
import { FakeModelProvider } from "./fake-model-provider";
import { CodexSubscriptionProvider } from "./codex-subscription-provider";
import { asModelProviderError, type ModelProvider } from "./model-provider";

interface RuntimeOptions {
  parentPid: number;
  port: number;
  provider: "codex" | "fake";
  token: string;
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
  return {
    parentPid: parseIntegerOption("--parent-pid"),
    port: parseIntegerOption("--port", true),
    provider,
    token: readOption("--token"),
  };
}

function sendJson(
  response: ServerResponse,
  statusCode: number,
  body: RuntimeError | RuntimeHealth | RuntimeModels | RuntimeShutdown,
): void {
  response.writeHead(statusCode, {
    "content-type": "application/json; charset=utf-8",
    "cache-control": "no-store",
  });
  response.end(JSON.stringify(body));
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

function startRuntime({ parentPid, port, provider: providerName, token }: RuntimeOptions): void {
  const instanceId = randomUUID();
  const provider = createProvider(providerName);
  const activeRuns = new Map<
    string,
    { controller: AbortController; conversationId: string; socket: WebSocket }
  >();
  let exiting = false;

  const server = createServer((request, response) => {
    if (request.headers.authorization !== `Bearer ${token}`) {
      sendJson(response, 401, {
        code: "unauthorized",
        message: "A valid one-time Runtime token is required.",
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
    socket.once("close", () => {
      sockets.delete(socket);
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
      if (isAgentRunCancel(message)) {
        const run = activeRuns.get(message.agentRunId);
        if (
          run &&
          run.socket === socket &&
          run.conversationId === message.conversationId
        ) {
          run.controller.abort();
        }
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
        sendEvent(socket, {
          ...base,
          type: "agent_run.started",
          eventId: randomUUID(),
          sequence: sequence++,
          model: message.model,
        });
        try {
          for await (const delta of provider.stream({
            model: message.model,
            input: message.input.text,
            signal: controller.signal,
          })) {
            output += delta;
            sendEvent(socket, {
              ...base,
              type: "agent_run.delta",
              eventId: randomUUID(),
              sequence: sequence++,
              delta,
            });
          }
          sendEvent(socket, {
            ...base,
            type: "agent_run.completed",
            eventId: randomUUID(),
            sequence,
            output: { role: "assistant", text: output },
          });
        } catch (error) {
          const providerError = asModelProviderError(error);
          sendEvent(socket, {
            ...base,
            type: "agent_run.failed",
            eventId: randomUUID(),
            sequence,
            error: { code: providerError.code, message: providerError.message },
          });
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
    server.close(forceExit);
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
  startRuntime(readOptions());
} catch (error) {
  const message = error instanceof Error ? error.message : String(error);
  process.stderr.write(`OfferAgent Runtime failed: ${message}\n`);
  process.exitCode = 1;
}
