import assert from "node:assert/strict";
import { createRequire } from "node:module";
import path from "node:path";
import test from "node:test";
import { fileURLToPath } from "node:url";

const repositoryRoot = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..");
const require = createRequire(import.meta.url);
const {
  buildMemoryChangeActions,
  PlanningMemoryModule,
  ProviderSemanticMemoryCapture,
  validateCaptureOperations,
} = require(
  path.join(repositoryRoot, "packages", "runtime", "dist", "planning-memory.js"),
);

const metadata = [
  { path: "memory/user/collaboration.md", name: "Collaboration", description: "Preferred response style", type: "user", modifiedVersion: "v1" },
  { path: "memory/feedback/corrections.md", name: "Corrections", description: "Durable corrections for planning", type: "feedback", modifiedVersion: "v2" },
  { path: "memory/project/offeragent.md", name: "OfferAgent", description: "Current product direction", type: "project", modifiedVersion: "v3" },
  { path: "memory/study/agentic-rl.md", name: "Agentic RL", description: "Cross-day learning sequence", type: "study", modifiedVersion: "v4" },
  { path: "memory/study/irrelevant.md", name: "Cooking", description: "Weekend recipes", type: "study", modifiedVersion: "v5" },
  { path: "memory/study/extra-a.md", name: "Extra A", description: "Extra", type: "study", modifiedVersion: "v6" },
  { path: "memory/study/extra-b.md", name: "Extra B", description: "Extra", type: "study", modifiedVersion: "v7" },
];

test("recall considers current request plus conversation context and reads at most five selected topics", async () => {
  const calls = { reads: [], selections: [] };
  const subject = new PlanningMemoryModule({
    listTopics: async () => [...metadata, { path: "memory/study/bad.md", name: "Bad", description: "", type: "wrong", modifiedVersion: "v8" }],
    readTopics: async (paths) => {
      calls.reads.push(paths);
      return paths.map((topicPath) => ({ path: topicPath, content: `body:${topicPath}`, modifiedVersion: metadata.find(({ path }) => path === topicPath).modifiedVersion }));
    },
    selector: {
      select: async (input) => {
        calls.selections.push(input);
        return [
          "memory/study/agentic-rl.md",
          "memory/project/offeragent.md",
          "memory/feedback/corrections.md",
          "memory/user/collaboration.md",
          "memory/study/extra-a.md",
          "memory/study/extra-b.md",
          "memory/study/agentic-rl.md",
          "memory/study/irrelevant.md",
        ];
      },
    },
  });

  const recalled = await subject.recall({
    request: "Continue the plan we discussed",
    conversationContext: [
      { type: "user_message", text: "I am studying Agentic RL" },
      { type: "assistant_message", text: "We can continue that sequence tomorrow." },
    ],
  });

  assert.equal(calls.selections.length, 1);
  assert.equal(calls.selections[0].request, "Continue the plan we discussed");
  assert.deepEqual(calls.selections[0].conversationContext.map(({ text }) => text), [
    "I am studying Agentic RL",
    "We can continue that sequence tomorrow.",
  ]);
  assert.equal(calls.selections[0].topics.some(({ path }) => path.endsWith("bad.md")), false);
  assert.deepEqual(calls.reads, [[
    "memory/study/agentic-rl.md",
    "memory/project/offeragent.md",
    "memory/feedback/corrections.md",
    "memory/user/collaboration.md",
    "memory/study/extra-a.md",
  ]]);
  assert.deepEqual(recalled.feedback.map(({ path }) => path), ["memory/feedback/corrections.md"]);
  assert.deepEqual(recalled.planning.map(({ path }) => path), [
    "memory/study/agentic-rl.md",
    "memory/project/offeragent.md",
    "memory/user/collaboration.md",
    "memory/study/extra-a.md",
  ]);
  assert.equal(JSON.stringify(recalled).includes("irrelevant"), false);
});

test("recall handles an empty or wholly malformed memory store without selecting or reading", async () => {
  for (const topics of [[], [{ path: "MEMORY.md", name: "Index", description: "full", type: "user" }]]) {
    let selectorCalled = false;
    let readerCalled = false;
    const subject = new PlanningMemoryModule({
      listTopics: async () => topics,
      readTopics: async () => { readerCalled = true; return []; },
      selector: { select: async () => { selectorCalled = true; return []; } },
    });
    assert.deepEqual(await subject.recall({ request: "hello", conversationContext: [] }), {
      feedback: [],
      planning: [],
    });
    assert.equal(selectorCalled, false);
    assert.equal(readerCalled, false);
  }
});

