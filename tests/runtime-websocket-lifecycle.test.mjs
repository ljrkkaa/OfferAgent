import assert from "node:assert/strict";
import { createServer } from "node:http";
import { mkdtemp, readFile, rm, writeFile } from "node:fs/promises";
import Module, { createRequire } from "node:module";
import os from "node:os";
import path from "node:path";
import test from "node:test";
import { fileURLToPath } from "node:url";
import initSqlJs from "sql.js/dist/sql-asm.js";

const repositoryRoot = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..");
const require = createRequire(import.meta.url);
const ActualWebSocket = require("ws");
let connectionCount = 0;

class TrackingWebSocket extends ActualWebSocket {
  constructor(...arguments_) {
    super(...arguments_);
    connectionCount += 1;
  }
}

const originalLoad = Module._load;
Module._load = function loadWithTrackedWebSocket(request, parent, isMain) {
  if (request === "ws") return TrackingWebSocket;
  return originalLoad.call(this, request, parent, isMain);
};
let RuntimeSupervisor;
let isExpectedConversationEvent;
try {
  ({ RuntimeSupervisor, isExpectedConversationEvent } = require(
    path.join(repositoryRoot, "packages", "plugin", "dist", "runtime-supervisor.js"),
  ));
} finally {
  Module._load = originalLoad;
}

const runtimeEntry = path.join(repositoryRoot, "packages", "runtime", "dist", "cli.js");

test("Conversation responses require the expected protocol identity and ordering", () => {
  const expected = {
    conversationId: "conversation-one",
    expectedSequence: 1,
    requestEventId: "request-event",
    requestId: "request-one",
  };
  const valid = {
    type: "conversation.deleted",
    protocolVersion: 1,
    eventId: "response-event",
    conversationId: "conversation-one",
    agentRunId: "request-one",
    sequence: 1,
  };
  assert.equal(isExpectedConversationEvent(valid, expected), true);
  for (const malformed of [
    { ...valid, protocolVersion: 2 },
    { ...valid, eventId: "request-event" },
    { ...valid, conversationId: "conversation-two" },
    { ...valid, agentRunId: "request-two" },
    { ...valid, sequence: 2 },
  ]) {
    assert.equal(isExpectedConversationEvent(malformed, expected), false);
  }
});

async function completeRun(supervisor, suffix) {
  const events = [];
  for await (const event of supervisor.runAgent({
    conversationId: "long-lived-conversation",
    agentRunId: `long-lived-run-${suffix}`,
    model: "fake-interview-model",
    input: `message ${suffix}`,
  })) {
    events.push(event);
  }
  assert.equal(events.at(-1)?.type, "agent_run.completed");
}

test("RuntimeSupervisor keeps one WebSocket across sequential Agent Runs", async (t) => {
  connectionCount = 0;
  const supervisor = new RuntimeSupervisor({
    nodeCandidates: [process.execPath],
    parentPid: process.pid,
    provider: "fake",
    runtimePath: runtimeEntry,
  });
  t.after(() => supervisor.stop());

  await supervisor.start();
  await completeRun(supervisor, "one");
  await completeRun(supervisor, "two");
  assert.equal(connectionCount, 1);
});

test("a consumed failed terminal event is durably acknowledged before iterator return", async (t) => {
  const temporaryDirectory = await mkdtemp(path.join(os.tmpdir(), "offeragent-terminal-ack-"));
  const statePath = path.join(temporaryDirectory, "state.db");
  const supervisor = new RuntimeSupervisor({
    nodeCandidates: [process.execPath],
    parentPid: process.pid,
    provider: "fake",
    runtimePath: runtimeEntry,
    statePath,
  });
  t.after(async () => {
    await supervisor.stop();
    await rm(temporaryDirectory, { recursive: true, force: true });
  });

  await supervisor.start();
  const events = [];
  for await (const event of supervisor.runAgent({
    conversationId: "terminal-ack-conversation",
    agentRunId: "terminal-ack-run",
    model: "missing-model",
    input: "Fail with a typed model error",
  })) {
    events.push(event);
    if (event.type === "agent_run.failed") break;
  }
  assert.equal(events.at(-1)?.type, "agent_run.failed");
  await new Promise((resolve) => setTimeout(resolve, 100));
  await supervisor.stop();

  const SQL = await initSqlJs();
  const database = new SQL.Database(await readFile(statePath));
  assert.deepEqual(
    database.exec(
      `SELECT event_type, acknowledged_at IS NOT NULL
       FROM durable_events ORDER BY sequence`,
    )[0].values,
    [["agent_run.started", 1], ["agent_run.failed", 1]],
  );
  assert.equal(
    database.exec("SELECT status FROM agent_runs WHERE id = 'terminal-ack-run'")[0].values[0][0],
    "failed",
  );
  database.close();
});

