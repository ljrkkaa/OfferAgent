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
      if (event.type === "tool_call.requested" && event.tool.kind === "local") {
        const result =
          event.tool.name === "agent_contract_read"
            ? {
                ok: true,
                value: {
                  type: "agent_contract_read",
                  path: "agent.md",
                  modifiedVersion: "mtime:1:size:24",
                  contentHash: "sha256:test-contract",
                  content: "# Test Agent Contract",
                },
              }
            : resultForCall(event);
        socket.send(
          JSON.stringify({
            type: "tool_result",
            protocolVersion: 1,
            eventId: `result-${event.toolCallId}`,
            conversationId: event.conversationId,
            agentRunId: event.agentRunId,
            sequence: event.sequence,
            toolCallId: event.toolCallId,
            result,
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

function runtimeHealth(port, token) {
  return new Promise((resolve, reject) => {
    const outgoing = request(
      {
        host: "127.0.0.1",
        port,
        path: "/health",
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
      "tool_call.requested",
      "tool_call.completed",
      "agent_run.delta",
      "agent_run.delta",
      "agent_run.completed",
    ],
  );
  assert.equal(successfulEvents[1].tool.name, "agent_contract_read");
  assert.equal(successfulEvents[3].tool.name, "vault_read");
  assert.equal(successfulEvents[4].status, "completed");
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
  assert.equal(
    failedToolEvents.find(
      (event) => event.type === "tool_call.completed" && event.tool.name === "vault_read",
    ).status,
    "failed",
  );
  assert.equal(failedToolEvents.at(-1).type, "agent_run.completed");

  const webReadEvents = await runWithToolPeer(
    socket,
    {
      type: "agent_run.start",
      protocolVersion: 1,
      eventId: "web-read-run-start",
      conversationId: "tool-conversation",
      agentRunId: "web-read-run",
      sequence: 0,
      model: "fake-interview-model",
      input: { role: "user", text: "web_read file:///etc/passwd" },
    },
    () => { throw new Error("Runtime-owned web_read must not reach the plugin peer."); },
  );
  const webReadRequest = webReadEvents.find(
    (event) => event.type === "tool_call.requested" && event.tool.name === "web_read",
  );
  const webReadResult = webReadEvents.find(
    (event) => event.type === "tool_call.completed" && event.tool.name === "web_read",
  );
  assert.ok(webReadRequest, JSON.stringify(webReadEvents));
  assert.ok(webReadResult, JSON.stringify(webReadEvents));
  assert.equal(webReadRequest.tool.kind, "runtime");
  assert.equal(webReadResult.status, "failed");
  assert.equal(webReadResult.error.code, "unsafe_url");
  assert.equal(webReadEvents.at(-1).type, "agent_run.completed");

  const citationLifecycleEvents = await runWithToolPeer(
    socket,
    {
      type: "agent_run.start",
      protocolVersion: 1,
      eventId: "citation-lifecycle-start",
      conversationId: "tool-conversation",
      agentRunId: "citation-lifecycle-run",
      sequence: 0,
      model: "fake-interview-model",
      input: { role: "user", text: "citation_then_tool" },
    },
    () => ({
      ok: true,
      value: {
        type: "vault_read",
        path: "notes/citation.md",
        lineStart: 1,
        lineEnd: 1,
        modifiedVersion: "mtime:2:size:4",
        contentHash: "sha256:citation-evidence",
        content: "fact",
        truncated: false,
      },
    }),
  );
  assert.equal(citationLifecycleEvents.at(-1).output.text, "Final answer without a citation");
  assert.equal("citations" in citationLifecycleEvents.at(-1).output, false);

  const searchEvents = await runWithToolPeer(
    socket,
    {
      type: "agent_run.start",
      protocolVersion: 1,
      eventId: "tool-search-run-start",
      conversationId: "tool-conversation",
      agentRunId: "tool-run-search",
      sequence: 0,
      model: "fake-interview-model",
      input: { role: "user", text: "vault_search attention" },
    },
    () => ({
      ok: true,
      value: {
        type: "vault_search",
        entries: [
          {
            path: "notes/search.md",
            modifiedVersion: "mtime:5678:size:30",
            contentHash: "sha256:changed-source-hash",
            matchTier: "body",
            snippets: [
              {
                lineStart: 2,
                lineEnd: 2,
                content: "attention candidate",
                truncated: false,
              },
            ],
          },
        ],
        truncated: false,
      },
    }),
  );
  assert.equal(
    searchEvents.find(
      (event) => event.type === "tool_call.requested" && event.tool.name === "vault_search",
    ).tool.name,
    "vault_search",
  );
  assert.equal(searchEvents.at(-1).type, "agent_run.completed");

  const skillResourceEvents = await runWithToolPeer(
    socket,
    {
      type: "agent_run.start",
      protocolVersion: 1,
      eventId: "skill-resource-start",
      conversationId: "tool-conversation",
      agentRunId: "tool-run-skill-resource",
      sequence: 0,
      model: "fake-interview-model",
      input: { role: "user", text: "skill_read study references/guide.md" },
    },
    (event) =>
      event.tool.arguments.resource
        ? {
            ok: true,
            value: {
              type: "skill_read",
              skill: "study",
              resource: "references/guide.md",
              path: ".codex/skills/study/references/guide.md",
              modifiedVersion: "mtime:3:size:13",
              contentHash: "sha256:skill-resource",
              content: "bounded guide",
            },
          }
        : {
            ok: true,
            value: {
              type: "skill_read",
              skill: "study",
              resource: "SKILL.md",
              path: ".codex/skills/study/SKILL.md",
              modifiedVersion: "mtime:2:size:32",
              contentHash: "sha256:skill-instructions",
              content: "# Study Skill\nUse bounded evidence.",
            },
          },
  );
  assert.deepEqual(
    skillResourceEvents
      .filter(
        (event) => event.type === "tool_call.requested" && event.tool.name === "skill_read",
      )
      .map((event) => event.tool.arguments),
    [{ skill: "study" }, { skill: "study", resource: "references/guide.md" }],
  );
  assert.match(skillResourceEvents.at(-1).output.text, /bounded guide/);

  for (const decision of ["applied", "rejected"]) {
    const proposal = {
      batchId: `batch-${decision}`,
      idempotencyKey: `batch-key-${decision}`,
      task: `Test ${decision} batch`,
      actions: [
        {
          actionId: `action-${decision}`,
          idempotencyKey: `action-key-${decision}`,
          operation: "append",
          path: "notes/batch.md",
          expectedVersion: "mtime:1234:size:25",
          content: "next\n",
        },
      ],
    };
    const batchEvents = await runWithToolPeer(
      socket,
      {
        type: "agent_run.start",
        protocolVersion: 1,
        eventId: `batch-${decision}-start`,
        conversationId: "tool-conversation",
        agentRunId: `tool-run-batch-${decision}`,
        sequence: 0,
        model: "fake-interview-model",
        input: { role: "user", text: `vault_propose_changes ${JSON.stringify(proposal)}` },
      },
      () => ({
        ok: true,
        value: {
          type: "vault_propose_changes",
          batchId: proposal.batchId,
          decision,
          ...(decision === "applied"
            ? { checkpointRef: `refs/offeragent/checkpoints/${proposal.batchId}` }
            : {}),
          targets: [
            {
              path: "notes/batch.md",
              beforeHash: "sha256:before",
              afterHash: "sha256:after",
            },
          ],
        },
      }),
    );
    assert.equal(
      batchEvents.find(
        (event) =>
          event.type === "tool_call.requested" && event.tool.name === "vault_propose_changes",
      ).tool.arguments.batchId,
      proposal.batchId,
    );
    assert.match(batchEvents.at(-1).output.text, new RegExp(decision));
  }

  const missingContractEvents = await new Promise((resolve, reject) => {
    const events = [];
    const timeout = setTimeout(() => reject(new Error("Missing contract run timed out")), 10_000);
    const onMessage = (data) => {
      const event = JSON.parse(data.toString("utf8"));
      if (event.agentRunId !== "tool-run-missing-contract") return;
      events.push(event);
      if (event.type === "tool_call.requested") {
        assert.equal(event.tool.name, "agent_contract_read");
        socket.send(
          JSON.stringify({
            type: "tool_result",
            protocolVersion: 1,
            eventId: `missing-${event.toolCallId}`,
            conversationId: event.conversationId,
            agentRunId: event.agentRunId,
            sequence: event.sequence,
            toolCallId: event.toolCallId,
            result: {
              ok: false,
              error: { code: "not_found", message: "The root Agent Contract is missing." },
            },
          }),
        );
      }
      if (event.type === "agent_run.failed") {
        clearTimeout(timeout);
        socket.off("message", onMessage);
        resolve(events);
      }
    };
    socket.on("message", onMessage);
    socket.send(
      JSON.stringify({
        type: "agent_run.start",
        protocolVersion: 1,
        eventId: "missing-contract-start",
        conversationId: "tool-conversation",
        agentRunId: "tool-run-missing-contract",
        sequence: 0,
        model: "fake-interview-model",
        input: { role: "user", text: "vault_read notes/must-not-run.md" },
      }),
    );
  });
  assert.deepEqual(
    missingContractEvents
      .filter((event) => event.type === "tool_call.requested")
      .map((event) => event.tool.name),
    ["agent_contract_read"],
  );
  assert.equal(
    missingContractEvents.find((event) => event.type === "tool_call.completed").error.code,
    "not_found",
  );
  assert.equal(missingContractEvents.at(-1).error.code, "instruction_error");

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
    (await snapshot).toolCalls
      .filter(({ name }) => name !== "agent_contract_read")
      .map(({ name, status }) => ({ name, status })),
    [
      { name: "vault_read", status: "completed" },
      { name: "vault_read", status: "failed" },
      { name: "web_read", status: "failed" },
      { name: "vault_read", status: "completed" },
      { name: "vault_search", status: "completed" },
      { name: "skill_read", status: "completed" },
      { name: "skill_read", status: "completed" },
      { name: "vault_propose_changes", status: "completed" },
      { name: "vault_propose_changes", status: "completed" },
    ],
  );

  await stopRuntime(runtime, ready.port, token);
  const SQL = await initSqlJs();
  const database = new SQL.Database(await readFile(statePath));
  assert.deepEqual(
    database.exec(
      "SELECT status FROM tool_calls WHERE name IN ('vault_read', 'vault_search') ORDER BY agent_run_id",
    )[0].values,
    [["completed"], ["failed"], ["completed"], ["completed"]],
  );
  assert.equal(
    database.exec("SELECT COUNT(*) FROM tool_calls WHERE name = 'skill_read' AND status = 'completed'")[0]
      .values[0][0],
    2,
  );
  assert.deepEqual(
    database.exec(
      `SELECT id, idempotency_key, state, checkpoint_ref
       FROM vault_change_batches ORDER BY created_at, id`,
    )[0].values,
    [
      [
        "batch-applied",
        "batch-key-applied",
        "applied",
        "refs/offeragent/checkpoints/batch-applied",
      ],
      ["batch-rejected", "batch-key-rejected", "rejected", null],
    ],
  );
  assert.deepEqual(
    database.exec(
      `SELECT path, line_start, line_end, modified_version, content_hash, content,
              is_stale, stale_detected_at IS NOT NULL
       FROM evidence_snapshots ORDER BY path`,
    )[0].values,
    [
      ["notes/citation.md", 1, 1, "mtime:2:size:4", "sha256:citation-evidence", "fact", 0, 0],
      ["notes/interview.md", 2, 3, "mtime:1234:size:25", "sha256:source-hash", "second\nthird", 0, 0],
    ],
  );
  assert.equal(database.exec("SELECT COUNT(*) FROM evidence_snapshots")[0].values[0][0], 2);
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

test("a real plugin socket drop leaves an incomplete tool step uncommitted for Resume", async (t) => {
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
      if (event.tool.name === "agent_contract_read") {
        socket.send(
          JSON.stringify({
            type: "tool_result",
            protocolVersion: 1,
            eventId: "tool-drop-contract-result",
            conversationId: event.conversationId,
            agentRunId: event.agentRunId,
            sequence: event.sequence,
            toolCallId: event.toolCallId,
            result: {
              ok: true,
              value: {
                type: "agent_contract_read",
                path: "agent.md",
                modifiedVersion: "mtime:1:size:24",
                contentHash: "sha256:test-contract",
                content: "# Test Agent Contract",
              },
            },
          }),
        );
        return;
      }
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
      "SELECT status, error_code FROM tool_calls WHERE agent_run_id = 'tool-drop-run' AND name = 'vault_read'",
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
      ["tool_call.requested"],
      ["agent_run.interrupted"],
    ],
  );
  database.close();
});

test("stale evidence is removed from Provider input and must be reread before completion", async (t) => {
  const temporaryDirectory = await mkdtemp(path.join(os.tmpdir(), "offeragent-stale-flow-"));
  const statePath = path.join(temporaryDirectory, "state.db");
  const token = "stale-flow-token";
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
  let readCount = 0;
  const events = await runWithToolPeer(
    socket,
    {
      type: "agent_run.start",
      protocolVersion: 1,
      eventId: "stale-flow-start",
      conversationId: "stale-flow-conversation",
      agentRunId: "stale-flow-run",
      sequence: 0,
      model: "fake-interview-model",
      input: { role: "user", text: "stale_evidence_flow notes/changing.md" },
    },
    (event) => {
      if (event.tool.name === "vault_search") {
        return {
          ok: true,
          value: {
            type: "vault_search",
            entries: [
              {
                path: "notes/changing.md",
                modifiedVersion: "mtime:2:size:11",
                contentHash: "sha256:new",
                matchTier: "body",
                snippets: [
                  { lineStart: 1, lineEnd: 1, content: "new content", truncated: false },
                ],
              },
            ],
            truncated: false,
          },
        };
      }
      readCount += 1;
      const current = readCount === 1 ? "old content" : "new content";
      return {
        ok: true,
        value: {
          type: "vault_read",
          path: "notes/changing.md",
          lineStart: 1,
          lineEnd: 1,
          modifiedVersion: `mtime:${readCount}:size:11`,
          contentHash: readCount === 1 ? "sha256:old" : "sha256:new",
          content: current,
          truncated: false,
        },
      };
    },
  );
  assert.deepEqual(
    events
      .filter(
        (event) => event.type === "tool_call.requested" && event.tool.name !== "agent_contract_read",
      )
      .map((event) => event.tool.name),
    ["vault_read", "vault_search", "vault_read"],
  );
  assert.match(events.at(-1).output.text, /new content/);
  assert.doesNotMatch(events.at(-1).output.text, /old content/);
  await stopRuntime(runtime, ready.port, token);

  const SQL = await initSqlJs();
  const database = new SQL.Database(await readFile(statePath));
  assert.deepEqual(
    database.exec(
      `SELECT content, is_stale FROM evidence_snapshots
       WHERE agent_run_id = 'stale-flow-run' ORDER BY created_at, rowid`,
    )[0].values,
    [["old content", 1], ["new content", 0]],
  );
  database.close();
});

test("malformed nested search results close only the offending socket", async (t) => {
  const temporaryDirectory = await mkdtemp(path.join(os.tmpdir(), "offeragent-malformed-search-"));
  const token = "malformed-search-token";
  const runtime = spawn(
    process.execPath,
    [
      runtimeEntry,
      "--port", "0",
      "--token", token,
      "--parent-pid", `${process.pid}`,
      "--provider", "fake",
      "--state-path", path.join(temporaryDirectory, "state.db"),
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
  const closed = once(socket, "close");
  socket.on("message", (data) => {
    const event = JSON.parse(data.toString("utf8"));
    if (event.type !== "tool_call.requested") return;
    if (event.tool.name === "agent_contract_read") {
      socket.send(
        JSON.stringify({
          type: "tool_result",
          protocolVersion: 1,
          eventId: "malformed-contract-result",
          conversationId: event.conversationId,
          agentRunId: event.agentRunId,
          sequence: event.sequence,
          toolCallId: event.toolCallId,
          result: {
            ok: true,
            value: {
              type: "agent_contract_read",
              path: "agent.md",
              modifiedVersion: "mtime:1:size:24",
              contentHash: "sha256:test-contract",
              content: "# Test Agent Contract",
            },
          },
        }),
      );
      return;
    }
    socket.send(
      JSON.stringify({
        type: "tool_result",
        protocolVersion: 1,
        eventId: "malformed-result",
        conversationId: event.conversationId,
        agentRunId: event.agentRunId,
        sequence: event.sequence,
        toolCallId: event.toolCallId,
        result: {
          ok: true,
          value: { type: "vault_search", entries: [null], truncated: false },
        },
      }),
    );
  });
  socket.send(
    JSON.stringify({
      type: "agent_run.start",
      protocolVersion: 1,
      eventId: "malformed-search-start",
      conversationId: "malformed-search-conversation",
      agentRunId: "malformed-search-run",
      sequence: 0,
      model: "fake-interview-model",
      input: { role: "user", text: "vault_search malformed" },
    }),
  );
  const [code] = await closed;
  assert.equal(code, 1008);
  await new Promise((resolve) => setTimeout(resolve, 50));
  assert.equal(runtime.exitCode, null);
  assert.equal(await runtimeHealth(ready.port, token), 200);
  await stopRuntime(runtime, ready.port, token);
});
