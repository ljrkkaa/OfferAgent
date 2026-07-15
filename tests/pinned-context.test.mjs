import assert from "node:assert/strict";
import { spawn } from "node:child_process";
import { once } from "node:events";
import { mkdtemp, readFile, rm } from "node:fs/promises";
import os from "node:os";
import path from "node:path";
import test from "node:test";
import { fileURLToPath } from "node:url";
import initSqlJs from "sql.js/dist/sql-asm.js";
import WebSocket from "ws";
import { handshake, runWithToolPeer, stopRuntime } from "./runtime-tool-peer.mjs";

const repositoryRoot = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..");
const runtimeEntry = path.join(repositoryRoot, "packages", "runtime", "dist", "cli.js");

test("Pinned Context is prioritized without becoming a Vault-search whitelist or Evidence Snapshot", async (t) => {
  const temporaryDirectory = await mkdtemp(path.join(os.tmpdir(), "offeragent-pinned-context-"));
  const statePath = path.join(temporaryDirectory, "state.db");
  const token = "pinned-context-token";
  const runtime = spawn(
    process.execPath,
    [
      runtimeEntry,
      "--port", "0",
      "--token", token,
      "--parent-pid", `${process.pid}`,
      "--provider", "fake",
      "--fake-scenario", "pinned-context",
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

  const events = await runWithToolPeer(
    socket,
    {
      type: "agent_run.start",
      protocolVersion: 1,
      eventId: "pinned-context-start",
      conversationId: "pinned-context-conversation",
      agentRunId: "pinned-context-run",
      sequence: 0,
      model: "fake-interview-model",
      input: {
        role: "user",
        text: "Use the preferred source, then search the rest of the Vault for a counterexample.",
        pinnedContext: [
          {
            kind: "selection",
            path: "notes/preferred.md",
            lineStart: 4,
            lineEnd: 8,
          },
          { kind: "document", path: "notes/unread-preferred.md" },
        ],
      },
    },
    (event) => {
      if (event.tool.name === "vault_read") {
        assert.deepEqual(event.tool.arguments, {
          path: "notes/preferred.md",
          lineStart: 4,
          lineEnd: 8,
        });
        return {
          ok: true,
          value: {
            type: "vault_read",
            path: "notes/preferred.md",
            lineStart: 4,
            lineEnd: 8,
            modifiedVersion: "mtime:1:size:24",
            contentHash: "sha256:preferred",
            content: "Preferred source content",
            truncated: false,
          },
        };
      }
      assert.equal(event.tool.name, "vault_search");
      return {
        ok: true,
        value: {
          type: "vault_search",
          entries: [{
            path: "notes/counterexample.md",
            matchTier: "body",
            modifiedVersion: "mtime:2:size:21",
            contentHash: "sha256:counterexample",
            snippets: [{ content: "Search-only candidate", lineStart: 1, lineEnd: 1, truncated: false }],
          }],
          truncated: false,
        },
      };
    },
  );

  assert.deepEqual(
    events.filter((event) => event.type === "tool_call.requested").map((event) => event.tool.name),
    ["agent_contract_read", "planning_memory_list", "vault_read", "vault_search"],
  );
  assert.match(events.at(-1).output.text, /preferred.*counterexample/i);

  const invalidSocket = new WebSocket(`ws://127.0.0.1:${ready.port}/events`, {
    headers: { authorization: `Bearer ${token}` },
  });
  await once(invalidSocket, "open");
  const invalidClosed = once(invalidSocket, "close");
  invalidSocket.send(JSON.stringify({
    type: "agent_run.start",
    protocolVersion: 1,
    eventId: "too-many-pins-start",
    conversationId: "pinned-context-conversation",
    agentRunId: "too-many-pins-run",
    sequence: 0,
    model: "fake-interview-model",
    input: {
      role: "user",
      text: "Too many pins must not start.",
      pinnedContext: Array.from({ length: 9 }, (_, index) => ({
        kind: "document",
        path: `notes/${index}.md`,
      })),
    },
  }));
  const [closeCode] = await invalidClosed;
  assert.equal(closeCode, 1008);

  await stopRuntime(runtime, ready.port, token);
  const SQL = await initSqlJs();
  const database = new SQL.Database(await readFile(statePath));
  assert.deepEqual(
    database.exec("SELECT path, line_start, line_end FROM evidence_snapshots ORDER BY path")[0].values,
    [["notes/preferred.md", 4, 8]],
  );
  assert.equal(
    database.exec("SELECT COUNT(*) FROM evidence_snapshots WHERE path = 'notes/counterexample.md'")[0].values[0][0],
    0,
  );
  assert.equal(
    database.exec("SELECT COUNT(*) FROM evidence_snapshots WHERE path = 'notes/unread-preferred.md'")[0].values[0][0],
    0,
  );
  database.close();
});
