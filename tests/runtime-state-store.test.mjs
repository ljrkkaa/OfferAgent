import assert from "node:assert/strict";
import { createRequire } from "node:module";
import { mkdtemp, readFile, rename, rm, writeFile } from "node:fs/promises";
import os from "node:os";
import path from "node:path";
import test from "node:test";
import { fileURLToPath } from "node:url";
import initSqlJs from "sql.js/dist/sql-asm.js";

const repositoryRoot = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..");
const require = createRequire(import.meta.url);
const { CURRENT_SCHEMA_VERSION, RuntimeStateStore } = require(
  path.join(repositoryRoot, "packages", "runtime", "dist", "state-store.js"),
);

test("Runtime State applies explicit schema migrations and rejects newer schemas", async (t) => {
  const temporaryDirectory = await mkdtemp(path.join(os.tmpdir(), "offeragent-migrations-"));
  t.after(() => rm(temporaryDirectory, { recursive: true, force: true }));
  const statePath = path.join(temporaryDirectory, "state.db");
  const store = await RuntimeStateStore.open(statePath);
  await store.close();

  const SQL = await initSqlJs();
  const migrated = new SQL.Database(await readFile(statePath));
  assert.equal(migrated.exec("PRAGMA user_version")[0].values[0][0], CURRENT_SCHEMA_VERSION);
  assert.deepEqual(
    migrated.exec("SELECT version FROM schema_migrations ORDER BY version")[0].values,
    Array.from({ length: CURRENT_SCHEMA_VERSION }, (_, index) => [index + 1]),
  );
  assert.equal(
    migrated.exec("SELECT value FROM settings_metadata WHERE key = 'schema_version'")[0].values[0][0],
    `${CURRENT_SCHEMA_VERSION}`,
  );

  const upgradePath = path.join(temporaryDirectory, "upgrade-from-v1.db");
  const timestamp = new Date().toISOString();
  migrated.run(
    `INSERT INTO conversations (id, title, model_id, created_at, updated_at)
     VALUES ('legacy-conversation', 'Legacy', 'fake-interview-model', ?, ?)`,
    [timestamp, timestamp],
  );
  migrated.run(
    `INSERT INTO agent_runs
      (id, conversation_id, model_id, status, created_at, updated_at)
     VALUES ('legacy-running-run', 'legacy-conversation', 'fake-interview-model', 'running', ?, ?)`,
    [timestamp, timestamp],
  );
  migrated.run(
    `INSERT INTO durable_events
      (id, conversation_id, agent_run_id, event_type, sequence, payload_json, created_at)
     VALUES (?, 'legacy-conversation', 'legacy-running-run', ?, ?, ?, ?)`,
    [
      "legacy-started-event",
      "agent_run.started",
      1,
      JSON.stringify({
        type: "agent_run.started",
        protocolVersion: 1,
        eventId: "legacy-started-event",
        conversationId: "legacy-conversation",
        agentRunId: "legacy-running-run",
        sequence: 1,
        model: "fake-interview-model",
      }),
      timestamp,
    ],
  );
  migrated.run(
    `INSERT INTO durable_events
      (id, conversation_id, agent_run_id, event_type, sequence, payload_json, created_at)
     VALUES (?, 'legacy-conversation', 'legacy-running-run', ?, ?, ?, ?)`,
    [
      "legacy-delta-event",
      "agent_run.delta",
      2,
      JSON.stringify({
        type: "agent_run.delta",
        protocolVersion: 1,
        eventId: "legacy-delta-event",
        conversationId: "legacy-conversation",
        agentRunId: "legacy-running-run",
        sequence: 2,
        delta: "legacy partial text must be removed",
      }),
      timestamp,
    ],
  );
  migrated.run("DROP TABLE vault_change_batches");
  migrated.run("DROP TABLE evidence_snapshots");
  migrated.run("DROP TABLE tool_calls");
  migrated.run("ALTER TABLE agent_runs DROP COLUMN last_sequence");
  migrated.run("DELETE FROM schema_migrations WHERE version >= 2");
  migrated.run("PRAGMA user_version = 1");
  await writeFile(upgradePath, migrated.export());
  migrated.close();
  const upgradedStore = await RuntimeStateStore.open(upgradePath);
  await upgradedStore.close();
  const upgraded = new SQL.Database(await readFile(upgradePath));
  assert.ok(
    upgraded.exec("PRAGMA table_info(agent_runs)")[0].values.some((column) => column[1] === "last_sequence"),
  );
  assert.equal(upgraded.exec("PRAGMA user_version")[0].values[0][0], CURRENT_SCHEMA_VERSION);
  assert.deepEqual(
    upgraded.exec(
      `SELECT event_type, sequence FROM durable_events
       WHERE agent_run_id = 'legacy-running-run' ORDER BY sequence`,
    )[0].values,
    [["agent_run.started", 1], ["agent_run.interrupted", 3]],
  );
  assert.equal(
    upgraded.exec(
      "SELECT last_sequence FROM agent_runs WHERE id = 'legacy-running-run'",
    )[0].values[0][0],
    3,
  );
  assert.equal(
    (await readFile(upgradePath)).includes(Buffer.from("legacy partial text must be removed")),
    false,
  );
  upgraded.close();

  const futurePath = path.join(temporaryDirectory, "future.db");
  const future = new SQL.Database();
  future.run(`PRAGMA user_version = ${CURRENT_SCHEMA_VERSION + 1}`);
  await writeFile(futurePath, future.export());
  future.close();
  await assert.rejects(
    RuntimeStateStore.open(futurePath),
    /newer than supported schema/,
  );
});

test("Runtime State reconstructs legacy compact durable-event envelopes", async (t) => {
  const temporaryDirectory = await mkdtemp(path.join(os.tmpdir(), "offeragent-compact-events-"));
  t.after(() => rm(temporaryDirectory, { recursive: true, force: true }));
  const statePath = path.join(temporaryDirectory, "state.db");
  const store = await RuntimeStateStore.open(statePath);
  await store.beginAgentRun(
    "compact-conversation",
    "compact-run",
    "compact-model",
    "prepare me",
    {
      type: "agent_run.started",
      protocolVersion: 1,
      eventId: "compact-started-event",
      conversationId: "compact-conversation",
      agentRunId: "compact-run",
      sequence: 1,
      model: "compact-model",
    },
  );
  await store.close();

  const SQL = await initSqlJs();
  const legacy = new SQL.Database(await readFile(statePath));
  legacy.run(
    "UPDATE durable_events SET payload_json = ? WHERE id = ?",
    [JSON.stringify({ model: "compact-model" }), "compact-started-event"],
  );
  await writeFile(statePath, legacy.export());
  legacy.close();

  const reopened = await RuntimeStateStore.open(statePath);
  const events = await reopened.listUnacknowledgedEvents();
  assert.deepEqual(events[0], {
    type: "agent_run.started",
    protocolVersion: 1,
    eventId: "compact-started-event",
    conversationId: "compact-conversation",
    agentRunId: "compact-run",
    sequence: 1,
    model: "compact-model",
  });
  assert.equal(events[1].type, "agent_run.interrupted");
  await reopened.close();
});

