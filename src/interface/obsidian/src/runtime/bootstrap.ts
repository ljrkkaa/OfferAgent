import { performance } from "node:perf_hooks";

import { HarnessClient, HarnessCompatibilityError, InitializeResult } from "./harness_client";
import { RemoteRpcError, RpcDisconnectedError, RpcRequestTimeoutError } from "./json_rpc";

export type BootstrapState =
    | "uninitialized"
    | "runtime_missing"
    | "verifying"
    | "installing"
    | "starting_host"
    | "attaching_worker"
    | "handshaking"
    | "ready"
    | "degraded"
    | "restarting"
    | "failed"
    | "stopped";

export type InstallerPhase =
    | "not_installed"
    | "locating_embedded_bundle"
    | "verifying_manifest_and_signature"
    | "extracting_to_staging"
    | "verifying_each_file"
    | "atomic_activate"
    | "runtime_self_test"
    | "ready";

export interface InstalledRuntime {
    readonly version: string;
    readonly workerExecutable: string;
    readonly protocolMinimum: string;
    readonly protocolMaximum: string;
    readonly schemaHash: string;
    /** Development builds verify the package once at the Worker spawn boundary. */
    readonly beforeWorkerLaunch?: (signal?: AbortSignal) => Promise<void>;
}

export interface RuntimeInstaller {
    ensureReady(signal: AbortSignal, onPhase: (phase: InstallerPhase) => void): Promise<InstalledRuntime>;
}

export interface HarnessClientFactory {
    create(runtime: InstalledRuntime, onDisconnected: (error: Error) => void): HarnessClient;
}

export interface BootstrapSnapshot {
    readonly state: BootstrapState;
    readonly generation: number;
    readonly attempt: number;
    readonly runtimeVersion: string | null;
    readonly workerPid: number | null;
    readonly error: BootstrapErrorInfo | null;
}

export interface BootstrapErrorInfo {
    readonly code: string;
    readonly message: string;
    readonly actionable: boolean;
    readonly causeCode?: string;
}

export interface RuntimeBootstrapOptions {
    maximumRestartAttempts?: number;
    restartWindowMs?: number;
    baseRestartDelayMs?: number;
    maximumRestartDelayMs?: number;
    random?: () => number;
    sleep?: (milliseconds: number, signal: AbortSignal) => Promise<void>;
    now?: () => number;
}

const INSTALLER_STATE: Record<InstallerPhase, BootstrapState> = {
    not_installed: "runtime_missing",
    locating_embedded_bundle: "runtime_missing",
    verifying_manifest_and_signature: "verifying",
    extracting_to_staging: "installing",
    verifying_each_file: "installing",
    atomic_activate: "installing",
    runtime_self_test: "installing",
    ready: "starting_host",
};

export class RuntimeBootstrap {
    private readonly installer: RuntimeInstaller;
    private readonly clients: HarnessClientFactory;
    private readonly maximumRestartAttempts: number;
    private readonly restartWindowMs: number;
    private readonly baseRestartDelayMs: number;
    private readonly maximumRestartDelayMs: number;
    private readonly random: () => number;
    private readonly sleep: (milliseconds: number, signal: AbortSignal) => Promise<void>;
    private readonly now: () => number;
    private readonly listeners = new Set<(snapshot: BootstrapSnapshot) => void>();
    private readonly restartTimes: number[] = [];
    private currentState: BootstrapState = "uninitialized";
    private generation = 0;
    private attempt = 0;
    private runtime: InstalledRuntime | null = null;
    private client: HarnessClient | null = null;
    private controller: AbortController | null = null;
    private failure: BootstrapErrorInfo | null = null;
    private operation: Promise<InitializeResult> | null = null;
    private lastClockReading = 0;

    constructor(installer: RuntimeInstaller, clients: HarnessClientFactory, options: RuntimeBootstrapOptions = {}) {
        this.installer = installer;
        this.clients = clients;
        this.maximumRestartAttempts = positiveInteger(options.maximumRestartAttempts ?? 5, "maximumRestartAttempts");
        this.restartWindowMs = positiveInteger(options.restartWindowMs ?? 10 * 60_000, "restartWindowMs");
        this.baseRestartDelayMs = positiveInteger(options.baseRestartDelayMs ?? 500, "baseRestartDelayMs");
        this.maximumRestartDelayMs = positiveInteger(options.maximumRestartDelayMs ?? 30_000, "maximumRestartDelayMs");
        if (this.maximumRestartDelayMs < this.baseRestartDelayMs) throw new RangeError("restart delay bounds are inverted");
        this.random = options.random ?? Math.random;
        this.sleep = options.sleep ?? abortableSleep;
        this.now = options.now ?? (() => performance.now());
    }

    get snapshot(): BootstrapSnapshot {
        return {
            state: this.currentState,
            generation: this.generation,
            attempt: this.attempt,
            runtimeVersion: this.runtime?.version ?? null,
            workerPid: this.client?.ready ? this.client.identity.workerPid : null,
            error: this.failure,
        };
    }

