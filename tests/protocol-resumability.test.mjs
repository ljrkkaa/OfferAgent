import assert from "node:assert/strict";
import { spawn } from "node:child_process";
import { once } from "node:events";
import { request } from "node:http";
import { mkdtemp, rm } from "node:fs/promises";
import os from "node:os";
import path from "node:path";
import test from "node:test";
import { fileURLToPath } from "node:url";
import WebSocket from "ws";

const repositoryRoot = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..");
const runtimeEntry = path.join(repositoryRoot, "packages", "runtime", "dist", "cli.js");

function readHandshake(stream) {
  return new Promise((resolve, reject) => {
    let buffer = "";
    const timeout = setTimeout(() => reject(new Error("Runtime handshake timed out")), 5_000);
    stream.on("data", function onData(chunk) {
      buffer += chunk.toString("utf8");
      const newline = buffer.indexOf("\n");
      if (newline < 0) return;
      clearTimeout(timeout);
      stream.off("data", onData);
      resolve(JSON.parse(buffer.slice(0, newline)));
    });
  });
}

async function startRuntime(statePath, token) {
  const runtime = spawn(
    process.execPath,
    [
      runtimeEntry,
      "--port", "0",
      "--token", token,
      "--parent-pid", `${process.pid}`,
      "--provider", "fake",
      "--state-path", statePath,
    ],
    { stdio: ["ignore", "pipe", "pipe"], windowsHide: true },
  );
  const handshake = await readHandshake(runtime.stdout);
  return { handshake, runtime, token };
}

async function connect(instance) {
  const socket = new WebSocket(`ws://127.0.0.1:${instance.handshake.port}/events`, {
    headers: { authorization: `Bearer ${instance.token}` },
  });
  await once(socket, "open");
  return socket;
}

async function stopRuntime(instance) {
  if (instance.runtime.exitCode !== null) return;
  const exited = once(instance.runtime, "exit");
  await new Promise((resolve, reject) => {
    const outgoing = request(
      {
        host: "127.0.0.1",
        port: instance.handshake.port,
        path: "/shutdown",
        method: "POST",
        headers: { authorization: `Bearer ${instance.token}` },
      },
      (response) => {
        response.resume();
        response.on("end", resolve);
      },
    );
    outgoing.once("error", reject);
    outgoing.end();
  });
  await exited;
}

function contractResult(event) {
  return {
    type: "tool_result",
    protocolVersion: 1,
    eventId: `result-${event.toolCallId}`,
    conversationId: event.conversationId,
    agentRunId: event.agentRunId,
    sequence: event.sequence,
    toolCallId: event.toolCallId,
    result: {
      ok: true,
      value: {
        type: "agent_contract_read",
        path: "agent.md",
        modifiedVersion: "mtime:1:size:8",
        contentHash: "sha256:contract",
        content: "# Agent",
      },
    },
  };
}

function planningMemoryListResult(event) {
  return {
    type: "tool_result",
    protocolVersion: 1,
    eventId: `memory-result-${event.toolCallId}`,
    conversationId: event.conversationId,
    agentRunId: event.agentRunId,
    sequence: event.sequence,
    toolCallId: event.toolCallId,
    result: { ok: true, value: { type: "planning_memory_list", topics: [], truncated: false } },
  };
}

function respondToRequiredPrelude(socket, event) {
  if (event.type !== "tool_call.requested") return false;
  const result = event.tool.name === "agent_contract_read"
    ? contractResult(event)
    : event.tool.name === "planning_memory_list"
      ? planningMemoryListResult(event)
      : undefined;
  if (!result) return false;
  socket.send(JSON.stringify(result));
  return true;
}

