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

async function runtimeFor(t, fixture) {
  const temporaryDirectory = await mkdtemp(path.join(os.tmpdir(), `offeragent-url-${fixture}-`));
  const statePath = path.join(temporaryDirectory, "state.db");
  const token = `url-${fixture}-token`;
  const runtime = spawn(
    process.execPath,
    [
      runtimeEntry,
      "--port", "0",
      "--token", token,
      "--parent-pid", `${process.pid}`,
      "--provider", "fake",
      "--fake-scenario", "url-interview-ingestion",
      "--fake-web-fixture", fixture,
      "--state-path", statePath,
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

function catalog(experiences, questions) {
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
          contentHash: "sha256:question-index",
        },
      ],
      truncated: false,
    },
  };
}

function read(pathname, content, modifiedVersion, contentHash) {
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

test("a supplied URL reuses canonical deduplication and atomic Interview ingestion", async (t) => {
  await t.test("new URL with an overlapping Question", async (t) => {
    const { socket, stderr } = await runtimeFor(t, "interview-url-success");
    const questionPath = "interview/distributed-cache-consistency.md";
    const indexPath = "experiences/index.md";
    const questionBefore = "---\ntitle: Distributed cache consistency\nanswer-state: draft\nfrequency: 4\n---\n\n# Distributed cache consistency\n\n## Occurrences\n\n- [[experiences/older-backend]]\n";
    const indexBefore = "# Interview Experiences\n\n- [[older-backend]]\n";
    let proposal;
    let catalogFingerprint;
    const events = await runWithToolPeer(
      socket,
      start("url-new-run", "Ingest https://example.com/shared/backend-42 as an Interview Experience."),
      (event) => {
        if (event.tool.name === "interview_catalog") {
          assert.equal(event.tool.arguments.canonicalUrl, "https://example.com/interviews/backend-42");
          assert.match(event.tool.arguments.sourceFingerprint, /^sha256:[a-f0-9]{64}$/);
          catalogFingerprint = event.tool.arguments.sourceFingerprint;
          return catalog(
            [],
            [{
              path: questionPath,
              title: "Distributed cache consistency",
              answerState: "draft",
              matchKinds: ["semantic-candidate"],
              modifiedVersion: "mtime:82:size:170",
              contentHash: "sha256:question-before",
            }],
          );
        }
        if (event.tool.name === "vault_read") {
          if (event.tool.arguments.path === questionPath) {
            return proposal
              ? read(questionPath, proposal.actions[1].replacement, "mtime:84:size:230", "sha256:question-after")
              : read(questionPath, questionBefore, "mtime:82:size:170", "sha256:question-before");
          }
          assert.equal(event.tool.arguments.path, indexPath);
          return proposal
            ? read(indexPath, proposal.actions[2].replacement, "mtime:85:size:75", "sha256:index-after")
            : read(indexPath, indexBefore, "mtime:80:size:30", "sha256:experience-index-before");
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
              beforeHash: targetPath.endsWith("url-interview.md") ? "missing" : "sha256:before",
              afterHash: "sha256:after",
            })),
          },
        };
      },
    );
    assert.equal(events.at(-1).type, "agent_run.completed", stderr());
    assert.deepEqual(
      proposal.actions.map(({ operation, path: targetPath }) => ({ operation, path: targetPath })),
      [
        { operation: "create", path: "experiences/url-interview.md" },
        { operation: "exact_replace", path: questionPath },
        { operation: "exact_replace", path: indexPath },
      ],
    );
    assert.match(proposal.actions[0].content, /source-url: https:\/\/example\.com\/interviews\/backend-42/);
    assert.match(proposal.actions[0].content, new RegExp(`source-fingerprint: ${catalogFingerprint}`));
    assert.match(proposal.actions[0].content, /source-title: Example Backend Interview/);
    assert.doesNotMatch(JSON.stringify(proposal), /FULL-PAGE-MARKER-URL-42/);
    assert.match(proposal.actions[1].replacement, /frequency: 5/);
    assert.match(proposal.actions[1].replacement, /url-interview/);
  });

  await t.test("canonical duplicate", async (t) => {
    const { socket, stderr } = await runtimeFor(t, "interview-url-success");
    const duplicatePath = "experiences/existing-url-interview.md";
    const questionPath = "interview/distributed-cache-consistency.md";
    const events = await runWithToolPeer(
      socket,
      start("url-duplicate-run", "Ingest this shared URL without duplicating existing knowledge."),
      (event) => {
        if (event.tool.name === "interview_catalog") {
          return catalog(
            [{
              path: duplicatePath,
              title: "Existing URL interview",
              company: "Example Corp",
              position: "Backend Engineer",
              round: "final",
              date: "2026-07-01",
              matchKinds: ["canonical-url"],
              modifiedVersion: "mtime:86:size:210",
              contentHash: "sha256:existing-url",
            }],
            [{
              path: questionPath,
              title: "Distributed cache consistency",
              answerState: "draft",
              matchKinds: ["semantic-candidate"],
              modifiedVersion: "mtime:82:size:170",
              contentHash: "sha256:question-before",
            }],
          );
        }
        assert.equal(event.tool.name, "vault_read");
        if (event.tool.arguments.path === duplicatePath) {
          return read(
            duplicatePath,
            "---\ntitle: Existing URL interview\ncandidate: candidate-b\nround: final\ndate: 2026-07-01\nsource-url: https://example.com/interviews/backend-42\n---",
            "mtime:86:size:210",
            "sha256:existing-url",
          );
        }
        return read(
          questionPath,
          "---\ntitle: Distributed cache consistency\nfrequency: 4\n---",
          "mtime:82:size:170",
          "sha256:question-before",
        );
      },
    );
    assert.equal(events.at(-1).type, "agent_run.completed", stderr());
    assert.match(events.at(-1).output.text, /Canonical duplicate Interview Experience suppressed/);
    assert.equal(
      events.some(
        (event) => event.type === "tool_call.requested" && event.tool.name === "vault_propose_changes",
      ),
      false,
    );
  });

  for (const [label, matchKinds] of [
    ["sparse exact-source candidate", ["canonical-url"]],
    ["sparse repost candidate", ["repost-candidate"]],
  ]) {
    await t.test(label, async (t) => {
      const { socket, stderr } = await runtimeFor(t, "interview-url-success");
      const candidatePath = `experiences/${label.replaceAll(" ", "-")}.md`;
      const events = await runWithToolPeer(
        socket,
        start(`${label.replaceAll(" ", "-")}-run`, "Do not guess an incomplete Experience identity."),
        (event) => {
          if (event.tool.name === "interview_catalog") {
            return catalog([{
              path: candidatePath,
              title: "Sparse candidate",
              matchKinds,
              modifiedVersion: "mtime:92:size:90",
              contentHash: "sha256:sparse-candidate",
            }], []);
          }
          assert.equal(event.tool.name, "vault_read");
          assert.equal(event.tool.arguments.path, candidatePath);
          return read(
            candidatePath,
            "---\ntitle: Sparse candidate\nsource-url: https://example.com/interviews/backend-42\n---",
            "mtime:92:size:90",
            "sha256:sparse-candidate",
          );
        },
      );
      assert.equal(events.at(-1).type, "agent_run.completed", stderr());
      assert.match(events.at(-1).output.text, /Ambiguous identity; no merge or Vault changes proposed/);
      assert.equal(
        events.some(
          (event) => event.type === "tool_call.requested" && event.tool.name === "vault_propose_changes",
        ),
        false,
      );
    });
  }

  await t.test("conflicting exact-source identity remains distinct", async (t) => {
    const { socket, stderr } = await runtimeFor(t, "interview-url-success");
    const candidatePath = "experiences/conflicting-canonical.md";
    const questionPath = "interview/distributed-cache-consistency.md";
    let proposal;
    const events = await runWithToolPeer(
      socket,
      start("conflicting-canonical-run", "Keep a distinct candidate identity distinct."),
      (event) => {
        if (event.tool.name === "interview_catalog") {
          return catalog([{
            path: candidatePath,
            title: "Conflicting canonical candidate",
            matchKinds: ["canonical-url"],
            modifiedVersion: "mtime:93:size:170",
            contentHash: "sha256:conflicting-canonical",
          }], [{
            path: questionPath,
            title: "Distributed cache consistency",
            answerState: "draft",
            matchKinds: ["semantic-candidate"],
            modifiedVersion: "mtime:82:size:170",
            contentHash: "sha256:question-before",
          }]);
        }
        if (event.tool.name === "vault_read") {
          if (event.tool.arguments.path === candidatePath) {
            return read(
              candidatePath,
              "---\ntitle: Conflicting canonical candidate\ncandidate: candidate-c\nround: phone\ndate: 2026-06-15\n---",
              "mtime:93:size:170",
              "sha256:conflicting-canonical",
            );
          }
          if (event.tool.arguments.path === questionPath) {
            return proposal
              ? read(questionPath, proposal.actions[1].replacement, "mtime:94:size:220", "sha256:question-after")
              : read(questionPath, "---\ntitle: Distributed cache consistency\nfrequency: 4\n---", "mtime:82:size:170", "sha256:question-before");
          }
          return proposal
            ? read("experiences/index.md", proposal.actions[2].replacement, "mtime:95:size:65", "sha256:index-after")
            : read("experiences/index.md", "# Interview Experiences\n", "mtime:80:size:30", "sha256:experience-index-before");
        }
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
              beforeHash: "sha256:before",
              afterHash: "sha256:after",
            })),
          },
        };
      },
    );
    assert.equal(events.at(-1).type, "agent_run.completed", stderr());
    assert.ok(proposal);
    assert.equal(proposal.actions[0].path, "experiences/url-interview.md");
  });

  await t.test("semantic repost duplicate", async (t) => {
    const { socket, stderr } = await runtimeFor(t, "interview-url-success");
    const duplicatePath = "experiences/existing-repost-interview.md";
    const events = await runWithToolPeer(
      socket,
      start("url-repost-run", "Ingest the URL while reusing semantic repost evidence."),
      (event) => {
        if (event.tool.name === "interview_catalog") {
          return catalog([{
            path: duplicatePath,
            title: "Existing repost interview",
            company: "Example Corp",
            position: "Backend Engineer",
            round: "final",
            date: "2026-07-01",
            matchKinds: ["repost-candidate"],
            modifiedVersion: "mtime:87:size:210",
            contentHash: "sha256:existing-repost",
          }], []);
        }
        assert.equal(event.tool.name, "vault_read");
        assert.equal(event.tool.arguments.path, duplicatePath);
        return read(
          duplicatePath,
          "---\ntitle: Existing repost interview\ncandidate: candidate-b\nround: final\ndate: 2026-07-01\n---",
          "mtime:87:size:210",
          "sha256:existing-repost",
        );
      },
    );
    assert.equal(events.at(-1).type, "agent_run.completed", stderr());
    assert.match(events.at(-1).output.text, /duplicate Interview Experience suppressed/i);
    assert.equal(
      events.some(
        (event) => event.type === "tool_call.requested" && event.tool.name === "vault_propose_changes",
      ),
      false,
    );
  });

  await t.test("new URL with a new Question", async (t) => {
    const { socket, stderr } = await runtimeFor(t, "interview-url-success");
    const experienceIndexPath = "experiences/index.md";
    const questionIndexPath = "interview/index.md";
    const experienceIndexBefore = "# Interview Experiences\n";
    const questionIndexBefore = "# Interview Questions\n";
    let proposal;
    const events = await runWithToolPeer(
      socket,
      start("url-new-question-run", "Ingest this URL and preserve its new Question."),
      (event) => {
        if (event.tool.name === "interview_catalog") return catalog([], []);
        if (event.tool.name === "vault_read") {
          if (event.tool.arguments.path === experienceIndexPath) {
            return proposal
              ? read(experienceIndexPath, proposal.actions[2].replacement, "mtime:90:size:50", "sha256:experience-index-after")
              : read(experienceIndexPath, experienceIndexBefore, "mtime:80:size:30", "sha256:experience-index-before");
          }
          assert.equal(event.tool.arguments.path, questionIndexPath);
          return proposal
            ? read(questionIndexPath, proposal.actions[3].replacement, "mtime:91:size:70", "sha256:question-index-after")
            : read(questionIndexPath, questionIndexBefore, "mtime:81:size:28", "sha256:question-index");
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
              beforeHash: targetPath.includes("url-interview") || targetPath.includes("distributed-cache")
                ? "missing"
                : "sha256:before",
              afterHash: "sha256:after",
            })),
          },
        };
      },
    );
    assert.equal(events.at(-1).type, "agent_run.completed", stderr());
    assert.deepEqual(
      proposal.actions.map(({ operation, path: targetPath }) => ({ operation, path: targetPath })),
      [
        { operation: "create", path: "experiences/url-interview.md" },
        { operation: "create", path: "interview/distributed-cache-consistency.md" },
        { operation: "exact_replace", path: experienceIndexPath },
        { operation: "exact_replace", path: questionIndexPath },
      ],
    );
    assert.match(proposal.actions[1].content, /answer-state: needs-research/);
    assert.match(proposal.actions[1].content, /frequency: 1/);
  });

  await t.test("atomic validation failure", async (t) => {
    const { socket } = await runtimeFor(t, "interview-url-success");
    const questionPath = "interview/distributed-cache-consistency.md";
    let proposals = 0;
    const events = await runWithToolPeer(
      socket,
      start("url-stale-run", "Ingest only if the whole URL batch remains current."),
      (event) => {
        if (event.tool.name === "interview_catalog") {
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
          if (event.tool.arguments.path === questionPath) {
            return read(
              questionPath,
              "---\ntitle: Distributed cache consistency\nfrequency: 4\n---",
              "mtime:82:size:170",
              "sha256:question-before",
            );
          }
          return read(
            "experiences/index.md",
            "# Interview Experiences",
            "mtime:80:size:30",
            "sha256:experience-index-before",
          );
        }
        proposals += 1;
        return {
          ok: false,
          error: { code: "stale_evidence", message: "Experience index changed before apply" },
        };
      },
    );
    assert.equal(proposals, 1);
    assert.match(events.at(-1).output.text, /URL ingestion batch failed: Experience index changed before apply/);
  });

  await t.test("inaccessible page", async (t) => {
    const { socket } = await runtimeFor(t, "interview-url-failure");
    const events = await runWithToolPeer(
      socket,
      start("url-failed-read-run", "Ingest the supplied inaccessible URL."),
      (event) => {
        throw new Error(`Unexpected plugin tool ${event.tool.name}`);
      },
    );
    assert.match(events.at(-1).output.text, /Could not read the supplied interview page: HTTP 503/);
    assert.equal(
      events.some(
        (event) => event.type === "tool_call.requested" && event.tool.name === "interview_catalog",
      ),
      false,
    );
  });

  await t.test("insufficient page", async (t) => {
    const { socket } = await runtimeFor(t, "interview-url-insufficient");
    const events = await runWithToolPeer(
      socket,
      start("url-insufficient-run", "Ingest the supplied page only if it contains interview evidence."),
      (event) => {
        throw new Error(`Unexpected plugin tool ${event.tool.name}`);
      },
    );
    assert.match(events.at(-1).output.text, /The supplied page did not contain enough interview evidence/);
    assert.doesNotMatch(events.at(-1).output.text, /Example Corp|Backend Engineer|final round/);
  });
});
