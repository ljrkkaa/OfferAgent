import assert from "node:assert/strict";
import { mkdir, mkdtemp, rm, symlink, writeFile } from "node:fs/promises";
import { createRequire } from "node:module";
import Module from "node:module";
import os from "node:os";
import path from "node:path";
import test from "node:test";
import { fileURLToPath } from "node:url";

const repositoryRoot = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..");
const require = createRequire(import.meta.url);
const modulePath = path.join(
  repositoryRoot,
  "packages",
  "plugin",
  "dist",
  "project-evidence-adapter.js",
);
const stateModulePath = path.join(repositoryRoot, "packages", "runtime", "dist", "state-store.js");
const originalLoad = Module._load;
Module._load = function loadWithObsidianStub(request, parent, isMain) {
  if (request === "obsidian") return {};
  return originalLoad.call(this, request, parent, isMain);
};
let ProjectEvidenceAdapter;
let RuntimeStateStore;
try {
  ({ ProjectEvidenceAdapter } = require(modulePath));
  ({ RuntimeStateStore } = require(stateModulePath));
} finally {
  Module._load = originalLoad;
}

function vaultFile(pathname, content, mtime = 1234) {
  return {
    path: pathname,
    extension: pathname.split(".").at(-1),
    stat: { mtime, size: Buffer.byteLength(content, "utf8") },
    content,
  };
}

function toolCall(arguments_) {
  const { action, ...toolArguments } = arguments_;
  return {
    type: "tool_call.requested",
    protocolVersion: 1,
    eventId: "project-event",
    conversationId: "conversation",
    agentRunId: "run",
    sequence: 2,
    toolCallId: "project-call",
    tool: { kind: "local", name: `project_${action}`, arguments: toolArguments },
  };
}

function adapter(root, { registered = true } = {}) {
  const descriptor = vaultFile(
    "projects/offeragent.md",
    `---\nproject-id: offeragent\nproject-root: ${root}\n---\n# OfferAgent\n`,
  );
  const files = [
    vaultFile(
      "projects/index.md",
      registered ? "# Project Registry\n\n- [[projects/offeragent|OfferAgent]]\n" : "# Project Registry\n",
    ),
    descriptor,
  ];
  return new ProjectEvidenceAdapter({
    getFiles: () => files,
    cachedRead: async (target) => target.content,
  });
}

test("Project Registry exclusively authorizes bounded project list, search, and read", async (t) => {
  const root = await mkdtemp(path.join(os.tmpdir(), "offeragent-project-evidence-"));
  t.after(() => rm(root, { recursive: true, force: true }));
  await mkdir(path.join(root, "src"), { recursive: true });
  await writeFile(
    path.join(root, "src", "cache.ts"),
    "export function invalidate(version: number) { return `cache:${version}`; }\n",
  );
  await writeFile(path.join(root, "README.md"), "# OfferAgent\n\nA local interview assistant.\n");

  const subject = adapter(root);
  const listed = await subject.execute(toolCall({ action: "list", projectId: "offeragent" }));
  assert.equal(listed.ok, true);
  assert.equal(listed.value.type, "project_list");
  assert.deepEqual(listed.value.entries.map(({ path }) => path), ["README.md", "src/cache.ts"]);
  assert.equal(JSON.stringify(listed).includes(root), false);

  const searched = await subject.execute(toolCall({
    action: "search",
    projectId: "offeragent",
    query: "invalidate version",
  }));
  assert.equal(searched.ok, true);
  assert.equal(searched.value.entries[0].path, "src/cache.ts");
  assert.match(searched.value.entries[0].snippets[0].content, /invalidate/);

  const read = await subject.execute(toolCall({
    action: "read",
    projectId: "offeragent",
    path: "src/cache.ts",
    lineStart: 1,
    lineEnd: 1,
  }));
  assert.equal(read.ok, true);
  assert.deepEqual(
    {
      type: read.value.type,
      projectId: read.value.projectId,
      path: read.value.path,
      evidencePath: read.value.evidencePath,
      content: read.value.content,
    },
    {
      type: "project_read",
      projectId: "offeragent",
      path: "src/cache.ts",
      evidencePath: "project/offeragent/src/cache.ts",
      content: "export function invalidate(version: number) { return `cache:${version}`; }",
    },
  );
  assert.match(read.value.contentHash, /^sha256:[a-f0-9]{64}$/);

  const unregistered = await adapter(root, { registered: false }).execute(
    toolCall({ action: "read", projectId: "offeragent", path: "README.md" }),
  );
  assert.deepEqual(unregistered, {
    ok: false,
    error: { code: "permission_denied", message: "Project 'offeragent' is not registered." },
  });
});

