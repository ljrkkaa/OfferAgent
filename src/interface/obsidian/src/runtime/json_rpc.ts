import { EventEmitter } from "node:events";
import { Buffer } from "node:buffer";
import { randomBytes } from "node:crypto";

import { DEFAULT_MAX_FRAME_BYTES, encodeFrame, FrameDecoder, ProtocolFrameError } from "./framing";
import {
    PROTOCOL_ERROR_CODES,
    PROTOCOL_EVENT_NOTIFICATION_METHOD,
    PROTOCOL_JSON_RPC_ERROR_CODES,
    PROTOCOL_RPC_CANCEL_METHOD,
} from "./generated_protocol";
import type { ErrorCode, JsonRpcErrorCode } from "./generated_protocol";

export type RpcId = string | number;
export type JsonScalar = string | number | boolean | null;
export type JsonValue = JsonScalar | JsonValue[] | { [key: string]: JsonValue };
export type JsonObject = { [key: string]: JsonValue };

export interface ByteChannel {
    write(data: Uint8Array): Promise<void>;
    close(): Promise<void>;
    onData(listener: (chunk: Uint8Array) => void): () => void;
    onClose(listener: (error?: Error) => void): () => void;
}

export interface RpcRequestContext {
    readonly signal: AbortSignal;
}

export type RpcRequestHandler = (params: JsonObject, context: RpcRequestContext) => Promise<JsonValue>;

export interface JsonRpcPeerOptions {
    maxFrameBytes?: number;
    maxInflight?: number;
    requestTimeoutMs?: number;
    writeTimeoutMs?: number;
    maxQueuedWrites?: number;
    maxAbandonedRequestIds?: number;
}

export interface RpcErrorData {
    readonly code: ErrorCode;
    readonly retryable: boolean;
    readonly cancelled: boolean;
    readonly userVisibleMessage: string;
    readonly details?: JsonObject;
    readonly retryAfterMs?: number | null;
    readonly traceId?: string | null;
}

export class RemoteRpcError extends Error {
    readonly rpcCode: JsonRpcErrorCode;
    readonly envelope: RpcErrorData;

    constructor(rpcCode: number, envelope: RpcErrorData) {
        const checkedCode = requireJsonRpcErrorCode(rpcCode);
        const checkedEnvelope = requireRpcErrorData(envelope);
        super(checkedEnvelope.userVisibleMessage);
        this.name = "RemoteRpcError";
        this.rpcCode = checkedCode;
        this.envelope = checkedEnvelope;
    }
}

export class RpcRequestTimeoutError extends Error {
    constructor() {
        super("OfferAgent Worker 请求超时。");
        this.name = "RpcRequestTimeoutError";
    }
}

export class RpcRequestCancelledError extends Error {
    constructor() {
        super("OfferAgent request cancelled");
        this.name = "AbortError";
    }
}

export function isRpcRequestCancellation(error: unknown): boolean {
    return error instanceof RpcRequestCancelledError ||
        (error instanceof RemoteRpcError &&
            (error.envelope.cancelled || error.envelope.code === "request.cancelled"));
}

export class RpcDisconnectedError extends Error {
    constructor(message = "OfferAgent Worker connection closed") {
        super(message);
        this.name = "RpcDisconnectedError";
    }
}

interface PendingRequest {
    readonly resolve: (value: JsonValue) => void;
    readonly reject: (error: Error) => void;
    readonly timer: ReturnType<typeof setTimeout>;
    readonly abortCleanup: () => void;
}

interface ActiveInbound {
    readonly controller: AbortController;
}

type PeerState = "open" | "closing" | "closed" | "poisoned";

export class JsonRpcPeer {
    private readonly channel: ByteChannel;
    private readonly decoder: FrameDecoder;
    private readonly maxInflight: number;
    private readonly requestTimeoutMs: number;
    private readonly writeTimeoutMs: number;
    private readonly maxQueuedWrites: number;
    private readonly maxAbandonedRequestIds: number;
    private readonly handlers = new Map<string, RpcRequestHandler>();
    private readonly pending = new Map<RpcId, PendingRequest>();
    private readonly abandoned = new Map<RpcId, true>();
    private readonly inbound = new Map<RpcId, ActiveInbound>();
    private readonly events = new EventEmitter();
    private readonly writeCancellation = new AbortController();
    private readonly disposeData: () => void;
    private readonly disposeClose: () => void;
    private nextId = 0;
    private writeTail: Promise<void> = Promise.resolve();
    private queuedWrites = 0;
    private state: PeerState = "open";

