import { ChildProcessWithoutNullStreams, spawn } from "node:child_process";
import { isAbsolute } from "node:path";

import { ByteChannel, JsonRpcPeer, RpcDisconnectedError } from "./json_rpc";

export interface RpcTransport {
    connect(signal?: AbortSignal): Promise<JsonRpcPeer>;
}

type ChildExitWaiter = (exit: Promise<void>, timeoutMs: number) => Promise<boolean>;

export interface ChildStdioChannelOptions {
    gracefulExitTimeoutMs?: number;
    waitForExit?: ChildExitWaiter;
}

// Peer EOF drives the Worker's direct shutdown path, whose persistence grace
// defaults to ten seconds. Leave modest teardown overhead before forcing exit;
// the returned close Promise still waits for the authoritative process join.
const DEFAULT_GRACEFUL_EXIT_TIMEOUT_MS = 15_000;

/**
 * One Worker is a child of this plugin.  Its inherited stdio handles are the
 * sole local transport; there is no listener or discovery record.
 */
export class StdioWorkerTransport implements RpcTransport {
    private readonly executable: string;
    private readonly vaultRoot: string;
    private readonly runtimeVersion: string;

    constructor(executable: string, vaultRoot: string, runtimeVersion: string) {
        if (!isAbsolute(executable) || !/offeragent-worker\.exe$/i.test(executable)) {
            throw new TypeError("Worker executable must be an absolute offeragent-worker.exe path");
        }
        if (!isAbsolute(vaultRoot) || vaultRoot.includes("\0")) throw new TypeError("Vault root must be absolute");
        if (!/^\d+\.\d+\.\d+(?:[-.][0-9A-Za-z.-]+)?$/.test(runtimeVersion)) {
            throw new TypeError("Runtime version is invalid");
        }
        this.executable = executable;
        this.vaultRoot = vaultRoot;
        this.runtimeVersion = runtimeVersion;
    }

    async connect(signal?: AbortSignal): Promise<JsonRpcPeer> {
        if (signal?.aborted) throw abortError();
        const child = spawn(this.executable, [
            "stdio",
            "--vault-root", this.vaultRoot,
            "--runtime-version", this.runtimeVersion,
        ], {
            windowsHide: true,
            stdio: ["pipe", "pipe", "pipe"],
            env: {
                SystemRoot: process.env.SystemRoot ?? "C:\\Windows",
                LOCALAPPDATA: process.env.LOCALAPPDATA ?? "",
            },
        });
        const channel = new ChildStdioChannel(child);
        const abort = () => void channel.close();
        signal?.addEventListener("abort", abort, { once: true });
        try {
            if (signal?.aborted) throw abortError();
            return new JsonRpcPeer(channel);
        } catch (error) {
            await channel.close();
            throw error;
        } finally {
            signal?.removeEventListener("abort", abort);
        }
    }
}

export class ChildStdioChannel implements ByteChannel {
    private readonly child: ChildProcessWithoutNullStreams;
    private readonly gracefulExitTimeoutMs: number;
    private readonly waitForExit: ChildExitWaiter;
    private readonly dataListeners = new Set<(chunk: Uint8Array) => void>();
    private readonly closeListeners = new Set<(error?: Error) => void>();
    private readonly stderr: Buffer[] = [];
    private readonly childTerminated: Promise<void>;
    private resolveChildTerminated: (() => void) | null = null;
    private stderrBytes = 0;
    private channelClosed = false;
    private childExited = false;
    private closeError: Error | undefined;
    private closeOperation: Promise<void> | null = null;

    constructor(child: ChildProcessWithoutNullStreams, options: ChildStdioChannelOptions = {}) {
        this.child = child;
        this.gracefulExitTimeoutMs = options.gracefulExitTimeoutMs ?? DEFAULT_GRACEFUL_EXIT_TIMEOUT_MS;
        if (!Number.isSafeInteger(this.gracefulExitTimeoutMs) || this.gracefulExitTimeoutMs < 1) {
            throw new RangeError("Worker exit grace period must be a positive integer");
        }
        this.waitForExit = options.waitForExit ?? settlesBeforeDeadline;
        this.childTerminated = new Promise((resolvePromise) => {
            this.resolveChildTerminated = resolvePromise;
        });
        child.stdout.on("data", (chunk: Buffer) => {
            for (const listener of this.dataListeners) listener(chunk);
        });
        child.stderr.on("data", (chunk: Buffer) => this.captureStderr(chunk));
        child.once("error", (error) => this.failChannel(error));
        child.once("exit", (code, signal) => this.acceptChildTermination(code, signal));
        // Failed spawns emit error + close without an exit event.  Treat close
        // as an equally authoritative join boundary for that path.
        child.once("close", (code, signal) => this.acceptChildTermination(code, signal));
        child.stdout.once("error", (error) => this.failChannel(error));
        child.stdin.once("error", (error) => this.failChannel(error));
        if (child.exitCode !== null || child.signalCode !== null) this.markChildTerminated();
    }

