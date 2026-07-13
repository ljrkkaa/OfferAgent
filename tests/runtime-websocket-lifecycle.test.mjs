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
const trackedSockets = [];

class TrackingWebSocket extends ActualWebSocket {
  constructor(...arguments_) {
    super(...arguments_);
    connectionCount += 1;
    trackedSockets.push(this);
  }
}

const originalLoad = Module._load;
Module._load = function loadWithTrackedWebSocket(request, parent, isMain) {
  if (request === "ws") return TrackingWebSocket;
  return originalLoad.call(this, request, parent, isMain);
};
let RuntimeSupervisor;
let isAcceptableAgentRunSequence;
let isExpectedConversationEvent;
let isExpectedVaultChangeEvent;
try {
  ({
    RuntimeSupervisor,
    isAcceptableAgentRunSequence,
    isExpectedConversationEvent,
    isExpectedVaultChangeEvent,
  } = require(
    path.join(repositoryRoot, "packages", "plugin", "dist", "runtime-supervisor.js"),
  ));
} finally {
  Module._load = originalLoad;
}

const runtimeEntry = path.join(repositoryRoot, "packages", "runtime", "dist", "cli.js");

function contractResult() {
  return {
    ok: true,
    value: {
      type: "agent_contract_read",
      path: "agent.md",
      modifiedVersion: "mtime:1:size:24",
      contentHash: "sha256:test-contract",
      content: "# Test Agent Contract",
    },
  };
}

const contractToolExecutor = {
  async execute(event) {
    assert.equal(event.tool.name, "agent_contract_read");
    return contractResult();
  },
};

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

test("Vault Change responses must match their pending command payload", () => {
  const command = {
    type: "vault_changes.state",
    protocolVersion: 1,
    eventId: "vault-request-event",
    conversationId: "vault-change-management",
    agentRunId: "vault-request-one",
    sequence: 0,
    batchId: "batch-one",
    state: "applied",
  };
  const expected = {
    command,
    conversationId: command.conversationId,
    expectedSequence: 1,
    requestEventId: command.eventId,
    requestId: command.agentRunId,
  };
  const valid = {
    type: "vault_changes.state_stored",
    protocolVersion: 1,
    eventId: "vault-response-event",
    conversationId: command.conversationId,
    agentRunId: command.agentRunId,
    sequence: 1,
    batchId: command.batchId,
    state: command.state,
  };
  assert.equal(isExpectedVaultChangeEvent(valid, expected), true);
  for (const malformed of [
    { ...valid, batchId: "batch-two" },
    { ...valid, state: "rolled_back" },
    { ...valid, type: "vault_changes.applying_stored" },
  ]) {
    assert.equal(isExpectedVaultChangeEvent(malformed, expected), false);
  }
});

test("only replay mode permits forward gaps to later durable Agent Run events", () => {
  const event = {
    type: "tool_call.requested",
    protocolVersion: 1,
    eventId: "replayed-tool-request",
    conversationId: "sequence-conversation",
    agentRunId: "sequence-run",
    sequence: 5,
    toolCallId: "sequence-tool",
    tool: { kind: "local", name: "vault_read", arguments: { path: "notes/a.md" } },
  };
  assert.equal(isAcceptableAgentRunSequence(event, 4, false), false);
  assert.equal(isAcceptableAgentRunSequence(event, 4, true), true);
  assert.equal(isAcceptableAgentRunSequence({ ...event, sequence: 3 }, 4, true), false);
  assert.equal(
    isAcceptableAgentRunSequence({ ...event, type: "agent_run.delta", delta: "lost" }, 4, true),
    false,
  );
  const terminal = {
    ...event,
    type: "agent_run.completed",
    output: { role: "assistant", text: "done" },
  };
  assert.equal(isAcceptableAgentRunSequence(terminal, 4, false), false);
  assert.equal(isAcceptableAgentRunSequence(terminal, 4, true), true);
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
    toolExecutor: contractToolExecutor,
  });
  t.after(() => supervisor.stop());

  await supervisor.start();
  await completeRun(supervisor, "one");
  await completeRun(supervisor, "two");
  assert.equal(connectionCount, 1);
});

