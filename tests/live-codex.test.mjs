import assert from "node:assert/strict";
import { spawn } from "node:child_process";
import { mkdtemp, readdir, rm } from "node:fs/promises";
import { request } from "node:http";
import os from "node:os";
import path from "node:path";
import test from "node:test";
import { fileURLToPath } from "node:url";
import { deflateSync } from "node:zlib";
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

function crc32(bytes) {
  let crc = 0xffffffff;
  for (const byte of bytes) {
    crc ^= byte;
    for (let bit = 0; bit < 8; bit += 1) {
      crc = (crc >>> 1) ^ (crc & 1 ? 0xedb88320 : 0);
    }
  }
  return (crc ^ 0xffffffff) >>> 0;
}

function pngChunk(type, data) {
  const typeBytes = Buffer.from(type, "ascii");
  const result = Buffer.alloc(12 + data.length);
  result.writeUInt32BE(data.length, 0);
  typeBytes.copy(result, 4);
  data.copy(result, 8);
  result.writeUInt32BE(crc32(Buffer.concat([typeBytes, data])), 8 + data.length);
  return result;
}

function solidMagentaPng() {
  const width = 32;
  const height = 32;
  const ihdr = Buffer.alloc(13);
  ihdr.writeUInt32BE(width, 0);
  ihdr.writeUInt32BE(height, 4);
  ihdr.set([8, 2, 0, 0, 0], 8);
  const scanlines = Buffer.alloc(height * (1 + width * 3));
  for (let row = 0; row < height; row += 1) {
    const offset = row * (1 + width * 3);
    scanlines[offset] = 0;
    for (let column = 0; column < width; column += 1) {
      scanlines.set([255, 0, 255], offset + 1 + column * 3);
    }
  }
  return Buffer.concat([
    Buffer.from([0x89, 0x50, 0x4e, 0x47, 0x0d, 0x0a, 0x1a, 0x0a]),
    pngChunk("IHDR", ihdr),
    pngChunk("IDAT", deflateSync(scanlines)),
    pngChunk("IEND", Buffer.alloc(0)),
  ]);
}

