import assert from "node:assert/strict";
import { spawn } from "node:child_process";
import { mkdtemp, rm } from "node:fs/promises";
import { request } from "node:http";
import os from "node:os";
import path from "node:path";
import test from "node:test";
import { fileURLToPath } from "node:url";
import WebSocket from "ws";

const repositoryRoot = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..");
const runtimeEntry = path.join(repositoryRoot, "packages", "runtime", "dist", "cli.js");
const enabled = process.env.OFFERAGENT_LIVE_CODEX === "1";

function handshake(stream) {
  return new Promise((resolve, reject) => {
    let buffer = "";
    const timeout = setTimeout(() => reject(new Error("Runtime startup timed out")), 10_000);
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

function getModels(port, token) {
  return new Promise((resolve, reject) => {
    const outgoing = request(
      { host: "127.0.0.1", port, path: "/models", headers: { authorization: `Bearer ${token}` } },
      (response) => {
        let body = "";
        response.setEncoding("utf8");
        response.on("data", (chunk) => (body += chunk));
        response.on("end", () => {
          const payload = JSON.parse(body);
          if (response.statusCode !== 200) reject(new Error(`${payload.code}: ${payload.message}`));
          else resolve(payload.models);
        });
      },
    );
    outgoing.once("error", reject);
    outgoing.end();
  });
}

test(
  "a live Codex subscription streams a real answer",
  { skip: enabled ? false : "set OFFERAGENT_LIVE_CODEX=1 to use the local Codex login" },
  async (t) => {
    const temporaryDirectory = await mkdtemp(path.join(os.tmpdir(), "offeragent-live-state-"));
    const statePath = path.join(temporaryDirectory, "state.db");
    const token = "offeragent-live-smoke-runtime-token";
    const runtime = spawn(
      process.execPath,
      [
        runtimeEntry,
        "--port",
        "0",
        "--token",
        token,
        "--parent-pid",
        `${process.pid}`,
        "--provider",
        "codex",
        "--state-path",
        statePath,
      ],
      { stdio: ["ignore", "pipe", "pipe"], windowsHide: true },
    );
    t.after(async () => {
      runtime.kill();
      await rm(temporaryDirectory, { recursive: true, force: true });
    });
    const ready = await handshake(runtime.stdout);
    const models = await getModels(ready.port, token);
    const model = models.find((candidate) => candidate.id === "gpt-5.4") ?? models[0];
    assert.ok(model?.id);

    const socket = new WebSocket(`ws://127.0.0.1:${ready.port}/events`, {
      headers: { authorization: `Bearer ${token}` },
    });
    t.after(() => socket.close());
    await new Promise((resolve, reject) => {
      socket.once("open", resolve);
      socket.once("error", reject);
    });
    const output = await new Promise((resolve, reject) => {
      let text = "";
      const timeout = setTimeout(() => reject(new Error("Live Agent Run timed out")), 60_000);
      socket.on("message", (data) => {
        const event = JSON.parse(data.toString("utf8"));
        if (event.type === "agent_run.delta") text += event.delta;
        if (event.type === "agent_run.failed") {
          clearTimeout(timeout);
          reject(new Error(`${event.error.code}: ${event.error.message}`));
        }
        if (event.type === "agent_run.completed") {
          clearTimeout(timeout);
          resolve(event.output.text || text);
        }
      });
      socket.send(
        JSON.stringify({
          type: "agent_run.start",
          protocolVersion: 1,
          eventId: "live-client-event-1",
          conversationId: "live-conversation-1",
          agentRunId: "live-agent-run-1",
          sequence: 0,
          model: model.id,
          input: { role: "user", text: "Reply with exactly OFFERAGENT_LIVE_OK." },
        }),
      );
    });
    assert.match(output, /OFFERAGENT_LIVE_OK/);
  },
);
