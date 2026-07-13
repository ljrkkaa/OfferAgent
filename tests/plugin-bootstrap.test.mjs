import assert from "node:assert/strict";
import { mkdtemp, rm, writeFile } from "node:fs/promises";
import { once } from "node:events";
import { spawn } from "node:child_process";
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