    constructor(channel: ByteChannel, options: JsonRpcPeerOptions = {}) {
        this.channel = channel;
        this.decoder = new FrameDecoder(options.maxFrameBytes ?? DEFAULT_MAX_FRAME_BYTES);
        this.maxInflight = positiveInteger(options.maxInflight ?? 128, "maxInflight");
        this.requestTimeoutMs = positiveInteger(options.requestTimeoutMs ?? 120_000, "requestTimeoutMs");
        this.writeTimeoutMs = positiveInteger(options.writeTimeoutMs ?? 30_000, "writeTimeoutMs");
        this.maxQueuedWrites = positiveInteger(options.maxQueuedWrites ?? 128, "maxQueuedWrites");
        this.maxAbandonedRequestIds = positiveInteger(
            options.maxAbandonedRequestIds ?? 256,
            "maxAbandonedRequestIds",
        );
        this.disposeData = channel.onData((chunk) => this.acceptChunk(chunk));
        this.disposeClose = channel.onClose((error) => this.finish(error ?? new RpcDisconnectedError()));
    }

    get closed(): boolean {
        return this.state === "closed" || this.state === "poisoned";
    }

    register(method: string, handler: RpcRequestHandler): () => void {
        requireMethod(method);
        if (this.handlers.has(method)) throw new Error(`JSON-RPC handler already registered: ${method}`);
        this.handlers.set(method, handler);
        return () => {
            if (this.handlers.get(method) === handler) this.handlers.delete(method);
        };
    }

    onNotification(method: string, listener: (params: JsonObject) => void): () => void {
        if (method !== PROTOCOL_EVENT_NOTIFICATION_METHOD) throw new TypeError("unsupported notification method");
        const event = `notification:${method}`;
        this.events.on(event, listener);
        return () => this.events.off(event, listener);
    }

    async request(
        method: string,
        params: JsonObject = {},
        options: { signal?: AbortSignal; timeoutMs?: number } = {},
    ): Promise<JsonValue> {
        this.requireOpen();
        requireMethod(method);
        requireJsonObject(params);
        if (this.pending.size >= this.maxInflight) throw new Error("JSON-RPC in-flight request limit reached");
        if (options.signal?.aborted) throw abortError();
        const id = `obsidian_${++this.nextId}_${randomBytes(8).toString("hex")}`;
        const timeoutMs = positiveInteger(options.timeoutMs ?? this.requestTimeoutMs, "timeoutMs");
        let frameWritten = false;
        let settle!: PendingRequest;
        const result = new Promise<JsonValue>((resolve, reject) => {
            const timer = setTimeout(() => {
                if (!this.pending.delete(id)) return;
                settle.abortCleanup();
                this.abandon(id);
                if (frameWritten) {
                    void this.notify(PROTOCOL_RPC_CANCEL_METHOD, { requestId: id }).catch(() => undefined);
                } else {
                    this.poison(new RpcDisconnectedError("OfferAgent request write did not finish before its deadline"));
                }
                reject(new RpcRequestTimeoutError());
            }, timeoutMs);
            const abort = () => {
                if (!this.pending.delete(id)) return;
                clearTimeout(timer);
                settle.abortCleanup();
                this.abandon(id);
                if (frameWritten) {
                    void this.notify(PROTOCOL_RPC_CANCEL_METHOD, { requestId: id }).catch(() => undefined);
                } else {
                    this.poison(new RpcDisconnectedError("OfferAgent request write did not finish before cancellation"));
                }
                reject(abortError());
            };
            if (options.signal) options.signal.addEventListener("abort", abort, { once: true });
            settle = {
                resolve,
                reject,
                timer,
                abortCleanup: () => options.signal?.removeEventListener("abort", abort),
            };
            this.pending.set(id, settle);
        });
        let write: Promise<void>;
        try {
            write = this.send({ jsonrpc: "2.0", id, method, params });
        } catch (error) {
            this.rejectPending(id, asError(error));
            return await result;
        }
        void write.then(
            () => { frameWritten = true; },
            (error) => this.rejectPending(id, asError(error)),
        );
        // Attach to the terminal result immediately.  In particular, the request
        // deadline must be observable even while the underlying write is stalled;
        // leaving this Promise unattached would also create an unhandled rejection.
        return await result;
    }

