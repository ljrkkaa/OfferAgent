const assert = require("node:assert/strict");
const { EventEmitter } = require("node:events");
const path = require("node:path");
const test = require("node:test");
const { buildSync } = require("esbuild");

function loadModule(entry) {
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

function loadPluginModule() {
    const output = buildSync({
        entryPoints: [path.join(__dirname, "../src/main.ts")],
        bundle: true,
        external: ["obsidian"],
        format: "cjs",
        platform: "node",
        target: "node16",
        write: false,
    }).outputFiles[0].text;
    const compiled = { exports: {} };
    class Component {}
    const obsidian = {
        FileSystemAdapter: class {},
        ItemView: Component,
        Menu: class {},
        Modal: Component,
        Notice: class {},
        Plugin: Component,
        PluginSettingTab: Component,
        Setting: class {},
        setIcon: () => undefined,
    };
    const localRequire = (specifier) => specifier === "obsidian" ? obsidian : require(specifier);
    new Function("require", "module", "exports", output)(localRequire, compiled, compiled.exports);
    return compiled.exports.default;
}

function deferred() {
    let resolve;
    const promise = new Promise((resolvePromise) => { resolve = resolvePromise; });
    return { promise, resolve };
}

function delay(milliseconds) {
    return new Promise((resolvePromise) => setTimeout(resolvePromise, milliseconds));
}

class FakeWritable extends EventEmitter {
    constructor() {
        super();
        this.writable = true;
        this.endCalls = 0;
    }

    write(_data, callback) {
        callback();
        return true;
    }

    end() {
        this.endCalls += 1;
        this.writable = false;
    }
}

class FakeChildProcess extends EventEmitter {
    constructor() {
        super();
        this.stdin = new FakeWritable();
        this.stdout = new EventEmitter();
        this.stderr = new EventEmitter();
        this.exitCode = null;
        this.signalCode = null;
        this.killed = false;
        this.killSignals = [];
        this.killObserved = deferred();
    }

    kill(signal) {
        this.killed = true;
        this.killSignals.push(signal);
        this.killObserved.resolve();
        return true;
    }

    emitExit(code = 0, signal = null) {
        this.exitCode = code;
        this.signalCode = signal;
        this.emit("exit", code, signal);
    }

    emitClose(code = this.exitCode, signal = this.signalCode) {
        this.emit("close", code, signal);
    }
}

test("stdio close gives the Worker EOF and joins a graceful exit without killing it", async () => {
    const { ChildStdioChannel } = loadModule("stdio_worker.ts");
    const child = new FakeChildProcess();
    const waitEntered = deferred();
    const channel = new ChildStdioChannel(child, {
        gracefulExitTimeoutMs: 15_000,
        waitForExit: async (exit, timeoutMs) => {
            assert.equal(timeoutMs, 15_000);
            waitEntered.resolve();
            await exit;
            return true;
        },
    });

    let closed = false;
    const firstClose = channel.close().then(() => { closed = true; });
    const secondClose = channel.close();
    assert.equal(child.stdin.endCalls, 1);
    await waitEntered.promise;

    assert.equal(child.stdin.endCalls, 1);
    assert.deepEqual(child.killSignals, []);
    assert.equal(closed, false);

    child.emitExit(0, null);
    await Promise.all([firstClose, secondClose]);

    assert.equal(closed, true);
    assert.equal(child.stdin.endCalls, 1);
    assert.deepEqual(child.killSignals, []);
});

test("Obsidian onunload returns void only after synchronously starting Worker stdin EOF", async () => {
    const OfferAgentPlugin = loadPluginModule();
    const { ChildStdioChannel } = loadModule("stdio_worker.ts");
    const child = new FakeChildProcess();
    const channel = new ChildStdioChannel(child, {
        waitForExit: async (exit) => {
            await exit;
            return true;
        },
    });
    let retirement;
    const plugin = Object.create(OfferAgentPlugin.prototype);
    Object.assign(plugin, {
        chatClient: null,
        chatStore: null,
        runtime: {
            beginUnload: () => {
                retirement = channel.close();
                return retirement;
            },
        },
        runtimeRestartOperation: null,
        runtimeRestartTimer: null,
        runtimeStart: null,
        unloading: false,
    });

    const returned = plugin.onunload();

    assert.equal(returned, undefined);
    assert.equal(plugin.unloading, true);
    assert.equal(child.stdin.endCalls, 1);
    child.emitExit(0, null);
    await retirement;
});

test("stdio close force-kills after its deadline but does not resolve before process join", async () => {
    const { ChildStdioChannel } = loadModule("stdio_worker.ts");
    const child = new FakeChildProcess();
    const deadlineObserved = deferred();
    const channel = new ChildStdioChannel(child, {
        gracefulExitTimeoutMs: 15_000,
        waitForExit: async (_exit, timeoutMs) => {
            assert.equal(timeoutMs, 15_000);
            deadlineObserved.resolve();
            return false;
        },
    });

    let closed = false;
    const closing = channel.close().then(() => { closed = true; });
    await deadlineObserved.promise;
    await child.killObserved.promise;

    assert.deepEqual(child.killSignals, ["SIGKILL"]);
    assert.equal(closed, false);

    child.emitExit(null, "SIGKILL");
    await closing;
    assert.equal(closed, true);
});

test("RPC write timeout never truncates terminal stdio process join", async () => {
    const { ChildStdioChannel } = loadModule("stdio_worker.ts");
    const { JsonRpcPeer } = loadModule("json_rpc.ts");
    const child = new FakeChildProcess();
    const deadlineObserved = deferred();
    const channel = new ChildStdioChannel(child, {
        gracefulExitTimeoutMs: 15_000,
        waitForExit: async () => {
            deadlineObserved.resolve();
            return false;
        },
    });
    const peer = new JsonRpcPeer(channel, { writeTimeoutMs: 1 });

    child.stdin.emit("error", new Error("stdin failed"));
    await deadlineObserved.promise;
    await child.killObserved.promise;

    let joined = false;
    const closing = peer.close().then(() => { joined = true; });
    // The former implementation treated writeTimeoutMs as a close deadline and
    // returned here even though SIGKILL had not produced an exit/close event.
    await delay(20);
    assert.equal(joined, false);
    assert.deepEqual(child.killSignals, ["SIGKILL"]);

    // Failed spawn/stream paths may produce close without an exit event.
    child.emitClose(null, "SIGKILL");
    await closing;
    assert.equal(joined, true);
});

test("JsonRpcPeer close remains pending when an underlying channel join never settles", async () => {
    const { JsonRpcPeer } = loadModule("json_rpc.ts");
    let closeCalls = 0;
    const never = new Promise(() => undefined);
    const channel = {
        write: async () => undefined,
        close: () => {
            closeCalls += 1;
            return never;
        },
        onData: () => () => undefined,
        onClose: () => () => undefined,
    };
    const peer = new JsonRpcPeer(channel, { writeTimeoutMs: 1 });

    let settled = false;
    const first = peer.close();
    const second = peer.close();
    void first.then(() => { settled = true; });
    await delay(20);

    assert.equal(first, second);
    assert.equal(closeCalls, 1);
    assert.equal(settled, false);
});