test("Runtime State repairs protocol columns even when the schema version is current", async (t) => {
  const temporaryDirectory = await mkdtemp(path.join(os.tmpdir(), "offeragent-current-schema-repair-"));
  t.after(() => rm(temporaryDirectory, { recursive: true, force: true }));
  const statePath = path.join(temporaryDirectory, "state.db");
  const store = await RuntimeStateStore.open(statePath);
  await store.close();

  const SQL = await initSqlJs();
  const drifted = new SQL.Database(await readFile(statePath));
  drifted.run(`
    DROP INDEX protocol_responses_by_conversation;
    DROP INDEX protocol_responses_by_owner;
    ALTER TABLE protocol_responses RENAME TO protocol_responses_current_fixture;
    CREATE TABLE protocol_responses (
      request_event_id TEXT PRIMARY KEY,
      request_type TEXT NOT NULL,
      conversation_id TEXT NOT NULL,
      agent_run_id TEXT NOT NULL,
      response_json TEXT,
      created_at TEXT NOT NULL
    );
    DROP TABLE protocol_responses_current_fixture;
    PRAGMA user_version = ${CURRENT_SCHEMA_VERSION};
  `);
  await writeFile(statePath, drifted.export());
  drifted.close();

  const repaired = await RuntimeStateStore.open(statePath);
  await repaired.close();
  const verified = new SQL.Database(await readFile(statePath));
  const columns = verified.exec("PRAGMA table_info(protocol_responses)")[0].values
    .map((column) => column[1]);
  assert.ok(columns.includes("owner_conversation_id"));
  assert.ok(columns.includes("request_hash"));
  verified.close();
});

test("populated v3 tool and evidence tables survive the current control-tool rebuild", async (t) => {
  const temporaryDirectory = await mkdtemp(path.join(os.tmpdir(), "offeragent-v3-evidence-"));
  t.after(() => rm(temporaryDirectory, { recursive: true, force: true }));
  const statePath = path.join(temporaryDirectory, "state.db");
  const store = await RuntimeStateStore.open(statePath);
  await store.beginAgentRun(
    "v3-conversation",
    "v3-run",
    "fake-interview-model",
    "legacy evidence",
  );
  await store.requestToolCall("v3-run", {
    type: "tool_call.requested",
    protocolVersion: 1,
    eventId: "v3-tool-requested",
    conversationId: "v3-conversation",
    agentRunId: "v3-run",
    sequence: 2,
    toolCallId: "v3-tool-call",
    tool: { kind: "local", name: "vault_read", arguments: { path: "notes/v3.md" } },
  });
  await store.completeToolCall(
    "v3-run",
    {
      ok: true,
      value: {
        type: "vault_read",
        path: "notes/v3.md",
        lineStart: 4,
        lineEnd: 5,
        modifiedVersion: "mtime:3:size:14",
        contentHash: "sha256:v3",
        content: "legacy\nevidence",
        truncated: false,
      },
    },
    {
      type: "tool_call.completed",
      protocolVersion: 1,
      eventId: "v3-tool-completed",
      conversationId: "v3-conversation",
      agentRunId: "v3-run",
      sequence: 3,
      toolCallId: "v3-tool-call",
      tool: { kind: "local", name: "vault_read" },
      status: "completed",
    },
  );
  await store.completeAgentRun("v3-run", "done", {
    type: "agent_run.completed",
    protocolVersion: 1,
    eventId: "v3-run-completed",
    conversationId: "v3-conversation",
    agentRunId: "v3-run",
    sequence: 4,
    output: { role: "assistant", text: "done" },
  });
  await store.close();

  const SQL = await initSqlJs();
  const v3 = new SQL.Database(await readFile(statePath));
  v3.run(`
    CREATE TABLE tool_calls_v3_fixture (
      id TEXT PRIMARY KEY,
      conversation_id TEXT NOT NULL REFERENCES conversations(id) ON DELETE CASCADE,
      agent_run_id TEXT NOT NULL REFERENCES agent_runs(id) ON DELETE CASCADE,
      name TEXT NOT NULL CHECK (name IN ('vault_list', 'vault_read')),
      arguments_json TEXT NOT NULL,
      status TEXT NOT NULL CHECK (status IN ('requested', 'completed', 'failed')),
      error_code TEXT,
      error_message TEXT,
      created_at TEXT NOT NULL,
      updated_at TEXT NOT NULL
    );
    INSERT INTO tool_calls_v3_fixture
      SELECT id, conversation_id, agent_run_id, name, arguments_json, status,
             error_code, error_message, created_at, updated_at
      FROM tool_calls;
    CREATE TABLE evidence_snapshots_v3_fixture (
      id TEXT PRIMARY KEY,
      conversation_id TEXT NOT NULL REFERENCES conversations(id) ON DELETE CASCADE,
      agent_run_id TEXT NOT NULL REFERENCES agent_runs(id) ON DELETE CASCADE,
      tool_call_id TEXT NOT NULL UNIQUE REFERENCES tool_calls_v3_fixture(id) ON DELETE CASCADE,
      path TEXT NOT NULL,
      line_start INTEGER NOT NULL,
      line_end INTEGER NOT NULL,
      modified_version TEXT NOT NULL,
      content_hash TEXT NOT NULL,
      content TEXT NOT NULL,
      created_at TEXT NOT NULL
    );
    INSERT INTO evidence_snapshots_v3_fixture
      SELECT id, conversation_id, agent_run_id, tool_call_id, path, line_start, line_end,
             modified_version, content_hash, content, created_at
      FROM evidence_snapshots;
    DROP TABLE vault_change_batches;
    DROP TABLE evidence_snapshots;
    DROP TABLE tool_calls;
    ALTER TABLE tool_calls_v3_fixture RENAME TO tool_calls;
    ALTER TABLE evidence_snapshots_v3_fixture RENAME TO evidence_snapshots;
    CREATE INDEX tool_calls_by_run ON tool_calls(agent_run_id, created_at);
    CREATE INDEX evidence_by_run ON evidence_snapshots(agent_run_id, created_at);
    DELETE FROM schema_migrations WHERE version >= 4;
    UPDATE settings_metadata SET value = '3' WHERE key = 'schema_version';
    PRAGMA user_version = 3;
  `);
  await writeFile(statePath, v3.export());
  v3.close();

  const upgradedStore = await RuntimeStateStore.open(statePath);
  await upgradedStore.close();
  const upgraded = new SQL.Database(await readFile(statePath));
  upgraded.run("PRAGMA foreign_keys = ON");
  assert.equal(
    upgraded.exec("PRAGMA user_version")[0].values[0][0],
    CURRENT_SCHEMA_VERSION,
  );
  assert.deepEqual(
    upgraded.exec("SELECT name, status FROM tool_calls")[0].values,
    [["vault_read", "completed"]],
  );
  assert.deepEqual(
    upgraded.exec(
      `SELECT path, line_start, line_end, content_hash, content, is_stale, stale_detected_at
       FROM evidence_snapshots`,
    )[0].values,
    [["notes/v3.md", 4, 5, "sha256:v3", "legacy\nevidence", 0, null]],
  );
  assert.deepEqual(upgraded.exec("PRAGMA foreign_key_check"), []);
  upgraded.run("DELETE FROM conversations WHERE id = 'v3-conversation'");
  assert.equal(upgraded.exec("SELECT COUNT(*) FROM tool_calls")[0].values[0][0], 0);
  assert.equal(upgraded.exec("SELECT COUNT(*) FROM evidence_snapshots")[0].values[0][0], 0);
  upgraded.close();
});

