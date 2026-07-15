import assert from "node:assert/strict";
import { createHash } from "node:crypto";
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
const sourcePath = "src/cache.ts";
const evidencePath = `project/offeragent/${sourcePath}`;
const experiencePath = "experiences/2026-07-offeragent-cache.md";
const profilePath = "projects/offeragent/profile.md";
const indexPath = "projects/offeragent/index.md";
const answerPath = "projects/offeragent/answers/cache-invalidation.md";
const sourceContent = [
  "export function invalidate(key, expectedVersion) {",
  "  const current = cache.get(key);",
  "  if (current.version === expectedVersion) cache.delete(key);",
  "}",
].join("\n");

function hash(content) {
  return `sha256:${createHash("sha256").update(content, "utf8").digest("hex")}`;
}

function readResult(pathname, content) {
  return {
    ok: true,
    value: {
      type: "vault_read",
      path: pathname,
      lineStart: 1,
      lineEnd: content.split("\n").length,
      modifiedVersion: `mtime:1:size:${Buffer.byteLength(content)}`,
      contentHash: hash(content),
      content,
      truncated: false,
    },
  };
}

function projectResult(name) {
  if (name === "project_search") {
    return { ok: true, value: {
      type: "project_search", projectId: "offeragent", truncated: false,
      entries: [{
        path: sourcePath,
        modifiedVersion: "mtime:1:size:150",
        contentHash: hash(sourceContent),
        snippets: [{
          content: "if (current.version === expectedVersion) cache.delete(key)",
          lineStart: 3,
          lineEnd: 3,
          truncated: false,
        }],
      }],
    } };
  }
  assert.equal(name, "project_read");
  return { ok: true, value: {
    type: "project_read", projectId: "offeragent", path: sourcePath, evidencePath,
    lineStart: 1, lineEnd: 4, modifiedVersion: "mtime:1:size:150",
    contentHash: hash(sourceContent), content: sourceContent, truncated: false,
  } };
}

function toolCalls(events) {
  return events.filter((event) => event.type === "tool_call.requested");
}

function output(events, stderr) {
  assert.equal(events.at(-1).type, "agent_run.completed", stderr);
  return events.at(-1).output.text;
}

