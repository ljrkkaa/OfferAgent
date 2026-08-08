import { randomBytes } from "node:crypto";

import { EventEnvelope, EventReducer } from "./event_reducer";
import type {
    CapabilityName,
    ProtocolCommandMethod,
    ProtocolCommandParams,
    ProtocolCommandResult,
} from "./generated_protocol";
import {
    JsonObject,
    JsonRpcPeer,
    JsonValue,
    RemoteRpcError,
    RpcDisconnectedError,
    RpcRequestTimeoutError,
    requireJsonObject,
} from "./json_rpc";
import { RpcTransport } from "./stdio_worker";

export const CLIENT_CAPABILITIES: Readonly<Record<CapabilityName, boolean>> = {
    eventReplay: true,
    multiSession: true,
    approvals: true,
    skills: true,
    shell: true,
    hooks: true,
    subagents: true,
    artifacts: true,
    contentBlocks: true,
    documentIngestion: true,
    cancellation: true,
    diagnostics: true,
};

/** Structural protocol support required from this complete local Runtime build.
 * User configuration may disable execution without changing handshake support.
 */
export const REQUIRED_RUNTIME_CAPABILITIES: readonly CapabilityName[] = Object.freeze([
    "eventReplay",
    "multiSession",
    "approvals",
    "skills",
    "shell",
    "hooks",
    "subagents",
    "artifacts",
    "contentBlocks",
    "documentIngestion",
    "cancellation",
    "diagnostics",
]);
const CAPABILITY_NAMES = Object.freeze(Object.keys(CLIENT_CAPABILITIES) as CapabilityName[]);
const CAPABILITY_NAME_SET: ReadonlySet<string> = new Set(CAPABILITY_NAMES);

export interface ProtocolIdentity {
    readonly protocolVersion: string;
    readonly minimumProtocolVersion: string;
    readonly maximumProtocolVersion: string;
    readonly schemaHash: string;
    readonly clientVersion: string;
}

export interface HarnessClientContext {
    readonly workspaceId: string;
    readonly identity: ProtocolIdentity;
    readonly requiredCapabilities: readonly CapabilityName[];
}

export interface InitializeResult {
    readonly protocolVersion: string;
    readonly runtimeVersion: string;
    readonly coreVersion: string;
    readonly schemaHash: string;
    readonly workspaceId: string;
    readonly workspaceInstanceId: string;
    readonly parentPid: number;
    readonly workerPid: number;
    readonly transport: "stdio";
    readonly runtimeArch: "win-x64";
    readonly capabilities: JsonObject;
    readonly buildCommit: string;
}

export interface ProtocolRequestClient {
    request<Method extends ProtocolCommandMethod>(
        method: Method,
        params: ProtocolCommandParams<Method>,
        options?: { signal?: AbortSignal; timeoutMs?: number },
    ): Promise<ProtocolCommandResult<Method>>;
}

export interface HarnessClientOptions {
    pingIntervalMs?: number;
    pingDeadlineMs?: number;
    onDisconnected?: (error: Error) => void;
}

type ClientState = "disconnected" | "connecting" | "ready" | "closing" | "closed";

export class HarnessCompatibilityError extends Error {
    constructor(message: string) {
        super(message);
        this.name = "HarnessCompatibilityError";
    }
}

export class HarnessClient {
    readonly reducer: EventReducer;
    private readonly transport: RpcTransport;
    private readonly context: HarnessClientContext;
    private readonly pingIntervalMs: number;
    private readonly pingDeadlineMs: number;
    private readonly onDisconnected: ((error: Error) => void) | undefined;
    private peer: JsonRpcPeer | null = null;
    private initializeResult: InitializeResult | null = null;
    private state: ClientState = "disconnected";
    private pingTimer: ReturnType<typeof setInterval> | null = null;
    private pingActive = false;
    private closeOperation: Promise<void> | null = null;
    private retiringPeer: JsonRpcPeer | null = null;

