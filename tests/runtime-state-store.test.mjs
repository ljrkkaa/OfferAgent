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