    async notify(method: string, params: JsonObject = {}): Promise<void> {
        this.requireOpen();
        requireNotificationMethod(method);
        requireJsonObject(params);
        await this.send({ jsonrpc: "2.0", method, params });
    }

    async close(): Promise<void> {
        if (this.closed || this.state === "closing") return;
        this.state = "closing";
        this.finish(new RpcDisconnectedError());
        // Closing is the escape hatch for a blocked transport write.  Never wait
        // for writeTail before destroying the channel that can unblock it.
        await settleWithin(
            Promise.resolve().then(() => this.channel.close()),
            this.writeTimeoutMs,
        );
    }

    private acceptChunk(chunk: Uint8Array): void {
        if (this.closed) return;
        let values: unknown[];
        try {
            values = this.decoder.feed(chunk);
        } catch (error) {
            this.poison(asError(error));
            return;
        }
        for (const value of values) {
            try {
                this.acceptMessage(value);
            } catch (error) {
                this.poison(asError(error));
                return;
            }
        }
    }

    private acceptMessage(value: unknown): void {
        const message = requireJsonObject(value);
        const keys = Object.keys(message);
        if (message.jsonrpc !== "2.0") throw new ProtocolFrameError("invalid JSON-RPC version");
        if (typeof message.method === "string") {
            requireMethod(message.method);
            if (!("params" in message) || !isJsonObject(message.params)) {
                throw new ProtocolFrameError("JSON-RPC request/notification params must be an object");
            }
            const allowed = "id" in message ? ["jsonrpc", "id", "method", "params"] : ["jsonrpc", "method", "params"];
            requireExactKeys(keys, allowed);
            if ("id" in message) {
                const id = requireRpcId(message.id);
                if (this.inbound.size >= this.maxInflight) throw new ProtocolFrameError("inbound request limit reached");
                this.acceptRequest(id, message.method, message.params);
            } else {
                this.acceptNotification(message.method, message.params);
            }
            return;
        }
        if (!("id" in message)) throw new ProtocolFrameError("JSON-RPC response has no id");
        const id = requireRpcId(message.id);
        if (("result" in message) === ("error" in message)) {
            throw new ProtocolFrameError("JSON-RPC response requires exactly one of result or error");
        }
        let result: JsonValue | undefined;
        let remoteError: RemoteRpcError | undefined;
        if ("result" in message) {
            requireExactKeys(keys, ["jsonrpc", "id", "result"]);
            result = requireJsonValue(message.result);
        } else {
            requireExactKeys(keys, ["jsonrpc", "id", "error"]);
            const error = requireJsonObject(message.error);
            requireExactKeys(Object.keys(error), ["code", "message", "data"]);
            const rpcCode = requireJsonRpcErrorCode(error.code);
            if (typeof error.message !== "string" || error.message.length < 1 || error.message.length > 4096) {
                throw new ProtocolFrameError("invalid JSON-RPC error response message");
            }
            remoteError = new RemoteRpcError(rpcCode, requireRpcErrorData(error.data));
        }
        if (this.abandoned.delete(id)) return;
        const pending = this.pending.get(id);
        if (!pending) throw new ProtocolFrameError("JSON-RPC response id is unknown or already terminal");
        this.pending.delete(id);
        clearTimeout(pending.timer);
        pending.abortCleanup();
        if (remoteError) {
            pending.reject(remoteError);
        } else {
            pending.resolve(result as JsonValue);
        }
    }

