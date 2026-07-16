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

function runtime() {
    return {
        version: "0.1.0-local.test",
        workerExecutable: "C:\\OfferAgent\\offeragent-worker.exe",
        protocolMinimum: "1.0",
        protocolMaximum: "1.0",
        schemaHash: `sha256:${"a".repeat(64)}`,
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