async function startFixture(t) {
  const temporaryDirectory = await mkdtemp(path.join(os.tmpdir(), "offeragent-project-training-"));
  const token = "project-training-token";
  const runtime = spawn(process.execPath, [
    runtimeEntry, "--port", "0", "--token", token, "--parent-pid", `${process.pid}`,
    "--provider", "fake", "--fake-scenario", "project-interview-training",
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
  return { socket, stderr: () => stderr };
}

test("Project Interview Training asks one question at a time and persists only a confirmed outcome", async (t) => {
  const { socket, stderr } = await startFixture(t);
  const vault = new Map([[experiencePath, [
    "---",
    "company: Example",
    "position: Senior Backend Engineer",
    "date: 2026-07-10",
    "---",
    "# Recent interview",
    "",
    "The interviewer focused on stale cache invalidation under concurrent requests.",
  ].join("\n")]]);
  const proposals = [];
  const conversationId = "project-training-confirmed";
  let runNumber = 0;

  const run = async (text) => {
    runNumber += 1;
    return runWithToolPeer(socket, {
      type: "agent_run.start", protocolVersion: 1,
      eventId: `project-training-confirmed-start-${runNumber}`,
      conversationId, agentRunId: `project-training-confirmed-run-${runNumber}`, sequence: 0,
      model: "fake-interview-model", input: { role: "user", text },
    }, (event) => {
      if (event.tool.name === "interview_catalog") {
        return { ok: true, value: {
          type: "interview_catalog", truncated: false,
          experienceCandidates: [{
            path: experiencePath, title: "Recent cache invalidation interview",
            company: "Example", position: "Senior Backend Engineer", date: "2026-07-10",
            matchKinds: ["repost-candidate"], modifiedVersion: "mtime:1:size:210", contentHash: hash(vault.get(experiencePath)),
          }],
          questionCandidates: [],
          indexes: [
            { kind: "experience", path: "experiences/index.md", exists: true, modifiedVersion: "mtime:1:size:10", contentHash: hash("index") },
            { kind: "question", path: "interview/index.md", exists: true, modifiedVersion: "mtime:1:size:10", contentHash: hash("index") },
          ],
        } };
      }
      if (event.tool.name === "vault_read") {
        const target = event.tool.arguments.path;
        const content = vault.get(target);
        return content === undefined
          ? { ok: false, error: { code: "not_found", message: "The requested Vault file does not exist." } }
          : readResult(target, content);
      }
      if (event.tool.name === "project_search" || event.tool.name === "project_read") {
        assert.equal(event.tool.arguments.projectId, "offeragent");
        return projectResult(event.tool.name);
      }
      assert.equal(event.tool.name, "vault_propose_changes");
      const proposal = event.tool.arguments;
      proposals.push(proposal);
      assert.equal(proposal.actions.length, 3);
      for (const action of proposal.actions) {
        assert.equal(action.operation, "create");
        assert.equal(action.expectedVersion, "missing");
        assert.equal(vault.has(action.path), false);
      }
      for (const action of proposal.actions) vault.set(action.path, action.content);
      return { ok: true, value: {
        type: "vault_propose_changes", batchId: proposal.batchId, decision: "applied",
        checkpointRef: `refs/offeragent/checkpoints/${proposal.batchId}`,
        targets: proposal.actions.map((action) => ({
          path: action.path, beforeHash: hash(""), afterHash: hash(action.content),
        })),
      } };
    });
  };

  const selected = await run("Start Project Interview Training for my registered OfferAgent project and a senior backend role. Choose the question for me.");
  const selectedText = output(selected, stderr());
  assert.equal((selectedText.match(/\?/g) ?? []).length, 1);
  assert.match(selectedText, /How did you prevent a stale request from invalidating a newer cached value\?/);
  assert.deepEqual(
    toolCalls(selected).map((event) => event.tool.name).filter((name) => name !== "agent_contract_read" && name !== "planning_memory_list"),
    ["interview_catalog", "vault_read", "project_search", "project_read"],
  );
  assert.equal(proposals.length, 0);

  const followedUp = await run("I compared the expected version with the current cache entry and deleted only when they matched.");
  const followUpText = output(followedUp, stderr());
  assert.equal((followUpText.match(/\?/g) ?? []).length, 1);
  assert.match(followUpText, /Project Evidence: project\/offeragent\/src\/cache\.ts:1-4/);
  assert.match(followUpText, /failure case/i);
  assert.deepEqual(
    toolCalls(followedUp).map((event) => event.tool.name).filter((name) => name.startsWith("project_")),
    ["project_search", "project_read"],
  );
  assert.equal(proposals.length, 0);

  const feedback = await run("A delayed invalidation can arrive after a refresh. The version guard makes that stale invalidation a no-op; I would monitor stale-invalidation count and cache hit rate, but those metrics were not recorded.");
  const feedbackText = output(feedback, stderr());
  assert.match(feedbackText, /Project facts.*covered/is);
  assert.match(feedbackText, /Design and tradeoffs.*covered/is);
  assert.match(feedbackText, /Metrics.*needs evidence/is);
  assert.match(feedbackText, /Failure cases.*covered/is);
  assert.match(feedbackText, /Implementation.*covered/is);
  assert.match(feedbackText, /Evidence:/);
  assert.match(feedbackText, /Coaching suggestion:/);
  assert.match(feedbackText, /Refined Project Answer/);
  assert.match(feedbackText, /solo ownership/i);
  assert.doesNotMatch(feedbackText, /(?:total|overall)\s*(?:score)?\s*[:=]?\s*\d|\d+\s*\/\s*\d+/i);
  assert.equal(proposals.length, 0);

  const confirmed = await run("Yes. Save that refined Training Outcome.");
  assert.match(output(confirmed, stderr()), /applied.*projects\/offeragent/is);
  assert.deepEqual(
    toolCalls(confirmed).map((event) => event.tool.name).filter((name) => name.startsWith("project_")),
    ["project_search", "project_read"],
  );
  assert.equal(proposals.length, 1);
  assert.deepEqual(proposals[0].actions.map(({ path: target }) => target), [profilePath, indexPath, answerPath]);
  assert.match(vault.get(profilePath), /solo ownership/i);
  assert.match(vault.get(indexPath), /\[Cache invalidation under stale requests\]\(answers\/cache-invalidation\.md\)/);
  const savedAnswer = vault.get(answerPath);
  assert.match(savedAnswer, /Training Outcome/);
  assert.match(savedAnswer, /Project Evidence: project\/offeragent\/src\/cache\.ts:1-4/);
  assert.match(savedAnswer, /Metrics remain an evidence gap/);
  assert.match(savedAnswer, /retrain/i);
  assert.doesNotMatch(savedAnswer, /User:|Assistant:|full transcript|I compared the expected version/i);
});

test("a later confirmed training outcome updates the existing project profile, index, and answer atomically", async (t) => {
  const { socket, stderr } = await startFixture(t);
  const vault = new Map([
    [profilePath, [
      "---",
      "title: OfferAgent Project Interview Profile",
      "project-id: offeragent",
      "ownership: solo",
      "---",
      "",
      "# OfferAgent Project Interview Profile",
      "",
      "## Training: Durable queue recovery",
      "",
      "Keep this existing stable fact and retraining note.",
      "",
    ].join("\n")],
    [indexPath, [
      "# OfferAgent Project Interview Answers",
      "",
      "- [Durable queue recovery](answers/durable-queue-recovery.md)",
      "",
    ].join("\n")],
    [answerPath, "# Outdated cache answer\n\nReplace this confirmed outcome without creating a second file.\n"],
  ]);
  const conversationId = "project-training-existing";
  let runNumber = 0;
  let proposal;
  const run = async (text) => {
    runNumber += 1;
    return runWithToolPeer(socket, {
      type: "agent_run.start", protocolVersion: 1,
      eventId: `project-training-existing-start-${runNumber}`,
      conversationId, agentRunId: `project-training-existing-run-${runNumber}`, sequence: 0,
      model: "fake-interview-model", input: { role: "user", text },
    }, (event) => {
      if (event.tool.name === "project_search" || event.tool.name === "project_read") {
        return projectResult(event.tool.name);
      }
      if (event.tool.name === "vault_read") {
        const target = event.tool.arguments.path;
        assert.equal(vault.has(target), true);
        return readResult(target, vault.get(target));
      }
      assert.equal(event.tool.name, "vault_propose_changes");
      proposal = event.tool.arguments;
      assert.equal(proposal.actions.length, 3);
      for (const action of proposal.actions) {
        const current = vault.get(action.path);
        assert.equal(action.operation, "exact_replace");
        assert.equal(action.expectedContent, current);
        assert.equal(action.expectedVersion, readResult(action.path, current).value.modifiedVersion);
        assert.ok(action.replacement.length > 0);
      }
      for (const action of proposal.actions) vault.set(action.path, action.replacement);
      return { ok: true, value: {
        type: "vault_propose_changes", batchId: proposal.batchId, decision: "applied",
        checkpointRef: `refs/offeragent/checkpoints/${proposal.batchId}`,
        targets: proposal.actions.map((action) => ({
          path: action.path, beforeHash: hash(action.expectedContent), afterHash: hash(action.replacement),
        })),
      } };
    });
  };

  output(await run("Train me on this exact OfferAgent question: How did you prevent a stale request from invalidating a newer cached value?"), stderr());
  output(await run("I used an expected-version check before deletion."), stderr());
  output(await run("A delayed stale invalidation becomes a no-op after a refresh, and measured production metrics remain unknown."), stderr());
  const confirmed = await run("Confirm and save this retrained outcome.");
  assert.match(output(confirmed, stderr()), /applied atomically/i);
  assert.ok(proposal);
  assert.deepEqual(proposal.actions.map(({ path: target }) => target), [profilePath, indexPath, answerPath]);
  assert.match(vault.get(profilePath), /Keep this existing stable fact and retraining note/);
  assert.match(vault.get(profilePath), /Training: Cache invalidation under stale requests/);
  assert.match(vault.get(indexPath), /Durable queue recovery/);
  assert.match(vault.get(indexPath), /Cache invalidation under stale requests/);
  assert.doesNotMatch(vault.get(answerPath), /Outdated cache answer/);
  assert.match(vault.get(answerPath), /Training Outcome/);
  for (const content of vault.values()) {
    assert.doesNotMatch(content, /User:|Assistant:|full transcript/i);
  }
});

test("a user-selected Project Interview question can end with a declined write", async (t) => {
  const { socket, stderr } = await startFixture(t);
  const conversationId = "project-training-declined";
  let runNumber = 0;
  let proposals = 0;
  const run = async (text) => {
    runNumber += 1;
    return runWithToolPeer(socket, {
      type: "agent_run.start", protocolVersion: 1,
      eventId: `project-training-declined-start-${runNumber}`,
      conversationId, agentRunId: `project-training-declined-run-${runNumber}`, sequence: 0,
      model: "fake-interview-model", input: { role: "user", text },
    }, (event) => {
      if (event.tool.name === "project_search" || event.tool.name === "project_read") return projectResult(event.tool.name);
      if (event.tool.name === "vault_propose_changes") proposals += 1;
      return { ok: false, error: { code: "not_found", message: "No Vault file exists." } };
    });
  };

  const question = await run("Train me on this exact OfferAgent question: How did you prevent a stale request from invalidating a newer cached value?");
  assert.equal((output(question, stderr()).match(/\?/g) ?? []).length, 1);
  assert.equal(toolCalls(question).some((event) => event.tool.name === "interview_catalog"), false);
  await run("I used an expected-version check before deletion.");
  const refined = await run("The failure is a delayed stale invalidation after refresh; the guard turns it into a no-op.");
  assert.match(output(refined, stderr()), /Refined Project Answer/);
  const declined = await run("No, do not save this training outcome.");
  assert.match(output(declined, stderr()), /not saved|no Vault change/i);
  assert.equal(proposals, 0);
  assert.equal(toolCalls(declined).some((event) => event.tool.name === "vault_propose_changes"), false);
});
