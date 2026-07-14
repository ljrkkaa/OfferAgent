import assert from "node:assert/strict";
import { spawn } from "node:child_process";
import { once } from "node:events";
import { mkdtemp, readFile, readdir, rename, rm, writeFile } from "node:fs/promises";
import os from "node:os";
import path from "node:path";
import test from "node:test";
import { fileURLToPath, pathToFileURL } from "node:url";
import WebSocket from "ws";
import { handshake, runWithToolPeer, stopRuntime } from "./runtime-tool-peer.mjs";

const repositoryRoot = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..");
const runtimeEntry = path.join(repositoryRoot, "packages", "runtime", "dist", "cli.js");
const stateStoreModule = path.join(
  repositoryRoot,
  "packages",
  "runtime",
  "dist",
  "state-store.js",
);
const PNG = Buffer.concat([
  Buffer.from([0x89, 0x50, 0x4e, 0x47, 0x0d, 0x0a, 0x1a, 0x0a]),
  Buffer.from("RUN-ATTACHMENT-SECRET-BYTES-31"),
]);

function waitFor(socket, predicate) {
  return new Promise((resolve, reject) => {
    const timeout = setTimeout(() => reject(new Error("Runtime event timed out")), 10_000);
    socket.on("message", function onMessage(data) {
      const event = JSON.parse(data.toString("utf8"));
      if (!predicate(event)) return;
      clearTimeout(timeout);
      socket.off("message", onMessage);
      resolve(event);
    });
  });
}

test("one staged image reaches a fake vision Provider without persistent bytes", async (t) => {
  const temporaryDirectory = await mkdtemp(path.join(os.tmpdir(), "offeragent-single-image-"));
  const statePath = path.join(temporaryDirectory, "state.db");
  const attachmentsPath = path.join(temporaryDirectory, "attachments");
  const token = "single-image-token";
  const runtime = spawn(
    process.execPath,
    [
      runtimeEntry,
      "--port", "0",
      "--token", token,
      "--parent-pid", `${process.pid}`,
      "--provider", "fake",
      "--fake-scenario", "single-image",
      "--state-path", statePath,
      "--attachments-path", attachmentsPath,
    ],
    { stdio: ["ignore", "pipe", "pipe"], windowsHide: true },
  );
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

  const conversationId = "single-image-conversation";
  const agentRunId = "single-image-run";
  const created = waitFor(socket, (event) => event.type === "conversation.created");
  socket.send(JSON.stringify({
    type: "conversation.create",
    protocolVersion: 1,
    eventId: "single-image-conversation-create",
    conversationId,
    agentRunId: "conversation-management",
    sequence: 0,
    title: "Single image",
    model: "fake-interview-model",
  }));
  await created;

  const discardRunId = "single-image-discard-run";
  const discardUpload = await fetch(`http://127.0.0.1:${ready.port}/attachments`, {
    method: "POST",
    headers: {
      authorization: `Bearer ${token}`,
      "content-type": "image/png",
      "x-offeragent-agent-run-id": discardRunId,
      "x-offeragent-conversation-id": conversationId,
      "x-offeragent-file-name": "discard.png",
    },
    body: PNG,
  });
  const discardStaged = await discardUpload.json();
  const wrongDiscard = await fetch(
    `http://127.0.0.1:${ready.port}/attachments/${discardStaged.attachmentId}`,
    {
      method: "DELETE",
      headers: {
        authorization: `Bearer ${token}`,
        "x-offeragent-agent-run-id": "wrong-run",
        "x-offeragent-conversation-id": conversationId,
      },
    },
  );
  assert.equal(wrongDiscard.status, 403);
  const discard = await fetch(
    `http://127.0.0.1:${ready.port}/attachments/${discardStaged.attachmentId}`,
    {
      method: "DELETE",
      headers: {
        authorization: `Bearer ${token}`,
        "x-offeragent-agent-run-id": discardRunId,
        "x-offeragent-conversation-id": conversationId,
      },
    },
  );
  assert.equal(discard.status, 200);
  assert.deepEqual(await readdir(attachmentsPath), []);

  const upload = await fetch(`http://127.0.0.1:${ready.port}/attachments`, {
    method: "POST",
    headers: {
      authorization: `Bearer ${token}`,
      "content-type": "image/png",
      "x-offeragent-agent-run-id": agentRunId,
      "x-offeragent-conversation-id": conversationId,
      "x-offeragent-file-name": encodeURIComponent("interview.png"),
    },
    body: PNG,
  });
  assert.equal(upload.status, 201);
  const staged = await upload.json();
  assert.equal(staged.mediaType, "image/png");
  assert.match(staged.contentHash, /^sha256:[a-f0-9]{64}$/);
  assert.deepEqual((await readdir(attachmentsPath)).length, 1);

  const events = await runWithToolPeer(
    socket,
    {
      type: "agent_run.start",
      protocolVersion: 1,
      eventId: "single-image-run-start",
      conversationId,
      agentRunId,
      sequence: 0,
      model: "fake-interview-model",
      input: {
        role: "user",
        text: "Describe the interview screenshot.",
        attachments: [{ attachmentId: staged.attachmentId, order: 0 }],
      },
    },
    () => {
      throw new Error("Unexpected non-contract tool call");
    },
  );
  assert.equal(events.at(-1).type, "agent_run.completed");
  assert.match(events.at(-1).output.text, /distributed cache consistency/);
  assert.deepEqual(await readdir(attachmentsPath), []);

  await stopRuntime(runtime, ready.port, token);
  const { RuntimeStateStore } = await import(pathToFileURL(stateStoreModule));
  const store = await RuntimeStateStore.open(statePath);
  const snapshot = await store.getConversation(conversationId);
  await store.close();
  assert.deepEqual(snapshot.messages[0].attachments, [{
    contentHash: staged.contentHash,
    fileName: "interview.png",
    mediaType: "image/png",
    order: 0,
    size: PNG.length,
  }]);
  assert.equal(JSON.stringify(snapshot.messages).includes(staged.attachmentId), false);
  const persisted = await readFile(statePath);
  assert.equal(persisted.includes(Buffer.from("RUN-ATTACHMENT-SECRET-BYTES-31")), false);
  assert.equal(persisted.includes(Buffer.from(PNG.toString("base64"))), false);
});