test("Project Evidence excludes secrets, internals, dependencies, generated output, binaries, oversized files, and escapes", async (t) => {
  const root = await mkdtemp(path.join(os.tmpdir(), "offeragent-project-exclusions-"));
  const outside = await mkdtemp(path.join(os.tmpdir(), "offeragent-project-outside-"));
  t.after(async () => {
    await rm(root, { recursive: true, force: true });
    await rm(outside, { recursive: true, force: true });
  });
  const files = {
    "src/main.ts": "export const safe = true;\n",
    ".env": "TOKEN=secret\n",
    "config/credentials.json": "{\"token\":\"secret\"}\n",
    ".git/config": "secret remote\n",
    ".git/leak.json": "{\"secret\":true}\n",
    "node_modules/pkg/index.js": "dependency\n",
    "dist/bundle.js": "generated\n",
    "assets/logo.png": "\u0000PNG",
    "src/huge.ts": "x".repeat(1_048_577),
    "src/client.generated.ts": "generated client\n",
    "package-lock.json": "{\"lockfileVersion\":3}\n",
  };
  for (const [relative, content] of Object.entries(files)) {
    const target = path.join(root, relative);
    await mkdir(path.dirname(target), { recursive: true });
    await writeFile(target, content);
  }
  await writeFile(path.join(outside, "escape.ts"), "export const stolen = true;\n");
  let linkedSecret = false;
  let linkedSecretDirectory = false;
  let linkedEligible = false;
  let linkedEligibleDirectory = false;
  try {
    await symlink(path.join(outside, "escape.ts"), path.join(root, "src", "escape.ts"));
    await symlink(path.join(root, "config", "credentials.json"), path.join(root, "src", "config.json"));
    linkedSecret = true;
  } catch (error) {
    if (process.platform !== "win32") throw error;
  }
  try {
    await symlink(path.join(root, "src", "main.ts"), path.join(root, "src", "alias.ts"));
    linkedEligible = true;
  } catch (error) {
    if (process.platform !== "win32") throw error;
  }
  try {
    await symlink(
      path.join(root, ".git"),
      path.join(root, "safe-config"),
      process.platform === "win32" ? "junction" : "dir",
    );
    linkedSecretDirectory = true;
  } catch (error) {
    if (process.platform !== "win32") throw error;
  }
  try {
    await symlink(
      path.join(root, "src"),
      path.join(root, "source-alias"),
      process.platform === "win32" ? "junction" : "dir",
    );
    linkedEligibleDirectory = true;
  } catch (error) {
    if (process.platform !== "win32") throw error;
  }

  const subject = adapter(root);
  const listed = await subject.execute(toolCall({ action: "list", projectId: "offeragent" }));
  assert.equal(listed.ok, true);
  assert.deepEqual(listed.value.entries.map(({ path }) => path), ["src/main.ts"]);
  const excludedDirectory = await subject.execute(toolCall({
    action: "list", projectId: "offeragent", directory: ".git",
  }));
  assert.equal(excludedDirectory.ok, false);
  assert.equal(excludedDirectory.error.code, "permission_denied");
  if (linkedSecretDirectory) {
    const aliasedDirectory = await subject.execute(toolCall({
      action: "list", projectId: "offeragent", directory: "safe-config",
    }));
    assert.equal(aliasedDirectory.ok, false);
    assert.equal(aliasedDirectory.error.code, "permission_denied");
  }
  if (linkedEligibleDirectory) {
    const aliasedDirectory = await subject.execute(toolCall({
      action: "list", projectId: "offeragent", directory: "source-alias",
    }));
    assert.equal(aliasedDirectory.ok, false);
    assert.equal(aliasedDirectory.error.code, "permission_denied");
  }

  for (const candidate of [
    ".env",
    "config/credentials.json",
    ".git/config",
    "node_modules/pkg/index.js",
    "dist/bundle.js",
    "assets/logo.png",
    "src/huge.ts",
    "src/client.generated.ts",
    "package-lock.json",
    "../escape.ts",
    "src/escape.ts",
    ...(linkedSecret ? ["src/config.json"] : []),
    ...(linkedEligible ? ["src/alias.ts"] : []),
  ]) {
    const result = await subject.execute(toolCall({
      action: "read",
      projectId: "offeragent",
      path: candidate,
    }));
    assert.equal(result.ok, false, candidate);
    assert.ok(["invalid_path", "not_found", "permission_denied", "response_too_large", "unreadable_content"].includes(result.error.code), candidate);
  }
});

