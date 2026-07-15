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
const fingerprint = `sha256:${"b".repeat(64)}`;

function indexes() {
  return [
    {
      kind: "experience",
      path: "experiences/index.md",
      exists: true,
      modifiedVersion: "mtime:50:size:30",
      contentHash: "sha256:experience-index",
    },
    {
      kind: "question",
      path: "interview/index.md",
      exists: true,
      modifiedVersion: "mtime:51:size:28",
      contentHash: "sha256:question-index",
    },
  ];
}

function catalog(experienceCandidates, questionCandidates = []) {
  return {
    ok: true,
    value: {
      type: "interview_catalog",
      experienceCandidates,
      questionCandidates,
      indexes: indexes(),
      truncated: false,
    },
  };
}

function read(pathname, content, version, hash) {
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

function start(agentRunId, text) {
  return {
    type: "agent_run.start",
    protocolVersion: 1,
    eventId: `${agentRunId}-start`,
    conversationId: `${agentRunId}-conversation`,
    agentRunId,
    sequence: 0,
    model: "fake-interview-model",
    input: { role: "user", text },
  };
}

test("deduplication suppresses duplicate Experiences and atomically merges recurring Questions", async (t) => {
  const temporaryDirectory = await mkdtemp(path.join(os.tmpdir(), "offeragent-interview-dedup-"));
  const statePath = path.join(temporaryDirectory, "state.db");
  const token = "interview-dedup-token";
  const runtime = spawn(
    process.execPath,
    [
      runtimeEntry,
      "--port", "0",
      "--token", token,
      "--parent-pid", `${process.pid}`,
      "--provider", "fake",
      "--fake-scenario", "interview-deduplication",
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

  const duplicatePath = "experiences/backend-second-round.md";
  const duplicateBefore = "---\ntitle: Backend final round\ncandidate: candidate-b\nround: final\ndate: 2026-07-01\nsource-fingerprint: " + fingerprint + "\n---\n\n# Backend final round";
  let metadataProposal;
  const duplicateEvents = await runWithToolPeer(
    socket,
    start("duplicate-metadata-run", "This is the same interview source; add its canonical URL if missing."),
    (event) => {
      if (event.tool.name === "interview_catalog") {
        assert.deepEqual(event.tool.arguments, {
          query: "Example backend distributed cache consistency",
          canonicalUrl: "https://example.com/interviews/backend-42",
          sourceFingerprint: fingerprint,
          limit: 10,
        });
        return catalog([
          {
            path: duplicatePath,
            title: "Backend final round",
            company: "Example Corp",
            position: "Backend Engineer",
            round: "final",
            date: "2026-07-01",
            matchKinds: ["source-fingerprint"],
            modifiedVersion: "mtime:52:size:120",
            contentHash: "sha256:duplicate-before",
          },
        ]);
      }
      if (event.tool.name === "vault_read") {
        assert.equal(event.tool.arguments.path, duplicatePath);
        return metadataProposal
          ? read(
              duplicatePath,
              metadataProposal.actions[0].replacement,
              "mtime:53:size:180",
              "sha256:duplicate-after",
            )
          : read(duplicatePath, duplicateBefore, "mtime:52:size:120", "sha256:duplicate-before");
      }
      assert.equal(event.tool.name, "vault_propose_changes");
      metadataProposal = event.tool.arguments;
      return {
        ok: true,
        value: {
          type: "vault_propose_changes",
          batchId: metadataProposal.batchId,
          decision: "applied",
          checkpointRef: `refs/offeragent/checkpoints/${metadataProposal.batchId}`,
          targets: [{
            path: duplicatePath,
            beforeHash: "sha256:duplicate-before",
            afterHash: "sha256:duplicate-after",
          }],
        },
      };
    },
  );
  assert.equal(duplicateEvents.at(-1).type, "agent_run.completed", runtimeStderr);
  assert.deepEqual(
    metadataProposal.actions.map(({ operation, path: targetPath }) => ({ operation, path: targetPath })),
    [{ operation: "exact_replace", path: duplicatePath }],
  );
  assert.match(metadataProposal.actions[0].replacement, /source-url: https:\/\/example\.com\/interviews\/backend-42/);
  assert.doesNotMatch(JSON.stringify(metadataProposal), /frequency:/);

  const camelCaseSourceEvents = await runWithToolPeer(
    socket,
    start("camel-source-run", "This exact duplicate already has canonical source metadata."),
    (event) => {
      if (event.tool.name === "interview_catalog") {
        return catalog([{
          path: duplicatePath,
          title: "Backend final round",
          company: "Example Corp",
          position: "Backend Engineer",
          round: "final",
          date: "2026-07-01",
          matchKinds: ["canonical-url"],
          modifiedVersion: "mtime:55:size:190",
          contentHash: "sha256:camel-source",
        }]);
      }
      assert.equal(event.tool.name, "vault_read");
      return read(
        duplicatePath,
        duplicateBefore.replace(
          "source-fingerprint:",
          "sourceUrl: https://example.com/interviews/backend-42\nsource-fingerprint:",
        ),
        "mtime:55:size:190",
        "sha256:camel-source",
      );
    },
  );
  assert.equal(camelCaseSourceEvents.at(-1).type, "agent_run.completed");
  assert.equal(
    camelCaseSourceEvents.some(
      (event) => event.type === "tool_call.requested" && event.tool.name === "vault_propose_changes",
    ),
    false,
  );

  const semanticDuplicatePath = "experiences/reposted-final-round.md";
  const semanticDuplicateContent = "---\ntitle: Reposted final round\ncompany: Example Corp\nposition: Backend Engineer\ncandidate: candidate-b\nround: final\ndate: 2026-07-01\n---\n\n# Reposted final round";
  const semanticDuplicateEvents = await runWithToolPeer(
    socket,
    start("semantic-duplicate-run", "The URL changed, but this may be the same final-round Experience."),
    (event) => {
      if (event.tool.name === "interview_catalog") {
        return catalog([{
          path: semanticDuplicatePath,
          title: "Reposted final round",
          company: "Example Corp",
          position: "Backend Engineer",
          round: "final",
          date: "2026-07-01",
          matchKinds: ["repost-candidate"],
          modifiedVersion: "mtime:54:size:140",
          contentHash: "sha256:semantic-duplicate",
        }]);
      }
      assert.equal(event.tool.name, "vault_read");
      assert.equal(event.tool.arguments.path, semanticDuplicatePath);
      return read(
        semanticDuplicatePath,
        semanticDuplicateContent,
        "mtime:54:size:140",
        "sha256:semantic-duplicate",
      );
    },
  );
  assert.equal(semanticDuplicateEvents.at(-1).type, "agent_run.completed");
  assert.match(
    semanticDuplicateEvents.at(-1).output.text,
    /Semantic duplicate Interview Experience suppressed after exact evidence review/,
  );
  assert.equal(
    semanticDuplicateEvents.some(
      (event) => event.type === "tool_call.requested" && event.tool.name === "vault_propose_changes",
    ),
    false,
  );

  const priorExperiencePath = "experiences/backend-candidate-a-final.md";
  const recurringQuestionPath = "interview/distributed-cache-consistency.md";
  const tangentialQuestionPath = "interview/cache-eviction-strategy.md";
  const experienceIndexPath = "experiences/index.md";
  const priorExperience = "---\ncompany: Example Corp\nposition: Backend Engineer\ncandidate: candidate-a\nround: final\ndate: 2026-07-01\n---\n\n# Candidate A final interview";
  const recurringBefore = "---\ntitle: Distributed cache consistency\nanswer-state: draft\nfrequency: 10\n---\n\n# Distributed cache consistency\n\n## Occurrences\n\n- [[experiences/backend-candidate-a-final]] — candidate-a, 2026-07-01 final round\n";
  const tangentialQuestion = "---\ntitle: Cache eviction strategy\nanswer-state: draft\nfrequency: 3\n---\n\n# Cache eviction strategy\n";
  const experienceIndexBefore = "# Interview Experiences\n\n- [[backend-candidate-a-final]]\n";
  let mergeProposal;
  const distinctEvents = await runWithToolPeer(
    socket,
    start("distinct-recurring-run", "This is a different candidate and final round with one recurring question."),
    (event) => {
      if (event.tool.name === "interview_catalog") {
        return catalog(
          [{
            path: priorExperiencePath,
            title: "Candidate A final interview",
            company: "Example Corp",
            position: "Backend Engineer",
            round: "final",
            date: "2026-07-01",
            matchKinds: ["repost-candidate"],
            modifiedVersion: "mtime:60:size:115",
            contentHash: "sha256:prior-experience",
          }],
          [
            {
              path: tangentialQuestionPath,
              title: "Cache eviction strategy",
              answerState: "draft",
              matchKinds: ["semantic-candidate"],
              modifiedVersion: "mtime:62:size:110",
              contentHash: "sha256:tangential-question",
            },
            {
              path: recurringQuestionPath,
              title: "Distributed cache consistency",
              answerState: "draft",
              matchKinds: ["semantic-candidate"],
              modifiedVersion: "mtime:61:size:211",
              contentHash: "sha256:recurring-before",
            },
          ],
        );
      }
      if (event.tool.name === "vault_read") {
        const targetPath = event.tool.arguments.path;
        if (targetPath === priorExperiencePath) {
          return read(targetPath, priorExperience, "mtime:60:size:115", "sha256:prior-experience");
        }
        if (targetPath === recurringQuestionPath) {
          return mergeProposal
            ? read(targetPath, mergeProposal.actions[1].replacement, "mtime:64:size:280", "sha256:recurring-after")
            : read(targetPath, recurringBefore, "mtime:61:size:210", "sha256:recurring-before");
        }
        if (targetPath === tangentialQuestionPath) {
          return read(targetPath, tangentialQuestion, "mtime:62:size:110", "sha256:tangential-question");
        }
        assert.equal(targetPath, experienceIndexPath);
        return mergeProposal
          ? read(targetPath, mergeProposal.actions[2].replacement, "mtime:65:size:100", "sha256:experience-index-after")
          : read(targetPath, experienceIndexBefore, "mtime:50:size:30", "sha256:experience-index");
      }
      assert.equal(event.tool.name, "vault_propose_changes");
      mergeProposal = event.tool.arguments;
      return {
        ok: true,
        value: {
          type: "vault_propose_changes",
          batchId: mergeProposal.batchId,
          decision: "applied",
          checkpointRef: `refs/offeragent/checkpoints/${mergeProposal.batchId}`,
          targets: mergeProposal.actions.map(({ path: targetPath }) => ({
            path: targetPath,
            beforeHash: targetPath.endsWith("final-round.md") ? "missing" : "sha256:before",
            afterHash: "sha256:after",
          })),
        },
      };
    },
  );
  assert.equal(distinctEvents.at(-1).type, "agent_run.completed", runtimeStderr);
  assert.deepEqual(
    mergeProposal.actions.map(({ operation, path: targetPath }) => ({ operation, path: targetPath })),
    [
      { operation: "create", path: "experiences/backend-final-round.md" },
      { operation: "exact_replace", path: recurringQuestionPath },
      { operation: "exact_replace", path: experienceIndexPath },
    ],
  );
  assert.match(mergeProposal.actions[0].content, /round: final/);
  assert.match(mergeProposal.actions[0].content, /date: 2026-07-01/);
  assert.match(mergeProposal.actions[0].content, /candidate: candidate-b/);
  assert.match(mergeProposal.actions[1].replacement, /frequency: 11/);
  assert.match(mergeProposal.actions[1].replacement, /backend-final-round.*2026-07-01 final round/);

  const ambiguousCandidates = ["ambiguous-a", "ambiguous-b"].map((name, index) => ({
    path: `experiences/${name}.md`,
    title: `Ambiguous ${index + 1}`,
    company: "Example Corp",
    position: "Backend Engineer",
    matchKinds: ["repost-candidate"],
    modifiedVersion: `mtime:${70 + index}:size:80`,
    contentHash: `sha256:ambiguous-${index + 1}`,
  }));
  const ambiguousEvents = await runWithToolPeer(
    socket,
    start("ambiguous-run", "These candidates lack enough identity evidence; do not merge."),
    (event) => {
      if (event.tool.name === "interview_catalog") return catalog(ambiguousCandidates);
      assert.equal(event.tool.name, "vault_read");
      const candidate = ambiguousCandidates.find(({ path: candidatePath }) => candidatePath === event.tool.arguments.path);
      assert.ok(candidate);
      return read(candidate.path, `---\ntitle: ${candidate.title}\n---`, candidate.modifiedVersion, candidate.contentHash);
    },
  );
  assert.equal(ambiguousEvents.at(-1).type, "agent_run.completed");
  assert.match(ambiguousEvents.at(-1).output.text, /Ambiguous identity; no merge or Vault changes proposed/);
  assert.equal(
    ambiguousEvents.some(
      (event) => event.type === "tool_call.requested" && event.tool.name === "vault_propose_changes",
    ),
    false,
  );

  let failedProposalCount = 0;
  const failedEvents = await runWithToolPeer(
    socket,
    start("failed-merge-run", "Merge the recurring question only if the atomic batch remains current."),
    (event) => {
      if (event.tool.name === "interview_catalog") {
        return catalog(
          [{
            path: priorExperiencePath,
            title: "Candidate A final interview",
            company: "Example Corp",
            position: "Backend Engineer",
            round: "final",
            date: "2026-07-01",
            matchKinds: ["repost-candidate"],
            modifiedVersion: "mtime:60:size:115",
            contentHash: "sha256:prior-experience",
          }],
          [{
            path: recurringQuestionPath,
            title: "Distributed cache consistency",
            answerState: "draft",
            matchKinds: ["semantic-candidate"],
            modifiedVersion: "mtime:61:size:210",
            contentHash: "sha256:recurring-before",
          }],
        );
      }
      if (event.tool.name === "vault_read") {
        if (event.tool.arguments.path === priorExperiencePath) {
          return read(priorExperiencePath, priorExperience, "mtime:60:size:115", "sha256:prior-experience");
        }
        if (event.tool.arguments.path === recurringQuestionPath) {
          return read(recurringQuestionPath, recurringBefore, "mtime:61:size:210", "sha256:recurring-before");
        }
        return read(experienceIndexPath, experienceIndexBefore, "mtime:50:size:30", "sha256:experience-index");
      }
      failedProposalCount += 1;
      return {
        ok: false,
        error: { code: "stale_evidence", message: "question changed before apply" },
      };
    },
  );
  assert.equal(
    failedProposalCount,
    1,
    `${JSON.stringify(failedEvents.slice(-12))}\n${runtimeStderr}`,
  );
  assert.match(failedEvents.at(-1).output.text, /Atomic deduplication batch failed: question changed before apply/);
});
