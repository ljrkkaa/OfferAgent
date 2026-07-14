import { Buffer } from "node:buffer";
import { ChildProcess, spawn } from "node:child_process";
import { createHmac, randomBytes, timingSafeEqual } from "node:crypto";
import { Socket } from "node:net";
import { isAbsolute } from "node:path";
import { Readable, Writable } from "node:stream";

import { encodeFrame, FrameDecoder } from "./framing";
import {
    ByteChannel,
    JsonObject,
    JsonRpcPeer,
    JsonRpcPeerOptions,
    JsonValue,
    RpcDisconnectedError,
    requireJsonObject,
} from "./json_rpc";
import { parseStrictJson } from "./strict_json";

const PIPE_PATTERN = /^\\\\\.\\pipe\\OfferAgent\.[0-9a-f]{64}$/;
const BASE64URL_PATTERN = /^[A-Za-z0-9_-]+$/;
const HANDSHAKE_DOMAIN = Buffer.from("OfferAgent.NamedPipe.Handshake.v1", "utf8");
const MAX_DISCOVERY_BYTES = 64 * 1024;
const MAX_HOST_STOP_RESULT_BYTES = 4 * 1024;
const DISCOVERY_SCHEMA_VERSION = 1;

export interface DiscoveryMaterial {
    readonly pipeName: string;
    readonly bootstrapNonce: Buffer;
    readonly issuedAt: Date;
    readonly expiresAt: Date;
}

export interface DiscoveryMaterialLoader {
    load(signal?: AbortSignal): Promise<DiscoveryMaterial>;
}

export interface HostStopResult {
    readonly status: "stopped" | "already_stopped";
}

export interface NamedPipeClientOptions extends JsonRpcPeerOptions {
    connectTimeoutMs?: number;
    handshakeTimeoutMs?: number;
    nonceSource?: (bytes: number) => Buffer;
}

export type NamedPipeFailureCode =
    | "pipe_connect_failed"
    | "pipe_connect_timeout"
    | "pipe_handshake_closed"
    | "pipe_handshake_timeout"
    | "host_start_failed"
    | "host_attach_rejected"
    | "host_discovery_timeout"
    | "host_discovery_failed"
    | "pipe_authentication_failed";

export class NamedPipeConnectionError extends Error {
    readonly failureCode: NamedPipeFailureCode;
    readonly retryable: boolean;

    constructor(failureCode: NamedPipeFailureCode, message: string, retryable: boolean) {
        super(message);
        this.name = "NamedPipeConnectionError";
        this.failureCode = failureCode;
        this.retryable = retryable;
    }
}

export class NamedPipeAuthenticationError extends NamedPipeConnectionError {
    constructor(message: string) {
        super("pipe_authentication_failed", message, false);
        this.name = "NamedPipeAuthenticationError";
    }
}

export class NetSocketChannel implements ByteChannel {
    private readonly socket: Socket;
    private readonly closeListeners = new Set<(error?: Error) => void>();
    private activeDataListeners = 0;
    private terminalError: RpcDisconnectedError | undefined;
    private terminalClosed = false;

    constructor(socket: Socket) {
        this.socket = socket;
        // A Socket stays paused whenever no protocol consumer is attached. This is
        // important during the synchronous hand-off from the authentication reader
        // to JsonRpcPeer: bytes received in that gap remain in Node's readable
        // buffer instead of being emitted and discarded in flowing mode.
        this.socket.pause();
        // EventEmitter treats an unhandled `error` event as an uncaught
        // exception.  Keep this internal listener for the entire Socket lifetime,
        // including both authentication hand-off gaps where no protocol consumer
        // is registered yet.
        this.socket.on("error", () => {
            this.terminalError = new RpcDisconnectedError();
        });
        this.socket.on("close", () => {
            if (this.terminalClosed) return;
            this.terminalClosed = true;
            const error = this.terminalError;
            for (const listener of [...this.closeListeners]) listener(error);
            this.closeListeners.clear();
        });
    }

