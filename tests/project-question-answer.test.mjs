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

test("one project interview answer uses only exact registered Project Evidence and states gaps", async (t) => {
  const temporaryDirectory = await mkdtemp(path.join(os.tmpdir(), "offeragent-project-answer-"));
  const token = "project-answer-token";
  const runtime = spawn(process.execPath, [
    runtimeEntry, "--port", "0", "--token", token, "--parent-pid", `${process.pid}`,
    "--provider", "fake", "--fake-scenario", "project-question-answer",
    "--state-path", path.join(temporaryDirectory, "state.db"),
  ], { stdio: ["ignore", "pipe", "pipe"], windowsHide: true });
  let stderr = "";
  runtime.stderr.on("data", (chunk) => { stderr += chunk.toString("utf8"); });
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

  const events = await runWithToolPeer(socket, {
    type: "agent_run.start", protocolVersion: 1, eventId: "project-answer-start",
    conversationId: "project-answer-conversation", agentRunId: "project-answer-run", sequence: 0,
    model: "fake-interview-model",
    input: { role: "user", text: "In OfferAgent, how did you make cache invalidation safe? Answer one project interview question from the registered project." },
  }, (event) => {
    assert.ok(["project_list", "project_search", "project_read"].includes(event.tool.name));
    assert.equal(event.tool.arguments.projectId, "offeragent");
    if (event.tool.name === "project_list") {
      return { ok: true, value: {
        type: "project_list", projectId: "offeragent", truncated: false,
        entries: [{ path: "src/cache.ts", modifiedVersion: "mtime:1:size:150", contentHash: `sha256:${"1".repeat(64)}` }],
      } };
    }
    if (event.tool.name === "project_search") {
      return { ok: true, value: {
        type: "project_search", projectId: "offeragent", truncated: false,
        entries: [{
          path: "src/cache.ts", modifiedVersion: "mtime:1:size:150", contentHash: `sha256:${"1".repeat(64)}`,
          snippets: [{ content: "invalidate only when current.version === expectedVersion", lineStart: 10, lineEnd: 10, truncated: false }],
        }],
      } };
    }
    assert.equal(event.tool.name, "project_read");
    assert.equal(event.tool.arguments.path, "src/cache.ts");
    return { ok: true, value: {
      type: "project_read", projectId: "offeragent", path: "src/cache.ts",
      evidencePath: "project/offeragent/src/cache.ts", lineStart: 1, lineEnd: 4,
      modifiedVersion: "mtime:1:size:150", contentHash: `sha256:${"1".repeat(64)}`,
      content: "export function invalidate(key, expectedVersion) {\n  const current = cache.get(key);\n  if (current.version === expectedVersion) cache.delete(key);\n}",
      truncated: false,
    } };
  });

  assert.equal(events.at(-1).type, "agent_run.completed", stderr);
  const projectCalls = events.filter((event) => event.type === "tool_call.requested" && event.tool.name.startsWith("project_"));
  assert.deepEqual(projectCalls.map((event) => event.tool.name), ["project_list", "project_search", "project_read"]);
  assert.equal(events.some((event) => event.type === "tool_call.requested" && ["vault_propose_changes", "research_browser", "web_read"].includes(event.tool.name)), false);
  assert.match(events.at(-1).output.text, /rejecting stale versions|older request cannot invalidate a newer/i);
  assert.match(events.at(-1).output.text, /Project Evidence: project\/offeragent\/src\/cache\.ts:1-4/);
  assert.match(events.at(-1).output.text, /Gap:.*production.*metrics/i);
  assert.doesNotMatch(JSON.stringify(events), /[A-Z]:\\|project-root/i);
});
