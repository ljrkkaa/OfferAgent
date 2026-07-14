import assert from "node:assert/strict";
import { spawn } from "node:child_process";
import { once } from "node:events";
import { request } from "node:http";
import { mkdtemp, rm } from "node:fs/promises";
import { createRequire } from "node:module";
import os from "node:os";
import path from "node:path";
import test from "node:test";
import { fileURLToPath } from "node:url";
import WebSocket from "ws";

const repositoryRoot = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..");
const runtimeEntry = path.join(repositoryRoot, "packages", "runtime", "dist", "cli.js");
const require = createRequire(import.meta.url);
const { RuntimeStateStore } = require(
  path.join(repositoryRoot, "packages", "runtime", "dist", "state-store.js"),
);

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

async function startRuntime(statePath, token) {
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
  return { handshake: await readHandshake(runtime.stdout), runtime, token };
}

async function connect(instance) {
  const socket = new WebSocket(`ws://127.0.0.1:${instance.handshake.port}/events`, {
    headers: { authorization: `Bearer ${instance.token}` },
  });
  await once(socket, "open");
  return socket;
}

async function stopRuntime(instance) {
  if (instance.runtime.exitCode !== null) return;
  const exited = once(instance.runtime, "exit");
  await new Promise((resolve, reject) => {
    const outgoing = request(
      {
        host: "127.0.0.1",
        port: instance.handshake.port,
        path: "/shutdown",
        method: "POST",
        headers: { authorization: `Bearer ${instance.token}` },
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

function toolResult(event, result) {
  return {
    type: "tool_result",
    protocolVersion: 1,
    eventId: `result-${event.toolCallId}`,
    conversationId: event.conversationId,
    agentRunId: event.agentRunId,
    sequence: event.sequence,
    toolCallId: event.toolCallId,
    result,
  };
}

function respondPlanningMemoryList(socket, event) {
  if (event.type !== "tool_call.requested" || event.tool.name !== "planning_memory_list") return false;
  socket.send(JSON.stringify(toolResult(event, {
    ok: true,
    value: { type: "planning_memory_list", topics: [], truncated: false },
  })));
  return true;
}

test("an explicit Resume continues one Interrupted Run without repeating a committed tool", async (t) => {
  const directory = await mkdtemp(path.join(os.tmpdir(), "offeragent-run-resume-"));
  const instance = await startRuntime(path.join(directory, "state.db"), "run-resume-token");
  t.after(async () => {
    await stopRuntime(instance);
    await rm(directory, { recursive: true, force: true });
  });

  const firstSocket = await connect(instance);
  let contractExecutions = 0;
  const firstInterrupted = new Promise((resolve, reject) => {
    const timeout = setTimeout(() => reject(new Error("Initial Run did not reach its Vault read")), 5_000);
    firstSocket.on("message", function onMessage(data) {
      const event = JSON.parse(data.toString("utf8"));
      if (event.agentRunId !== "resume-run" || event.type !== "tool_call.requested") return;
      if (respondPlanningMemoryList(firstSocket, event)) return;
      if (event.tool.name === "agent_contract_read") {
        contractExecutions += 1;
        firstSocket.send(JSON.stringify(toolResult(event, {
          ok: true,
          value: {
            type: "agent_contract_read",
            path: "agent.md",
            modifiedVersion: "mtime:1:size:8",
            contentHash: "sha256:contract",
            content: "# Agent",
          },
        })));
      }
      if (event.tool.name === "vault_read") {
        clearTimeout(timeout);
        firstSocket.terminate();
        resolve();
      }
    });
  });
  firstSocket.send(JSON.stringify({
    type: "agent_run.start",
    protocolVersion: 1,
    eventId: "resume-run-start",
    conversationId: "resume-conversation",
    agentRunId: "resume-run",
    sequence: 0,
    model: "fake-interview-model",
    input: { role: "user", text: "vault_read notes/resume.md" },
  }));
  await firstInterrupted;
  await once(firstSocket, "close");

  const resumedSocket = await connect(instance);
  const replayedInterruption = new Promise((resolve, reject) => {
    const timeout = setTimeout(() => reject(new Error("Interrupted state was not replayed")), 5_000);
    resumedSocket.on("message", function onReplay(data) {
      const event = JSON.parse(data.toString("utf8"));
      if (event.agentRunId !== "resume-run" || event.type !== "agent_run.interrupted") return;
      clearTimeout(timeout);
      resumedSocket.off("message", onReplay);
      resolve(event);
    });
  });
  const interrupted = await replayedInterruption;
  resumedSocket.send(JSON.stringify({
    type: "event.ack",
    protocolVersion: 1,
    eventId: "ack-interrupted-before-resume",
    acknowledgedEventId: interrupted.eventId,
    conversationId: interrupted.conversationId,
    agentRunId: interrupted.agentRunId,
    sequence: interrupted.sequence,
  }));

  const resumedEvents = [];
  const completed = new Promise((resolve, reject) => {
    const timeout = setTimeout(() => reject(new Error("Resumed Run timed out")), 5_000);
    resumedSocket.on("message", function onMessage(data) {
      const event = JSON.parse(data.toString("utf8"));
      if (event.agentRunId !== "resume-run") return;
      if (respondPlanningMemoryList(resumedSocket, event)) return;
      resumedEvents.push(event);
      if (event.type === "tool_call.requested" && event.tool.name === "agent_contract_read") {
        contractExecutions += 1;
        resumedSocket.send(JSON.stringify(toolResult(event, {
          ok: true,
          value: {
            type: "agent_contract_read",
            path: "agent.md",
            modifiedVersion: "mtime:1:size:8",
            contentHash: "sha256:contract",
            content: "# Agent",
          },
        })));
      }
      if (event.type === "tool_call.requested" && event.tool.name === "vault_read") {
        resumedSocket.send(JSON.stringify(toolResult(event, {
          ok: true,
          value: {
            type: "vault_read",
            path: "notes/resume.md",
            lineStart: 1,
            lineEnd: 1,
            modifiedVersion: "mtime:2:size:4",
            contentHash: "sha256:fact",
            content: "fact",
            truncated: false,
          },
        })));
      }
      if (event.type === "agent_run.completed") {
        clearTimeout(timeout);
        resumedSocket.off("message", onMessage);
        resolve();
      }
    });
  });
  resumedSocket.send(JSON.stringify({
    type: "agent_run.resume",
    protocolVersion: 1,
    eventId: "explicit-resume-command",
    conversationId: "resume-conversation",
    agentRunId: "resume-run",
    sequence: 0,
  }));
  await completed;
  assert.ok(resumedEvents.some((event) => event.type === "agent_run.resumed"));
  assert.equal(contractExecutions, 2);
  assert.equal(resumedEvents.at(-1).type, "agent_run.completed");
  resumedSocket.close();
});

test("Resume restores a committed Daily target before atomically augmenting its plan batch", async (t) => {
  const directory = await mkdtemp(path.join(os.tmpdir(), "offeragent-daily-context-gap-"));
  const statePath = path.join(directory, "state.db");
  const store = await RuntimeStateStore.open(statePath);
  await store.beginAgentRun(
    "daily-gap-conversation",
    "daily-gap-run",
    "fake-interview-model",
    "planning_memory_daily_only",
  );
  await store.saveRunCheckpoint("daily-gap-run", {
    version: 1,
    input: [
      { type: "user_message", text: "planning_memory_daily_only" },
      { type: "local_tool_call", callId: "daily-gap-provider", name: "daily_note_context", arguments: {} },
    ],
    localSkills: [],
    canonicalReadPaths: [],
    requiredRereads: [],
    hostedWebSearchProbeAttempted: false,
    completedSteps: 0,
    pendingToolStep: {
      completedSteps: 1,
      name: "daily_note_context",
      providerCallId: "daily-gap-provider",
      toolCallId: "daily-gap-tool",
    },
  });
  await store.requestToolCall("daily-gap-run", {
    type: "tool_call.requested",
    protocolVersion: 1,
    eventId: "daily-gap-requested",
    conversationId: "daily-gap-conversation",
    agentRunId: "daily-gap-run",
    sequence: 2,
    toolCallId: "daily-gap-tool",
    tool: { kind: "local", name: "daily_note_context", arguments: {} },
  });
  const dailyContextResult = {
    ok: true,
    value: {
      type: "daily_note_context",
      resolvedDate: "2026-07-14",
      dateFormat: "YYYY-MM-DD",
      targetPath: "journal/2026-07-14.md",
      targetExists: false,
      targetVersion: "missing",
      templatePath: null,
      templateContent: null,
      templateVersion: null,
    },
  };
  await store.completeToolCall("daily-gap-run", dailyContextResult, {
    type: "tool_call.completed",
    protocolVersion: 1,
    eventId: "daily-gap-completed",
    conversationId: "daily-gap-conversation",
    agentRunId: "daily-gap-run",
    sequence: 3,
    toolCallId: "daily-gap-tool",
    tool: { kind: "local", name: "daily_note_context" },
    status: "completed",
  }, "daily-gap-result");
  await store.interruptAgentRun("daily-gap-run");
  await store.close();

  const instance = await startRuntime(statePath, "daily-gap-token");
  t.after(async () => {
    await stopRuntime(instance);
    await rm(directory, { recursive: true, force: true });
  });
  const socket = await connect(instance);
  const interrupted = await new Promise((resolve, reject) => {
    const timeout = setTimeout(() => reject(new Error("Daily gap interruption was not replayed")), 5_000);
    socket.on("message", function onReplay(data) {
      const event = JSON.parse(data.toString("utf8"));
      if (event.agentRunId !== "daily-gap-run" || event.type !== "agent_run.interrupted") return;
      clearTimeout(timeout);
      socket.off("message", onReplay);
      resolve(event);
    });
  });
  socket.send(JSON.stringify({
    type: "event.ack",
    protocolVersion: 1,
    eventId: "daily-gap-ack",
    acknowledgedEventId: interrupted.eventId,
    conversationId: interrupted.conversationId,
    agentRunId: interrupted.agentRunId,
    sequence: interrupted.sequence,
  }));

  let proposals = 0;
  let proposalPaths = [];
  const completed = new Promise((resolve, reject) => {
    const timeout = setTimeout(() => reject(new Error("Daily gap Resume timed out")), 8_000);
    socket.on("message", function onMessage(data) {
      const event = JSON.parse(data.toString("utf8"));
      if (event.agentRunId !== "daily-gap-run") return;
      if (respondPlanningMemoryList(socket, event)) return;
      if (event.type === "tool_call.requested" && event.tool.name === "agent_contract_read") {
        socket.send(JSON.stringify(toolResult(event, {
          ok: true,
          value: {
            type: "agent_contract_read",
            path: "agent.md",
            modifiedVersion: "mtime:1:size:8",
            contentHash: "sha256:contract",
            content: "# Agent",
          },
        })));
      } else if (event.type === "tool_call.requested" && event.tool.name === "vault_read") {
        socket.send(JSON.stringify(toolResult(event, {
          ok: false,
          error: { code: "not_found", message: "Memory index is missing." },
        })));
      } else if (event.type === "tool_call.requested" && event.tool.name === "vault_propose_changes") {
        proposals += 1;
        proposalPaths = event.tool.arguments.actions.map(({ path: targetPath }) => targetPath);
        socket.send(JSON.stringify(toolResult(event, {
          ok: true,
          value: {
            type: "vault_propose_changes",
            batchId: event.tool.arguments.batchId,
            decision: "applied",
            checkpointRef: `refs/offeragent/checkpoints/${event.tool.arguments.batchId}`,
            targets: event.tool.arguments.actions.map(({ path: targetPath }) => ({
              path: targetPath,
              beforeHash: "missing",
              afterHash: "sha256:after",
            })),
          },
        })));
      } else if (event.type === "agent_run.completed") {
        clearTimeout(timeout);
        socket.off("message", onMessage);
        resolve(event);
      } else if (event.type === "agent_run.failed") {
        clearTimeout(timeout);
        socket.off("message", onMessage);
        reject(new Error(event.error.message));
      }
    });
  });
  socket.send(JSON.stringify({
    type: "agent_run.resume",
    protocolVersion: 1,
    eventId: "daily-gap-resume",
    conversationId: "daily-gap-conversation",
    agentRunId: "daily-gap-run",
    sequence: 0,
  }));
  await completed;
  assert.equal(proposals, 1);
  assert.deepEqual(proposalPaths, [
    "journal/2026-07-14.md",
    "memory/study/retrieval-evaluation.md",
    "memory/MEMORY.md",
  ]);
  socket.close();
});

test("a restored pending confirmation continues from Apply or Reject without a second Resume", async (t) => {
  const directory = await mkdtemp(path.join(os.tmpdir(), "offeragent-pending-resume-"));
  const instance = await startRuntime(path.join(directory, "state.db"), "pending-resume-token");
  t.after(async () => {
    await stopRuntime(instance);
    await rm(directory, { recursive: true, force: true });
  });
  const proposal = {
    batchId: "pending-resume-batch",
    idempotencyKey: "pending-resume-batch-key",
    task: "Keep one pending decision",
    actions: [{
      actionId: "pending-resume-action",
      idempotencyKey: "pending-resume-action-key",
      operation: "append",
      path: "notes/resume.md",
      expectedVersion: "mtime:1:size:4",
      content: "next\n",
    }],
  };
  const firstSocket = await connect(instance);
  let pendingToolCall;
  await new Promise((resolve, reject) => {
    const timeout = setTimeout(() => reject(new Error("Run did not reach confirmation")), 5_000);
    firstSocket.on("message", function onMessage(data) {
      const event = JSON.parse(data.toString("utf8"));
      if (event.agentRunId !== "pending-resume-run" || event.type !== "tool_call.requested") return;
      if (respondPlanningMemoryList(firstSocket, event)) return;
      if (event.tool.name === "agent_contract_read") {
        firstSocket.send(JSON.stringify(toolResult(event, {
          ok: true,
          value: {
            type: "agent_contract_read",
            path: "agent.md",
            modifiedVersion: "mtime:1:size:8",
            contentHash: "sha256:contract",
            content: "# Agent",
          },
        })));
      } else if (event.tool.name === "vault_propose_changes") {
        pendingToolCall = event;
        clearTimeout(timeout);
        firstSocket.terminate();
        resolve();
      }
    });
    firstSocket.send(JSON.stringify({
      type: "agent_run.start",
      protocolVersion: 1,
      eventId: "pending-resume-start",
      conversationId: "pending-resume-conversation",
      agentRunId: "pending-resume-run",
      sequence: 0,
      model: "fake-interview-model",
      input: { role: "user", text: `vault_propose_changes ${JSON.stringify(proposal)}` },
    }));
  });
  await once(firstSocket, "close");

  const resumedSocket = await connect(instance);
  await new Promise((resolve, reject) => {
    const timeout = setTimeout(() => reject(new Error("Pending Run was not restored as Interrupted")), 5_000);
    resumedSocket.on("message", function onInterrupted(data) {
      const event = JSON.parse(data.toString("utf8"));
      if (event.agentRunId !== "pending-resume-run" || event.type !== "agent_run.interrupted") return;
      clearTimeout(timeout);
      resumedSocket.off("message", onInterrupted);
      resolve();
    });
  });
  let repeatedProposal = false;
  let resumeStarted = false;
  const completed = new Promise((resolve, reject) => {
    const timeout = setTimeout(() => reject(new Error("Recovered decision did not continue")), 5_000);
    resumedSocket.on("message", function onMessage(data) {
      const event = JSON.parse(data.toString("utf8"));
      if (event.agentRunId !== "pending-resume-run") return;
      if (event.type === "agent_run.resumed") resumeStarted = true;
      if (resumeStarted && respondPlanningMemoryList(resumedSocket, event)) return;
      if (resumeStarted && event.type === "tool_call.requested" && event.tool.name === "agent_contract_read") {
        resumedSocket.send(JSON.stringify(toolResult(event, {
          ok: true,
          value: {
            type: "agent_contract_read", path: "agent.md", modifiedVersion: "mtime:1:size:8",
            contentHash: "sha256:contract", content: "# Agent",
          },
        })));
      }
      if (resumeStarted && event.type === "tool_call.requested" && event.tool.name === "vault_propose_changes") {
        repeatedProposal = true;
      }
      if (event.type === "agent_run.completed") {
        clearTimeout(timeout);
        resumedSocket.off("message", onMessage);
        resolve();
      }
    });
  });
  resumedSocket.send(JSON.stringify({
    type: "agent_run.resume",
    protocolVersion: 1,
    eventId: "pending-decision-resume",
    conversationId: "pending-resume-conversation",
    agentRunId: "pending-resume-run",
    sequence: 0,
    recoveredToolResult: {
      eventId: "pending-decision-result",
      toolCallId: pendingToolCall.toolCallId,
      result: {
        ok: true,
        value: {
          type: "vault_propose_changes",
          batchId: proposal.batchId,
          decision: "rejected",
          targets: [{
            path: "notes/resume.md",
            beforeHash: "sha256:before",
            afterHash: "sha256:before",
          }],
        },
      },
    },
  }));
  await completed;
  assert.equal(repeatedProposal, false);
  resumedSocket.close();
});

test("Resume revalidates committed Evidence and replans before using a changed source", async (t) => {
  const directory = await mkdtemp(path.join(os.tmpdir(), "offeragent-stale-resume-"));
  const instance = await startRuntime(path.join(directory, "state.db"), "stale-resume-token");
  t.after(async () => {
    await stopRuntime(instance);
    await rm(directory, { recursive: true, force: true });
  });
  const firstSocket = await connect(instance);
  await new Promise((resolve, reject) => {
    const timeout = setTimeout(() => reject(new Error("Initial stale flow did not reach search")), 5_000);
    firstSocket.on("message", function onMessage(data) {
      const event = JSON.parse(data.toString("utf8"));
      if (event.agentRunId !== "stale-resume-run" || event.type !== "tool_call.requested") return;
      if (respondPlanningMemoryList(firstSocket, event)) return;
      if (event.tool.name === "agent_contract_read") {
        firstSocket.send(JSON.stringify(toolResult(event, {
          ok: true,
          value: {
            type: "agent_contract_read", path: "agent.md", modifiedVersion: "mtime:1:size:8",
            contentHash: "sha256:contract", content: "# Agent",
          },
        })));
      } else if (event.tool.name === "vault_read") {
        firstSocket.send(JSON.stringify(toolResult(event, {
          ok: true,
          value: {
            type: "vault_read", path: "notes/stale.md", lineStart: 1, lineEnd: 1,
            modifiedVersion: "mtime:1:size:3", contentHash: "sha256:old", content: "old",
            truncated: false,
          },
        })));
      } else if (event.tool.name === "vault_search") {
        clearTimeout(timeout);
        firstSocket.terminate();
        resolve();
      }
    });
    firstSocket.send(JSON.stringify({
      type: "agent_run.start", protocolVersion: 1, eventId: "stale-resume-start",
      conversationId: "stale-resume-conversation", agentRunId: "stale-resume-run", sequence: 0,
      model: "fake-interview-model", input: { role: "user", text: "stale_evidence_flow notes/stale.md" },
    }));
  });
  await once(firstSocket, "close");

  const resumedSocket = await connect(instance);
  const resumedTools = [];
  await new Promise((resolve, reject) => {
    const timeout = setTimeout(() => reject(new Error("Interrupted stale Run was not replayed")), 5_000);
    resumedSocket.on("message", function onInterrupted(data) {
      const event = JSON.parse(data.toString("utf8"));
      if (event.agentRunId !== "stale-resume-run" || event.type !== "agent_run.interrupted") return;
      clearTimeout(timeout);
      resumedSocket.off("message", onInterrupted);
      resolve();
    });
  });
  const completed = new Promise((resolve, reject) => {
    const timeout = setTimeout(() => reject(new Error("Stale Run did not complete after replanning")), 5_000);
    resumedSocket.on("message", function onMessage(data) {
      const event = JSON.parse(data.toString("utf8"));
      if (event.agentRunId !== "stale-resume-run") return;
      if (event.type === "tool_call.requested") {
        resumedTools.push(event.tool.name);
        if (respondPlanningMemoryList(resumedSocket, event)) {
          return;
        } else if (event.tool.name === "agent_contract_read") {
          resumedSocket.send(JSON.stringify(toolResult(event, {
            ok: true,
            value: {
              type: "agent_contract_read", path: "agent.md", modifiedVersion: "mtime:1:size:8",
              contentHash: "sha256:contract", content: "# Agent",
            },
          })));
        } else if (event.tool.name === "vault_read") {
          resumedSocket.send(JSON.stringify(toolResult(event, {
            ok: true,
            value: {
              type: "vault_read", path: "notes/stale.md", lineStart: 1, lineEnd: 1,
              modifiedVersion: "mtime:2:size:3", contentHash: "sha256:new", content: "new",
              truncated: false,
            },
          })));
        } else if (event.tool.name === "vault_search") {
          resumedSocket.send(JSON.stringify(toolResult(event, {
            ok: true,
            value: { type: "vault_search", entries: [], truncated: false },
          })));
        }
      } else if (event.type === "agent_run.completed") {
        clearTimeout(timeout);
        resumedSocket.off("message", onMessage);
        resolve();
      }
    });
  });
  resumedSocket.send(JSON.stringify({
    type: "agent_run.resume", protocolVersion: 1, eventId: "stale-explicit-resume",
    conversationId: "stale-resume-conversation", agentRunId: "stale-resume-run", sequence: 0,
  }));
  await completed;
  assert.deepEqual(resumedTools.slice(0, 3), ["agent_contract_read", "planning_memory_list", "vault_read"]);
  assert.ok(resumedTools.includes("vault_search"));
  resumedSocket.close();
});

test("an Interrupted Run resumes after the Runtime process itself restarts", async (t) => {
  const directory = await mkdtemp(path.join(os.tmpdir(), "offeragent-process-resume-"));
  const statePath = path.join(directory, "state.db");
  const firstInstance = await startRuntime(statePath, "process-resume-token-1");
  let secondInstance;
  t.after(async () => {
    await stopRuntime(firstInstance);
    if (secondInstance) await stopRuntime(secondInstance);
    await rm(directory, { recursive: true, force: true });
  });
  const firstSocket = await connect(firstInstance);
  await new Promise((resolve, reject) => {
    const timeout = setTimeout(() => reject(new Error("Run did not reach its restart boundary")), 5_000);
    firstSocket.on("message", function onMessage(data) {
      const event = JSON.parse(data.toString("utf8"));
      if (event.agentRunId !== "process-resume-run" || event.type !== "tool_call.requested") return;
      if (respondPlanningMemoryList(firstSocket, event)) return;
      if (event.tool.name === "agent_contract_read") {
        firstSocket.send(JSON.stringify(toolResult(event, {
          ok: true,
          value: {
            type: "agent_contract_read", path: "agent.md", modifiedVersion: "mtime:1:size:8",
            contentHash: "sha256:contract", content: "# Agent",
          },
        })));
      } else if (event.tool.name === "vault_read") {
        clearTimeout(timeout);
        firstSocket.terminate();
        resolve();
      }
    });
    firstSocket.send(JSON.stringify({
      type: "agent_run.start", protocolVersion: 1, eventId: "process-resume-start",
      conversationId: "process-resume-conversation", agentRunId: "process-resume-run", sequence: 0,
      model: "fake-interview-model", input: { role: "user", text: "vault_read notes/process.md" },
    }));
  });
  await once(firstSocket, "close");
  const verificationSocket = await connect(firstInstance);
  await new Promise((resolve, reject) => {
    const timeout = setTimeout(() => reject(new Error("Run was not durably Interrupted before restart")), 5_000);
    verificationSocket.on("message", function onMessage(data) {
      const event = JSON.parse(data.toString("utf8"));
      if (event.agentRunId !== "process-resume-run" || event.type !== "agent_run.interrupted") return;
      clearTimeout(timeout);
      verificationSocket.off("message", onMessage);
      resolve();
    });
  });
  verificationSocket.close();
  await once(verificationSocket, "close");
  await stopRuntime(firstInstance);
  secondInstance = await startRuntime(statePath, "process-resume-token-2");
  const resumedSocket = await connect(secondInstance);
  const observedEvents = [];
  let processResumeStarted = false;
  const completed = new Promise((resolve, reject) => {
    const timeout = setTimeout(
      () => reject(new Error(`Process-restarted Run did not complete: ${JSON.stringify(observedEvents)}`)),
      5_000,
    );
    resumedSocket.on("message", function onMessage(data) {
      const event = JSON.parse(data.toString("utf8"));
      if (event.agentRunId !== "process-resume-run") return;
      observedEvents.push(event.type === "tool_call.requested" ? `${event.type}:${event.tool.name}` : event.type);
      if (event.type === "agent_run.resumed") processResumeStarted = true;
      if (event.type === "agent_run.interrupted") {
        resumedSocket.send(JSON.stringify({
          type: "agent_run.resume", protocolVersion: 1, eventId: "process-explicit-resume",
          conversationId: "process-resume-conversation", agentRunId: "process-resume-run", sequence: 0,
        }));
      } else if (processResumeStarted && respondPlanningMemoryList(resumedSocket, event)) {
        return;
      } else if (
        processResumeStarted &&
        event.type === "tool_call.requested" &&
        event.tool.name === "agent_contract_read"
      ) {
        resumedSocket.send(JSON.stringify(toolResult(event, {
          ok: true,
          value: {
            type: "agent_contract_read", path: "agent.md", modifiedVersion: "mtime:1:size:8",
            contentHash: "sha256:contract", content: "# Agent",
          },
        })));
      } else if (
        processResumeStarted &&
        event.type === "tool_call.requested" &&
        event.tool.name === "vault_read"
      ) {
        resumedSocket.send(JSON.stringify(toolResult(event, {
          ok: true,
          value: {
            type: "vault_read", path: "notes/process.md", lineStart: 1, lineEnd: 1,
            modifiedVersion: "mtime:2:size:4", contentHash: "sha256:fact", content: "fact",
            truncated: false,
          },
        })));
      } else if (event.type === "agent_run.completed") {
        clearTimeout(timeout);
        resumedSocket.off("message", onMessage);
        resolve();
      }
    });
    resumedSocket.once("close", (code, reason) => {
      clearTimeout(timeout);
      reject(new Error(`Process resume socket closed ${code}: ${reason.toString("utf8")}; ${JSON.stringify(observedEvents)}`));
    });
  });
  await completed;
  resumedSocket.close();
});

test("a partial model stream is discarded and its whole Provider step reruns", async (t) => {
  const directory = await mkdtemp(path.join(os.tmpdir(), "offeragent-partial-resume-"));
  const instance = await startRuntime(path.join(directory, "state.db"), "partial-resume-token");
  t.after(async () => {
    await stopRuntime(instance);
    await rm(directory, { recursive: true, force: true });
  });
  const firstSocket = await connect(instance);
  await new Promise((resolve, reject) => {
    const timeout = setTimeout(() => reject(new Error("Partial stream boundary was not reached")), 5_000);
    firstSocket.on("message", function onMessage(data) {
      const event = JSON.parse(data.toString("utf8"));
      if (event.agentRunId !== "partial-resume-run") return;
      if (respondPlanningMemoryList(firstSocket, event)) return;
      if (event.type === "tool_call.requested" && event.tool.name === "agent_contract_read") {
        firstSocket.send(JSON.stringify(toolResult(event, {
          ok: true,
          value: {
            type: "agent_contract_read", path: "agent.md", modifiedVersion: "mtime:1:size:8",
            contentHash: "sha256:contract", content: "# Agent",
          },
        })));
      } else if (event.type === "agent_run.delta") {
        clearTimeout(timeout);
        firstSocket.terminate();
        resolve();
      }
    });
    firstSocket.send(JSON.stringify({
      type: "agent_run.start", protocolVersion: 1, eventId: "partial-resume-start",
      conversationId: "partial-resume-conversation", agentRunId: "partial-resume-run", sequence: 0,
      model: "fake-interview-model", input: { role: "user", text: "citation_then_tool" },
    }));
  });
  await once(firstSocket, "close");
  const resumedSocket = await connect(instance);
  let resumeStarted = false;
  const resumedDeltas = [];
  const completed = new Promise((resolve, reject) => {
    const timeout = setTimeout(() => reject(new Error("Partial stream did not rerun")), 5_000);
    resumedSocket.on("message", function onMessage(data) {
      const event = JSON.parse(data.toString("utf8"));
      if (event.agentRunId !== "partial-resume-run") return;
      if (event.type === "agent_run.interrupted") {
        resumedSocket.send(JSON.stringify({
          type: "agent_run.resume", protocolVersion: 1, eventId: "partial-explicit-resume",
          conversationId: "partial-resume-conversation", agentRunId: "partial-resume-run", sequence: 0,
        }));
      } else if (event.type === "agent_run.resumed") {
        resumeStarted = true;
      } else if (resumeStarted && event.type === "agent_run.delta") {
        resumedDeltas.push(event.delta);
      } else if (resumeStarted && event.type === "tool_call.requested") {
        const result = event.tool.name === "planning_memory_list"
          ? {
              ok: true,
              value: { type: "planning_memory_list", topics: [], truncated: false },
            }
          : event.tool.name === "agent_contract_read"
          ? {
              ok: true,
              value: {
                type: "agent_contract_read", path: "agent.md", modifiedVersion: "mtime:1:size:8",
                contentHash: "sha256:contract", content: "# Agent",
              },
            }
          : {
              ok: true,
              value: {
                type: "vault_read", path: "notes/citation.md", lineStart: 1, lineEnd: 1,
                modifiedVersion: "mtime:2:size:4", contentHash: "sha256:fact", content: "fact",
                truncated: false,
              },
            };
        resumedSocket.send(JSON.stringify(toolResult(event, result)));
      } else if (event.type === "agent_run.completed") {
        clearTimeout(timeout);
        assert.equal(event.output.text, "Final answer without a citation");
        resolve();
      }
    });
  });
  await completed;
  assert.equal(resumedDeltas.join(""), "Old cited [1]Final answer without a citation");
  resumedSocket.close();
});
