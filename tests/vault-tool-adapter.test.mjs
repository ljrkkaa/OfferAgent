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

function adapter(files, metadata = {}) {
  return new ObsidianVaultToolAdapter(
    {
      getFiles: () => files,
      cachedRead: async (target) => target.content,
    },
    {
      getFileCache: (target) => metadata[target.path],
    },
  );
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

test("vault_search ranks paths before metadata and body and supports exact phrases", async () => {
  const subject = adapter(
    [
      file("attention-map.md", "No matching body text."),
      file("notes/metadata.md", "Metadata-backed note."),
      file("notes/body.md", "intro\nThe attention mechanism is useful.\noutro"),
      file("notes/exact.md", "intro\nScaled dot product attention\noutro"),
      file("notes/reordered.md", "product scaled attention dot"),
    ],
    {
      "notes/metadata.md": {
        frontmatter: { title: "Attention Interview Guide", tags: ["transformers"] },
      },
    },
  );

  const ranked = await subject.execute(call("vault_search", { query: "attention" }));
  assert.equal(ranked.ok, true);
  assert.deepEqual(
    ranked.value.entries.slice(0, 3).map(({ path, matchTier }) => ({ path, matchTier })),
    [
      { path: "attention-map.md", matchTier: "path" },
      { path: "notes/metadata.md", matchTier: "metadata" },
      { path: "notes/body.md", matchTier: "body" },
    ],
  );
  assert.deepEqual(ranked.value.entries[2].snippets[0], {
    lineStart: 2,
    lineEnd: 2,
    content: "The attention mechanism is useful.",
    truncated: false,
  });
  assert.match(ranked.value.entries[2].modifiedVersion, /^mtime:\d+:size:\d+$/);
  assert.match(ranked.value.entries[2].contentHash, /^sha256:[a-f0-9]{64}$/);

  const exact = await subject.execute(
    call("vault_search", { query: "scaled dot product", exactPhrase: true }),
  );
  assert.equal(exact.ok, true);
  assert.deepEqual(exact.value.entries.map((entry) => entry.path), ["notes/exact.md"]);
  assert.equal(exact.value.entries[0].snippets[0].lineStart, 2);

  const structuralKey = await subject.execute(call("vault_search", { query: "frontmatter" }));
  assert.equal(structuralKey.ok, true);
  assert.deepEqual(structuralKey.value.entries, []);
});

test("vault_search enforces result, snippet, query, and snippet-size bounds on large notes", async () => {
  const largeBody = Array.from(
    { length: 50 },
    (_, index) => `${index + 1}: needle ${"x".repeat(80)}`,
  ).join("\n");
  const subject = adapter([
    file("notes/a-large.md", largeBody),
    ...Array.from({ length: 24 }, (_, index) =>
      file(`notes/note-${String(index).padStart(2, "0")}.md`, `needle ${index}`),
    ),
    file(".git/needle.md", "needle secret"),
    file(".hidden/needle.md", "needle hidden"),
    file("Node_Modules/needle.md", "needle dependency"),
  ]);
  const defaults = await subject.execute(call("vault_search", { query: "needle" }));
  assert.equal(defaults.ok, true);
  assert.equal(defaults.value.entries.length, 10);
  assert.equal(defaults.value.truncated, true);
  assert.ok(defaults.value.entries.every((entry) => entry.snippets.length <= 2));
  assert.ok(
    defaults.value.entries.every((entry) =>
      entry.snippets.every((snippet) => Buffer.byteLength(snippet.content, "utf8") <= 240),
    ),
  );
  const maxima = await subject.execute(
    call("vault_search", {
      query: "needle",
      limit: 20,
      snippetsPerFile: 3,
      snippetMaxBytes: 512,
    }),
  );
  assert.equal(maxima.ok, true);
  assert.equal(maxima.value.entries.length, 20);
  assert.ok(maxima.value.entries.every((entry) => entry.snippets.length <= 3));
  const bounded = await subject.execute(
    call("vault_search", {
      query: "needle",
      limit: 1,
      snippetsPerFile: 1,
      snippetMaxBytes: 24,
    }),
  );
  assert.equal(bounded.ok, true);
  assert.equal(bounded.value.entries.length, 1);
  assert.equal(bounded.value.entries[0].snippets.length, 1);
  assert.ok(Buffer.byteLength(bounded.value.entries[0].snippets[0].content, "utf8") <= 24);
  assert.equal(bounded.value.entries[0].snippets[0].truncated, true);
  assert.equal(bounded.value.truncated, true);
  assert.equal(bounded.value.entries.some((entry) => entry.path.startsWith(".")), false);
  assert.equal(
    bounded.value.entries.some((entry) => entry.path.toLowerCase().startsWith("node_modules/")),
    false,
  );

  for (const arguments_ of [
    { query: "x".repeat(513) },
    { query: "needle", limit: 21 },
    { query: "needle", snippetsPerFile: 4 },
    { query: "needle", snippetMaxBytes: 513 },
  ]) {
    const result = await subject.execute(call("vault_search", arguments_));
    assert.equal(result.ok, false);
    assert.equal(result.error.code, "request_too_large");
  }
});

test("vault_search truncates Unicode snippets only at complete code points", async () => {
  const subject = adapter([file("notes/unicode.md", `needle ${"😀".repeat(100)}`)]);
  const result = await subject.execute(
    call("vault_search", {
      query: "needle",
      snippetMaxBytes: 18,
      snippetsPerFile: 1,
    }),
  );
  assert.equal(result.ok, true);
  const snippet = result.value.entries[0].snippets[0];
  assert.ok(Buffer.byteLength(snippet.content, "utf8") <= 18);
  assert.equal(
    /[\uD800-\uDBFF](?![\uDC00-\uDFFF])|(?<![\uD800-\uDBFF])[\uDC00-\uDFFF]/u.test(snippet.content),
    false,
  );
});
