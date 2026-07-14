const assert = require("node:assert/strict");
const { EventEmitter } = require("node:events");
const { readFileSync } = require("node:fs");
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
        this.closed = new Set();
        this.peer = null;
        this.isClosed = false;
        this.writes = [];
    }

    connect(peer) {
        this.peer = peer;
    }

    async write(data) {
        if (this.isClosed) throw new Error("closed");
        const copy = Buffer.from(data);
        this.writes.push(copy);
        queueMicrotask(() => {
            for (const listener of this.peer?.data ?? []) listener(copy);
        });
    }

    async close() {
        if (this.isClosed) return;
        this.isClosed = true;
        for (const listener of this.closed) listener();
        if (this.peer && !this.peer.isClosed) {
            this.peer.isClosed = true;
            for (const listener of this.peer.closed) listener();
        }
    }

    onData(listener) {
        this.data.add(listener);
        return () => this.data.delete(listener);
    }

    onClose(listener) {
        this.closed.add(listener);
        return () => this.closed.delete(listener);
    }
}

class HungWriteChannel {
    constructor() {
        this.data = new Set();
        this.closed = new Set();
        this.closeCalls = 0;
    }

    write() {
        return new Promise(() => undefined);
    }

    async close() {
        this.closeCalls += 1;
        for (const listener of this.closed) listener();
    }

    onData(listener) {
        this.data.add(listener);
        return () => this.data.delete(listener);
    }

    onClose(listener) {
        this.closed.add(listener);
        return () => this.closed.delete(listener);
    }
}

class PausableMemorySocket extends EventEmitter {
    constructor() {
        super();
        this.destroyed = false;
        this.paused = true;
        this.pendingData = [];
        this.writes = [];
        this.onWrite = undefined;
        this.flushScheduled = false;
    }

    pause() {
        this.paused = true;
        return this;
    }

    resume() {
        if (this.destroyed) return this;
        this.paused = false;
        if (!this.flushScheduled) {
            this.flushScheduled = true;
            queueMicrotask(() => {
                this.flushScheduled = false;
                while (!this.paused && this.pendingData.length > 0) {
                    this.emit("data", this.pendingData.shift());
                }
            });
        }
        return this;
    }

    pushData(data) {
        const copy = Buffer.from(data);
        if (this.paused) this.pendingData.push(copy);
        else this.emit("data", copy);
    }

    write(data, callback) {
        const copy = Buffer.from(data);
        this.writes.push(copy);
        this.onWrite?.(copy);
        queueMicrotask(() => callback?.());
        return true;
    }

    destroy() {
        if (this.destroyed) return this;
        this.destroyed = true;
        queueMicrotask(() => this.emit("close"));
        return this;
    }
}

function channelPair() {
    const left = new MemoryChannel();
    const right = new MemoryChannel();
    left.connect(right);
    right.connect(left);
    return [left, right];
}

function decodedWrites(channel) {
    const { FrameDecoder } = loadModule("framing.ts");
    const decoder = new FrameDecoder();
    return channel.writes.flatMap((frame) => decoder.feed(frame));
}

function schemaRoot() {
    return path.join(__dirname, "../../../../packages/offeragent-harness/schema");
}

async function nextTasks(count = 2) {
    for (let index = 0; index < count; index += 1) {
        await new Promise((resolve) => setImmediate(resolve));
    }
}

function makeHandshakeFixture(namedPipe, framing) {
    const now = Date.now();
    const pipeName = `\\\\.\\pipe\\OfferAgent.${"a".repeat(64)}`;
    const bootstrapNonce = Buffer.alloc(32, 11);
    const challenge = Buffer.alloc(32, 23);
    const expiresAt = new Date(now + 30_000).toISOString();
    const material = {
        pipeName,
        bootstrapNonce,
        issuedAt: new Date(now - 1_000),
        expiresAt: new Date(now + 60_000),
    };
    const serverProof = namedPipe.handshakeProof(
        bootstrapNonce,
        Buffer.from("server"),
        Buffer.from(pipeName),
        challenge,
        Buffer.from(expiresAt),
    );
    const challengeFrame = framing.encodeFrame({
        jsonrpc: "2.0",
        id: "challenge-1",
        method: "transport/challenge",
        params: {
            challenge: challenge.toString("base64url"),
            expiresAt,
            serverProof: serverProof.toString("base64url"),
        },
    }, 16 * 1024);
    return { challengeFrame, material };
}

