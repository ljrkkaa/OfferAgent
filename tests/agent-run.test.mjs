import assert from "node:assert/strict";
import { spawn } from "node:child_process";
import { request } from "node:http";
import { createServer } from "node:http";
import { mkdtemp, rm, writeFile } from "node:fs/promises";
import os from "node:os";
import path from "node:path";
import test from "node:test";
import { fileURLToPath } from "node:url";
import WebSocket from "ws";

const repositoryRoot = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..");
const runtimeEntry = path.join(repositoryRoot, "packages", "runtime", "dist", "cli.js");

function readJsonLine(stream, timeoutMs = 5_000) {
  return new Promise((resolve, reject) => {
    let buffer = "";
    const timeout = setTimeout(() => reject(new Error("Timed out waiting for Runtime handshake")), timeoutMs);
    stream.on("data", function onData(chunk) {
      buffer += chunk.toString("utf8");
      const newline = buffer.indexOf("\n");
      if (newline === -1) return;
      clearTimeout(timeout);
      stream.off("data", onData);
      resolve(JSON.parse(buffer.slice(0, newline)));
    });
    stream.once("error", reject);
  });
}

function getJson(port, token, pathname) {
  return new Promise((resolve, reject) => {
    const outgoing = request(
      {
        host: "127.0.0.1",
        port,
        path: pathname,
        headers: { authorization: `Bearer ${token}` },
      },
      (response) => {
        let body = "";
        response.setEncoding("utf8");
        response.on("data", (chunk) => (body += chunk));
        response.on("end", () => resolve({ statusCode: response.statusCode, body: JSON.parse(body) }));
      },
    );
    outgoing.once("error", reject);
    outgoing.end();
  });
}

function collectRunEvents(socket, agentRunId) {
  return new Promise((resolve, reject) => {
    const events = [];
    const timeout = setTimeout(() => reject(new Error("Timed out waiting for Agent Run")), 5_000);
    socket.on("message", (data) => {
      const event = JSON.parse(data.toString("utf8"));
      if (event.agentRunId !== agentRunId) return;
      events.push(event);
      if (event.type === "agent_run.completed") {
        clearTimeout(timeout);
        resolve(events);
      }
      if (event.type === "agent_run.failed") {
        clearTimeout(timeout);
        reject(new Error(`${event.error.code}: ${event.error.message}`));
      }
    });
  });
}

test("a deterministic Provider lists models and streams one Agent Run", async (t) => {
  const token = "agent-run-test-token";
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
      "fake",
    ],
    { stdio: ["ignore", "pipe", "pipe"], windowsHide: true },
  );
  t.after(() => runtime.kill());

  const handshake = await readJsonLine(runtime.stdout);
  const models = await getJson(handshake.port, token, "/models");
  assert.equal(models.statusCode, 200);
  assert.deepEqual(models.body, {
    models: [{ id: "fake-interview-model", label: "Fake Interview Model" }],
  });

  const socket = new WebSocket(`ws://127.0.0.1:${handshake.port}/events`, {
    headers: { authorization: `Bearer ${token}` },
  });
  t.after(() => socket.close());
  await new Promise((resolve, reject) => {
    socket.once("open", resolve);
    socket.once("error", reject);
  });

  const conversationId = "conversation-test-1";
  const agentRunId = "agent-run-test-1";
  const completed = collectRunEvents(socket, agentRunId);
  socket.send(
    JSON.stringify({
      type: "agent_run.start",
      protocolVersion: 1,
      eventId: "client-event-1",
      conversationId,
      agentRunId,
      sequence: 0,
      model: "fake-interview-model",
      input: { role: "user", text: "Help me prepare." },
    }),
  );

  const events = await completed;
  assert.deepEqual(events.map((event) => event.type), [
    "agent_run.started",
    "agent_run.delta",
    "agent_run.delta",
    "agent_run.completed",
  ]);
  assert.deepEqual(events.map((event) => event.sequence), [1, 2, 3, 4]);
  for (const event of events) {
    assert.equal(event.protocolVersion, 1);
    assert.equal(event.conversationId, conversationId);
    assert.equal(event.agentRunId, agentRunId);
    assert.equal(typeof event.eventId, "string");
  }
  assert.equal(events[1].delta, "OfferAgent received: ");
  assert.equal(events[2].delta, "Help me prepare.");
  assert.equal(events[3].output.text, "OfferAgent received: Help me prepare.");

  const failedRun = new Promise((resolve) => {
    socket.on("message", function onMessage(data) {
      const event = JSON.parse(data.toString("utf8"));
      if (event.agentRunId === "agent-run-invalid-model" && event.type === "agent_run.failed") {
        socket.off("message", onMessage);
        resolve(event);
      }
    });
  });
  socket.send(
    JSON.stringify({
      type: "agent_run.start",
      protocolVersion: 1,
      eventId: "client-event-invalid-model",
      conversationId,
      agentRunId: "agent-run-invalid-model",
      sequence: 0,
      model: "missing-model",
      input: { role: "user", text: "Hello" },
    }),
  );
  assert.deepEqual((await failedRun).error, {
    code: "model_unavailable",
    message: "The selected model 'missing-model' is not available.",
  });
});