test("Project Evidence bounds output and observes changed source versions without any write surface", async (t) => {
  const root = await mkdtemp(path.join(os.tmpdir(), "offeragent-project-bounds-"));
  t.after(() => rm(root, { recursive: true, force: true }));
  await mkdir(path.join(root, "src"), { recursive: true });
  const target = path.join(root, "src", "many.ts");
  await writeFile(target, Array.from({ length: 300 }, (_, index) => `line ${index + 1}`).join("\n"));
  const subject = adapter(root);

  const first = await subject.execute(toolCall({
    action: "read", projectId: "offeragent", path: "src/many.ts", lineStart: 1, lineEnd: 999,
  }));
  assert.equal(first.ok, true);
  assert.equal(first.value.lineEnd, 200);
  assert.equal(first.value.truncated, true);
  assert.equal(first.value.content.split("\n").length, 200);

  const store = await RuntimeStateStore.open(path.join(root, ".state", "state.db"));
  await store.beginAgentRun("project-adapter-conversation", "project-adapter-run", "fake-interview-model", "Read project");
  const completeRead = async (id, result, sequence) => {
    await store.requestToolCall("project-adapter-run", {
      type: "tool_call.requested", protocolVersion: 1, eventId: `${id}-requested`,
      conversationId: "project-adapter-conversation", agentRunId: "project-adapter-run", sequence,
      toolCallId: id, tool: { kind: "local", name: "project_read", arguments: { projectId: "offeragent", path: "src/many.ts" } },
    });
    return store.completeToolCall("project-adapter-run", result, {
      type: "tool_call.completed", protocolVersion: 1, eventId: `${id}-completed`,
      conversationId: "project-adapter-conversation", agentRunId: "project-adapter-run", sequence: sequence + 1,
      toolCallId: id, tool: { kind: "local", name: "project_read" }, status: "completed",
    });
  };
  assert.deepEqual(await completeRead("project-adapter-first", first, 2), []);

  await writeFile(target, "changed\n");
  const second = await subject.execute(toolCall({
    action: "read", projectId: "offeragent", path: "src/many.ts",
  }));
  assert.equal(second.ok, true);
  assert.notEqual(second.value.contentHash, first.value.contentHash);
  assert.notEqual(second.value.modifiedVersion, first.value.modifiedVersion);
  assert.deepEqual(
    await completeRead("project-adapter-second", second, 4),
    ["project/offeragent/src/many.ts"],
  );
  await store.close();
  assert.equal(typeof subject.write, "undefined");
  assert.equal(typeof subject.executeCommand, "undefined");
});
