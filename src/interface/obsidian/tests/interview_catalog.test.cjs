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

test("Interview Catalog normalizes raw sources, fingerprints ordered images, and exposes exact indexes", async () => {
    const { InterviewCatalogAdapter } = loadModule();
    const vault = vaultFrom({
        "experiences/index.md": "# Interview Experiences\n",
        "interview/index.md": "# Interview Questions\n",
    });
    const firstImage = `sha256:${"a".repeat(64)}`;
    const secondImage = `sha256:${"b".repeat(64)}`;

    const result = await new InterviewCatalogAdapter(vault).execute(call({
        sourceUrls: [
            "HTTPS://Example.COM/post?z=last&utm_source=feed&access_token=secret&a=first#comments",
            "https://docs.example.com/guide?b=2&a=3&a=1&X-Amz-Signature=secret&expires=123",
        ],
        orderedImageContentHashes: [firstImage, secondImage],
        questionTerms: [],
    }));

    assert.equal(result.status, "succeeded");
    assert.deepEqual(result.data.normalizedSource, {
        canonicalUrls: [
            "https://example.com/post?a=first&z=last",
            "https://docs.example.com/guide?a=1&a=3&b=2",
        ],
        sourceFingerprint: "sha256:0cd65e3ab3208489c6bfaa9d2ce2cb125ae8fbef34c9a965c1efe3f7d2302c37",
        orderedImageContentHashes: [firstImage, secondImage],
    });
    assert.deepEqual(result.data.indexes, [
        {
            kind: "experience",
            path: "experiences/index.md",
            exists: true,
            modifiedVersion: "mtime:1:size:24",
            contentHash: "sha256:fcf6d9aac00bb0df3b23505378986f54d49c1bcef25999dbde149bdf2b50dd6b",
        },
        {
            kind: "question",
            path: "interview/index.md",
            exists: true,
            modifiedVersion: "mtime:2:size:22",
            contentHash: "sha256:161e2c72c700dc8a4d3f37889f9f9ae99ea4c17d4beb9a20de7a50c6ebc2faa8",
        },
    ]);
});

test("Interview Catalog rejects non-public and userinfo source URLs", async () => {
    const { InterviewCatalogAdapter } = loadModule();
    const catalog = new InterviewCatalogAdapter(vaultFrom({}));

    for (const sourceUrl of [
        "http://localhost/interview",
        "http://127.0.0.1/interview",
        "http://10.0.0.7/interview",
        "http://169.254.1.2/interview",
        "http://[::1]/interview",
        "http://[::ffff:127.0.0.1]/interview",
        "http://[::ffff:0a00:0007]/interview",
        "http://example/interview",
        "https://user:password@example.com/interview",
    ]) {
        const result = await catalog.execute(call({ sourceUrls: [sourceUrl] }));
        assert.equal(result.status, "failed", sourceUrl);
        assert.equal(result.error.code, "protocol.invalid_params", sourceUrl);
    }
});

test("Interview Catalog removes temporary credential and tracking query parameters", async () => {
    const { InterviewCatalogAdapter } = loadModule();
    const result = await new InterviewCatalogAdapter(vaultFrom({})).execute(call({
        sourceUrls: [
            "https://cdn.example.com/interview?keep=1&refresh_token=refresh&id_token=id" +
            "&session-token=session&security_token=security&GoogleAccessId=principal" +
            "&Signature=signed&Expires=100&Key-Pair-Id=pair&Policy=policy" +
            "&igshid=tracking&msclkid=tracking-too",
        ],
    }));

    assert.equal(result.status, "succeeded");
    assert.deepEqual(result.data.normalizedSource.canonicalUrls, [
        "https://cdn.example.com/interview?keep=1",
    ]);
});

test("Interview Catalog sorts retained query parameters by deterministic code-point order", async () => {
    const { InterviewCatalogAdapter } = loadModule();
    const result = await new InterviewCatalogAdapter(vaultFrom({})).execute(call({
        sourceUrls: ["https://example.com/interview?b=2&A=upper&a=lower&B=1"],
    }));

    assert.equal(result.status, "succeeded");
    assert.deepEqual(result.data.normalizedSource.canonicalUrls, [
        "https://example.com/interview?A=upper&B=1&a=lower&b=2",
    ]);
});