    write(data: Uint8Array): Promise<void> {
        return new Promise((resolve, reject) => {
            if (this.socket.destroyed || this.terminalClosed) {
                reject(new RpcDisconnectedError());
                return;
            }
            this.socket.write(data, (error?: Error | null) => error ? reject(new RpcDisconnectedError()) : resolve());
        });
    }

    close(): Promise<void> {
        return new Promise((resolve) => {
            if (this.terminalClosed) {
                resolve();
                return;
            }
            const dispose = this.onClose(() => {
                dispose();
                resolve();
            });
            if (!this.socket.destroyed) this.socket.destroy();
        });
    }

    onData(listener: (chunk: Uint8Array) => void): () => void {
        if (this.terminalClosed) return () => undefined;
        let disposed = false;
        const wrapped = (chunk: Uint8Array) => listener(chunk);
        this.socket.on("data", wrapped);
        this.activeDataListeners += 1;
        if (this.activeDataListeners === 1) this.socket.resume();
        return () => {
            if (disposed) return;
            disposed = true;
            this.socket.off("data", wrapped);
            this.activeDataListeners -= 1;
            if (this.activeDataListeners === 0 && !this.socket.destroyed) this.socket.pause();
        };
    }

    onClose(listener: (error?: Error) => void): () => void {
        let disposed = false;
        if (this.terminalClosed) {
            queueMicrotask(() => {
                if (!disposed) listener(this.terminalError);
            });
            return () => { disposed = true; };
        }
        this.closeListeners.add(listener);
        return () => {
            if (disposed) return;
            disposed = true;
            this.closeListeners.delete(listener);
        };
    }
}

export class NamedPipeClient {
    private readonly loader: DiscoveryMaterialLoader;
    private readonly options: NamedPipeClientOptions;

    constructor(loader: DiscoveryMaterialLoader, options: NamedPipeClientOptions = {}) {
        this.loader = loader;
        this.options = options;
    }

    async connect(signal?: AbortSignal): Promise<JsonRpcPeer> {
        if (signal?.aborted) throw abortError();
        const material = await this.loader.load(signal);
        signal?.throwIfAborted();
        requireFresh(material, new Date());
        const socket = await connectSocket(
            material.pipeName,
            this.options.connectTimeoutMs ?? 10_000,
            signal,
        );
        const channel = new NetSocketChannel(socket);
        try {
            await authenticateClient(
                channel,
                material,
                this.options.handshakeTimeoutMs ?? 10_000,
                this.options.nonceSource ?? randomBytes,
                signal,
            );
            return new JsonRpcPeer(channel, this.options);
        } catch (error) {
            await channel.close().catch(() => undefined);
            throw error;
        }
    }
}

export class HostDiscoveryLoader implements DiscoveryMaterialLoader {
    private readonly hostExecutable: string;
    private readonly vaultRoot: string;
    private readonly timeoutMs: number;
    private readonly beforeLaunch?: (signal?: AbortSignal) => Promise<void>;

    constructor(hostExecutable: string, vaultRoot: string, options: {
        timeoutMs?: number;
        beforeLaunch?: (signal?: AbortSignal) => Promise<void>;
    } = {}) {
        if (!isAbsolute(hostExecutable) || !/\.exe$/i.test(hostExecutable) || hostExecutable.includes("\0")) {
            throw new TypeError("Host executable must be an absolute .exe path");
        }
        if (!isAbsolute(vaultRoot) || vaultRoot.includes("\0") || /[\r\n]/.test(vaultRoot)) {
            throw new TypeError("Vault root must be an absolute local path");
        }
        this.hostExecutable = hostExecutable;
        this.vaultRoot = vaultRoot;
        this.timeoutMs = positiveInteger(options.timeoutMs ?? 10_000, "timeoutMs");
        this.beforeLaunch = options.beforeLaunch;
    }