function collectRun(socket, runId) {
  return new Promise((resolve, reject) => {
    const events = [];
    const timeout = setTimeout(() => reject(new Error("Agent Run timed out")), 5_000);
    socket.on("message", function onMessage(data) {
      const event = JSON.parse(data.toString("utf8"));
      if (event.agentRunId !== runId) return;
      events.push(event);
      respondToRequiredPrelude(socket, event);
      if (["agent_run.completed", "agent_run.failed", "agent_run.interrupted"].includes(event.type)) {
        clearTimeout(timeout);
        socket.off("message", onMessage);
        resolve(events);
      }
    });
  });
}

function sendStart(socket, runId, eventId = `start-${runId}`) {
  socket.send(JSON.stringify({
    type: "agent_run.start",
    protocolVersion: 1,
    eventId,
    conversationId: `conversation-${runId}`,
    agentRunId: runId,
    sequence: 0,
    model: "fake-interview-model",
    input: { role: "user", text: "protocol test" },
  }));
}

function waitForMessage(socket, predicate, timeoutMs = 2_000) {
  return new Promise((resolve, reject) => {
    const timeout = setTimeout(() => {
      socket.off("message", onMessage);
      reject(new Error("Protocol response timed out"));
    }, timeoutMs);
    function onMessage(data) {
      const message = JSON.parse(data.toString("utf8"));
      if (!predicate(message)) return;
      clearTimeout(timeout);
      socket.off("message", onMessage);
      resolve(message);
    }
    socket.on("message", onMessage);
  });
}

async function expectNoMessage(socket, timeoutMs = 150) {
  const observed = await Promise.race([
    once(socket, "message").then(() => true),
    new Promise((resolve) => setTimeout(() => resolve(false), timeoutMs)),
  ]);
  assert.equal(observed, false);
}

function httpStatus(instance, pathname, token = instance.token) {
  return new Promise((resolve, reject) => {
    const outgoing = request(
      {
        host: "127.0.0.1",
        port: instance.handshake.port,
        path: pathname,
        headers: { authorization: `Bearer ${token}` },
      },
      (response) => {
        response.resume();
        response.on("end", () => resolve(response.statusCode));
      },
    );
    outgoing.once("error", reject);
    outgoing.end();
  });
}

test("cumulative acknowledgement replays only later durable events in order", async (t) => {
  const directory = await mkdtemp(path.join(os.tmpdir(), "offeragent-protocol-ack-"));
  const instance = await startRuntime(path.join(directory, "state.db"), "protocol-ack-token");
  t.after(async () => {
    await stopRuntime(instance);
    await rm(directory, { recursive: true, force: true });
  });
  const socket = await connect(instance);
  const completed = collectRun(socket, "ack-run");
  sendStart(socket, "ack-run");
  const events = await completed;
  const acknowledged = events.find(
    (event) => event.type === "tool_call.completed" && event.tool.name === "agent_contract_read",
  );
  socket.send(JSON.stringify({
    type: "event.ack",
    protocolVersion: 1,
    eventId: "ack-through-contract",
    acknowledgedEventId: acknowledged.eventId,
    conversationId: acknowledged.conversationId,
    agentRunId: acknowledged.agentRunId,
    sequence: acknowledged.sequence,
  }));
  await new Promise((resolve) => setTimeout(resolve, 50));
  socket.close();
  await once(socket, "close");

  const replay = await connect(instance);
  const replayed = [];
  replay.on("message", (data) => replayed.push(JSON.parse(data.toString("utf8"))));
  await new Promise((resolve) => setTimeout(resolve, 150));
  assert.deepEqual(
    replayed.filter((event) => event.agentRunId === "ack-run").map((event) => event.sequence),
    events
      .filter((event) => event.sequence > acknowledged.sequence && event.type !== "agent_run.delta")
      .map((event) => event.sequence),
  );
  replay.close();
});

