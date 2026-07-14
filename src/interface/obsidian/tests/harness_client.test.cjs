const assert = require("node:assert/strict");
const path = require("node:path");
const test = require("node:test");
const { buildSync } = require("esbuild");

function loadModule(entry) {
    const output = buildSync({
        entryPoints: [path.join(__dirname, `../src/runtime/${entry}`)],
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

class MemoryChannel {
    constructor() {
        this.data = new Set();
        this.closeListeners = new Set();
        this.peer = null;
        this.isClosed = false;
    }
    connect(peer) { this.peer = peer; }
    async write(data) {
        if (this.isClosed) throw new Error("closed");
        const copy = Buffer.from(data);
        queueMicrotask(() => { for (const listener of this.peer?.data ?? []) listener(copy); });
    }
    async close() {
        if (this.isClosed) return;
        this.isClosed = true;
        for (const listener of this.closeListeners) listener();
        if (this.peer && !this.peer.isClosed) {
            this.peer.isClosed = true;
            for (const listener of this.peer.closeListeners) listener();
        }
    }
    onData(listener) { this.data.add(listener); return () => this.data.delete(listener); }
    onClose(listener) { this.closeListeners.add(listener); return () => this.closeListeners.delete(listener); }
}

function pair() {
    const left = new MemoryChannel();
    const right = new MemoryChannel();
    left.connect(right); right.connect(left);
    return [left, right];
}

const SCHEMA_HASH = `sha256:${"a".repeat(64)}`;
const WORKSPACE = "wsi_01J00000000000000000000000";

function context() {
    return {
        workspaceId: WORKSPACE,
        identity: {
            protocolVersion: "1.0",
            minimumProtocolVersion: "1.0",
            maximumProtocolVersion: "1.0",
            schemaHash: SCHEMA_HASH,
            clientVersion: "2.1.0",
        },
        requiredCapabilities: ["clientTools", "eventReplay", "multiSession"],
    };
}

function initializeResult(overrides = {}) {
    return {
        protocolVersion: "1.0",
        supportedProtocolRange: { minimum: "1.0", maximum: "1.0" },
        runtimeVersion: "2.1.0",
        coreVersion: "2.1.0",
        schemaHash: SCHEMA_HASH,
        workspaceId: WORKSPACE,
        workspaceInstanceId: "wsi_01J00000000000000000000001",
        hostPid: 123,
        workerPid: 456,
        transport: "windows-named-pipe",
        runtimeArch: "win-x64",
        capabilities: {
            clientTools: true,
            eventReplay: true,
            multiSession: true,
            subagents: true,
        },
        buildCommit: "abcdef0123456789",
        ...overrides,
    };
}

test("complete local Runtime requires structural Subagent protocol support independent of enablement", () => {
    const { CLIENT_CAPABILITIES, REQUIRED_RUNTIME_CAPABILITIES } = loadModule("harness_client.ts");
    assert.equal(CLIENT_CAPABILITIES.subagents, true);
    assert.equal(REQUIRED_RUNTIME_CAPABILITIES.includes("subagents"), true);
    for (const required of ["clientTools", "eventReplay", "multiSession", "loopbackWeb", "diagnostics"]) {
        assert.equal(REQUIRED_RUNTIME_CAPABILITIES.includes(required), true, `${required} must remain required`);
    }
});

function runEvent(sequence, delta) {
    return {
        protocolVersion: "1.0",
        schemaVersion: "1",
        eventId: `evt_${String(sequence).padStart(26, "0")}`,
        sequence,
        timestamp: "2026-07-13T02:00:00+00:00",
        traceId: "trc_01J00000000000000000000000",
        workspaceId: WORKSPACE,
        sessionId: "ses_01J00000000000000000000000",
        turnId: "trn_01J00000000000000000000000",
        runId: "run_01J00000000000000000000000",
        rootRunId: "run_01J00000000000000000000000",
        parentRunId: null,
        type: "assistant.delta",
        payload: { blockIndex: 0, offset: sequence === 1 ? 0 : 3, delta },
    };
}

test("client performs strict initialize and serves reverse Client Tools on the same peer", async () => {
    const { HarnessClient } = loadModule("harness_client.ts");
    const { JsonRpcPeer } = loadModule("json_rpc.ts");
    const [clientChannel, serverChannel] = pair();
    const clientPeer = new JsonRpcPeer(clientChannel);
    const serverPeer = new JsonRpcPeer(serverChannel);
    let initializeParams;
    serverPeer.register("initialize", async (params) => {
        initializeParams = params;
        return initializeResult();
    });
    serverPeer.register("runtime/ping", async (params) => ({
        nonce: params.nonce,
        timestamp: "2026-07-13T02:00:00+00:00",
        workerPid: 456,
    }));
    const reverseCalls = [];
    const client = new HarnessClient(
        { connect: async () => clientPeer },
        context(),
        {
            contextGet: async (params) => { reverseCalls.push(["context", params]); return { context: {}, capturedAt: "2026-07-13T02:00:00+00:00" }; },
            toolPreview: async (params) => { reverseCalls.push(["preview", params]); return { stateHash: `sha256:${"a".repeat(64)}` }; },
            toolCommitObserve: async (params) => { reverseCalls.push(["commit-observe", params]); return { paths: params.paths }; },
            toolInvoke: async (params) => { reverseCalls.push(["invoke", params]); return { status: "succeeded", actualOperations: [] }; },
            toolLookup: async (params) => { reverseCalls.push(["lookup", params]); return { invocationId: params.invocationId, found: false, result: null }; },
            toolCancel: async () => ({ invocationId: "inv_1", accepted: true, alreadyTerminal: false }),
            approvalPresent: async () => ({ approvalId: "apr_1", presented: true }),
        },
        { pingIntervalMs: 10_000 },
    );

    const identity = await client.connect();
    assert.equal(identity.workerPid, 456);
    assert.equal(identity.capabilities.subagents, true);
    assert.equal(initializeParams.schemaHash, SCHEMA_HASH);
    assert.equal("memory" in initializeParams.capabilities, false);
    assert.equal(initializeParams.capabilities.skills, true);
    assert.equal(initializeParams.capabilities.shell, true);
    assert.equal(initializeParams.capabilities.hooks, true);
    assert.equal(initializeParams.capabilities.headlessVaultWrite, true);
    const contextResult = await serverPeer.request("client/context/get", { fields: ["activeFile"] });
    assert.equal(contextResult.capturedAt, "2026-07-13T02:00:00+00:00");
    assert.equal(reverseCalls[0][0], "context");
    await serverPeer.request("client/tool/preview", { invocationId: "inv_01" });
    assert.equal(reverseCalls[1][0], "preview");
    await serverPeer.request("client/tool/commit-observe", { invocationId: "inv_01", paths: ["notes/a.md"] });
    assert.equal(reverseCalls[2][0], "commit-observe");
    const lookup = await serverPeer.request("client/tool/lookup", { invocationId: "inv_01", runId: "run_01" });
    assert.equal(lookup.found, false);
    assert.equal(reverseCalls[3][0], "lookup");
    await client.close();
});

test("schema mismatch fails closed and never becomes ready", async () => {
    const { HarnessClient, HarnessCompatibilityError } = loadModule("harness_client.ts");
    const { JsonRpcPeer } = loadModule("json_rpc.ts");
    const [clientChannel, serverChannel] = pair();
    const clientPeer = new JsonRpcPeer(clientChannel);
    const serverPeer = new JsonRpcPeer(serverChannel);
    serverPeer.register("initialize", async () => initializeResult({ schemaHash: `sha256:${"b".repeat(64)}` }));
    const noop = async () => ({});
    const client = new HarnessClient(
        { connect: async () => clientPeer },
        context(),
        { contextGet: noop, toolPreview: noop, toolCommitObserve: noop, toolInvoke: noop, toolLookup: noop, toolCancel: noop, approvalPresent: noop },
    );

    await assert.rejects(() => client.connect(), HarnessCompatibilityError);
    assert.equal(client.ready, false);
    await serverPeer.close();
});

test("initialize capability set defaults missing known fields to false and rejects extra or non-boolean fields", async (t) => {
    const scenarios = [
        { name: "extra", capabilities: { clientTools: true, eventReplay: true, multiSession: true, invented: true } },
        { name: "non-boolean", capabilities: { clientTools: true, eventReplay: true, multiSession: "yes" } },
    ];
    for (const scenario of scenarios) {
        await t.test(scenario.name, async () => {
            const { HarnessClient, HarnessCompatibilityError } = loadModule("harness_client.ts");
            const { JsonRpcPeer } = loadModule("json_rpc.ts");
            const [clientChannel, serverChannel] = pair();
            const clientPeer = new JsonRpcPeer(clientChannel);
            const serverPeer = new JsonRpcPeer(serverChannel);
            serverPeer.register("initialize", async () => initializeResult({ capabilities: scenario.capabilities }));
            const noop = async () => ({});
            const client = new HarnessClient(
                { connect: async () => clientPeer },
                context(),
                {
                    contextGet: noop, toolPreview: noop, toolCommitObserve: noop, toolInvoke: noop,
                    toolLookup: noop, toolCancel: noop, approvalPresent: noop,
                },
            );
            await assert.rejects(() => client.connect(), HarnessCompatibilityError);
            assert.equal(client.ready, false);
            await serverPeer.close();
        });
    }
});

test("event notification plus replay converge through one idempotent reducer", async () => {
    const { HarnessClient } = loadModule("harness_client.ts");
    const { JsonRpcPeer } = loadModule("json_rpc.ts");
    const [clientChannel, serverChannel] = pair();
    const clientPeer = new JsonRpcPeer(clientChannel);
    const serverPeer = new JsonRpcPeer(serverChannel);
    serverPeer.register("initialize", async () => initializeResult());
    serverPeer.register("events/replay", async (params) => ({
        events: [runEvent(1, "hel"), runEvent(2, "lo")],
        lastSequence: 2,
        runCursors: {},
        hasMore: false,
    }));
    const noop = async () => ({});
    const client = new HarnessClient(
        { connect: async () => clientPeer },
        context(),
        { contextGet: noop, toolPreview: noop, toolCommitObserve: noop, toolInvoke: noop, toolLookup: noop, toolCancel: noop, approvalPresent: noop },
    );
    await client.connect();
    await serverPeer.notify("event", runEvent(1, "hel"));
    await new Promise((resolve) => setImmediate(resolve));
    const cursor = await client.replay({ runId: "run_01J00000000000000000000000" }, 0);

    assert.equal(cursor, 2);
    assert.equal(client.reducer.state.runs.get("run_01J00000000000000000000000").assistantBlocks[0], "hello");
    await client.close();
});

test("Session replay keeps one monotonic cursor per Run and never invents an aggregate sequence", async () => {
    const { HarnessClient } = loadModule("harness_client.ts");
    const { JsonRpcPeer } = loadModule("json_rpc.ts");
    const [clientChannel, serverChannel] = pair();
    const clientPeer = new JsonRpcPeer(clientChannel);
    const serverPeer = new JsonRpcPeer(serverChannel);
    serverPeer.register("initialize", async () => initializeResult());
    let page = 0;
    serverPeer.register("events/replay", async (params) => {
        assert.equal(params.afterSequence, 0);
        assert.equal(params.runId, null);
        if (page++ === 0) return {
            events: [runEvent(1, "a")], lastSequence: null,
            runCursors: { run_01J00000000000000000000000: 1 }, hasMore: true,
        };
        assert.deepEqual(params.runCursors, { run_01J00000000000000000000000: 1 });
        return {
            events: [{ ...runEvent(2, "b"), payload: { blockIndex: 0, offset: 1, delta: "b" } }], lastSequence: null,
            runCursors: { run_01J00000000000000000000000: 2 }, hasMore: false,
        };
    });
    const noop = async () => ({});
    const client = new HarnessClient(
        { connect: async () => clientPeer }, context(),
        { contextGet: noop, toolPreview: noop, toolCommitObserve: noop, toolInvoke: noop, toolLookup: noop, toolCancel: noop, approvalPresent: noop },
    );
    await client.connect();
    const cursors = await client.replaySession("ses_01J00000000000000000000000");

    assert.deepEqual(cursors, { run_01J00000000000000000000000: 2 });
    assert.equal(client.reducer.state.runs.get("run_01J00000000000000000000000").assistantBlocks[0], "ab");
    await client.close();
});

test("ping identity change disconnects and reports an actionable local error", async () => {
    const { HarnessClient } = loadModule("harness_client.ts");
    const { JsonRpcPeer } = loadModule("json_rpc.ts");
    const [clientChannel, serverChannel] = pair();
    const clientPeer = new JsonRpcPeer(clientChannel);
    const serverPeer = new JsonRpcPeer(serverChannel);
    serverPeer.register("initialize", async () => initializeResult());
    serverPeer.register("runtime/ping", async (params) => ({
        nonce: params.nonce,
        timestamp: "2026-07-13T02:00:00+00:00",
        workerPid: 999,
    }));
    let disconnected;
    const noop = async () => ({});
    const client = new HarnessClient(
        { connect: async () => clientPeer },
        context(),
        { contextGet: noop, toolPreview: noop, toolCommitObserve: noop, toolInvoke: noop, toolLookup: noop, toolCancel: noop, approvalPresent: noop },
        { pingIntervalMs: 5, pingDeadlineMs: 100, onDisconnected: (error) => { disconnected = error; } },
    );
    await client.connect();
    await new Promise((resolve) => setTimeout(resolve, 30));

    assert.equal(client.ready, false);
    assert.match(disconnected.message, /identity changed/);
    await serverPeer.close();
});