test("length-prefixed decoder preserves split and coalesced UTF-8 frames", () => {
    const { encodeFrame, FrameDecoder } = loadModule("framing.ts");
    const first = encodeFrame({ jsonrpc: "2.0", method: "event", params: { text: "你好" } });
    const second = encodeFrame({ jsonrpc: "2.0", id: 7, result: null });
    const bytes = Buffer.concat([first, second]);
    const decoder = new FrameDecoder();

    assert.deepEqual(decoder.feed(bytes.subarray(0, 3)), []);
    assert.deepEqual(decoder.feed(bytes.subarray(3, first.length + 2)), [
        { jsonrpc: "2.0", method: "event", params: { text: "你好" } },
    ]);
    assert.deepEqual(decoder.feed(bytes.subarray(first.length + 2)), [
        { jsonrpc: "2.0", id: 7, result: null },
    ]);
    decoder.end();
});

test("decoder rejects zero, oversized, malformed UTF-8, and partial frames", () => {
    const { FrameDecoder, ProtocolFrameError } = loadModule("framing.ts");
    const zero = Buffer.alloc(4);
    assert.throws(() => new FrameDecoder(16).feed(zero), ProtocolFrameError);
    const oversized = Buffer.alloc(4);
    oversized.writeUInt32BE(17);
    assert.throws(() => new FrameDecoder(16).feed(oversized), ProtocolFrameError);
    const malformed = Buffer.from([0, 0, 0, 2, 0xc3, 0x28]);
    assert.throws(() => new FrameDecoder(16).feed(malformed), ProtocolFrameError);
    const partial = new FrameDecoder();
    partial.feed(Buffer.from([0, 0, 0, 5, 0x7b]));
    assert.throws(() => partial.end(), ProtocolFrameError);
});

test("full-duplex RPC supports concurrent requests, notifications, and reverse handlers", async () => {
    const { JsonRpcPeer } = loadModule("json_rpc.ts");
    const [leftChannel, rightChannel] = channelPair();
    const left = new JsonRpcPeer(leftChannel, { requestTimeoutMs: 1_000 });
    const right = new JsonRpcPeer(rightChannel, { requestTimeoutMs: 1_000 });
    right.register("sum", async (params) => params.a + params.b);
    left.register("client/context/get", async () => ({ activeFile: "notes/a.md" }));
    const notifications = [];
    right.onNotification("event", (params) => notifications.push(params.revision));

    const [sum, context] = await Promise.all([
        left.request("sum", { a: 2, b: 5 }),
        right.request("client/context/get", {}),
        left.notify("event", { revision: 3 }),
    ]);

    assert.equal(sum, 7);
    assert.deepEqual(context, { activeFile: "notes/a.md" });
    assert.deepEqual(notifications, [3]);
    await left.close();
    await right.close();
});

