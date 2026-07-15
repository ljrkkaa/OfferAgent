import { ChildProcessWithoutNullStreams, spawn } from "node:child_process";
import { isAbsolute } from "node:path";

import { ByteChannel, JsonRpcPeer, RpcDisconnectedError } from "./json_rpc";

export interface RpcTransport {
    connect(signal?: AbortSignal): Promise<JsonRpcPeer>;
}

/**
 * One Worker is a child of this plugin.  Its inherited stdio handles are the
 * sole local transport; there is no listener, discovery record, or Host.
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

class ChildStdioChannel implements ByteChannel {
    private readonly child: ChildProcessWithoutNullStreams;
    private readonly dataListeners = new Set<(chunk: Uint8Array) => void>();
    private readonly closeListeners = new Set<(error?: Error) => void>();
    private readonly stderr: Buffer[] = [];
    private stderrBytes = 0;
    private closed = false;

    constructor(child: ChildProcessWithoutNullStreams) {
        this.child = child;
        child.stdout.on("data", (chunk: Buffer) => {
            for (const listener of this.dataListeners) listener(chunk);
        });
        child.stderr.on("data", (chunk: Buffer) => this.captureStderr(chunk));
        child.once("error", (error) => this.finish(error));
        child.once("exit", (code, signal) => this.finish(new RpcDisconnectedError(
            this.exitMessage(code, signal),
        )));
        child.stdout.once("error", (error) => this.finish(error));
        child.stdin.once("error", (error) => this.finish(error));
    }

    async write(data: Uint8Array): Promise<void> {
        if (this.closed || !this.child.stdin.writable) throw new RpcDisconnectedError("OfferAgent Worker stdin is closed");
        await new Promise<void>((resolvePromise, rejectPromise) => {
            this.child.stdin.write(data, (error) => error ? rejectPromise(error) : resolvePromise());
        });
    }

    async close(): Promise<void> {
        if (this.closed) return;
        this.closed = true;
        this.child.stdin.end();
        if (!this.child.killed) this.child.kill();
    }

    onData(listener: (chunk: Uint8Array) => void): () => void {
        this.dataListeners.add(listener);
        return () => this.dataListeners.delete(listener);
    }

    onClose(listener: (error?: Error) => void): () => void {
        if (this.closed) listener(new RpcDisconnectedError("OfferAgent Worker is closed"));
        else this.closeListeners.add(listener);
        return () => this.closeListeners.delete(listener);
    }

    private finish(error?: Error): void {
        if (this.closed) return;
        this.closed = true;
        for (const listener of this.closeListeners) listener(error);
        this.closeListeners.clear();
        this.dataListeners.clear();
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