    constructor(
        transport: RpcTransport,
        context: HarnessClientContext,
        options: HarnessClientOptions = {},
    ) {
        requireIdentifier(context.workspaceId, "workspaceId");
        requireIdentity(context.identity);
        if (new Set(context.requiredCapabilities).size !== context.requiredCapabilities.length) {
            throw new TypeError("required capabilities must be unique");
        }
        this.transport = transport;
        this.context = context;
        this.pingIntervalMs = positiveInteger(options.pingIntervalMs ?? 15_000, "pingIntervalMs");
        this.pingDeadlineMs = positiveInteger(options.pingDeadlineMs ?? 45_000, "pingDeadlineMs");
        this.onDisconnected = options.onDisconnected;
        this.reducer = new EventReducer(context.workspaceId);
    }

    get ready(): boolean {
        return this.state === "ready";
    }

    get identity(): InitializeResult {
        if (!this.initializeResult) throw new Error("Harness is not initialized");
        return this.initializeResult;
    }

    async connect(signal?: AbortSignal): Promise<InitializeResult> {
        if (this.state !== "disconnected") throw new Error(`cannot connect Harness client from ${this.state}`);
        this.state = "connecting";
        try {
            const peer = await this.transport.connect(signal);
            this.peer = peer;
            peer.onNotification("event", (params) => this.reducer.accept(params));
            const raw = await this.requestOnPeer(peer, "initialize", {
                protocolVersion: this.context.identity.protocolVersion,
                clientVersion: this.context.identity.clientVersion,
                workspaceId: this.context.workspaceId,
                capabilities: CLIENT_CAPABILITIES,
                supportedProtocolRange: {
                    minimum: this.context.identity.minimumProtocolVersion,
                    maximum: this.context.identity.maximumProtocolVersion,
                },
                requiredCapabilities: [...this.context.requiredCapabilities],
                schemaHash: this.context.identity.schemaHash,
            }, { signal, timeoutMs: this.pingDeadlineMs });
            const result = validateInitializeResult(raw, this.context);
            this.initializeResult = result;
            this.state = "ready";
            this.startPing();
            return result;
        } catch (error) {
            this.state = "disconnected";
            const peer = this.peer;
            this.peer = null;
            if (peer) await peer.close().catch(() => undefined);
            throw error;
        }
    }

    async request<Method extends ProtocolCommandMethod>(
        method: Method,
        params: ProtocolCommandParams<Method>,
        options?: { signal?: AbortSignal; timeoutMs?: number },
    ): Promise<ProtocolCommandResult<Method>> {
        return this.requestOnPeer(this.requirePeer(), method, params, options);
    }

    async replay(
        scope: { sessionId?: string; runId?: string },
        afterSequence: number,
        options: { signal?: AbortSignal; limit?: number } = {},
    ): Promise<number> {
        if (!scope.runId || scope.sessionId) throw new TypeError("single-cursor replay requires exactly one Run scope");
        if (!Number.isSafeInteger(afterSequence) || afterSequence < 0) throw new RangeError("invalid replay sequence");
        let cursor = afterSequence;
        const limit = positiveInteger(options.limit ?? 1_000, "replayLimit");
        do {
            const raw = await this.request("events/replay", {
                sessionId: null,
                runId: scope.runId,
                afterSequence: cursor,
                runCursors: {},
                limit,
                types: [],
            }, { signal: options.signal });
            const result = requireJsonObject(raw);
            const events = arrayField(result, "events");
            for (const event of events) this.reducer.accept(event);
            const last = integerField(result, "lastSequence", 0);
            if (last < cursor || (events.length > 0 && last === cursor)) {
                throw new Error("Worker returned a non-advancing event replay cursor");
            }
            cursor = last;
            if (result.hasMore !== true) break;
            if (events.length === 0) throw new Error("Worker returned hasMore without replay events");
        } while (true);
        return cursor;
    }