test("a duplicate stable Agent Run start is ignored instead of closing the socket", async (t) => {
  const directory = await mkdtemp(path.join(os.tmpdir(), "offeragent-protocol-duplicate-"));
  const instance = await startRuntime(path.join(directory, "state.db"), "protocol-duplicate-token");
  t.after(async () => {
    await stopRuntime(instance);
    await rm(directory, { recursive: true, force: true });
  });
  const socket = await connect(instance);
  const completed = collectRun(socket, "duplicate-run");
  sendStart(socket, "duplicate-run", "stable-start-event");
  sendStart(socket, "duplicate-run", "stable-start-event");
  const events = await completed;
  const terminal = events.at(-1);
  assert.equal(terminal.type, "agent_run.completed");
  assert.equal(socket.readyState, WebSocket.OPEN);
  socket.send(JSON.stringify({
    type: "event.ack",
    protocolVersion: 1,
    eventId: "ack-duplicate-run",
    acknowledgedEventId: terminal.eventId,
    conversationId: terminal.conversationId,
    agentRunId: terminal.agentRunId,
    sequence: terminal.sequence,
  }));
  await new Promise((resolve) => setTimeout(resolve, 50));
  sendStart(socket, "duplicate-run", "stable-start-event");
  await expectNoMessage(socket);
  assert.equal(socket.readyState, WebSocket.OPEN);

  const conflictingIdentity = once(socket, "close");
  sendStart(socket, "different-run", "stable-start-event");
  const [code, reason] = await conflictingIdentity;
  assert.equal(code, 1008);
  assert.match(reason.toString("utf8"), /conflicting duplicate agent run start/i);
});

test("duplicate Tool Results are safe and conflicting delayed results are diagnosed", async (t) => {
  const directory = await mkdtemp(path.join(os.tmpdir(), "offeragent-protocol-tool-result-"));
  const instance = await startRuntime(path.join(directory, "state.db"), "protocol-tool-token");
  t.after(async () => {
    await stopRuntime(instance);
    await rm(directory, { recursive: true, force: true });
  });
  const socket = await connect(instance);
  let vaultResult;
  const completed = new Promise((resolve, reject) => {
    const timeout = setTimeout(() => reject(new Error("Tool Result run timed out")), 5_000);
    socket.on("message", function onMessage(data) {
      const event = JSON.parse(data.toString("utf8"));
      if (event.agentRunId !== "tool-result-run") return;
      if (respondToRequiredPrelude(socket, event)) return;
      if (event.type === "tool_call.requested" && event.tool.name === "vault_read") {
        vaultResult = {
          type: "tool_result",
          protocolVersion: 1,
          eventId: "stable-vault-result",
          conversationId: event.conversationId,
          agentRunId: event.agentRunId,
          sequence: event.sequence,
          toolCallId: event.toolCallId,
          result: {
            ok: true,
            value: {
              type: "vault_read",
              path: "notes/a.md",
              lineStart: 1,
              lineEnd: 1,
              modifiedVersion: "mtime:1:size:4",
              contentHash: "sha256:fact",
              content: "fact",
              truncated: false,
            },
          },
        };
        socket.send(JSON.stringify(vaultResult));
        socket.send(JSON.stringify(vaultResult));
      }
      if (event.type === "agent_run.completed") {
        clearTimeout(timeout);
        socket.off("message", onMessage);
        resolve();
      }
    });
  });
  socket.send(JSON.stringify({
    type: "agent_run.start",
    protocolVersion: 1,
    eventId: "tool-result-start",
    conversationId: "conversation-tool-result-run",
    agentRunId: "tool-result-run",
    sequence: 0,
    model: "fake-interview-model",
    input: { role: "user", text: "vault_read notes/a.md" },
  }));
  await completed;
  socket.send(JSON.stringify(vaultResult));
  await new Promise((resolve) => setTimeout(resolve, 50));
  assert.equal(socket.readyState, WebSocket.OPEN);

  const closed = once(socket, "close");
  socket.send(JSON.stringify({
    ...vaultResult,
    result: { ok: false, error: { code: "tool_error", message: "conflicting duplicate" } },
  }));
  const [code, reason] = await closed;
  assert.equal(code, 1008);
  assert.match(reason.toString("utf8"), /conflicting tool result/i);
});

