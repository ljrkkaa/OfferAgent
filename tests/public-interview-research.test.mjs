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

function formatLocalDate(date) {
  const year = date.getFullYear();
  const month = String(date.getMonth() + 1).padStart(2, "0");
  const day = String(date.getDate()).padStart(2, "0");
  return `${year}-${month}-${day}`;
}

function subtractCalendarMonths(date, months) {
  const target = new Date(date.getFullYear(), date.getMonth() - months, 1);
  const lastDay = new Date(target.getFullYear(), target.getMonth() + 1, 0).getDate();
  target.setDate(Math.min(date.getDate(), lastDay));
  return target;
}

async function runtimeFor(t) {
  const temporaryDirectory = await mkdtemp(path.join(os.tmpdir(), "offeragent-public-research-"));
  const token = "public-research-token";
  const runtime = spawn(
    process.execPath,
    [
      runtimeEntry,
      "--port", "0",
      "--token", token,
      "--parent-pid", `${process.pid}`,
      "--provider", "fake",
      "--fake-scenario", "public-interview-research",
      "--fake-web-fixture", "interview-url-success",
      "--state-path", path.join(temporaryDirectory, "state.db"),
    ],
    { stdio: ["ignore", "pipe", "pipe"], windowsHide: true },
  );
  let stderr = "";
  runtime.stderr.on("data", (chunk) => {
    stderr += chunk.toString("utf8");
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
  return { socket, stderr: () => stderr };
}

function start(id, text) {
  return {
    type: "agent_run.start",
    protocolVersion: 1,
    eventId: `${id}-start`,
    conversationId: `${id}-conversation`,
    agentRunId: id,
    sequence: 0,
    model: "fake-interview-model",
    input: { role: "user", text },
  };
}

function catalog(experiences = [], questions = []) {
  return {
    ok: true,
    value: {
      type: "interview_catalog",
      experienceCandidates: experiences,
      questionCandidates: questions,
      indexes: [
        {
          kind: "experience",
          path: "experiences/index.md",
          exists: true,
          modifiedVersion: "mtime:80:size:30",
          contentHash: "sha256:experience-index-before",
        },
        {
          kind: "question",
          path: "interview/index.md",
          exists: true,
          modifiedVersion: "mtime:81:size:28",
          contentHash: "sha256:question-index-before",
        },
      ],
      truncated: false,
    },
  };
}

function read(pathname, content, modifiedVersion = "mtime:90:size:180", contentHash = "sha256:read") {
  return {
    ok: true,
    value: {
      type: "vault_read",
      path: pathname,
      lineStart: 1,
      lineEnd: content.split("\n").length,
      modifiedVersion,
      contentHash,
      content,
      truncated: false,
    },
  };
}

test("public-web Interview research stays scoped, ranked, deduplicated, and canonical", async (t) => {
  await t.test("default six-month scope ingests only the top new match", async (t) => {
    const { socket, stderr } = await runtimeFor(t);
    const today = new Date();
    const expectedEnd = formatLocalDate(today);
    const expectedStart = formatLocalDate(subtractCalendarMonths(today, 6));
    const existingPath = "experiences/example-backend-existing.md";
    const questionPath = "interview/distributed-cache-consistency.md";
    const questionBefore = "---\ntitle: Distributed cache consistency\nanswer-state: draft\nfrequency: 4\n---\n\n# Distributed cache consistency\n";
    const indexBefore = "# Interview Experiences\n\n- [[example-backend-existing]]\n";
    let researchCatalogSeen = false;
    let proposal;

    const events = await runWithToolPeer(
      socket,
      start(
        "public-research-default",
        "Research recent Example Corp Backend Engineer distributed-systems Interview Experiences and ingest the best new result.",
      ),
      (event) => {
        if (event.tool.name === "interview_catalog") {
          if (!event.tool.arguments.canonicalUrl) {
            researchCatalogSeen = true;
            assert.match(event.tool.arguments.query, /Example Corp/i);
            assert.match(event.tool.arguments.query, /Backend Engineer/i);
            assert.match(event.tool.arguments.query, /distributed systems/i);
            assert.match(event.tool.arguments.query, new RegExp(`${expectedStart}.*${expectedEnd}`));
            return catalog([{
              path: existingPath,
              title: "Existing Example backend interview",
              company: "Example Corp",
              position: "Backend Engineer",
              date: expectedEnd,
              matchKinds: ["repost-candidate"],
              modifiedVersion: "mtime:88:size:170",
              contentHash: "sha256:existing-experience",
            }]);
          }
          assert.equal(event.tool.arguments.canonicalUrl, "https://example.com/interviews/backend-42");
          assert.match(event.tool.arguments.sourceFingerprint, /^sha256:[a-f0-9]{64}$/);
          return catalog([], [{
            path: questionPath,
            title: "Distributed cache consistency",
            answerState: "draft",
            matchKinds: ["semantic-candidate"],
            modifiedVersion: "mtime:82:size:170",
            contentHash: "sha256:question-before",
          }]);
        }
        if (event.tool.name === "vault_read") {
          if (event.tool.arguments.path === existingPath) {
            return read(
              existingPath,
              "---\ntitle: Existing Example backend interview\ncompany: Example Corp\nposition: Backend Engineer\nsource-url: https://example.com/interviews/already-stored\n---",
              "mtime:88:size:170",
              "sha256:existing-experience",
            );
          }
          if (event.tool.arguments.path === questionPath) {
            return proposal
              ? read(questionPath, proposal.actions[1].replacement, "mtime:92:size:230", "sha256:question-after")
              : read(questionPath, questionBefore, "mtime:82:size:170", "sha256:question-before");
          }
          assert.equal(event.tool.arguments.path, "experiences/index.md");
          return proposal
            ? read("experiences/index.md", proposal.actions[2].replacement, "mtime:93:size:90", "sha256:index-after")
            : read("experiences/index.md", indexBefore, "mtime:80:size:30", "sha256:experience-index-before");
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
            targets: proposal.actions.map(({ path: targetPath }) => ({
              path: targetPath,
              beforeHash: targetPath === "experiences/url-interview.md" ? "missing" : "sha256:before",
              afterHash: "sha256:after",
            })),
          },
        };
      },
    );

    assert.equal(events.at(-1).type, "agent_run.completed", `${stderr()}\n${JSON.stringify(events.slice(-8))}`);
    assert.equal(researchCatalogSeen, true);
    const probe = events.find(
      (event) => event.type === "tool_call.requested" && event.tool.name === "hosted_web_search_probe",
    );
    assert.ok(probe);
    assert.match(probe.tool.arguments.query, new RegExp(`${expectedStart}.*${expectedEnd}`));
    const search = events.find((event) => event.type === "hosted_web_search.completed");
    assert.deepEqual(search.sources.map(({ url }) => url), [
      "https://example.com/shared/backend-42",
      "https://example.com/interviews/backend-platform-older",
    ]);
    assert.equal(search.sources.some(({ url }) => url.includes("already-stored")), false);
    assert.equal(search.sources.length <= 5, true);
    assert.ok(proposal);
    assert.deepEqual(
      proposal.actions.map(({ operation, path: targetPath }) => ({ operation, path: targetPath })),
      [
        { operation: "create", path: "experiences/url-interview.md" },
        { operation: "exact_replace", path: questionPath },
        { operation: "exact_replace", path: "experiences/index.md" },
      ],
    );
    assert.match(events.at(-1).output.text, /top new match/i);
    assert.match(events.at(-1).output.text, /excluded 1 existing/i);
    assert.doesNotMatch(events.at(-1).output.text, /(?:high|medium|low)[ -]reliability|reliability grade|score:\s*\d/i);
  });

  await t.test("an explicit range is respected without ingestion", async (t) => {
    const { socket, stderr } = await runtimeFor(t);
    const events = await runWithToolPeer(
      socket,
      start(
        "public-research-explicit",
        "Research Example Corp Backend Engineer distributed-systems interviews from 2025-01-01 through 2025-03-31. Return matches but do not ingest them.",
      ),
      (event) => {
        assert.equal(event.tool.name, "interview_catalog");
        assert.match(event.tool.arguments.query, /2025-01-01.*2025-03-31/);
        return catalog();
      },
    );
    assert.equal(events.at(-1).type, "agent_run.completed", stderr());
    const probe = events.find(
      (event) => event.type === "tool_call.requested" && event.tool.name === "hosted_web_search_probe",
    );
    assert.match(probe.tool.arguments.query, /2025-01-01.*2025-03-31/);
    assert.equal(
      events.some((event) => event.type === "tool_call.requested" && event.tool.name === "web_read"),
      false,
    );
    assert.equal(
      events.some((event) => event.type === "tool_call.requested" && event.tool.name === "vault_propose_changes"),
      false,
    );
    assert.match(events.at(-1).output.text, /2025-01-01.*2025-03-31/);
  });

  await t.test("insufficient results are explicit and never widen the requested scope", async (t) => {
    const { socket, stderr } = await runtimeFor(t);
    const events = await runWithToolPeer(
      socket,
      start(
        "public-research-insufficient",
        "Research only Example Corp iOS Engineer SwiftUI interviews from 2026-06-01 through 2026-06-30. Do not broaden any part of the scope.",
      ),
      (event) => {
        assert.equal(event.tool.name, "interview_catalog");
        assert.match(event.tool.arguments.query, /Example Corp.*iOS Engineer.*SwiftUI.*2026-06-01.*2026-06-30/i);
        assert.doesNotMatch(event.tool.arguments.query, /Android|Backend|Frontend/i);
        return catalog();
      },
    );
    assert.equal(events.at(-1).type, "agent_run.completed", stderr());
    const probe = events.find(
      (event) => event.type === "tool_call.requested" && event.tool.name === "hosted_web_search_probe",
    );
    assert.match(probe.tool.arguments.query, /Example Corp.*iOS Engineer.*SwiftUI.*2026-06-01.*2026-06-30/i);
    assert.doesNotMatch(probe.tool.arguments.query, /Android|Backend|Frontend/i);
    const search = events.find((event) => event.type === "hosted_web_search.completed");
    assert.deepEqual(search.sources, []);
    assert.match(events.at(-1).output.text, /insufficient matching public results/i);
    assert.match(events.at(-1).output.text, /scope was not widened/i);
    assert.equal(
      events.some((event) => event.type === "tool_call.requested" && event.tool.name === "vault_propose_changes"),
      false,
    );
  });
});