test("RuntimeSupervisor reconnects and delivers the durable interruption after a socket drop", async (t) => {
  connectionCount = 0;
  trackedSockets.length = 0;
  const temporaryDirectory = await mkdtemp(path.join(os.tmpdir(), "offeragent-reconnect-"));
  let releaseRead;
  let contractExecutions = 0;
  let vaultReadExecutions = 0;
  const delayedRead = new Promise((resolve) => (releaseRead = resolve));
  const supervisor = new RuntimeSupervisor({
    nodeCandidates: [process.execPath],
    parentPid: process.pid,
    provider: "fake",
    runtimePath: runtimeEntry,
    statePath: path.join(temporaryDirectory, "state.db"),
    toolExecutor: {
      execute: async (event) => {
        if (event.tool.name === "agent_contract_read") {
          contractExecutions += 1;
          return contractResult();
        }
        vaultReadExecutions += 1;
        return delayedRead;
      },
    },
  });
  t.after(async () => {
    releaseRead?.({ ok: false, error: { code: "tool_error", message: "released" } });
    await supervisor.stop();
    await rm(temporaryDirectory, { recursive: true, force: true });
  });

  await supervisor.start();
  const iterator = supervisor.runAgent({
    conversationId: "reconnect-conversation",
    agentRunId: "reconnect-run",
    model: "fake-interview-model",
    input: "vault_read notes/reconnect.md",
  })[Symbol.asyncIterator]();
  assert.equal((await iterator.next()).value.type, "agent_run.started");
  assert.equal((await iterator.next()).value.tool.name, "agent_contract_read");
  assert.equal((await iterator.next()).value.type, "tool_call.completed");
  assert.equal((await iterator.next()).value.tool.name, "vault_read");
  trackedSockets[0].terminate();

  const afterDrop = [];
  while (true) {
    const next = await Promise.race([
      iterator.next(),
      new Promise((_, reject) => setTimeout(() => reject(new Error("Reconnect timed out")), 5_000)),
    ]);
    if (next.done) break;
    afterDrop.push(next.value);
    if (next.value.type === "agent_run.interrupted") break;
  }
  assert.equal(afterDrop.at(-1).type, "agent_run.interrupted");
  assert.equal(connectionCount, 2);
  assert.equal(contractExecutions, 1);
  assert.equal(vaultReadExecutions, 1);
  releaseRead({ ok: false, error: { code: "tool_error", message: "late result" } });
  await completeRun(supervisor, "after-reconnect");
});

test("RuntimeSupervisor resends a stable Run start dropped before durable receipt", async (t) => {
  connectionCount = 0;
  trackedSockets.length = 0;
  const temporaryDirectory = await mkdtemp(path.join(os.tmpdir(), "offeragent-start-reconnect-"));
  const supervisor = new RuntimeSupervisor({
    nodeCandidates: [process.execPath],
    parentPid: process.pid,
    provider: "fake",
    runtimePath: runtimeEntry,
    statePath: path.join(temporaryDirectory, "state.db"),
    toolExecutor: contractToolExecutor,
  });
  t.after(async () => {
    await supervisor.stop();
    await rm(temporaryDirectory, { recursive: true, force: true });
  });

  await supervisor.start();
  const iterator = supervisor.runAgent({
    conversationId: "dropped-start-conversation",
    agentRunId: "dropped-start-run",
    model: "fake-interview-model",
    input: "dropped start",
  })[Symbol.asyncIterator]();
  const firstEvent = iterator.next();
  trackedSockets[0].terminate();
  const first = await Promise.race([
    firstEvent,
    new Promise((_, reject) => setTimeout(() => reject(new Error("Run start replay timed out")), 5_000)),
  ]);
  assert.equal(first.value.type, "agent_run.started");
  let terminal = first.value;
  while (!["agent_run.completed", "agent_run.interrupted"].includes(terminal.type)) {
    const next = await iterator.next();
    if (next.done) break;
    terminal = next.value;
  }
  assert.ok(["agent_run.completed", "agent_run.interrupted"].includes(terminal.type));
  assert.equal(connectionCount, 2);
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
    toolExecutor: contractToolExecutor,
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
    [
      ["agent_run.started", 1],
      ["tool_call.requested", 1],
      ["tool_call.completed", 1],
      ["agent_run.failed", 1],
    ],
  );
  assert.equal(
    database.exec("SELECT status FROM agent_runs WHERE id = 'terminal-ack-run'")[0].values[0][0],
    "failed",
  );
  database.close();
});

