const assert = require("node:assert/strict");
const { createHash } = require("node:crypto");
const { execFile } = require("node:child_process");
const { mkdir, mkdtemp, readFile, rm, writeFile } = require("node:fs/promises");
const os = require("node:os");
const path = require("node:path");
const { promisify } = require("node:util");
const test = require("node:test");
const { buildSync } = require("esbuild");

const execFileAsync = promisify(execFile);

function loadModule() {
    const output = buildSync({
        entryPoints: [path.join(__dirname, "../src/runtime/vault_changes.ts")],
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

function digest(content) {
    return `sha256:${createHash("sha256").update(content, "utf8").digest("hex")}`;
}

function initialModifiedVersion(position, content) {
    return `mtime:${position}:size:${Buffer.byteLength(content)}`;
}

function memoryTopic(type = "user") {
    return [
        "---",
        "name: Old preference",
        "description: A superseded durable preference.",
        `type: ${type}`,
        "---",
        "old memory",
        "",
    ].join("\n");
}

function call(batchId, operations, argumentOverrides = {}, overrides = {}) {
    const arguments_ = {
        batchId,
        task: `Apply ${batchId}`,
        changeKind: "general",
        sourceBindings: [],
        interviewSubmission: null,
        operations,
        ...argumentOverrides,
    };
    return {
        toolCallId: `call_${batchId}`,
        workspaceId: "ws_vault",
        runId: "run_changes",
        name: "vault.changes.apply",
        version: "1",
        arguments: arguments_,
        argsHash: digest(JSON.stringify(arguments_)),
        idempotencyKey: `idem_${batchId}`,
        risk: "write",
        reason: null,
        agentLineage: ["run_changes"],
        executorLocation: "plugin",
        definitionFingerprint: `sha256:${"b".repeat(64)}`,
        resultSensitivity: "workspace",
        deadline: null,
        ...overrides,
    };
}

function approve(proposal) {
    return { decision: "accept", reviewHash: proposal.reviewHash };
}

function reject(proposal) {
    return { decision: "reject", reviewHash: proposal.reviewHash };
}

class MemoryVault {
    constructor(entries) {
        this.entries = new Map(Object.entries(entries));
        this.versions = new Map([...this.entries].map(([target, content], index) => [
            target, `mtime:${index + 1}:size:${Buffer.byteLength(content)}`,
        ]));
        this.failPath = null;
        this.writeSequence = 0;
    }
    async read(target) { return this.entries.has(target) ? this.entries.get(target) : undefined; }
    async snapshot(target) {
        return this.entries.has(target)
            ? { content: this.entries.get(target), modifiedVersion: this.versions.get(target) }
            : { content: undefined, modifiedVersion: "missing" };
    }
    async applyConditional(mutation) {
        const snapshot = await this.snapshot(mutation.path);
        const observed = {
            contentHash: snapshot.content === undefined ? "absent" : digest(snapshot.content),
            modifiedVersion: snapshot.modifiedVersion,
        };
        if (observed.contentHash !== mutation.expected.contentHash ||
            observed.modifiedVersion !== mutation.expected.modifiedVersion) {
            return { status: "conflict", observed };
        }
        if (mutation.kind === "create") await this.create(mutation.path, mutation.afterContent);
        else if (mutation.kind === "modify") await this.modify(mutation.path, mutation.afterContent);
        else await this.remove(mutation.path);
        const applied = await this.snapshot(mutation.path);
        return {
            status: "applied",
            applied: {
                contentHash: applied.content === undefined ? "absent" : digest(applied.content),
                modifiedVersion: applied.modifiedVersion,
            },
        };
    }
    async create(target, content) {
        if (this.entries.has(target)) throw new Error(`create target already exists: ${target}`);
        await this.write(target, content);
    }
    async modify(target, content) {
        if (!this.entries.has(target)) throw new Error(`modify target is missing: ${target}`);
        await this.write(target, content);
    }
    async write(target, content) {
        if (target === this.failPath) throw new Error(`injected write failure: ${target}`);
        this.entries.set(target, content);
        this.versions.set(target, `mtime:write:${++this.writeSequence}:size:${Buffer.byteLength(content)}`);
    }
    async remove(target) { this.entries.delete(target); this.versions.delete(target); }
}

class MemoryJournal {
    constructor() { this.records = new Map(); }
    async load(batchId) { return structuredClone(this.records.get(batchId)); }
    async save(record) { this.records.set(record.batchId, structuredClone(record)); }
    async findInterviewSubmissionByRootRun(rootRunId) {
        const record = [...this.records.values()].find((candidate) =>
            candidate.version === 2 && candidate.changeKind === "interview_submission" &&
            candidate.rootRunId === rootRunId,
        );
        return structuredClone(record);
    }
    async listUnresolved() {
        return [...this.records.values()].filter((record) => ["prepared", "applying", "undoing", "recovery_failed"].includes(record.state))
            .map((record) => structuredClone(record));
    }
}

class MemoryCheckpoints {
    constructor(vault) { this.vault = vault; this.snapshots = new Map(); }
    async create(batchId, paths) {
        const ref = `refs/offeragent/checkpoints/${batchId}`;
        this.snapshots.set(ref, new Map(paths.map((target) => [target, this.vault.entries.get(target)])));
        return ref;
    }
    async read(ref, target) { return this.snapshots.get(ref)?.get(target); }
}

function interviewFixture() {
    const sourcePath = "interview/catalog-candidate.md";
    const sourceContent = "---\ntype: interview-question\nquestion-id: catalog_candidate\n---\n# Candidate\n";
    const experienceIndexPath = "experiences/index.md";
    const experienceIndexContent = "# Experiences\n";
    const questionIndexPath = "interview/index.md";
    const questionIndexContent = "# Questions\n";
    const sourceFingerprint = "sha256:ba16ab9e53946934b5cb4e3f89e3977c905d09c1960ec45a03867f57a8411545";
    const imageHashes = [`sha256:${"1".repeat(64)}`, `sha256:${"2".repeat(64)}`];
    const experiencePath = "experiences/acme-backend-2026-07-18.md";
    const questionPath = "interview/database-isolation.md";
    const experienceContent = [
        "---",
        "type: interview-experience",
        "experience-id: exp_acme_backend_20260718",
        "source-kind: mixed",
        "captured-on: 2026-07-18",
        "source-url: https://example.com/interview/42",
        `source-fingerprint: ${sourceFingerprint}`,
        "company: Acme",
        "role: Backend Engineer",
        "event-date: unknown",
        "round: unknown",
        "---",
        "# Acme Backend Interview",
        "",
        "## Questions",
        "- [[../interview/database-isolation]]",
        "",
    ].join("\n");
    const questionContent = [
        "---",
        "type: interview-question",
        "question-id: question_database_isolation",
        "title: Explain database isolation",
        "answer-state: needs-research",
        "frequency: 1",
        "---",
        "# Explain database isolation",
        "",
        "## Occurrences",
        "- [[../experiences/acme-backend-2026-07-18]]",
        "",
    ].join("\n");
    const entries = {
        [sourcePath]: sourceContent,
        [experienceIndexPath]: experienceIndexContent,
        [questionIndexPath]: questionIndexContent,
    };
    const operations = [
        {
            op: "create", path: experiencePath, content: experienceContent,
            expectedContentHash: "absent", expectedModifiedVersion: "missing",
        },
        {
            op: "create", path: questionPath, content: questionContent,
            expectedContentHash: "absent", expectedModifiedVersion: "missing",
        },
        {
            op: "append", path: experienceIndexPath,
            content: "- [[acme-backend-2026-07-18]]\n",
            expectedContentHash: digest(experienceIndexContent),
            expectedModifiedVersion: `mtime:2:size:${Buffer.byteLength(experienceIndexContent)}`,
        },
        {
            op: "append", path: questionIndexPath,
            content: "- [[database-isolation]]\n",
            expectedContentHash: digest(questionIndexContent),
            expectedModifiedVersion: `mtime:3:size:${Buffer.byteLength(questionIndexContent)}`,
        },
    ];
    return {
        entries,
        operations,
        argumentOverrides: {
            changeKind: "interview_submission",
            sourceBindings: [{
                path: sourcePath,
                expectedModifiedVersion: `mtime:1:size:${Buffer.byteLength(sourceContent)}`,
                expectedContentHash: digest(sourceContent),
            }],
            interviewSubmission: {
                sourceKind: "mixed",
                capturedOn: "2026-07-18",
                canonicalUrls: ["https://example.com/interview/42"],
                orderedImageContentHashes: imageHashes,
                sourceFingerprint,
                reviewItems: [
                    {
                        kind: "experience", path: experiencePath,
                        identity: "new", mutation: "create",
                    },
                    {
                        kind: "question", path: questionPath,
                        identity: "new", mutation: "create",
                    },
                    {
                        kind: "index", path: experienceIndexPath,
                        identity: "existing", mutation: "modify",
                    },
                    {
                        kind: "index", path: questionIndexPath,
                        identity: "existing", mutation: "modify",
                    },
                ],
            },
        },
        experiencePath,
        questionPath,
        experienceIndexPath,
        questionIndexPath,
    };
}

test("structured Interview review badges distinguish new, merge, modify, and no-op outcomes", () => {
    const { interviewReviewBadges } = loadModule();

    assert.deepEqual(interviewReviewBadges({
        kind: "experience", path: "experiences/new.md", identity: "new", mutation: "create",
    }), ["身份 · 新增", "新增"]);
    assert.deepEqual(interviewReviewBadges({
        kind: "experience", path: "experiences/existing.md", identity: "existing", mutation: "modify",
    }), ["身份 · 既有", "合并"]);
    assert.deepEqual(interviewReviewBadges({
        kind: "question", path: "interview/existing.md", identity: "existing", mutation: "modify",
    }), ["身份 · 既有", "修改"]);
    assert.deepEqual(interviewReviewBadges({
        kind: "question", path: "interview/existing.md", identity: "existing", mutation: "none",
    }), ["身份 · 既有", "无操作"]);
});

test("trusted Vault previews and confirms one categorized Interview Submission batch", async () => {
    const { VaultChangeCoordinator } = loadModule();
    const fixture = interviewFixture();
    const vault = new MemoryVault(fixture.entries);
    const journal = new MemoryJournal();
    let proposal;
    let approvals = 0;
    const coordinator = new VaultChangeCoordinator({
        vault,
        journal,
        checkpoints: new MemoryCheckpoints(vault),
        permissionMode: () => "trusted_vault",
        authorize: async (candidate) => {
            approvals += 1;
            proposal = candidate;
            return approve(candidate);
        },
    });
    const request = call("batch_interview_preview", fixture.operations, fixture.argumentOverrides);

    const result = await coordinator.execute(request);

    assert.equal(result.status, "succeeded");
    assert.equal(approvals, 1);
    assert.equal(proposal.batchId, "batch_interview_preview");
    assert.equal(proposal.argsHash, request.argsHash);
    assert.equal(proposal.changeKind, "interview_submission");
    assert.deepEqual(proposal.categorizedTargets, [
        {
            path: fixture.experiencePath, category: "experience", operation: "create",
            expectedModifiedVersion: "missing", expectedContentHash: "absent",
        },
        {
            path: fixture.questionPath, category: "question", operation: "create",
            expectedModifiedVersion: "missing", expectedContentHash: "absent",
        },
        {
            path: fixture.experienceIndexPath, category: "index", operation: "append",
            expectedModifiedVersion: fixture.operations[2].expectedModifiedVersion,
            expectedContentHash: fixture.operations[2].expectedContentHash,
        },
        {
            path: fixture.questionIndexPath, category: "index", operation: "append",
            expectedModifiedVersion: fixture.operations[3].expectedModifiedVersion,
            expectedContentHash: fixture.operations[3].expectedContentHash,
        },
    ]);
    assert.deepEqual(proposal.sourceBindings, fixture.argumentOverrides.sourceBindings);
    assert.deepEqual(
        proposal.interviewSubmission.reviewItems,
        fixture.argumentOverrides.interviewSubmission.reviewItems,
    );
    assert.deepEqual(proposal.reviewTargets.map((target) => target.path), [
        fixture.experiencePath,
        fixture.questionPath,
        fixture.experienceIndexPath,
        fixture.questionIndexPath,
    ]);
    assert.equal(proposal.reviewTargets[0].afterContent, fixture.operations[0].content);
    assert.match(proposal.reviewHash, /^sha256:[0-9a-f]{64}$/u);
    assert.equal(vault.entries.get(fixture.experiencePath), fixture.operations[0].content);
    assert.equal(vault.entries.get(fixture.questionPath), fixture.operations[1].content);
    assert.match(vault.entries.get(fixture.experienceIndexPath), /acme-backend/u);
    assert.match(vault.entries.get(fixture.questionIndexPath), /database-isolation/u);
});

test("result state hashes order Unicode paths by code point across runtimes", async () => {
    const { VaultChangeCoordinator } = loadModule();
    const vault = new MemoryVault({});
    const coordinator = new VaultChangeCoordinator({
        vault,
        journal: new MemoryJournal(),
        checkpoints: new MemoryCheckpoints(vault),
        permissionMode: () => "trusted_vault",
    });
    const operations = [
        {
            op: "create", path: "notes/ä.md", content: "umlaut\n",
            expectedContentHash: "absent", expectedModifiedVersion: "missing",
        },
        {
            op: "create", path: "notes/z.md", content: "zed\n",
            expectedContentHash: "absent", expectedModifiedVersion: "missing",
        },
    ];

    const result = await coordinator.execute(call("batch_unicode_state_hash", operations));

    assert.equal(result.status, "succeeded");
    assert.equal(result.data.beforeStateHash, digest("notes/z.md\0absent\nnotes/ä.md\0absent"));
    assert.equal(
        result.data.afterStateHash,
        digest(`notes/z.md\0${digest("zed\n")}\nnotes/ä.md\0${digest("umlaut\n")}`),
    );
});

test("Interview Submission review exposes every byte beyond 100 lines and 32 KiB", async () => {
    const { VaultChangeCoordinator } = loadModule();
    const fixture = interviewFixture();
    const tail = [
        ...Array.from({ length: 220 }, (_unused, index) => `review-line-${index}-${"x".repeat(180)}`),
        "COMPLETE-REVIEW-TAIL-SENTINEL",
        "",
    ].join("\n");
    const longBefore = [
        "# Experiences",
        ...Array.from({ length: 190 }, (_unused, index) => `existing-line-${index}-${"y".repeat(180)}`),
        "COMPLETE-BEFORE-TAIL-SENTINEL",
        "",
    ].join("\n");
    const entries = { ...fixture.entries, [fixture.experienceIndexPath]: longBefore };
    const operations = fixture.operations.map((operation, index) => {
        if (index === 0) return { ...operation, content: `${operation.content}${tail}` };
        if (index === 2) {
            return {
                ...operation,
                expectedContentHash: digest(longBefore),
                expectedModifiedVersion: initialModifiedVersion(2, longBefore),
            };
        }
        return operation;
    });
    const vault = new MemoryVault(entries);
    let proposal;
    const coordinator = new VaultChangeCoordinator({
        vault,
        journal: new MemoryJournal(),
        checkpoints: new MemoryCheckpoints(vault),
        permissionMode: () => "trusted_vault",
        authorize: async (candidate) => {
            proposal = candidate;
            return reject(candidate);
        },
    });

    const result = await coordinator.execute(call(
        "batch_interview_complete_review", operations, fixture.argumentOverrides,
    ));

    assert.equal(result.status, "denied");
    const experience = proposal.reviewTargets.find((target) => target.path === fixture.experiencePath);
    assert.equal(experience.beforeContent, null);
    assert.equal(experience.afterContent, operations[0].content);
    assert.ok(Buffer.byteLength(experience.afterContent, "utf8") > 32_768);
    assert.match(experience.afterContent, /COMPLETE-REVIEW-TAIL-SENTINEL/u);
    const experienceIndex = proposal.reviewTargets.find((target) => target.path === fixture.experienceIndexPath);
    assert.equal(experienceIndex.beforeContent, longBefore);
    assert.equal(experienceIndex.afterContent, `${longBefore}${operations[2].content}`);
    assert.ok(Buffer.byteLength(experienceIndex.beforeContent, "utf8") > 32_768);
    assert.match(experienceIndex.beforeContent, /COMPLETE-BEFORE-TAIL-SENTINEL/u);
});

test("authorization cannot accept a different review hash", async () => {
    const { VaultChangeCoordinator } = loadModule();
    const fixture = interviewFixture();
    const vault = new MemoryVault(fixture.entries);
    const coordinator = new VaultChangeCoordinator({
        vault,
        journal: new MemoryJournal(),
        checkpoints: new MemoryCheckpoints(vault),
        permissionMode: () => "trusted_vault",
        authorize: async () => ({ decision: "accept", reviewHash: digest("different review") }),
    });

    const result = await coordinator.execute(call(
        "batch_interview_review_hash_mismatch", fixture.operations, fixture.argumentOverrides,
    ));

    assert.equal(result.status, "failed");
    assert.equal(result.error.code, "resource.conflict");
    assert.deepEqual(Object.fromEntries(vault.entries), fixture.entries);
});

test("a rejected Interview Submission durably consumes the root Run proposal slot", async () => {
    const { VaultChangeCoordinator } = loadModule();
    const fixture = interviewFixture();
    const vault = new MemoryVault(fixture.entries);
    const journal = new MemoryJournal();
    let reviews = 0;
    const coordinator = new VaultChangeCoordinator({
        vault,
        journal,
        checkpoints: new MemoryCheckpoints(vault),
        permissionMode: () => "trusted_vault",
        authorize: async (proposal) => {
            reviews += 1;
            return reject(proposal);
        },
    });
    const rootRun = ["run_interview_root", "run_interview_child"];
    const first = call(
        "batch_interview_root_first",
        fixture.operations,
        fixture.argumentOverrides,
        { runId: rootRun[1], agentLineage: rootRun },
    );
    const second = call(
        "batch_interview_root_second",
        fixture.operations,
        fixture.argumentOverrides,
        { runId: "run_interview_sibling", agentLineage: [rootRun[0], "run_interview_sibling"] },
    );

    const rejected = await coordinator.execute(first);
    const replay = await coordinator.execute(first);
    const duplicate = await coordinator.execute(second);

    assert.equal(rejected.status, "denied");
    assert.equal(replay.status, "denied");
    assert.equal(duplicate.status, "failed");
    assert.equal(duplicate.error.code, "resource.conflict");
    assert.equal(reviews, 1);
    assert.equal((await journal.load(first.arguments.batchId)).state, "rejected");
    assert.equal(await journal.load(second.arguments.batchId), undefined);
    assert.deepEqual(Object.fromEntries(vault.entries), fixture.entries);
});

test("Interview Submission accepts the Catalog's deterministic code-point URL ordering", async () => {
    const { VaultChangeCoordinator } = loadModule();
    const fixture = interviewFixture();
    const canonicalUrl = "https://example.com/interview/42?Z=1&a=2";
    const operations = fixture.operations.map((operation, index) => index === 0
        ? { ...operation, content: operation.content.replace("https://example.com/interview/42", canonicalUrl) }
        : operation);
    const argumentOverrides = {
        ...fixture.argumentOverrides,
        interviewSubmission: {
            ...fixture.argumentOverrides.interviewSubmission,
            canonicalUrls: [canonicalUrl],
        },
    };
    const vault = new MemoryVault(fixture.entries);
    const coordinator = new VaultChangeCoordinator({
        vault,
        journal: new MemoryJournal(),
        checkpoints: new MemoryCheckpoints(vault),
        permissionMode: () => "trusted_vault",
        authorize: async (proposal) => approve(proposal),
    });

    const result = await coordinator.execute(call(
        "batch_interview_canonical_query", operations, argumentOverrides,
    ));

    assert.equal(result.status, "succeeded");
});

test("Interview Submission accepts the Catalog's canonical public IPv6 URL", async () => {
    const { VaultChangeCoordinator } = loadModule();
    const fixture = interviewFixture();
    const canonicalUrl = "https://[2001:4860:4860::8888]/interview/42";
    const operations = fixture.operations.map((operation, index) => index === 0
        ? { ...operation, content: operation.content.replace("https://example.com/interview/42", canonicalUrl) }
        : operation);
    const argumentOverrides = {
        ...fixture.argumentOverrides,
        interviewSubmission: {
            ...fixture.argumentOverrides.interviewSubmission,
            canonicalUrls: [canonicalUrl],
        },
    };
    const vault = new MemoryVault(fixture.entries);
    const coordinator = new VaultChangeCoordinator({
        vault,
        journal: new MemoryJournal(),
        checkpoints: new MemoryCheckpoints(vault),
        permissionMode: () => "trusted_vault",
        authorize: async (proposal) => approve(proposal),
    });

    const result = await coordinator.execute(call(
        "batch_interview_canonical_ipv6", operations, argumentOverrides,
    ));

    assert.equal(result.status, "succeeded");
});

test("a distinct Experience adds one existing Question occurrence without rewriting its unchanged index", async () => {
    const { VaultChangeCoordinator } = loadModule();
    const fixture = interviewFixture();
    const existingQuestionContent = fixture.operations[1].content.replace(
        "acme-backend-2026-07-18",
        "older-experience",
    );
    const entries = {
        ...fixture.entries,
        [fixture.questionPath]: existingQuestionContent,
    };
    const operations = [
        fixture.operations[0],
        {
            op: "patch",
            path: fixture.questionPath,
            edits: [
                { startLine: 6, endLine: 6, replacement: "frequency: 2" },
                {
                    startLine: 11,
                    endLine: 11,
                    replacement: [
                        "- [[../experiences/older-experience]]",
                        "- [[../experiences/acme-backend-2026-07-18]]",
                    ].join("\n"),
                },
            ],
            expectedContentHash: digest(existingQuestionContent),
            expectedModifiedVersion: initialModifiedVersion(4, existingQuestionContent),
        },
        fixture.operations[2],
    ];
    const argumentOverrides = {
        ...fixture.argumentOverrides,
        sourceBindings: [
            ...fixture.argumentOverrides.sourceBindings,
            {
                path: fixture.questionPath,
                expectedContentHash: digest(existingQuestionContent),
                expectedModifiedVersion: initialModifiedVersion(4, existingQuestionContent),
            },
        ],
        interviewSubmission: {
            ...fixture.argumentOverrides.interviewSubmission,
            reviewItems: [
                {
                    kind: "experience", path: fixture.experiencePath,
                    identity: "new", mutation: "create",
                },
                {
                    kind: "question", path: fixture.questionPath,
                    identity: "existing", mutation: "modify",
                },
                {
                    kind: "index", path: fixture.experienceIndexPath,
                    identity: "existing", mutation: "modify",
                },
            ],
        },
    };
    let proposal;
    const vault = new MemoryVault(entries);
    const coordinator = new VaultChangeCoordinator({
        vault,
        journal: new MemoryJournal(),
        checkpoints: new MemoryCheckpoints(vault),
        permissionMode: () => "trusted_vault",
        authorize: async (candidate) => {
            proposal = candidate;
            return approve(candidate);
        },
    });

    const result = await coordinator.execute(call(
        "batch_interview_existing_question", operations, argumentOverrides,
    ));

    assert.equal(result.status, "succeeded");
    assert.match(vault.entries.get(fixture.questionPath), /frequency: 2/u);
    assert.equal(
        vault.entries.get(fixture.questionPath).match(/acme-backend-2026-07-18/gu)?.length,
        1,
    );
    assert.equal(vault.entries.get(fixture.questionIndexPath), fixture.entries[fixture.questionIndexPath]);
    assert.deepEqual(
        proposal.interviewSubmission.reviewItems,
        argumentOverrides.interviewSubmission.reviewItems,
    );
});

test("the same source event merges one existing Experience while its linked Question and indexes remain no-ops", async () => {
    const { VaultChangeCoordinator } = loadModule();
    const fixture = interviewFixture();
    const existingExperienceContent = fixture.operations[0].content;
    const existingQuestionContent = fixture.operations[1].content;
    const entries = {
        ...fixture.entries,
        [fixture.experiencePath]: existingExperienceContent,
        [fixture.questionPath]: existingQuestionContent,
    };
    const experienceVersion = initialModifiedVersion(4, existingExperienceContent);
    const questionVersion = initialModifiedVersion(5, existingQuestionContent);
    const operations = [{
        op: "append",
        path: fixture.experiencePath,
        content: "\n## Source-supported detail\n- The interviewer asked for a concrete trade-off.\n",
        expectedContentHash: digest(existingExperienceContent),
        expectedModifiedVersion: experienceVersion,
    }];
    const argumentOverrides = {
        ...fixture.argumentOverrides,
        sourceBindings: [
            ...fixture.argumentOverrides.sourceBindings,
            {
                path: fixture.experiencePath,
                expectedContentHash: digest(existingExperienceContent),
                expectedModifiedVersion: experienceVersion,
            },
            {
                path: fixture.questionPath,
                expectedContentHash: digest(existingQuestionContent),
                expectedModifiedVersion: questionVersion,
            },
        ],
        interviewSubmission: {
            ...fixture.argumentOverrides.interviewSubmission,
            reviewItems: [
                {
                    kind: "experience", path: fixture.experiencePath,
                    identity: "existing", mutation: "modify",
                },
                {
                    kind: "question", path: fixture.questionPath,
                    identity: "existing", mutation: "none",
                },
            ],
        },
    };
    let proposal;
    const vault = new MemoryVault(entries);
    const coordinator = new VaultChangeCoordinator({
        vault,
        journal: new MemoryJournal(),
        checkpoints: new MemoryCheckpoints(vault),
        permissionMode: () => "trusted_vault",
        authorize: async (candidate) => {
            proposal = candidate;
            return approve(candidate);
        },
    });

    const result = await coordinator.execute(call(
        "batch_interview_same_event_merge", operations, argumentOverrides,
    ));

    assert.equal(result.status, "succeeded");
    assert.match(vault.entries.get(fixture.experiencePath), /concrete trade-off/u);
    assert.equal(vault.entries.get(fixture.questionPath), existingQuestionContent);
    assert.equal(vault.entries.get(fixture.experienceIndexPath), fixture.entries[fixture.experienceIndexPath]);
    assert.equal(vault.entries.get(fixture.questionIndexPath), fixture.entries[fixture.questionIndexPath]);
    assert.deepEqual(
        proposal.interviewSubmission.reviewItems,
        argumentOverrides.interviewSubmission.reviewItems,
    );
});

test("a no-op Question changed during the final merge check rolls back without overwriting the user edit", async () => {
    const { VaultChangeCoordinator } = loadModule();
    const fixture = interviewFixture();
    const existingExperienceContent = fixture.operations[0].content;
    const existingQuestionContent = fixture.operations[1].content;
    const entries = {
        ...fixture.entries,
        [fixture.experiencePath]: existingExperienceContent,
        [fixture.questionPath]: existingQuestionContent,
    };
    const experienceVersion = initialModifiedVersion(4, existingExperienceContent);
    const questionVersion = initialModifiedVersion(5, existingQuestionContent);
    const operations = [{
        op: "append",
        path: fixture.experiencePath,
        content: "\n## Source-supported detail\n- Merge this detail.\n",
        expectedContentHash: digest(existingExperienceContent),
        expectedModifiedVersion: experienceVersion,
    }];
    const argumentOverrides = {
        ...fixture.argumentOverrides,
        sourceBindings: [
            ...fixture.argumentOverrides.sourceBindings,
            {
                path: fixture.experiencePath,
                expectedContentHash: digest(existingExperienceContent),
                expectedModifiedVersion: experienceVersion,
            },
            {
                path: fixture.questionPath,
                expectedContentHash: digest(existingQuestionContent),
                expectedModifiedVersion: questionVersion,
            },
        ],
        interviewSubmission: {
            ...fixture.argumentOverrides.interviewSubmission,
            reviewItems: [
                {
                    kind: "experience", path: fixture.experiencePath,
                    identity: "existing", mutation: "modify",
                },
                {
                    kind: "question", path: fixture.questionPath,
                    identity: "existing", mutation: "none",
                },
            ],
        },
    };
    const vault = new MemoryVault(entries);
    const originalApplyConditional = vault.applyConditional.bind(vault);
    let raced = false;
    vault.applyConditional = async (mutation) => {
        const outcome = await originalApplyConditional(mutation);
        if (!raced && mutation.path === fixture.experiencePath && outcome.status === "applied") {
            raced = true;
            await vault.write(fixture.questionPath, "user edit during Experience merge\n");
        }
        return outcome;
    };
    const journal = new MemoryJournal();
    const coordinator = new VaultChangeCoordinator({
        vault,
        journal,
        checkpoints: new MemoryCheckpoints(vault),
        permissionMode: () => "trusted_vault",
        authorize: async (proposal) => approve(proposal),
    });

    const result = await coordinator.execute(call(
        "batch_interview_noop_source_race", operations, argumentOverrides,
    ));

    assert.equal(result.status, "failed");
    assert.equal(result.error.code, "resource.conflict");
    assert.equal(vault.entries.get(fixture.experiencePath), existingExperienceContent);
    assert.equal(vault.entries.get(fixture.questionPath), "user edit during Experience merge\n");
    assert.equal((await journal.load("batch_interview_noop_source_race")).state, "rolled_back");
});

test("an existing Experience merge cannot replace its Catalog identity", async () => {
    const { VaultChangeCoordinator } = loadModule();
    const fixture = interviewFixture();
    const existingExperienceContent = fixture.operations[0].content;
    const existingQuestionContent = fixture.operations[1].content;
    const entries = {
        ...fixture.entries,
        [fixture.experiencePath]: existingExperienceContent,
        [fixture.questionPath]: existingQuestionContent,
    };
    const experienceVersion = initialModifiedVersion(4, existingExperienceContent);
    const questionVersion = initialModifiedVersion(5, existingQuestionContent);
    const operations = [{
        op: "replace",
        path: fixture.experiencePath,
        find: "experience-id: exp_acme_backend_20260718",
        replacement: "experience-id: exp_replaced_identity",
        expectedContentHash: digest(existingExperienceContent),
        expectedModifiedVersion: experienceVersion,
    }];
    const argumentOverrides = {
        ...fixture.argumentOverrides,
        sourceBindings: [
            ...fixture.argumentOverrides.sourceBindings,
            {
                path: fixture.questionPath,
                expectedContentHash: digest(existingQuestionContent),
                expectedModifiedVersion: questionVersion,
            },
        ],
        interviewSubmission: {
            ...fixture.argumentOverrides.interviewSubmission,
            reviewItems: [
                {
                    kind: "experience", path: fixture.experiencePath,
                    identity: "existing", mutation: "modify",
                },
                {
                    kind: "question", path: fixture.questionPath,
                    identity: "existing", mutation: "none",
                },
            ],
        },
    };
    const vault = new MemoryVault(entries);
    let approvals = 0;
    const coordinator = new VaultChangeCoordinator({
        vault,
        journal: new MemoryJournal(),
        checkpoints: new MemoryCheckpoints(vault),
        permissionMode: () => "trusted_vault",
        authorize: async (proposal) => {
            approvals += 1;
            return approve(proposal);
        },
    });

    const result = await coordinator.execute(call(
        "batch_interview_existing_identity_drift", operations, argumentOverrides,
    ));

    assert.equal(result.status, "failed");
    assert.equal(result.error.code, "protocol.invalid_params");
    assert.equal(approvals, 0);
    assert.equal(vault.entries.get(fixture.experiencePath), existingExperienceContent);
});

test("an existing Experience merge cannot replace its canonical source identity", async () => {
    const { VaultChangeCoordinator } = loadModule();
    const fixture = interviewFixture();
    const existingExperienceContent = fixture.operations[0].content;
    const existingQuestionContent = fixture.operations[1].content;
    const entries = {
        ...fixture.entries,
        [fixture.experiencePath]: existingExperienceContent,
        [fixture.questionPath]: existingQuestionContent,
    };
    const experienceVersion = initialModifiedVersion(4, existingExperienceContent);
    const questionVersion = initialModifiedVersion(5, existingQuestionContent);
    const operations = [{
        op: "replace",
        path: fixture.experiencePath,
        find: "source-url: https://example.com/interview/42",
        replacement: "source-url: http://127.0.0.1/private",
        expectedContentHash: digest(existingExperienceContent),
        expectedModifiedVersion: experienceVersion,
    }];
    const argumentOverrides = {
        ...fixture.argumentOverrides,
        sourceBindings: [
            ...fixture.argumentOverrides.sourceBindings,
            {
                path: fixture.questionPath,
                expectedContentHash: digest(existingQuestionContent),
                expectedModifiedVersion: questionVersion,
            },
        ],
        interviewSubmission: {
            ...fixture.argumentOverrides.interviewSubmission,
            reviewItems: [
                {
                    kind: "experience", path: fixture.experiencePath,
                    identity: "existing", mutation: "modify",
                },
                {
                    kind: "question", path: fixture.questionPath,
                    identity: "existing", mutation: "none",
                },
            ],
        },
    };
    const vault = new MemoryVault(entries);
    let approvals = 0;
    const coordinator = new VaultChangeCoordinator({
        vault,
        journal: new MemoryJournal(),
        checkpoints: new MemoryCheckpoints(vault),
        permissionMode: () => "trusted_vault",
        authorize: async (proposal) => {
            approvals += 1;
            return approve(proposal);
        },
    });

    const result = await coordinator.execute(call(
        "batch_interview_existing_source_identity_drift", operations, argumentOverrides,
    ));

    assert.equal(result.status, "failed");
    assert.equal(result.error.code, "protocol.invalid_params");
    assert.equal(approvals, 0);
    assert.equal(vault.entries.get(fixture.experiencePath), existingExperienceContent);
});

test("an existing Question occurrence update cannot replace its learning-unit identity", async () => {
    const { VaultChangeCoordinator } = loadModule();
    const fixture = interviewFixture();
    const existingQuestionContent = fixture.operations[1].content.replace(
        "acme-backend-2026-07-18",
        "older-experience",
    );
    const entries = {
        ...fixture.entries,
        [fixture.questionPath]: existingQuestionContent,
    };
    const questionVersion = initialModifiedVersion(4, existingQuestionContent);
    const operations = [
        fixture.operations[0],
        {
            op: "patch",
            path: fixture.questionPath,
            edits: [
                { startLine: 3, endLine: 3, replacement: "question-id: replaced_learning_unit" },
                { startLine: 6, endLine: 6, replacement: "frequency: 2" },
                {
                    startLine: 11,
                    endLine: 11,
                    replacement: [
                        "- [[../experiences/older-experience]]",
                        "- [[../experiences/acme-backend-2026-07-18]]",
                    ].join("\n"),
                },
            ],
            expectedContentHash: digest(existingQuestionContent),
            expectedModifiedVersion: questionVersion,
        },
        fixture.operations[2],
    ];
    const argumentOverrides = {
        ...fixture.argumentOverrides,
        sourceBindings: [
            ...fixture.argumentOverrides.sourceBindings,
            {
                path: fixture.questionPath,
                expectedContentHash: digest(existingQuestionContent),
                expectedModifiedVersion: questionVersion,
            },
        ],
        interviewSubmission: {
            ...fixture.argumentOverrides.interviewSubmission,
            reviewItems: [
                {
                    kind: "experience", path: fixture.experiencePath,
                    identity: "new", mutation: "create",
                },
                {
                    kind: "question", path: fixture.questionPath,
                    identity: "existing", mutation: "modify",
                },
                {
                    kind: "index", path: fixture.experienceIndexPath,
                    identity: "existing", mutation: "modify",
                },
            ],
        },
    };
    const vault = new MemoryVault(entries);
    let approvals = 0;
    const coordinator = new VaultChangeCoordinator({
        vault,
        journal: new MemoryJournal(),
        checkpoints: new MemoryCheckpoints(vault),
        permissionMode: () => "trusted_vault",
        authorize: async (proposal) => {
            approvals += 1;
            return approve(proposal);
        },
    });

    const result = await coordinator.execute(call(
        "batch_interview_existing_question_identity_drift", operations, argumentOverrides,
    ));

    assert.equal(result.status, "failed");
    assert.equal(result.error.code, "protocol.invalid_params");
    assert.equal(approvals, 0);
    assert.equal(vault.entries.get(fixture.questionPath), existingQuestionContent);
});

test("rejecting an Interview Submission preview leaves every Vault target unchanged", async () => {
    const { VaultChangeCoordinator } = loadModule();
    const fixture = interviewFixture();
    const vault = new MemoryVault(fixture.entries);
    const journal = new MemoryJournal();
    let approvals = 0;
    const coordinator = new VaultChangeCoordinator({
        vault,
        journal,
        checkpoints: new MemoryCheckpoints(vault),
        permissionMode: () => "trusted_vault",
        authorize: async (proposal) => { approvals += 1; return reject(proposal); },
    });

    const result = await coordinator.execute(call(
        "batch_interview_rejected", fixture.operations, fixture.argumentOverrides,
    ));

    assert.equal(result.status, "denied");
    assert.equal(approvals, 1);
    assert.deepEqual(Object.fromEntries(vault.entries), fixture.entries);
    assert.equal((await journal.load("batch_interview_rejected")).state, "rejected");
});

test("Interview Submission create never overwrites a user file raced into the final missing check", async () => {
    const { VaultChangeCoordinator } = loadModule();
    const fixture = interviewFixture();
    const vault = new MemoryVault(fixture.entries);
    const journal = new MemoryJournal();
    const originalWrite = vault.write.bind(vault);
    let raced = false;
    const raceUserCreate = () => {
        if (raced) return;
        raced = true;
        vault.entries.set(fixture.experiencePath, "user-created experience\n");
        vault.versions.set(fixture.experiencePath, "mtime:user:size:24");
    };
    vault.write = async (target, content) => {
        if (target === fixture.experiencePath) raceUserCreate();
        await originalWrite(target, content);
    };
    vault.create = async (target, content) => {
        if (target === fixture.experiencePath) raceUserCreate();
        if (vault.entries.has(target)) throw new Error(`create target already exists: ${target}`);
        await originalWrite(target, content);
    };
    const coordinator = new VaultChangeCoordinator({
        vault,
        journal,
        checkpoints: new MemoryCheckpoints(vault),
        permissionMode: () => "trusted_vault",
        authorize: async (proposal) => approve(proposal),
    });

    const result = await coordinator.execute(call(
        "batch_interview_create_race", fixture.operations, fixture.argumentOverrides,
    ));

    assert.equal(result.status, "failed");
    assert.equal(result.error.code, "resource.conflict");
    assert.equal(vault.entries.get(fixture.experiencePath), "user-created experience\n");
    assert.equal(vault.entries.has(fixture.questionPath), false);
    assert.equal(vault.entries.get(fixture.experienceIndexPath), fixture.entries[fixture.experienceIndexPath]);
    assert.equal(vault.entries.get(fixture.questionIndexPath), fixture.entries[fixture.questionIndexPath]);
    assert.equal((await journal.load("batch_interview_create_race")).state, "rolled_back");
});

test("Interview Submission preserves an identical user file raced into a create conflict", async () => {
    const { VaultChangeCoordinator } = loadModule();
    const fixture = interviewFixture();
    const vault = new MemoryVault(fixture.entries);
    const journal = new MemoryJournal();
    const originalWrite = vault.write.bind(vault);
    vault.create = async (target, content) => {
        if (target === fixture.experiencePath) {
            vault.entries.set(target, content);
            vault.versions.set(target, `mtime:user:size:${Buffer.byteLength(content)}`);
        }
        if (vault.entries.has(target)) throw new Error(`create target already exists: ${target}`);
        await originalWrite(target, content);
    };
    const coordinator = new VaultChangeCoordinator({
        vault,
        journal,
        checkpoints: new MemoryCheckpoints(vault),
        permissionMode: () => "trusted_vault",
        authorize: async (proposal) => approve(proposal),
    });

    const result = await coordinator.execute(call(
        "batch_interview_identical_create_race", fixture.operations, fixture.argumentOverrides,
    ));

    assert.equal(result.status, "unknown_outcome");
    assert.equal(result.error.code, "tool.unknown_outcome");
    assert.equal(vault.entries.get(fixture.experiencePath), fixture.operations[0].content);
    assert.equal((await journal.load("batch_interview_identical_create_race")).state, "recovery_failed");
    assert.deepEqual(
        (await journal.load("batch_interview_identical_create_race")).manualReviewPaths,
        [fixture.experiencePath],
    );
    const recovered = new VaultChangeCoordinator({
        vault,
        journal,
        checkpoints: new MemoryCheckpoints(vault),
        permissionMode: () => "trusted_vault",
    });
    assert.deepEqual(await recovered.reconcile(), [{
        batchId: "batch_interview_identical_create_race",
        state: "recovery_failed",
        manualReviewPaths: [fixture.experiencePath],
    }]);
    assert.equal(vault.entries.get(fixture.experiencePath), fixture.operations[0].content);
});

test("Interview Submission reports unknown outcome when modify writes and then rejects", async () => {
    const { VaultChangeCoordinator } = loadModule();
    const fixture = interviewFixture();
    const vault = new MemoryVault(fixture.entries);
    const journal = new MemoryJournal();
    const originalModify = vault.modify.bind(vault);
    vault.modify = async (target, content) => {
        await originalModify(target, content);
        if (target === fixture.questionIndexPath) throw new Error("modify completion was lost");
    };
    const coordinator = new VaultChangeCoordinator({
        vault,
        journal,
        checkpoints: new MemoryCheckpoints(vault),
        permissionMode: () => "trusted_vault",
        authorize: async (proposal) => approve(proposal),
    });

    const result = await coordinator.execute(call(
        "batch_interview_modify_unknown", fixture.operations, fixture.argumentOverrides,
    ));

    assert.equal(result.status, "unknown_outcome");
    assert.equal(result.error.code, "tool.unknown_outcome");
    assert.equal(vault.entries.has(fixture.experiencePath), false);
    assert.equal(vault.entries.has(fixture.questionPath), false);
    assert.equal(vault.entries.get(fixture.experienceIndexPath), fixture.entries[fixture.experienceIndexPath]);
    assert.match(vault.entries.get(fixture.questionIndexPath), /database-isolation/u);
    assert.equal((await journal.load("batch_interview_modify_unknown")).state, "recovery_failed");
    assert.deepEqual(
        (await journal.load("batch_interview_modify_unknown")).manualReviewPaths,
        [fixture.questionIndexPath],
    );
});

test("Vault conditional apply rejects a user edit after coordinator revalidation", async () => {
    const { VaultChangeCoordinator } = loadModule();
    const before = "alpha\n";
    const userEdit = "manual edit\n";
    const vault = new MemoryVault({ "notes/a.md": before });
    vault.applyConditional = async (mutation) => {
        vault.entries.set(mutation.path, userEdit);
        vault.versions.set(mutation.path, "mtime:user:size:12");
        return {
            status: "conflict",
            observed: {
                contentHash: digest(userEdit),
                modifiedVersion: "mtime:user:size:12",
            },
        };
    };
    const journal = new MemoryJournal();
    const coordinator = new VaultChangeCoordinator({
        vault,
        journal,
        checkpoints: new MemoryCheckpoints(vault),
        permissionMode: () => "trusted_vault",
    });

    const result = await coordinator.execute(call("batch_conditional_modify_race", [{
        op: "append",
        path: "notes/a.md",
        content: "agent edit\n",
        expectedContentHash: digest(before),
        expectedModifiedVersion: initialModifiedVersion(1, before),
    }]));

    assert.equal(result.status, "failed");
    assert.equal(result.error.code, "resource.conflict");
    assert.equal(vault.entries.get("notes/a.md"), userEdit);
    assert.equal((await journal.load("batch_conditional_modify_race")).state, "rolled_back");
});

test("Interview Submission never recreates an existing target deleted after its final check", async () => {
    const { VaultChangeCoordinator } = loadModule();
    const fixture = interviewFixture();
    const vault = new MemoryVault(fixture.entries);
    const journal = new MemoryJournal();
    const originalWrite = vault.write.bind(vault);
    let raced = false;
    const raceUserDelete = (target) => {
        if (target !== fixture.questionIndexPath || raced) return;
        raced = true;
        vault.entries.delete(target);
        vault.versions.delete(target);
    };
    vault.write = async (target, content) => {
        raceUserDelete(target);
        await originalWrite(target, content);
    };
    vault.modify = async (target, content) => {
        raceUserDelete(target);
        if (!vault.entries.has(target)) throw new Error(`modify target is missing: ${target}`);
        await originalWrite(target, content);
    };
    const coordinator = new VaultChangeCoordinator({
        vault,
        journal,
        checkpoints: new MemoryCheckpoints(vault),
        permissionMode: () => "trusted_vault",
        authorize: async (proposal) => approve(proposal),
    });

    const result = await coordinator.execute(call(
        "batch_interview_modify_delete_race", fixture.operations, fixture.argumentOverrides,
    ));

    assert.equal(result.status, "failed");
    assert.equal(result.error.code, "resource.conflict");
    assert.equal(vault.entries.has(fixture.questionIndexPath), false);
    assert.equal(vault.entries.has(fixture.experiencePath), false);
    assert.equal(vault.entries.has(fixture.questionPath), false);
    assert.equal(vault.entries.get(fixture.experienceIndexPath), fixture.entries[fixture.experienceIndexPath]);
    assert.equal((await journal.load("batch_interview_modify_delete_race")).state, "rolled_back");
});

test("Vault delete reports unknown outcome when removal succeeds and then rejects", async () => {
    const { VaultChangeCoordinator } = loadModule();
    const indexContent = "[[memory/user/old]]\n";
    const topicContent = memoryTopic();
    const vault = new MemoryVault({
        "memory/MEMORY.md": indexContent,
        "memory/user/old.md": topicContent,
    });
    const journal = new MemoryJournal();
    const originalRemove = vault.remove.bind(vault);
    vault.remove = async (target) => {
        await originalRemove(target);
        throw new Error("delete completion was lost");
    };
    const coordinator = new VaultChangeCoordinator({
        vault,
        journal,
        checkpoints: new MemoryCheckpoints(vault),
        permissionMode: () => "trusted_vault",
        authorize: async (proposal) => approve(proposal),
    });

    const result = await coordinator.execute(call("batch_delete_unknown", [
        {
            op: "delete",
            path: "memory/user/old.md",
            expectedContentHash: digest(topicContent),
            expectedModifiedVersion: initialModifiedVersion(2, topicContent),
        },
        {
            op: "replace",
            path: "memory/MEMORY.md",
            find: indexContent,
            replacement: "",
            expectedContentHash: digest(indexContent),
            expectedModifiedVersion: initialModifiedVersion(1, indexContent),
        },
    ]));

    assert.equal(result.status, "unknown_outcome");
    assert.equal(result.error.code, "tool.unknown_outcome");
    assert.equal(vault.entries.has("memory/user/old.md"), false);
    assert.equal(vault.entries.get("memory/MEMORY.md"), indexContent);
    assert.equal((await journal.load("batch_delete_unknown")).state, "recovery_failed");
    assert.deepEqual((await journal.load("batch_delete_unknown")).manualReviewPaths, ["memory/user/old.md"]);
});

test("Obsidian Vault conditional modify uses process and compares the callback-time version", async () => {
    const { ObsidianVaultChangePort } = loadModule();
    const before = "alpha\n";
    const after = "alpha\nagent\n";
    const file = {
        path: "notes/a.md",
        stat: { mtime: 1, size: Buffer.byteLength(before) },
    };
    let content = before;
    let processCalls = 0;
    let modifyCalls = 0;
    let callbackThrew = false;
    const vault = {
        getFileByPath: () => file,
        getAbstractFileByPath: () => file,
        cachedRead: async () => content,
        create: async () => { throw new Error("unexpected create"); },
        createFolder: async () => { throw new Error("unexpected folder create"); },
        delete: async () => { throw new Error("unexpected delete"); },
        modify: async () => { modifyCalls += 1; },
        process: async (_file, callback) => {
            processCalls += 1;
            file.stat.mtime = 2;
            try {
                content = callback(content);
            } catch {
                // Model an implementation whose undocumented callback-exception behavior
                // still commits a value. Safety must not depend on that exception aborting.
                callbackThrew = true;
                content = after;
            }
            return content;
        },
    };
    const port = new ObsidianVaultChangePort(vault);

    const outcome = await port.applyConditional({
        kind: "modify",
        path: file.path,
        expected: {
            contentHash: digest(before),
            modifiedVersion: `mtime:1:size:${Buffer.byteLength(before)}`,
        },
        afterContent: after,
    });

    assert.equal(outcome.status, "conflict");
    assert.equal(content, before);
    assert.equal(processCalls, 1);
    assert.equal(modifyCalls, 0);
    assert.equal(callbackThrew, false);
});

test("Obsidian Vault conditional modify reports applied and ambiguous process outcomes", async () => {
    const { ObsidianVaultChangePort } = loadModule();
    const before = "alpha\n";
    const after = "alpha\nagent\n";

    async function execute(rejectAfterWrite) {
        const file = {
            path: "notes/a.md",
            stat: { mtime: 1, size: Buffer.byteLength(before) },
        };
        let content = before;
        const port = new ObsidianVaultChangePort({
            getFileByPath: () => file,
            getAbstractFileByPath: () => file,
            cachedRead: async () => content,
            create: async () => { throw new Error("unexpected create"); },
            createFolder: async () => { throw new Error("unexpected folder create"); },
            delete: async () => { throw new Error("unexpected delete"); },
            modify: async () => { throw new Error("Vault.modify must not be used"); },
            process: async (_file, callback) => {
                content = callback(content);
                file.stat = { mtime: 2, size: Buffer.byteLength(content) };
                if (rejectAfterWrite) throw new Error("process completion was lost");
                return content;
            },
        });
        const outcome = await port.applyConditional({
            kind: "modify",
            path: file.path,
            expected: {
                contentHash: digest(before),
                modifiedVersion: `mtime:1:size:${Buffer.byteLength(before)}`,
            },
            afterContent: after,
        });
        return { content, outcome };
    }

    assert.deepEqual(await execute(false), {
        content: after,
        outcome: {
            status: "applied",
            applied: { contentHash: digest(after), modifiedVersion: `mtime:2:size:${Buffer.byteLength(after)}` },
        },
    });
    const ambiguous = await execute(true);
    assert.equal(ambiguous.content, after);
    assert.equal(ambiguous.outcome.status, "unknown");
    assert.equal(ambiguous.outcome.observed.contentHash, digest(after));
});

test("Obsidian Vault conditional delete is unsupported and never invokes Vault.delete", async () => {
    const { ObsidianVaultChangePort } = loadModule();
    const before = memoryTopic();
    const file = {
        path: "memory/user/old.md",
        stat: { mtime: 1, size: Buffer.byteLength(before) },
    };
    let deleteCalls = 0;
    const port = new ObsidianVaultChangePort({
        getFileByPath: () => file,
        getAbstractFileByPath: () => file,
        cachedRead: async () => before,
        create: async () => { throw new Error("unexpected create"); },
        createFolder: async () => { throw new Error("unexpected folder create"); },
        delete: async () => { deleteCalls += 1; },
        modify: async () => { throw new Error("unexpected modify"); },
        process: async () => { throw new Error("unexpected process"); },
    });

    const outcome = await port.applyConditional({
        kind: "delete",
        path: file.path,
        expected: {
            contentHash: digest(before),
            modifiedVersion: `mtime:1:size:${Buffer.byteLength(before)}`,
        },
    });

    assert.deepEqual(outcome, { status: "unsupported", operation: "delete" });
    assert.equal(deleteCalls, 0);
});

test("production adapter undo keeps a created file when compare-and-delete is unavailable", async () => {
    const { ObsidianVaultChangePort, VaultChangeCoordinator } = loadModule();
    const files = new Map();
    const contents = new Map();
    let sequence = 0;
    let deleteCalls = 0;
    const obsidianVault = {
        getFileByPath: (target) => files.get(target) ?? null,
        getAbstractFileByPath: (target) => files.get(target) ?? null,
        cachedRead: async (file) => contents.get(file.path),
        createFolder: async () => {},
        create: async (target, content) => {
            const file = {
                path: target,
                stat: { mtime: ++sequence, size: Buffer.byteLength(content) },
            };
            files.set(target, file);
            contents.set(target, content);
            return file;
        },
        delete: async () => { deleteCalls += 1; },
        modify: async () => { throw new Error("Vault.modify must not be used"); },
        process: async (file, callback) => {
            const content = callback(contents.get(file.path));
            contents.set(file.path, content);
            file.stat = { mtime: ++sequence, size: Buffer.byteLength(content) };
            return content;
        },
    };
    const journal = new MemoryJournal();
    const coordinator = new VaultChangeCoordinator({
        vault: new ObsidianVaultChangePort(obsidianVault),
        journal,
        checkpoints: {
            create: async (batchId) => `refs/offeragent/checkpoints/${batchId}`,
            read: async () => undefined,
        },
        permissionMode: () => "trusted_vault",
    });
    const request = call("batch_production_created_undo", [{
        op: "create",
        path: "notes/created.md",
        content: "agent content\n",
        expectedContentHash: "absent",
        expectedModifiedVersion: "missing",
    }]);

    assert.equal((await coordinator.execute(request)).status, "succeeded");
    const undone = await coordinator.undo("batch_production_created_undo");

    assert.equal(undone.status, "conflict");
    assert.deepEqual(undone.paths, ["notes/created.md"]);
    assert.equal(contents.get("notes/created.md"), "agent content\n");
    assert.equal(deleteCalls, 0);
    assert.equal((await journal.load("batch_production_created_undo")).state, "undoing");
    assert.deepEqual(
        (await journal.load("batch_production_created_undo")).manualReviewPaths,
        ["notes/created.md"],
    );
});

test("Interview Submission source version drift after confirmation invalidates the whole batch", async () => {
    const { VaultChangeCoordinator } = loadModule();
    const fixture = interviewFixture();
    const vault = new MemoryVault(fixture.entries);
    const journal = new MemoryJournal();
    const sourcePath = fixture.argumentOverrides.sourceBindings[0].path;
    const coordinator = new VaultChangeCoordinator({
        vault,
        journal,
        checkpoints: new MemoryCheckpoints(vault),
        permissionMode: () => "trusted_vault",
        authorize: async (proposal) => {
            vault.versions.set(sourcePath, "mtime:manual:size:76");
            return approve(proposal);
        },
    });

    const result = await coordinator.execute(call(
        "batch_interview_source_drift", fixture.operations, fixture.argumentOverrides,
    ));

    assert.equal(result.status, "failed");
    assert.equal(result.error.code, "resource.conflict");
    assert.deepEqual(Object.fromEntries(vault.entries), fixture.entries);
    assert.equal((await journal.load("batch_interview_source_drift")).state, "rolled_back");
});

test("Interview Submission target version drift after checkpoint invalidates the whole batch", async () => {
    const { VaultChangeCoordinator } = loadModule();
    const fixture = interviewFixture();
    const vault = new MemoryVault(fixture.entries);
    const journal = new MemoryJournal();
    const checkpoints = new MemoryCheckpoints(vault);
    const originalCreate = checkpoints.create.bind(checkpoints);
    checkpoints.create = async (...arguments_) => {
        const checkpoint = await originalCreate(...arguments_);
        vault.versions.set(fixture.questionIndexPath, "mtime:manual:size:12");
        return checkpoint;
    };
    const coordinator = new VaultChangeCoordinator({
        vault,
        journal,
        checkpoints,
        permissionMode: () => "trusted_vault",
        authorize: async (proposal) => approve(proposal),
    });

    const result = await coordinator.execute(call(
        "batch_interview_target_drift", fixture.operations, fixture.argumentOverrides,
    ));

    assert.equal(result.status, "failed");
    assert.equal(result.error.code, "resource.conflict");
    assert.deepEqual(Object.fromEntries(vault.entries), fixture.entries);
    assert.equal((await journal.load("batch_interview_target_drift")).state, "rolled_back");
});

test("Interview Submission rolls back an earlier Experience when a later Question write fails", async () => {
    const { VaultChangeCoordinator } = loadModule();
    const fixture = interviewFixture();
    const vault = new MemoryVault(fixture.entries);
    vault.failPath = fixture.questionPath;
    const journal = new MemoryJournal();
    const coordinator = new VaultChangeCoordinator({
        vault,
        journal,
        checkpoints: new MemoryCheckpoints(vault),
        permissionMode: () => "trusted_vault",
        authorize: async (proposal) => approve(proposal),
    });

    const result = await coordinator.execute(call(
        "batch_interview_atomic_failure", fixture.operations, fixture.argumentOverrides,
    ));

    assert.equal(result.status, "failed");
    assert.equal(result.error.code, "tool.failed");
    assert.deepEqual(Object.fromEntries(vault.entries), fixture.entries);
    assert.equal((await journal.load("batch_interview_atomic_failure")).state, "rolled_back");
});

test("structural Interview Experience ingestion cannot be mislabeled as a general Vault change", async () => {
    const { VaultChangeCoordinator } = loadModule();
    const fixture = interviewFixture();
    const vault = new MemoryVault(fixture.entries);
    const journal = new MemoryJournal();
    let approvals = 0;
    const coordinator = new VaultChangeCoordinator({
        vault,
        journal,
        checkpoints: new MemoryCheckpoints(vault),
        permissionMode: () => "trusted_vault",
        authorize: async (proposal) => { approvals += 1; return approve(proposal); },
    });

    const result = await coordinator.execute(call("batch_interview_mislabeled", fixture.operations));

    assert.equal(result.status, "failed");
    assert.equal(result.error.code, "protocol.invalid_params");
    assert.equal(approvals, 0);
    assert.deepEqual(Object.fromEntries(vault.entries), fixture.entries);
    assert.equal(await journal.load("batch_interview_mislabeled"), undefined);
});

test("unsafe Interview Submission structure is rejected before preview or mutation", async (t) => {
    const { VaultChangeCoordinator } = loadModule();
    const fixture = interviewFixture();
    const existingQuestionContent = fixture.operations[1].content
        .replace(
            "- [[../experiences/acme-backend-2026-07-18]]",
            "- [[../experiences/older-experience]]",
        );
    const entriesWithExistingQuestion = {
        ...fixture.entries,
        [fixture.questionPath]: existingQuestionContent,
    };
    const existingQuestionVersion = initialModifiedVersion(4, existingQuestionContent);
    const invalidStateQuestionContent = existingQuestionContent.replace(
        "answer-state: needs-research",
        "answer-state: unsupported",
    );
    const inconsistentFrequencyQuestionContent = existingQuestionContent.replace("frequency: 1", "frequency: 2");
    const existingQuestionReviewItems = [
        {
            kind: "experience", path: fixture.experiencePath,
            identity: "new", mutation: "create",
        },
        {
            kind: "question", path: fixture.questionPath,
            identity: "existing", mutation: "modify",
        },
        {
            kind: "index", path: fixture.experienceIndexPath,
            identity: "existing", mutation: "modify",
        },
    ];
    const cases = [
        {
            name: "structured review plan is missing",
            operations: fixture.operations,
            argumentOverrides: {
                ...fixture.argumentOverrides,
                interviewSubmission: {
                    ...fixture.argumentOverrides.interviewSubmission,
                    reviewItems: undefined,
                },
            },
        },
        {
            name: "structured review plan repeats a case-folded path",
            operations: fixture.operations,
            argumentOverrides: {
                ...fixture.argumentOverrides,
                interviewSubmission: {
                    ...fixture.argumentOverrides.interviewSubmission,
                    reviewItems: [
                        ...fixture.argumentOverrides.interviewSubmission.reviewItems,
                        fixture.argumentOverrides.interviewSubmission.reviewItems[0],
                    ],
                },
            },
        },
        {
            name: "structured review identity disagrees with its mutation",
            operations: fixture.operations,
            argumentOverrides: {
                ...fixture.argumentOverrides,
                interviewSubmission: {
                    ...fixture.argumentOverrides.interviewSubmission,
                    reviewItems: fixture.argumentOverrides.interviewSubmission.reviewItems.map((item, index) =>
                        index === 0 ? { ...item, mutation: "modify" } : item),
                },
            },
        },
        {
            name: "informational no-op has no exact source binding",
            entries: {
                ...fixture.entries,
                [fixture.questionPath]: fixture.operations[1].content,
            },
            operations: [fixture.operations[0], fixture.operations[2]],
            argumentOverrides: {
                ...fixture.argumentOverrides,
                interviewSubmission: {
                    ...fixture.argumentOverrides.interviewSubmission,
                    reviewItems: [
                        fixture.argumentOverrides.interviewSubmission.reviewItems[0],
                        {
                            kind: "question", path: fixture.questionPath,
                            identity: "existing", mutation: "none",
                        },
                        fixture.argumentOverrides.interviewSubmission.reviewItems[2],
                    ],
                },
            },
        },
        {
            name: "complete duplicate attempts an empty Apply batch",
            entries: {
                ...fixture.entries,
                [fixture.experiencePath]: fixture.operations[0].content,
                [fixture.questionPath]: fixture.operations[1].content,
            },
            operations: [],
            argumentOverrides: {
                ...fixture.argumentOverrides,
                sourceBindings: [
                    ...fixture.argumentOverrides.sourceBindings,
                    {
                        path: fixture.experiencePath,
                        expectedContentHash: digest(fixture.operations[0].content),
                        expectedModifiedVersion: initialModifiedVersion(4, fixture.operations[0].content),
                    },
                    {
                        path: fixture.questionPath,
                        expectedContentHash: digest(fixture.operations[1].content),
                        expectedModifiedVersion: initialModifiedVersion(5, fixture.operations[1].content),
                    },
                ],
                interviewSubmission: {
                    ...fixture.argumentOverrides.interviewSubmission,
                    reviewItems: [
                        {
                            kind: "experience", path: fixture.experiencePath,
                            identity: "existing", mutation: "none",
                        },
                        {
                            kind: "question", path: fixture.questionPath,
                            identity: "existing", mutation: "none",
                        },
                    ],
                },
            },
        },
        {
            name: "mismatched source manifest",
            operations: fixture.operations.map((operation, index) => index === 0
                ? { ...operation, content: operation.content.replace(
                    "source-url: https://example.com/interview/42",
                    "source-url: https://example.com/interview/other",
                ) }
                : operation),
        },
        {
            name: "new Question with a standard answer",
            operations: fixture.operations.map((operation, index) => index === 1
                ? { ...operation, content: `${operation.content}\n## Standard Answer\nDo not store this.\n` }
                : operation),
        },
        {
            name: "second new Experience",
            operations: [
                ...fixture.operations,
                {
                    ...fixture.operations[0],
                    path: "experiences/acme-second.md",
                    content: fixture.operations[0].content.replace(
                        "experience-id: exp_acme_backend_20260718",
                        "experience-id: exp_acme_second",
                    ),
                },
            ],
        },
        {
            name: "fingerprint inconsistent with ordered image hashes",
            operations: fixture.operations.map((operation, index) => index === 0
                ? { ...operation, content: operation.content.replace(
                    fixture.argumentOverrides.interviewSubmission.sourceFingerprint,
                    `sha256:${"9".repeat(64)}`,
                ) }
                : operation),
            argumentOverrides: {
                ...fixture.argumentOverrides,
                interviewSubmission: {
                    ...fixture.argumentOverrides.interviewSubmission,
                    sourceFingerprint: `sha256:${"9".repeat(64)}`,
                },
            },
        },
        {
            name: "source fingerprint is supplied without ordered images",
            operations: fixture.operations.map((operation, index) => index === 0
                ? { ...operation, content: operation.content.replace("source-kind: mixed", "source-kind: public_url") }
                : operation),
            argumentOverrides: {
                ...fixture.argumentOverrides,
                interviewSubmission: {
                    ...fixture.argumentOverrides.interviewSubmission,
                    sourceKind: "public_url",
                    orderedImageContentHashes: [],
                },
            },
        },
        {
            name: "create target uses the obsolete absent modified version",
            operations: fixture.operations.map((operation, index) => index === 0
                ? { ...operation, expectedModifiedVersion: "absent" }
                : operation),
        },
        {
            name: "target path has surrounding whitespace",
            operations: fixture.operations.map((operation, index) => index === 0
                ? { ...operation, path: ` ${operation.path}` }
                : operation),
        },
        {
            name: "Experience omits a required identity field",
            operations: fixture.operations.map((operation, index) => index === 0
                ? { ...operation, content: operation.content.replace("round: unknown\n", "") }
                : operation),
        },
        {
            name: "Experience omits its Catalog identity",
            operations: fixture.operations.map((operation, index) => index === 0
                ? { ...operation, content: operation.content.replace(
                    "experience-id: exp_acme_backend_20260718\n",
                    "",
                ) }
                : operation),
        },
        {
            name: "Experience has an impossible event date",
            operations: fixture.operations.map((operation, index) => index === 0
                ? { ...operation, content: operation.content.replace(
                    "event-date: unknown",
                    "event-date: 2026-02-30",
                ) }
                : operation),
        },
        {
            name: "Experience stores candidate PII in frontmatter",
            operations: fixture.operations.map((operation, index) => index === 0
                ? { ...operation, content: operation.content.replace(
                    "round: unknown\n",
                    "round: unknown\ncandidate-name: Alice Example\n",
                ) }
                : operation),
        },
        {
            name: "Experience stores a candidate email in frontmatter",
            operations: fixture.operations.map((operation, index) => index === 0
                ? { ...operation, content: operation.content.replace(
                    "round: unknown\n",
                    "round: unknown\ncandidate-email: alice@example.com\n",
                ) }
                : operation),
        },
        {
            name: "Experience stores an e-mail alias in frontmatter",
            operations: fixture.operations.map((operation, index) => index === 0
                ? { ...operation, content: operation.content.replace(
                    "round: unknown\n",
                    "round: unknown\ne-mail: alice@example.com\n",
                ) }
                : operation),
        },
        {
            name: "source binding uses the obsolete absent modified version",
            operations: fixture.operations,
            argumentOverrides: {
                ...fixture.argumentOverrides,
                sourceBindings: fixture.argumentOverrides.sourceBindings.map((binding) => ({
                    ...binding,
                    expectedModifiedVersion: "absent",
                })),
            },
        },
        {
            name: "new Question omits its Catalog identity",
            operations: fixture.operations.map((operation, index) => index === 1
                ? { ...operation, content: operation.content.replace(
                    "question-id: question_database_isolation\n",
                    "",
                ) }
                : operation),
        },
        {
            name: "new Question has no title",
            operations: fixture.operations.map((operation, index) => index === 1
                ? { ...operation, content: operation.content.replace(
                    "title: Explain database isolation",
                    "title: ",
                ) }
                : operation),
        },
        {
            name: "new Question does not start at frequency one",
            operations: fixture.operations.map((operation, index) => index === 1
                ? { ...operation, content: operation.content.replace("frequency: 1", "frequency: 2") }
                : operation),
        },
        {
            name: "new Question contains a reference answer",
            operations: fixture.operations.map((operation, index) => index === 1
                ? { ...operation, content: `${operation.content}\n## Reference Answer\nDo not store this.\n` }
                : operation),
        },
        {
            name: "canonical URL retains a tracking parameter",
            operations: fixture.operations.map((operation, index) => index === 0
                ? { ...operation, content: operation.content.replace(
                    "https://example.com/interview/42",
                    "https://example.com/interview/42?utm_source=feed",
                ) }
                : operation),
            argumentOverrides: {
                ...fixture.argumentOverrides,
                interviewSubmission: {
                    ...fixture.argumentOverrides.interviewSubmission,
                    canonicalUrls: ["https://example.com/interview/42?utm_source=feed"],
                },
            },
        },
        {
            name: "canonical URL retains a temporary token",
            operations: fixture.operations.map((operation, index) => index === 0
                ? { ...operation, content: operation.content.replace(
                    "https://example.com/interview/42",
                    "https://example.com/interview/42?token=temporary",
                ) }
                : operation),
            argumentOverrides: {
                ...fixture.argumentOverrides,
                interviewSubmission: {
                    ...fixture.argumentOverrides.interviewSubmission,
                    canonicalUrls: ["https://example.com/interview/42?token=temporary"],
                },
            },
        },
        {
            name: "canonical URL is IPv4-mapped loopback",
            operations: fixture.operations.map((operation, index) => index === 0
                ? { ...operation, content: operation.content.replace(
                    "https://example.com/interview/42",
                    "http://[::ffff:7f00:1]/",
                ) }
                : operation),
            argumentOverrides: {
                ...fixture.argumentOverrides,
                interviewSubmission: {
                    ...fixture.argumentOverrides.interviewSubmission,
                    canonicalUrls: ["http://[::ffff:7f00:1]/"],
                },
            },
        },
        {
            name: "canonical URL is IPv6 unspecified-prefix space",
            operations: fixture.operations.map((operation, index) => index === 0
                ? { ...operation, content: operation.content.replace(
                    "https://example.com/interview/42",
                    "http://[::2]/",
                ) }
                : operation),
            argumentOverrides: {
                ...fixture.argumentOverrides,
                interviewSubmission: {
                    ...fixture.argumentOverrides.interviewSubmission,
                    canonicalUrls: ["http://[::2]/"],
                },
            },
        },
        {
            name: "legacy Interview path is used as a new target",
            operations: fixture.operations.map((operation, index) => index === 1
                ? { ...operation, path: "interviews/questions/database-isolation.md" }
                : operation),
        },
        {
            name: "one primary index is omitted",
            operations: fixture.operations.slice(0, -1),
        },
        {
            name: "primary index does not link its new target",
            operations: fixture.operations.map((operation, index) => index === 2
                ? { ...operation, content: "- unrelated\n" }
                : operation),
        },
        {
            name: "Experience does not link its Question",
            operations: fixture.operations.map((operation, index) => index === 0
                ? { ...operation, content: operation.content.replace(
                    "- [[../interview/database-isolation]]",
                    "- relationship omitted",
                ) }
                : operation),
        },
        {
            name: "Question does not link its Experience occurrence",
            operations: fixture.operations.map((operation, index) => index === 1
                ? { ...operation, content: operation.content.replace(
                    "- [[../experiences/acme-backend-2026-07-18]]",
                    "- occurrence omitted",
                ) }
                : operation),
        },
        {
            name: "existing Question update omits the new Experience occurrence",
            entries: entriesWithExistingQuestion,
            operations: [
                fixture.operations[0],
                {
                    op: "append",
                    path: fixture.questionPath,
                    content: "\n## Source Notes\n- Additional context.\n",
                    expectedContentHash: digest(existingQuestionContent),
                    expectedModifiedVersion: existingQuestionVersion,
                },
                fixture.operations[2],
            ],
            argumentOverrides: {
                ...fixture.argumentOverrides,
                interviewSubmission: {
                    ...fixture.argumentOverrides.interviewSubmission,
                    reviewItems: existingQuestionReviewItems,
                },
            },
        },
        {
            name: "existing Question update leaves an invalid answer state",
            entries: {
                ...fixture.entries,
                [fixture.questionPath]: invalidStateQuestionContent,
            },
            operations: [
                fixture.operations[0],
                {
                    op: "append",
                    path: fixture.questionPath,
                    content: "\n## Source Notes\n- Additional context.\n",
                    expectedContentHash: digest(invalidStateQuestionContent),
                    expectedModifiedVersion: initialModifiedVersion(4, invalidStateQuestionContent),
                },
                fixture.operations[2],
            ],
            argumentOverrides: {
                ...fixture.argumentOverrides,
                interviewSubmission: {
                    ...fixture.argumentOverrides.interviewSubmission,
                    reviewItems: existingQuestionReviewItems,
                },
            },
        },
        {
            name: "existing Question frequency disagrees with its unique occurrences",
            entries: {
                ...fixture.entries,
                [fixture.questionPath]: inconsistentFrequencyQuestionContent,
            },
            operations: [
                fixture.operations[0],
                {
                    op: "patch",
                    path: fixture.questionPath,
                    edits: [{
                        startLine: 11,
                        endLine: 11,
                        replacement: [
                            "- [[../experiences/older-experience]]",
                            "- [[../experiences/acme-backend-2026-07-18]]",
                        ].join("\n"),
                    }],
                    expectedContentHash: digest(inconsistentFrequencyQuestionContent),
                    expectedModifiedVersion: initialModifiedVersion(4, inconsistentFrequencyQuestionContent),
                },
                fixture.operations[2],
            ],
            argumentOverrides: {
                ...fixture.argumentOverrides,
                interviewSubmission: {
                    ...fixture.argumentOverrides.interviewSubmission,
                    reviewItems: existingQuestionReviewItems,
                },
            },
        },
        {
            name: "existing Question repeats the current Experience occurrence",
            entries: entriesWithExistingQuestion,
            operations: [
                fixture.operations[0],
                {
                    op: "patch",
                    path: fixture.questionPath,
                    edits: [
                        { startLine: 6, endLine: 6, replacement: "frequency: 3" },
                        {
                            startLine: 11,
                            endLine: 11,
                            replacement: [
                                "- [[../experiences/older-experience]]",
                                "- [[../experiences/acme-backend-2026-07-18]]",
                                "- [[../experiences/acme-backend-2026-07-18]]",
                            ].join("\n"),
                        },
                    ],
                    expectedContentHash: digest(existingQuestionContent),
                    expectedModifiedVersion: existingQuestionVersion,
                },
                fixture.operations[2],
            ],
            argumentOverrides: {
                ...fixture.argumentOverrides,
                interviewSubmission: {
                    ...fixture.argumentOverrides.interviewSubmission,
                    reviewItems: existingQuestionReviewItems,
                },
            },
        },
        {
            name: "Interview batch deletes a Question",
            entries: entriesWithExistingQuestion,
            operations: fixture.operations.map((operation, index) => index === 1
                ? {
                    op: "delete",
                    path: operation.path,
                    expectedContentHash: digest(existingQuestionContent),
                    expectedModifiedVersion: existingQuestionVersion,
                }
                : operation),
        },
        {
            name: "general batch carries Interview metadata",
            operations: fixture.operations,
            argumentOverrides: {
                ...fixture.argumentOverrides,
                changeKind: "general",
            },
        },
        {
            name: "Interview batch carries null metadata",
            operations: fixture.operations,
            argumentOverrides: {
                ...fixture.argumentOverrides,
                interviewSubmission: null,
            },
        },
        {
            name: "Interview metadata field is missing",
            operations: fixture.operations,
            removeInterviewSubmission: true,
        },
    ];
    for (const candidate of cases) {
        await t.test(candidate.name, async () => {
            const initialEntries = candidate.entries ?? fixture.entries;
            const vault = new MemoryVault(initialEntries);
            const journal = new MemoryJournal();
            let approvals = 0;
            const coordinator = new VaultChangeCoordinator({
                vault,
                journal,
                checkpoints: new MemoryCheckpoints(vault),
                permissionMode: () => "trusted_vault",
                authorize: async (proposal) => { approvals += 1; return approve(proposal); },
            });

            const request = call(
                `batch_unsafe_${candidate.name.replaceAll(" ", "_")}`,
                candidate.operations,
                candidate.argumentOverrides ?? fixture.argumentOverrides,
            );
            if (candidate.removeInterviewSubmission) delete request.arguments.interviewSubmission;
            const result = await coordinator.execute(request);

            assert.equal(result.status, "failed");
            assert.equal(result.error.code, "protocol.invalid_params");
            assert.equal(approvals, 0);
            assert.deepEqual(Object.fromEntries(vault.entries), initialEntries);
        });
    }
});

test("every general Vault operation requires an explicit modified-version precondition", async () => {
    const { VaultChangeCoordinator } = loadModule();
    const vault = new MemoryVault({ "notes/a.md": "alpha\n" });
    const coordinator = new VaultChangeCoordinator({
        vault,
        journal: new MemoryJournal(),
        checkpoints: new MemoryCheckpoints(vault),
        permissionMode: () => "trusted_vault",
    });

    const result = await coordinator.execute(call("batch_general_missing_version", [{
        op: "append", path: "notes/a.md", content: "next\n", expectedContentHash: digest("alpha\n"),
    }]));

    assert.equal(result.status, "failed");
    assert.equal(result.error.code, "protocol.invalid_params");
    assert.equal(vault.entries.get("notes/a.md"), "alpha\n");
});

test("Vault Change Batch validates every target before mutation and rolls back a partial failure", async () => {
    const { VaultChangeCoordinator } = loadModule();
    const vault = new MemoryVault({ "notes/a.md": "alpha\n", "notes/b.md": "beta\n" });
    const journal = new MemoryJournal();
    const checkpoints = new MemoryCheckpoints(vault);
    vault.failPath = "notes/b.md";
    const coordinator = new VaultChangeCoordinator({
        vault, journal, checkpoints, permissionMode: () => "trusted_vault",
    });

    const result = await coordinator.execute(call("batch_failure", [
        {
            op: "append", path: "notes/a.md", content: "next\n", expectedContentHash: digest("alpha\n"),
            expectedModifiedVersion: initialModifiedVersion(1, "alpha\n"),
        },
        {
            op: "replace", path: "notes/b.md", find: "beta", replacement: "changed",
            expectedContentHash: digest("beta\n"), expectedModifiedVersion: initialModifiedVersion(2, "beta\n"),
        },
    ]));

    assert.equal(result.status, "failed");
    assert.equal(result.error.code, "tool.failed");
    assert.equal(vault.entries.get("notes/a.md"), "alpha\n");
    assert.equal(vault.entries.get("notes/b.md"), "beta\n");
    assert.equal((await journal.load("batch_failure")).state, "rolled_back");
});

test("unsupported reverse deletion preserves the created file and records manual review", async () => {
    const { VaultChangeCoordinator } = loadModule();
    const vault = new MemoryVault({ "notes/existing.md": "before\n" });
    const journal = new MemoryJournal();
    const originalApplyConditional = vault.applyConditional.bind(vault);
    vault.applyConditional = async (mutation) => {
        if (mutation.kind === "delete") return { status: "unsupported", operation: "delete" };
        if (mutation.path === "notes/existing.md") {
            const observed = await vault.snapshot(mutation.path);
            return {
                status: "conflict",
                observed: { contentHash: digest(observed.content), modifiedVersion: observed.modifiedVersion },
            };
        }
        return originalApplyConditional(mutation);
    };
    const coordinator = new VaultChangeCoordinator({
        vault,
        journal,
        checkpoints: new MemoryCheckpoints(vault),
        permissionMode: () => "trusted_vault",
    });

    const result = await coordinator.execute(call("batch_reverse_delete_unsupported", [
        {
            op: "create", path: "notes/created.md", content: "agent content\n",
            expectedContentHash: "absent", expectedModifiedVersion: "missing",
        },
        {
            op: "append", path: "notes/existing.md", content: "agent\n",
            expectedContentHash: digest("before\n"),
            expectedModifiedVersion: initialModifiedVersion(1, "before\n"),
        },
    ]));

    assert.equal(result.status, "unknown_outcome");
    assert.equal(vault.entries.get("notes/created.md"), "agent content\n");
    assert.equal(vault.entries.get("notes/existing.md"), "before\n");
    assert.equal((await journal.load("batch_reverse_delete_unsupported")).state, "recovery_failed");
    assert.deepEqual(
        (await journal.load("batch_reverse_delete_unsupported")).manualReviewPaths,
        ["notes/created.md"],
    );
});

test("an unresolved apply outcome latches the current coordinator fail-closed", async () => {
    const { VaultChangeCoordinator } = loadModule();
    const vault = new MemoryVault({ "notes/a.md": "alpha\n", "notes/b.md": "beta\n", "notes/c.md": "gamma\n" });
    const originalWrite = vault.write.bind(vault);
    vault.write = async (target, content) => {
        if (target === "notes/b.md") throw new Error("injected apply failure");
        if (target === "notes/a.md" && content === "alpha\n") throw new Error("injected rollback failure");
        await originalWrite(target, content);
    };
    const journal = new MemoryJournal();
    const coordinator = new VaultChangeCoordinator({
        vault, journal, checkpoints: new MemoryCheckpoints(vault), permissionMode: () => "trusted_vault",
    });

    const unresolved = await coordinator.execute(call("batch_unresolved_apply", [
        {
            op: "append", path: "notes/a.md", content: "next\n", expectedContentHash: digest("alpha\n"),
            expectedModifiedVersion: initialModifiedVersion(1, "alpha\n"),
        },
        {
            op: "append", path: "notes/b.md", content: "next\n", expectedContentHash: digest("beta\n"),
            expectedModifiedVersion: initialModifiedVersion(2, "beta\n"),
        },
    ]));
    const blocked = await coordinator.execute(call("batch_after_unresolved_apply", [
        {
            op: "append", path: "notes/c.md", content: "later\n", expectedContentHash: digest("gamma\n"),
            expectedModifiedVersion: initialModifiedVersion(3, "gamma\n"),
        },
    ]));

    assert.equal(unresolved.status, "unknown_outcome");
    assert.equal((await journal.load("batch_unresolved_apply")).state, "recovery_failed");
    assert.equal(blocked.status, "unknown_outcome");
    assert.equal(blocked.retryable, false);
    assert.equal(vault.entries.get("notes/c.md"), "gamma\n");
});

test("Vault Change Batch revalidates after checkpoint and never overwrites a racing user edit", async () => {
    const { VaultChangeCoordinator } = loadModule();
    const vault = new MemoryVault({ "notes/a.md": "alpha\n" });
    const journal = new MemoryJournal();
    const checkpoints = new MemoryCheckpoints(vault);
    const originalCreate = checkpoints.create.bind(checkpoints);
    checkpoints.create = async (...arguments_) => {
        const checkpoint = await originalCreate(...arguments_);
        vault.entries.set("notes/a.md", "manual edit\n");
        return checkpoint;
    };
    const coordinator = new VaultChangeCoordinator({
        vault, journal, checkpoints, permissionMode: () => "trusted_vault",
    });

    const result = await coordinator.execute(call("batch_checkpoint_race", [
        {
            op: "append", path: "notes/a.md", content: "agent edit\n", expectedContentHash: digest("alpha\n"),
            expectedModifiedVersion: initialModifiedVersion(1, "alpha\n"),
        },
    ]));

    assert.equal(result.status, "failed");
    assert.equal(result.error.code, "resource.conflict");
    assert.equal(vault.entries.get("notes/a.md"), "manual edit\n");
    assert.equal((await journal.load("batch_checkpoint_race")).state, "rolled_back");
});

test("Vault Change Batch revalidates each target and rolls back earlier writes around a later user edit", async () => {
    const { VaultChangeCoordinator } = loadModule();
    const vault = new MemoryVault({ "notes/a.md": "alpha\n", "notes/b.md": "beta\n" });
    const originalWrite = vault.write.bind(vault);
    vault.write = async (target, content) => {
        await originalWrite(target, content);
        if (target === "notes/a.md") vault.entries.set("notes/b.md", "manual beta\n");
    };
    const journal = new MemoryJournal();
    const checkpoints = new MemoryCheckpoints(vault);
    const coordinator = new VaultChangeCoordinator({
        vault, journal, checkpoints, permissionMode: () => "trusted_vault",
    });

    const result = await coordinator.execute(call("batch_target_race", [
        {
            op: "append", path: "notes/a.md", content: "agent alpha\n", expectedContentHash: digest("alpha\n"),
            expectedModifiedVersion: initialModifiedVersion(1, "alpha\n"),
        },
        {
            op: "append", path: "notes/b.md", content: "agent beta\n", expectedContentHash: digest("beta\n"),
            expectedModifiedVersion: initialModifiedVersion(2, "beta\n"),
        },
    ]));

    assert.equal(result.status, "failed");
    assert.equal(result.error.code, "resource.conflict");
    assert.equal(vault.entries.get("notes/a.md"), "alpha\n");
    assert.equal(vault.entries.get("notes/b.md"), "manual beta\n");
    assert.equal((await journal.load("batch_target_race")).state, "rolled_back");
});

test("plugin permission is fail-closed and control or memory-delete batches always ask", async () => {
    const { VaultChangeCoordinator } = loadModule();
    const vault = new MemoryVault({
        "agent.md": "old contract\n",
        "memory/MEMORY.md": "[[memory/user/old]]\n",
        "memory/user/old.md": memoryTopic(),
    });
    const journal = new MemoryJournal();
    const checkpoints = new MemoryCheckpoints(vault);
    let approvals = 0;
    const readOnly = new VaultChangeCoordinator({
        vault, journal, checkpoints, permissionMode: () => "read_only",
        authorize: async (proposal) => { approvals += 1; return approve(proposal); },
    });
    const denied = await readOnly.execute(call("batch_denied", [
        {
            op: "append", path: "agent.md", content: "new\n", expectedContentHash: digest("old contract\n"),
            expectedModifiedVersion: initialModifiedVersion(1, "old contract\n"),
        },
    ]));
    assert.equal(denied.status, "denied");
    assert.equal(approvals, 0);

    const trusted = new VaultChangeCoordinator({
        vault, journal, checkpoints, permissionMode: () => "trusted_vault",
        authorize: async (proposal) => {
            approvals += 1;
            assert.ok(proposal.reviewTargets.some((target) => target.path === "memory/user/old.md"));
            return approve(proposal);
        },
    });
    const applied = await trusted.execute(call("batch_memory_delete", [
        {
            op: "delete", path: "memory/user/old.md", expectedContentHash: digest(memoryTopic()),
            expectedModifiedVersion: initialModifiedVersion(3, memoryTopic()),
        },
        {
            op: "replace", path: "memory/MEMORY.md", find: "[[memory/user/old]]\n", replacement: "",
            expectedContentHash: digest("[[memory/user/old]]\n"),
            expectedModifiedVersion: initialModifiedVersion(2, "[[memory/user/old]]\n"),
        },
    ]));
    assert.equal(applied.status, "succeeded");
    assert.equal(approvals, 1);
    assert.equal(vault.entries.has("memory/user/old.md"), false);
});

test("Planning Memory delete rejects malformed topic metadata or a missing index relationship before mutation", async () => {
    const { VaultChangeCoordinator } = loadModule();
    for (const [suffix, topic, index] of [
        ["metadata", "old memory\n", "[[memory/user/old]]\n"],
        ["type", memoryTopic("study"), "[[memory/user/old]]\n"],
        ["index", memoryTopic(), "# Memory\n"],
    ]) {
        const vault = new MemoryVault({
            "memory/MEMORY.md": index,
            "memory/user/old.md": topic,
        });
        const journal = new MemoryJournal();
        const coordinator = new VaultChangeCoordinator({
            vault,
            journal,
            checkpoints: new MemoryCheckpoints(vault),
            permissionMode: () => "trusted_vault",
            authorize: async () => { throw new Error("invalid delete must not ask for authorization"); },
        });
        const request = call(`batch_bad_memory_${suffix}`, [
            {
                op: "delete", path: "memory/user/old.md", expectedContentHash: digest(topic),
                expectedModifiedVersion: initialModifiedVersion(2, topic),
            },
            {
                op: "replace", path: "memory/MEMORY.md", find: index, replacement: "",
                expectedContentHash: digest(index),
                expectedModifiedVersion: initialModifiedVersion(1, index),
            },
        ]);

        const result = await coordinator.execute(request);

        assert.equal(result.status, "failed");
        assert.equal(result.error.code, "protocol.invalid_params");
        assert.equal(vault.entries.get("memory/user/old.md"), topic);
        assert.equal(vault.entries.get("memory/MEMORY.md"), index);
        assert.equal(await journal.load(request.arguments.batchId), undefined);
    }
});

test("Daily plan create fill append rewrite stale and no-op scenarios preserve unrelated evidence", async () => {
    const { VaultChangeCoordinator } = loadModule();
    const target = "daily/2026-07-17.md";

    async function apply(batchId, before, operation) {
        const vault = new MemoryVault(before === undefined ? {} : { [target]: before });
        const journal = new MemoryJournal();
        const coordinator = new VaultChangeCoordinator({
            vault,
            journal,
            checkpoints: new MemoryCheckpoints(vault),
            permissionMode: () => "trusted_vault",
        });
        const result = await coordinator.execute(call(batchId, [operation]));
        return { result, content: vault.entries.get(target), journal };
    }

    const createdContent = "---\ndate: 2026-07-17\n---\n\n## 今日计划\n\n- [ ] Agentic RL\n";
    const created = await apply("daily_create", undefined, {
        op: "create", path: target, content: createdContent, expectedContentHash: "absent",
        expectedModifiedVersion: "missing",
    });
    assert.equal(created.result.status, "succeeded");
    assert.equal(created.content, createdContent);

    const fillBefore = [
        "---", "date: 2026-07-17", "---", "", "- [x] Completed review", "", "## 今日计划", "",
        "<!-- offeragent-plan -->", "", "## 随手记录", "", "Keep this.", "",
    ].join("\n");
    const filled = await apply("daily_fill", fillBefore, {
        op: "replace",
        path: target,
        find: "## 今日计划\n\n<!-- offeragent-plan -->",
        replacement: "## 今日计划\n\n- [ ] Reward modeling",
        expectedContentHash: digest(fillBefore),
        expectedModifiedVersion: initialModifiedVersion(1, fillBefore),
    });
    assert.equal(filled.result.status, "succeeded");
    assert.match(filled.content, /- \[x\] Completed review/u);
    assert.match(filled.content, /- \[ \] Reward modeling/u);
    assert.match(filled.content, /Keep this\./u);

    const appendBefore = "---\ndate: 2026-07-17\n---\n\n- [x] Evidence\n\nUnrelated note.\n";
    const appended = await apply("daily_append", appendBefore, {
        op: "append",
        path: target,
        content: "\n## 今日计划\n\n- [ ] Policy optimization\n",
        expectedContentHash: digest(appendBefore),
        expectedModifiedVersion: initialModifiedVersion(1, appendBefore),
    });
    assert.equal(appended.result.status, "succeeded");
    assert.match(appended.content, /- \[x\] Evidence/u);
    assert.match(appended.content, /Unrelated note\./u);
    assert.match(appended.content, /- \[ \] Policy optimization/u);

    const oldPlan = "## 今日计划\n\n- [ ] Old future task";
    const rewriteBefore = `---\ndate: 2026-07-17\n---\n\n- [x] Study Evidence\n\n${oldPlan}\n\nKeep this.\n`;
    const rewritten = await apply("daily_rewrite", rewriteBefore, {
        op: "replace",
        path: target,
        find: oldPlan,
        replacement: "## 今日计划\n\n- [ ] Explicitly rescheduled task",
        expectedContentHash: digest(rewriteBefore),
        expectedModifiedVersion: initialModifiedVersion(1, rewriteBefore),
    });
    assert.equal(rewritten.result.status, "succeeded");
    assert.match(rewritten.content, /- \[x\] Study Evidence/u);
    assert.doesNotMatch(rewritten.content, /Old future task/u);
    assert.match(rewritten.content, /Keep this\./u);

    const stale = await apply("daily_stale", appendBefore, {
        op: "append",
        path: target,
        content: "\n- [ ] Must not apply\n",
        expectedContentHash: digest("stale version"),
        expectedModifiedVersion: initialModifiedVersion(1, appendBefore),
    });
    assert.equal(stale.result.status, "failed");
    assert.equal(stale.result.error.code, "resource.conflict");
    assert.equal(stale.content, appendBefore);

    const noOp = await apply("daily_noop", appendBefore, {
        op: "replace",
        path: target,
        find: "Unrelated note.",
        replacement: "Unrelated note.",
        expectedContentHash: digest(appendBefore),
        expectedModifiedVersion: initialModifiedVersion(1, appendBefore),
    });
    assert.equal(noOp.result.status, "failed");
    assert.equal(noOp.result.error.code, "protocol.invalid_params");
    assert.equal(noOp.content, appendBefore);
    assert.equal(await noOp.journal.load("daily_noop"), undefined);
});

test("crash reconciliation reaches a stable rolled-back state and exact replay is idempotent", async () => {
    const { VaultChangeCoordinator, VaultChangeCrashInjectionError } = loadModule();
    const vault = new MemoryVault({ "notes/a.md": "alpha\n", "notes/b.md": "beta\n" });
    const journal = new MemoryJournal();
    const checkpoints = new MemoryCheckpoints(vault);
    let crashed = false;
    const crashing = new VaultChangeCoordinator({
        vault,
        journal,
        checkpoints,
        permissionMode: () => "trusted_vault",
        injectCrash: (point) => {
            if (!crashed && point === "after-target-write") {
                crashed = true;
                throw new VaultChangeCrashInjectionError(point);
            }
        },
    });
    const request = call("batch_crash", [
        {
            op: "append", path: "notes/a.md", content: "next\n", expectedContentHash: digest("alpha\n"),
            expectedModifiedVersion: initialModifiedVersion(1, "alpha\n"),
        },
        {
            op: "append", path: "notes/b.md", content: "next\n", expectedContentHash: digest("beta\n"),
            expectedModifiedVersion: initialModifiedVersion(2, "beta\n"),
        },
    ]);
    await assert.rejects(crashing.execute(request), VaultChangeCrashInjectionError);
    const crashedRecord = await journal.load("batch_crash");
    assert.equal(crashedRecord.state, "applying");
    assert.equal(crashedRecord.version, 2);
    assert.deepEqual(crashedRecord.appliedPaths, []);
    assert.equal(crashedRecord.targets[0].afterModifiedVersion, vault.versions.get("notes/a.md"));
    assert.equal(crashedRecord.targets[1].afterModifiedVersion, null);

    const unresolvedReplay = await crashing.execute(request);
    assert.equal(unresolvedReplay.status, "unknown_outcome");
    assert.equal(unresolvedReplay.retryable, false);

    const recovered = new VaultChangeCoordinator({
        vault, journal, checkpoints, permissionMode: () => "trusted_vault",
    });
    const report = await recovered.reconcile();
    assert.deepEqual(report, [{ batchId: "batch_crash", state: "rolled_back", manualReviewPaths: [] }]);
    assert.equal(vault.entries.get("notes/a.md"), "alpha\n");
    assert.equal(vault.entries.get("notes/b.md"), "beta\n");

    const replay = await recovered.execute(request);
    assert.equal(replay.status, "failed");
    assert.equal(replay.error.code, "resource.conflict");
});

test("lost completion acknowledgement replays the exact applied result and rejects conflicting identity", async () => {
    const { VaultChangeCoordinator } = loadModule();
    const vault = new MemoryVault({ "notes/a.md": "alpha\n" });
    const journal = new MemoryJournal();
    const checkpoints = new MemoryCheckpoints(vault);
    const coordinator = new VaultChangeCoordinator({
        vault, journal, checkpoints, permissionMode: () => "trusted_vault",
    });
    const request = call("batch_lost_ack", [
        {
            op: "append", path: "notes/a.md", content: "next\n", expectedContentHash: digest("alpha\n"),
            expectedModifiedVersion: initialModifiedVersion(1, "alpha\n"),
        },
    ]);

    const applied = await coordinator.execute(request);
    const replay = await coordinator.execute(request);
    const conflict = await coordinator.execute({ ...request, argsHash: digest("different") });

    assert.equal(applied.status, "succeeded");
    assert.deepEqual(replay, applied);
    assert.equal(conflict.status, "failed");
    assert.equal(conflict.error.code, "resource.conflict");
    assert.equal(vault.entries.get("notes/a.md"), "alpha\nnext\n");
});

test("restart reconciliation recognizes an all-applied crash and preserves exact replay", async () => {
    const { VaultChangeCoordinator, VaultChangeCrashInjectionError } = loadModule();
    const vault = new MemoryVault({ "notes/a.md": "alpha\n", "notes/b.md": "beta\n" });
    const journal = new MemoryJournal();
    const checkpoints = new MemoryCheckpoints(vault);
    const request = call("batch_all_after", [
        {
            op: "append", path: "notes/a.md", content: "next\n", expectedContentHash: digest("alpha\n"),
            expectedModifiedVersion: initialModifiedVersion(1, "alpha\n"),
        },
        {
            op: "append", path: "notes/b.md", content: "next\n", expectedContentHash: digest("beta\n"),
            expectedModifiedVersion: initialModifiedVersion(2, "beta\n"),
        },
    ]);
    const crashing = new VaultChangeCoordinator({
        vault, journal, checkpoints, permissionMode: () => "trusted_vault",
        injectCrash: (point, target) => {
            if (point === "after-target-journal" && target === "notes/b.md") {
                throw new VaultChangeCrashInjectionError(point);
            }
        },
    });
    await assert.rejects(crashing.execute(request), VaultChangeCrashInjectionError);

    const recovered = new VaultChangeCoordinator({
        vault, journal, checkpoints, permissionMode: () => "trusted_vault",
    });
    assert.deepEqual(await recovered.reconcile(), [
        { batchId: "batch_all_after", state: "applied", manualReviewPaths: [] },
    ]);
    assert.equal((await recovered.execute(request)).status, "succeeded");
    assert.equal(vault.entries.get("notes/a.md"), "alpha\nnext\n");
    assert.equal(vault.entries.get("notes/b.md"), "beta\nnext\n");
});

test("restart reconciliation reports unexpected target state for manual review without guessing", async () => {
    const { VaultChangeCoordinator, VaultChangeCrashInjectionError } = loadModule();
    const vault = new MemoryVault({ "notes/a.md": "alpha\n", "notes/b.md": "beta\n" });
    const journal = new MemoryJournal();
    const checkpoints = new MemoryCheckpoints(vault);
    let crashed = false;
    const crashing = new VaultChangeCoordinator({
        vault, journal, checkpoints, permissionMode: () => "trusted_vault",
        injectCrash: (point) => {
            if (!crashed && point === "after-target-write") {
                crashed = true;
                throw new VaultChangeCrashInjectionError(point);
            }
        },
    });
    const request = call("batch_manual", [
        {
            op: "append", path: "notes/a.md", content: "next\n", expectedContentHash: digest("alpha\n"),
            expectedModifiedVersion: initialModifiedVersion(1, "alpha\n"),
        },
        {
            op: "append", path: "notes/b.md", content: "next\n", expectedContentHash: digest("beta\n"),
            expectedModifiedVersion: initialModifiedVersion(2, "beta\n"),
        },
    ]);
    await assert.rejects(crashing.execute(request), VaultChangeCrashInjectionError);
    vault.entries.set("notes/a.md", "user changed after crash\n");

    const recovered = new VaultChangeCoordinator({
        vault, journal, checkpoints, permissionMode: () => "trusted_vault",
    });
    assert.deepEqual(await recovered.reconcile(), [
        { batchId: "batch_manual", state: "recovery_failed", manualReviewPaths: ["notes/a.md"] },
    ]);
    assert.equal(vault.entries.get("notes/a.md"), "user changed after crash\n");
    assert.equal(vault.entries.get("notes/b.md"), "beta\n");

    const gated = new VaultChangeCoordinator({
        vault, journal, checkpoints, permissionMode: () => "trusted_vault",
    });
    await assert.rejects(gated.beginRecovery(), /manual review/);
    const blocked = await gated.execute(call("batch_blocked_by_recovery", [
        {
            op: "append", path: "notes/b.md", content: "later\n", expectedContentHash: digest("beta\n"),
            expectedModifiedVersion: initialModifiedVersion(2, "beta\n"),
        },
    ]));
    assert.equal(blocked.status, "unknown_outcome");
    assert.equal(blocked.retryable, false);
    assert.equal(vault.entries.get("notes/b.md"), "beta\n");
});

test("guarded undo restores only an unchanged applied batch", async () => {
    const { VaultChangeCoordinator } = loadModule();
    const vault = new MemoryVault({ "notes/a.md": "alpha\n" });
    const journal = new MemoryJournal();
    const checkpoints = new MemoryCheckpoints(vault);
    const coordinator = new VaultChangeCoordinator({
        vault, journal, checkpoints, permissionMode: () => "trusted_vault",
    });
    const applied = await coordinator.execute(call("batch_undo", [
        {
            op: "append", path: "notes/a.md", content: "next\n", expectedContentHash: digest("alpha\n"),
            expectedModifiedVersion: initialModifiedVersion(1, "alpha\n"),
        },
    ]));
    assert.equal(applied.status, "succeeded");
    vault.entries.set("notes/a.md", "user changed\n");
    const conflict = await coordinator.undo("batch_undo");
    assert.equal(conflict.status, "conflict");
    assert.match(conflict.diff, /user changed/);

    vault.entries.set("notes/a.md", "alpha\nnext\n");
    const undone = await coordinator.undo("batch_undo");
    assert.equal(undone.status, "undone");
    assert.equal(vault.entries.get("notes/a.md"), "alpha\n");
});

test("guarded undo preserves a user edit racing the reverse compare-exchange", async () => {
    const { VaultChangeCoordinator } = loadModule();
    const vault = new MemoryVault({ "notes/a.md": "alpha\n" });
    const journal = new MemoryJournal();
    const coordinator = new VaultChangeCoordinator({
        vault,
        journal,
        checkpoints: new MemoryCheckpoints(vault),
        permissionMode: () => "trusted_vault",
    });
    const applied = await coordinator.execute(call("batch_reverse_race", [{
        op: "append",
        path: "notes/a.md",
        content: "next\n",
        expectedContentHash: digest("alpha\n"),
        expectedModifiedVersion: initialModifiedVersion(1, "alpha\n"),
    }]));
    assert.equal(applied.status, "succeeded");
    const originalApplyConditional = vault.applyConditional.bind(vault);
    let raced = false;
    vault.applyConditional = async (mutation) => {
        if (!raced && mutation.kind === "modify" && mutation.afterContent === "alpha\n") {
            raced = true;
            await vault.write(mutation.path, "user edit during undo\n");
        }
        return originalApplyConditional(mutation);
    };

    const result = await coordinator.undo("batch_reverse_race");

    assert.equal(result.status, "conflict");
    assert.deepEqual(result.paths, ["notes/a.md"]);
    assert.equal(vault.entries.get("notes/a.md"), "user edit during undo\n");
    assert.equal((await journal.load("batch_reverse_race")).state, "undoing");
    assert.deepEqual((await journal.load("batch_reverse_race")).manualReviewPaths, ["notes/a.md"]);
});

test("an unresolved guarded undo latches later writes in the current coordinator", async () => {
    const { VaultChangeCoordinator } = loadModule();
    const vault = new MemoryVault({ "notes/a.md": "alpha\n", "notes/b.md": "beta\n" });
    const journal = new MemoryJournal();
    const coordinator = new VaultChangeCoordinator({
        vault, journal, checkpoints: new MemoryCheckpoints(vault), permissionMode: () => "trusted_vault",
    });
    await coordinator.execute(call("batch_unresolved_undo", [
        {
            op: "append", path: "notes/a.md", content: "next\n", expectedContentHash: digest("alpha\n"),
            expectedModifiedVersion: initialModifiedVersion(1, "alpha\n"),
        },
    ]));
    const originalApplyConditional = vault.applyConditional.bind(vault);
    let raced = false;
    vault.applyConditional = async (mutation) => {
        if (!raced && mutation.kind === "modify" && mutation.afterContent === "alpha\n") {
            raced = true;
            await vault.write(mutation.path, "racing user edit\n");
        }
        return originalApplyConditional(mutation);
    };

    const conflict = await coordinator.undo("batch_unresolved_undo");
    vault.applyConditional = originalApplyConditional;
    const blocked = await coordinator.execute(call("batch_after_unresolved_undo", [
        {
            op: "append", path: "notes/b.md", content: "later\n", expectedContentHash: digest("beta\n"),
            expectedModifiedVersion: initialModifiedVersion(2, "beta\n"),
        },
    ]));

    assert.equal(conflict.status, "conflict");
    assert.equal((await journal.load("batch_unresolved_undo")).state, "undoing");
    assert.equal(blocked.status, "unknown_outcome");
    assert.equal(blocked.retryable, false);
    assert.equal(vault.entries.get("notes/b.md"), "beta\n");
});

test("guarded undo journals progress and completes safely after a crash", async () => {
    const { VaultChangeCoordinator, VaultChangeCrashInjectionError } = loadModule();
    const vault = new MemoryVault({ "notes/a.md": "alpha\n", "notes/b.md": "beta\n" });
    const journal = new MemoryJournal();
    const checkpoints = new MemoryCheckpoints(vault);
    let crashed = false;
    const coordinator = new VaultChangeCoordinator({
        vault, journal, checkpoints, permissionMode: () => "trusted_vault",
        injectCrash(point) {
            if (point === "after-undo-target-write" && !crashed) {
                crashed = true;
                throw new VaultChangeCrashInjectionError(point);
            }
        },
    });
    await coordinator.execute(call("batch_undo_crash", [
        {
            op: "append", path: "notes/a.md", content: "A2\n", expectedContentHash: digest("alpha\n"),
            expectedModifiedVersion: initialModifiedVersion(1, "alpha\n"),
        },
        {
            op: "append", path: "notes/b.md", content: "B2\n", expectedContentHash: digest("beta\n"),
            expectedModifiedVersion: initialModifiedVersion(2, "beta\n"),
        },
    ]));

    await assert.rejects(coordinator.undo("batch_undo_crash"), VaultChangeCrashInjectionError);
    assert.equal((await journal.load("batch_undo_crash")).state, "undoing");

    const recovered = new VaultChangeCoordinator({
        vault, journal, checkpoints, permissionMode: () => "trusted_vault",
    });
    assert.deepEqual(await recovered.reconcile(), [
        { batchId: "batch_undo_crash", state: "undone", manualReviewPaths: [] },
    ]);
    assert.equal(vault.entries.get("notes/a.md"), "alpha\n");
    assert.equal(vault.entries.get("notes/b.md"), "beta\n");
    assert.equal((await journal.load("batch_undo_crash")).state, "undone");
});

test("Git checkpoints use an independent index and preserve staged and unstaged state", async (t) => {
    const { GitCheckpointStore } = loadModule();
    const root = await mkdtemp(path.join(os.tmpdir(), "offeragent-checkpoint-"));
    t.after(() => rm(root, { recursive: true, force: true }));
    await mkdir(path.join(root, "notes"), { recursive: true });
    await writeFile(path.join(root, "notes", "a.md"), "base\n");
    await writeFile(path.join(root, "staged.md"), "base staged\n");
    await execFileAsync("git", ["-C", root, "init", "-q"]);
    await execFileAsync("git", ["-C", root, "config", "user.name", "OfferAgent Test"]);
    await execFileAsync("git", ["-C", root, "config", "user.email", "test@example.invalid"]);
    await execFileAsync("git", ["-C", root, "add", "."]);
    await execFileAsync("git", ["-C", root, "commit", "-qm", "base"]);
    await writeFile(path.join(root, "notes", "a.md"), "working\n");
    await writeFile(path.join(root, "staged.md"), "staged user change\n");
    await execFileAsync("git", ["-C", root, "add", "staged.md"]);
    const before = (await execFileAsync("git", ["-C", root, "status", "--porcelain=v1"])).stdout;

    const store = new GitCheckpointStore(root);
    const ref = await store.create("batch_git", ["notes/a.md"]);

    assert.equal(await store.read(ref, "notes/a.md"), "working\n");
    const after = (await execFileAsync("git", ["-C", root, "status", "--porcelain=v1"])).stdout;
    assert.equal(after, before);
    assert.equal((await readFile(path.join(root, ".git", "HEAD"), "utf8")).includes("offeragent"), false);
});

test("file journal atomically replaces durable state and rejects unsafe recovery records", async (t) => {
    const { FileVaultChangeJournal } = loadModule();
    const root = await mkdtemp(path.join(os.tmpdir(), "offeragent-journal-"));
    t.after(() => rm(root, { recursive: true, force: true }));
    const store = new FileVaultChangeJournal(root);
    const base = {
        version: 1,
        batchId: "batch_journal",
        toolCallId: "call_journal",
        workspaceId: "ws_vault",
        runId: "run_journal",
        argsHash: digest("arguments"),
        idempotencyKey: "idem_journal",
        state: "prepared",
        checkpointRef: null,
        targets: [{
            operation: "append",
            path: "notes/a.md",
            beforeHash: digest("before"),
            afterHash: digest("after"),
        }],
        appliedPaths: [],
        manualReviewPaths: [],
    };

    await store.save(base);
    await store.save({
        ...base,
        state: "applying",
        checkpointRef: "refs/offeragent/checkpoints/batch_journal",
        appliedPaths: ["notes/a.md"],
    });

    assert.equal((await store.load("batch_journal")).state, "applying");
    await writeFile(path.join(root, "batch_unsafe.json"), JSON.stringify({
        ...base,
        batchId: "batch_unsafe",
        targets: [{ ...base.targets[0], path: "../outside.md" }],
    }));
    await assert.rejects(store.load("batch_unsafe"), /malformed/);
    await rm(path.join(root, "batch_unsafe.json"));

    const reservation = {
        ...base,
        version: 2,
        batchId: "batch_interview_reservation",
        toolCallId: "call_interview_reservation",
        runId: "run_interview_child",
        rootRunId: "run_interview_root",
        changeKind: "interview_submission",
        reviewHash: digest("complete review"),
        state: "rejected",
        targets: [{
            ...base.targets[0],
            beforeModifiedVersion: "mtime:1:size:6",
            afterModifiedVersion: null,
        }],
    };
    await store.save(reservation);
    assert.deepEqual(
        await store.findInterviewSubmissionByRootRun("run_interview_root"),
        reservation,
    );
    assert.equal(await store.findInterviewSubmissionByRootRun("another_root"), undefined);
});

test("file journal publishes the exact Worker recovery token without exposing the marker as a batch", async (t) => {
    const { FileVaultChangeJournal } = loadModule();
    const root = await mkdtemp(path.join(os.tmpdir(), "offeragent-recovery-marker-"));
    t.after(() => rm(root, { recursive: true, force: true }));
    const store = new FileVaultChangeJournal(root);
    const recoveryToken = "a".repeat(64);

    await store.markRecoveryReady(recoveryToken);

    assert.equal(
        await readFile(path.join(root, ".recovery-ready.json"), "utf8"),
        `${JSON.stringify({ schemaVersion: 2, recoveryToken })}\n`,
    );
    assert.deepEqual(await store.listUnresolved(), []);
});

test("file journal recovery marker seals every durable batch record by exact bytes", async (t) => {
    const { FileVaultChangeJournal } = loadModule();
    const root = await mkdtemp(path.join(os.tmpdir(), "offeragent-recovery-manifest-"));
    t.after(() => rm(root, { recursive: true, force: true }));
    const store = new FileVaultChangeJournal(root);
    const record = {
        version: 2,
        batchId: "batch_recovery_manifest",
        toolCallId: "call_recovery_manifest",
        workspaceId: "ws_vault",
        runId: "run_recovery_manifest",
        rootRunId: "run_recovery_manifest",
        changeKind: "interview_submission",
        reviewHash: digest("review"),
        argsHash: digest("arguments"),
        idempotencyKey: "idem_recovery_manifest",
        state: "prepared",
        checkpointRef: null,
        targets: [{
            operation: "append",
            path: "notes/a.md",
            beforeHash: digest("before"),
            afterHash: digest("after"),
            beforeModifiedVersion: "mtime:1:size:6",
            afterModifiedVersion: null,
        }],
        appliedPaths: [],
        manualReviewPaths: [],
    };
    await store.save(record);
    const recordBytes = await readFile(path.join(root, "batch_recovery_manifest.json"), "utf8");
    const recoveryToken = "b".repeat(64);

    await store.markRecoveryReady(recoveryToken);

    assert.deepEqual(JSON.parse(await readFile(path.join(root, ".recovery-ready.json"), "utf8")), {
        schemaVersion: 2,
        recoveryToken,
    });
    assert.deepEqual(
        JSON.parse(await readFile(path.join(
            root,
            ".recovery-seals",
            "current",
            `${digest("batch_recovery_manifest").slice("sha256:".length)}.json`,
        ), "utf8")),
        {
            schemaVersion: 1,
            recoveryToken,
            batchId: "batch_recovery_manifest",
            contentHash: digest(recordBytes),
            byteLength: Buffer.byteLength(recordBytes),
        },
    );
});

test("file journal refuses to publish a malformed Worker recovery token", async (t) => {
    const { FileVaultChangeJournal } = loadModule();
    const root = await mkdtemp(path.join(os.tmpdir(), "offeragent-invalid-recovery-marker-"));
    t.after(() => rm(root, { recursive: true, force: true }));
    const store = new FileVaultChangeJournal(root);

    await assert.rejects(store.markRecoveryReady("A".repeat(64)), /recovery token is invalid/);
    await assert.rejects(readFile(path.join(root, ".recovery-ready.json")), /ENOENT/);
});

test("file journal migrates the plugin-local journal idempotently and fails closed on conflicts", async (t) => {
    const { FileVaultChangeJournal } = loadModule();
    const root = await mkdtemp(path.join(os.tmpdir(), "offeragent-journal-migration-"));
    t.after(() => rm(root, { recursive: true, force: true }));
    const legacyDirectory = path.join(root, "plugin", "vault-change-journal");
    const stableDirectory = path.join(root, ".obsidian", "offeragent", "vault-change-journal");
    const legacy = new FileVaultChangeJournal(legacyDirectory);
    const stable = new FileVaultChangeJournal(stableDirectory);
    const base = {
        version: 1,
        batchId: "batch_migrated",
        toolCallId: "call_migrated",
        workspaceId: "ws_vault",
        runId: "run_migrated",
        argsHash: digest("arguments"),
        idempotencyKey: "idem_migrated",
        state: "applying",
        checkpointRef: "refs/offeragent/checkpoints/batch_migrated",
        targets: [{
            operation: "append",
            path: "notes/a.md",
            beforeHash: digest("before"),
            afterHash: digest("after"),
        }],
        appliedPaths: ["notes/a.md"],
        manualReviewPaths: [],
    };
    await legacy.save(base);

    await stable.migrateLegacyDirectory(legacyDirectory);
    await stable.migrateLegacyDirectory(legacyDirectory);

    assert.deepEqual(await stable.load("batch_migrated"), base);
    await assert.rejects(readFile(path.join(legacyDirectory, "batch_migrated.json")), /ENOENT/);

    await legacy.save({ ...base, state: "recovery_failed", manualReviewPaths: ["notes/a.md"] });
    await assert.rejects(stable.migrateLegacyDirectory(legacyDirectory), /conflicts with stable journal/);
    assert.equal((await stable.load("batch_migrated")).state, "applying");
    assert.equal((await legacy.load("batch_migrated")).state, "recovery_failed");
});
