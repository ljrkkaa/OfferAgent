import assert from "node:assert/strict";
import { spawn } from "node:child_process";
import { once } from "node:events";
import { mkdtemp, readFile, rm, writeFile } from "node:fs/promises";
import { createServer, request } from "node:http";
import os from "node:os";
import path from "node:path";
import test from "node:test";
import { fileURLToPath } from "node:url";
import initSqlJs from "sql.js/dist/sql-asm.js";
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

function waitForEvent(socket, predicate, timeoutMs = 5_000) {
  return new Promise((resolve, reject) => {
    const timeout = setTimeout(() => {
      socket.off("message", onMessage);
      reject(new Error("Timed out waiting for Runtime event"));
    }, timeoutMs);
    function onMessage(data) {
      const event = JSON.parse(data.toString("utf8"));
      if (!predicate(event)) return;
      clearTimeout(timeout);
      socket.off("message", onMessage);
      resolve(event);
    }
    socket.on("message", onMessage);
  });
}

async function startRuntime(statePath, token) {
  const runtime = spawn(
    process.execPath,
    [
      runtimeEntry,
      "--port",
      "0",
      "--token",
      token,
      "--parent-pid",
      `${process.pid}`,
      "--provider",
      "fake",
      "--state-path",
      statePath,
    ],
    {
      env: { ...process.env, OPENAI_API_KEY: "must-never-enter-runtime-state" },
      stdio: ["ignore", "pipe", "pipe"],
      windowsHide: true,
    },
  );
  const handshake = await readHandshake(runtime.stdout);
  const socket = await connectRuntimeSocket(handshake, token);
  return { runtime, handshake, socket };
}

async function connectRuntimeSocket(handshake, token) {
  const socket = new WebSocket(`ws://127.0.0.1:${handshake.port}/events`, {
    headers: { authorization: `Bearer ${token}` },
  });
  await once(socket, "open");
  return socket;
}