test("Interview Catalog preserves ordered-image identity when page order changes", async () => {
    const { InterviewCatalogAdapter } = loadModule();
    const catalog = new InterviewCatalogAdapter(vaultFrom({}));
    const firstImage = `sha256:${"a".repeat(64)}`;
    const secondImage = `sha256:${"b".repeat(64)}`;

    const forward = await catalog.execute(call({ orderedImageContentHashes: [firstImage, secondImage] }));
    const reversed = await catalog.execute(call({ orderedImageContentHashes: [secondImage, firstImage] }));

    assert.equal(forward.status, "succeeded");
    assert.equal(reversed.status, "succeeded");
    assert.equal(
        forward.data.normalizedSource.sourceFingerprint,
        "sha256:0cd65e3ab3208489c6bfaa9d2ce2cb125ae8fbef34c9a965c1efe3f7d2302c37",
    );
    assert.equal(
        reversed.data.normalizedSource.sourceFingerprint,
        "sha256:91248f80db44bc7684df6335570b2143d476a4ee87edb7baa2c896383e283aba",
    );
});

test("Interview Catalog rejects legacy source aliases and unknown input fields", async () => {
    const { InterviewCatalogAdapter } = loadModule();
    const catalog = new InterviewCatalogAdapter(vaultFrom({}));
    const legacyFingerprint = `sha256:${"7".repeat(64)}`;

    for (const arguments_ of [
        { sourceUrl: "https://example.com/post/7" },
        { sourceFingerprint: legacyFingerprint },
        { sourceUrls: ["https://example.com/post/7"], unexpected: true },
    ]) {
        const result = await catalog.execute(call(arguments_));
        assert.equal(result.status, "failed");
        assert.equal(result.error.code, "protocol.invalid_params");
    }
});

test("Interview Catalog bounds raw URLs and ordered image hashes", async () => {
    const { InterviewCatalogAdapter } = loadModule();
    const catalog = new InterviewCatalogAdapter(vaultFrom({}));
    const tooManyUrls = await catalog.execute(call({
        sourceUrls: Array.from({ length: 9 }, (_, index) => `https://example.com/interview/${index}`),
    }));
    const tooManyImages = await catalog.execute(call({
        orderedImageContentHashes: Array.from(
            { length: 21 },
            (_, index) => `sha256:${index.toString(16).padStart(64, "0")}`,
        ),
    }));
    const canonicalExpansion = await catalog.execute(call({
        sourceUrls: [`https://example.com/${"中".repeat(600)}`],
    }));

    assert.equal(tooManyUrls.status, "failed");
    assert.equal(tooManyUrls.error.code, "protocol.invalid_params");
    assert.equal(tooManyImages.status, "failed");
    assert.equal(tooManyImages.error.code, "protocol.invalid_params");
    assert.equal(canonicalExpansion.status, "failed");
    assert.equal(canonicalExpansion.error.code, "protocol.invalid_params");
});

