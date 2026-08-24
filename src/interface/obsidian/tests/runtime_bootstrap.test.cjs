const assert = require("node:assert/strict");
const path = require("node:path");
const test = require("node:test");
const { buildSync } = require("esbuild");

function loadBootstrap() {
    const output = buildSync({
        entryPoints: [path.join(__dirname, "../src/runtime/bootstrap.ts")],
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

function loadRuntimeModule(entry) {
    const output = buildSync({
        entryPoints: [path.join(__dirname, "../src/runtime", entry)],
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

function runtime() {
    return {
        version: "0.1.0-local.test",
        workerExecutable: "C:\\OfferAgent\\offeragent-worker.exe",
        protocolMinimum: "1.0",
        protocolMaximum: "1.0",
        schemaHash: `sha256:${"a".repeat(64)}`,
    };
}

function deferred() {
    let resolve;
    const promise = new Promise((resolvePromise) => { resolve = resolvePromise; });
    return { promise, resolve };
}

function nextTurn() {
    return new Promise((resolvePromise) => setImmediate(resolvePromise));
}

const HARNESS_SCHEMA_HASH = `sha256:${"a".repeat(64)}`;
const HARNESS_WORKSPACE = "wsi_01J00000000000000000000000";

function harnessContext() {
    return {
        workspaceId: HARNESS_WORKSPACE,
        identity: {
            protocolVersion: "1.0",
            minimumProtocolVersion: "1.0",
            maximumProtocolVersion: "1.0",
            schemaHash: HARNESS_SCHEMA_HASH,
            clientVersion: "2.1.0",
        },
        requiredCapabilities: [],
    };
}

function harnessInitializeResult() {
    return {
        protocolVersion: "1.0",
        supportedProtocolRange: { minimum: "1.0", maximum: "1.0" },
        runtimeVersion: "2.1.0",
        coreVersion: "2.1.0",
        schemaHash: HARNESS_SCHEMA_HASH,
        workspaceId: HARNESS_WORKSPACE,
        workspaceInstanceId: "wsi_01J00000000000000000000001",
        parentPid: 123,
        workerPid: 456,
        transport: "stdio",
        runtimeArch: "win-x64",
        capabilities: {},
        buildCommit: "abcdef0123456789",
    };
}

test("Worker startup exit retains a bounded stable cause through crash-loop reporting", async () => {
    const { RpcDisconnectedError, RuntimeBootstrap } = loadBootstrap();
    const snapshots = [];
    const bootstrap = new RuntimeBootstrap(
        { ensureReady: async () => runtime() },
        {
            create: () => ({
                ready: false,
                connect: async () => {
                    throw new RpcDisconnectedError(
                        "OfferAgent Worker exited (exit code 2): " +
                        "offeragent-worker: local development startup failed (startup_scan_failed)",
                    );
                },
                close: async () => undefined,
            }),
        },
        {
            maximumRestartAttempts: 1,
            sleep: async () => undefined,
            random: () => 0,
            now: (() => { let value = 0; return () => ++value; })(),
        },
    );
    bootstrap.subscribe((snapshot) => snapshots.push(snapshot));

    await assert.rejects(bootstrap.start());

    const degraded = snapshots.find((snapshot) => snapshot.state === "degraded");
    assert.equal(degraded.error.code, "runtime_disconnected");
    assert.equal(degraded.error.causeCode, "startup_scan_failed");
    assert.match(degraded.error.message, /startup_scan_failed/);
    assert.equal(bootstrap.snapshot.state, "failed");
    assert.equal(bootstrap.snapshot.error.code, "runtime_crash_loop");
    assert.equal(bootstrap.snapshot.error.causeCode, "startup_scan_failed");
});

test("arbitrary disconnect text is not reflected into Runtime diagnostics", async () => {
    const { RpcDisconnectedError, RuntimeBootstrap } = loadBootstrap();
    const bootstrap = new RuntimeBootstrap(
        { ensureReady: async () => runtime() },
        {
            create: () => ({
                ready: false,
                connect: async () => { throw new RpcDisconnectedError("secret path must-not-leak"); },
                close: async () => undefined,
            }),
        },
        {
            maximumRestartAttempts: 1,
            sleep: async () => undefined,
            random: () => 0,
            now: (() => { let value = 0; return () => ++value; })(),
        },
    );

    await assert.rejects(bootstrap.start());

    assert.equal(bootstrap.snapshot.error.causeCode, "runtime_disconnected");
    assert.doesNotMatch(bootstrap.snapshot.error.message, /must-not-leak/);
});

test("each Worker spawn revalidates the pinned Runtime before client construction", async () => {
    const { RpcDisconnectedError, RuntimeBootstrap } = loadBootstrap();
    const snapshots = [];
    let verifications = 0;
    let clients = 0;
    const installed = {
        ...runtime(),
        beforeWorkerLaunch: async () => { verifications += 1; },
    };
    const bootstrap = new RuntimeBootstrap(
        { ensureReady: async () => installed },
        {
            create: () => {
                clients += 1;
                const current = clients;
                return {
                    ready: current > 1,
                    identity: { workerPid: 42 },
                    connect: async () => {
                        if (current === 1) throw new RpcDisconnectedError();
                        return { workerPid: 42 };
                    },
                    close: async () => undefined,
                };
            },
        },
        {
            maximumRestartAttempts: 2,
            sleep: async () => undefined,
            random: () => 0,
            now: (() => { let value = 0; return () => ++value; })(),
        },
    );
    bootstrap.subscribe((snapshot) => snapshots.push(snapshot));

    await bootstrap.start();

    assert.equal(verifications, 2);
    assert.equal(clients, 2);
    assert.equal(snapshots.some((snapshot) => snapshot.state === "starting_worker"), true);
    assert.equal(snapshots.some((snapshot) => snapshot.state === "handshaking"), true);
    assert.equal(bootstrap.snapshot.state, "ready");
});

test("Runtime verification failure rejects before creating a Worker client", async () => {
    const { RuntimeBootstrap } = loadBootstrap();
    let clients = 0;
    const bootstrap = new RuntimeBootstrap(
        {
            ensureReady: async () => ({
                ...runtime(),
                beforeWorkerLaunch: async () => { throw new Error("pinned Runtime changed"); },
            }),
        },
        {
            create: () => {
                clients += 1;
                throw new Error("must not create client");
            },
        },
    );

    await assert.rejects(bootstrap.start(), /pinned Runtime changed/);

    assert.equal(clients, 0);
    assert.equal(bootstrap.snapshot.state, "failed");
    assert.equal(bootstrap.snapshot.error.code, "runtime_unavailable");
});

test("automatic disconnect joins the retiring Worker before creating its replacement", async () => {
    const { RpcDisconnectedError, RuntimeBootstrap } = loadBootstrap();
    const closeStarted = deferred();
    const allowClose = deferred();
    const reconnected = deferred();
    const order = [];
    let clients = 0;
    let disconnect;
    const bootstrap = new RuntimeBootstrap(
        { ensureReady: async () => runtime() },
        {
            create: (_installed, onDisconnected) => {
                clients += 1;
                const current = clients;
                let ready = false;
                if (current === 1) disconnect = onDisconnected;
                order.push(`create:${current}`);
                return {
                    get ready() { return ready; },
                    get identity() { return { workerPid: current }; },
                    connect: async () => {
                        order.push(`connect:${current}`);
                        ready = true;
                        return { workerPid: current };
                    },
                    close: async () => {
                        order.push(`close:${current}:start`);
                        ready = false;
                        if (current === 1) {
                            closeStarted.resolve();
                            await allowClose.promise;
                        }
                        order.push(`close:${current}:end`);
                    },
                };
            },
        },
        {
            maximumRestartAttempts: 2,
            sleep: async () => { order.push("sleep"); },
            random: () => 0,
            now: (() => { let value = 0; return () => ++value; })(),
        },
    );
    bootstrap.subscribe((snapshot) => {
        if (snapshot.state === "ready" && clients === 2) reconnected.resolve();
    });
    await bootstrap.start();

    disconnect(new RpcDisconnectedError("Worker pipe failed"));
    await closeStarted.promise;
    await nextTurn();

    assert.equal(clients, 1);
    assert.deepEqual(order, ["create:1", "connect:1", "close:1:start"]);

    allowClose.resolve();
    await reconnected.promise;
    assert.deepEqual(order, [
        "create:1",
        "connect:1",
        "close:1:start",
        "close:1:end",
        "sleep",
        "create:2",
        "connect:2",
    ]);
    assert.equal(bootstrap.snapshot.state, "ready");

    await bootstrap.stop();
});

test("terminal disconnect still joins the Worker and stop waits for that retirement", async () => {
    const { HarnessCompatibilityError, RuntimeBootstrap } = loadBootstrap();
    const closeStarted = deferred();
    const allowClose = deferred();
    let disconnect;
    let clients = 0;
    const bootstrap = new RuntimeBootstrap(
        { ensureReady: async () => runtime() },
        {
            create: (_installed, onDisconnected) => {
                clients += 1;
                disconnect = onDisconnected;
                return {
                    ready: true,
                    identity: { workerPid: 1 },
                    connect: async () => ({ workerPid: 1 }),
                    close: async () => {
                        closeStarted.resolve();
                        await allowClose.promise;
                    },
                };
            },
        },
    );
    await bootstrap.start();

    disconnect(new HarnessCompatibilityError("incompatible Worker"));
    await closeStarted.promise;
    assert.equal(bootstrap.snapshot.state, "failed");

    let stopped = false;
    const stopping = bootstrap.stop().then(() => { stopped = true; });
    await nextTurn();
    assert.equal(stopped, false);
    assert.equal(clients, 1);

    allowClose.resolve();
    await stopping;
    assert.equal(stopped, true);
    assert.equal(bootstrap.snapshot.state, "stopped");
});

test("concurrent stops share one cleanup and both wait for the Worker join", async () => {
    const { RuntimeBootstrap } = loadBootstrap();
    const closeStarted = deferred();
    const allowClose = deferred();
    let closeCalls = 0;
    const bootstrap = new RuntimeBootstrap(
        { ensureReady: async () => runtime() },
        {
            create: () => ({
                ready: true,
                identity: { workerPid: 1 },
                connect: async () => ({ workerPid: 1 }),
                close: async () => {
                    closeCalls += 1;
                    closeStarted.resolve();
                    await allowClose.promise;
                },
            }),
        },
    );
    await bootstrap.start();

    let firstStopped = false;
    let secondStopped = false;
    const first = bootstrap.stop();
    const second = bootstrap.stop();
    assert.equal(first, second);
    void first.then(() => { firstStopped = true; });
    void second.then(() => { secondStopped = true; });

    await closeStarted.promise;
    await nextTurn();
    assert.equal(closeCalls, 1);
    assert.equal(firstStopped, false);
    assert.equal(secondStopped, false);

    allowClose.resolve();
    await Promise.all([first, second]);
    assert.equal(firstStopped, true);
    assert.equal(secondStopped, true);
    assert.equal(closeCalls, 1);
    assert.equal(bootstrap.snapshot.state, "stopped");
});

test("unload synchronously starts non-RPC client teardown while the stop gate tracks join", async () => {
    const { RuntimeBootstrap } = loadBootstrap();
    const allowClose = deferred();
    let closeCalls = 0;
    let closeOptions;
    const bootstrap = new RuntimeBootstrap(
        { ensureReady: async () => runtime() },
        {
            create: () => ({
                ready: true,
                identity: { workerPid: 1 },
                connect: async () => ({ workerPid: 1 }),
                close: (options) => {
                    closeCalls += 1;
                    closeOptions = options;
                    return allowClose.promise;
                },
            }),
        },
    );
    await bootstrap.start();

    const retirement = bootstrap.beginUnload();
    assert.equal(closeCalls, 1);
    assert.deepEqual(closeOptions, { shutdown: false });
    assert.equal(bootstrap.stop(), retirement);
    let retired = false;
    void retirement.then(() => { retired = true; });
    await nextTurn();
    assert.equal(retired, false);

    allowClose.resolve();
    await retirement;
    assert.equal(retired, true);
    assert.equal(bootstrap.snapshot.state, "stopped");
});

test("beginUnload cannot pass a pending peer join already registered by ping retirement", async () => {
    const { RuntimeBootstrap } = loadBootstrap();
    const { HarnessClient } = loadRuntimeModule("harness_client.ts");
    const closeStarted = deferred();
    const joined = deferred();
    let peerCloseOperation = null;
    let closeCalls = 0;
    let client;
    const peer = {
        onNotification: () => () => undefined,
        request: async (method) => {
            if (method === "initialize") return harnessInitializeResult();
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
    const bootstrap = new RuntimeBootstrap(
        { ensureReady: async () => runtime() },
        {
            create: (_installed, onDisconnected) => {
                client = new HarnessClient(
                    { connect: async () => peer },
                    harnessContext(),
                    { pingIntervalMs: 60_000, onDisconnected },
                );
                return client;
            },
        },
    );
    await bootstrap.start();

    const pinging = client.ping();
    await closeStarted.promise;
    let unloaded = false;
    const unloading = bootstrap.beginUnload().then(() => { unloaded = true; });
    await nextTurn();

    assert.equal(closeCalls, 1);
    assert.equal(unloaded, false);
    assert.equal(bootstrap.snapshot.workerPid, null);

    joined.resolve();
    await Promise.all([pinging, unloading]);
    assert.equal(unloaded, true);
    assert.equal(bootstrap.snapshot.state, "stopped");
});

test("unload escalates an already-running graceful stop through the same gate", async () => {
    const { RuntimeBootstrap } = loadBootstrap();
    const allowClose = deferred();
    let immediateCalls = 0;
    const bootstrap = new RuntimeBootstrap(
        { ensureReady: async () => runtime() },
        {
            create: () => ({
                ready: true,
                identity: { workerPid: 1 },
                connect: async () => ({ workerPid: 1 }),
                close: () => allowClose.promise,
                beginImmediateClose: () => {
                    immediateCalls += 1;
                    return allowClose.promise;
                },
            }),
        },
    );
    await bootstrap.start();

    const graceful = bootstrap.stop();
    const unload = bootstrap.beginUnload();
    assert.equal(unload, graceful);
    assert.equal(immediateCalls, 1);

    allowClose.resolve();
    await unload;
    assert.equal(bootstrap.snapshot.state, "stopped");
});

test("start requested during stop waits for the Worker join before spawning", async () => {
    const { RuntimeBootstrap } = loadBootstrap();
    const closeStarted = deferred();
    const allowClose = deferred();
    const order = [];
    let clients = 0;
    const bootstrap = new RuntimeBootstrap(
        { ensureReady: async () => runtime() },
        {
            create: () => {
                clients += 1;
                const current = clients;
                order.push(`create:${current}`);
                return {
                    ready: true,
                    identity: { workerPid: current },
                    connect: async () => {
                        order.push(`connect:${current}`);
                        return { workerPid: current };
                    },
                    close: async () => {
                        order.push(`close:${current}:start`);
                        if (current === 1) {
                            closeStarted.resolve();
                            await allowClose.promise;
                        }
                        order.push(`close:${current}:end`);
                    },
                };
            },
        },
    );
    await bootstrap.start();

    const stopping = bootstrap.stop();
    await closeStarted.promise;
    let started = false;
    const starting = bootstrap.start().then((identity) => {
        started = true;
        return identity;
    });
    await nextTurn();

    assert.equal(started, false);
    assert.equal(clients, 1);
    assert.deepEqual(order, ["create:1", "connect:1", "close:1:start"]);

    allowClose.resolve();
    await stopping;
    const identity = await starting;
    assert.equal(identity.workerPid, 2);
    assert.equal(started, true);
    assert.equal(clients, 2);
    assert.deepEqual(order.slice(0, 5), [
        "create:1",
        "connect:1",
        "close:1:start",
        "close:1:end",
        "create:2",
    ]);
    assert.equal(bootstrap.snapshot.state, "ready");

    await bootstrap.stop();
});