    async replaySession(
        sessionId: string,
        runCursors: Readonly<Record<string, number>> = {},
        options: { signal?: AbortSignal; limit?: number } = {},
    ): Promise<Readonly<Record<string, number>>> {
        requireIdentifier(sessionId, "sessionId");
        let cursors = validateRunCursors(runCursors);
        const limit = positiveInteger(options.limit ?? 1_000, "replayLimit");
        do {
            const raw = await this.request("events/replay", {
                sessionId,
                runId: null,
                afterSequence: 0,
                runCursors: cursors,
                limit,
                types: [],
            }, { signal: options.signal });
            const result = requireJsonObject(raw);
            if (result.lastSequence !== null) throw new Error("Session replay returned an aggregate sequence");
            const events = arrayField(result, "events");
            for (const event of events) this.reducer.accept(event);
            const next = validateRunCursors(objectField(result, "runCursors"));
            for (const [runId, sequence] of Object.entries(cursors)) {
                if (!(runId in next) || next[runId] < sequence) throw new Error("Session replay Run cursor regressed");
            }
            cursors = next;
            if (result.hasMore !== true) break;
            if (events.length === 0) throw new Error("Worker returned hasMore without Session replay events");
        } while (true);
        return cursors;
    }

    close(options: {
        shutdown?: boolean;
        reason?: ProtocolCommandParams<"shutdown">["reason"];
        gracePeriodMs?: number;
    } = {}): Promise<void> {
        if (this.closeOperation === null) {
            // closeOnce runs synchronously through its first await.  In the
            // non-RPC unload path this reaches peer.close/stdio EOF before the
            // Obsidian onunload callback returns.
            this.closeOperation = this.closeOnce(options);
        }
        return this.closeOperation;
    }

    /** Escalate an in-flight graceful close to immediate transport teardown. */
    beginImmediateClose(): Promise<void> {
        if (this.closeOperation === null) return this.close({ shutdown: false });
        const peer = this.retiringPeer ?? this.peer;
        if (peer) void peer.close().catch(() => undefined);
        return this.closeOperation;
    }

    private async closeOnce(options: {
        shutdown?: boolean;
        reason?: ProtocolCommandParams<"shutdown">["reason"];
        gracePeriodMs?: number;
    }): Promise<void> {
        if (this.state === "closed") return;
        this.state = "closing";
        this.stopPing();
        const peer = this.peer;
        this.peer = null;
        this.retiringPeer = peer;
        if (peer && options.shutdown) {
            await this.requestOnPeer(peer, "shutdown", {
                reason: options.reason ?? "plugin_disabled",
                gracePeriodMs: options.gracePeriodMs ?? 30_000,
            }, { timeoutMs: (options.gracePeriodMs ?? 30_000) + 5_000 }).catch(() => undefined);
        }
        await peer?.close().catch(() => undefined);
        if (this.retiringPeer === peer) this.retiringPeer = null;
        this.state = "closed";
    }

    private startPing(): void {
        this.stopPing();
        this.pingTimer = setInterval(() => void this.ping(), this.pingIntervalMs);
    }

    private stopPing(): void {
        if (this.pingTimer) clearInterval(this.pingTimer);
        this.pingTimer = null;
        this.pingActive = false;
    }

    private async ping(): Promise<void> {
        if (!this.ready || this.pingActive) return;
        this.pingActive = true;
        const nonce = `req_${randomBytes(16).toString("hex")}`;
        try {
            const result = requireJsonObject(await this.requestOnPeer(this.requirePeer(),
                "runtime/ping",
                { nonce },
                { timeoutMs: this.pingDeadlineMs },
            ));
            if (result.nonce !== nonce || integerField(result, "workerPid", 1) !== this.identity.workerPid) {
                throw new HarnessCompatibilityError("Worker ping identity changed");
            }
            timestampField(result, "timestamp");
        } catch (error) {
            // A ping failure owns disconnect notification only when it wins the
            // race to close this client. Register the peer retirement in the
            // same single-flight gate used by explicit stop/unload before
            // awaiting its process join; those callers must never observe an
            // empty/complete close while this peer is still retiring.
            const notifyDisconnect = this.closeOperation === null && this.state === "ready";
            await this.close({ shutdown: false }).catch(() => undefined);
            if (notifyDisconnect) this.onDisconnected?.(safeConnectionError(error));
        } finally {
            this.pingActive = false;
        }
    }