test("unavailable vision cleans the image and leaves later text Runs healthy", async (t) => {
  const temporaryDirectory = await mkdtemp(path.join(os.tmpdir(), "offeragent-no-vision-"));
  const attachmentsPath = path.join(temporaryDirectory, "attachments");
  const token = "no-vision-token";
  const runtime = spawn(
    process.execPath,
    [
      runtimeEntry, "--port", "0", "--token", token, "--parent-pid", `${process.pid}`,
      "--provider", "fake", "--fake-scenario", "vision-unavailable",
      "--state-path", path.join(temporaryDirectory, "state.db"),
      "--attachments-path", attachmentsPath,
    ],
    { stdio: ["ignore", "pipe", "pipe"], windowsHide: true },
  );
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
  const conversationId = "no-vision-conversation";
  const created = waitFor(socket, (event) => event.type === "conversation.created");
  socket.send(JSON.stringify({
    type: "conversation.create", protocolVersion: 1, eventId: "no-vision-create",
    conversationId, agentRunId: "conversation-management", sequence: 0,
    title: "No vision", model: "fake-interview-model",
  }));
  await created;
  const upload = await fetch(`http://127.0.0.1:${ready.port}/attachments`, {
    method: "POST",
    headers: {
      authorization: `Bearer ${token}`,
      "content-type": "image/png",
      "x-offeragent-agent-run-id": "no-vision-run",
      "x-offeragent-conversation-id": conversationId,
      "x-offeragent-file-name": "interview.png",
    },
    body: PNG,
  });
  const staged = await upload.json();
  const failed = await runWithToolPeer(socket, {
    type: "agent_run.start", protocolVersion: 1, eventId: "no-vision-start",
    conversationId, agentRunId: "no-vision-run", sequence: 0,
    model: "fake-interview-model",
    input: {
      role: "user", text: "Read the image.",
      attachments: [{ attachmentId: staged.attachmentId, order: 0 }],
    },
  }, () => { throw new Error("Unexpected tool"); });
  assert.equal(failed.at(-1).type, "agent_run.failed");
  assert.match(failed.at(-1).error.message, /vision-capable model|provide text/i);
  assert.deepEqual(await readdir(attachmentsPath), []);

  const textRun = await runWithToolPeer(socket, {
    type: "agent_run.start", protocolVersion: 1, eventId: "after-no-vision-start",
    conversationId, agentRunId: "after-no-vision-run", sequence: 0,
    model: "fake-interview-model",
    input: { role: "user", text: "Text still works." },
  }, () => { throw new Error("Unexpected tool"); });
  assert.equal(textRun.at(-1).type, "agent_run.completed");
  assert.match(textRun.at(-1).output.text, /Text still works/);
});