    async load(signal?: AbortSignal): Promise<DiscoveryMaterial> {
        if (signal?.aborted) throw abortError();
        await this.beforeLaunch?.(signal);
        if (signal?.aborted) throw abortError();
        const plaintext = await requestDiscoveryFromHost(
            this.hostExecutable,
            this.vaultRoot,
            this.timeoutMs,
            signal,
        );
        try {
            return parseDiscoveryMaterial(plaintext);
        } finally {
            plaintext.fill(0);
        }
    }
}

export async function requestHostStop(
    executable: string,
    options: {
        timeoutMs?: number;
        signal?: AbortSignal;
        spawnProcess?: typeof spawn;
        beforeLaunch?: (signal?: AbortSignal) => Promise<void>;
    } = {},
): Promise<HostStopResult> {
    if (!isAbsolute(executable) || !/\.exe$/i.test(executable) || executable.includes("\0") || /[\r\n]/.test(executable)) {
        throw new TypeError("Host executable must be an absolute .exe path");
    }
    const timeoutMs = positiveInteger(options.timeoutMs ?? 60_000, "timeoutMs");
    options.signal?.throwIfAborted();
    await options.beforeLaunch?.(options.signal);
    options.signal?.throwIfAborted();
    return new Promise((resolve, reject) => {
        let child: ChildProcess;
        try {
            child = (options.spawnProcess ?? spawn)(executable, ["stop-all", "--result-fd", "3"], {
                windowsHide: true,
                detached: false,
                stdio: ["ignore", "ignore", "pipe", "pipe"],
                env: { SystemRoot: process.env.SystemRoot ?? "C:\\Windows", LOCALAPPDATA: process.env.LOCALAPPDATA ?? "" },
            });
        } catch {
            reject(new NamedPipeAuthenticationError("could not start the signed OfferAgent Host stop client"));
            return;
        }
        const resultStream = child.stdio[3];
        if (!(resultStream instanceof Readable)) {
            child.kill();
            reject(new NamedPipeAuthenticationError("OfferAgent Host did not open its private stop result handle"));
            return;
        }
        const chunks: Buffer[] = [];
        let size = 0;
        let resultEnded = false;
        let exited = false;
        let exitCode: number | null = null;
        let settled = false;
        const cleanup = () => {
            clearTimeout(timer);
            options.signal?.removeEventListener("abort", onAbort);
        };
        const failStop = (error: Error) => {
            if (settled) return;
            settled = true;
            cleanup();
            child.kill();
            reject(error);
        };
        const finish = () => {
            if (settled || !resultEnded || !exited) return;
            if (exitCode !== 0 || size === 0) {
                failStop(new NamedPipeAuthenticationError("OfferAgent Host rejected explicit stop"));
                return;
            }
            try {
                const result = parseHostStopResult(Buffer.concat(chunks, size));
                settled = true;
                cleanup();
                resolve(result);
            } catch (error) {
                failStop(error instanceof Error ? error : new Error("invalid Host stop result"));
            }
        };
        const onAbort = () => failStop(abortError());
        const timer = setTimeout(
            () => failStop(new NamedPipeAuthenticationError("OfferAgent Host explicit stop timed out")),
            timeoutMs,
        );
        options.signal?.addEventListener("abort", onAbort, { once: true });
        child.stdio[2]?.resume();
        resultStream.on("data", (chunk: Buffer) => {
            size += chunk.length;
            if (size > MAX_HOST_STOP_RESULT_BYTES) {
                failStop(new NamedPipeAuthenticationError("OfferAgent Host stop result exceeded its limit"));
                return;
            }
            chunks.push(Buffer.from(chunk));
        });
        resultStream.once("end", () => { resultEnded = true; finish(); });
        resultStream.once("error", () => failStop(
            new NamedPipeAuthenticationError("OfferAgent Host stop result handle failed"),
        ));
        child.once("error", () => failStop(new NamedPipeAuthenticationError("OfferAgent Host stop client failed")));
        child.once("exit", (code) => {
            exited = true;
            exitCode = code;
            finish();
        });
    });
}