    private async requestOnPeer<Method extends ProtocolCommandMethod>(
        peer: JsonRpcPeer,
        method: Method,
        params: ProtocolCommandParams<Method>,
        options: { signal?: AbortSignal; timeoutMs?: number } = {},
    ): Promise<ProtocolCommandResult<Method>> {
        const result = await peer.request(method, params as unknown as JsonObject, options);
        return result as unknown as ProtocolCommandResult<Method>;
    }

    private requirePeer(): JsonRpcPeer {
        if (!this.ready || !this.peer) throw new Error("Harness client is not ready");
        return this.peer;
    }
}

function validateInitializeResult(raw: JsonValue, context: HarnessClientContext): InitializeResult {
    try {
        return validateInitializeResultShape(raw, context);
    } catch (error) {
        if (error instanceof HarnessCompatibilityError) throw error;
        throw new HarnessCompatibilityError("Runtime initialize result does not match the protocol schema");
    }
}

function validateInitializeResultShape(raw: JsonValue, context: HarnessClientContext): InitializeResult {
    const value = requireJsonObject(raw);
    const protocolVersion = textField(value, "protocolVersion");
    const schemaHash = textField(value, "schemaHash");
    if (protocolVersion !== context.identity.protocolVersion || schemaHash !== context.identity.schemaHash) {
        throw new HarnessCompatibilityError("Runtime protocol/schema is incompatible with this plugin");
    }
    const range = objectField(value, "supportedProtocolRange");
    const selected = protocolTuple(protocolVersion);
    if (compareProtocol(selected, protocolTuple(textField(range, "minimum"))) < 0 ||
        compareProtocol(selected, protocolTuple(textField(range, "maximum"))) > 0) {
        throw new HarnessCompatibilityError("Runtime selected a protocol outside its declared range");
    }
    if (textField(value, "workspaceId") !== context.workspaceId) {
        throw new HarnessCompatibilityError("Runtime attached a different Workspace");
    }
    if (value.transport !== "stdio") throw new HarnessCompatibilityError("local plugin requires direct stdio");
    const capabilities = validateCapabilitySet(value.capabilities);
    for (const required of context.requiredCapabilities) {
        if (capabilities[required] !== true) throw new HarnessCompatibilityError(`Runtime lacks required capability: ${required}`);
    }
    const runtimeArch = textField(value, "runtimeArch");
    if (runtimeArch !== "win-x64") {
        throw new HarnessCompatibilityError("Runtime architecture is unsupported");
    }
    const buildCommit = textField(value, "buildCommit");
    if (!/^[0-9a-f]{7,64}$/.test(buildCommit)) throw new HarnessCompatibilityError("Runtime build identity is invalid");
    return {
        protocolVersion,
        runtimeVersion: semanticVersionField(value, "runtimeVersion"),
        coreVersion: semanticVersionField(value, "coreVersion"),
        schemaHash,
        workspaceId: context.workspaceId,
        workspaceInstanceId: textField(value, "workspaceInstanceId"),
        parentPid: integerField(value, "parentPid", 1),
        workerPid: integerField(value, "workerPid", 1),
        transport: "stdio",
        runtimeArch,
        capabilities,
        buildCommit,
    };
}

function validateCapabilitySet(value: JsonValue | undefined): JsonObject {
    if (value === undefined) throw new HarnessCompatibilityError("capabilities must be an object");
    const raw = requireJsonObject(value);
    for (const key of Object.keys(raw)) {
        if (!CAPABILITY_NAME_SET.has(key)) throw new HarnessCompatibilityError(`unknown Runtime capability: ${key}`);
        if (typeof raw[key] !== "boolean") throw new HarnessCompatibilityError(`Runtime capability is not boolean: ${key}`);
    }
    const normalized: JsonObject = {};
    for (const name of CAPABILITY_NAMES) normalized[name] = raw[name] === true;
    return normalized;
}