test("the Codex Provider uses the existing OAuth cache without exposing auth material", async (t) => {
  const temporaryDirectory = await mkdtemp(path.join(os.tmpdir(), "offeragent-codex-provider-"));
  const authPath = path.join(temporaryDirectory, "auth.json");
  const accessToken = "oauth-access-token-that-must-stay-private";
  await writeFile(
    authPath,
    JSON.stringify({ tokens: { access_token: accessToken, account_id: "account-test-1" } }),
    "utf8",
  );

  const upstreamRequests = [];
  let responseMode = "complete";
  const upstream = createServer((incoming, response) => {
    let body = "";
    incoming.setEncoding("utf8");
    incoming.on("data", (chunk) => (body += chunk));
    incoming.on("end", () => {
      upstreamRequests.push({ method: incoming.method, url: incoming.url, headers: incoming.headers, body });
      if (incoming.url?.startsWith("/models")) {
        response.writeHead(200, { "content-type": "application/json" });
        response.end(
          JSON.stringify({
            models: [
              { slug: "gpt-5.4", display_name: "GPT-5.4", visibility: "list" },
              { slug: "hidden", display_name: "Hidden", visibility: "hide" },
            ],
          }),
        );
        return;
      }
      if (incoming.url === "/responses") {
        response.writeHead(200, { "content-type": "text/event-stream" });
        response.write('data: {"type":"response.output_text.delta","delta":"Interview "}\r\n\r\n');
        if (responseMode === "truncated") {
          response.end();
          return;
        }
        response.write('data: {"type":"response.output_text.delta","delta":"answer"}\r\n\r\n');
        response.end('data: {"type":"response.completed","response":{"status":"completed"}}\r\n\r\ndata: [DONE]\r\n\r\n');
        return;
      }
      response.writeHead(404).end();
    });
  });
  await new Promise((resolve) => upstream.listen(0, "127.0.0.1", resolve));
  const upstreamPort = upstream.address().port;

  const token = "codex-provider-runtime-token";
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
      path.join(temporaryDirectory, "state.db"),
    ],
    {
      env: {
        ...process.env,
        OFFERAGENT_CODEX_AUTH_FILE: authPath,
        OFFERAGENT_CODEX_BASE_URL: `http://127.0.0.1:${upstreamPort}`,
      },
      stdio: ["ignore", "pipe", "pipe"],
      windowsHide: true,
    },
  );
  let runtimeStderr = "";
  runtime.stderr.on("data", (chunk) => (runtimeStderr += chunk.toString("utf8")));
  t.after(async () => {
    runtime.kill();
    await new Promise((resolve) => upstream.close(resolve));
    await rm(temporaryDirectory, { recursive: true, force: true });
  });

  const handshake = await readJsonLine(runtime.stdout);
  const models = await getJson(handshake.port, token, "/models");
  assert.deepEqual(models, {
    statusCode: 200,
    body: { models: [{ id: "gpt-5.4", label: "GPT-5.4" }] },
  });

  const socket = new WebSocket(`ws://127.0.0.1:${handshake.port}/events`, {
    headers: { authorization: `Bearer ${token}` },
  });
  await new Promise((resolve, reject) => {
    socket.once("open", resolve);
    socket.once("error", reject);
  });
  const completed = collectRunEvents(socket, "agent-run-codex-1");
  socket.send(
    JSON.stringify({
      type: "agent_run.start",
      protocolVersion: 1,
      eventId: "client-codex-event-1",
      conversationId: "conversation-codex-1",
      agentRunId: "agent-run-codex-1",
      sequence: 0,
      model: "gpt-5.4",
      input: { role: "user", text: "Prepare me." },
    }),
  );
  const events = await completed;

  assert.equal(events.at(-1).output.text, "Interview answer");
  assert.equal(JSON.stringify(events).includes(accessToken), false);
  assert.equal(runtimeStderr.includes(accessToken), false);
  assert.equal(upstreamRequests.length, 2);
  for (const upstreamRequest of upstreamRequests) {
    assert.equal(upstreamRequest.headers.authorization, `Bearer ${accessToken}`);
    assert.equal(upstreamRequest.headers["chatgpt-account-id"], "account-test-1");
    assert.equal(upstreamRequest.headers.originator, "codex_cli_rs");
  }
  assert.match(upstreamRequests[0].url, /^\/models\?client_version=/);
  const responseRequest = JSON.parse(upstreamRequests[1].body);
  assert.equal(responseRequest.model, "gpt-5.4");
  assert.equal(responseRequest.store, false);
  assert.equal(responseRequest.stream, true);
  assert.equal(responseRequest.input[0].content[0].text, "Prepare me.");

  responseMode = "truncated";
  const truncatedRun = new Promise((resolve) => {
    socket.on("message", function onMessage(data) {
      const event = JSON.parse(data.toString("utf8"));
      if (
        event.agentRunId === "agent-run-codex-truncated" &&
        (event.type === "agent_run.failed" || event.type === "agent_run.completed")
      ) {
        socket.off("message", onMessage);
        resolve(event);
      }
    });
  });
  socket.send(
    JSON.stringify({
      type: "agent_run.start",
      protocolVersion: 1,
      eventId: "client-codex-event-truncated",
      conversationId: "conversation-codex-1",
      agentRunId: "agent-run-codex-truncated",
      sequence: 0,
      model: "gpt-5.4",
      input: { role: "user", text: "This stream will truncate." },
    }),
  );
  const truncatedTerminal = await truncatedRun;
  assert.equal(truncatedTerminal.type, "agent_run.failed");
  assert.equal(truncatedTerminal.error.code, "transport_error");
  socket.close();
});

