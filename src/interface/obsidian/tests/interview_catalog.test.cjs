const assert = require("node:assert/strict");
const path = require("node:path");
const test = require("node:test");
const { buildSync } = require("esbuild");

function loadModule() {
    const output = buildSync({
        entryPoints: [path.join(__dirname, "../src/runtime/interview_catalog.ts")],
        bundle: true,
        format: "cjs",
        platform: "node",
        target: "node16",
        write: false,
    }).outputFiles[0].text;
    const compiled = { exports: {} };
    new Function("require", "module", "exports", output)(require, compiled, compiled.exports);
    return compiled.exports;
}

function call(arguments_) {
    return {
        toolCallId: "call_catalog",
        workspaceId: "ws_vault",
        runId: "run_ingest",
        name: "interview_catalog.search",
        version: "1",
        arguments: arguments_,
        argsHash: `sha256:${"a".repeat(64)}`,
        idempotencyKey: "catalog-1",
        risk: "read",
        reason: null,
        agentLineage: ["run_ingest"],
        executorLocation: "plugin",
        definitionFingerprint: `sha256:${"b".repeat(64)}`,
        resultSensitivity: "workspace",
        deadline: null,
    };
}

function vaultFrom(entries) {
    const files = new Map(Object.entries(entries).map(([filePath, content], index) => [filePath, {
        file: { path: filePath, extension: "md", stat: { mtime: index + 1, size: Buffer.byteLength(content) } },
        content,
    }]));
    return {
        getFiles: () => [...files.values()].map((entry) => entry.file),
        getFileByPath: (filePath) => files.get(filePath)?.file ?? null,
        cachedRead: async (file) => files.get(file.path).content,
        files,
    };
}

const experience = (id, url, fingerprint, candidate, date) => `---
type: interview-experience
experience-id: ${id}
source-kind: url
source-url: ${url}
source-fingerprint: ${fingerprint}
company: Acme
role: Backend Engineer
candidate: ${candidate}
event-date: ${date}
round: technical-1
---
# ${id}
`;

const question = (id, title, state = "needs-research", frequency = 1) => `---
type: interview-question
question-id: ${id}
title: ${title}
answer-state: ${state}
frequency: ${frequency}
---
# ${title}
`;

test("Interview Catalog finds exact source identity and bounded semantic candidates without merging events", async () => {
    const { InterviewCatalogAdapter } = loadModule();
    const fingerprint = `sha256:${"1".repeat(64)}`;
    const vault = vaultFrom({
        "interviews/experiences/acme-alice.md": experience(
            "exp_alice", "https://example.com/post/7?utm_source=feed", fingerprint, "alice", "2026-06-01",
        ),
        "interviews/experiences/acme-bob.md": experience(
            "exp_bob", "https://example.com/post/8", `sha256:${"2".repeat(64)}`, "bob", "2026-06-03",
        ),
        "interviews/questions/event-loop.md": question("question_event_loop", "Explain the Node.js event loop", "draft", 3),
        "interviews/questions/sql-index.md": question("question_sql_index", "How does a SQL index work?"),
        "memory/study/not-catalog.md": question("question_hidden", "Event loop"),
    });
    const result = await new InterviewCatalogAdapter(vault).execute(call({
        sourceUrl: "https://EXAMPLE.com/post/7?utm_source=repost#comments",
        company: "Acme",
        role: "Backend Engineer",
        questionTerms: ["Node event loop"],
    }));

    assert.equal(result.status, "succeeded");
    assert.equal(result.data.experienceCandidates.length, 2);
    assert.equal(result.data.experienceCandidates[0].experienceId, "exp_alice");
    assert.equal(result.data.experienceCandidates[0].exactSourceMatch, true);
    assert.deepEqual(result.data.experienceCandidates.map((item) => item.candidate).sort(), ["alice", "bob"]);
    assert.deepEqual(result.data.questionCandidates.map((item) => item.questionId), ["question_event_loop"]);
    assert.equal(result.data.questionCandidates[0].answerState, "draft");
    assert.equal(result.data.questionCandidates[0].frequency, 3);
    assert.match(result.data.questionCandidates[0].contentHash, /^sha256:[0-9a-f]{64}$/);
    assert.equal(result.data.truncated, false);
});

test("Interview Catalog rejects stale discovery and marks over-capacity question candidates truncated", async () => {
    const { InterviewCatalogAdapter } = loadModule();
    const entries = Object.fromEntries(Array.from({ length: 101 }, (_, index) => [
        `interviews/questions/q-${index}.md`, question(`question_${index}`, `Distributed systems topic ${index}`),
    ]));
    const vault = vaultFrom(entries);
    const bounded = await new InterviewCatalogAdapter(vault).execute(call({ questionTerms: [] }));
    assert.equal(bounded.status, "succeeded");
    assert.equal(bounded.data.questionCandidates.length, 100);
    assert.equal(bounded.data.truncated, true);

    const staleVault = vaultFrom({ "interviews/questions/q.md": question("question_q", "Queues") });
    staleVault.cachedRead = async (file) => {
        file.stat.mtime += 1;
        return staleVault.files.get(file.path).content;
    };
    const stale = await new InterviewCatalogAdapter(staleVault).execute(call({}));
    assert.equal(stale.status, "failed");
    assert.equal(stale.error.code, "resource.conflict");
});