test("Vault Change recovery uses WebSocket and its former HTTP endpoint is absent", async (t) => {
  const directory = await mkdtemp(path.join(os.tmpdir(), "offeragent-protocol-vault-"));
  const instance = await startRuntime(path.join(directory, "state.db"), "protocol-vault-token");
  t.after(async () => {
    await stopRuntime(instance);
    await rm(directory, { recursive: true, force: true });
  });
  assert.equal(await httpStatus(instance, "/vault-changes?states=applied"), 404);
  const socket = await connect(instance);
  const command = {
    type: "vault_changes.list",
    protocolVersion: 1,
    eventId: "vault-list-command",
    conversationId: "vault-change-management",
    agentRunId: "vault-list-request",
    sequence: 0,
    states: ["applied"],
  };
  const listed = waitForMessage(socket, (message) => message.type === "vault_changes.listed");
  socket.send(JSON.stringify(command));
  assert.deepEqual((await listed).batches, []);

  const proposal = {
    batchId: "stable-batch-decision",
    idempotencyKey: "stable-batch-key",
    task: "test stable batch decisions",
    actions: [{
      operation: "append",
      path: "notes/stable.md",
      expectedVersion: "mtime:1:size:1",
      content: "next",
    }],
  };
  const runFinished = new Promise((resolve, reject) => {
    const timeout = setTimeout(() => reject(new Error("Vault decision setup timed out")), 5_000);
    socket.on("message", function onMessage(data) {
      const event = JSON.parse(data.toString("utf8"));
      if (event.agentRunId !== "vault-decision-run") return;
      if (respondToRequiredPrelude(socket, event)) return;
      if (event.type === "tool_call.requested" && event.tool.name === "vault_propose_changes") {
        socket.send(JSON.stringify({
          type: "tool_result",
          protocolVersion: 1,
          eventId: "vault-decision-result",
          conversationId: event.conversationId,
          agentRunId: event.agentRunId,
          sequence: event.sequence,
          toolCallId: event.toolCallId,
          result: {
            ok: true,
            value: {
              type: "vault_propose_changes",
              batchId: proposal.batchId,
              decision: "applied",
              checkpointRef: `refs/offeragent/checkpoints/${proposal.batchId}`,
              targets: [{
                path: "notes/stable.md",
                beforeHash: "sha256:before",
                afterHash: "sha256:after",
              }],
            },
          },
        }));
      }
      if (event.type === "agent_run.completed") {
        clearTimeout(timeout);
        socket.off("message", onMessage);
        resolve();
      }
    });
  });
  socket.send(JSON.stringify({
    type: "agent_run.start",
    protocolVersion: 1,
    eventId: "vault-decision-start",
    conversationId: "vault-decision-conversation",
    agentRunId: "vault-decision-run",
    sequence: 0,
    model: "fake-interview-model",
    input: { role: "user", text: `vault_propose_changes ${JSON.stringify(proposal)}` },
  }));
  await runFinished;

  const decision = {
    type: "vault_changes.state",
    protocolVersion: 1,
    eventId: "stable-vault-decision-command",
    conversationId: "vault-change-management",
    agentRunId: "stable-vault-decision-request",
    sequence: 0,
    batchId: proposal.batchId,
    state: "expired",
  };
  const stored = waitForMessage(socket, (message) => message.type === "vault_changes.state_stored");
  socket.send(JSON.stringify(decision));
  assert.equal((await stored).state, "expired");
  const conflictingPayload = once(socket, "close");
  socket.send(JSON.stringify({ ...decision, state: "undone" }));
  const [code, reason] = await conflictingPayload;
  assert.equal(code, 1008);
  assert.match(reason.toString("utf8"), /conflicting duplicate protocol event identity/i);
});