export function parseHostStopResult(payload: Buffer): HostStopResult {
    if (!Buffer.isBuffer(payload) || payload.length === 0 || payload.length > MAX_HOST_STOP_RESULT_BYTES) {
        throw new NamedPipeAuthenticationError("Host stop result has an invalid size");
    }
    let value: JsonObject;
    try {
        value = requireJsonObject(parseStrictJson(
            new TextDecoder("utf-8", { fatal: true }).decode(payload),
            { maximumCharacters: MAX_HOST_STOP_RESULT_BYTES },
        ));
    } catch {
        throw new NamedPipeAuthenticationError("Host stop result is malformed");
    }
    requireExactKeys(value, ["schemaVersion", "status", "type"]);
    if (value.schemaVersion !== 1 || value.type !== "result" ||
        (value.status !== "stopped" && value.status !== "already_stopped")) {
        throw new NamedPipeAuthenticationError("Host stop result fields are invalid");
    }
    const canonical = Buffer.from(JSON.stringify({ schemaVersion: 1, status: value.status, type: "result" }) + "\n");
    if (!canonical.equals(payload)) throw new NamedPipeAuthenticationError("Host stop result is not canonical");
    return { status: value.status };
}

export function parseDiscoveryMaterial(payload: Buffer, now = new Date()): DiscoveryMaterial {
    if (!Buffer.isBuffer(payload) || payload.length === 0 || payload.length > MAX_DISCOVERY_BYTES) {
        throw new NamedPipeAuthenticationError("Named Pipe discovery material has an invalid size");
    }
    let value: JsonObject;
    try {
        const text = new TextDecoder("utf-8", { fatal: true }).decode(payload);
        value = requireJsonObject(parseStrictJson(text, { maximumCharacters: MAX_DISCOVERY_BYTES }));
    } catch (error) {
        throw new NamedPipeAuthenticationError("Named Pipe discovery material is malformed");
    }
    requireExactKeys(value, ["bootstrapNonce", "expiresAt", "issuedAt", "pipeName", "schemaVersion"]);
    if (value.schemaVersion !== DISCOVERY_SCHEMA_VERSION || typeof value.pipeName !== "string" ||
        typeof value.issuedAt !== "string" || typeof value.expiresAt !== "string" ||
        typeof value.bootstrapNonce !== "string") {
        throw new NamedPipeAuthenticationError("Named Pipe discovery material has invalid fields");
    }
    if (!PIPE_PATTERN.test(value.pipeName)) throw new NamedPipeAuthenticationError("invalid Named Pipe name");
    const nonce = decodeCanonicalNonce(value.bootstrapNonce);
    const issuedAt = parseTimestamp(value.issuedAt);
    const expiresAt = parseTimestamp(value.expiresAt);
    if (expiresAt.getTime() <= issuedAt.getTime()) {
        throw new NamedPipeAuthenticationError("discovery material has an invalid lifetime");
    }
    const canonical = Buffer.from(JSON.stringify({
        bootstrapNonce: value.bootstrapNonce,
        expiresAt: value.expiresAt,
        issuedAt: value.issuedAt,
        pipeName: value.pipeName,
        schemaVersion: DISCOVERY_SCHEMA_VERSION,
    }) + "\n", "utf8");
    if (!canonical.equals(payload)) throw new NamedPipeAuthenticationError("discovery material is not canonical");
    const material = { pipeName: value.pipeName, bootstrapNonce: nonce, issuedAt, expiresAt };
    requireFresh(material, now);
    return material;
}

