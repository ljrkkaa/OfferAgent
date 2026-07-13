import { randomUUID } from "node:crypto";
import { execFile, spawn, type ChildProcessByStdio } from "node:child_process";
import { access } from "node:fs/promises";
import { request } from "node:http";
import path from "node:path";
import { delimiter } from "node:path";
import { once } from "node:events";
import type { Readable } from "node:stream";
import {
  PROTOCOL_VERSION,
  type RuntimeHandshake,
  type RuntimeHealth,
  type RuntimeShutdown,
} from "@offeragent/protocol";

const NODE_DIAGNOSTIC =
  "OfferAgent could not find Node.js 20 or newer. Install Node.js and restart Obsidian, or set OFFERAGENT_NODE_PATH to node.exe.";

export interface RuntimeSupervisorOptions {
  nodeCandidates?: string[];
  parentPid?: number;
  runtimePath: string;
  startupTimeoutMs?: number;
}

type UnavailableSubscriber = (message: string) => void;

interface RuntimeConnection extends RuntimeHandshake {
  token: string;
}

type RuntimeChild = ChildProcessByStdio<null, Readable, Readable>;

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
            reject(new Error(`OfferAgent Runtime request failed with status ${response.statusCode}.`));
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

export class RuntimeSupervisor {
  readonly #options: Required<
    Pick<RuntimeSupervisorOptions, "parentPid" | "runtimePath" | "startupTimeoutMs">
  > & { nodeCandidates: string[] };
  readonly #unavailableSubscribers = new Set<UnavailableSubscriber>();
  #child?: RuntimeChild;
  #connection?: RuntimeConnection;
  #healthCheckInFlight = false;
  #healthTimer?: NodeJS.Timeout;
  #stopping = false;

  constructor(options: RuntimeSupervisorOptions) {
    this.#options = {
      nodeCandidates: options.nodeCandidates ?? defaultNodeCandidates(process.env),
      parentPid: options.parentPid ?? process.pid,
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
      this.#connection = connection;
      this.#startHealthMonitor(child, connection);
      return handshake;
    } catch (error) {
      if (child.exitCode === null) child.kill();
      this.#child = undefined;
      throw error;
    }
  }

  async stop(): Promise<void> {
    const child = this.#child;
    const connection = this.#connection;
    this.#child = undefined;
    this.#connection = undefined;
    this.#clearHealthMonitor();
    if (!child || child.exitCode !== null) return;

    this.#stopping = true;
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
    if (!wasConnected || this.#stopping) return;

    const reason = signal ? `signal ${signal}` : `exit code ${code ?? "unknown"}`;
    this.#notifyUnavailable(`OfferAgent Runtime stopped unexpectedly (${reason}).`);
  }

  #markUnavailable(child: RuntimeChild, message: string): void {
    if (this.#child !== child) return;
    this.#child = undefined;
    this.#connection = undefined;
    this.#clearHealthMonitor();
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