test("Codex auth, model, transport, and Provider failures remain typed", async (t) => {
  const temporaryDirectory = await mkdtemp(path.join(os.tmpdir(), "offeragent-provider-errors-"));
  const authPath = path.join(temporaryDirectory, "auth.json");
  const authPayload = JSON.stringify({ tokens: { access_token: "typed-error-test-token" } });
  await writeFile(authPath, authPayload, "utf8");
  let upstreamStatus = 400;
  const upstream = createServer((_incoming, response) => {
    response.writeHead(upstreamStatus, { "content-type": "application/json" });
    response.end(JSON.stringify({ detail: "deliberately hidden upstream detail" }));
  });
  await new Promise((resolve) => upstream.listen(0, "127.0.0.1", resolve));
  const upstreamPort = upstream.address().port;

  const token = "provider-errors-runtime-token";
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
      path.join(temporaryDirectory, "state.db"),
    ],
    {
      env: {
        ...process.env,
        OFFERAGENT_CODEX_AUTH_FILE: authPath,
        OFFERAGENT_CODEX_BASE_URL: `http://127.0.0.1:${upstreamPort}`,
      },
      stdio: ["ignore", "pipe", "pipe"],
      windowsHide: true,
    },
  );
  let upstreamOpen = true;
  t.after(async () => {
    runtime.kill();
    if (upstreamOpen) await new Promise((resolve) => upstream.close(resolve));
    await rm(temporaryDirectory, { recursive: true, force: true });
  });
  const ready = await readJsonLine(runtime.stdout);

  const unavailable = await getJson(ready.port, token, "/models");
  assert.deepEqual(unavailable, {
    statusCode: 502,
    body: {
      code: "provider_error",
      message: "Codex could not complete the request (HTTP 400).",
    },
  });

  upstreamStatus = 500;
  const providerFailure = await getJson(ready.port, token, "/models");
  assert.equal(providerFailure.statusCode, 502);
  assert.equal(providerFailure.body.code, "provider_error");

  await rm(authPath);
  const authFailure = await getJson(ready.port, token, "/models");
  assert.equal(authFailure.statusCode, 401);
  assert.equal(authFailure.body.code, "auth_required");

  await writeFile(authPath, authPayload, "utf8");
  await new Promise((resolve) => upstream.close(resolve));
  upstreamOpen = false;
  const transportFailure = await getJson(ready.port, token, "/models");
  assert.equal(transportFailure.statusCode, 502);
  assert.equal(transportFailure.body.code, "transport_error");
});