test("request cancellation reaches the active reverse handler", async () => {
    const { JsonRpcPeer, RpcRequestCancelledError } = loadModule("json_rpc.ts");
    const [leftChannel, rightChannel] = channelPair();
    const left = new JsonRpcPeer(leftChannel, { requestTimeoutMs: 1_000 });
    const right = new JsonRpcPeer(rightChannel, { requestTimeoutMs: 1_000 });
    let handlerSignal;
    right.register("wait", async (_params, context) => {
        handlerSignal = context.signal;
        return new Promise((resolve) => context.signal.addEventListener("abort", () => resolve("cancelled"), { once: true }));
    });
    const controller = new AbortController();
    const pending = left.request("wait", {}, { signal: controller.signal });
    await new Promise((resolve) => setImmediate(resolve));
    controller.abort();

    await assert.rejects(pending, (error) => error instanceof RpcRequestCancelledError && error.name === "AbortError");
    await nextTasks(3);
    assert.equal(handlerSignal.aborted, true);
    const fixture = JSON.parse(readFileSync(path.join(schemaRoot(), "examples/rpc-cancel.notification.json"), "utf8"));
    const cancellations = decodedWrites(leftChannel).filter((message) => message.method === fixture.method);
    assert.equal(cancellations.length, 1);
    assert.deepEqual(Object.keys(cancellations[0]).sort(), Object.keys(fixture).sort());
    assert.deepEqual(Object.keys(cancellations[0].params), ["requestId"]);
    assert.equal(left.closed, false);
    assert.equal(right.closed, false);
    right.register("echo", async (params) => params.value);
    assert.equal(await left.request("echo", { value: "still-open" }), "still-open");
    await left.close();
});

test("request deadline covers a stalled transport write without an unhandled rejection", async () => {
    const { JsonRpcPeer, RpcRequestTimeoutError } = loadModule("json_rpc.ts");
    const channel = new HungWriteChannel();
    const peer = new JsonRpcPeer(channel, { requestTimeoutMs: 20, writeTimeoutMs: 1_000 });
    const unhandled = [];
    const onUnhandled = (error) => unhandled.push(error);
    process.on("unhandledRejection", onUnhandled);
    try {
        const request = peer.request("runtime/status", {});
        const bounded = new Promise((resolve, reject) => {
            const timer = setTimeout(() => reject(new Error("request remained hung past its total deadline")), 250);
            void request.then(
                (value) => { clearTimeout(timer); resolve(value); },
                (error) => { clearTimeout(timer); reject(error); },
            );
        });
        await assert.rejects(bounded, RpcRequestTimeoutError);
        await new Promise((resolve) => setTimeout(resolve, 25));
        assert.deepEqual(unhandled, []);
        assert.equal(peer.closed, true);
        assert.equal(channel.closeCalls >= 1, true);
    } finally {
        process.off("unhandledRejection", onUnhandled);
        await peer.close();
    }
});

test("explicit close aborts a stalled write instead of waiting for writeTail", async () => {
    const { JsonRpcPeer, RpcDisconnectedError } = loadModule("json_rpc.ts");
    const channel = new HungWriteChannel();
    const peer = new JsonRpcPeer(channel, { requestTimeoutMs: 1_000, writeTimeoutMs: 1_000 });
    const request = peer.request("runtime/status", {});
    const rejected = assert.rejects(request, RpcDisconnectedError);
    await nextTasks(1);
    const close = peer.close();
    const boundedClose = Promise.race([
        close,
        new Promise((_resolve, reject) => setTimeout(() => reject(new Error("close waited for stalled write")), 250)),
    ]);
    await boundedClose;
    await rejected;
    assert.equal(channel.closeCalls, 1);
});

test("Worker-to-plugin cancellation uses the authoritative notification and keeps the peer usable", async () => {
    const { JsonRpcPeer, RpcRequestCancelledError } = loadModule("json_rpc.ts");
    const [pluginChannel, workerChannel] = channelPair();
    const plugin = new JsonRpcPeer(pluginChannel, { requestTimeoutMs: 1_000 });
    const worker = new JsonRpcPeer(workerChannel, { requestTimeoutMs: 1_000 });
    let pluginSignal;
    plugin.register("client/tool/invoke", async (_params, context) => {
        pluginSignal = context.signal;
        return new Promise((resolve) => context.signal.addEventListener("abort", () => resolve({ status: "cancelled" }), {
            once: true,
        }));
    });
    const controller = new AbortController();
    const pending = worker.request("client/tool/invoke", {}, { signal: controller.signal });
    await nextTasks(1);
    controller.abort();

    await assert.rejects(pending, (error) => error instanceof RpcRequestCancelledError);
    await nextTasks(3);
    assert.equal(pluginSignal.aborted, true);
    assert.equal(decodedWrites(workerChannel).some((message) => message.method === "rpc/cancel"), true);
    assert.equal(plugin.closed, false);
    assert.equal(worker.closed, false);
    await worker.close();
});

