const assert = require("node:assert/strict");
const path = require("node:path");
const test = require("node:test");
const { buildSync } = require("esbuild");

function loadModule() {
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

const runtime = {
    version: "2.1.0",
    hostExecutable: "C:\\OfferAgent\\runtime\\2.1.0\\offeragent-host.exe",
    hostDiscoveryTimeoutMs: 10_000,
    protocolMinimum: "1.0",
    protocolMaximum: "1.0",
    schemaHash: `sha256:${"a".repeat(64)}`,
};

const identity = {
    workerPid: 321,
    hostPid: 123,
    runtimeVersion: "2.1.0",
};

async function flushTasks(count = 3) {
    for (let index = 0; index < count; index += 1) {
        await new Promise((resolve) => setImmediate(resolve));
    }
}

class FakeClient {
    constructor(connect, onDisconnected) {
        this.connectImpl = connect;
        this.disconnect = onDisconnected;
        this.ready = false;
        this.closeCalls = [];
        this.identity = identity;
    }
    async connect(signal) {
        const value = await this.connectImpl(signal);
        this.ready = true;
        return value;
    }
    async close(options = {}) {
        this.ready = false;
        this.closeCalls.push(options);
    }
}

test("bootstrap exposes the full install-to-ready state sequence", async () => {
    const { RuntimeBootstrap } = loadModule();
    const phases = [
        "not_installed",
        "locating_embedded_bundle",
        "verifying_manifest_and_signature",
        "extracting_to_staging",
        "verifying_each_file",
        "atomic_activate",
        "runtime_self_test",
        "ready",
    ];
    const installer = {
        async ensureReady(_signal, onPhase) {
            for (const phase of phases) onPhase(phase);
            return runtime;
        },
    };
    let client;
    const clients = {
        create(_runtime, disconnected) {
            client = new FakeClient(async () => identity, disconnected);
            return client;
        },
    };
    const bootstrap = new RuntimeBootstrap(installer, clients);
    const states = [];
    bootstrap.subscribe((snapshot) => states.push(snapshot.state));

    const result = await bootstrap.start();

    assert.equal(result.workerPid, 321);
    assert.equal(bootstrap.harness, client);
    assert.deepEqual(states, [
        "uninitialized", "runtime_missing", "runtime_missing", "verifying", "installing", "installing",
        "installing", "installing", "starting_host", "starting_host", "attaching_worker", "handshaking", "ready",
    ]);
    await bootstrap.stop();
    assert.equal(bootstrap.snapshot.state, "stopped");
});

test("unexpected disconnect reconnects with bounded exponential delay", async () => {
    const { RpcDisconnectedError, RuntimeBootstrap } = loadModule();
    const installer = { ensureReady: async (_signal, onPhase) => { onPhase("ready"); return runtime; } };
    const created = [];
    const clients = {
        create(_runtime, disconnected) {
            const client = new FakeClient(async () => ({ ...identity, workerPid: 321 + created.length }), disconnected);
            client.identity = { ...identity, workerPid: 321 + created.length };
            created.push(client);
            return client;
        },
    };
    const delays = [];
    const bootstrap = new RuntimeBootstrap(installer, clients, {
        baseRestartDelayMs: 100,
        random: () => 0,
        sleep: async (delay) => { delays.push(delay); },
    });
    await bootstrap.start();
    created[0].disconnect(new RpcDisconnectedError());
    await flushTasks();

    assert.equal(created.length, 2);
    assert.equal(bootstrap.snapshot.state, "ready");
    assert.deepEqual(delays, [50]);
    assert.equal(bootstrap.snapshot.workerPid, 322);
    await bootstrap.stop();
});

test("connect crash loop opens a circuit instead of retrying forever", async () => {
    const { RpcDisconnectedError, RuntimeBootstrap, RuntimeBootstrapCircuitOpen } = loadModule();
    const installer = { ensureReady: async (_signal, onPhase) => { onPhase("ready"); return runtime; } };
    let attempts = 0;
    const clients = {
        create(_runtime, disconnected) {
            attempts += 1;
            return new FakeClient(async () => { throw new RpcDisconnectedError(); }, disconnected);
        },
    };
    const bootstrap = new RuntimeBootstrap(installer, clients, {
        maximumRestartAttempts: 2,
        sleep: async () => undefined,
    });

    await assert.rejects(() => bootstrap.start(), RuntimeBootstrapCircuitOpen);
    assert.equal(attempts, 3);
    assert.equal(bootstrap.snapshot.state, "failed");
    assert.equal(bootstrap.snapshot.error.code, "runtime_crash_loop");
    assert.equal(bootstrap.snapshot.error.causeCode, "runtime_disconnected");
});

test("protocol mismatch, authentication, non-retryable Worker, and unknown failures fail closed after one attempt", async (t) => {
    const {
        HarnessCompatibilityError,
        NamedPipeAuthenticationError,
        RemoteRpcError,
        RuntimeBootstrap,
    } = loadModule();
    const scenarios = [
        {
            name: "protocol_mismatch",
            createError: () => new HarnessCompatibilityError("raw incompatible detail"),
            expectedCode: "runtime_incompatible",
        },
        {
            name: "authentication",
            createError: () => new NamedPipeAuthenticationError("raw authentication detail"),
            expectedCode: "runtime_auth_failed",
        },
        {
            name: "remote",
            createError: () => new RemoteRpcError(-32000, {
                cancelled: false,
                code: "provider.auth_required",
                retryable: false,
                userVisibleMessage: "请配置模型凭据。",
            }),
            expectedCode: "provider.auth_required",
        },
        {
            name: "unknown",
            createError: () => new Error("raw unknown detail"),
            expectedCode: "runtime_unavailable",
        },
    ];
    for (const scenario of scenarios) {
        await t.test(scenario.name, async () => {
            let attempts = 0;
            const clients = {
                create(_runtime, disconnected) {
                    attempts += 1;
                    return new FakeClient(async () => { throw scenario.createError(); }, disconnected);
                },
            };
            const bootstrap = new RuntimeBootstrap(
                { ensureReady: async () => runtime },
                clients,
                { maximumRestartAttempts: 5, sleep: async () => undefined },
            );
            await assert.rejects(() => bootstrap.start());
            assert.equal(attempts, 1);
            assert.equal(bootstrap.snapshot.state, "failed");
            assert.equal(bootstrap.snapshot.error.code, scenario.expectedCode);
            assert.doesNotMatch(bootstrap.snapshot.error.message, /raw/i);
        });
    }
});

test("retryable Worker errors honor typed retryAfterMs before reconnecting", async () => {
    const { RemoteRpcError, RuntimeBootstrap } = loadModule();
    let attempts = 0;
    const delays = [];
    const clients = {
        create(_runtime, disconnected) {
            attempts += 1;
            return new FakeClient(async () => {
                if (attempts === 1) {
                    throw new RemoteRpcError(-32000, {
                        cancelled: false,
                        code: "provider.unreachable",
                        retryable: true,
                        retryAfterMs: 250,
                        userVisibleMessage: "模型服务暂不可用。",
                    });
                }
                return identity;
            }, disconnected);
        },
    };
    const bootstrap = new RuntimeBootstrap(
        { ensureReady: async () => runtime },
        clients,
        { baseRestartDelayMs: 100, random: () => 0, sleep: async (delay) => { delays.push(delay); } },
    );

    await bootstrap.start();
    assert.equal(attempts, 2);
    assert.deepEqual(delays, [250]);
    assert.equal(bootstrap.snapshot.state, "ready");
    await bootstrap.stop();
});

test("repeated ready-disconnect flapping opens the circuit instead of resetting history", async () => {
    const { RpcDisconnectedError, RuntimeBootstrap } = loadModule();
    const created = [];
    const clients = {
        create(_runtime, disconnected) {
            const client = new FakeClient(async () => identity, disconnected);
            created.push(client);
            return client;
        },
    };
    const bootstrap = new RuntimeBootstrap(
        { ensureReady: async () => runtime },
        clients,
        { maximumRestartAttempts: 2, random: () => 0, sleep: async () => undefined, now: () => 100 },
    );
    await bootstrap.start();
    created[0].disconnect(new RpcDisconnectedError());
    await flushTasks();
    assert.equal(bootstrap.snapshot.state, "ready");
    created[1].disconnect(new RpcDisconnectedError());
    await flushTasks();
    assert.equal(bootstrap.snapshot.state, "ready");
    created[2].disconnect(new RpcDisconnectedError());
    await flushTasks();

    assert.equal(created.length, 3);
    assert.equal(bootstrap.snapshot.state, "failed");
    assert.equal(bootstrap.snapshot.error.code, "runtime_crash_loop");
    assert.equal(bootstrap.snapshot.error.causeCode, "runtime_disconnected");
});

test("restart history naturally expires under an injected monotonic clock", async () => {
    const { RpcDisconnectedError, RuntimeBootstrap } = loadModule();
    let clock = 0;
    const created = [];
    const clients = {
        create(_runtime, disconnected) {
            const client = new FakeClient(async () => identity, disconnected);
            created.push(client);
            return client;
        },
    };
    const bootstrap = new RuntimeBootstrap(
        { ensureReady: async () => runtime },
        clients,
        {
            maximumRestartAttempts: 1,
            restartWindowMs: 1_000,
            sleep: async () => undefined,
            now: () => clock,
        },
    );
    await bootstrap.start();
    created[0].disconnect(new RpcDisconnectedError());
    await flushTasks();
    clock = 1_001;
    created[1].disconnect(new RpcDisconnectedError());
    await flushTasks();

    assert.equal(created.length, 3);
    assert.equal(bootstrap.snapshot.state, "ready");
    await bootstrap.stop();
});

test("stop cancels an in-flight local installation without deleting runtime state", async () => {
    const { RuntimeBootstrap } = loadModule();
    let aborted = false;
    const installer = {
        ensureReady(signal, onPhase) {
            onPhase("extracting_to_staging");
            return new Promise((_resolve, reject) => signal.addEventListener("abort", () => {
                aborted = true;
                const error = new Error("aborted"); error.name = "AbortError"; reject(error);
            }, { once: true }));
        },
    };
    const bootstrap = new RuntimeBootstrap(installer, { create: () => { throw new Error("not reached"); } });
    const start = bootstrap.start();
    await new Promise((resolve) => setImmediate(resolve));
    await bootstrap.stop();

    await assert.rejects(start, (error) => error.name === "AbortError");
    assert.equal(aborted, true);
    assert.equal(bootstrap.snapshot.state, "stopped");
});

test("background Vault bookkeeping cannot restart a failed or explicitly stopped Runtime", () => {
    const { canBackgroundStartRuntime } = loadModule();
    for (const state of [
        "uninitialized", "runtime_missing", "verifying", "installing", "starting_host",
        "attaching_worker", "handshaking", "ready", "degraded", "restarting",
    ]) assert.equal(canBackgroundStartRuntime(state), true, state);
    assert.equal(canBackgroundStartRuntime("failed"), false);
    assert.equal(canBackgroundStartRuntime("stopped"), false);
});
