import assert from "node:assert/strict";
import { spawn } from "node:child_process";
import { once } from "node:events";
import { request } from "node:http";
import { mkdtemp, readFile, rm } from "node:fs/promises";
import os from "node:os";
import path from "node:path";
import test from "node:test";
import { fileURLToPath } from "node:url";
import initSqlJs from "sql.js/dist/sql-asm.js";
import WebSocket from "ws";

const repositoryRoot = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..");
const runtimeEntry = path.join(repositoryRoot, "packages", "runtime", "dist", "cli.js");

function handshake(stream) {
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

async function stopRuntime(runtime, port, token) {
  const exited = once(runtime, "exit");
  await new Promise((resolve, reject) => {
    const outgoing = request(
      {
        host: "127.0.0.1",
        port,
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

function runWithToolPeer(socket, requestPayload, resultForCall) {
  return new Promise((resolve, reject) => {
    const events = [];
    const timeout = setTimeout(() => reject(new Error("Agent tool loop timed out")), 10_000);
    socket.on("message", function onMessage(data) {
      const event = JSON.parse(data.toString("utf8"));
      if (event.agentRunId !== requestPayload.agentRunId) return;
      events.push(event);
      if (event.type === "tool_call.requested") {
        socket.send(
          JSON.stringify({
            type: "tool_result",
            protocolVersion: 1,
            eventId: `result-${event.toolCallId}`,
            conversationId: event.conversationId,
            agentRunId: event.agentRunId,
            sequence: event.sequence,
            toolCallId: event.toolCallId,
            result: resultForCall(event),
          }),
        );
      }
      if (event.type === "agent_run.completed" || event.type === "agent_run.failed") {
        clearTimeout(timeout);
        socket.off("message", onMessage);
        resolve(events);
      }
    });
    socket.send(JSON.stringify(requestPayload));
  });
}

test("the real Runtime executes local Vault tools through a simulated plugin peer", async (t) => {
  const temporaryDirectory = await mkdtemp(path.join(os.tmpdir(), "offeragent-tool-loop-"));
  const statePath = path.join(temporaryDirectory, "state.db");
  const token = "tool-loop-token";
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
  t.after(async () => {
    if (runtime.exitCode === null) runtime.kill();
    await rm(temporaryDirectory, { recursive: true, force: true });
  });
  const ready = await handshake(runtime.stdout);
  const socket = new WebSocket(`ws://127.0.0.1:${ready.port}/events`, {
    headers: { authorization: `Bearer ${token}` },
  });
  await once(socket, "open");

  const successfulEvents = await runWithToolPeer(
    socket,
    {
      type: "agent_run.start",
      protocolVersion: 1,
      eventId: "tool-run-start",
      conversationId: "tool-conversation",
      agentRunId: "tool-run-success",
      sequence: 0,
      model: "fake-interview-model",
      input: { role: "user", text: "vault_read notes/interview.md 2-3" },
    },
    () => ({
      ok: true,
      value: {
        type: "vault_read",
        path: "notes/interview.md",
        lineStart: 2,
        lineEnd: 3,
        modifiedVersion: "mtime:1234:size:25",
        contentHash: "sha256:source-hash",
        content: "second\nthird",
        truncated: true,
      },
    }),
  );
  assert.deepEqual(
    successfulEvents.map((event) => event.type),
    [
      "agent_run.started",
      "tool_call.requested",
      "tool_call.completed",
      "agent_run.delta",
      "agent_run.delta",
      "agent_run.completed",
    ],
  );
  assert.equal(successfulEvents[1].tool.name, "vault_read");
  assert.equal(successfulEvents[2].status, "completed");
  assert.match(successfulEvents.at(-1).output.text, /second\\nthird/);

  const failedToolEvents = await runWithToolPeer(
    socket,
    {
      type: "agent_run.start",
      protocolVersion: 1,
      eventId: "tool-error-run-start",
      conversationId: "tool-conversation",
      agentRunId: "tool-run-error",
      sequence: 0,
      model: "fake-interview-model",
      input: { role: "user", text: "vault_read ../outside.md" },
    },
    () => ({
      ok: false,
      error: { code: "invalid_path", message: "The requested Vault path is invalid." },
    }),
  );
  assert.equal(failedToolEvents.find((event) => event.type === "tool_call.completed").status, "failed");
  assert.equal(failedToolEvents.at(-1).type, "agent_run.completed");

  const snapshot = new Promise((resolve) => {
    socket.on("message", function onMessage(data) {
      const event = JSON.parse(data.toString("utf8"));
      if (event.type !== "conversation.snapshot" || event.agentRunId !== "tool-snapshot") return;
      socket.off("message", onMessage);
      resolve(event);
    });
  });
  socket.send(
    JSON.stringify({
      type: "conversation.open",
      protocolVersion: 1,
      eventId: "tool-snapshot-request",
      conversationId: "tool-conversation",
      agentRunId: "tool-snapshot",
      sequence: 0,
    }),
  );
  assert.deepEqual(
    (await snapshot).toolCalls.map(({ name, status }) => ({ name, status })),
    [
      { name: "vault_read", status: "completed" },
      { name: "vault_read", status: "failed" },
    ],
  );

  await stopRuntime(runtime, ready.port, token);
  const SQL = await initSqlJs();
  const database = new SQL.Database(await readFile(statePath));
  assert.deepEqual(
    database.exec("SELECT status FROM tool_calls ORDER BY agent_run_id")[0].values,
    [["failed"], ["completed"]],
  );
  assert.deepEqual(
    database.exec(
      `SELECT path, line_start, line_end, modified_version, content_hash, content
       FROM evidence_snapshots`,
    )[0].values,
    [["notes/interview.md", 2, 3, "mtime:1234:size:25", "sha256:source-hash", "second\nthird"]],
  );
  assert.equal(
    (await readFile(statePath)).includes(Buffer.from("first\nsecond\nthird\nfourth")),
    false,
  );
  database.run("PRAGMA foreign_keys = ON");
  database.run("DELETE FROM conversations WHERE id = 'tool-conversation'");
  for (const table of ["tool_calls", "evidence_snapshots"]) {
    assert.equal(database.exec(`SELECT COUNT(*) FROM ${table}`)[0].values[0][0], 0);
  }
  database.close();
});

test("a real plugin socket drop terminalizes its pending tool call as disconnected", async (t) => {
  const temporaryDirectory = await mkdtemp(path.join(os.tmpdir(), "offeragent-tool-drop-"));
  const statePath = path.join(temporaryDirectory, "state.db");
  const token = "tool-drop-token";
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
  t.after(async () => {
    if (runtime.exitCode === null) runtime.kill();
    await rm(temporaryDirectory, { recursive: true, force: true });
  });
  const ready = await handshake(runtime.stdout);
  const socket = new WebSocket(`ws://127.0.0.1:${ready.port}/events`, {
    headers: { authorization: `Bearer ${token}` },
  });
  await once(socket, "open");
  const requested = new Promise((resolve) => {
    socket.on("message", function onMessage(data) {
      const event = JSON.parse(data.toString("utf8"));
      if (event.agentRunId !== "tool-drop-run" || event.type !== "tool_call.requested") return;
      socket.off("message", onMessage);
      resolve();
    });
  });
  socket.send(
    JSON.stringify({
      type: "agent_run.start",
      protocolVersion: 1,
      eventId: "tool-drop-start",
      conversationId: "tool-drop-conversation",
      agentRunId: "tool-drop-run",
      sequence: 0,
      model: "fake-interview-model",
      input: { role: "user", text: "vault_read notes/drop.md" },
    }),
  );
  await requested;
  socket.close();
  await once(socket, "close");
  await new Promise((resolve) => setTimeout(resolve, 100));
  await stopRuntime(runtime, ready.port, token);

  const SQL = await initSqlJs();
  const database = new SQL.Database(await readFile(statePath));
  assert.deepEqual(
    database.exec(
      "SELECT status, error_code FROM tool_calls WHERE agent_run_id = 'tool-drop-run'",
    )[0].values,
    [["failed", "plugin_disconnected"]],
  );
  assert.deepEqual(
    database.exec(
      "SELECT event_type FROM durable_events WHERE agent_run_id = 'tool-drop-run' ORDER BY sequence",
    )[0].values,
    [
      ["agent_run.started"],
      ["tool_call.requested"],
      ["tool_call.completed"],
      ["agent_run.interrupted"],
    ],
  );
  database.close();
});