test("a missing connected Vault executor returns a typed tool error and the Run continues", async (t) => {
  const temporaryDirectory = await mkdtemp(path.join(os.tmpdir(), "offeragent-tool-disconnected-"));
  const supervisor = new RuntimeSupervisor({
    nodeCandidates: [process.execPath],
    parentPid: process.pid,
    provider: "fake",
    runtimePath: runtimeEntry,
    statePath: path.join(temporaryDirectory, "state.db"),
    toolExecutor: {
      async execute(event) {
        if (event.tool.name === "agent_contract_read") {
          return {
            ok: true,
            value: {
              type: "agent_contract_read",
              path: "agent.md",
              modifiedVersion: "mtime:1:size:24",
              contentHash: "sha256:test-contract",
              content: "# Test Agent Contract",
            },
          };
        }
        return {
          ok: false,
          error: {
            code: "plugin_disconnected",
            message: "The connected Obsidian plugin has no Vault tool executor.",
          },
        };
      },
    },
  });
  t.after(async () => {
    await supervisor.stop();
    await rm(temporaryDirectory, { recursive: true, force: true });
  });

  await supervisor.start();
  const events = [];
  for await (const event of supervisor.runAgent({
    conversationId: "tool-disconnected-conversation",
    agentRunId: "tool-disconnected-run",
    model: "fake-interview-model",
    input: "vault_read notes/a.md",
  })) {
    events.push(event);
  }
  const toolResult = events.find(
    (event) => event.type === "tool_call.completed" && event.tool.name === "vault_read",
  );
  assert.equal(toolResult.status, "failed");
  assert.equal(toolResult.error.code, "plugin_disconnected");
  assert.equal(events.at(-1).type, "agent_run.completed");
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
    toolExecutor: contractToolExecutor,
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
  assert.equal((await iterator.next()).value.type, "tool_call.requested");
  assert.equal((await iterator.next()).value.type, "tool_call.completed");
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

test("cancelling during a delayed Vault tool call ignores its late result and terminalizes the call", async (t) => {
  connectionCount = 0;
  const temporaryDirectory = await mkdtemp(path.join(os.tmpdir(), "offeragent-tool-cancel-"));
  const statePath = path.join(temporaryDirectory, "state.db");
  let releaseTool;
  const delayedResult = new Promise((resolve) => (releaseTool = resolve));
  const supervisor = new RuntimeSupervisor({
    nodeCandidates: [process.execPath],
    parentPid: process.pid,
    provider: "fake",
    runtimePath: runtimeEntry,
    statePath,
    toolExecutor: {
      execute: async (event) =>
        event.tool.name === "agent_contract_read" ? contractResult() : delayedResult,
    },
  });
  t.after(async () => {
    await supervisor.stop();
    await rm(temporaryDirectory, { recursive: true, force: true });
  });

  await supervisor.start();
  const iterator = supervisor
    .runAgent({
      conversationId: "tool-cancel-conversation",
      agentRunId: "tool-cancel-run",
      model: "fake-interview-model",
      input: "vault_read notes/slow.md",
    })
    [Symbol.asyncIterator]();
  assert.equal((await iterator.next()).value.type, "agent_run.started");
  assert.equal((await iterator.next()).value.type, "tool_call.requested");
  assert.equal((await iterator.next()).value.type, "tool_call.completed");
  assert.equal((await iterator.next()).value.type, "tool_call.requested");
  await iterator.return();
  releaseTool({
    ok: true,
    value: {
      type: "vault_read",
      path: "notes/slow.md",
      lineStart: 1,
      lineEnd: 1,
      modifiedVersion: "mtime:1:size:4",
      contentHash: "sha256:slow",
      content: "slow",
      truncated: false,
    },
  });
  await new Promise((resolve) => setTimeout(resolve, 100));

  await completeRun(supervisor, "after-tool-cancel");
  assert.equal(connectionCount, 1);
  await supervisor.stop();

  const SQL = await initSqlJs();
  const database = new SQL.Database(await readFile(statePath));
  assert.deepEqual(
    database.exec(
      "SELECT status, error_code FROM tool_calls WHERE agent_run_id = 'tool-cancel-run' AND name = 'vault_read'",
    )[0].values,
    [["failed", "tool_error"]],
  );
  assert.equal(
    database.exec("SELECT status FROM agent_runs WHERE id = 'tool-cancel-run'")[0].values[0][0],
    "cancelled",
  );
  database.close();
});