test("one late response to an abandoned request is ignored but a duplicate still poisons the peer", async () => {
    const { JsonRpcPeer, RpcRequestCancelledError } = loadModule("json_rpc.ts");
    const { encodeFrame } = loadModule("framing.ts");
    const [leftChannel, rightChannel] = channelPair();
    const left = new JsonRpcPeer(leftChannel, { requestTimeoutMs: 1_000 });
    const controller = new AbortController();
    const pending = left.request("runtime/ping", { nonce: "req_1234567890abcdef" }, { signal: controller.signal });
    await nextTasks(1);
    const request = decodedWrites(leftChannel).find((message) => message.method === "runtime/ping");
    controller.abort();
    await assert.rejects(pending, (error) => error instanceof RpcRequestCancelledError);
    const late = { jsonrpc: "2.0", id: request.id, result: { nonce: "req_1234567890abcdef" } };

    await rightChannel.write(encodeFrame(late));
    await nextTasks(1);
    assert.equal(left.closed, false);
    await rightChannel.write(encodeFrame(late));
    await nextTasks(1);
    assert.equal(left.closed, true);
});

test("malformed error responses reject the still-pending request instead of leaving a hung Promise", async () => {
    const { JsonRpcPeer } = loadModule("json_rpc.ts");
    const { encodeFrame } = loadModule("framing.ts");
    const invalidErrors = [
        { code: -32000, message: "Request failed" },
        {
            code: -31999,
            message: "Request failed",
            data: { cancelled: false, code: "internal.error", retryable: false, userVisibleMessage: "安全错误。" },
        },
        {
            code: -32000,
            message: "Request failed",
            data: {
                cancelled: false,
                code: "internal.error",
                extra: true,
                retryable: false,
                userVisibleMessage: "安全错误。",
            },
        },
        {
            code: -32000,
            message: "Request failed",
            data: { cancelled: "no", code: "internal.error", retryable: false, userVisibleMessage: "安全错误。" },
        },
    ];
    for (const errorEnvelope of invalidErrors) {
        const [leftChannel, rightChannel] = channelPair();
        const left = new JsonRpcPeer(leftChannel, { requestTimeoutMs: 1_000 });
        const pending = left.request("runtime/status", {});
        await nextTasks(1);
        const request = decodedWrites(leftChannel).find((message) => message.method === "runtime/status");
        await rightChannel.write(encodeFrame({ jsonrpc: "2.0", id: request.id, error: errorEnvelope }));
        const outcome = await Promise.race([
            pending.then(
                () => ({ state: "resolved" }),
                (error) => ({ state: "rejected", error }),
            ),
            new Promise((resolve) => setTimeout(() => resolve({ state: "hung" }), 100)),
        ]);
        assert.equal(outcome.state, "rejected");
        assert.equal(outcome.error.name, "ProtocolFrameError");
        assert.equal(left.closed, true);
    }
});

test("RemoteRpcError exposes only the validated user-visible envelope and never the outer wire message", async () => {
    const { JsonRpcPeer, RemoteRpcError } = loadModule("json_rpc.ts");
    const { encodeFrame } = loadModule("framing.ts");
    const [leftChannel, rightChannel] = channelPair();
    const left = new JsonRpcPeer(leftChannel, { requestTimeoutMs: 1_000 });
    const pending = left.request("runtime/status", {});
    await nextTasks(1);
    const request = decodedWrites(leftChannel).find((message) => message.method === "runtime/status");
    await rightChannel.write(encodeFrame({
        jsonrpc: "2.0",
        id: request.id,
        error: {
            code: -32000,
            message: "RAW_UNSAFE_PATH_OR_SECRET",
            data: {
                cancelled: false,
                code: "provider.auth_required",
                retryable: false,
                userVisibleMessage: "请在设置中配置模型凭据。",
            },
        },
    }));

    await assert.rejects(pending, (error) => {
        assert.equal(error instanceof RemoteRpcError, true);
        assert.equal(error.message, "请在设置中配置模型凭据。");
        assert.equal(error.rpcCode, -32000);
        assert.equal(error.envelope.code, "provider.auth_required");
        assert.doesNotMatch(error.message, /RAW_UNSAFE/);
        return true;
    });
});