    private acceptRequest(id: RpcId, method: string, params: JsonObject): void {
        if (this.inbound.has(id)) throw new ProtocolFrameError("duplicate inbound JSON-RPC request id");
        const controller = new AbortController();
        this.inbound.set(id, { controller });
        const handler = this.handlers.get(method);
        const operation: Promise<JsonValue> = handler
            ? Promise.resolve().then(() => handler(params, { signal: controller.signal })).then(requireJsonValue)
            : Promise.reject(new RemoteRpcError(-32601, {
                code: "protocol.method_not_found",
                retryable: false,
                cancelled: false,
                userVisibleMessage: "客户端不支持该 Runtime 请求。",
            }));
        void operation.then(
            (result) => this.send({ jsonrpc: "2.0", id, result }),
            (error) => {
                const normalized = controller.signal.aborted
                    ? new RemoteRpcError(-32000, {
                        code: "request.cancelled",
                        retryable: false,
                        cancelled: true,
                        userVisibleMessage: "客户端请求已取消。",
                    })
                    : error instanceof RemoteRpcError
                        ? error
                        : new RemoteRpcError(-32603, {
                            code: "protocol.internal_error",
                            retryable: false,
                            cancelled: false,
                            userVisibleMessage: "客户端请求处理失败。",
                        });
                const response: JsonObject = {
                    jsonrpc: "2.0",
                    id,
                    error: {
                        code: normalized.rpcCode,
                        message: jsonRpcErrorMessage(normalized.rpcCode),
                        data: rpcErrorDataToJsonObject(normalized.envelope),
                    },
                };
                return this.send(response);
            },
        ).catch((error) => this.poison(asError(error))).finally(() => this.inbound.delete(id));
    }

    private acceptNotification(method: string, params: JsonObject): void {
        if (method === PROTOCOL_RPC_CANCEL_METHOD) {
            requireExactKeys(Object.keys(params), ["requestId"]);
            const id = requireRpcId(params.requestId);
            this.inbound.get(id)?.controller.abort();
            return;
        }
        if (method !== PROTOCOL_EVENT_NOTIFICATION_METHOD) {
            throw new ProtocolFrameError("unsupported JSON-RPC notification method");
        }
        this.events.emit(`notification:${method}`, params);
    }

    private abandon(id: RpcId): void {
        this.abandoned.delete(id);
        while (this.abandoned.size >= this.maxAbandonedRequestIds) {
            const oldest = this.abandoned.keys().next().value as RpcId | undefined;
            if (oldest === undefined) break;
            this.abandoned.delete(oldest);
        }
        this.abandoned.set(id, true);
    }

    private rejectPending(id: RpcId, error: Error): void {
        const pending = this.pending.get(id);
        if (!pending) return;
        this.pending.delete(id);
        clearTimeout(pending.timer);
        pending.abortCleanup();
        pending.reject(error);
    }

    private send(message: JsonObject): Promise<void> {
        this.requireOpen();
        if (this.queuedWrites >= this.maxQueuedWrites) return Promise.reject(new Error("JSON-RPC write queue is full"));
        this.queuedWrites += 1;
        let frame: Buffer;
        try {
            frame = encodeFrame(message);
        } catch (error) {
            this.queuedWrites -= 1;
            throw error;
        }
        const rawWrite = this.writeTail.then(() => this.channel.write(frame));
        const write = rejectAfter(
            rawWrite,
            this.writeTimeoutMs,
            () => new RpcDisconnectedError("OfferAgent Named Pipe write deadline exceeded"),
            this.writeCancellation.signal,
            () => new RpcDisconnectedError(),
        );
        this.writeTail = write.catch((error) => {
            this.poison(asError(error));
        }).finally(() => {
            this.queuedWrites -= 1;
        });
        return write;
    }

    private requireOpen(): void {
        if (this.state !== "open") throw new RpcDisconnectedError();
    }

    private poison(error: Error): void {
        if (this.closed) return;
        this.state = "poisoned";
        void Promise.resolve().then(() => this.channel.close()).catch(() => undefined);
        this.finish(error, "poisoned");
    }

    private finish(error: Error, terminal: PeerState = "closed"): void {
        if (this.state === "closed" || (this.state === "poisoned" && terminal !== "poisoned")) return;
        this.state = terminal;
        this.writeCancellation.abort();
        this.disposeData();
        this.disposeClose();
        for (const item of this.pending.values()) {
            clearTimeout(item.timer);
            item.abortCleanup();
            item.reject(error);
        }
        this.pending.clear();
        this.abandoned.clear();
        for (const item of this.inbound.values()) item.controller.abort();
        this.inbound.clear();
        this.events.removeAllListeners();
    }
}