test("hosted Web Search capability is persisted per backend and model", async (t) => {
  const temporaryDirectory = await mkdtemp(path.join(os.tmpdir(), "offeragent-capability-"));
  t.after(() => rm(temporaryDirectory, { recursive: true, force: true }));
  const statePath = path.join(temporaryDirectory, "state.db");
  let store = await RuntimeStateStore.open(statePath);
  assert.equal(await store.getProviderCapability("codex:test", "model-a", "hosted_web_search"), "unknown");
  await store.setProviderCapability("codex:test", "model-a", "hosted_web_search", "available");
  assert.equal(await store.getProviderCapability("codex:test", "model-a", "hosted_web_search"), "available");
  assert.equal(await store.getProviderCapability("codex:test", "model-b", "hosted_web_search"), "unknown");
  await store.close();

  store = await RuntimeStateStore.open(statePath);
  assert.equal(await store.getProviderCapability("codex:test", "model-a", "hosted_web_search"), "available");
  await store.resetProviderCapability("codex:test", "model-a", "hosted_web_search");
  assert.equal(await store.getProviderCapability("codex:test", "model-a", "hosted_web_search"), "unknown");

  await store.beginAgentRun("web-conversation", "web-run", "model-a", "read web");
  await store.requestToolCall("web-run", {
    type: "tool_call.requested",
    protocolVersion: 1,
    eventId: "web-read-requested",
    conversationId: "web-conversation",
    agentRunId: "web-run",
    sequence: 2,
    toolCallId: "web-read-call",
    tool: { kind: "runtime", name: "web_read", arguments: { url: "https://example.com" } },
  });
  await store.completeToolCall("web-run", {
    ok: false,
    error: { code: "unreadable_content", message: "Page unavailable" },
  }, {
    type: "tool_call.completed",
    protocolVersion: 1,
    eventId: "web-read-completed",
    conversationId: "web-conversation",
    agentRunId: "web-run",
    sequence: 3,
    toolCallId: "web-read-call",
    tool: { kind: "runtime", name: "web_read" },
    status: "failed",
    error: { code: "unreadable_content", message: "Page unavailable" },
  });
  const citation = {
    url: "https://example.com/source",
    title: "Source",
    startIndex: 7,
    endIndex: 10,
  };
  await store.completeAgentRun("web-run", "Answer [1]", {
    type: "agent_run.completed",
    protocolVersion: 1,
    eventId: "web-run-completed",
    conversationId: "web-conversation",
    agentRunId: "web-run",
    sequence: 4,
    output: { role: "assistant", text: "Answer [1]", citations: [citation] },
  });
  const snapshot = await store.getConversation("web-conversation");
  assert.equal(snapshot.toolCalls[0].name, "web_read");
  assert.deepEqual(snapshot.messages.at(-1).citations, [citation]);
  await store.close();
});

test("Project Evidence reads become snapshots and later observations invalidate stale versions", async (t) => {
  const temporaryDirectory = await mkdtemp(path.join(os.tmpdir(), "offeragent-project-state-"));
  t.after(() => rm(temporaryDirectory, { recursive: true, force: true }));
  const statePath = path.join(temporaryDirectory, "state.db");
  const store = await RuntimeStateStore.open(statePath);
  await store.beginAgentRun("project-conversation", "project-run", "fake-interview-model", "Explain cache invalidation");
  const readMarker = "PROJECT-READ-BODY-MUST-ONLY-BE-EVIDENCE";
  const searchMarker = "PROJECT-SEARCH-SNIPPET-MUST-NOT-PERSIST";

  const request = async (id, name, arguments_, result, sequence) => {
    await store.requestToolCall("project-run", {
      type: "tool_call.requested", protocolVersion: 1, eventId: `${id}-requested`,
      conversationId: "project-conversation", agentRunId: "project-run", sequence,
      toolCallId: id, tool: { kind: "local", name, arguments: arguments_ },
    });
    return store.completeToolCall("project-run", { ok: true, value: result }, {
      type: "tool_call.completed", protocolVersion: 1, eventId: `${id}-completed`,
      conversationId: "project-conversation", agentRunId: "project-run", sequence: sequence + 1,
      toolCallId: id, tool: { kind: "local", name }, status: "completed",
    });
  };

  const readResult = {
    type: "project_read", projectId: "offeragent", path: "src/cache.ts",
    evidencePath: "project/offeragent/src/cache.ts", lineStart: 1, lineEnd: 1,
    modifiedVersion: "mtime:1:size:3", contentHash: "sha256:old", content: readMarker, truncated: false,
  };
  const searchResult = {
    type: "project_search", projectId: "offeragent", truncated: false,
    entries: [{
      path: "src/cache.ts", modifiedVersion: "mtime:2:size:3", contentHash: "sha256:new",
      snippets: [{ content: searchMarker, lineStart: 1, lineEnd: 1, truncated: false }],
    }],
  };
  assert.deepEqual(await request("project-read-old", "project_read", {
    projectId: "offeragent", path: "src/cache.ts",
  }, readResult, 2), []);
  assert.deepEqual(await request("project-search-new", "project_search", {
    projectId: "offeragent", query: "new",
  }, searchResult, 4), ["project/offeragent/src/cache.ts"]);

  await store.saveRunCheckpoint("project-run", {
    version: 1,
    input: [
      { type: "user_message", text: "Explain cache invalidation" },
      { type: "local_tool_call", callId: "provider-search", name: "project_search", arguments: { projectId: "offeragent", query: "new" } },
      { type: "local_tool_result", callId: "provider-search", result: { ok: true, value: searchResult } },
      { type: "local_tool_call", callId: "provider-read", name: "project_read", arguments: { projectId: "offeragent", path: "src/cache.ts" } },
      { type: "local_tool_result", callId: "provider-read", result: { ok: true, value: readResult } },
    ],
    localSkills: [], canonicalReadPaths: [["provider-read", "project/offeragent/src/cache.ts"]],
    requiredRereads: [], hostedWebSearchProbeAttempted: false, completedSteps: 2,
  });

  await store.close();
  const SQL = await initSqlJs();
  const database = new SQL.Database(await readFile(statePath));
  assert.deepEqual(database.exec(
    "SELECT path, content_hash, content, is_stale FROM evidence_snapshots",
  )[0].values, [["project/offeragent/src/cache.ts", "sha256:old", readMarker, 1]]);
  const toolResults = database.exec("SELECT result_json FROM tool_calls ORDER BY created_at")[0].values.flat().join("\n");
  const checkpoint = database.exec("SELECT checkpoint_json FROM run_checkpoints")[0].values[0][0];
  assert.equal(toolResults.includes(readMarker), false);
  assert.equal(toolResults.includes(searchMarker), false);
  assert.equal(checkpoint.includes(readMarker), false);
  assert.equal(checkpoint.includes(searchMarker), false);
  const serialized = Buffer.from(await readFile(statePath)).toString("utf8");
  assert.equal(serialized.includes(temporaryDirectory), false);
  assert.equal(serialized.includes(searchMarker), false);
  database.close();
});