async function stopRuntime(instance, token) {
  const exited = once(instance.runtime, "exit");
  await new Promise((resolve, reject) => {
    const outgoing = request(
      {
        host: "127.0.0.1",
        port: instance.handshake.port,
        path: "/shutdown",
        method: "POST",
        headers: { authorization: `Bearer ${token}` },
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

test("a Conversation and its completed Agent Run survive restart and delete transactionally", async (t) => {
  const temporaryDirectory = await mkdtemp(path.join(os.tmpdir(), "offeragent-state-"));
  const statePath = path.join(temporaryDirectory, "state.db");
  const conversationId = "conversation-persistence-1";
  const agentRunId = "agent-run-persistence-1";
  const token = "conversation-persistence-token";
  let instance = await startRuntime(statePath, token);
  t.after(async () => {
    if (instance.runtime.exitCode === null) instance.runtime.kill();
    await rm(temporaryDirectory, { recursive: true, force: true });
  });

  const created = waitForEvent(instance.socket, (event) => event.type === "conversation.created");
  instance.socket.send(
    JSON.stringify({
      type: "conversation.create",
      protocolVersion: 1,
      eventId: "create-conversation-event",
      conversationId,
      agentRunId: "conversation-management",
      sequence: 0,
      title: "Interview preparation",
      model: "fake-interview-model",
    }),
  );
  assert.deepEqual((await created).conversation, {
    id: conversationId,
    title: "Interview preparation",
    modelId: "fake-interview-model",
  });

  const completed = waitForEvent(
    instance.socket,
    (event) => event.type === "agent_run.completed" && event.agentRunId === agentRunId,
  );
  instance.socket.send(
    JSON.stringify({
      type: "agent_run.start",
      protocolVersion: 1,
      eventId: "persistent-run-start",
      conversationId,
      agentRunId,
      sequence: 0,
      model: "fake-interview-model",
      input: { role: "user", text: "Persist this exchange." },
    }),
  );
  await completed;

  const updated = waitForEvent(instance.socket, (event) => event.type === "conversation.updated");
  instance.socket.send(
    JSON.stringify({
      type: "conversation.update",
      protocolVersion: 1,
      eventId: "update-conversation-event",
      conversationId,
      agentRunId: "conversation-management",
      sequence: 0,
      model: "persisted-model-choice",
    }),
  );
  assert.equal((await updated).conversation.modelId, "persisted-model-choice");
  await stopRuntime(instance, token);

  const SQL = await initSqlJs();
  const withCheckpoint = new SQL.Database(await readFile(statePath));
  withCheckpoint.run(
    `INSERT INTO run_checkpoints
      (id, conversation_id, agent_run_id, checkpoint_json, created_at)
     VALUES (?, ?, ?, ?, ?)`,
    ["future-checkpoint", conversationId, agentRunId, "{}", new Date().toISOString()],
  );
  await writeFile(statePath, withCheckpoint.export());
  withCheckpoint.close();
  assert.equal(
    (await readFile(statePath)).includes(Buffer.from("must-never-enter-runtime-state")),
    false,
  );

  instance = await startRuntime(statePath, token);
  const disconnected = once(instance.socket, "close");
  instance.socket.close();
  await disconnected;
  const reconnectPeers = Array.from({ length: 5 }, (_, index) => `conversation-management-${index}`);
  const reconnectSockets = await Promise.all(
    reconnectPeers.map(() => connectRuntimeSocket(instance.handshake, token)),
  );
  const snapshots = reconnectPeers.map((peer, index) =>
    waitForEvent(
      reconnectSockets[index],
      (event) => event.type === "conversation.snapshot" && event.agentRunId === peer,
    ),
  );
  for (const [index, peer] of reconnectPeers.entries()) {
    reconnectSockets[index].send(
      JSON.stringify({
        type: "conversation.open",
        protocolVersion: 1,
        eventId: `open-conversation-event-${index}`,
        conversationId,
        agentRunId: peer,
        sequence: 0,
      }),
    );
  }
  const reopenedSnapshots = await Promise.all(snapshots);
  const reopened = reopenedSnapshots[0];
  for (const candidate of reopenedSnapshots) {
    assert.deepEqual(candidate.messages, reopened.messages);
    assert.deepEqual(candidate.agentRuns, reopened.agentRuns);
  }
  assert.equal(reopened.conversation.id, conversationId);
  assert.equal(reopened.conversation.modelId, "persisted-model-choice");
  assert.deepEqual(
    reopened.messages.map(({ role, text }) => ({ role, text })),
    [
      { role: "user", text: "Persist this exchange." },
      { role: "assistant", text: "OfferAgent received: Persist this exchange." },
    ],
  );
  assert.deepEqual(reopened.agentRuns.map(({ id, status }) => ({ id, status })), [
    { id: agentRunId, status: "completed" },
  ]);

  instance.socket = reconnectSockets[0];
  for (const socket of reconnectSockets.slice(1)) socket.close();

  const deleted = waitForEvent(instance.socket, (event) => event.type === "conversation.deleted");
  instance.socket.send(
    JSON.stringify({
      type: "conversation.delete",
      protocolVersion: 1,
      eventId: "delete-conversation-event",
      conversationId,
      agentRunId: "conversation-management",
      sequence: 0,
    }),
  );
  await deleted;
  await stopRuntime(instance, token);

  const database = new SQL.Database(await readFile(statePath));
  for (const table of [
    "conversations",
    "messages",
    "agent_runs",
    "durable_events",
    "run_checkpoints",
    "tool_calls",
    "evidence_snapshots",
  ]) {
    const result = database.exec(`SELECT COUNT(*) AS count FROM ${table}`);
    assert.equal(result[0].values[0][0], 0, `${table} should be empty after cascade deletion`);
  }
  database.close();
});

test("only committed Agent Run boundary events replay with stable identities", async (t) => {
  const temporaryDirectory = await mkdtemp(path.join(os.tmpdir(), "offeragent-event-replay-"));
  const statePath = path.join(temporaryDirectory, "state.db");
  const token = "event-replay-runtime-token";
  const conversationId = "event-replay-conversation";
  const agentRunId = "event-replay-run";
  const instance = await startRuntime(statePath, token);
  t.after(async () => {
    if (instance.runtime.exitCode === null) instance.runtime.kill();
    await rm(temporaryDirectory, { recursive: true, force: true });
  });

  const originalEvents = [];
  const completed = new Promise((resolve) => {
    instance.socket.on("message", function onMessage(data) {
      const event = JSON.parse(data.toString("utf8"));
      if (event.agentRunId !== agentRunId) return;
      originalEvents.push(event);
      if (event.type === "agent_run.completed") {
        instance.socket.off("message", onMessage);
        resolve();
      }
    });
  });
  instance.socket.send(
    JSON.stringify({
      type: "agent_run.start",
      protocolVersion: 1,
      eventId: "event-replay-start",
      conversationId,
      agentRunId,
      sequence: 0,
      model: "fake-interview-model",
      input: { role: "user", text: "Replay me." },
    }),
  );
  await completed;
  const firstClosed = once(instance.socket, "close");
  instance.socket.close();
  await firstClosed;

  const replaySocket = await connectRuntimeSocket(instance.handshake, token);
  const replayedEvents = [];
  await new Promise((resolve) => {
    replaySocket.on("message", function onMessage(data) {
      const event = JSON.parse(data.toString("utf8"));
      if (event.agentRunId !== agentRunId) return;
      replayedEvents.push(event);
      if (event.type === "agent_run.completed") {
        replaySocket.off("message", onMessage);
        resolve();
      }
    });
  });
  assert.deepEqual(
    replayedEvents,
    originalEvents.filter((event) => event.type !== "agent_run.delta"),
  );
  for (const event of replayedEvents) {
    replaySocket.send(
      JSON.stringify({
        type: "event.ack",
        protocolVersion: 1,
        eventId: `ack-${event.eventId}`,
        acknowledgedEventId: event.eventId,
        conversationId: event.conversationId,
        agentRunId: event.agentRunId,
        sequence: event.sequence,
      }),
    );
  }
  await new Promise((resolve) => setTimeout(resolve, 300));
  const replayClosed = once(replaySocket, "close");
  replaySocket.close();
  await replayClosed;

  const acknowledgedSocket = await connectRuntimeSocket(instance.handshake, token);
  const unexpectedReplay = await Promise.race([
    once(acknowledgedSocket, "message").then(() => true),
    new Promise((resolve) => setTimeout(() => resolve(false), 300)),
  ]);
  assert.equal(unexpectedReplay, false);
  instance.socket = acknowledgedSocket;
  await stopRuntime(instance, token);
  const SQL = await initSqlJs();
  const database = new SQL.Database(await readFile(statePath));
  assert.deepEqual(
    database.exec(
      "SELECT event_type FROM durable_events ORDER BY sequence",
    )[0].values,
    [["agent_run.started"], ["agent_run.completed"]],
  );
  assert.equal(
    database.exec(
      "SELECT COUNT(*) FROM durable_events WHERE acknowledged_at IS NULL",
    )[0].values[0][0],
    0,
  );
  database.close();
});

test("an explicitly stopped Agent Run is durably cancelled", async (t) => {
  const temporaryDirectory = await mkdtemp(path.join(os.tmpdir(), "offeragent-cancelled-state-"));
  const statePath = path.join(temporaryDirectory, "state.db");
  const authPath = path.join(temporaryDirectory, "auth.json");
  await writeFile(authPath, JSON.stringify({ tokens: { access_token: "cancel-state-token" } }), "utf8");
  let markUpstreamClosed;
  const upstreamClosed = new Promise((resolve) => (markUpstreamClosed = resolve));
  const upstream = createServer((incoming, response) => {
    if (incoming.url !== "/responses") return response.writeHead(404).end();
    response.on("close", markUpstreamClosed);
    response.writeHead(200, { "content-type": "text/event-stream" });
    response.write('data: {"type":"response.output_text.delta","delta":"partial"}\n\n');
  });
  await new Promise((resolve) => upstream.listen(0, "127.0.0.1", resolve));
  const upstreamPort = upstream.address().port;
  const token = "cancelled-conversation-runtime-token";
  const runtime = spawn(
    process.execPath,
    [
      runtimeEntry,
      "--port",
      "0",
      "--token",
      token,
      "--parent-pid",
      `${process.pid}`,
      "--provider",
      "codex",
      "--state-path",
      statePath,
    ],
    {
      env: {
        ...process.env,
        OFFERAGENT_CODEX_AUTH_FILE: authPath,
        OFFERAGENT_CODEX_BASE_URL: `http://127.0.0.1:${upstreamPort}`,
      },
      stdio: ["ignore", "pipe", "pipe"],
      windowsHide: true,
    },
  );
  let instance = {
    runtime,
    handshake: await readHandshake(runtime.stdout),
    socket: undefined,
  };
  instance.socket = new WebSocket(`ws://127.0.0.1:${instance.handshake.port}/events`, {
    headers: { authorization: `Bearer ${token}` },
  });
  await once(instance.socket, "open");
  t.after(async () => {
    if (instance.runtime.exitCode === null) instance.runtime.kill();
    await new Promise((resolve) => upstream.close(resolve));
    await rm(temporaryDirectory, { recursive: true, force: true });
  });

  const created = waitForEvent(instance.socket, (event) => event.type === "conversation.created");
  instance.socket.send(
    JSON.stringify({
      type: "conversation.create",
      protocolVersion: 1,
      eventId: "cancel-create",
      conversationId: "cancel-conversation",
      agentRunId: "cancel-management",
      sequence: 0,
      title: "Cancellation",
      model: "gpt-5.4",
    }),
  );
  await created;

  const delta = waitForEvent(
    instance.socket,
    (event) => event.type === "agent_run.delta" && event.agentRunId === "cancel-run",
  );
  instance.socket.send(
    JSON.stringify({
      type: "agent_run.start",
      protocolVersion: 1,
      eventId: "cancel-run-start",
      conversationId: "cancel-conversation",
      agentRunId: "cancel-run",
      sequence: 0,
      model: "gpt-5.4",
      input: { role: "user", text: "Stop this request." },
    }),
  );
  await delta;
  const cancelled = waitForEvent(
    instance.socket,
    (event) => event.type === "agent_run.cancelled" && event.agentRunId === "cancel-run",
  );
  instance.socket.send(
    JSON.stringify({
      type: "agent_run.cancel",
      protocolVersion: 1,
      eventId: "cancel-run-command",
      conversationId: "cancel-conversation",
      agentRunId: "cancel-run",
      sequence: 2,
    }),
  );
  await cancelled;
  await upstreamClosed;
  await stopRuntime(instance, token);

  instance = await startRuntime(statePath, token);
  const snapshot = waitForEvent(instance.socket, (event) => event.type === "conversation.snapshot");
  instance.socket.send(
    JSON.stringify({
      type: "conversation.open",
      protocolVersion: 1,
      eventId: "cancel-open",
      conversationId: "cancel-conversation",
      agentRunId: "cancel-management",
      sequence: 0,
    }),
  );
  const reopened = await snapshot;
  assert.deepEqual(reopened.agentRuns.map(({ id, status }) => ({ id, status })), [
    { id: "cancel-run", status: "cancelled" },
  ]);
  assert.deepEqual(reopened.messages.map(({ role, text }) => ({ role, text })), [
    { role: "user", text: "Stop this request." },
  ]);
  await stopRuntime(instance, token);
  const persistedBytes = await readFile(statePath);
  assert.equal(persistedBytes.includes(Buffer.from("cancel-state-token")), false);
  assert.equal(persistedBytes.includes(Buffer.from(token)), false);
  assert.equal(persistedBytes.includes(Buffer.from('"delta":"partial"')), false);
});
