import assert from "node:assert/strict";
import { mkdtemp, rm, writeFile } from "node:fs/promises";
import { once } from "node:events";
import { spawn } from "node:child_process";
import { createServer } from "node:http";
import { fileURLToPath, pathToFileURL } from "node:url";
import os from "node:os";
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
const controllerModule = path.join(
  repositoryRoot,
  "packages",
  "plugin",
  "dist",
  "sidebar-controller.js",
);

async function loadControllerModule() {
  return import(pathToFileURL(controllerModule));
}

function waitForRuntimeState(controller, expectedState, timeoutMs = 5_000) {
  return new Promise((resolve, reject) => {
    let unsubscribe = () => {};
    const timeout = setTimeout(() => {
      unsubscribe();
      reject(new Error(`Timed out waiting for Runtime state ${expectedState}`));
    }, timeoutMs);
    unsubscribe = controller.subscribe((viewModel) => {
      if (viewModel.runtime.state !== expectedState) return;
      clearTimeout(timeout);
      unsubscribe();
      resolve(viewModel.runtime);
    });
  });
}

test("the sidebar controller starts and stops the bundled Runtime", async (t) => {
  const { RuntimeSupervisor, SidebarController } = await loadControllerModule();
  const supervisor = new RuntimeSupervisor({
    nodeCandidates: [process.execPath],
    parentPid: process.pid,
    provider: "fake",
    runtimePath: runtimeEntry,
  });
  const controller = new SidebarController(supervisor);
  const observedStates = [];
  const unsubscribe = controller.subscribe((viewModel) => {
    observedStates.push(viewModel.runtime.state);
  });

  t.after(async () => {
    unsubscribe();
    await controller.stop();
  });

  await controller.start();
  assert.equal(controller.getViewModel().runtime.state, "connected");
  assert.ok(observedStates.indexOf("starting") < observedStates.indexOf("connected"));

  await controller.stop();
  assert.equal(controller.getViewModel().runtime.state, "idle");
});

test("the bundled Runtime uses the persisted user proxy when Obsidian has stale environment", async (t) => {
  const { RuntimeSupervisor } = await loadControllerModule();
  const temporaryDirectory = await mkdtemp(path.join(os.tmpdir(), "offeragent-user-proxy-"));
  const authPath = path.join(temporaryDirectory, "auth.json");
  await writeFile(authPath, JSON.stringify({ tokens: { access_token: "user-proxy-token" } }), "utf8");

  const proxy = createServer();
  proxy.on("connect", (request, socket) => {
    if (request.url !== "offeragent.invalid:80") {
      socket.end("HTTP/1.1 502 Bad Gateway\r\n\r\n");
      return;
    }
    socket.write("HTTP/1.1 200 Connection Established\r\n\r\n");
    socket.once("data", () => {
      const body = JSON.stringify({
        models: [{ slug: "proxy-model", display_name: "Proxy Model" }],
      });
      socket.end(
        `HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nContent-Length: ${Buffer.byteLength(body)}\r\nConnection: close\r\n\r\n${body}`,
      );
    });
  });
  await new Promise((resolve) => proxy.listen(0, "127.0.0.1", resolve));
  const proxyUrl = `http://127.0.0.1:${proxy.address().port}`;

  const previousEnvironment = Object.fromEntries(
    [
      "ALL_PROXY",
      "HTTP_PROXY",
      "HTTPS_PROXY",
      "NO_PROXY",
      "OFFERAGENT_CODEX_AUTH_FILE",
      "OFFERAGENT_CODEX_BASE_URL",
    ].map((name) => [name, process.env[name]]),
  );
  for (const name of ["ALL_PROXY", "HTTP_PROXY", "HTTPS_PROXY", "NO_PROXY"]) {
    delete process.env[name];
  }
  process.env.OFFERAGENT_CODEX_AUTH_FILE = authPath;
  process.env.OFFERAGENT_CODEX_BASE_URL = "http://offeragent.invalid";

  const supervisor = new RuntimeSupervisor({
    loadUserProxyEnvironment: async () => ({
      ALL_PROXY: proxyUrl,
      HTTP_PROXY: proxyUrl,
      HTTPS_PROXY: proxyUrl,
      NO_PROXY: "localhost,127.0.0.1",
    }),
    nodeCandidates: [process.execPath],
    parentPid: process.pid,
    provider: "codex",
    runtimePath: runtimeEntry,
    statePath: path.join(temporaryDirectory, "state.db"),
  });
  t.after(async () => {
    await supervisor.stop();
    await new Promise((resolve) => proxy.close(resolve));
    await rm(temporaryDirectory, { recursive: true, force: true });
    for (const [name, value] of Object.entries(previousEnvironment)) {
      if (value === undefined) delete process.env[name];
      else process.env[name] = value;
    }
  });

  await supervisor.start();
  assert.deepEqual(await supervisor.listModels(), [
    { id: "proxy-model", label: "Proxy Model" },
  ]);
});

test("the sidebar explains how to repair a missing Node.js installation", async () => {
  const { RuntimeSupervisor, SidebarController } = await loadControllerModule();
  const supervisor = new RuntimeSupervisor({
    nodeCandidates: [path.join(repositoryRoot, "missing-node", "node.exe")],
    parentPid: process.pid,
    runtimePath: runtimeEntry,
  });
  const controller = new SidebarController(supervisor);

  await assert.rejects(controller.start(), /Node\.js 20 or newer/);
  assert.deepEqual(controller.getViewModel().runtime, {
    state: "unavailable",
    message:
      "OfferAgent could not find Node.js 20 or newer. Install Node.js and restart Obsidian, or set OFFERAGENT_NODE_PATH to node.exe.",
  });
});

test("the sidebar gives the same repair guidance for an unusable Node.js executable", async (t) => {
  const { RuntimeSupervisor, SidebarController } = await loadControllerModule();
  const temporaryDirectory = await mkdtemp(path.join(os.tmpdir(), "offeragent-node-"));
  const unusableNode = path.join(
    temporaryDirectory,
    process.platform === "win32" ? "node.exe" : "node",
  );
  await writeFile(unusableNode, "not an executable", "utf8");
  t.after(() => rm(temporaryDirectory, { recursive: true, force: true }));

  const controller = new SidebarController(
    new RuntimeSupervisor({
      nodeCandidates: [unusableNode],
      parentPid: process.pid,
      runtimePath: runtimeEntry,
    }),
  );

  await assert.rejects(controller.start(), /Node\.js 20 or newer/);
  assert.match(controller.getViewModel().runtime.message, /Install Node\.js/);
});

test("the sidebar becomes unavailable when a connected Runtime exits", async (t) => {
  const { RuntimeSupervisor, SidebarController } = await loadControllerModule();
  const temporaryParent = spawn(
    process.execPath,
    ["-e", "setTimeout(() => process.exit(0), 1000)"],
    { stdio: "ignore", windowsHide: true },
  );
  await once(temporaryParent, "spawn");

  const controller = new SidebarController(
    new RuntimeSupervisor({
      nodeCandidates: [process.execPath],
      parentPid: temporaryParent.pid,
      provider: "fake",
      runtimePath: runtimeEntry,
    }),
  );
  t.after(async () => {
    if (temporaryParent.exitCode === null) temporaryParent.kill();
    await controller.stop();
  });

  await controller.start();
  assert.equal(controller.getViewModel().runtime.state, "connected");

  const unavailable = await waitForRuntimeState(controller, "unavailable");
  assert.match(
    unavailable.message,
    /Runtime (?:event connection failed|stopped unexpectedly|health checks failed)/,
  );
});
