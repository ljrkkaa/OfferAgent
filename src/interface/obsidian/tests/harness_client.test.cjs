const assert = require("node:assert/strict");
const path = require("node:path");
const test = require("node:test");
const { buildSync } = require("esbuild");
const { createHash } = require("node:crypto");

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

function deferred() {
    let resolve;
    let reject;
    const promise = new Promise((resolvePromise, rejectPromise) => {
        resolve = resolvePromise;
        reject = rejectPromise;
    });
    return { promise, resolve, reject };
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
        requiredCapabilities: ["eventReplay", "multiSession"],
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
        parentPid: 123,
        workerPid: 456,
        transport: "stdio",
        runtimeArch: "win-x64",
        capabilities: {
            eventReplay: true,
            multiSession: true,
            approvals: true,
            skills: true,
            shell: true,
            hooks: true,
            subagents: true,
            artifacts: true,
            contentBlocks: true,
            cancellation: true,
            diagnostics: true,
        },
        buildCommit: "abcdef0123456789",
        ...overrides,
    };
}

test("complete local Runtime requires structural Subagent protocol support independent of enablement", () => {
    const { CLIENT_CAPABILITIES, REQUIRED_RUNTIME_CAPABILITIES } = loadModule("harness_client.ts");
    assert.equal(CLIENT_CAPABILITIES.subagents, true);
    assert.equal(REQUIRED_RUNTIME_CAPABILITIES.includes("subagents"), true);
    for (const required of ["eventReplay", "multiSession", "diagnostics"]) {
        assert.equal(REQUIRED_RUNTIME_CAPABILITIES.includes(required), true, `${required} must remain required`);
    }
    assert.equal("loopbackWeb" in CLIENT_CAPABILITIES, false);
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

test("client waits for plugin recovery before spawning the Worker transport", async () => {
    const { HarnessClient } = loadModule("harness_client.ts");
    const order = [];
    const peer = {
        onNotification: () => () => undefined,
        request: async (method) => {
            assert.equal(method, "initialize");
            return initializeResult();
        },
        close: async () => undefined,
    };
    const client = new HarnessClient(
        { connect: async () => { order.push("spawn"); return peer; } },
        context(),
        {
            beforeConnect: async () => { order.push("plugin-recovery"); },
            pingIntervalMs: 60_000,
        },
    );

    await client.connect();

    assert.deepEqual(order, ["plugin-recovery", "spawn"]);
    await client.close();
});

test("plugin recovery failure prevents Worker transport creation", async () => {
    const { HarnessClient } = loadModule("harness_client.ts");
    let spawned = false;
    const client = new HarnessClient(
        { connect: async () => { spawned = true; throw new Error("must not spawn"); } },
        context(),
        { beforeConnect: async () => { throw new Error("journal recovery failed"); } },
    );

    await assert.rejects(() => client.connect(), /journal recovery failed/u);

    assert.equal(spawned, false);
});

test("client performs strict initialize and subscribes to Worker events", async () => {
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
    const client = new HarnessClient(
        { connect: async () => clientPeer },
        context(),
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
    assert.equal("headlessVaultWrite" in initializeParams.capabilities, false);
    assert.equal("clientTools" in initializeParams.capabilities, false);
    await serverPeer.notify("event", runEvent(1, "hel"));
    await new Promise((resolve) => setImmediate(resolve));
    assert.equal(client.reducer.state.runs.get("run_01J00000000000000000000000").timeline.length, 1);
    await client.close();
});

test("attachment helper keeps ordered bytes behind bounded commands and verifies replay", async () => {
    const { HarnessClient } = loadModule("harness_client.ts");
    const bytes = Buffer.concat([Buffer.from("\x89PNG\r\n\x1a\n"), Buffer.alloc(70_000, 7)]);
    const contentHash = `sha256:${createHash("sha256").update(bytes).digest("hex")}`;
    const uploaded = [];
    const methods = [];
    const peer = {
        onNotification: () => () => undefined,
        request: async (method, params) => {
            methods.push(method);
            if (method === "initialize") return initializeResult();
            if (method === "attachments/begin") {
                assert.equal(params.byteLength, bytes.length);
                assert.equal(params.contentHash, contentHash);
                return { uploadId: "upload_one", artifactId: "art_one", maxChunkBytes: 65_536, nextOffset: 0, duplicate: false };
            }
            if (method === "attachments/chunk") {
                const chunk = Buffer.from(params.contentBase64, "base64");
                assert.equal(params.offset, uploaded.reduce((size, item) => size + item.length, 0));
                assert.equal(params.contentHash, `sha256:${createHash("sha256").update(chunk).digest("hex")}`);
                uploaded.push(chunk);
                return { uploadId: "upload_one", receivedBytes: params.offset + chunk.length, duplicate: false };
            }
            if (method === "attachments/commit") {
                assert.deepEqual(Buffer.concat(uploaded), bytes);
                return {
                    uploadId: "upload_one",
                    duplicate: false,
                    artifact: {
                        artifactId: "art_one",
                        contentHash,
                        mediaType: "image/png",
                        sizeBytes: bytes.length,
                        sensitivity: "private",
                        state: "complete",
                        title: "evidence.png",
                    },
                };
            }
            if (method === "attachments/read") {
                const content = bytes.subarray(params.offset, Math.min(params.offset + params.maxBytes, bytes.length));
                return {
                    artifact: {
                        artifactId: "art_one", contentHash, mediaType: "image/png", sizeBytes: bytes.length,
                        sensitivity: "private", state: "complete", title: "evidence.png",
                    },
                    offset: params.offset,
                    nextOffset: params.offset + content.length,
                    contentBase64: content.toString("base64"),
                    eof: params.offset + content.length === bytes.length,
                };
            }
            throw new Error(`unexpected method: ${method}`);
        },
        close: async () => undefined,
    };
    const client = new HarnessClient({ connect: async () => peer }, context(), { pingIntervalMs: 60_000 });
    await client.connect();
    const image = await client.uploadAttachment("ses_one", {
        fileName: "evidence.png",
        mediaType: "image/png",
        bytes,
        clientRequestId: "req_upload",
    });
    assert.equal(image.artifact.artifactId, "art_one");
    assert.equal(methods.filter((method) => method === "attachments/chunk").length, 2);
    assert.deepEqual(Buffer.from(await client.readAttachment("ses_one", image.artifact)), bytes);
    assert.equal(methods.includes("attachments/abort"), false);
    await client.close();
});

test("non-RPC Harness close starts transport teardown synchronously and tracks its join", async () => {
    const { HarnessClient } = loadModule("harness_client.ts");
    const joined = deferred();
    let closeCalls = 0;
    const peer = {
        onNotification: () => () => undefined,
        request: async (method) => {
            assert.equal(method, "initialize");
            return initializeResult();
        },
        close: () => {
            closeCalls += 1;
            return joined.promise;
        },
    };
    const client = new HarnessClient({ connect: async () => peer }, context());
    await client.connect();

    let closed = false;
    const closing = client.close().then(() => { closed = true; });
    assert.equal(closeCalls, 1);
    assert.equal(closed, false);

    joined.resolve();
    await closing;
    assert.equal(closed, true);
});

test("immediate Harness close escalates an in-flight shutdown RPC without replacing its join gate", async () => {
    const { HarnessClient } = loadModule("harness_client.ts");
    const shutdown = deferred();
    const joined = deferred();
    let closeCalls = 0;
    let peerCloseOperation = null;
    const peer = {
        onNotification: () => () => undefined,
        request: async (method) => {
            if (method === "initialize") return initializeResult();
            if (method === "shutdown") return shutdown.promise;
            throw new Error(`unexpected method: ${method}`);
        },
        close: () => {
            if (peerCloseOperation === null) {
                closeCalls += 1;
                shutdown.reject(new Error("transport closed"));
                peerCloseOperation = joined.promise;
            }
            return peerCloseOperation;
        },
    };
    const client = new HarnessClient({ connect: async () => peer }, context());
    await client.connect();

    const graceful = client.close({ shutdown: true });
    assert.equal(closeCalls, 0);
    const immediate = client.beginImmediateClose();
    assert.equal(immediate, graceful);
    assert.equal(closeCalls, 1);

    joined.resolve();
    await graceful;
    assert.equal(closeCalls, 1);
});

test("schema mismatch fails closed and never becomes ready", async () => {
    const { HarnessClient, HarnessCompatibilityError } = loadModule("harness_client.ts");
    const { JsonRpcPeer } = loadModule("json_rpc.ts");
    const [clientChannel, serverChannel] = pair();
    const clientPeer = new JsonRpcPeer(clientChannel);
    const serverPeer = new JsonRpcPeer(serverChannel);
    serverPeer.register("initialize", async () => initializeResult({ schemaHash: `sha256:${"b".repeat(64)}` }));
    const client = new HarnessClient(
        { connect: async () => clientPeer },
        context(),
    );

    await assert.rejects(() => client.connect(), HarnessCompatibilityError);
    assert.equal(client.ready, false);
    await serverPeer.close();
});

test("initialize capability set defaults missing known fields to false and rejects extra or non-boolean fields", async (t) => {
    const scenarios = [
        { name: "extra", capabilities: { ...initializeResult().capabilities, invented: true } },
        { name: "non-boolean", capabilities: { ...initializeResult().capabilities, multiSession: "yes" } },
    ];
    for (const scenario of scenarios) {
        await t.test(scenario.name, async () => {
            const { HarnessClient, HarnessCompatibilityError } = loadModule("harness_client.ts");
            const { JsonRpcPeer } = loadModule("json_rpc.ts");
            const [clientChannel, serverChannel] = pair();
            const clientPeer = new JsonRpcPeer(clientChannel);
            const serverPeer = new JsonRpcPeer(serverChannel);
            serverPeer.register("initialize", async () => initializeResult({ capabilities: scenario.capabilities }));
            const client = new HarnessClient(
                { connect: async () => clientPeer },
                context(),
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
    const completed = {
        ...runEvent(3, ""),
        type: "assistant.completed",
        payload: { content: [{ type: "text", text: "hello" }], finishReason: "stop" },
    };
    const terminal = {
        ...runEvent(4, ""),
        type: "turn.completed",
        payload: { terminationReason: "completed" },
    };
    serverPeer.register("events/replay", async (params) => ({
        events: [runEvent(1, "hel"), runEvent(2, "lo"), completed, terminal],
        lastSequence: 4,
        runCursors: {},
        hasMore: false,
    }));
    const client = new HarnessClient(
        { connect: async () => clientPeer },
        context(),
    );
    await client.connect();
    const deliveries = [];
    client.reducer.subscribe((value, _state, origin) => deliveries.push([value.sequence, origin]));
    await serverPeer.notify("event", runEvent(1, "hel"));
    await new Promise((resolve) => setImmediate(resolve));
    const cursor = await client.replay({ runId: "run_01J00000000000000000000000" }, 0);

    assert.equal(cursor, 4);
    const replayedRun = client.reducer.state.runs.get("run_01J00000000000000000000000");
    assert.equal(replayedRun.status, "completed");
    assert.equal(
        replayedRun.timeline
            .find((item) => item.kind === "assistant_message").blocks[0],
        "hello",
    );
    assert.deepEqual(deliveries, [[1, "live"], [2, "replay"], [3, "replay"], [4, "replay"]]);
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
    const client = new HarnessClient(
        { connect: async () => clientPeer }, context(),
    );
    await client.connect();
    const cursors = await client.replaySession("ses_01J00000000000000000000000");

    assert.deepEqual(cursors, { run_01J00000000000000000000000: 2 });
    assert.equal(
        client.reducer.state.runs.get("run_01J00000000000000000000000").timeline
            .find((item) => item.kind === "assistant_message").blocks[0],
        "ab",
    );
    await client.close();
});

test("ping retirement shares the explicit close gate until the old peer join completes", async () => {
    const { HarnessClient } = loadModule("harness_client.ts");
    const closeStarted = deferred();
    const joined = deferred();
    let peerCloseOperation = null;
    let closeCalls = 0;
    let disconnectCalls = 0;
    const peer = {
        onNotification: () => () => undefined,
        request: async (method) => {
            if (method === "initialize") return initializeResult();
            if (method === "runtime/ping") throw new Error("ping transport failed");
            throw new Error(`unexpected method: ${method}`);
        },
        close: () => {
            if (peerCloseOperation === null) {
                closeCalls += 1;
                closeStarted.resolve();
                peerCloseOperation = joined.promise;
            }
            return peerCloseOperation;
        },
    };
    const client = new HarnessClient(
        { connect: async () => peer },
        context(),
        { pingIntervalMs: 60_000, onDisconnected: () => { disconnectCalls += 1; } },
    );
    await client.connect();

    const pinging = client.ping();
    await closeStarted.promise;
    const closing = client.close();
    const immediate = client.beginImmediateClose();
    assert.equal(closing, immediate);
    let closed = false;
    void closing.then(() => { closed = true; });
    await new Promise((resolvePromise) => setImmediate(resolvePromise));

    assert.equal(closeCalls, 1);
    assert.equal(closed, false);
    assert.equal(disconnectCalls, 0);

    joined.resolve();
    await Promise.all([pinging, closing]);
    assert.equal(closed, true);
    assert.equal(disconnectCalls, 1);
    assert.equal(client.ready, false);
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
    const client = new HarnessClient(
        { connect: async () => clientPeer },
        context(),
        { pingIntervalMs: 5, pingDeadlineMs: 100, onDisconnected: (error) => { disconnected = error; } },
    );
    await client.connect();
    await new Promise((resolve) => setTimeout(resolve, 30));

    assert.equal(client.ready, false);
    assert.match(disconnected.message, /identity changed/);
    await serverPeer.close();
});