test("a pre-Run attachment validation failure removes its staged bytes", async (t) => {
  const temporaryDirectory = await mkdtemp(path.join(os.tmpdir(), "offeragent-invalid-image-"));
  const attachmentsPath = path.join(temporaryDirectory, "attachments");
  const token = "invalid-image-token";
  const runtime = spawn(
    process.execPath,
    [
      runtimeEntry, "--port", "0", "--token", token, "--parent-pid", `${process.pid}`,
      "--provider", "fake", "--fake-scenario", "single-image",
      "--state-path", path.join(temporaryDirectory, "state.db"),
      "--attachments-path", attachmentsPath,
    ],
    { stdio: ["ignore", "pipe", "pipe"], windowsHide: true },
  );
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
  const conversationId = "invalid-image-conversation";
  const agentRunId = "invalid-image-run";
  const created = waitFor(socket, (event) => event.type === "conversation.created");
  socket.send(JSON.stringify({
    type: "conversation.create", protocolVersion: 1, eventId: "invalid-image-create",
    conversationId, agentRunId: "conversation-management", sequence: 0,
    title: "Invalid image", model: "fake-interview-model",
  }));
  await created;
  const upload = await fetch(`http://127.0.0.1:${ready.port}/attachments`, {
    method: "POST",
    headers: {
      authorization: `Bearer ${token}`,
      "content-type": "image/png",
      "x-offeragent-agent-run-id": agentRunId,
      "x-offeragent-conversation-id": conversationId,
      "x-offeragent-file-name": "interview.png",
    },
    body: PNG,
  });
  const staged = await upload.json();
  const [stagedFile] = await readdir(attachmentsPath);
  await writeFile(path.join(attachmentsPath, stagedFile), Buffer.from("tampered"));

  const failed = await runWithToolPeer(socket, {
    type: "agent_run.start", protocolVersion: 1, eventId: "invalid-image-start",
    conversationId, agentRunId, sequence: 0, model: "fake-interview-model",
    input: {
      role: "user", text: "Read the image.",
      attachments: [{ attachmentId: staged.attachmentId, order: 0 }],
    },
  }, () => { throw new Error("Unexpected tool"); });
  assert.equal(failed.at(-1).type, "agent_run.failed");
  assert.equal(failed.at(-1).error.code, "provider_error");
  assert.deepEqual(await readdir(attachmentsPath), []);
});