test("reverse request failures always emit the authoritative closed ErrorEnvelope without raw exceptions", async () => {
    const { JsonRpcPeer } = loadModule("json_rpc.ts");
    const { encodeFrame } = loadModule("framing.ts");
    const manifest = JSON.parse(readFileSync(path.join(schemaRoot(), "protocol-manifest.json"), "utf8"));
    const schema = JSON.parse(readFileSync(path.join(schemaRoot(), manifest.schemaBundle), "utf8"));
    for (const scenario of [
        { id: "worker_unknown", method: "client/unknown", expectedCode: "protocol.method_not_found" },
        { id: "worker_failure", method: "client/context/get", expectedCode: "protocol.internal_error" },
    ]) {
        const [pluginChannel, workerChannel] = channelPair();
        const plugin = new JsonRpcPeer(pluginChannel);
        if (scenario.method === "client/context/get") {
            plugin.register(scenario.method, () => { throw new Error("RAW_HANDLER_SECRET"); });
        }
        await workerChannel.write(encodeFrame({ jsonrpc: "2.0", id: scenario.id, method: scenario.method, params: {} }));
        await nextTasks(2);
        const response = decodedWrites(pluginChannel).find((message) => message.id === scenario.id);
        assert.ok(response?.error);
        assert.deepEqual(Object.keys(response.error).sort(), ["code", "data", "message"]);
        assert.deepEqual(Object.keys(response.error.data).sort(), [...schema.$defs.ErrorEnvelope.required].sort());
        assert.equal(response.error.data.code, scenario.expectedCode);
        assert.equal(schema.$defs.ErrorCode.enum.includes(response.error.data.code), true);
        assert.equal(typeof response.error.data.cancelled, "boolean");
        assert.equal(typeof response.error.data.retryable, "boolean");
        assert.equal(typeof response.error.data.userVisibleMessage, "string");
        assert.doesNotMatch(JSON.stringify(response), /RAW_HANDLER_SECRET/);
        await plugin.close();
    }
});

test("unknown response ids poison the connection instead of crossing request boundaries", async () => {
    const { JsonRpcPeer, RpcDisconnectedError } = loadModule("json_rpc.ts");
    const { encodeFrame } = loadModule("framing.ts");
    const [leftChannel, rightChannel] = channelPair();
    const left = new JsonRpcPeer(leftChannel);

    await rightChannel.write(encodeFrame({ jsonrpc: "2.0", id: "unknown", result: null }));
    await new Promise((resolve) => setImmediate(resolve));

    assert.equal(left.closed, true);
    await assert.rejects(() => left.request("runtime/ping", {}), RpcDisconnectedError);
});

test("handshake proof is byte-compatible with the Worker implementation", () => {
    const { handshakeProof } = loadModule("named_pipe.ts");
    const key = Buffer.from(Array.from({ length: 32 }, (_, index) => index));
    const proof = handshakeProof(
        key,
        Buffer.from("client"),
        Buffer.from("pipe"),
        Buffer.from(Array.from({ length: 32 }, (_, index) => 31 - index)),
        Buffer.from("nonce"),
        Buffer.from("2026-07-13T00:00:00+00:00"),
    );
    assert.equal(proof.toString("hex"), "8e6e0ced93d1073c09ec98801648ea5b057bae3086a90562af85e3fb84bb4042");
});