function safeConnectionError(error: unknown): Error {
    if (error instanceof RemoteRpcError || error instanceof RpcDisconnectedError ||
        error instanceof RpcRequestTimeoutError || error instanceof HarnessCompatibilityError) {
        return error;
    }
    return new RpcDisconnectedError();
}

function requireIdentity(identity: ProtocolIdentity): void {
    protocolTuple(identity.protocolVersion);
    const minimum = protocolTuple(identity.minimumProtocolVersion);
    const maximum = protocolTuple(identity.maximumProtocolVersion);
    if (compareProtocol(minimum, maximum) > 0 || compareProtocol(protocolTuple(identity.protocolVersion), minimum) < 0 ||
        compareProtocol(protocolTuple(identity.protocolVersion), maximum) > 0) {
        throw new TypeError("client protocol version is outside its supported range");
    }
    if (!/^sha256:[0-9a-f]{64}$/.test(identity.schemaHash)) throw new TypeError("invalid protocol schema hash");
    if (!/^\d+\.\d+\.\d+(?:[-+][0-9A-Za-z.-]+)?$/.test(identity.clientVersion)) {
        throw new TypeError("invalid client semantic version");
    }
}

function protocolTuple(value: string): [number, number] {
    const match = /^(0|[1-9]\d*)\.(0|[1-9]\d*)$/.exec(value);
    if (!match) throw new HarnessCompatibilityError("invalid protocol version");
    return [Number(match[1]), Number(match[2])];
}

function compareProtocol(left: [number, number], right: [number, number]): number {
    return left[0] - right[0] || left[1] - right[1];
}

function semanticVersionField(value: JsonObject, key: string): string {
    const field = textField(value, key);
    if (!/^\d+\.\d+\.\d+(?:[-+][0-9A-Za-z.-]+)?$/.test(field)) throw new HarnessCompatibilityError(`invalid ${key}`);
    return field;
}

function textField(value: JsonObject, key: string): string {
    const field = value[key];
    if (typeof field !== "string" || field.length === 0) throw new HarnessCompatibilityError(`${key} must be text`);
    return field;
}

function timestampField(value: JsonObject, key: string): string {
    const field = textField(value, key);
    if (!Number.isFinite(Date.parse(field)) || !/(?:Z|[+-]\d{2}:\d{2})$/.test(field)) {
        throw new HarnessCompatibilityError(`${key} must be RFC3339`);
    }
    return field;
}

function integerField(value: JsonObject, key: string, minimum: number): number {
    const field = value[key];
    if (typeof field !== "number" || !Number.isSafeInteger(field) || field < minimum) {
        throw new HarnessCompatibilityError(`${key} must be an integer >= ${minimum}`);
    }
    return field;
}

function objectField(value: JsonObject, key: string): JsonObject {
    const field = value[key];
    if (field === null || typeof field !== "object" || Array.isArray(field)) {
        throw new HarnessCompatibilityError(`${key} must be an object`);
    }
    return field;
}

function arrayField(value: JsonObject, key: string): JsonValue[] {
    const field = value[key];
    if (!Array.isArray(field)) throw new HarnessCompatibilityError(`${key} must be an array`);
    return field;
}

function requireIdentifier(value: string, name: string): void {
    if (!value || value.length > 256 || /[\0\r\n]/.test(value)) throw new TypeError(`invalid ${name}`);
}

function validateRunCursors(value: Readonly<Record<string, unknown>>): Record<string, number> {
    const entries = Object.entries(value);
    if (entries.length > 10_000) throw new RangeError("too many Session replay Run cursors");
    const result: Record<string, number> = {};
    for (const [runId, sequence] of entries) {
        if (!/^run_[A-Za-z0-9_-]{1,124}$/.test(runId) || typeof sequence !== "number" ||
            !Number.isSafeInteger(sequence) || sequence < 0) {
            throw new TypeError("invalid Session replay Run cursor");
        }
        result[runId] = sequence;
    }
    return result;
}

function positiveInteger(value: number, name: string): number {
    if (!Number.isSafeInteger(value) || value < 1) throw new RangeError(`${name} must be a positive integer`);
    return value;
}