test("Runtime State rolls back a failed write and serves consistent concurrent reads", async (t) => {
  const temporaryDirectory = await mkdtemp(path.join(os.tmpdir(), "offeragent-transactions-"));
  t.after(() => rm(temporaryDirectory, { recursive: true, force: true }));
  const statePath = path.join(temporaryDirectory, "state.db");
  let store = await RuntimeStateStore.open(statePath);
  await store.createConversation({
    id: "transaction-conversation",
    title: "Original title",
    modelId: "fake-interview-model",
  });
  await assert.rejects(
    store.createConversation({
      id: "transaction-conversation",
      title: "Must roll back",
      modelId: "different-model",
    }),
    /UNIQUE constraint failed/,
  );

  const write = store.beginAgentRun(
    "transaction-conversation",
    "transaction-run",
    "fake-interview-model",
    "Concurrent request",
  );
  const snapshots = await Promise.all(
    Array.from({ length: 8 }, () => store.getConversation("transaction-conversation")),
  );
  await write;
  for (const snapshot of snapshots) {
    assert.equal(snapshot.conversation.title, "Original title");
    assert.equal(snapshot.agentRuns.length, snapshot.messages.length);
    assert.ok(snapshot.agentRuns.length === 0 || snapshot.agentRuns[0].status === "running");
  }
  const committed = await store.getConversation("transaction-conversation");
  assert.deepEqual(committed.agentRuns.map((run) => run.status), ["running"]);
  assert.deepEqual(committed.messages.map((message) => message.text), ["Concurrent request"]);
  await store.beginAgentRun(
    "transaction-conversation",
    "failed-run",
    "fake-interview-model",
    "This request fails",
  );
  await store.failAgentRun("failed-run", "provider_error", "Provider failed.");
  await store.close();

  store = await RuntimeStateStore.open(statePath);
  const reopened = await store.getConversation("transaction-conversation");
  assert.equal(reopened.conversation.title, "Original title");
  assert.deepEqual(reopened.agentRuns.map((run) => run.status).sort(), ["failed", "interrupted"]);
  assert.deepEqual(
    reopened.agentRuns.find((run) => run.id === "failed-run")?.error,
    { code: "provider_error", message: "Provider failed." },
  );
  await store.close();
});

test("concurrent first runs create their shared Conversation exactly once", async (t) => {
  const temporaryDirectory = await mkdtemp(path.join(os.tmpdir(), "offeragent-concurrent-create-"));
  t.after(() => rm(temporaryDirectory, { recursive: true, force: true }));
  const store = await RuntimeStateStore.open(path.join(temporaryDirectory, "state.db"));

  await Promise.all([
    store.beginAgentRun("shared-conversation", "shared-run-one", "fake-interview-model", "One"),
    store.beginAgentRun("shared-conversation", "shared-run-two", "fake-interview-model", "Two"),
  ]);

  const snapshot = await store.getConversation("shared-conversation");
  assert.equal(snapshot.agentRuns.length, 2);
  assert.deepEqual(snapshot.messages.map((message) => message.text).sort(), ["One", "Two"]);
  await store.close();
});

test("Conversation Context trims interleaved messages as complete Agent Run turns", async (t) => {
  const temporaryDirectory = await mkdtemp(path.join(os.tmpdir(), "offeragent-context-turns-"));
  t.after(() => rm(temporaryDirectory, { recursive: true, force: true }));
  const store = await RuntimeStateStore.open(path.join(temporaryDirectory, "state.db"));
  const olderUser = "A".repeat(20_000);
  const newerUser = "B".repeat(20_000);
  const olderAssistant = "a".repeat(20_000);
  const newerAssistant = "b".repeat(20_000);

  await store.beginAgentRun("interleaved-context", "older-run", "model", olderUser);
  await store.beginAgentRun("interleaved-context", "newer-run", "model", newerUser);
  await store.completeAgentRun("newer-run", newerAssistant);
  await store.completeAgentRun("older-run", olderAssistant);
  await store.beginAgentRun("interleaved-context", "current-run", "model", "current");

  assert.deepEqual(
    await store.getConversationContext("interleaved-context", "current-run"),
    [
      { type: "user_message", text: newerUser },
      { type: "assistant_message", text: newerAssistant },
      { type: "user_message", text: "current" },
    ],
  );
  await store.close();
});

test("Conversation Context remains bounded when completed messages are empty", async (t) => {
  const temporaryDirectory = await mkdtemp(path.join(os.tmpdir(), "offeragent-context-empty-"));
  t.after(() => rm(temporaryDirectory, { recursive: true, force: true }));
  const store = await RuntimeStateStore.open(
    path.join(temporaryDirectory, "state.db"),
    async () => {},
  );
  for (let index = 0; index < 1_000; index += 1) {
    await store.beginAgentRun("empty-context", `empty-run-${index}`, "model", "");
    await store.completeAgentRun(`empty-run-${index}`, "");
  }
  await store.beginAgentRun("empty-context", "empty-current", "model", "current");

  const context = await store.getConversationContext("empty-context", "empty-current");
  assert.ok(context.length < 2_001);
  assert.deepEqual(context.at(-1), { type: "user_message", text: "current" });
  await store.close();
});