test("Named Pipe connect rechecks cancellation after asynchronous Host discovery", async () => {
    const namedPipe = loadModule("named_pipe.ts");
    const framing = loadModule("framing.ts");
    const { material } = makeHandshakeFixture(namedPipe, framing);
    const controller = new AbortController();
    const client = new namedPipe.NamedPipeClient({
        load: async () => {
            controller.abort();
            return material;
        },
    });

    await assert.rejects(client.connect(controller.signal), (error) => error?.name === "AbortError");
});

test("authentication accepts exactly one challenge frame at every input split point", async () => {
    const namedPipe = loadModule("named_pipe.ts");
    const framing = loadModule("framing.ts");
    const { challengeFrame, material } = makeHandshakeFixture(namedPipe, framing);

    for (let split = 0; split <= challengeFrame.length; split += 1) {
        const [clientChannel, serverChannel] = channelPair();
        const pending = namedPipe.authenticateClient(
            clientChannel,
            material,
            1_000,
            () => Buffer.alloc(32, 31),
        );
        if (split > 0) await serverChannel.write(challengeFrame.subarray(0, split));
        if (split < challengeFrame.length) await serverChannel.write(challengeFrame.subarray(split));
        await pending;
        const responses = decodedWrites(clientChannel);
        assert.equal(responses.length, 1, `split ${split} must produce one client proof`);
        assert.equal(responses[0].id, "challenge-1");
        assert.deepEqual(Object.keys(responses[0]).sort(), ["id", "jsonrpc", "result"]);
    }
});

test("authentication fails closed when a challenge chunk contains any bytes of a following frame", async () => {
    const namedPipe = loadModule("named_pipe.ts");
    const framing = loadModule("framing.ts");
    const { challengeFrame, material } = makeHandshakeFixture(namedPipe, framing);
    const followingFrame = framing.encodeFrame({
        jsonrpc: "2.0",
        id: "must-not-cross-auth-boundary",
        method: "runtime/status",
        params: {},
    });

    // Exhaust every boundary: partial length header (1..3), complete header (4),
    // every partial payload, and the complete second frame.
    for (let trailingBytes = 1; trailingBytes <= followingFrame.length; trailingBytes += 1) {
        const [clientChannel, serverChannel] = channelPair();
        const pending = namedPipe.authenticateClient(
            clientChannel,
            material,
            1_000,
            () => Buffer.alloc(32, 31),
        );
        await serverChannel.write(Buffer.concat([
            challengeFrame,
            followingFrame.subarray(0, trailingBytes),
        ]));
        await assert.rejects(
            pending,
            (error) => error instanceof namedPipe.NamedPipeAuthenticationError &&
                error.message === "handshake chunk contains trailing frame bytes",
            `trailing prefix length ${trailingBytes} must be rejected`,
        );
        assert.equal(clientChannel.writes.length, 0);
    }
});

test("NetSocketChannel preserves a separately received RPC frame across authentication hand-off", async () => {
    const namedPipe = loadModule("named_pipe.ts");
    const framing = loadModule("framing.ts");
    const { JsonRpcPeer } = loadModule("json_rpc.ts");
    const { challengeFrame, material } = makeHandshakeFixture(namedPipe, framing);
    const socket = new PausableMemorySocket();
    const channel = new namedPipe.NetSocketChannel(socket);
    const followingFrame = framing.encodeFrame({
        jsonrpc: "2.0",
        id: "handoff-request",
        method: "client/not-registered",
        params: {},
    });
    let injectedDuringHandoff = false;
    socket.onWrite = () => {
        if (injectedDuringHandoff) return;
        injectedDuringHandoff = true;
        // The authentication reader has already detached at this point. A real
        // Socket may receive the first RPC frame before JsonRpcPeer is constructed.
        socket.pushData(followingFrame);
    };

    const authentication = namedPipe.authenticateClient(
        channel,
        material,
        1_000,
        () => Buffer.alloc(32, 31),
    );
    socket.pushData(challengeFrame);
    await authentication;
    assert.equal(socket.paused, true);
    assert.equal(socket.pendingData.length, 1);

    const peer = new JsonRpcPeer(channel, { requestTimeoutMs: 1_000 });
    await nextTasks(4);
    const handoffResponse = decodedWrites(socket).find((message) => message.id === "handoff-request");
    assert.ok(handoffResponse?.error, "the post-authentication frame must reach JsonRpcPeer");
    assert.equal(socket.pendingData.length, 0);
    await peer.close();
});