test("read-only protocol commands reject altered reuse without caching snapshots", async (t) => {
  const directory = await mkdtemp(path.join(os.tmpdir(), "offeragent-protocol-read-id-"));
  const instance = await startRuntime(path.join(directory, "state.db"), "protocol-read-id-token");
  t.after(async () => {
    await stopRuntime(instance);
    await rm(directory, { recursive: true, force: true });
  });
  const socket = await connect(instance);
  const command = {
    type: "vault_changes.list",
    protocolVersion: 1,
    eventId: "stable-read-command",
    conversationId: "vault-change-management",
    agentRunId: "stable-read-request",
    sequence: 0,
    states: ["applied"],
  };
  const listed = waitForMessage(socket, (message) => message.type === "vault_changes.listed");
  socket.send(JSON.stringify(command));
  assert.deepEqual((await listed).batches, []);
  const closed = once(socket, "close");
  socket.send(JSON.stringify({ ...command, states: ["pending"] }));
  const [code, reason] = await closed;
  assert.equal(code, 1008);
  assert.match(reason.toString("utf8"), /conflicting duplicate protocol event identity/i);
});

test("duplicate Conversation commands replay one stable cached response across restart", async (t) => {
  const directory = await mkdtemp(path.join(os.tmpdir(), "offeragent-protocol-command-"));
  const statePath = path.join(directory, "state.db");
  let instance = await startRuntime(statePath, "protocol-command-token-one");
  t.after(async () => {
    await stopRuntime(instance);
    await rm(directory, { recursive: true, force: true });
  });
  const command = {
    type: "conversation.create",
    protocolVersion: 1,
    eventId: "stable-conversation-command",
    conversationId: "stable-conversation",
    agentRunId: "stable-conversation-request",
    sequence: 0,
    title: "Stable conversation",
    model: "fake-interview-model",
  };
  const socket = await connect(instance);
  const firstResponse = waitForMessage(socket, (message) => message.type === "conversation.created");
  socket.send(JSON.stringify(command));
  const first = await firstResponse;
  const duplicateResponse = waitForMessage(socket, (message) => message.type === "conversation.created");
  socket.send(JSON.stringify(command));
  const duplicate = await duplicateResponse;
  assert.equal(duplicate.eventId, first.eventId);
  socket.close();
  await once(socket, "close");
  await stopRuntime(instance);

  instance = await startRuntime(statePath, "protocol-command-token-two");
  const restartedSocket = await connect(instance);
  const restartedResponse = waitForMessage(
    restartedSocket,
    (message) => message.type === "conversation.created",
  );
  restartedSocket.send(JSON.stringify(command));
  assert.equal((await restartedResponse).eventId, first.eventId);

  const conflictingIdentity = once(restartedSocket, "close");
  restartedSocket.send(JSON.stringify({
    ...command,
    model: "different-model",
  }));
  const [code, reason] = await conflictingIdentity;
  assert.equal(code, 1008);
  assert.match(reason.toString("utf8"), /conflicting duplicate protocol event identity/i);
});