test("a failed atomic file replacement restores the pre-transaction in-memory state", async (t) => {
  const temporaryDirectory = await mkdtemp(path.join(os.tmpdir(), "offeragent-persist-failure-"));
  t.after(() => rm(temporaryDirectory, { recursive: true, force: true }));
  const statePath = path.join(temporaryDirectory, "state.db");
  let failPersistence = false;
  const store = await RuntimeStateStore.open(
    statePath,
    async (targetPath, temporaryPath, bytes) => {
      if (failPersistence) throw new Error("simulated atomic replacement failure");
      await writeFile(temporaryPath, bytes);
      await rename(temporaryPath, targetPath);
    },
  );

  failPersistence = true;
  await assert.rejects(
    store.createConversation({
      id: "ghost-conversation",
      title: "Must not survive",
      modelId: "fake-interview-model",
    }),
    /simulated atomic replacement failure/,
  );
  assert.equal(await store.hasConversation("ghost-conversation"), false);

  await assert.rejects(
    store.beginAgentRun(
      "ghost-run-conversation",
      "ghost-run",
      "fake-interview-model",
      "Must roll back with its started event",
    ),
    /simulated atomic replacement failure/,
  );
  assert.equal(await store.hasConversation("ghost-run-conversation"), false);

  failPersistence = false;
  await store.beginAgentRun(
    "atomic-transition-conversation",
    "atomic-transition-run",
    "fake-interview-model",
    "Keep this running",
  );
  failPersistence = true;
  await assert.rejects(
    store.completeAgentRun("atomic-transition-run", "Must not commit"),
    /simulated atomic replacement failure/,
  );
  const afterFailedCompletion = await store.getConversation("atomic-transition-conversation");
  assert.deepEqual(afterFailedCompletion.agentRuns.map((run) => run.status), ["running"]);
  assert.deepEqual(afterFailedCompletion.messages.map((message) => message.text), ["Keep this running"]);

  failPersistence = false;
  await store.close();
  const SQL = await initSqlJs();
  const persisted = new SQL.Database(await readFile(statePath));
  assert.equal(
    persisted.exec(
      "SELECT COUNT(*) FROM durable_events WHERE event_type = 'agent_run.completed'",
    )[0].values[0][0],
    0,
  );
  persisted.close();
  const reopened = await RuntimeStateStore.open(statePath);
  assert.equal(await reopened.hasConversation("ghost-conversation"), false);
  assert.equal(await reopened.hasConversation("ghost-run-conversation"), false);
  await reopened.close();
});

test("restart interruption advances beyond the last emitted live-only delta sequence", async (t) => {
  const temporaryDirectory = await mkdtemp(path.join(os.tmpdir(), "offeragent-sequence-recovery-"));
  t.after(() => rm(temporaryDirectory, { recursive: true, force: true }));
  const statePath = path.join(temporaryDirectory, "state.db");
  let store = await RuntimeStateStore.open(statePath);
  await store.beginAgentRun(
    "sequence-conversation",
    "sequence-run",
    "fake-interview-model",
    "Stream without persisting text",
  );
  await store.advanceAgentRunSequence("sequence-run", 2);
  await store.advanceAgentRunSequence("sequence-run", 3);
  await store.close();

  store = await RuntimeStateStore.open(statePath);
  const events = await store.listUnacknowledgedEvents();
  assert.deepEqual(
    events.map(({ type, sequence }) => ({ type, sequence })),
    [
      { type: "agent_run.started", sequence: 1 },
      { type: "agent_run.interrupted", sequence: 4 },
    ],
  );
  assert.equal(JSON.stringify(events).includes("Stream without persisting text"), false);
  await store.close();
});

test("the Vault Change journal durably records applying metadata without file bodies", async (t) => {
  const temporaryDirectory = await mkdtemp(path.join(os.tmpdir(), "offeragent-change-journal-"));
  t.after(() => rm(temporaryDirectory, { recursive: true, force: true }));
  const statePath = path.join(temporaryDirectory, "state.db");
  let store = await RuntimeStateStore.open(statePath);
  await store.beginAgentRun(
    "journal-conversation",
    "journal-run",
    "fake-interview-model",
    "Prepare a safe update",
  );
  await store.requestToolCall("journal-run", {
    type: "tool_call.requested",
    protocolVersion: 1,
    eventId: "journal-requested",
    conversationId: "journal-conversation",
    agentRunId: "journal-run",
    sequence: 2,
    toolCallId: "journal-call",
    tool: {
      kind: "local",
      name: "vault_propose_changes",
      arguments: {
        batchId: "journal-batch",
        idempotencyKey: "journal-key",
        task: "Update one note",
        actions: [{
          actionId: "journal-action",
          idempotencyKey: "journal-action-key",
          operation: "exact_replace",
          path: "notes/private.md",
          expectedVersion: "mtime:1:size:99",
          expectedContent: "SOURCE-BODY-MUST-NOT-BE-IN-SQLITE",
          replacement: "POST-BODY-MUST-NOT-BE-IN-SQLITE",
        }],
      },
    },
  });
  await store.markVaultChangeApplying(
    "journal-batch",
    "refs/offeragent/checkpoints/journal-batch",
    [{
      path: "notes/private.md",
      beforeHash: "sha256:before",
      afterHash: "sha256:after",
    }],
  );
  await store.close();

  const bytes = await readFile(statePath);
  assert.equal(bytes.includes(Buffer.from("SOURCE-BODY-MUST-NOT-BE-IN-SQLITE")), false);
  assert.equal(bytes.includes(Buffer.from("POST-BODY-MUST-NOT-BE-IN-SQLITE")), false);
  const SQL = await initSqlJs();
  const database = new SQL.Database(bytes);
  assert.equal(
    database.exec("PRAGMA table_info(vault_change_batches)")[0].values
      .some((column) => column[1] === "proposal_json"),
    false,
  );
  assert.deepEqual(
    database.exec(
      `SELECT state, checkpoint_ref, before_hashes_json, after_hashes_json
       FROM vault_change_batches WHERE id = 'journal-batch'`,
    )[0].values,
    [[
      "applying",
      "refs/offeragent/checkpoints/journal-batch",
      JSON.stringify({ "notes/private.md": "sha256:before" }),
      JSON.stringify({ "notes/private.md": "sha256:after" }),
    ]],
  );
  database.close();

  store = await RuntimeStateStore.open(statePath);
  assert.deepEqual(await store.listVaultChangeBatches(["applying"]), [{
    batchId: "journal-batch",
    checkpointRef: "refs/offeragent/checkpoints/journal-batch",
    state: "applying",
    targets: [{
      path: "notes/private.md",
      beforeHash: "sha256:before",
      afterHash: "sha256:after",
    }],
  }]);
  await store.markVaultChangeState("journal-batch", "applied");
  assert.equal((await store.listVaultChangeBatches(["applied"]))[0].state, "applied");
  await store.close();
});

test("Run Checkpoints resume only Interrupted Runs from the latest committed step", async (t) => {
  const temporaryDirectory = await mkdtemp(path.join(os.tmpdir(), "offeragent-run-checkpoint-"));
  t.after(() => rm(temporaryDirectory, { recursive: true, force: true }));
  const statePath = path.join(temporaryDirectory, "state.db");
  let store = await RuntimeStateStore.open(statePath);
  await store.beginAgentRun(
    "checkpoint-conversation",
    "checkpoint-run",
    "fake-interview-model",
    "resume me",
  );
  const first = {
    version: 1,
    input: [{ type: "user_message", text: "resume me" }],
    localSkills: [],
    canonicalReadPaths: [],
    requiredRereads: [],
    hostedWebSearchProbeAttempted: false,
    completedSteps: 0,
  };
  await store.saveRunCheckpoint("checkpoint-run", first);
  const latest = { ...first, completedSteps: 1 };
  await store.saveRunCheckpoint("checkpoint-run", latest);
  await store.interruptAgentRun("checkpoint-run");
  await store.close();

  store = await RuntimeStateStore.open(statePath);
  assert.deepEqual(await store.resumeAgentRun("checkpoint-conversation", "checkpoint-run"), {
    checkpoint: latest,
    model: "fake-interview-model",
    nextSequence: 3,
  });
  assert.equal((await store.getConversation("checkpoint-conversation")).agentRuns[0].status, "running");
  await store.cancelAgentRun("checkpoint-run");
  await assert.rejects(
    store.resumeAgentRun("checkpoint-conversation", "checkpoint-run"),
    /cannot be resumed from 'cancelled'/,
  );
  await store.close();
});

