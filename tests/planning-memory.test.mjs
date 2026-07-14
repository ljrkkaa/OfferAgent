import assert from "node:assert/strict";
import { createRequire } from "node:module";
import path from "node:path";
import test from "node:test";
import { fileURLToPath } from "node:url";

const repositoryRoot = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..");
const require = createRequire(import.meta.url);
const { PlanningMemoryModule } = require(
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