export async function authenticateClient(
    channel: ByteChannel,
    material: DiscoveryMaterial,
    timeoutMs = 10_000,
    nonceSource: (bytes: number) => Buffer = randomBytes,
    signal?: AbortSignal,
): Promise<void> {
    positiveInteger(timeoutMs, "timeoutMs");
    requireFresh(material, new Date());
    const message = requireJsonObject(await readOneMessage(channel, timeoutMs, signal));
    signal?.throwIfAborted();
    requireExactKeys(message, ["jsonrpc", "id", "method", "params"]);
    if (message.jsonrpc !== "2.0" || message.method !== "transport/challenge") {
        throw new NamedPipeAuthenticationError("Worker did not present the required challenge");
    }
    if ((typeof message.id !== "string" || message.id.length === 0) &&
        (typeof message.id !== "number" || !Number.isSafeInteger(message.id))) {
        throw new NamedPipeAuthenticationError("Worker challenge id is invalid");
    }
    const params = requireJsonObject(message.params);
    requireExactKeys(params, ["challenge", "expiresAt", "serverProof"]);
    if (typeof params.challenge !== "string" || typeof params.expiresAt !== "string" ||
        typeof params.serverProof !== "string") {
        throw new NamedPipeAuthenticationError("Worker challenge fields are invalid");
    }
    const challenge = decodeCanonicalNonce(params.challenge);
    const serverProof = decodeCanonicalNonce(params.serverProof);
    const expiresAt = parseTimestamp(params.expiresAt);
    const current = new Date();
    if (current.getTime() >= expiresAt.getTime() || expiresAt.getTime() > material.expiresAt.getTime()) {
        throw new NamedPipeAuthenticationError("Worker challenge is expired or out of bounds");
    }
    const expected = handshakeProof(
        material.bootstrapNonce,
        Buffer.from("server"),
        Buffer.from(material.pipeName),
        challenge,
        Buffer.from(params.expiresAt),
    );
    if (!timingSafeEqual(serverProof, expected)) {
        throw new NamedPipeAuthenticationError("Worker challenge proof was rejected");
    }
    const clientNonce = nonceSource(32);
    if (!Buffer.isBuffer(clientNonce) || clientNonce.length !== 32) {
        throw new NamedPipeAuthenticationError("nonce source must return 256 random bits");
    }
    const proof = handshakeProof(
        material.bootstrapNonce,
        Buffer.from("client"),
        Buffer.from(material.pipeName),
        challenge,
        clientNonce,
        Buffer.from(params.expiresAt),
    );
    const response: JsonObject = {
        jsonrpc: "2.0",
        id: message.id,
        result: { clientNonce: base64url(clientNonce), clientProof: base64url(proof) },
    };
    await channel.write(encodeFrame(response, 16 * 1024));
    signal?.throwIfAborted();
}

export function handshakeProof(key: Buffer, label: Buffer, ...parts: Buffer[]): Buffer {
    if (key.length !== 32 || label.length === 0) throw new NamedPipeAuthenticationError("invalid handshake inputs");
    const hmac = createHmac("sha256", key);
    hmac.update(HANDSHAKE_DOMAIN);
    hmac.update(lengthPrefix(label));
    hmac.update(label);
    for (const part of parts) {
        hmac.update(lengthPrefix(part));
        hmac.update(part);
    }
    return hmac.digest();
}

async function connectSocket(pipeName: string, timeoutMs: number, signal?: AbortSignal): Promise<Socket> {
    if (signal?.aborted) throw abortError();
    if (!PIPE_PATTERN.test(pipeName)) throw new NamedPipeAuthenticationError("invalid Named Pipe name");
    positiveInteger(timeoutMs, "connectTimeoutMs");
    return new Promise((resolve, reject) => {
        const socket = new Socket();
        let settled = false;
        const cleanup = () => {
            clearTimeout(timer);
            signal?.removeEventListener("abort", onAbort);
            socket.off("error", onError);
            socket.off("connect", onConnect);
        };
        const fail = (error: Error) => {
            if (settled) return;
            settled = true;
            cleanup();
            socket.destroy();
            reject(error);
        };
        const onError = () => fail(new NamedPipeConnectionError(
            "pipe_connect_failed",
            "OfferAgent Named Pipe connection failed",
            true,
        ));
        const onAbort = () => fail(abortError());
        const onConnect = () => {
            if (settled) return;
            settled = true;
            cleanup();
            resolve(socket);
        };
        const timer = setTimeout(() => fail(new NamedPipeConnectionError(
            "pipe_connect_timeout",
            "OfferAgent Named Pipe connection timed out",
            true,
        )), timeoutMs);
        signal?.addEventListener("abort", onAbort, { once: true });
        socket.once("error", onError);
        socket.once("connect", onConnect);
        socket.connect(pipeName);
    });
}