test("returning one Agent Run iterator cancels only that run", async (t) => {
  connectionCount = 0;
  const temporaryDirectory = await mkdtemp(path.join(os.tmpdir(), "offeragent-iterator-cancel-"));
  const authPath = path.join(temporaryDirectory, "auth.json");
  await writeFile(authPath, JSON.stringify({ tokens: { access_token: "iterator-cancel-token" } }), "utf8");
  let responseMode = "hanging";
  let markHangingRequestClosed;
  const hangingRequestClosed = new Promise((resolve) => (markHangingRequestClosed = resolve));
  const upstream = createServer((incoming, response) => {
    if (incoming.url !== "/responses") return response.writeHead(404).end();
    response.writeHead(200, { "content-type": "text/event-stream" });
    response.write('data: {"type":"response.output_text.delta","delta":"first"}\n\n');
    if (responseMode === "hanging") {
      response.on("close", markHangingRequestClosed);
      return;
    }
    response.end('data: {"type":"response.completed","response":{"status":"completed"}}\n\n');
  });
  await new Promise((resolve) => upstream.listen(0, "127.0.0.1", resolve));
  const upstreamPort = upstream.address().port;
  const previousAuthPath = process.env.OFFERAGENT_CODEX_AUTH_FILE;
  const previousBaseUrl = process.env.OFFERAGENT_CODEX_BASE_URL;
  process.env.OFFERAGENT_CODEX_AUTH_FILE = authPath;
  process.env.OFFERAGENT_CODEX_BASE_URL = `http://127.0.0.1:${upstreamPort}`;

  const supervisor = new RuntimeSupervisor({
    nodeCandidates: [process.execPath],
    parentPid: process.pid,
    provider: "codex",
    runtimePath: runtimeEntry,
    statePath: path.join(temporaryDirectory, "state.db"),
  });
  t.after(async () => {
    await supervisor.stop();
    await new Promise((resolve) => upstream.close(resolve));
    await rm(temporaryDirectory, { recursive: true, force: true });
    if (previousAuthPath === undefined) delete process.env.OFFERAGENT_CODEX_AUTH_FILE;
    else process.env.OFFERAGENT_CODEX_AUTH_FILE = previousAuthPath;
    if (previousBaseUrl === undefined) delete process.env.OFFERAGENT_CODEX_BASE_URL;
    else process.env.OFFERAGENT_CODEX_BASE_URL = previousBaseUrl;
  });

  await supervisor.start();
  const iterator = supervisor
    .runAgent({
      conversationId: "iterator-cancel-conversation",
      agentRunId: "iterator-cancel-run-one",
      model: "gpt-5.4",
      input: "start a hanging response",
    })
    [Symbol.asyncIterator]();
  assert.equal((await iterator.next()).value.type, "agent_run.started");
  assert.equal((await iterator.next()).value.type, "agent_run.delta");
  await iterator.return();
  await Promise.race([
    hangingRequestClosed,
    new Promise((_, reject) =>
      setTimeout(() => reject(new Error("Returning the iterator did not cancel its Provider request")), 2_000),
    ),
  ]);

  responseMode = "complete";
  const secondEvents = [];
  for await (const event of supervisor.runAgent({
    conversationId: "iterator-cancel-conversation",
    agentRunId: "iterator-cancel-run-two",
    model: "gpt-5.4",
    input: "complete normally",
  })) {
    secondEvents.push(event);
  }
  assert.equal(secondEvents.at(-1)?.type, "agent_run.completed");
  assert.equal(connectionCount, 1);
});