function positiveInteger(value: number, name: string): number {
    if (!Number.isSafeInteger(value) || value < 1) throw new RangeError(`${name} must be a positive integer`);
    return value;
}

function requireMethod(value: string): void {
    if (value.length > 256 || !/^[A-Za-z][A-Za-z0-9_.-]*(?:\/[A-Za-z][A-Za-z0-9_.-]*)*$/.test(value)) {
        throw new TypeError("invalid JSON-RPC method");
    }
}

function requireNotificationMethod(value: string): void {
    requireMethod(value);
    if (value !== PROTOCOL_EVENT_NOTIFICATION_METHOD && value !== PROTOCOL_RPC_CANCEL_METHOD) {
        throw new TypeError("unsupported notification method");
    }
}

function requireRpcId(value: unknown): RpcId {
    if ((typeof value === "string" && value.length > 0 && value.length <= 128) ||
        (typeof value === "number" && Number.isSafeInteger(value))) {
        return value;
    }
    throw new ProtocolFrameError("invalid JSON-RPC request id");
}

function requireJsonValue(value: unknown, depth = 0): JsonValue {
    if (depth > 64) throw new ProtocolFrameError("JSON value exceeds maximum nesting depth");
    if (value === null || typeof value === "string" || typeof value === "boolean") return value;
    if (typeof value === "number" && Number.isFinite(value)) return value;
    if (Array.isArray(value)) {
        if (value.length > 100_000) throw new ProtocolFrameError("JSON array exceeds item limit");
        return value.map((item) => requireJsonValue(item, depth + 1));
    }
    if (isJsonObject(value)) {
        const keys = Object.keys(value);
        if (keys.length > 100_000) throw new ProtocolFrameError("JSON object exceeds key limit");
        const copy: JsonObject = {};
        for (const key of keys) {
            if (key === "__proto__" || key === "prototype" || key === "constructor") {
                throw new ProtocolFrameError("unsafe JSON object key");
            }
            copy[key] = requireJsonValue(value[key], depth + 1);
        }
        return copy;
    }
    throw new ProtocolFrameError("value is not JSON-compatible");
}

function requireJsonObject(value: unknown): JsonObject {
    const checked = requireJsonValue(value);
    if (checked === null || Array.isArray(checked) || typeof checked !== "object") {
        throw new ProtocolFrameError("expected a JSON object");
    }
    return checked;
}

function isJsonObject(value: unknown): value is { [key: string]: unknown } {
    return value !== null && typeof value === "object" && !Array.isArray(value);
}

function requireExactKeys(actual: string[], expected: string[]): void {
    if (actual.length !== expected.length || expected.some((key) => !actual.includes(key))) {
        throw new ProtocolFrameError("JSON-RPC envelope has unexpected fields");
    }
}

function abortError(): Error {
    return new RpcRequestCancelledError();
}

function asError(value: unknown): Error {
    return value instanceof Error ? value : new Error("OfferAgent transport failed");
}

const ERROR_CODE_SET: ReadonlySet<string> = new Set(PROTOCOL_ERROR_CODES);
const JSON_RPC_ERROR_CODE_SET: ReadonlySet<number> = new Set(PROTOCOL_JSON_RPC_ERROR_CODES);

function requireJsonRpcErrorCode(value: unknown): JsonRpcErrorCode {
    if (typeof value !== "number" || !Number.isSafeInteger(value) || !JSON_RPC_ERROR_CODE_SET.has(value)) {
        throw new ProtocolFrameError("invalid JSON-RPC error code");
    }
    return value as JsonRpcErrorCode;
}