test("closing the Runtime WebSocket aborts an in-flight Codex request", async (t) => {
  const temporaryDirectory = await mkdtemp(path.join(os.tmpdir(), "offeragent-provider-abort-"));
  const authPath = path.join(temporaryDirectory, "auth.json");
  await writeFile(authPath, JSON.stringify({ tokens: { access_token: "abort-test-token" } }), "utf8");

  let markRequestStarted;
  const requestStarted = new Promise((resolve) => (markRequestStarted = resolve));
  let markUpstreamClosed;
  const upstreamClosed = new Promise((resolve) => (markUpstreamClosed = resolve));
  const upstream = createServer((incoming, response) => {
    if (incoming.url !== "/responses") return response.writeHead(404).end();
    response.on("close", markUpstreamClosed);
    response.writeHead(200, { "content-type": "text/event-stream" });
    response.write('data: {"type":"response.output_text.delta","delta":"still running"}\n\n');
    markRequestStarted();
  });
  await new Promise((resolve) => upstream.listen(0, "127.0.0.1", resolve));
  const upstreamPort = upstream.address().port;
  const runtimeToken = "abort-runtime-token";
  const runtime = spawn(
    process.execPath,
    [
      runtimeEntry,
      "--port",
      "0",
      "--token",
      runtimeToken,
      "--parent-pid",
      `${process.pid}`,
      "--provider",
      "codex",
      "--state-path",
      path.join(temporaryDirectory, "state.db"),
    ],
    {
      env: {
        ...process.env,
        OFFERAGENT_CODEX_AUTH_FILE: authPath,
        OFFERAGENT_CODEX_BASE_URL: `http://127.0.0.1:${upstreamPort}`,
      },
      stdio: ["ignore", "pipe", "pipe"],
      windowsHide: true,
    },
  );
  t.after(async () => {
    runtime.kill();
    await new Promise((resolve) => upstream.close(resolve));
    await rm(temporaryDirectory, { recursive: true, force: true });
  });
  const ready = await readJsonLine(runtime.stdout);
  const socket = new WebSocket(`ws://127.0.0.1:${ready.port}/events`, {
    headers: { authorization: `Bearer ${runtimeToken}` },
  });
  await new Promise((resolve, reject) => {
    socket.once("open", resolve);
    socket.once("error", reject);
  });
  socket.send(
    JSON.stringify({
      type: "agent_run.start",
      protocolVersion: 1,
      eventId: "abort-client-event",
      conversationId: "abort-conversation",
      agentRunId: "abort-agent-run",
      sequence: 0,
      model: "gpt-5.4",
      input: { role: "user", text: "Keep streaming." },
    }),
  );
  await requestStarted;
  socket.close();
  await Promise.race([
    upstreamClosed,
    new Promise((_, reject) => setTimeout(() => reject(new Error("Codex request was not aborted")), 2_000)),
  ]);
});
