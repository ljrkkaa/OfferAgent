import assert from "node:assert/strict";
import { spawn } from "node:child_process";
import { once } from "node:events";
import { mkdtemp, readFile, readdir, rm } from "node:fs/promises";
import os from "node:os";
import path from "node:path";
import test from "node:test";
import { fileURLToPath, pathToFileURL } from "node:url";
import WebSocket from "ws";
import { handshake, runWithToolPeer, stopRuntime } from "./runtime-tool-peer.mjs";

const repositoryRoot = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..");
const runtimeEntry = path.join(repositoryRoot, "packages", "runtime", "dist", "cli.js");
const stateStoreModule = path.join(repositoryRoot, "packages", "runtime", "dist", "state-store.js");
const PNG_HEADER = Buffer.from([0x89, 0x50, 0x4e, 0x47, 0x0d, 0x0a, 0x1a, 0x0a]);

function readResult(pathname, content, version, hash) {
  return {
    ok: true,
    value: {
      type: "vault_read",
      path: pathname,
      lineStart: 1,
      lineEnd: content.split("\n").length,
      modifiedVersion: version,
      contentHash: hash,
      content,
      truncated: false,
    },
  };
}

test("an ordered multi-image Interview Submission ingests once as one atomic Experience", async (t) => {
  const temporaryDirectory = await mkdtemp(path.join(os.tmpdir(), "offeragent-multi-image-"));
  const attachmentsPath = path.join(temporaryDirectory, "attachments");
  const token = "multi-image-token";
  const runtime = spawn(process.execPath, [
    runtimeEntry, "--port", "0", "--token", token, "--parent-pid", `${process.pid}`,
    "--provider", "fake", "--fake-scenario", "multi-image-interview-ingestion",
    "--state-path", path.join(temporaryDirectory, "state.db"),
    "--attachments-path", attachmentsPath,
  ], { stdio: ["ignore", "pipe", "pipe"], windowsHide: true });
  let runtimeStderr = "";
  runtime.stderr.on("data", (chunk) => { runtimeStderr += chunk.toString("utf8"); });
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
  const conversationId = "multi-image-conversation";
  const created = new Promise((resolve) => {
    socket.on("message", function onMessage(data) {
      const event = JSON.parse(data.toString("utf8"));
      if (event.type !== "conversation.created" || event.conversation.id !== conversationId) return;
      socket.off("message", onMessage);
      resolve(event);
    });
  });
  socket.send(JSON.stringify({
    type: "conversation.create", protocolVersion: 1, eventId: "multi-image-create",
    conversationId, agentRunId: "conversation-management", sequence: 0,
    title: "Multi image", model: "fake-interview-model",
  }));
  await created;

  const stageImages = async (agentRunId) => {
    const staged = [];
    for (const [index, marker] of ["SCREENSHOT-A-SECRET", "SCREENSHOT-B-SECRET", "SCREENSHOT-C-SECRET"].entries()) {
      const response = await fetch(`http://127.0.0.1:${ready.port}/attachments`, {
        method: "POST",
        headers: {
          authorization: `Bearer ${token}`,
          "content-type": "image/png",
          "x-offeragent-agent-run-id": agentRunId,
          "x-offeragent-conversation-id": conversationId,
          "x-offeragent-file-name": `${index + 1}.png`,
        },
        body: Buffer.concat([PNG_HEADER, Buffer.from(marker)]),
      });
      assert.equal(response.status, 201);
      staged.push(await response.json());
    }
    return staged;
  };

  const recurringPath = "interview/distributed-cache-consistency.md";
  const recurringBefore = "---\ntitle: Distributed cache consistency\ntype: interview-question\nanswer-state: draft\nfrequency: 2\n---\n\n# Distributed cache consistency\n\n## Occurrences\n\n- [[experiences/older-backend]]\n";
  const experienceIndex = "# Interview Experiences\n\n- [[older-backend]]\n";
  const questionIndex = "# Interview Questions\n\n- [[distributed-cache-consistency]]\n";
  const indexes = [
    { kind: "experience", path: "experiences/index.md", exists: true, modifiedVersion: "mtime:2:size:40", contentHash: "sha256:experience-index" },
    { kind: "question", path: "interview/index.md", exists: true, modifiedVersion: "mtime:3:size:50", contentHash: "sha256:question-index" },
  ];
  const staged = await stageImages("multi-image-run");
  let proposal;
  let fingerprint;
  const events = await runWithToolPeer(socket, {
    type: "agent_run.start", protocolVersion: 1, eventId: "multi-image-start",
    conversationId, agentRunId: "multi-image-run", sequence: 0,
    model: "fake-interview-model",
    input: {
      role: "user",
      text: "Ingest these ordered screenshots as one Interview Experience.",
      attachments: staged.map(({ attachmentId }, order) => ({ attachmentId, order })),
    },
  }, (event) => {
    if (event.tool.name === "interview_catalog") {
      fingerprint = event.tool.arguments.sourceFingerprint;
      assert.match(fingerprint, /^sha256:[a-f0-9]{64}$/);
      return { ok: true, value: {
        type: "interview_catalog", experienceCandidates: [],
        questionCandidates: [{
          path: recurringPath, title: "Distributed cache consistency",
          answerState: "draft", matchKinds: ["semantic-candidate"],
          modifiedVersion: "mtime:1:size:180", contentHash: "sha256:recurring-before",
        }], indexes, truncated: false,
      } };
    }
    if (event.tool.name === "vault_read") {
      if (event.tool.arguments.path === recurringPath) {
        return readResult(recurringPath, recurringBefore, "mtime:1:size:180", "sha256:recurring-before");
      }
      if (event.tool.arguments.path === "experiences/index.md") {
        return readResult("experiences/index.md", experienceIndex, "mtime:2:size:40", "sha256:experience-index");
      }
      return readResult("interview/index.md", questionIndex, "mtime:3:size:50", "sha256:question-index");
    }
    assert.equal(event.tool.name, "vault_propose_changes");
    proposal = event.tool.arguments;
    return { ok: true, value: {
      type: "vault_propose_changes", batchId: proposal.batchId, decision: "applied",
      checkpointRef: `refs/offeragent/checkpoints/${proposal.batchId}`,
      targets: proposal.actions.map(({ path: targetPath }) => ({
        path: targetPath,
        beforeHash: targetPath.includes("multi-image") || targetPath.includes("backpressure")
          ? "missing" : "sha256:before",
        afterHash: "sha256:after",
      })),
    } };
  });
  assert.equal(events.at(-1).type, "agent_run.completed", `${runtimeStderr}\n${JSON.stringify(events.slice(-10))}`);
  assert.deepEqual(proposal.actions.map(({ operation, path: targetPath }) => ({ operation, path: targetPath })), [
    { operation: "create", path: "experiences/multi-image-backend-interview.md" },
    { operation: "exact_replace", path: recurringPath },
    { operation: "create", path: "interview/queue-backpressure.md" },
    { operation: "exact_replace", path: "experiences/index.md" },
    { operation: "exact_replace", path: "interview/index.md" },
  ]);
  assert.match(proposal.actions[0].content, new RegExp(`source-fingerprint: ${fingerprint}`));
  assert.match(proposal.actions[1].replacement, /frequency: 3/);
  assert.match(proposal.actions[1].replacement, /multi-image-backend-interview/);
  assert.match(proposal.actions[2].content, /answer-state: needs-research/);
  assert.equal(/SCREENSHOT-[ABC]-SECRET|data:image|base64/i.test(JSON.stringify(proposal)), false);
  assert.equal(events.findIndex((event) => event.type === "tool_call.requested" && event.tool.name === "interview_catalog") <
    events.findIndex((event) => event.type === "tool_call.requested" && event.tool.name === "vault_propose_changes"), true);
  assert.deepEqual(
    new Set(await readdir(attachmentsPath)),
    new Set(staged.map(({ attachmentId }) => attachmentId)),
  );

  const repeated = await stageImages("multi-image-duplicate-run");
  let duplicateProposals = 0;
  const duplicateEvents = await runWithToolPeer(socket, {
    type: "agent_run.start", protocolVersion: 1, eventId: "multi-image-duplicate-start",
    conversationId, agentRunId: "multi-image-duplicate-run", sequence: 0,
    model: "fake-interview-model",
    input: {
      role: "user", text: "Ingest the same screenshots again.",
      attachments: repeated.map(({ attachmentId }, order) => ({ attachmentId, order })),
    },
  }, (event) => {
    if (event.tool.name === "interview_catalog") {
      assert.equal(event.tool.arguments.sourceFingerprint, fingerprint);
      return { ok: true, value: {
        type: "interview_catalog",
        experienceCandidates: [{
          path: "experiences/multi-image-backend-interview.md",
          title: "Multi-image backend interview", matchKinds: ["source-fingerprint"],
          modifiedVersion: "mtime:4:size:240", contentHash: "sha256:stored-experience",
        }], questionCandidates: [], indexes, truncated: false,
      } };
    }
    if (event.tool.name === "vault_read") {
      return readResult(
        "experiences/multi-image-backend-interview.md",
        `---\ntitle: Multi-image backend interview\nsource-fingerprint: ${fingerprint}\n---`,
        "mtime:4:size:240", "sha256:stored-experience",
      );
    }
    duplicateProposals += 1;
    throw new Error("A repeated screenshot submission must not propose Vault changes.");
  });
  assert.equal(duplicateEvents.at(-1).type, "agent_run.completed");
  assert.equal(duplicateProposals, 0);
  assert.match(duplicateEvents.at(-1).output.text, /duplicate/i);
  assert.deepEqual(
    new Set(await readdir(attachmentsPath)),
    new Set([...staged, ...repeated].map(({ attachmentId }) => attachmentId)),
  );

  await stopRuntime(runtime, ready.port, token);
  const { RuntimeStateStore } = await import(pathToFileURL(stateStoreModule));
  const store = await RuntimeStateStore.open(path.join(temporaryDirectory, "state.db"));
  const snapshot = await store.getConversation(conversationId);
  await store.close();
  const submittedMessages = snapshot.messages.filter(({ role, attachments }) =>
    role === "user" && attachments?.length === 3
  );
  assert.equal(submittedMessages.length, 2);
  assert.deepEqual(submittedMessages[0].attachments.map(({ fileName, order }) => ({ fileName, order })), [
    { fileName: "1.png", order: 0 },
    { fileName: "2.png", order: 1 },
    { fileName: "3.png", order: 2 },
  ]);
  assert.equal(JSON.stringify(snapshot.messages).includes(staged[0].attachmentId), false);
  const persistedBytes = await readFile(path.join(temporaryDirectory, "state.db"));
  for (const marker of ["SCREENSHOT-A-SECRET", "SCREENSHOT-B-SECRET", "SCREENSHOT-C-SECRET"]) {
    assert.equal(persistedBytes.includes(Buffer.from(marker)), false);
  }
});