    get harness(): HarnessClient {
        if (this.currentState !== "ready" || !this.client?.ready) throw new Error("OfferAgent Runtime is not ready");
        return this.client;
    }

    get installedRuntime(): InstalledRuntime | null {
        return this.runtime;
    }

    subscribe(listener: (snapshot: BootstrapSnapshot) => void): () => void {
        this.listeners.add(listener);
        listener(this.snapshot);
        return () => this.listeners.delete(listener);
    }

    start(): Promise<InitializeResult> {
        if (this.currentState === "ready" && this.client) return Promise.resolve(this.client.identity);
        if (this.operation) return this.operation;
        if (this.currentState === "stopped" || this.currentState === "failed") {
            this.restartTimes.length = 0;
        }
        const generation = ++this.generation;
        this.controller?.abort();
        this.controller = new AbortController();
        this.failure = null;
        this.attempt = 0;
        const operation = this.installAndConnect(generation, this.controller.signal);
        this.operation = operation;
        void operation.then(
            () => { if (this.generation === generation && this.operation === operation) this.operation = null; },
            () => { if (this.generation === generation && this.operation === operation) this.operation = null; },
        );
        return operation;
    }

    async stop(options: { shutdownWorker?: boolean } = {}): Promise<void> {
        ++this.generation;
        this.controller?.abort();
        this.controller = null;
        const client = this.client;
        this.client = null;
        this.operation = null;
        await client?.close({ shutdown: options.shutdownWorker }).catch(() => undefined);
        this.transition("stopped");
    }

    private async installAndConnect(generation: number, signal: AbortSignal): Promise<InitializeResult> {
        try {
            this.runtime = await this.installer.ensureReady(signal, (phase) => {
                if (this.generation === generation) this.transition(INSTALLER_STATE[phase]);
            });
            signal.throwIfAborted();
            return await this.connectLoop(generation, signal);
        } catch (error) {
            if (signal.aborted || this.generation !== generation) throw abortError();
            this.failure = classifyBootstrapFailure(error).info;
            this.transition("failed");
            throw error;
        }
    }

    private async connectLoop(generation: number, signal: AbortSignal): Promise<InitializeResult> {
        while (true) {
            signal.throwIfAborted();
            this.attempt += 1;
            this.transition(this.attempt === 1 ? "starting_host" : "restarting");
            const disconnect = (error: Error) => this.handleDisconnect(generation, error);
            const client = this.clients.create(this.runtime as InstalledRuntime, disconnect);
            this.client = client;
            try {
                this.transition("attaching_worker");
                this.transition("handshaking");
                const identity = await client.connect(signal);
                if (this.generation !== generation) {
                    await client.close();
                    throw abortError();
                }
                this.failure = null;
                this.transition("ready");
                return identity;
            } catch (error) {
                await client.close().catch(() => undefined);
                if (this.client === client) this.client = null;
                if (signal.aborted || this.generation !== generation) throw abortError();
                const classified = classifyBootstrapFailure(error);
                this.failure = classified.info;
                this.transition("degraded");
                if (!classified.retryable) throw error;
                if (!this.recordRestart()) throw new RuntimeBootstrapCircuitOpen(classified.info);
                await this.sleep(this.retryDelay(classified), signal);
            }
        }
    }

    private handleDisconnect(generation: number, error: Error): void {
        if (this.generation !== generation || this.currentState !== "ready" || !this.controller) return;
        const client = this.client;
        this.client = null;
        void client?.close().catch(() => undefined);
        const classified = classifyBootstrapFailure(error);
        this.failure = classified.info;
        this.transition("degraded");
        if (!classified.retryable) {
            this.transition("failed");
            return;
        }
        if (!this.recordRestart()) {
            this.failure = classifyBootstrapFailure(new RuntimeBootstrapCircuitOpen(classified.info)).info;
            this.transition("failed");
            return;
        }
        const signal = this.controller.signal;
        const operation = this.sleep(this.retryDelay(classified), signal).then(
            () => this.connectLoop(generation, signal),
        ).catch((failure) => {
            if (this.generation === generation && !this.controller?.signal.aborted) {
                this.failure = classifyBootstrapFailure(failure).info;
                this.transition("failed");
            }
            throw failure;
        });
        this.operation = operation;
        void operation.then(
            () => { if (this.generation === generation && this.operation === operation) this.operation = null; },
            () => { if (this.generation === generation && this.operation === operation) this.operation = null; },
        );
        // A background reconnect failure is surfaced through state/listeners.
        void operation.catch(() => undefined);
    }

    private recordRestart(): boolean {
        const now = this.now();
        if (!Number.isFinite(now) || now < 0 || now < this.lastClockReading) {
            throw new Error("restart clock must be finite and monotonic");
        }
        this.lastClockReading = now;
        while (this.restartTimes.length > 0 && this.restartTimes[0] <= now - this.restartWindowMs) {
            this.restartTimes.shift();
        }
        this.restartTimes.push(now);
        return this.restartTimes.length <= this.maximumRestartAttempts;
    }

