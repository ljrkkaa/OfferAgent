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
    INSERT INTO tool_calls_v3_fixture SELECT * FROM tool_calls;
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
