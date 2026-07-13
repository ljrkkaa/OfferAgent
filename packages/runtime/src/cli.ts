import { randomUUID } from "node:crypto";
import { createServer, type ServerResponse } from "node:http";
import type { AddressInfo } from "node:net";
import {
  PROTOCOL_VERSION,
  type RuntimeError,
  type RuntimeHandshake,
  type RuntimeHealth,
  type RuntimeShutdown,
} from "@offeragent/protocol";

interface RuntimeOptions {
  parentPid: number;
  port: number;
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
  return {
    parentPid: parseIntegerOption("--parent-pid"),
    port: parseIntegerOption("--port", true),
    token: readOption("--token"),
  };
}

function sendJson(
  response: ServerResponse,
  statusCode: number,
  body: RuntimeError | RuntimeHealth | RuntimeShutdown,
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

function startRuntime({ parentPid, port, token }: RuntimeOptions): void {
  const instanceId = randomUUID();
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
