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

async function runtimeFor(t) {
  const temporaryDirectory = await mkdtemp(path.join(os.tmpdir(), "offeragent-knowledge-plan-"));
  const token = "knowledge-plan-token";
  const runtime = spawn(process.execPath, [
    runtimeEntry,
    "--port", "0",
    "--token", token,
    "--parent-pid", `${process.pid}`,
    "--provider", "fake",
    "--fake-scenario", "interview-knowledge-planning",
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

function dailyContext(targetExists = false) {
  return {
    ok: true,
    value: {
      type: "daily_note_context",
      resolvedDate: "2026-07-15",
      dateFormat: "YYYY-MM-DD",
      targetPath: "daily/2026-07-15.md",
      targetExists,
      targetVersion: targetExists ? "mtime:50:size:200" : "missing",
      templatePath: "templates/daily.md",
      templateContent: "# {{date}}\n\n## Daily Study Plan\n",
      templateVersion: "mtime:10:size:40",
    },
  };
}

function catalog(entries, experiences = []) {
  return {
    ok: true,
    value: {
      type: "interview_catalog",
      experienceCandidates: experiences.map(({ path: experiencePath, title, company, position, date }, index) => ({
        path: experiencePath,
        title,
        company,
        position,
        date,
        matchKinds: ["repost-candidate"],
        modifiedVersion: `mtime:${70 + index}:size:300`,
        contentHash: `sha256:experience-${index}`,
      })),
      questionCandidates: entries.map(({ path: questionPath, title, answerState }, index) => ({
        path: questionPath,
        title,
        answerState,
        matchKinds: ["semantic-candidate"],
        modifiedVersion: `mtime:${100 + index}:size:300`,
        contentHash: `sha256:question-${index}`,
      })),
      indexes: [
        { kind: "experience", path: "experiences/index.md", exists: true, modifiedVersion: "mtime:80:size:40", contentHash: "sha256:experiences" },
        { kind: "question", path: "interview/index.md", exists: true, modifiedVersion: "mtime:81:size:40", contentHash: "sha256:questions" },
      ],
      truncated: false,
    },
  };
}

function question({ title, answerState, learningState, frequency, extra = "" }) {
  return [
    "---",
    `title: ${title}`,
    `answer-state: ${answerState}`,
    `learning-state: ${learningState}`,
    `frequency: ${frequency}`,
    extra,
    "---",
    "",
    `# ${title}`,
    "",
    "Exact Interview Question evidence.",
    "",
  ].filter((line) => line !== "").join("\n");
}

function experience({ title, company, position, date, questionPaths }) {
  return `---\ntitle: ${title}\ncompany: ${company}\nposition: ${position}\ndate: ${date}\n---\n\n# ${title}\n\n${questionPaths.map((questionPath) => `- [[${questionPath.replace(/\.md$/u, "")}]]`).join("\n")}\n`;
}

function read(pathname, content) {
  return {
    ok: true,
    value: {
      type: "vault_read",
      path: pathname,
      lineStart: 1,
      lineEnd: content.split("\n").length,
      modifiedVersion: `mtime:200:size:${content.length}`,
      contentHash: `sha256:${pathname.replaceAll("/", "-")}`,
      content,
      truncated: false,
    },
  };
}

function noRecentPlans() {
  return { ok: true, value: { type: "vault_search", entries: [], truncated: false } };
}

function applied(proposal) {
  return {
    ok: true,
    value: {
      type: "vault_propose_changes",
      batchId: proposal.batchId,
      decision: "applied",
      checkpointRef: `refs/offeragent/checkpoints/${proposal.batchId}`,
      targets: proposal.actions.map(({ path: targetPath }) => ({
        path: targetPath,
        beforeHash: `sha256:${"a".repeat(64)}`,
        afterHash: `sha256:${"b".repeat(64)}`,
      })),
    },
  };
}

test("explicit Interview Knowledge planning separates research, study, and in-progress work", async (t) => {
  const { socket, stderr } = await runtimeFor(t);
  const questions = [
    { path: "interview/distributed-cache-consistency.md", title: "Distributed cache consistency", answerState: "needs-research", learningState: "study-todo", frequency: 6, extra: "technical-direction: distributed systems" },
    { path: "interview/consensus-failure-modes.md", title: "Consensus failure modes", answerState: "verified", learningState: "study-todo", frequency: 8, extra: "technical-direction: distributed systems" },
    { path: "interview/idempotent-consumer.md", title: "Idempotent consumer", answerState: "draft", learningState: "study-in-progress", frequency: 4, extra: "technical-direction: distributed systems" },
    { path: "interview/css-layout.md", title: "CSS layout", answerState: "verified", learningState: "study-todo", frequency: 25, extra: "technical-direction: frontend" },
  ];
  const experiences = [
    { path: "experiences/example-backend-july.md", title: "Example backend July", company: "Example Corp", position: "Backend Engineer", date: "2026-07-01", questionPaths: questions.slice(0, 3).map(({ path: questionPath }) => questionPath) },
    { path: "experiences/example-backend-june.md", title: "Example backend June", company: "Example Corp", position: "Backend Engineer", date: "2026-06-15", questionPaths: [questions[1].path] },
  ];
  let proposal;
  const events = await runWithToolPeer(
    socket,
    start(
      "knowledge-plan-explicit",
      "Create today's Daily Study Plan for the Example Corp Backend Engineer interview on 2026-07-20; my study goal is distributed systems readiness.",
    ),
    (event) => {
      if (event.tool.name === "daily_note_context") return dailyContext();
      if (event.tool.name === "interview_catalog") {
        assert.equal(event.tool.arguments.limit <= 5, true);
        assert.match(event.tool.arguments.query, /Example Corp.*Backend Engineer.*2026-07-20.*distributed systems/i);
        return catalog(questions, experiences);
      }
      if (event.tool.name === "vault_search") {
        assert.equal(event.tool.arguments.limit <= 3, true);
        return noRecentPlans();
      }
      if (event.tool.name === "vault_read") {
        if (event.tool.arguments.path === "projects/index.md") return read("projects/index.md", "# Project Registry\n");
        const matchingExperience = experiences.find(({ path: experiencePath }) => experiencePath === event.tool.arguments.path);
        if (matchingExperience) return read(matchingExperience.path, experience(matchingExperience));
        const entry = questions.find(({ path: questionPath }) => questionPath === event.tool.arguments.path);
        assert.ok(entry, `Unexpected exact read ${event.tool.arguments.path}`);
        return read(entry.path, question(entry));
      }
      assert.equal(event.tool.name, "vault_propose_changes");
      proposal = event.tool.arguments;
      return applied(proposal);
    },
  );
  assert.equal(events.at(-1).type, "agent_run.completed", stderr());
  assert.ok(proposal);
  assert.equal(proposal.actions.length, 1);
  assert.equal(proposal.actions[0].path, "daily/2026-07-15.md");
  assert.match(proposal.actions[0].content, /Research answer.*Distributed cache consistency/i);
  assert.match(proposal.actions[0].content, /Study and rehearse.*Consensus failure modes/i);
  assert.match(proposal.actions[0].content, /Continue.*Idempotent consumer/i);
  assert.doesNotMatch(proposal.actions[0].content, /CSS layout/i);
  assert.match(proposal.actions[0].content, /Answer State.*Learning State/i);
  assert.doesNotMatch(proposal.actions[0].content, /\[x\]|study-done|Study Evidence:/i);
});

test("registered-project risk and justified review outrank low-value recent repetition", async (t) => {
  const { socket, stderr } = await runtimeFor(t);
  const questions = [
    { path: "interview/sqlite-crash-recovery.md", title: "SQLite crash recovery", answerState: "needs-research", learningState: "study-todo", frequency: 2, extra: "project: OfferAgent\nresume-deep-dive-risk: high" },
    { path: "interview/tls-handshake.md", title: "TLS handshake", answerState: "verified", learningState: "study-todo", frequency: 7 },
    { path: "interview/http-caching.md", title: "HTTP caching", answerState: "needs-research", learningState: "study-todo", frequency: 9 },
    { path: "interview/dns-caching.md", title: "DNS caching", answerState: "verified", learningState: "study-todo", frequency: 25 },
  ];
  const experiences = [
    { path: "experiences/backend-current-one.md", title: "Backend current one", company: "Example Corp", position: "Backend Engineer", date: "2026-07-01", questionPaths: [questions[2].path, questions[1].path] },
    { path: "experiences/backend-current-two.md", title: "Backend current two", company: "Example Corp", position: "Backend Engineer", date: "2026-06-20", questionPaths: [questions[2].path] },
    { path: "experiences/other-old.md", title: "Other old interview", company: "Other Corp", position: "Backend Engineer", date: "2024-01-01", questionPaths: [questions[3].path] },
  ];
  const recentPath = "daily/2026-07-14.md";
  let proposal;
  const events = await runWithToolPeer(
    socket,
    start("knowledge-plan-project", "Create today's Daily Study Plan for backend interview preparation and include my registered OfferAgent project risks."),
    (event) => {
      if (event.tool.name === "daily_note_context") return dailyContext();
      if (event.tool.name === "interview_catalog") return catalog(questions, experiences);
      if (event.tool.name === "vault_search") {
        return {
          ok: true,
          value: {
            type: "vault_search",
            entries: [{
              path: recentPath,
              matchTier: "body",
              modifiedVersion: "mtime:220:size:100",
              contentHash: "sha256:recent-plan",
              snippets: [{ content: "HTTP caching and DNS caching", lineStart: 4, lineEnd: 6, truncated: false }],
            }],
            truncated: false,
          },
        };
      }
      if (event.tool.name === "vault_read") {
        if (event.tool.arguments.path === recentPath) {
          return read(recentPath, "# 2026-07-14\n\n## Daily Study Plan\n\n- [ ] HTTP caching\n- [ ] DNS caching\n");
        }
        if (event.tool.arguments.path === "projects/index.md") {
          return read("projects/index.md", "# Project Registry\n\n- [[projects/offeragent]]\n");
        }
        if (event.tool.arguments.path === "projects/offeragent.md") {
          return read("projects/offeragent.md", "# OfferAgent\n\nResume deep-dive risk: SQLite crash recovery and atomic persistence.");
        }
        const matchingExperience = experiences.find(({ path: experiencePath }) => experiencePath === event.tool.arguments.path);
        if (matchingExperience) return read(matchingExperience.path, experience(matchingExperience));
        const entry = questions.find(({ path: questionPath }) => questionPath === event.tool.arguments.path);
        assert.ok(entry, `Unexpected exact read ${event.tool.arguments.path}`);
        return read(entry.path, question(entry));
      }
      assert.equal(event.tool.name, "vault_propose_changes");
      proposal = event.tool.arguments;
      return applied(proposal);
    },
  );
  assert.equal(events.at(-1).type, "agent_run.completed", stderr());
  assert.ok(proposal);
  const content = proposal.actions[0].content;
  assert.match(content, /Research answer.*SQLite crash recovery.*Project resume deep-dive/i);
  assert.match(content, /Research answer.*HTTP caching.*Justified review.*2 recent matching Experiences/i);
  assert.match(content, /TLS handshake/i);
  assert.doesNotMatch(content, /DNS caching/i);
  assert.equal(content.indexOf("SQLite crash recovery") < content.indexOf("TLS handshake"), true);
  assert.doesNotMatch(content, /\[x\]|study-in-progress\s*->|learning-state:/i);
});

test("an existing Daily plan is merged without deleting checked or user-authored content", async (t) => {
  const { socket, stderr } = await runtimeFor(t);
  const entry = { path: "interview/consensus-failure-modes.md", title: "Consensus failure modes", answerState: "verified", learningState: "study-todo", frequency: 3, extra: "technical-direction: distributed systems" };
  const currentDaily = "# 2026-07-15\n\n## Daily Study Plan\n\n- [x] Preserve completed user review\n- [ ] User-authored custom task\n\n## Notes\n\nKeep this note.\n";
  let proposal;
  const events = await runWithToolPeer(
    socket,
    start("knowledge-plan-existing", "Create today's Daily Study Plan; my study goal is distributed systems readiness."),
    (event) => {
      if (event.tool.name === "daily_note_context") return dailyContext(true);
      if (event.tool.name === "interview_catalog") return catalog([entry]);
      if (event.tool.name === "vault_search") return noRecentPlans();
      if (event.tool.name === "vault_read") {
        if (event.tool.arguments.path === "projects/index.md") return read("projects/index.md", "# Project Registry\n");
        if (event.tool.arguments.path === "daily/2026-07-15.md") return read("daily/2026-07-15.md", currentDaily);
        return read(entry.path, question(entry));
      }
      assert.equal(event.tool.name, "vault_propose_changes");
      proposal = event.tool.arguments;
      return applied(proposal);
    },
  );
  assert.equal(events.at(-1).type, "agent_run.completed", stderr());
  assert.ok(proposal);
  assert.equal(proposal.actions[0].operation, "exact_replace");
  assert.match(proposal.actions[0].replacement, /\[x\] Preserve completed user review/);
  assert.match(proposal.actions[0].replacement, /\[ \] User-authored custom task/);
  assert.match(proposal.actions[0].replacement, /Consensus failure modes/);
  assert.match(proposal.actions[0].replacement, /## Notes\n\nKeep this note\./);
});

test("a prose-only Daily plan section is preserved and augmented", async (t) => {
  const { socket, stderr } = await runtimeFor(t);
  const entry = { path: "interview/cache-observability.md", title: "Cache observability", answerState: "draft", learningState: "study-todo", frequency: 2 };
  const currentDaily = "# 2026-07-15\n\n## Daily Study Plan\n\nFocus on explanations, not memorization.\n\n| Topic | Why |\n| --- | --- |\n| Existing custom row | User rationale |\n\n## Notes\n\nPreserve me.\n";
  let proposal;
  const events = await runWithToolPeer(
    socket,
    start("knowledge-plan-prose", "Create today's Daily Study Plan for backend interview preparation."),
    (event) => {
      if (event.tool.name === "daily_note_context") return dailyContext(true);
      if (event.tool.name === "interview_catalog") return catalog([entry]);
      if (event.tool.name === "vault_search") return noRecentPlans();
      if (event.tool.name === "vault_read") {
        if (event.tool.arguments.path === "projects/index.md") return read("projects/index.md", "# Project Registry\n");
        if (event.tool.arguments.path === "daily/2026-07-15.md") return read("daily/2026-07-15.md", currentDaily);
        return read(entry.path, question(entry));
      }
      assert.equal(event.tool.name, "vault_propose_changes");
      proposal = event.tool.arguments;
      return applied(proposal);
    },
  );
  assert.equal(events.at(-1).type, "agent_run.completed", stderr());
  assert.ok(proposal);
  assert.match(proposal.actions[0].replacement, /Focus on explanations, not memorization\./);
  assert.match(proposal.actions[0].replacement, /Existing custom row \| User rationale/);
  assert.match(proposal.actions[0].replacement, /Cache observability/);
  assert.match(proposal.actions[0].replacement, /## Notes\n\nPreserve me\./);
});

test("Interview Knowledge planning never starts in the background", async (t) => {
  const { socket, stderr } = await runtimeFor(t);
  const events = await runWithToolPeer(
    socket,
    start("knowledge-plan-background", "Summarize the previous response in one sentence."),
    (event) => assert.fail(`Unexpected local tool ${event.tool.name}`),
  );
  assert.equal(events.at(-1).type, "agent_run.completed", stderr());
  assert.match(events.at(-1).output.text, /no daily study plan|summary/i);
});