test("Run Checkpoints do not persist Agent Contract, Local Skill, or pending proposal bodies", async (t) => {
  const temporaryDirectory = await mkdtemp(path.join(os.tmpdir(), "offeragent-checkpoint-bodies-"));
  t.after(() => rm(temporaryDirectory, { recursive: true, force: true }));
  const statePath = path.join(temporaryDirectory, "state.db");
  const store = await RuntimeStateStore.open(statePath);
  await store.beginAgentRun("body-conversation", "body-run", "fake-interview-model", "change it");
  const proposalMarker = "PENDING-PROPOSAL-BODY-MUST-NOT-PERSIST";
  const instructionMarker = "CONTROL-INSTRUCTION-BODY-MUST-NOT-PERSIST";
  const attachmentIdMarker = "OPAQUE-ATTACHMENT-ID-MUST-NOT-PERSIST";
  await store.saveRunCheckpoint("body-run", {
    version: 1,
    input: [
      {
        type: "user_message",
        text: "change it",
        attachments: [{
          attachmentId: attachmentIdMarker,
          contentHash: `sha256:${"a".repeat(64)}`,
          fileName: "interview.png",
          mediaType: "image/png",
          order: 0,
          size: 12,
        }],
      },
      {
        type: "local_tool_call",
        callId: "skill-provider-call",
        name: "skill_read",
        arguments: { skill: "study" },
      },
      {
        type: "local_tool_result",
        callId: "skill-provider-call",
        result: {
          ok: true,
          value: {
            type: "skill_read",
            skill: "study",
            resource: "SKILL.md",
            path: ".codex/skills/study/SKILL.md",
            modifiedVersion: "mtime:1:size:1",
            contentHash: "sha256:skill",
            content: instructionMarker,
          },
        },
      },
      {
        type: "local_tool_call",
        callId: "proposal-provider-call",
        name: "vault_propose_changes",
        arguments: {
          batchId: "body-batch",
          idempotencyKey: "body-batch-key",
          task: "Safe metadata",
          actions: [{
            actionId: "body-action",
            idempotencyKey: "body-action-key",
            operation: "create",
            path: "notes/body.md",
            expectedVersion: "missing",
            content: proposalMarker,
          }],
        },
      },
    ],
    agentContract: instructionMarker,
    localSkills: [["study", instructionMarker]],
    canonicalReadPaths: [],
    requiredRereads: [],
    hostedWebSearchProbeAttempted: false,
    completedSteps: 1,
    runInput: {
      text: "change it",
      attachments: [{
        attachmentId: attachmentIdMarker,
        contentHash: `sha256:${"a".repeat(64)}`,
        fileName: "interview.png",
        mediaType: "image/png",
        order: 0,
        size: 12,
      }],
    },
  });
  await store.close();
  const databaseText = (await readFile(statePath)).toString("utf8");
  assert.equal(databaseText.includes(proposalMarker), false);
  assert.equal(databaseText.includes(instructionMarker), false);
  assert.equal(databaseText.includes(attachmentIdMarker), false);
});