async function readOneMessage(channel: ByteChannel, timeoutMs: number, signal?: AbortSignal): Promise<JsonValue> {
    if (signal?.aborted) throw abortError();
    return new Promise((resolve, reject) => {
        const decoder = new FrameDecoder(16 * 1024);
        let settled = false;
        const cleanup = () => {
            clearTimeout(timer);
            disposeData();
            disposeClose();
            signal?.removeEventListener("abort", onAbort);
        };
        const fail = (error: Error) => {
            if (settled) return;
            settled = true;
            cleanup();
            reject(error);
        };
        const onAbort = () => fail(abortError());
        const disposeData = channel.onData((chunk) => {
            try {
                const values = decoder.feed(chunk);
                if (values.length > 1 || (values.length === 1 && decoder.hasPendingFrame)) {
                    throw new NamedPipeAuthenticationError("handshake chunk contains trailing frame bytes");
                }
                if (values.length === 1) {
                    settled = true;
                    cleanup();
                    resolve(values[0] as JsonValue);
                }
            } catch (error) {
                fail(error instanceof Error ? error : new NamedPipeAuthenticationError("invalid handshake frame"));
            }
        });
        const disposeClose = channel.onClose(() => fail(new NamedPipeConnectionError(
            "pipe_handshake_closed",
            "Worker closed during handshake",
            true,
        )));
        const timer = setTimeout(() => fail(new NamedPipeConnectionError(
            "pipe_handshake_timeout",
            "Worker handshake timed out",
            true,
        )), timeoutMs);
        signal?.addEventListener("abort", onAbort, { once: true });
    });
}

function requestDiscoveryFromHost(
    executable: string,
    vaultRoot: string,
    timeoutMs: number,
    signal?: AbortSignal,
): Promise<Buffer> {
    return new Promise((resolve, reject) => {
        let child: ChildProcess;
        try {
            child = spawn(executable, [
                "attach",
                "--discovery-fd", "3",
                "--vault-root-fd", "4",
            ], {
                windowsHide: true,
                detached: false,
                stdio: ["ignore", "ignore", "pipe", "pipe", "pipe"],
                env: { SystemRoot: process.env.SystemRoot ?? "C:\\Windows", LOCALAPPDATA: process.env.LOCALAPPDATA ?? "" },
            });
        } catch (error) {
            reject(new NamedPipeConnectionError(
                "host_start_failed",
                "could not start the signed OfferAgent Host",
                false,
            ));
            return;
        }
        const discovery = child.stdio[3];
        const vaultInput = child.stdio[4];
        if (!(discovery instanceof Readable) || !(vaultInput instanceof Writable)) {
            child.kill();
            reject(new NamedPipeAuthenticationError("OfferAgent Host did not open its private discovery handle"));
            return;
        }
        vaultInput.end(Buffer.from(vaultRoot, "utf8"));
        const chunks: Buffer[] = [];
        let size = 0;
        let settled = false;
        const stop = (error: Error) => {
            if (settled) return;
            settled = true;
            cleanup();
            child.kill();
            reject(error);
        };
        const onAbort = () => stop(abortError());
        const timer = setTimeout(() => stop(new NamedPipeConnectionError(
            "host_discovery_timeout",
            "OfferAgent Host discovery timed out",
            true,
        )), timeoutMs);
        const cleanup = () => {
            clearTimeout(timer);
            signal?.removeEventListener("abort", onAbort);
        };
        signal?.addEventListener("abort", onAbort, { once: true });
        discovery.on("data", (chunk: Buffer) => {
            size += chunk.length;
            if (size > MAX_DISCOVERY_BYTES) {
                stop(new NamedPipeAuthenticationError("OfferAgent Host discovery exceeded its limit"));
                return;
            }
            chunks.push(Buffer.from(chunk));
        });
        child.stdio[2]?.resume();
        child.once("error", () => stop(new NamedPipeConnectionError(
            "host_start_failed",
            "OfferAgent Host failed to start",
            false,
        )));
        child.once("exit", (code: number | null) => {
            if (!settled && code !== 0) stop(new NamedPipeConnectionError(
                "host_attach_rejected",
                "OfferAgent Host rejected attach",
                true,
            ));
        });
        discovery.once("error", () => stop(new NamedPipeConnectionError(
            "host_discovery_failed",
            "OfferAgent Host discovery handle failed",
            true,
        )));
        discovery.once("end", () => {
            if (settled) return;
            settled = true;
            cleanup();
            if (size === 0) {
                reject(new NamedPipeAuthenticationError("OfferAgent Host returned no discovery material"));
                return;
            }
            child.unref();
            resolve(Buffer.concat(chunks, size));
        });
    });
}