    private retryDelay(failure: ClassifiedBootstrapFailure): number {
        return Math.max(this.restartDelay(Math.max(1, this.restartTimes.length)), failure.retryAfterMs ?? 0);
    }

    private restartDelay(attempt: number): number {
        const ceiling = Math.min(this.maximumRestartDelayMs, this.baseRestartDelayMs * 2 ** Math.min(attempt - 1, 20));
        const random = this.random();
        if (!Number.isFinite(random) || random < 0 || random > 1) throw new Error("restart jitter source is invalid");
        return Math.max(1, Math.round(ceiling * (0.5 + random * 0.5)));
    }

    private transition(state: BootstrapState): void {
        this.currentState = state;
        const snapshot = this.snapshot;
        for (const listener of this.listeners) listener(snapshot);
    }
}

export class RuntimeBootstrapCircuitOpen extends Error {
    readonly lastFailure: BootstrapErrorInfo;

    constructor(lastFailure: BootstrapErrorInfo) {
        super("OfferAgent Runtime restart circuit is open");
        this.name = "RuntimeBootstrapCircuitOpen";
        this.lastFailure = lastFailure;
    }
}

/** Background Vault bookkeeping must not defeat a terminal failure or explicit stop. */
export function canBackgroundStartRuntime(state: BootstrapState): boolean {
    return state !== "failed" && state !== "stopped";
}

interface ClassifiedBootstrapFailure {
    readonly info: BootstrapErrorInfo;
    readonly retryable: boolean;
    readonly retryAfterMs: number | null;
}

function classifyBootstrapFailure(error: unknown): ClassifiedBootstrapFailure {
    if (error instanceof RuntimeBootstrapCircuitOpen) {
        return {
            info: {
                code: "runtime_crash_loop",
                message: "Runtime 连续失败，已暂停自动重启。",
                actionable: true,
                causeCode: error.lastFailure.causeCode ?? error.lastFailure.code,
            },
            retryable: false,
            retryAfterMs: null,
        };
    }
    if (error instanceof HarnessCompatibilityError) {
        return {
            info: { code: "runtime_incompatible", message: "Runtime 与插件协议不兼容，请升级或回滚。", actionable: true },
            retryable: false,
            retryAfterMs: null,
        };
    }
    if (error instanceof RemoteRpcError) {
        return {
            info: {
                code: error.envelope.code,
                message: error.envelope.userVisibleMessage,
                actionable: true,
            },
            retryable: error.envelope.retryable && !error.envelope.cancelled,
            retryAfterMs: error.envelope.retryAfterMs ?? null,
        };
    }
    if (error instanceof RpcRequestTimeoutError) {
        return {
            info: { code: "runtime_timeout", message: "本地 Runtime 响应超时，正在按退避策略重试。", actionable: true },
            retryable: true,
            retryAfterMs: null,
        };
    }
    if (error instanceof RpcDisconnectedError) {
        const causeCode = workerExitCauseCode(error.message);
        return {
            info: {
                code: "runtime_disconnected",
                message: causeCode
                    ? `本地 Worker 已退出（${causeCode}），正在按退避策略重连。`
                    : "本地 Runtime 连接已中断，正在按退避策略重连。",
                actionable: true,
                ...(causeCode ? { causeCode } : {}),
            },
            retryable: true,
            retryAfterMs: null,
        };
    }
    return {
        info: { code: "runtime_unavailable", message: "本地 Runtime 发生未知失败；为避免错误重试，已停止自动连接。", actionable: true },
        retryable: false,
        retryAfterMs: null,
    };
}

function workerExitCauseCode(message: string): string | undefined {
    const match = /OfferAgent Worker exited \([^\r\n]{1,64}\): offeragent-worker: local development startup failed \(([A-Za-z][A-Za-z0-9_.-]{0,63})\)$/.exec(message);
    return match?.[1];
}

function abortableSleep(milliseconds: number, signal: AbortSignal): Promise<void> {
    return new Promise((resolve, reject) => {
        if (signal.aborted) { reject(abortError()); return; }
        const timer = setTimeout(() => { cleanup(); resolve(); }, milliseconds);
        const abort = () => { clearTimeout(timer); cleanup(); reject(abortError()); };
        const cleanup = () => signal.removeEventListener("abort", abort);
        signal.addEventListener("abort", abort, { once: true });
    });
}

function positiveInteger(value: number, name: string): number {
    if (!Number.isSafeInteger(value) || value < 1) throw new RangeError(`${name} must be a positive integer`);
    return value;
}

function abortError(): Error {
    const error = new Error("OfferAgent Runtime bootstrap cancelled");
    error.name = "AbortError";
    return error;
}

export {
    HarnessCompatibilityError,
    RemoteRpcError,
    RpcDisconnectedError,
    RpcRequestTimeoutError,
};