test("Research Browser rendered content and enumerated links are ephemeral", async (t) => {
  const temporaryDirectory = await mkdtemp(path.join(os.tmpdir(), "offeragent-research-ephemeral-"));
  t.after(() => rm(temporaryDirectory, { recursive: true, force: true }));
  const statePath = path.join(temporaryDirectory, "state.db");
  const store = await RuntimeStateStore.open(statePath);
  const contentMarker = "HOSTILE-RENDERED-PAGE-MUST-NOT-PERSIST";
  const linkMarker = "https://dynamic.example/private-result-marker";
  const credentialMarker = "OAUTH-CODE-MUST-NOT-PERSIST";
  const sessionMarker = "OAUTH-STATE-MUST-NOT-PERSIST";
  const failureMarker = "ELECTRON-FAILURE-URL-MUST-NOT-PERSIST";
  await store.beginAgentRun("research-conversation", "research-run", "fake-interview-model", "research it");
  const callEvent = {
    type: "tool_call.requested",
    protocolVersion: 1,
    eventId: "research-request-event",
    conversationId: "research-conversation",
    agentRunId: "research-run",
    sequence: 2,
    toolCallId: "research-tool-call",
    tool: {
      kind: "local",
      name: "research_browser",
      arguments: {
        action: "open",
        url: `https://dynamic.example/signin-oidc?code=${credentialMarker}&state=${sessionMarker}&view=interview`,
      },
    },
  };
  await store.requestToolCall("research-run", callEvent);
  const result = {
    ok: true,
    value: {
      type: "research_browser",
      action: "read",
      status: "ready",
      title: "Dynamic interview",
      url: "https://dynamic.example/interview/42",
      content: `${contentMarker}\n${linkMarker}`,
      sourceFingerprint: `sha256:${"a".repeat(64)}`,
      truncated: false,
      untrusted: true,
    },
  };
  await store.completeToolCall("research-run", result, {
    type: "tool_call.completed",
    protocolVersion: 1,
    eventId: "research-completed-event",
    conversationId: "research-conversation",
    agentRunId: "research-run",
    sequence: 3,
    toolCallId: "research-tool-call",
    tool: { kind: "local", name: "research_browser" },
    status: "completed",
  });
  await store.requestToolCall("research-run", {
    ...callEvent,
    eventId: "research-enumerate-request-event",
    sequence: 4,
    toolCallId: "research-enumerate-tool-call",
    tool: { kind: "local", name: "research_browser", arguments: { action: "enumerate", limit: 10 } },
  });
  await store.completeToolCall("research-run", {
    ok: true,
    value: {
      type: "research_browser",
      action: "enumerate",
      status: "ready",
      title: "Dynamic interview search",
      url: "https://dynamic.example/search",
      entries: [{ id: "result-1", title: "Private marker", url: linkMarker }],
      truncated: false,
      untrusted: true,
    },
  }, {
    type: "tool_call.completed",
    protocolVersion: 1,
    eventId: "research-enumerate-completed-event",
    conversationId: "research-conversation",
    agentRunId: "research-run",
    sequence: 5,
    toolCallId: "research-enumerate-tool-call",
    tool: { kind: "local", name: "research_browser" },
    status: "completed",
  });
  await store.requestToolCall("research-run", {
    ...callEvent,
    eventId: "research-failed-request-event",
    sequence: 6,
    toolCallId: "research-failed-tool-call",
    tool: {
      kind: "local",
      name: "research_browser",
      arguments: { action: "open", url: `https://dynamic.example/cb?code=${failureMarker}` },
    },
  });
  await store.completeToolCall("research-run", {
    ok: false,
    error: {
      code: "tool_error",
      message: `ERR_FAILED loading https://dynamic.example/cb?code=${failureMarker}`,
    },
  }, {
    type: "tool_call.completed",
    protocolVersion: 1,
    eventId: "research-failed-completed-event",
    conversationId: "research-conversation",
    agentRunId: "research-run",
    sequence: 7,
    toolCallId: "research-failed-tool-call",
    tool: { kind: "local", name: "research_browser" },
    status: "failed",
    error: {
      code: "tool_error",
      message: `ERR_FAILED loading https://dynamic.example/cb?code=${failureMarker}`,
    },
  });
  await store.saveRunCheckpoint("research-run", {
    version: 1,
    input: [
      { type: "user_message", text: "research it" },
      { type: "local_tool_call", callId: "research-provider-call", name: "research_browser", arguments: { action: "read" } },
      { type: "local_tool_result", callId: "research-provider-call", result },
    ],
    localSkills: [],
    canonicalReadPaths: [],
    requiredRereads: [],
    hostedWebSearchProbeAttempted: false,
    completedSteps: 1,
    pendingToolStep: {
      completedSteps: 1,
      name: "research_browser",
      providerCallId: "research-provider-call",
      toolCallId: "research-tool-call",
    },
  });
  await store.interruptAgentRun("research-run");
  const resumed = await store.resumeAgentRun("research-conversation", "research-run");
  assert.deepEqual(resumed.checkpoint.input, [{ type: "user_message", text: "research it" }]);
  assert.equal(resumed.checkpoint.pendingToolStep, undefined);
  const stored = await store.getToolCallResult("research-tool-call", "research-run");
  assert.equal(stored.value.content, undefined);
  const storedEnumeration = await store.getToolCallResult("research-enumerate-tool-call", "research-run");
  assert.equal(storedEnumeration.value.entries, undefined);
  await store.close();
  const stateBytes = await readFile(statePath);
  const SQL = await initSqlJs();
  const database = new SQL.Database(stateBytes);
  const storedArguments = JSON.parse(database.exec(
    "SELECT arguments_json FROM tool_calls WHERE id = 'research-tool-call'",
  )[0].values[0][0]);
  assert.deepEqual(storedArguments, { action: "open" });
  const storedRequestEvent = JSON.parse(database.exec(
    "SELECT payload_json FROM durable_events WHERE id = 'research-request-event'",
  )[0].values[0][0]);
  assert.deepEqual(storedRequestEvent.tool.arguments, { action: "open" });
  const [storedFailureMessage, storedFailureResult] = database.exec(
    "SELECT error_message, result_json FROM tool_calls WHERE id = 'research-failed-tool-call'",
  )[0].values[0];
  assert.equal(storedFailureMessage, "The Research Browser action failed.");
  assert.equal(
    JSON.parse(storedFailureResult).error.message,
    "The Research Browser action failed.",
  );
  const storedFailureEvent = database.exec(
    "SELECT payload_json FROM durable_events WHERE id = 'research-failed-completed-event'",
  )[0].values[0][0];
  assert.equal(storedFailureEvent.includes(failureMarker), false);
  database.close();
  const databaseText = stateBytes.toString("utf8");
  assert.equal(databaseText.includes(contentMarker), false);
  assert.equal(databaseText.includes(linkMarker), false);
  assert.equal(databaseText.includes(credentialMarker), false);
  assert.equal(databaseText.includes(sessionMarker), false);
  assert.equal(databaseText.includes(failureMarker), false);
});

test("post-response fallback phase survives interruption for direct finalization", async (t) => {
  const temporaryDirectory = await mkdtemp(path.join(os.tmpdir(), "offeragent-post-response-"));
  t.after(() => rm(temporaryDirectory, { recursive: true, force: true }));
  const store = await RuntimeStateStore.open(path.join(temporaryDirectory, "state.db"));
  await store.beginAgentRun("post-response-conversation", "post-response-run", "fake-interview-model", "remember this");
  await store.saveRunCheckpoint("post-response-run", {
    version: 1,
    input: [{ type: "user_message", text: "remember this" }],
    localSkills: [],
    canonicalReadPaths: [],
    requiredRereads: [],
    hostedWebSearchProbeAttempted: false,
    completedSteps: 1,
    dailyPlanApplied: true,
    memoryHandledByMain: true,
    resolvedDailyNotePaths: ["journal/2026-07-14.md"],
    changedMemoryPaths: ["memory/study/current.md"],
    postResponseOutput: "The answer produced before fallback.",
    postResponseCitations: [],
  });
  await store.interruptActiveRuns();
  const resumed = await store.resumeAgentRun("post-response-conversation", "post-response-run");
  assert.equal(resumed.checkpoint.postResponseOutput, "The answer produced before fallback.");
  assert.equal(resumed.checkpoint.memoryHandledByMain, true);
  assert.equal(resumed.checkpoint.dailyPlanApplied, true);
  assert.deepEqual(resumed.checkpoint.resolvedDailyNotePaths, ["journal/2026-07-14.md"]);
  assert.deepEqual(resumed.checkpoint.changedMemoryPaths, ["memory/study/current.md"]);
  assert.equal(resumed.checkpoint.pendingToolStep, undefined);
  await store.close();
});