    async write(data: Uint8Array): Promise<void> {
        if (this.channelClosed || !this.child.stdin.writable) {
            throw new RpcDisconnectedError("OfferAgent Worker stdin is closed");
        }
        await new Promise<void>((resolvePromise, rejectPromise) => {
            this.child.stdin.write(data, (error) => error ? rejectPromise(error) : resolvePromise());
        });
    }

    close(): Promise<void> {
        return this.ensureChildStopped();
    }

    onData(listener: (chunk: Uint8Array) => void): () => void {
        this.dataListeners.add(listener);
        return () => this.dataListeners.delete(listener);
    }

    onClose(listener: (error?: Error) => void): () => void {
        if (this.channelClosed) listener(this.closeError ?? new RpcDisconnectedError("OfferAgent Worker is closed"));
        else this.closeListeners.add(listener);
        return () => this.closeListeners.delete(listener);
    }

    private closeChannel(error?: Error): void {
        if (this.channelClosed) return;
        this.channelClosed = true;
        this.closeError = error;
        for (const listener of this.closeListeners) listener(error);
        this.closeListeners.clear();
        this.dataListeners.clear();
    }

    private failChannel(error: Error): void {
        this.closeChannel(error);
        // Transport failure is not proof that the process exited.  Start the
        // same bounded terminate-and-join operation used by an explicit close.
        void this.ensureChildStopped().catch(() => undefined);
    }

    private acceptChildTermination(code: number | null, signal: NodeJS.Signals | null): void {
        this.markChildTerminated();
        this.closeChannel(new RpcDisconnectedError(this.exitMessage(code, signal)));
    }

    private markChildTerminated(): void {
        if (this.childExited) return;
        this.childExited = true;
        this.resolveChildTerminated?.();
        this.resolveChildTerminated = null;
    }

    private ensureChildStopped(): Promise<void> {
        if (this.closeOperation === null) {
            let resolveOperation!: () => void;
            let rejectOperation!: (error: unknown) => void;
            this.closeOperation = new Promise<void>((resolvePromise, rejectPromise) => {
                resolveOperation = resolvePromise;
                rejectOperation = rejectPromise;
            });
            // Publish closeOperation before stopChild closes stdin: stream error
            // handlers may re-enter ensureChildStopped synchronously.
            void this.stopChild().then(resolveOperation, rejectOperation);
        }
        return this.closeOperation;
    }

    private async stopChild(): Promise<void> {
        this.closeChannel();
        if (this.childExited) return;
        try {
            this.child.stdin.end();
        } catch {
            // A synchronous stream failure is not a terminal process state.
            // Continue to the bounded wait and forceful termination below.
        }
        let exitedGracefully = false;
        try {
            exitedGracefully = await this.waitForExit(this.childTerminated, this.gracefulExitTimeoutMs);
        } catch {
            // A faulty deadline implementation must fail closed by forcing the
            // child down, never by allowing a replacement to race it.
        }
        if (exitedGracefully || this.childExited) return;

        let killError: unknown;
        try {
            this.child.kill("SIGKILL");
        } catch (error) {
            killError = error;
        }
        // Do not return merely because a signal was sent: ChildProcess.killed
        // is not an exit acknowledgement.  Joining here preserves the one-
        // Worker invariant before stop/restart or reconnect can continue.
        await this.childTerminated;
        if (killError !== undefined) throw killError;
    }

    private captureStderr(chunk: Buffer): void {
        const capacity = 8 * 1024 - this.stderrBytes;
        if (capacity <= 0) return;
        const accepted = chunk.subarray(0, capacity);
        this.stderr.push(Buffer.from(accepted));
        this.stderrBytes += accepted.length;
    }

    private exitMessage(code: number | null, signal: NodeJS.Signals | null): string {
        const detail = Buffer.concat(this.stderr).toString("utf8").trim().replace(/[\r\n]+/g, " ");
        const outcome = signal ? `signal ${signal}` : `exit code ${code ?? "unknown"}`;
        return detail ? `OfferAgent Worker exited (${outcome}): ${detail}` : `OfferAgent Worker exited (${outcome})`;
    }
}

function abortError(): Error {
    const error = new Error("OfferAgent Worker startup was cancelled");
    error.name = "AbortError";
    return error;
}

function settlesBeforeDeadline(operation: Promise<void>, timeoutMs: number): Promise<boolean> {
    return new Promise((resolvePromise) => {
        let settled = false;
        const finish = (result: boolean) => {
            if (settled) return;
            settled = true;
            clearTimeout(timer);
            resolvePromise(result);
        };
        const timer = setTimeout(() => finish(false), timeoutMs);
        void operation.then(() => finish(true));
    });
}
