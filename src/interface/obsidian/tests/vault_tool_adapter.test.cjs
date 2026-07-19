const assert = require("node:assert/strict");
const path = require("node:path");
const test = require("node:test");
const { buildSync } = require("esbuild");

function loadModule() {
    const output = buildSync({
        entryPoints: [path.join(__dirname, "../src/runtime/vault_tool_adapter.ts")],
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

const DIGEST = `sha256:${"a".repeat(64)}`;

function call(overrides = {}) {
    return {
        toolCallId: "call_contract",
        workspaceId: "ws_vault",
        runId: "run_contract",
        name: "agent_contract.read",
        version: "1",
        arguments: {},
        argsHash: DIGEST,
        idempotencyKey: "contract-read-1",
        risk: "read",
        reason: null,
        agentLineage: ["run_contract"],
        executorLocation: "plugin",
        definitionFingerprint: DIGEST,
        resultSensitivity: "workspace",
        deadline: "2026-07-17T12:00:00+00:00",
        ...overrides,
    };
}

test("Vault Tool Adapter reads agent.md and completes the bound plugin call", async () => {
    const { VaultToolAdapter } = loadModule();
    const requests = [];
    const file = { path: "agent.md", extension: "md", stat: { mtime: 16, size: 37 } };
    const vault = {
        getFileByPath: (target) => target === "agent.md" ? file : null,
        cachedRead: async (target) => {
            assert.equal(target, file);
            return "# OfferAgent\n\nUse Vault evidence.";
        },
    };
    const client = {
        request: async (method, params) => {
            requests.push({ method, params });
            return { accepted: true, replayed: false };
        },
    };
    const adapter = new VaultToolAdapter(vault, client, "ws_vault");

    const response = await adapter.execute(call());

    assert.deepEqual(response, { accepted: true, replayed: false });
    assert.equal(requests.length, 1);
    assert.equal(requests[0].method, "plugin-tools/complete");
    assert.equal(requests[0].params.workspaceId, "ws_vault");
    assert.equal(requests[0].params.runId, "run_contract");
    assert.equal(requests[0].params.result.status, "succeeded");
    assert.equal(requests[0].params.result.data.content, "# OfferAgent\n\nUse Vault evidence.");
    assert.match(requests[0].params.result.data.contentHash, /^sha256:[0-9a-f]{64}$/);
    assert.equal(requests[0].params.result.sourceRefs[0].file.path, "agent.md");
});

test("plugin tool event observer delegates only plugin-owned started calls", async () => {
    const { observePluginToolEvents } = loadModule();
    let listener;
    const events = {
        subscribe: (candidate) => {
            listener = candidate;
            return () => { listener = undefined; };
        },
    };
    const observed = [];
    const cancelled = [];
    const observer = observePluginToolEvents(events, {
        execute: async (candidate) => {
            observed.push(candidate);
            return { accepted: true, replayed: false };
        },
        cancelRun: (runId) => cancelled.push(runId),
    }, (error) => { throw error; });

    listener({ type: "tool.started", payload: { call: call({ toolCallId: "call_replayed" }) } }, undefined, "replay");
    listener({ type: "turn.interrupted", runId: "run_replayed", payload: {} }, undefined, "replay");
    listener({ type: "tool.started", payload: { call: { ...call(), executorLocation: "local" } } }, undefined, "live");
    listener({ type: "tool.started", payload: { call: call() } });
    listener({ type: "turn.interrupted", runId: "run_contract", payload: {} });
    await new Promise((resolve) => setImmediate(resolve));

    assert.equal(observed.length, 1);
    assert.equal(observed[0].toolCallId, "call_contract");
    assert.deepEqual(cancelled, ["run_contract"]);
    await observer.dispose();
    assert.equal(listener, undefined);
});

test("plugin tool event observer unsubscribes immediately and drains in-flight executions", async () => {
    const { observePluginToolEvents } = loadModule();
    let listener;
    let releaseExecution;
    let executionStarted = false;
    const executionGate = new Promise((resolve) => { releaseExecution = resolve; });
    const observer = observePluginToolEvents({
        subscribe: (candidate) => {
            listener = candidate;
            return () => { listener = undefined; };
        },
    }, {
        execute: async () => {
            executionStarted = true;
            await executionGate;
            return { accepted: true, replayed: false };
        },
    }, (error) => { throw error; });

    listener({ type: "tool.started", payload: { call: call() } });
    await new Promise((resolve) => setImmediate(resolve));
    assert.equal(executionStarted, true);

    let drained = false;
    const draining = observer.dispose().then(() => { drained = true; });
    assert.equal(listener, undefined);
    await new Promise((resolve) => setImmediate(resolve));
    assert.equal(drained, false);

    releaseExecution();
    await draining;
    assert.equal(drained, true);
});

test("same-process Vault execution fence serializes replacement adapters and recovery", async () => {
    const { SerializedPluginToolExecutionFence } = loadModule();
    const events = [];
    let releaseFirst;
    const firstGate = new Promise((resolve) => { releaseFirst = resolve; });
    const first = new SerializedPluginToolExecutionFence("C:/vault", {
        execute: async () => {
            events.push("first:start");
            await firstGate;
            events.push("first:end");
            return { accepted: true, replayed: false };
        },
    }, async () => { events.push("first:recover"); });
    const firstExecution = first.execute(call({ toolCallId: "call_first" }));
    await new Promise((resolve) => setImmediate(resolve));

    const second = new SerializedPluginToolExecutionFence("c:\\VAULT", {
        execute: async () => {
            events.push("second:execute");
            return { accepted: true, replayed: false };
        },
    }, async () => { events.push("second:recover"); });
    const secondExecution = second.execute(call({ toolCallId: "call_second" }));
    await new Promise((resolve) => setImmediate(resolve));
    assert.deepEqual(events, ["first:recover", "first:start"]);

    releaseFirst();
    await Promise.all([firstExecution, secondExecution]);
    assert.deepEqual(events, ["first:recover", "first:start", "first:end", "second:recover", "second:execute"]);
});

test("Vault Tool Adapter rejects a cross-Vault call before reading or completing", async () => {
    const { VaultToolAdapter } = loadModule();
    let reads = 0;
    let requests = 0;
    const adapter = new VaultToolAdapter({
        getFileByPath: () => { reads += 1; return { path: "agent.md" }; },
        cachedRead: async () => "# OfferAgent",
    }, {
        request: async () => { requests += 1; return { accepted: true, replayed: false }; },
    }, "ws_vault");

    await assert.rejects(adapter.execute({ ...call(), workspaceId: "ws_other" }), /another Vault/);
    assert.equal(reads, 0);
    assert.equal(requests, 0);
});

test("Vault Tool Adapter routes plugin-owned Vault Change completion through the bound coordinator", async () => {
    const { VaultToolAdapter } = loadModule();
    const requests = [];
    const calls = [];
    const result = {
        toolCallId: "call_change",
        status: "succeeded",
        summary: "Applied one Vault Change Batch.",
        data: {
            batchId: "batch_adapter",
            state: "applied",
            checkpointRef: "refs/offeragent/checkpoints/batch_adapter",
            paths: ["notes/a.md"],
            beforeStateHash: DIGEST,
            afterStateHash: `sha256:${"b".repeat(64)}`,
            undoAvailable: true,
        },
        sideEffects: [],
        retryable: false,
    };
    const adapter = new VaultToolAdapter({
        getFiles: () => [],
        getFileByPath: () => null,
        cachedRead: async () => "",
    }, {
        request: async (method, params) => {
            requests.push({ method, params });
            return { accepted: true, replayed: false };
        },
    }, "ws_vault", undefined, undefined, {
        execute: async (candidate) => { calls.push(candidate); return result; },
    });
    const writeCall = call({
        toolCallId: "call_change",
        name: "vault.changes.apply",
        risk: "write",
        arguments: {
            batchId: "batch_adapter",
            task: "Append a note",
            operations: [{ op: "append", path: "notes/a.md", content: "next", expectedContentHash: DIGEST }],
        },
    });

    const response = await adapter.execute(writeCall);

    assert.deepEqual(response, { accepted: true, replayed: false });
    assert.equal(calls.length, 1);
    assert.equal(calls[0], writeCall);
    assert.equal(requests.length, 1);
    assert.equal(requests[0].params.result, result);
});

test("plugin tool event observer reports malformed plugin calls without executing them", async () => {
    const { observePluginToolEvents } = loadModule();
    let listener;
    const errors = [];
    let executions = 0;
    observePluginToolEvents({ subscribe: (candidate) => { listener = candidate; return () => undefined; } }, {
        execute: async () => { executions += 1; return { accepted: true, replayed: false }; },
    }, (error) => errors.push(error));

    listener({ type: "tool.started", payload: { call: { executorLocation: "plugin" } } });

    assert.equal(executions, 0);
    assert.equal(errors.length, 1);
    assert.match(errors[0].message, /toolCallId/);
});

test("Vault Tool Adapter lists, searches, then precisely reads current bounded evidence", async () => {
    const { VaultToolAdapter } = loadModule();
    const source = {
        path: "notes/source.md",
        extension: "md",
        stat: { mtime: 17, size: 31 },
    };
    const excluded = {
        path: ".obsidian/private.md",
        extension: "md",
        stat: { mtime: 18, size: 10 },
    };
    const contract = {
        path: "agent.md",
        extension: "md",
        stat: { mtime: 19, size: 10 },
    };
    const content = "heading\nPrecise evidence line\ntail";
    const files = [source, excluded, contract];
    const requests = [];
    const adapter = new VaultToolAdapter({
        getFiles: () => files,
        getFileByPath: (target) => files.find((file) => file.path === target) ?? null,
        cachedRead: async (file) => file === source ? content : "private",
    }, {
        request: async (method, params) => {
            requests.push({ method, params });
            return { accepted: true, replayed: false };
        },
    }, "ws_vault", {
        getFileCache: (file) => file === source ? { headings: [{ heading: "Evidence", level: 1 }], tags: [] } : null,
    });

    await adapter.execute(call({
        toolCallId: "call_list",
        name: "vault.list",
        arguments: { directory: "notes", limit: 10 },
    }));
    await adapter.execute(call({
        toolCallId: "call_search",
        name: "vault.search",
        arguments: { query: "evidence", limit: 10, snippetsPerFile: 2, snippetMaxBytes: 128 },
    }));
    const search = requests[1].params.result;
    await adapter.execute(call({
        toolCallId: "call_read",
        name: "vault.read",
        arguments: {
            path: "notes/source.md",
            lineStart: 2,
            lineEnd: 2,
            expectedContentHash: search.data.entries[0].contentHash,
            expectedModifiedVersion: search.data.entries[0].modifiedVersion,
        },
    }));

    assert.deepEqual(requests[0].params.result.data.entries.map((entry) => entry.path), ["notes/source.md"]);
    assert.equal(search.data.entries[0].snippets[0].lineStart, 2);
    assert.equal(requests[2].params.result.data.content, "Precise evidence line");
    assert.equal(requests[2].params.result.sourceRefs[0].file.path, "notes/source.md");
    assert.equal(requests[2].params.result.sourceRefs[0].file.lineStart, 2);
    assert.equal(requests[2].params.result.sourceRefs[0].file.lineEnd, 2);
});

test("Local Skill resources stay inside the selected Skill and must be directly referenced", async () => {
    const { VaultToolAdapter } = loadModule();
    const skill = { path: ".codex/skills/review/SKILL.md", extension: "md", stat: { mtime: 20, size: 40 } };
    const resource = { path: ".codex/skills/review/references/checks.md", extension: "md", stat: { mtime: 21, size: 12 } };
    const files = [skill, resource];
    const requests = [];
    const adapter = new VaultToolAdapter({
        getFiles: () => files,
        getFileByPath: (target) => files.find((file) => file.path === target) ?? null,
        cachedRead: async (file) => file === skill
            ? "# Review\n\n[Checks](references/checks.md)"
            : "Use evidence.",
    }, {
        request: async (_method, params) => { requests.push(params); return { accepted: true, replayed: false }; },
    }, "ws_vault");

    await adapter.execute(call({
        toolCallId: "call_skill",
        name: "skill.read",
        arguments: { skill: "review", resource: "references/checks.md" },
    }));
    await adapter.execute(call({
        toolCallId: "call_skill_escape",
        name: "skill.read",
        arguments: { skill: "review", resource: "../secret.md" },
    }));

    assert.equal(requests[0].result.status, "succeeded");
    assert.equal(requests[0].result.data.content, "Use evidence.");
    assert.equal(requests[1].result.status, "failed");
});

test("Daily Note Context resolves local configuration and template without creating the target", async () => {
    const { VaultToolAdapter } = loadModule();
    const template = { path: "templates/daily.md", extension: "md", stat: { mtime: 22, size: 18 } };
    const requests = [];
    const adapter = new VaultToolAdapter({
        getFiles: () => [template],
        getFileByPath: (target) => target === template.path ? template : null,
        cachedRead: async () => "# {{date}}\n\n- [ ]",
    }, {
        request: async (_method, params) => { requests.push(params); return { accepted: true, replayed: false }; },
    }, "ws_vault", undefined, {
        dailyNotes: {
            resolveToday: () => "2026-07-17",
            readConfiguration: async () => ({ folder: "daily", format: "YYYY-MM-DD", template: "templates/daily" }),
            formatDate: (date) => date,
        },
    });

    await adapter.execute(call({
        toolCallId: "call_daily",
        name: "daily_note.context",
        arguments: {},
    }));

    const result = requests[0].result;
    assert.equal(result.status, "succeeded");
    assert.equal(result.data.resolvedDate, "2026-07-17");
    assert.equal(result.data.targetPath, "daily/2026-07-17.md");
    assert.equal(result.data.targetExists, false);
    assert.equal(result.data.templatePath, "templates/daily.md");
    assert.equal(result.data.templateContent, "# {{date}}\n\n- [ ]");
});

test("Planning Memory lists only bounded topic metadata then reads at most five exact topics", async () => {
    const { VaultToolAdapter } = loadModule();
    const topic = (path, mtime, content) => ({
        file: { path, extension: "md", stat: { mtime, size: Buffer.byteLength(content, "utf8") } },
        content,
    });
    const entries = [
        topic("memory/study/agentic-rl.md", 32,
            '---\nname: "Agentic RL"\ndescription: "Cross-day study sequence"\ntype: study\n---\n\nContinue reward modeling.\n'),
        topic("memory/user/collaboration.md", 31,
            '---\nname: "Collaboration"\ndescription: "Preferred working style"\ntype: user\n---\n\nLead with outcomes.\n'),
        topic("memory/study/invalid.md", 33, "---\nname: Invalid\ntype: study\n---\n\nmissing description\n"),
        topic("memory/MEMORY.md", 34, "# Planning Memory\n\nDo not inject this index.\n"),
    ];
    const byPath = new Map(entries.map(({ file, content }) => [file.path, { file, content }]));
    const reads = [];
    const requests = [];
    const adapter = new VaultToolAdapter({
        getFiles: () => entries.map(({ file }) => file),
        getFileByPath: (target) => byPath.get(target)?.file ?? null,
        cachedRead: async (file) => { reads.push(file.path); return byPath.get(file.path).content; },
    }, {
        request: async (_method, params) => { requests.push(params); return { accepted: true, replayed: false }; },
    }, "ws_vault");

    await adapter.execute(call({
        toolCallId: "call_memory_list",
        name: "planning_memory.list",
        arguments: {},
    }));
    const listed = requests[0].result;
    assert.equal(listed.status, "succeeded");
    assert.deepEqual(listed.data.topics.map(({ path }) => path), [
        "memory/study/agentic-rl.md",
        "memory/user/collaboration.md",
    ]);
    assert.equal(JSON.stringify(listed.data).includes("Continue reward modeling"), false);
    assert.equal(reads.includes("memory/MEMORY.md"), false);
    assert.deepEqual(listed.sourceRefs, []);

    await adapter.execute(call({
        toolCallId: "call_memory_read",
        name: "planning_memory.read",
        arguments: {
            topics: [listed.data.topics[1], listed.data.topics[0]].map((item) => ({
                path: item.path,
                expectedModifiedVersion: item.modifiedVersion,
                expectedContentHash: item.contentHash,
            })),
        },
    }));
    const recalled = requests[1].result;
    assert.deepEqual(recalled.data.topics.map(({ path }) => path), [
        "memory/user/collaboration.md",
        "memory/study/agentic-rl.md",
    ]);
    assert.match(recalled.data.topics[0].content, /Lead with outcomes/);
    assert.deepEqual(recalled.sourceRefs.map(({ file }) => file.path), [
        "memory/user/collaboration.md",
        "memory/study/agentic-rl.md",
    ]);

    await adapter.execute(call({
        toolCallId: "call_memory_overflow",
        name: "planning_memory.read",
        arguments: {
            topics: Array.from({ length: 6 }, (_, index) => ({
                path: `memory/study/topic-${index}.md`,
                expectedModifiedVersion: `mtime:${index}:size:1`,
                expectedContentHash: `sha256:${"a".repeat(64)}`,
            })),
        },
    }));
    assert.equal(requests[2].result.status, "failed");
    assert.match(requests[2].result.error.userVisibleMessage, /one to five/i);

    byPath.get("memory/study/agentic-rl.md").file.stat.mtime += 1;
    await adapter.execute(call({
        toolCallId: "call_memory_stale",
        name: "planning_memory.read",
        arguments: {
            topics: [{
                path: listed.data.topics[0].path,
                expectedModifiedVersion: listed.data.topics[0].modifiedVersion,
                expectedContentHash: listed.data.topics[0].contentHash,
            }],
        },
    }));
    assert.equal(requests[3].result.status, "failed");
    assert.equal(requests[3].result.error.code, "resource.conflict");
});