test("recall rejects selector paths and stale or oversized topic bodies", async () => {
  const subject = new PlanningMemoryModule({
    listTopics: async () => metadata.slice(0, 2),
    readTopics: async () => [
      { path: "memory/user/collaboration.md", content: "x".repeat(32_769), modifiedVersion: "v1" },
      { path: "memory/feedback/corrections.md", content: "stale", modifiedVersion: "old" },
    ],
    selector: { select: async () => ["../outside.md", "memory/user/collaboration.md", "memory/feedback/corrections.md"] },
  });
  assert.deepEqual(await subject.recall({ request: "hello", conversationContext: [] }), {
    feedback: [],
    planning: [],
  });
});

test("semantic capture receives only current-run messages and validates confined typed operations", async () => {
  const requests = [];
  const capture = new ProviderSemanticMemoryCapture({
    provider: {
      stream: async function* (request) {
        requests.push(request);
        yield { type: "output_text.delta", delta: JSON.stringify([{
          kind: "upsert",
          path: "memory/study/retrieval.md",
          type: "study",
          name: "Retrieval",
          description: "Cross-day direction",
          content: "Continue retrieval evaluation tomorrow.",
        }]) };
      },
    },
    model: "test",
    fastMode: false,
    signal: new AbortController().signal,
  });
  const operations = await capture.extract({
    newMessages: [
      { type: "user_message", text: "new user message" },
      { type: "assistant_message", text: "new assistant message" },
    ],
    recalledTopics: [],
  });
  assert.equal(operations[0].path, "memory/study/retrieval.md");
  assert.equal(requests.length, 1);
  assert.deepEqual(requests[0].tools, []);
  assert.match(requests[0].input[0].text, /new user message/);
  assert.doesNotMatch(requests[0].input[0].text, /older conversation/);
  assert.deepEqual(validateCaptureOperations([{ kind: "delete", path: "../outside.md" }]), []);
});

test("strict semantic capture distinguishes malformed output from a valid empty result", async () => {
  const capture = new ProviderSemanticMemoryCapture({
    provider: {
      stream: async function* () {
        yield { type: "output_text.delta", delta: "{malformed" };
      },
    },
    model: "test",
    fastMode: false,
    signal: new AbortController().signal,
  });
  const input = {
    newMessages: [{ type: "user_message", text: "new direction" }],
    recalledTopics: [],
  };
  assert.deepEqual(await capture.extract(input), []);
  await assert.rejects(capture.extractStrict(input), /not valid JSON/);
});

test("capture batches consolidate, delete, and keep the concise index atomic", () => {
  const existing = {
    path: "memory/study/old.md",
    type: "study",
    name: "Old",
    description: "Superseded direction",
    modifiedVersion: "v1",
    content: "---\nname: Old\ndescription: Superseded direction\ntype: study\n---\nOld direction.\n",
  };
  const change = buildMemoryChangeActions({
    operations: [
      { kind: "delete", path: existing.path },
      {
        kind: "upsert",
        path: "memory/study/current.md",
        type: "study",
        name: "Current",
        description: "Current direction",
        content: "Current consolidated direction.",
      },
    ],
    topics: [existing],
    bodies: [existing],
    index: { content: "# Planning Memory\n\n- [Old](study/old.md) - Superseded direction\n", modifiedVersion: "index-v1" },
  });
  assert.deepEqual(change.changedPaths, ["memory/study/old.md", "memory/study/current.md"]);
  assert.equal(change.actions[0].operation, "delete");
  assert.equal(change.actions[1].operation, "create");
  assert.equal(change.actions[2].path, "memory/MEMORY.md");
  assert.match(change.actions[2].replacement, /study\/current\.md/);
  assert.doesNotMatch(change.actions[2].replacement, /study\/old\.md/);
});

test("identical semantic capture is a no-op without a redundant checkpoint batch", () => {
  const content = "---\nname: \"Retrieval\"\ndescription: \"Current direction\"\ntype: study\n---\n\nContinue retrieval.\n";
  const existing = {
    path: "memory/study/retrieval.md",
    type: "study",
    name: "Retrieval",
    description: "Current direction",
    modifiedVersion: "v1",
    content,
  };
  assert.equal(buildMemoryChangeActions({
    operations: [{
      kind: "upsert",
      path: existing.path,
      type: "study",
      name: existing.name,
      description: existing.description,
      content: "Continue retrieval.",
    }],
    topics: [existing],
    bodies: [existing],
    index: { content: "# Planning Memory\n\n- [Retrieval](study/retrieval.md) - Current direction\n", modifiedVersion: "i1" },
  }), undefined);
});