test(
  "a live Codex subscription streams a real answer",
  { skip: enabled ? false : "set OFFERAGENT_LIVE_CODEX=1 to use the local Codex login" },
  async (t) => {
    const temporaryDirectory = await mkdtemp(path.join(os.tmpdir(), "offeragent-live-state-"));
    const statePath = path.join(temporaryDirectory, "state.db");
    const attachmentsPath = path.join(temporaryDirectory, "attachments");
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
        "--attachments-path",
        attachmentsPath,
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
    socket.on("message", (data) => {
      const event = JSON.parse(data.toString("utf8"));
      if (
        event.type !== "tool_call.requested" ||
        (event.tool.name !== "agent_contract_read" && event.tool.name !== "planning_memory_list")
      ) {
        return;
      }
      socket.send(
        JSON.stringify({
          type: "tool_result",
          protocolVersion: 1,
          eventId: `live-contract-result-${event.toolCallId}`,
          conversationId: event.conversationId,
          agentRunId: event.agentRunId,
          sequence: event.sequence,
          toolCallId: event.toolCallId,
          result: {
            ok: true,
            value: event.tool.name === "agent_contract_read"
              ? {
                  type: "agent_contract_read",
                  path: "agent.md",
                  modifiedVersion: "mtime:1:size:52",
                  contentHash: "sha256:live-agent-contract",
                  content: "# Live Agent Contract\nUse only explicit Vault evidence.",
                }
              : { type: "planning_memory_list", topics: [], truncated: false },
          },
        }),
      );
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

    const imageRunId = "live-agent-run-image";
    const imageUpload = await fetch(`http://127.0.0.1:${ready.port}/attachments`, {
      method: "POST",
      headers: {
        authorization: `Bearer ${token}`,
        "content-type": "image/png",
        "x-offeragent-agent-run-id": imageRunId,
        "x-offeragent-conversation-id": "live-conversation-1",
        "x-offeragent-file-name": "solid-magenta.png",
      },
      body: solidMagentaPng(),
    });
    assert.equal(imageUpload.status, 201);
    const stagedImage = await imageUpload.json();
    const imageOutput = await new Promise((resolve, reject) => {
      let text = "";
      const timeout = setTimeout(() => reject(new Error("Live Codex image Run timed out")), 60_000);
      socket.on("message", function onMessage(data) {
        const event = JSON.parse(data.toString("utf8"));
        if (event.agentRunId !== imageRunId) return;
        if (event.type === "agent_run.delta") text += event.delta;
        if (event.type === "agent_run.failed") {
          clearTimeout(timeout);
          socket.off("message", onMessage);
          reject(new Error(`${event.error.code}: ${event.error.message}`));
        }
        if (event.type === "agent_run.completed") {
          clearTimeout(timeout);
          socket.off("message", onMessage);
          resolve(event.output.text || text);
        }
      });
      socket.send(JSON.stringify({
        type: "agent_run.start",
        protocolVersion: 1,
        eventId: "live-client-image-event",
        conversationId: "live-conversation-1",
        agentRunId: imageRunId,
        sequence: 0,
        model: model.id,
        input: {
          role: "user",
          text: "Name the dominant color in the attached image in one word.",
          attachments: [{ attachmentId: stagedImage.attachmentId, order: 0 }],
        },
      }));
    });
    assert.match(imageOutput, /magenta/i);
    assert.deepEqual(await readdir(attachmentsPath), []);

    let requestedVaultRead = false;
    const toolOutput = await new Promise((resolve, reject) => {
      const timeout = setTimeout(() => reject(new Error("Live Codex tool Run timed out")), 60_000);
      socket.on("message", function onMessage(data) {
        const event = JSON.parse(data.toString("utf8"));
        if (event.agentRunId !== "live-agent-run-tool") return;
        if (event.type === "tool_call.requested") {
          const isPrelude =
            event.tool.name === "agent_contract_read" ||
            event.tool.name === "planning_memory_list";
          if (isPrelude) return;
          requestedVaultRead = event.tool.name === "vault_read";
          socket.send(
            JSON.stringify({
              type: "tool_result",
              protocolVersion: 1,
              eventId: "live-tool-result",
              conversationId: event.conversationId,
              agentRunId: event.agentRunId,
              sequence: event.sequence,
              toolCallId: event.toolCallId,
              result: {
                ok: true,
                value: {
                  type: "vault_read",
                  path: "notes/live.md",
                  lineStart: 1,
                  lineEnd: 1,
                  modifiedVersion: "mtime:1:size:27",
                  contentHash: "sha256:live-tool-smoke",
                  content: "OFFERAGENT_VAULT_TOOL_OK",
                  truncated: false,
                },
              },
            }),
          );
        }
        if (event.type === "agent_run.failed") {
          clearTimeout(timeout);
          socket.off("message", onMessage);
          reject(new Error(`${event.error.code}: ${event.error.message}`));
        }
        if (event.type === "agent_run.completed") {
          clearTimeout(timeout);
          socket.off("message", onMessage);
          resolve(event.output.text);
        }
      });
      socket.send(
        JSON.stringify({
          type: "agent_run.start",
          protocolVersion: 1,
          eventId: "live-client-tool-event",
          conversationId: "live-conversation-1",
          agentRunId: "live-agent-run-tool",
          sequence: 0,
          model: model.id,
          input: {
            role: "user",
            text: "You must call vault_read for notes/live.md lines 1-1, then reply with the exact content returned by the tool.",
          },
        }),
      );
    });
    assert.equal(requestedVaultRead, true);
    assert.match(toolOutput, /OFFERAGENT_VAULT_TOOL_OK/);
  },
);