test("Interview Catalog finds exact source identity and bounded semantic candidates without merging events", async () => {
    const { InterviewCatalogAdapter } = loadModule();
    const fingerprint = `sha256:${"1".repeat(64)}`;
    const vault = vaultFrom({
        "experiences/acme-alice.md": experience(
            "exp_alice", "https://example.com/post/7?utm_source=feed", fingerprint, "alice", "2026-06-01",
        ),
        "experiences/acme-bob.md": experience(
            "exp_bob", "https://example.com/post/8", `sha256:${"2".repeat(64)}`, "bob", "unknown",
        ),
        "interview/event-loop.md": question("question_event_loop", "Explain the Node.js event loop", "draft", 3),
        "interview/sql-index.md": question("question_sql_index", "How does a SQL index work?"),
        "memory/study/not-catalog.md": question("question_hidden", "Event loop"),
    });
    const result = await new InterviewCatalogAdapter(vault).execute(call({
        sourceUrls: ["https://EXAMPLE.com/post/7?utm_source=repost#comments"],
        company: "Acme",
        role: "Backend Engineer",
        questionTerms: ["Node event loop"],
    }));

    assert.equal(result.status, "succeeded");
    assert.equal(result.data.experienceCandidates.length, 2);
    assert.equal(result.data.experienceCandidates[0].experienceId, "exp_alice");
    assert.equal(result.data.experienceCandidates[0].exactSourceMatch, true);
    assert.deepEqual(result.data.experienceCandidates.map((item) => item.candidate).sort(), ["alice", "bob"]);
    assert.equal(result.data.experienceCandidates.find((item) => item.experienceId === "exp_bob").eventDate, "unknown");
    assert.deepEqual(result.data.questionCandidates.map((item) => item.questionId), ["question_event_loop"]);
    assert.equal(result.data.questionCandidates[0].answerState, "draft");
    assert.equal(result.data.questionCandidates[0].frequency, 3);
    assert.match(result.data.questionCandidates[0].contentHash, /^sha256:[0-9a-f]{64}$/);
    assert.equal(result.data.truncated, false);
});

test("Interview Catalog omits absent optional metadata from JSON-RPC results", async () => {
    const { InterviewCatalogAdapter } = loadModule();
    const firstImage = `sha256:${"a".repeat(64)}`;
    const secondImage = `sha256:${"b".repeat(64)}`;
    const fingerprint = "sha256:0cd65e3ab3208489c6bfaa9d2ce2cb125ae8fbef34c9a965c1efe3f7d2302c37";
    const vault = vaultFrom({
        "experiences/ordered-images.md": `---
type: interview-experience
experience-id: exp_ordered_images
source-kind: ordered_images
source-fingerprint: ${fingerprint}
company: Acme
role: Backend Engineer
event-date: unknown
round: technical-1
---
# Ordered image interview
`,
    });

    const result = await new InterviewCatalogAdapter(vault).execute(call({
        orderedImageContentHashes: [firstImage, secondImage],
        questionTerms: [],
    }));

    assert.equal(result.status, "succeeded");
    const candidate = result.data.experienceCandidates[0];
    assert.equal(candidate.exactSourceMatch, true);
    assert.equal(Object.hasOwn(candidate, "sourceUrl"), false);
    assert.equal(Object.hasOwn(candidate, "candidate"), false);
    assert.equal(Object.values(candidate).includes(undefined), false);
});

test("Interview Catalog rejects stale discovery and marks over-capacity question candidates truncated", async () => {
    const { InterviewCatalogAdapter } = loadModule();
    const entries = Object.fromEntries(Array.from({ length: 101 }, (_, index) => [
        `interview/q-${index}.md`, question(`question_${index}`, `Distributed systems topic ${index}`),
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

test("Interview Catalog keeps the nested fused layout discoverable during upgrade", async () => {
    const { InterviewCatalogAdapter } = loadModule();
    const fingerprint = `sha256:${"4".repeat(64)}`;
    const vault = vaultFrom({
        "interviews/experiences/legacy.md": experience(
            "exp_legacy", "https://example.com/legacy", fingerprint, "legacy", "2026-05-01",
        ),
        "interviews/questions/legacy.md": question("question_legacy", "Legacy queue backpressure"),
    });

    const result = await new InterviewCatalogAdapter(vault).execute(call({
        sourceUrls: ["https://example.com/legacy"],
        questionTerms: ["queue backpressure"],
    }));

    assert.equal(result.status, "succeeded");
    assert.deepEqual(result.data.experienceCandidates.map((item) => item.experienceId), ["exp_legacy"]);
    assert.deepEqual(result.data.questionCandidates.map((item) => item.questionId), ["question_legacy"]);
    assert.deepEqual(result.data.indexes, [
        { kind: "experience", path: "experiences/index.md", exists: false, modifiedVersion: "missing" },
        { kind: "question", path: "interview/index.md", exists: false, modifiedVersion: "missing" },
    ]);
});
