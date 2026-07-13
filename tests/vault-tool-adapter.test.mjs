import assert from "node:assert/strict";
import { createRequire } from "node:module";
import Module from "node:module";
import path from "node:path";
import test from "node:test";
import { fileURLToPath } from "node:url";

const repositoryRoot = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..");
const require = createRequire(import.meta.url);
const modulePath = path.join(repositoryRoot, "packages", "plugin", "dist", "vault-tool-adapter.js");
const originalLoad = Module._load;
Module._load = function loadWithObsidianStub(request, parent, isMain) {
  if (request === "obsidian") return {};
  return originalLoad.call(this, request, parent, isMain);
};
let ObsidianVaultToolAdapter;
try {
  ({ ObsidianVaultToolAdapter } = require(modulePath));
} finally {
  Module._load = originalLoad;
}

function file(pathname, content, mtime = 1234) {
  return {
    path: pathname,
    extension: pathname.split(".").at(-1),
    stat: { mtime, size: Buffer.byteLength(content, "utf8") },
    content,
  };
}

function call(name, arguments_) {
  return {
    type: "tool_call.requested",
    protocolVersion: 1,
    eventId: `event-${name}`,
    conversationId: "conversation",
    agentRunId: "run",
    sequence: 2,
    toolCallId: `call-${name}`,
    tool: { kind: "local", name, arguments: arguments_ },
  };
}

function adapter(files) {
  return new ObsidianVaultToolAdapter({
    getFiles: () => files,
    cachedRead: async (target) => target.content,
  });
}

test("vault_read returns bounded exact evidence with stable source metadata", async () => {
  const subject = adapter([file("notes/interview.md", "first\nsecond\nthird\nfourth", 5678)]);
  const result = await subject.execute(
    call("vault_read", { path: "notes/interview.md", lineStart: 2, lineEnd: 3 }),
  );
  assert.equal(result.ok, true);
  assert.deepEqual(result.value, {
    type: "vault_read",
    path: "notes/interview.md",
    lineStart: 2,
    lineEnd: 3,
    modifiedVersion: "mtime:5678:size:25",
    contentHash: "sha256:2b7d36e0066223fac14ca86edd9fa468a4352c6a138b0238e563880956c9e5b4",
    content: "second\nthird",
    truncated: true,
  });
});

test("vault_list is sorted, bounded, and excludes control or non-note files", async () => {
  const subject = adapter([
    file("z-last.md", "z"),
    file("notes/a.md", "a"),
    file(".obsidian/private.md", "secret"),
    file("agent.md", "contract"),
    file("image.png", "binary"),
  ]);
  const result = await subject.execute(call("vault_list", { limit: 1 }));
  assert.equal(result.ok, true);
  assert.equal(result.value.truncated, true);
  assert.deepEqual(result.value.entries.map((entry) => entry.path), ["notes/a.md"]);
});

test("Vault tools reject escapes, missing files, oversized ranges, and oversized content", async () => {
  const subject = adapter([
    file("notes/interview.md", "one\ntwo"),
    file("notes/large.md", "x".repeat(32_769)),
  ]);
  const cases = [
    [call("vault_read", { path: "../outside.md" }), "invalid_path"],
    [call("vault_read", { path: ".obsidian/config" }), "invalid_path"],
    [call("vault_read", { path: "C:/outside.md" }), "invalid_path"],
    [call("vault_read", { path: "notes/missing.md" }), "not_found"],
    [call("vault_read", { path: "notes/interview.md", lineStart: 1, lineEnd: 201 }), "request_too_large"],
    [call("vault_read", { path: "notes/large.md" }), "request_too_large"],
    [call("vault_list", { limit: 101 }), "request_too_large"],
  ];
  for (const [request, code] of cases) {
    const result = await subject.execute(request);
    assert.equal(result.ok, false);
    assert.equal(result.error.code, code);
  }
});