function requireFresh(material: DiscoveryMaterial, now: Date): void {
    if (!PIPE_PATTERN.test(material.pipeName) || material.bootstrapNonce.length !== 32 ||
        !Number.isFinite(material.issuedAt.getTime()) || !Number.isFinite(material.expiresAt.getTime())) {
        throw new NamedPipeAuthenticationError("invalid discovery material");
    }
    if (now.getTime() < material.issuedAt.getTime() - 5_000 || now.getTime() >= material.expiresAt.getTime()) {
        throw new NamedPipeAuthenticationError("Named Pipe discovery material is expired");
    }
}

function decodeCanonicalNonce(value: string): Buffer {
    if (!BASE64URL_PATTERN.test(value)) throw new NamedPipeAuthenticationError("invalid nonce encoding");
    const decoded = Buffer.from(value.replace(/-/g, "+").replace(/_/g, "/") + "=".repeat((4 - value.length % 4) % 4), "base64");
    if (decoded.length !== 32 || base64url(decoded) !== value) {
        throw new NamedPipeAuthenticationError("nonce must contain exactly 256 canonical bits");
    }
    return decoded;
}

function base64url(value: Buffer): string {
    return value.toString("base64").replace(/=/g, "").replace(/\+/g, "-").replace(/\//g, "_");
}

function parseTimestamp(value: string): Date {
    if (!/(?:Z|[+-]\d{2}:\d{2})$/.test(value)) throw new NamedPipeAuthenticationError("timestamp lacks a timezone");
    const parsed = new Date(value);
    if (!Number.isFinite(parsed.getTime())) throw new NamedPipeAuthenticationError("invalid timestamp");
    return parsed;
}

function requireExactKeys(value: JsonObject, expected: string[]): void {
    const actual = Object.keys(value);
    if (actual.length !== expected.length || expected.some((key) => !actual.includes(key))) {
        throw new NamedPipeAuthenticationError("message contains unexpected fields");
    }
}

function lengthPrefix(value: Buffer): Buffer {
    const length = Buffer.allocUnsafe(4);
    length.writeUInt32BE(value.length, 0);
    return length;
}

function positiveInteger(value: number, name: string): number {
    if (!Number.isSafeInteger(value) || value < 1) throw new RangeError(`${name} must be a positive integer`);
    return value;
}

function abortError(): Error {
    const error = new Error("OfferAgent connection cancelled");
    error.name = "AbortError";
    return error;
}