test("Conversation deletion purges cached snapshots instead of resurrecting history", async (t) => {
  const directory = await mkdtemp(path.join(os.tmpdir(), "offeragent-protocol-delete-"));
  const statePath = path.join(directory, "state.db");
  let instance = await startRuntime(statePath, "protocol-delete-token");
  t.after(async () => {
    await stopRuntime(instance);
    await rm(directory, { recursive: true, force: true });
  });
  const socket = await connect(instance);
  const base = {
    protocolVersion: 1,
    conversationId: "deleted-conversation",
    sequence: 0,
  };
  const create = {
    ...base,
    type: "conversation.create",
    eventId: "create-deleted-conversation",
    agentRunId: "create-deleted-request",
    title: "Delete me",
    model: "fake-interview-model",
  };
  let response = waitForMessage(socket, (message) => message.agentRunId === create.agentRunId);
  socket.send(JSON.stringify(create));
  assert.equal((await response).type, "conversation.created");

  const open = {
    ...base,
    type: "conversation.open",
    eventId: "open-deleted-conversation",
    agentRunId: "open-deleted-request",
  };
  response = waitForMessage(socket, (message) => message.agentRunId === open.agentRunId);
  socket.send(JSON.stringify(open));
  assert.equal((await response).type, "conversation.snapshot");

  const remove = {
    ...base,
    type: "conversation.delete",
    eventId: "delete-deleted-conversation",
    agentRunId: "delete-deleted-request",
  };
  response = waitForMessage(socket, (message) => message.agentRunId === remove.agentRunId);
  socket.send(JSON.stringify(remove));
  assert.equal((await response).type, "conversation.deleted");

  response = waitForMessage(socket, (message) => message.agentRunId === open.agentRunId);
  socket.send(JSON.stringify(open));
  assert.equal((await response).type, "conversation.error");
  socket.close();
  await once(socket, "close");
  await stopRuntime(instance);

  instance = await startRuntime(statePath, "protocol-delete-restart-token");
  const restartedSocket = await connect(instance);
  const delayedCreateClosed = once(restartedSocket, "close");
  restartedSocket.send(JSON.stringify(create));
  const [code, reason] = await delayedCreateClosed;
  assert.equal(code, 1008);
  assert.match(reason.toString("utf8"), /conflicting duplicate protocol event identity/i);

  const verificationSocket = await connect(instance);
  const verification = waitForMessage(
    verificationSocket,
    (message) => message.agentRunId === "verify-deleted-request",
  );
  verificationSocket.send(JSON.stringify({
    ...open,
    eventId: "verify-deleted-conversation",
    agentRunId: "verify-deleted-request",
  }));
  assert.equal((await verification).type, "conversation.error");
  verificationSocket.close();
});

test("a protocol-version mismatch closes with an actionable diagnostic", async (t) => {
  const directory = await mkdtemp(path.join(os.tmpdir(), "offeragent-protocol-version-"));
  const instance = await startRuntime(path.join(directory, "state.db"), "protocol-version-token");
  t.after(async () => {
    await stopRuntime(instance);
    await rm(directory, { recursive: true, force: true });
  });
  const invalidSocket = await connect(instance);
  const invalidClosed = once(invalidSocket, "close");
  invalidSocket.send(JSON.stringify({
    type: "vault_changes.list",
    protocolVersion: 1,
    eventId: "duplicate-vault-states",
    conversationId: "vault-change-management",
    agentRunId: "duplicate-vault-states-request",
    sequence: 0,
    states: ["applied", "applied"],
  }));
  const [invalidCode, invalidReason] = await invalidClosed;
  assert.equal(invalidCode, 1008);
  assert.match(invalidReason.toString("utf8"), /unsupported protocol message/i);

  const socket = await connect(instance);
  const versionClosed = once(socket, "close");
  socket.send(JSON.stringify({
    type: "conversation.list",
    protocolVersion: 999,
    eventId: "wrong-version",
    conversationId: "conversation-management",
    agentRunId: "wrong-version-request",
    sequence: 0,
  }));
  const [code, reason] = await versionClosed;
  assert.equal(code, 1002);
  assert.match(reason.toString("utf8"), /protocol version mismatch/i);
});

test("a Runtime restart rotates instance identity and rejects its prior token", async (t) => {
  const directory = await mkdtemp(path.join(os.tmpdir(), "offeragent-protocol-token-"));
  const statePath = path.join(directory, "state.db");
  const first = await startRuntime(statePath, "first-one-time-token");
  await stopRuntime(first);
  const second = await startRuntime(statePath, "second-one-time-token");
  t.after(async () => {
    await stopRuntime(second);
    await rm(directory, { recursive: true, force: true });
  });
  assert.notEqual(second.handshake.instanceId, first.handshake.instanceId);
  assert.equal("token" in second.handshake, false);
  assert.equal(await httpStatus(second, "/health", "first-one-time-token"), 401);
  assert.equal(await httpStatus(second, "/health"), 200);
});
