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

function call(batchId, operations, overrides = {}) {
    const arguments_ = { batchId, task: `Apply ${batchId}`, operations };
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

class MemoryVault {
    constructor(entries) {
        this.entries = new Map(Object.entries(entries));
        this.failPath = null;
    }
    async read(target) { return this.entries.has(target) ? this.entries.get(target) : undefined; }
    async write(target, content) {
        if (target === this.failPath) throw new Error(`injected write failure: ${target}`);
        this.entries.set(target, content);
    }
    async remove(target) { this.entries.delete(target); }
}

class MemoryJournal {
    constructor() { this.records = new Map(); }
    async load(batchId) { return structuredClone(this.records.get(batchId)); }
    async save(record) { this.records.set(record.batchId, structuredClone(record)); }
    async listUnresolved() {
        return [...this.records.values()].filter((record) => ["prepared", "applying", "recovery_failed"].includes(record.state))
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
        { op: "append", path: "notes/a.md", content: "next\n", expectedContentHash: digest("alpha\n") },
        { op: "replace", path: "notes/b.md", find: "beta", replacement: "changed", expectedContentHash: digest("beta\n") },
    ]));

    assert.equal(result.status, "failed");
    assert.equal(result.error.code, "tool.failed");
    assert.equal(vault.entries.get("notes/a.md"), "alpha\n");
    assert.equal(vault.entries.get("notes/b.md"), "beta\n");
    assert.equal((await journal.load("batch_failure")).state, "rolled_back");
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
        authorize: async () => { approvals += 1; return true; },
    });
    const denied = await readOnly.execute(call("batch_denied", [
        { op: "append", path: "agent.md", content: "new\n", expectedContentHash: digest("old contract\n") },
    ]));
    assert.equal(denied.status, "denied");
    assert.equal(approvals, 0);

    const trusted = new VaultChangeCoordinator({
        vault, journal, checkpoints, permissionMode: () => "trusted_vault",
        authorize: async (proposal) => {
            approvals += 1;
            assert.match(proposal.diff, /memory\/user\/old\.md/);
            return true;
        },
    });
    const applied = await trusted.execute(call("batch_memory_delete", [
        { op: "delete", path: "memory/user/old.md", expectedContentHash: digest(memoryTopic()) },
        {
            op: "replace", path: "memory/MEMORY.md", find: "[[memory/user/old]]\n", replacement: "",
            expectedContentHash: digest("[[memory/user/old]]\n"),
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
            { op: "delete", path: "memory/user/old.md", expectedContentHash: digest(topic) },
            {
                op: "replace", path: "memory/MEMORY.md", find: index, replacement: "",
                expectedContentHash: digest(index),
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
        { op: "append", path: "notes/a.md", content: "next\n", expectedContentHash: digest("alpha\n") },
        { op: "append", path: "notes/b.md", content: "next\n", expectedContentHash: digest("beta\n") },
    ]);
    await assert.rejects(crashing.execute(request), VaultChangeCrashInjectionError);
    assert.equal((await journal.load("batch_crash")).state, "applying");

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
        { op: "append", path: "notes/a.md", content: "next\n", expectedContentHash: digest("alpha\n") },
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
        { op: "append", path: "notes/a.md", content: "next\n", expectedContentHash: digest("alpha\n") },
        { op: "append", path: "notes/b.md", content: "next\n", expectedContentHash: digest("beta\n") },
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
        { op: "append", path: "notes/a.md", content: "next\n", expectedContentHash: digest("alpha\n") },
        { op: "append", path: "notes/b.md", content: "next\n", expectedContentHash: digest("beta\n") },
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
        { op: "append", path: "notes/b.md", content: "later\n", expectedContentHash: digest("beta\n") },
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
        { op: "append", path: "notes/a.md", content: "next\n", expectedContentHash: digest("alpha\n") },
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
