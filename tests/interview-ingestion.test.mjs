import assert from "node:assert/strict";
import { spawn } from "node:child_process";
import { once } from "node:events";
import { mkdtemp, rm } from "node:fs/promises";
import os from "node:os";
import path from "node:path";
import test from "node:test";
import { fileURLToPath } from "node:url";
import WebSocket from "ws";
import { handshake, runWithToolPeer, stopRuntime } from "./runtime-tool-peer.mjs";

const repositoryRoot = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..");
const runtimeEntry = path.join(repositoryRoot, "packages", "runtime", "dist", "cli.js");

test("a text Interview Submission becomes one normalized atomic knowledge batch", async (t) => {
  const temporaryDirectory = await mkdtemp(path.join(os.tmpdir(), "offeragent-interview-ingestion-"));
  const statePath = path.join(temporaryDirectory, "state.db");
  const token = "interview-ingestion-token";
  const runtime = spawn(
    process.execPath,
    [
      runtimeEntry,
      "--port", "0",
      "--token", token,
      "--parent-pid", `${process.pid}`,
      "--provider", "fake",
      "--fake-scenario", "text-interview-ingestion",
      "--state-path", statePath,
    ],
    { stdio: ["ignore", "pipe", "pipe"], windowsHide: true },
  );
  let runtimeStderr = "";
  runtime.stderr.on("data", (chunk) => {
    runtimeStderr += chunk.toString("utf8");
  });
  const ready = await handshake(runtime.stdout);
  const socket = new WebSocket(`ws://127.0.0.1:${ready.port}/events`, {
    headers: { authorization: `Bearer ${token}` },
  });
  await once(socket, "open");
  t.after(async () => {
    if (socket.readyState === WebSocket.OPEN) socket.close();
    if (runtime.exitCode === null) await stopRuntime(runtime, ready.port, token);
    await rm(temporaryDirectory, { recursive: true, force: true });
  });

  let proposal;
  const events = await runWithToolPeer(
    socket,
    {
      type: "agent_run.start",
      protocolVersion: 1,
      eventId: "text-interview-start",
      conversationId: "text-interview-conversation",
      agentRunId: "text-interview-run",
      sequence: 0,
      model: "fake-interview-model",
      input: {
        role: "user",
        text: "请把这段后端工程师面经入库。问题是解释 Node.js 事件循环，以及如何保证消息处理幂等。原始标记 RAW-TRANSCRIPT-MARKER-7F9C。",
      },
    },
    (event) => {
      if (event.tool.name === "interview_catalog") {
        assert.deepEqual(event.tool.arguments, {
          query: "后端工程师 Node.js 事件循环 消息处理幂等",
          limit: 10,
        });
        return {
          ok: true,
          value: {
            type: "interview_catalog",
            experienceCandidates: [
              {
                path: "experiences/unrelated-frontend.md",
                title: "Frontend interview",
                matchKinds: ["repost-candidate"],
                position: "Frontend engineer",
                modifiedVersion: "mtime:8:size:30",
                contentHash: "sha256:frontend-experience",
              },
            ],
            questionCandidates: [
              {
                path: "interview/database-isolation.md",
                title: "Database isolation",
                matchKinds: ["semantic-candidate"],
                answerState: "verified",
                modifiedVersion: "mtime:9:size:28",
                contentHash: "sha256:database-question",
              },
            ],
            indexes: [
              {
                kind: "experience",
                path: "experiences/index.md",
                exists: true,
                modifiedVersion: "mtime:10:size:23",
                contentHash: "sha256:experience-index",
              },
              {
                kind: "question",
                path: "interview/index.md",
                exists: true,
                modifiedVersion: "mtime:11:size:21",
                contentHash: "sha256:question-index",
              },
            ],
            truncated: false,
          },
        };
      }
      if (event.tool.name === "vault_read") {
        if (event.tool.arguments.path === "experiences/unrelated-frontend.md") {
          return {
            ok: true,
            value: {
              type: "vault_read",
              path: "experiences/unrelated-frontend.md",
              lineStart: 1,
              lineEnd: 3,
              modifiedVersion: "mtime:8:size:30",
              contentHash: "sha256:frontend-experience",
              content: "---\ntitle: Frontend interview\n---",
              truncated: false,
            },
          };
        }
        if (event.tool.arguments.path === "interview/database-isolation.md") {
          return {
            ok: true,
            value: {
              type: "vault_read",
              path: "interview/database-isolation.md",
              lineStart: 1,
              lineEnd: 3,
              modifiedVersion: "mtime:9:size:28",
              contentHash: "sha256:database-question",
              content: "---\ntitle: Database isolation\n---",
              truncated: false,
            },
          };
        }
        if (event.tool.arguments.path === "experiences/index.md") {
          return {
            ok: true,
            value: {
              type: "vault_read",
              path: "experiences/index.md",
              lineStart: 1,
              lineEnd: 1,
              modifiedVersion: "mtime:10:size:23",
              contentHash: "sha256:experience-index",
              content: "# Interview Experiences",
              truncated: false,
            },
          };
        }
        assert.equal(event.tool.arguments.path, "interview/index.md");
        return {
          ok: true,
          value: {
            type: "vault_read",
            path: "interview/index.md",
            lineStart: 1,
            lineEnd: 1,
            modifiedVersion: "mtime:11:size:21",
            contentHash: "sha256:question-index",
            content: "# Interview Questions",
            truncated: false,
          },
        };
      }
      assert.equal(event.tool.name, "vault_propose_changes");
      proposal = event.tool.arguments;
      return {
        ok: true,
        value: {
          type: "vault_propose_changes",
          batchId: proposal.batchId,
          decision: "applied",
          checkpointRef: `refs/offeragent/checkpoints/${proposal.batchId}`,
          targets: proposal.actions.map(({ path }) => ({
            path,
            beforeHash: path.endsWith("index.md") ? "sha256:before" : "missing",
            afterHash: "sha256:after",
          })),
        },
      };
    },
  );

  assert.equal(
    events.at(-1).type,
    "agent_run.completed",
    `${JSON.stringify(events.slice(-8))}\n${runtimeStderr}`,
  );
  assert.deepEqual(
    events
      .filter((event) => event.type === "tool_call.requested")
      .map((event) => event.tool.name),
    [
      "agent_contract_read",
      "planning_memory_list",
      "interview_catalog",
      "vault_read",
      "vault_read",
      "vault_read",
      "vault_read",
      "vault_propose_changes",
      "vault_read",
      "vault_read",
    ],
  );
  assert.equal(
    events.filter(
      (event) => event.type === "tool_call.requested" && event.tool.name === "vault_propose_changes",
    ).length,
    1,
  );
  assert.deepEqual(
    proposal.actions.map(({ operation, path }) => ({ operation, path })),
    [
      { operation: "create", path: "experiences/backend-engineer-interview.md" },
      { operation: "create", path: "interview/nodejs-event-loop.md" },
      { operation: "create", path: "interview/message-processing-idempotency.md" },
      { operation: "exact_replace", path: "experiences/index.md" },
      { operation: "exact_replace", path: "interview/index.md" },
    ],
  );
  const serializedProposal = JSON.stringify(proposal);
  assert.equal(serializedProposal.includes("RAW-TRANSCRIPT-MARKER-7F9C"), false);
  assert.equal(/(?:^|\\n)(company|round|date):/i.test(serializedProposal), false);
  const questionActions = proposal.actions.filter(({ path }) => path.startsWith("interview/") && path !== "interview/index.md");
  assert.equal(questionActions.length, 2);
  assert.ok(questionActions.every(({ content }) => /answer-state: needs-research/.test(content)));
  assert.equal(proposal.actions.filter(({ path }) => path.startsWith("experiences/") && !path.endsWith("index.md")).length, 1);
  assert.match(events.at(-1).output.text, /5 Vault changes applied/);
  assert.match(events.at(-1).output.text, /refs\/offeragent\/checkpoints\//);

  const catalogFailureEvents = await runWithToolPeer(
    socket,
    {
      type: "agent_run.start",
      protocolVersion: 1,
      eventId: "catalog-failure-start",
      conversationId: "catalog-failure-conversation",
      agentRunId: "catalog-failure-run",
      sequence: 0,
      model: "fake-interview-model",
      input: { role: "user", text: "Ingest this interview submission." },
    },
    (event) => {
      assert.equal(event.tool.name, "interview_catalog");
      return {
        ok: false,
        error: { code: "tool_error", message: "catalog unavailable" },
      };
    },
  );
  assert.equal(catalogFailureEvents.at(-1).type, "agent_run.completed");
  assert.deepEqual(
    catalogFailureEvents
      .filter((event) => event.type === "tool_call.requested")
      .map((event) => event.tool.name),
    ["agent_contract_read", "planning_memory_list", "interview_catalog"],
  );
  assert.match(catalogFailureEvents.at(-1).output.text, /Interview Catalog failed: catalog unavailable/);

  const candidateFailureEvents = await runWithToolPeer(
    socket,
    {
      type: "agent_run.start",
      protocolVersion: 1,
      eventId: "candidate-failure-start",
      conversationId: "candidate-failure-conversation",
      agentRunId: "candidate-failure-run",
      sequence: 0,
      model: "fake-interview-model",
      input: { role: "user", text: "Ingest overlapping interview material." },
    },
    (event) => {
      if (event.tool.name === "interview_catalog") {
        return {
          ok: true,
          value: {
            type: "interview_catalog",
            experienceCandidates: [
              {
                path: "experiences/existing-backend.md",
                title: "Existing backend interview",
                matchKinds: ["repost-candidate"],
                modifiedVersion: "mtime:20:size:40",
                contentHash: "sha256:existing-backend",
              },
            ],
            questionCandidates: [],
            indexes: [
              {
                kind: "experience",
                path: "experiences/index.md",
                exists: true,
                modifiedVersion: "mtime:21:size:20",
                contentHash: "sha256:experience-index-2",
              },
              {
                kind: "question",
                path: "interview/index.md",
                exists: true,
                modifiedVersion: "mtime:22:size:20",
                contentHash: "sha256:question-index-2",
              },
            ],
            truncated: false,
          },
        };
      }
      assert.equal(event.tool.name, "vault_read");
      assert.equal(event.tool.arguments.path, "experiences/existing-backend.md");
      return {
        ok: false,
        error: { code: "not_found", message: "candidate disappeared" },
      };
    },
  );
  assert.deepEqual(
    candidateFailureEvents
      .filter((event) => event.type === "tool_call.requested")
      .map((event) => event.tool.name),
    ["agent_contract_read", "planning_memory_list", "interview_catalog", "vault_read"],
  );
  assert.match(candidateFailureEvents.at(-1).output.text, /Candidate evidence failed: candidate disappeared/);

  let missingIndexProposal;
  const missingIndexEvents = await runWithToolPeer(
    socket,
    {
      type: "agent_run.start",
      protocolVersion: 1,
      eventId: "missing-index-start",
      conversationId: "missing-index-conversation",
      agentRunId: "missing-index-run",
      sequence: 0,
      model: "fake-interview-model",
      input: { role: "user", text: "Ingest into a clean interview knowledge vault." },
    },
    (event) => {
      if (event.tool.name === "interview_catalog") {
        return {
          ok: true,
          value: {
            type: "interview_catalog",
            experienceCandidates: [],
            questionCandidates: [],
            indexes: [
              {
                kind: "experience",
                path: "experiences/index.md",
                exists: false,
                modifiedVersion: "missing",
              },
              {
                kind: "question",
                path: "interview/index.md",
                exists: false,
                modifiedVersion: "missing",
              },
            ],
            truncated: false,
          },
        };
      }
      if (event.tool.name === "vault_propose_changes") {
        missingIndexProposal = event.tool.arguments;
        return {
          ok: true,
          value: {
            type: "vault_propose_changes",
            batchId: missingIndexProposal.batchId,
            decision: "applied",
            checkpointRef: `refs/offeragent/checkpoints/${missingIndexProposal.batchId}`,
            targets: missingIndexProposal.actions.map(({ path: targetPath }) => ({
              path: targetPath,
              beforeHash: "missing",
              afterHash: "sha256:created",
            })),
          },
        };
      }
      assert.equal(event.tool.name, "vault_read");
      const targetPath = event.tool.arguments.path;
      assert.ok(["experiences/index.md", "interview/index.md"].includes(targetPath));
      return {
        ok: true,
        value: {
          type: "vault_read",
          path: targetPath,
          lineStart: 1,
          lineEnd: 3,
          modifiedVersion: "mtime:30:size:60",
          contentHash: "sha256:created",
          content: targetPath.startsWith("experiences/")
            ? "# Interview Experiences\n\n- [[backend-engineer-interview]]"
            : "# Interview Questions\n\n- [[nodejs-event-loop]]",
          truncated: false,
        },
      };
    },
  );
  assert.equal(
    missingIndexEvents.at(-1).type,
    "agent_run.completed",
    `${JSON.stringify(missingIndexEvents.slice(-8))}\n${runtimeStderr}`,
  );
  assert.deepEqual(
    missingIndexEvents
      .filter((event) => event.type === "tool_call.requested")
      .map((event) => event.tool.name),
    [
      "agent_contract_read",
      "planning_memory_list",
      "interview_catalog",
      "vault_propose_changes",
      "vault_read",
      "vault_read",
    ],
  );
  assert.deepEqual(
    missingIndexProposal.actions
      .filter(({ path: targetPath }) => targetPath.endsWith("index.md"))
      .map(({ operation, expectedVersion }) => ({ operation, expectedVersion })),
    [
      { operation: "create", expectedVersion: "missing" },
      { operation: "create", expectedVersion: "missing" },
    ],
  );
});