function requireRpcErrorData(value: unknown): RpcErrorData {
    const raw = requireJsonObject(value);
    const expected = ["cancelled", "code", "retryable", "userVisibleMessage"];
    for (const optional of ["details", "retryAfterMs", "traceId"]) {
        if (optional in raw) expected.push(optional);
    }
    requireExactKeys(Object.keys(raw), expected);
    if (typeof raw.code !== "string" || !ERROR_CODE_SET.has(raw.code)) {
        throw new ProtocolFrameError("invalid OfferAgent error code");
    }
    if (typeof raw.retryable !== "boolean" || typeof raw.cancelled !== "boolean") {
        throw new ProtocolFrameError("invalid OfferAgent error flags");
    }
    if (typeof raw.userVisibleMessage !== "string" || raw.userVisibleMessage.length < 1 ||
        raw.userVisibleMessage.length > 4096) {
        throw new ProtocolFrameError("invalid OfferAgent user-visible error message");
    }
    let details: JsonObject | undefined;
    if ("details" in raw) details = requireJsonObject(raw.details);
    let retryAfterMs: number | null | undefined;
    if ("retryAfterMs" in raw) {
        if (raw.retryAfterMs !== null && (typeof raw.retryAfterMs !== "number" ||
            !Number.isSafeInteger(raw.retryAfterMs) || raw.retryAfterMs < 0 || raw.retryAfterMs > 86_400_000)) {
            throw new ProtocolFrameError("invalid OfferAgent retry delay");
        }
        retryAfterMs = raw.retryAfterMs as number | null;
    }
    let traceId: string | null | undefined;
    if ("traceId" in raw) {
        if (raw.traceId !== null && (typeof raw.traceId !== "string" ||
            !/^trace_[A-Za-z0-9][A-Za-z0-9_-]{0,121}$/.test(raw.traceId))) {
            throw new ProtocolFrameError("invalid OfferAgent trace id");
        }
        traceId = raw.traceId as string | null;
    }
    return {
        code: raw.code as ErrorCode,
        retryable: raw.retryable,
        cancelled: raw.cancelled,
        userVisibleMessage: raw.userVisibleMessage,
        ...(details ? { details } : {}),
        ...(retryAfterMs !== undefined ? { retryAfterMs } : {}),
        ...(traceId !== undefined ? { traceId } : {}),
    };
}

function rpcErrorDataToJsonObject(value: RpcErrorData): JsonObject {
    const checked = requireRpcErrorData(value);
    return {
        cancelled: checked.cancelled,
        code: checked.code,
        retryable: checked.retryable,
        userVisibleMessage: checked.userVisibleMessage,
        ...(checked.details ? { details: checked.details } : {}),
        ...(checked.retryAfterMs !== undefined ? { retryAfterMs: checked.retryAfterMs } : {}),
        ...(checked.traceId !== undefined ? { traceId: checked.traceId } : {}),
    };
}

function jsonRpcErrorMessage(code: JsonRpcErrorCode): string {
    if (code === -32601) return "Method not found";
    if (code === -32603) return "Internal error";
    return "Request failed";
}

function rejectAfter<T>(
    promise: Promise<T>,
    timeoutMs: number,
    timeoutError: () => Error,
    signal: AbortSignal,
    cancellationError: () => Error,
): Promise<T> {
    return new Promise<T>((resolve, reject) => {
        let terminal = false;
        let timer: ReturnType<typeof setTimeout> | null = null;
        const cleanup = () => {
            if (timer) clearTimeout(timer);
            signal.removeEventListener("abort", onAbort);
        };
        const fail = (failure: unknown) => {
            if (terminal) return;
            terminal = true;
            cleanup();
            reject(failure);
        };
        const onAbort = () => fail(cancellationError());
        timer = setTimeout(() => fail(timeoutError()), timeoutMs);
        signal.addEventListener("abort", onAbort, { once: true });
        void promise.then(
            (value) => {
                if (terminal) return;
                terminal = true;
                cleanup();
                resolve(value);
            },
            fail,
        );
        if (signal.aborted) onAbort();
    });
}

function settleWithin(promise: Promise<unknown>, timeoutMs: number): Promise<void> {
    return new Promise((resolve) => {
        let terminal = false;
        const finish = () => {
            if (terminal) return;
            terminal = true;
            clearTimeout(timer);
            resolve();
        };
        const timer = setTimeout(finish, timeoutMs);
        void promise.then(finish, finish);
    });
}

export { requireJsonObject, requireJsonValue };
