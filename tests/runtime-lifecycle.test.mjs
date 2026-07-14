import assert from "node:assert/strict";
import { once } from "node:events";
import { request } from "node:http";
import { spawn } from "node:child_process";
import { fileURLToPath } from "node:url";
import path from "node:path";
import test from "node:test";

const repositoryRoot = path.resolve(
  path.dirname(fileURLToPath(import.meta.url)),
  "..",
);
const runtimeEntry = path.join(
  repositoryRoot,
  "packages",
  "runtime",
  "dist",
  "cli.js",
);
const parentHelper = path.join(repositoryRoot, "tests", "runtime-parent-helper.mjs");

function readJsonLine(stream, timeoutMs = 5_000) {
  return new Promise((resolve, reject) => {
    let buffer = "";
    const timeout = setTimeout(() => {
      cleanup();
      reject(new Error("Timed out waiting for Runtime handshake"));
    }, timeoutMs);

    const cleanup = () => {
      clearTimeout(timeout);
      stream.off("data", onData);
      stream.off("error", onError);
    };

    const onError = (error) => {
      cleanup();
      reject(error);
    };

    const onData = (chunk) => {
      buffer += chunk.toString("utf8");
      const newline = buffer.indexOf("\n");
      if (newline === -1) return;

      cleanup();
      try {
        resolve(JSON.parse(buffer.slice(0, newline)));
      } catch (error) {
        reject(error);
      }
    };

    stream.on("data", onData);
    stream.on("error", onError);
  });
}

function callRuntime({ port, token, method = "GET", pathname }) {
  return new Promise((resolve, reject) => {
    const outgoing = request(
      {
        host: "127.0.0.1",
        port,
        path: pathname,
        method,
        headers: { authorization: `Bearer ${token}` },
      },
      (response) => {
        let body = "";
        response.setEncoding("utf8");
        response.on("data", (chunk) => {
          body += chunk;
        });
        response.on("end", () => {
          resolve({
            statusCode: response.statusCode,
            body: JSON.parse(body),
          });
        });
      },
    );
    outgoing.on("error", reject);
    outgoing.end();
  });
}

test("the bundled Runtime reports health and exits gracefully", async (t) => {
  const token = "runtime-smoke-test-token";
  const runtime = spawn(
    process.execPath,
    [runtimeEntry, "--port", "0", "--token", token, "--parent-pid", `${process.pid}`],
    { stdio: ["ignore", "pipe", "pipe"], windowsHide: true },
  );

  t.after(() => {
    if (runtime.exitCode === null) runtime.kill();
  });

  const handshake = await readJsonLine(runtime.stdout);
  assert.deepEqual(Object.keys(handshake).sort(), [
    "instanceId",
    "pid",
    "port",
    "protocolVersion",
  ]);
  assert.equal(handshake.protocolVersion, 1);
  assert.equal(handshake.pid, runtime.pid);
  assert.ok(Number.isInteger(handshake.port) && handshake.port > 0);

  const health = await callRuntime({
    port: handshake.port,
    token,
    pathname: "/health",
  });
  assert.equal(health.statusCode, 200);
  assert.deepEqual(health.body, {
    status: "healthy",
    instanceId: handshake.instanceId,
    protocolVersion: 1,
  });

  const exited = once(runtime, "exit");
  const shutdown = await callRuntime({
    port: handshake.port,
    token,
    method: "POST",
    pathname: "/shutdown",
  });
  assert.equal(shutdown.statusCode, 202);
  assert.deepEqual(shutdown.body, { status: "shutting_down" });
  assert.deepEqual(await exited, [0, null]);
});

test("the bundled Runtime exits after its plugin parent disappears", async (t) => {
  const helper = spawn(
    process.execPath,
    [parentHelper, runtimeEntry, "parent-loss-smoke-test-token"],
    { stdio: ["ignore", "pipe", "pipe"], windowsHide: true },
  );
  const helperExited = once(helper, "exit");
  const handshake = await readJsonLine(helper.stdout);

  t.after(() => {
    try {
      process.kill(handshake.pid);
    } catch {
      // The expected outcome is that the Runtime has already exited.
    }
  });

  assert.deepEqual(await helperExited, [0, null]);

  const deadline = Date.now() + 5_000;
  while (Date.now() < deadline) {
    try {
      process.kill(handshake.pid, 0);
    } catch {
      return;
    }
    await new Promise((resolve) => setTimeout(resolve, 100));
  }

  assert.fail(`Runtime process ${handshake.pid} survived after its parent exited`);
});
