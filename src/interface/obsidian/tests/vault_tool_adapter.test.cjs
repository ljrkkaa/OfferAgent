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

function call() {
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
    };
}

test("Vault Tool Adapter reads agent.md and completes the bound plugin call", async () => {
    const { VaultToolAdapter } = loadModule();
    const requests = [];
    const file = { path: "agent.md" };
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
    const dispose = observePluginToolEvents(events, {
        execute: async (candidate) => {
            observed.push(candidate);
            return { accepted: true, replayed: false };
        },
    }, (error) => { throw error; });

    listener({ type: "tool.started", payload: { call: { ...call(), executorLocation: "local" } } });
    listener({ type: "tool.started", payload: { call: call() } });
    await new Promise((resolve) => setImmediate(resolve));

    assert.equal(observed.length, 1);
    assert.equal(observed[0].toolCallId, "call_contract");
    dispose();
    assert.equal(listener, undefined);
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