test("Interrupted image Runs retain bytes for Resume and cancelled Runs remove them", async (t) => {
  const temporaryDirectory = await mkdtemp(path.join(os.tmpdir(), "offeragent-image-resume-"));
  const attachmentsPath = path.join(temporaryDirectory, "attachments");
  const token = "image-resume-token";
  const runtime = spawn(
    process.execPath,
    [
      runtimeEntry, "--port", "0", "--token", token, "--parent-pid", `${process.pid}`,
      "--provider", "fake", "--fake-scenario", "single-image",
      "--state-path", path.join(temporaryDirectory, "state.db"),
      "--attachments-path", attachmentsPath,
    ],
    { stdio: ["ignore", "pipe", "pipe"], windowsHide: true },
  );
  const ready = await handshake(runtime.stdout);
  let socket = new WebSocket(`ws://127.0.0.1:${ready.port}/events`, {
    headers: { authorization: `Bearer ${token}` },
  });
  await once(socket, "open");
  t.after(async () => {
    if (socket.readyState === WebSocket.OPEN) socket.close();
    if (runtime.exitCode === null) await stopRuntime(runtime, ready.port, token);
    await rm(temporaryDirectory, { recursive: true, force: true });
  });
  const conversationId = "image-resume-conversation";
  const created = waitFor(socket, (event) => event.type === "conversation.created");
  socket.send(JSON.stringify({
    type: "conversation.create", protocolVersion: 1, eventId: "image-resume-create",
    conversationId, agentRunId: "conversation-management", sequence: 0,
    title: "Resume image", model: "fake-interview-model",
  }));
  await created;
  const uploadFor = async (agentRunId) => {
    const response = await fetch(`http://127.0.0.1:${ready.port}/attachments`, {
      method: "POST",
      headers: {
        authorization: `Bearer ${token}`,
        "content-type": "image/png",
        "x-offeragent-agent-run-id": agentRunId,
        "x-offeragent-conversation-id": conversationId,
        "x-offeragent-file-name": "interview.png",
      },
      body: PNG,
    });
    return response.json();
  };
  const priorAttachment = await uploadFor("image-prior-run");
  const prior = await runWithToolPeer(socket, {
    type: "agent_run.start", protocolVersion: 1, eventId: "image-prior-start",
    conversationId, agentRunId: "image-prior-run", sequence: 0,
    model: "fake-interview-model",
    input: {
      role: "user", text: "Describe the prior image.",
      attachments: [{ attachmentId: priorAttachment.attachmentId, order: 0 }],
    },
  }, () => { throw new Error("Unexpected non-contract tool call"); });
  assert.equal(prior.at(-1).type, "agent_run.completed");
  assert.deepEqual(await readdir(attachmentsPath), []);

  const staged = await uploadFor("image-interrupted-run");
  const interrupted = new Promise((resolve, reject) => {
    const observed = [];
    const timeout = setTimeout(() => reject(new Error(
      `Image Run did not reach interruption seam: ${JSON.stringify(observed)}`,
    )), 15_000);
    socket.on("message", function onMessage(data) {
      const event = JSON.parse(data.toString("utf8"));
      if (event.agentRunId !== "image-interrupted-run") return;
      observed.push({ type: event.type, tool: event.tool?.name, error: event.error });
      if (event.type === "agent_run.failed") {
        clearTimeout(timeout);
        socket.off("message", onMessage);
        reject(new Error(`${event.error.code}: ${event.error.message}; ${JSON.stringify(observed)}`));
        return;
      }
      if (event.type !== "tool_call.requested") return;
      if (event.tool.name === "agent_contract_read") {
        socket.send(JSON.stringify({
          type: "tool_result", protocolVersion: 1, eventId: `result-${event.toolCallId}`,
          conversationId, agentRunId: event.agentRunId, sequence: event.sequence,
          toolCallId: event.toolCallId,
          result: { ok: true, value: {
            type: "agent_contract_read", path: "agent.md", modifiedVersion: "mtime:1:size:4",
            contentHash: "sha256:contract", content: "# Contract",
          } },
        }));
        return;
      }
      if (event.tool.name === "planning_memory_list") {
        socket.send(JSON.stringify({
          type: "tool_result", protocolVersion: 1, eventId: `result-${event.toolCallId}`,
          conversationId, agentRunId: event.agentRunId, sequence: event.sequence,
          toolCallId: event.toolCallId,
          result: { ok: true, value: {
            type: "planning_memory_list", topics: [], truncated: false,
          } },
        }));
        return;
      }
      if (event.tool.name === "vault_read") {
        clearTimeout(timeout);
        socket.off("message", onMessage);
        socket.close();
        resolve();
      }
    });
    socket.send(JSON.stringify({
      type: "agent_run.start", protocolVersion: 1, eventId: "image-interrupted-start",
      conversationId, agentRunId: "image-interrupted-run", sequence: 0,
      model: "fake-interview-model",
      input: {
        role: "user", text: "image_empty_interrupt",
        attachments: [{ attachmentId: staged.attachmentId, order: 0 }],
      },
    }));
  });
  await interrupted;
  await new Promise((resolve) => setTimeout(resolve, 100));
  assert.equal((await readdir(attachmentsPath)).length, 1);

  socket = new WebSocket(`ws://127.0.0.1:${ready.port}/events`, {
    headers: { authorization: `Bearer ${token}` },
  });
  await once(socket, "open");
  await waitFor(socket, (event) =>
    event.type === "agent_run.interrupted" && event.agentRunId === "image-interrupted-run"
  );
  const protectedDiscard = await fetch(
    `http://127.0.0.1:${ready.port}/attachments/${staged.attachmentId}`,
    {
      method: "DELETE",
      headers: {
        authorization: `Bearer ${token}`,
        "x-offeragent-agent-run-id": "image-interrupted-run",
        "x-offeragent-conversation-id": conversationId,
      },
    },
  );
  assert.equal(protectedDiscard.status, 409);
  assert.equal((await readdir(attachmentsPath)).length, 1);

  const retainedPath = path.join(attachmentsPath, staged.attachmentId);
  const heldPath = path.join(temporaryDirectory, "held-interrupted-image");
  await rename(retainedPath, heldPath);
  const rejectedResume = once(socket, "close");
  socket.send(JSON.stringify({
    type: "agent_run.resume", protocolVersion: 1, eventId: "missing-image-resume-command",
    conversationId, agentRunId: "image-interrupted-run", sequence: 0,
  }));
  const [closeCode] = await rejectedResume;
  assert.equal(closeCode, 1008);
  await rename(heldPath, retainedPath);

  socket = new WebSocket(`ws://127.0.0.1:${ready.port}/events`, {
    headers: { authorization: `Bearer ${token}` },
  });
  await once(socket, "open");
  await waitFor(socket, (event) =>
    event.type === "agent_run.interrupted" && event.agentRunId === "image-interrupted-run"
  );
  const resumed = await runWithToolPeer(socket, {
    type: "agent_run.resume", protocolVersion: 1, eventId: "image-resume-command",
    conversationId, agentRunId: "image-interrupted-run", sequence: 0,
  }, (event) => readResult(event.tool.arguments.path));
  assert.equal(resumed.at(-1).type, "agent_run.completed");
  assert.deepEqual(await readdir(attachmentsPath), []);

  const cancelAttachment = await uploadFor("image-cancelled-run");
  const cancelled = new Promise((resolve, reject) => {
    const timeout = setTimeout(() => reject(new Error("Image Run did not cancel")), 15_000);
    socket.on("message", function onMessage(data) {
      const event = JSON.parse(data.toString("utf8"));
      if (event.agentRunId !== "image-cancelled-run") return;
      if (event.type === "tool_call.requested" && event.tool.name === "agent_contract_read") {
        socket.send(JSON.stringify({
          type: "tool_result", protocolVersion: 1, eventId: `result-${event.toolCallId}`,
          conversationId, agentRunId: event.agentRunId, sequence: event.sequence,
          toolCallId: event.toolCallId,
          result: { ok: true, value: {
            type: "agent_contract_read", path: "agent.md", modifiedVersion: "mtime:1:size:4",
            contentHash: "sha256:contract", content: "# Contract",
          } },
        }));
      } else if (event.type === "tool_call.requested" && event.tool.name === "planning_memory_list") {
        socket.send(JSON.stringify({
          type: "tool_result", protocolVersion: 1, eventId: `result-${event.toolCallId}`,
          conversationId, agentRunId: event.agentRunId, sequence: event.sequence,
          toolCallId: event.toolCallId,
          result: { ok: true, value: {
            type: "planning_memory_list", topics: [], truncated: false,
          } },
        }));
      } else if (event.type === "tool_call.requested" && event.tool.name === "vault_read") {
        socket.send(JSON.stringify({
          type: "agent_run.cancel", protocolVersion: 1, eventId: "image-cancel-command",
          conversationId, agentRunId: event.agentRunId, sequence: event.sequence,
        }));
      } else if (event.type === "agent_run.cancelled") {
        clearTimeout(timeout);
        socket.off("message", onMessage);
        resolve();
      }
    });
    socket.send(JSON.stringify({
      type: "agent_run.start", protocolVersion: 1, eventId: "image-cancelled-start",
      conversationId, agentRunId: "image-cancelled-run", sequence: 0,
      model: "fake-interview-model",
      input: {
        role: "user", text: "image_interrupt",
        attachments: [{ attachmentId: cancelAttachment.attachmentId, order: 0 }],
      },
    }));
  });
  await cancelled;
  assert.deepEqual(await readdir(attachmentsPath), []);

  await uploadFor("image-unstarted-run");
  assert.equal((await readdir(attachmentsPath)).length, 1);
  const deleted = waitFor(socket, (event) => event.type === "conversation.deleted");
  socket.send(JSON.stringify({
    type: "conversation.delete", protocolVersion: 1, eventId: "image-conversation-delete",
    conversationId, agentRunId: "conversation-management", sequence: 0,
  }));
  await deleted;
  assert.deepEqual(await readdir(attachmentsPath), []);
});

function readResult(pathname) {
  return {
    ok: true,
    value: {
      type: "vault_read", path: pathname, lineStart: 1, lineEnd: 1,
      modifiedVersion: "mtime:2:size:7", contentHash: "sha256:image-context",
      content: "context", truncated: false,
    },
  };
}