test("NetSocketChannel owns Socket errors across the authentication-to-peer hand-off", async () => {
    const namedPipe = loadModule("named_pipe.ts");
    const framing = loadModule("framing.ts");
    const { JsonRpcPeer } = loadModule("json_rpc.ts");
    const { challengeFrame, material } = makeHandshakeFixture(namedPipe, framing);
    const socket = new PausableMemorySocket();
    const channel = new namedPipe.NetSocketChannel(socket);
    socket.onWrite = () => {
        assert.doesNotThrow(() => socket.emit("error", new Error("simulated reset during hand-off")));
        socket.destroy();
    };

    const authentication = namedPipe.authenticateClient(
        channel,
        material,
        1_000,
        () => Buffer.alloc(32, 31),
    );
    socket.pushData(challengeFrame);
    await authentication;
    const peer = new JsonRpcPeer(channel, { requestTimeoutMs: 1_000 });
    await nextTasks(3);

    assert.equal(peer.closed, true);
    await peer.close();
});

test("canonical discovery parser rejects expiry, aliases, and added fields", () => {
    const { parseDiscoveryMaterial, NamedPipeAuthenticationError } = loadModule("named_pipe.ts");
    const now = new Date("2026-07-13T02:00:00.000Z");
    const nonce = Buffer.alloc(32, 7).toString("base64url");
    const raw = {
        bootstrapNonce: nonce,
        expiresAt: "2026-07-13T03:00:00+00:00",
        issuedAt: "2026-07-13T01:00:00+00:00",
        pipeName: `\\\\.\\pipe\\OfferAgent.${"a".repeat(64)}`,
        schemaVersion: 1,
    };
    const payload = Buffer.from(JSON.stringify(raw) + "\n");
    const parsed = parseDiscoveryMaterial(payload, now);
    assert.equal(parsed.pipeName, raw.pipeName);
    assert.equal(parsed.bootstrapNonce.length, 32);

    assert.throws(
        () => parseDiscoveryMaterial(Buffer.from(JSON.stringify({ ...raw, extra: true }) + "\n"), now),
        NamedPipeAuthenticationError,
    );
    assert.throws(
        () => parseDiscoveryMaterial(Buffer.from(JSON.stringify({ ...raw, bootstrapNonce: `${nonce}=` }) + "\n"), now),
        NamedPipeAuthenticationError,
    );
    assert.throws(
        () => parseDiscoveryMaterial(payload, new Date("2026-07-13T03:00:00.000Z")),
        NamedPipeAuthenticationError,
    );
});

test("Host stop result accepts only bounded canonical closed-schema receipts", () => {
    const { parseHostStopResult, NamedPipeAuthenticationError } = loadModule("named_pipe.ts");
    assert.deepEqual(
        parseHostStopResult(Buffer.from('{"schemaVersion":1,"status":"stopped","type":"result"}\n')),
        { status: "stopped" },
    );
    assert.deepEqual(
        parseHostStopResult(Buffer.from('{"schemaVersion":1,"status":"already_stopped","type":"result"}\n')),
        { status: "already_stopped" },
    );
    assert.throws(
        () => parseHostStopResult(Buffer.from('{"status":"stopped","schemaVersion":1,"type":"result"}\n')),
        NamedPipeAuthenticationError,
    );
    assert.throws(
        () => parseHostStopResult(Buffer.from('{"extra":true,"schemaVersion":1,"status":"stopped","type":"result"}\n')),
        NamedPipeAuthenticationError,
    );
});