test("a committed Tool Result remains recoverable when the following checkpoint write is lost", async (t) => {
  const temporaryDirectory = await mkdtemp(path.join(os.tmpdir(), "offeragent-committed-tool-gap-"));
  t.after(() => rm(temporaryDirectory, { recursive: true, force: true }));
  const statePath = path.join(temporaryDirectory, "state.db");
  const store = await RuntimeStateStore.open(statePath);
  await store.beginAgentRun("gap-conversation", "gap-run", "fake-interview-model", "change it");
  const proposal = {
    batchId: "gap-batch",
    idempotencyKey: "gap-batch-key",
    task: "Commit exactly once",
    actions: [{
      actionId: "gap-action",
      idempotencyKey: "gap-action-key",
      operation: "create",
      path: "notes/gap.md",
      expectedVersion: "missing",
      content: "once\n",
    }],
  };
  await store.saveRunCheckpoint("gap-run", {
    version: 1,
    input: [
      { type: "user_message", text: "change it" },
      { type: "local_tool_call", callId: "gap-provider-call", name: "vault_propose_changes", arguments: proposal },
    ],
    localSkills: [],
    canonicalReadPaths: [],
    requiredRereads: [],
    hostedWebSearchProbeAttempted: false,
    completedSteps: 0,
    pendingToolStep: {
      completedSteps: 1,
      name: "vault_propose_changes",
      providerCallId: "gap-provider-call",
      toolCallId: "gap-tool-call",
    },
  });
  await store.requestToolCall("gap-run", {
    type: "tool_call.requested",
    protocolVersion: 1,
    eventId: "gap-request-event",
    conversationId: "gap-conversation",
    agentRunId: "gap-run",
    sequence: 2,
    toolCallId: "gap-tool-call",
    tool: { kind: "local", name: "vault_propose_changes", arguments: proposal },
  });
  const result = {
    ok: true,
    value: {
      type: "vault_propose_changes",
      batchId: "gap-batch",
      decision: "rejected",
      targets: [{ path: "notes/gap.md", beforeHash: "missing", afterHash: "sha256:after" }],
    },
  };
  await store.completeToolCall("gap-run", result, {
    type: "tool_call.completed",
    protocolVersion: 1,
    eventId: "gap-completed-event",
    conversationId: "gap-conversation",
    agentRunId: "gap-run",
    sequence: 3,
    toolCallId: "gap-tool-call",
    tool: { kind: "local", name: "vault_propose_changes" },
    status: "completed",
  }, "gap-result-event");
  await store.interruptAgentRun("gap-run");
  await assert.rejects(
    store.resumeAgentRun("gap-conversation", "gap-run", {
      toolCallId: "gap-tool-call",
      result: {
        ok: true,
        value: {
          type: "vault_propose_changes",
          batchId: "wrong-batch",
          decision: "rejected",
          targets: [{ path: "notes/gap.md", beforeHash: "missing", afterHash: "sha256:after" }],
        },
      },
    }),
    /mismatched batch result/,
  );
  const resumed = await store.resumeAgentRun("gap-conversation", "gap-run");
  assert.equal(resumed.checkpoint.pendingToolStep.toolCallId, "gap-tool-call");
  assert.deepEqual(await store.getToolCallResult("gap-tool-call", "gap-run"), result);
  await store.close();
});

test("the v7 migration purges legacy Vault Change bodies from tables and database pages", async (t) => {
  const temporaryDirectory = await mkdtemp(path.join(os.tmpdir(), "offeragent-v6-body-purge-"));
  t.after(() => rm(temporaryDirectory, { recursive: true, force: true }));
  const statePath = path.join(temporaryDirectory, "state.db");
  const marker = "LEGACY-COMPLETE-VAULT-BODY-MUST-BE-PURGED";
  const store = await RuntimeStateStore.open(statePath);
  await store.beginAgentRun("legacy-body-conversation", "legacy-body-run", "fake-interview-model", "Safe input");
  await store.requestToolCall("legacy-body-run", {
    type: "tool_call.requested",
    protocolVersion: 1,
    eventId: "legacy-body-event",
    conversationId: "legacy-body-conversation",
    agentRunId: "legacy-body-run",
    sequence: 2,
    toolCallId: "legacy-body-call",
    tool: {
      kind: "local",
      name: "vault_propose_changes",
      arguments: {
        batchId: "legacy-body-batch",
        idempotencyKey: "legacy-body-key",
        task: "Legacy proposal",
        actions: [{
          actionId: "legacy-body-action",
          idempotencyKey: "legacy-body-action-key",
          operation: "create",
          path: "notes/legacy.md",
          expectedVersion: "missing",
          content: "safe placeholder",
        }],
      },
    },
  });
  await store.close();

  const SQL = await initSqlJs();
  const legacy = new SQL.Database(await readFile(statePath));
  const event = JSON.parse(
    legacy.exec("SELECT payload_json FROM durable_events WHERE id = 'legacy-body-event'")[0].values[0][0],
  );
  event.tool.arguments.actions[0].content = marker;
  legacy.run(`
    DROP INDEX vault_change_batches_by_state;
    DROP INDEX vault_change_batches_by_run;
    ALTER TABLE vault_change_batches RENAME TO vault_change_batches_v7_fixture;
    CREATE TABLE vault_change_batches (
      id TEXT PRIMARY KEY,
      conversation_id TEXT NOT NULL REFERENCES conversations(id) ON DELETE CASCADE,
      agent_run_id TEXT NOT NULL REFERENCES agent_runs(id) ON DELETE CASCADE,
      tool_call_id TEXT NOT NULL UNIQUE REFERENCES tool_calls(id) ON DELETE CASCADE,
      idempotency_key TEXT NOT NULL UNIQUE,
      task TEXT NOT NULL,
      proposal_json TEXT NOT NULL,
      target_paths_json TEXT NOT NULL,
      state TEXT NOT NULL CHECK (state IN ('pending', 'applied', 'rejected', 'failed')),
      checkpoint_ref TEXT,
      after_hashes_json TEXT,
      created_at TEXT NOT NULL,
      updated_at TEXT NOT NULL
    );
    INSERT INTO vault_change_batches
      (id, conversation_id, agent_run_id, tool_call_id, idempotency_key, task,
       proposal_json, target_paths_json, state, checkpoint_ref, after_hashes_json,
       created_at, updated_at)
    SELECT id, conversation_id, agent_run_id, tool_call_id, idempotency_key, task,
           '${marker}', target_paths_json, state, checkpoint_ref, after_hashes_json,
           created_at, updated_at
    FROM vault_change_batches_v7_fixture;
    DROP TABLE vault_change_batches_v7_fixture;
    CREATE INDEX vault_change_batches_by_run ON vault_change_batches(agent_run_id, created_at);
    DELETE FROM schema_migrations WHERE version >= 7;
    UPDATE settings_metadata SET value = '6' WHERE key = 'schema_version';
    PRAGMA user_version = 6;
  `);
  legacy.run(
    "UPDATE tool_calls SET arguments_json = ? WHERE id = 'legacy-body-call'",
    [JSON.stringify(event.tool.arguments)],
  );
  legacy.run(
    "UPDATE durable_events SET payload_json = ? WHERE id = 'legacy-body-event'",
    [JSON.stringify(event)],
  );
  legacy.run(
    `UPDATE vault_change_batches
     SET state = 'applied', checkpoint_ref = ?, after_hashes_json = ?
     WHERE id = 'legacy-body-batch'`,
    [
      "refs/offeragent/checkpoints/legacy-body-batch",
      JSON.stringify({ "notes/legacy.md": "sha256:legacy-after" }),
    ],
  );
  await writeFile(statePath, legacy.export());
  legacy.close();
  assert.equal((await readFile(statePath)).includes(Buffer.from(marker)), true);

  const upgraded = await RuntimeStateStore.open(statePath);
  assert.deepEqual(await upgraded.listVaultChangeBatches(["applied"]), [{
    batchId: "legacy-body-batch",
    checkpointRef: "refs/offeragent/checkpoints/legacy-body-batch",
    state: "applied",
    targets: [{
      path: "notes/legacy.md",
      beforeHash: "missing",
      afterHash: "sha256:legacy-after",
    }],
  }]);
  await upgraded.close();
  const upgradedBytes = await readFile(statePath);
  assert.equal(upgradedBytes.includes(Buffer.from(marker)), false);
});
