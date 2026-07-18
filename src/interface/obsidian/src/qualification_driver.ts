import * as Module from "node:module";
import * as path from "node:path";
import { randomBytes } from "node:crypto";
import { readFileSync } from "node:fs";
import { createInterface } from "node:readline";

import { StdioWorkerTransport } from "./runtime/stdio_worker";
import { HarnessClient, REQUIRED_RUNTIME_CAPABILITIES } from "./runtime/harness_client";
import {
    observePluginToolEvents,
    PluginToolEventObserver,
    SerializedPluginToolExecutionFence,
    VaultToolAdapter,
} from "./runtime/vault_tool_adapter";
import {
    FileVaultChangeJournal,
    GitCheckpointStore,
    VaultChangeAuthorizationDecision,
    VaultChangeAuthorizationProposal,
    VaultChangeCoordinator,
} from "./runtime/vault_changes";
import { QualificationVaultPort } from "./qualification_vault";

const PRODUCTION_EXPORTS = Object.freeze([
    "StdioWorkerTransport",
    "VaultToolAdapter",
    "VaultChangeCoordinator",
    "FileVaultChangeJournal",
    "GitCheckpointStore",
]);

function installSourceRootGuard(): void {
    const raw = process.env.OFFERAGENT_QUALIFICATION_FORBID_SOURCE_ROOT;
    if (raw === undefined || raw.length === 0) return;
    const forbidden = path.resolve(raw);
    const withinForbidden = (candidate: string): boolean => {
        const resolved = path.resolve(candidate);
        return resolved === forbidden || resolved.startsWith(`${forbidden}${path.sep}`);
    };
    const sealedDriver = path.resolve(__filename);
    for (const loaded of Object.keys(require.cache)) {
        if (path.resolve(loaded) !== sealedDriver && withinForbidden(loaded)) {
            throw new Error("qualification driver loaded repository source");
        }
    }
    const loader = Module as unknown as {
        _resolveFilename: (
            request: string,
            parent: NodeModule | null,
            isMain: boolean,
            options?: unknown,
        ) => string;
    };
    const resolveFilename = loader._resolveFilename;
    loader._resolveFilename = function guardedResolve(
        request: string,
        parent: NodeModule | null,
        isMain: boolean,
        options?: unknown,
    ): string {
        const resolved = resolveFilename.call(loader, request, parent, isMain, options);
        if (path.isAbsolute(resolved) && withinForbidden(resolved)) {
            throw new Error("qualification driver attempted a repository source import");
        }
        return resolved;
    };
}

function probe(): void {
    installSourceRootGuard();
    const constructors = [
        StdioWorkerTransport,
        VaultToolAdapter,
        VaultChangeCoordinator,
        FileVaultChangeJournal,
        GitCheckpointStore,
    ];
    if (constructors.some((value) => typeof value !== "function")) {
        throw new Error("qualification driver production adapter closure is incomplete");
    }
    process.stdout.write(`${JSON.stringify({
        driverProtocolVersion: 1,
        productionExports: PRODUCTION_EXPORTS,
        sourceFreeRuntime: true,
    })}\n`);
}

type SmokeInput = {
    readonly localAppData: string;
    readonly pluginVersion: string;
    readonly protocolVersion: string;
    readonly runtimeVersion: string;
    readonly schemaHash: string;
    readonly vaultRoot: string;
    readonly workerExecutable: string;
    readonly workspaceId: string;
};

function smokeInput(value: unknown): SmokeInput {
    if (value === null || typeof value !== "object" || Array.isArray(value)) {
        throw new TypeError("qualification smoke input must be an object");
    }
    const candidate = value as Record<string, unknown>;
    const expected = [
        "localAppData", "pluginVersion", "protocolVersion", "runtimeVersion",
        "schemaHash", "vaultRoot", "workerExecutable", "workspaceId",
    ];
    if (Object.keys(candidate).sort().join("\n") !== [...expected].sort().join("\n") ||
        expected.some((key) => typeof candidate[key] !== "string" || candidate[key] === "")) {
        throw new TypeError("qualification smoke input shape is invalid");
    }
    for (const key of ["localAppData", "vaultRoot", "workerExecutable"]) {
        if (!path.isAbsolute(candidate[key] as string)) throw new TypeError(`${key} must be absolute`);
    }
    if (!/^sha256:[0-9a-f]{64}$/u.test(candidate.schemaHash as string)) {
        throw new TypeError("qualification schema hash is invalid");
    }
    return candidate as unknown as SmokeInput;
}

async function smoke(): Promise<void> {
    installSourceRootGuard();
    const input = smokeInput(JSON.parse(readFileSync(0, "utf8")));
    process.env.LOCALAPPDATA = input.localAppData;
    const journalDirectory = path.join(
        input.vaultRoot,
        ".obsidian",
        "offeragent",
        "vault-change-journal",
    );
    const recoveryToken = randomBytes(32).toString("hex");
    const journal = new FileVaultChangeJournal(journalDirectory);
    await journal.markRecoveryReady(recoveryToken);
    const transport = new StdioWorkerTransport(
        input.workerExecutable,
        input.vaultRoot,
        input.runtimeVersion,
        journalDirectory,
        recoveryToken,
    );
    const peer = await transport.connect();
    let workerPid: number | undefined;
    try {
        const result = await peer.request("initialize", {
            protocolVersion: input.protocolVersion,
            clientVersion: input.pluginVersion,
            workspaceId: input.workspaceId,
            capabilities: {
                eventReplay: true,
                multiSession: true,
                approvals: true,
                skills: true,
                shell: true,
                hooks: true,
                subagents: true,
                artifacts: true,
                contentBlocks: true,
                cancellation: true,
                diagnostics: true,
            },
            supportedProtocolRange: {
                minimum: input.protocolVersion,
                maximum: input.protocolVersion,
            },
            requiredCapabilities: [
                "eventReplay", "multiSession", "approvals", "skills", "shell", "hooks",
                "subagents", "artifacts", "contentBlocks", "cancellation", "diagnostics",
            ],
            schemaHash: input.schemaHash,
        });
        if (result === null || typeof result !== "object" || Array.isArray(result)) {
            throw new Error("Worker initialize response is invalid");
        }
        const initialized = result as Record<string, unknown>;
        if (!Number.isSafeInteger(initialized.workerPid) || initialized.transport !== "stdio") {
            throw new Error("Worker initialize identity is invalid");
        }
        workerPid = initialized.workerPid as number;
        await peer.request("shutdown", { reason: "user", gracePeriodMs: 30_000 });
    } finally {
        await peer.close();
    }
    process.stdout.write(`${JSON.stringify({
        driverProtocolVersion: 1,
        sourceFreeRuntime: true,
        transport: "stdio",
        workerPid,
    })}\n`);
}

type DriverRequest = {
    readonly id: string;
    readonly command: string;
    readonly params: Record<string, unknown>;
};

type PendingReview = {
    readonly proposal: VaultChangeAuthorizationProposal;
    readonly resolve: (decision: VaultChangeAuthorizationDecision) => void;
};

type ProductState = {
    readonly client: HarnessClient;
    readonly observer: PluginToolEventObserver;
    readonly fence: SerializedPluginToolExecutionFence;
    readonly unsubscribeEvents: () => void;
    readonly workerPid: number;
};

let product: ProductState | null = null;
const reviews = new Map<string, PendingReview>();

function driverRequest(value: unknown): DriverRequest {
    if (value === null || typeof value !== "object" || Array.isArray(value)) {
        throw new TypeError("qualification control request must be an object");
    }
    const candidate = value as Record<string, unknown>;
    if (typeof candidate.id !== "string" || candidate.id.length === 0 ||
        typeof candidate.command !== "string" || candidate.command.length === 0 ||
        candidate.params === null || typeof candidate.params !== "object" || Array.isArray(candidate.params)) {
        throw new TypeError("qualification control request shape is invalid");
    }
    return candidate as unknown as DriverRequest;
}

function writeDriver(value: unknown): void {
    process.stdout.write(`${JSON.stringify(value)}\n`);
}

function errorMessage(error: unknown): string {
    return error instanceof Error ? error.message : String(error);
}

function requiredString(value: Record<string, unknown>, key: string): string {
    const result = value[key];
    if (typeof result !== "string" || result.length === 0 || result.includes("\0")) {
        throw new TypeError(`${key} must be a non-empty string`);
    }
    return result;
}

function boundedInteger(
    value: Record<string, unknown>,
    key: string,
    minimum: number,
    maximum: number,
): number {
    const result = value[key];
    if (!Number.isSafeInteger(result) || (result as number) < minimum || (result as number) > maximum) {
        throw new TypeError(`${key} must be an integer between ${minimum} and ${maximum}`);
    }
    return result as number;
}

async function startProduct(params: Record<string, unknown>): Promise<Record<string, unknown>> {
    if (product !== null) throw new Error("qualification product is already started");
    const input = smokeInput(params);
    process.env.LOCALAPPDATA = input.localAppData;
    const journalDirectory = path.join(
        input.vaultRoot,
        ".obsidian",
        "offeragent",
        "vault-change-journal",
    );
    const recoveryToken = randomBytes(32).toString("hex");
    const journal = new FileVaultChangeJournal(journalDirectory);
    const vault = new QualificationVaultPort(input.vaultRoot);
    const changes = new VaultChangeCoordinator({
        vault,
        checkpoints: new GitCheckpointStore(input.vaultRoot),
        journal,
        permissionMode: () => "ask_every_time",
        authorize: proposal => new Promise<VaultChangeAuthorizationDecision>((resolve) => {
            const reviewId = `review_${randomBytes(16).toString("hex")}`;
            reviews.set(reviewId, { proposal, resolve });
            writeDriver({ event: "review.proposed", reviewId, proposal });
        }),
    });
    const transport = new StdioWorkerTransport(
        input.workerExecutable,
        input.vaultRoot,
        input.runtimeVersion,
        journalDirectory,
        recoveryToken,
    );
    let fence: SerializedPluginToolExecutionFence | null = null;
    const client = new HarnessClient(
        transport,
        {
            workspaceId: input.workspaceId,
            identity: {
                protocolVersion: input.protocolVersion,
                minimumProtocolVersion: input.protocolVersion,
                maximumProtocolVersion: input.protocolVersion,
                schemaHash: input.schemaHash,
                clientVersion: input.pluginVersion,
            },
            requiredCapabilities: REQUIRED_RUNTIME_CAPABILITIES,
        },
        {
            beforeConnect: async () => fence?.ready(),
            onDisconnected: error => writeDriver({ event: "product.disconnected", error: error.message }),
        },
    );
    const adapter = new VaultToolAdapter(vault, client, input.workspaceId, undefined, undefined, changes);
    fence = new SerializedPluginToolExecutionFence(input.vaultRoot, adapter, async () => {
        await changes.beginRecovery();
        await journal.markRecoveryReady(recoveryToken);
    });
    const observer = observePluginToolEvents(client.reducer, fence, error => {
        writeDriver({ event: "adapter.error", error: error.message });
    });
    const unsubscribeEvents = client.reducer.subscribe(event => {
        writeDriver({ event: "runtime.event", value: event });
    });
    try {
        const initialized = await client.connect();
        product = {
            client,
            observer,
            fence,
            unsubscribeEvents,
            workerPid: initialized.workerPid,
        };
        return {
            identity: initialized,
            reviewResolution: "explicit",
            sourceFreeRuntime: true,
        };
    } catch (error) {
        unsubscribeEvents();
        await observer.dispose().catch(() => undefined);
        await fence.drain().catch(() => undefined);
        await client.close({ shutdown: false }).catch(() => undefined);
        throw error;
    }
}

async function stopProduct(): Promise<Record<string, unknown>> {
    const current = product;
    if (current === null) return { stopped: false };
    product = null;
    current.unsubscribeEvents();
    for (const [reviewId, review] of reviews) {
        review.resolve({ decision: "reject", reviewHash: review.proposal.reviewHash });
        reviews.delete(reviewId);
    }
    await current.observer.dispose();
    await current.fence.drain();
    await current.client.close({ shutdown: true, reason: "user", gracePeriodMs: 30_000 });
    return { stopped: true, workerPid: current.workerPid };
}

async function executeDriverCommand(request: DriverRequest): Promise<Record<string, unknown>> {
    if (request.command === "hello") {
        return {
            driverProtocolVersion: 2,
            reviewResolution: "explicit",
            sourceFreeRuntime: true,
        };
    }
    if (request.command === "product/start") return startProduct(request.params);
    if (request.command === "product/stop") return stopProduct();
    if (request.command === "rpc") {
        if (product === null) throw new Error("qualification product is not started");
        const method = requiredString(request.params, "method");
        const params = request.params.params;
        if (params === null || typeof params !== "object" || Array.isArray(params)) {
            throw new TypeError("rpc params must be an object");
        }
        const genericClient = product.client as unknown as {
            request(method: string, params: Record<string, unknown>): Promise<Record<string, unknown>>;
        };
        return genericClient["request"](method, params as Record<string, unknown>);
    }
    if (request.command === "events/replay") {
        if (product === null) throw new Error("qualification product is not started");
        const runId = requiredString(request.params, "runId");
        const afterSequence = boundedInteger(request.params, "afterSequence", 0, Number.MAX_SAFE_INTEGER);
        const limit = request.params.limit === undefined
            ? 1_000
            : boundedInteger(request.params, "limit", 1, 10_000);
        const lastSequence = await product.client.replay({ runId }, afterSequence, { limit });
        return { lastSequence };
    }
    if (request.command === "attachment/upload") {
        if (product === null) throw new Error("qualification product is not started");
        const sessionId = requiredString(request.params, "sessionId");
        const fileName = requiredString(request.params, "fileName");
        const mediaType = requiredString(request.params, "mediaType");
        if (!["image/png", "image/jpeg", "image/gif", "image/webp"].includes(mediaType)) {
            throw new TypeError("attachment mediaType is unsupported");
        }
        const contentBase64 = requiredString(request.params, "contentBase64");
        const bytes = Buffer.from(contentBase64, "base64");
        if (bytes.toString("base64") !== contentBase64) throw new TypeError("attachment base64 is invalid");
        const altText = request.params.altText;
        if (altText !== undefined && typeof altText !== "string") throw new TypeError("attachment altText is invalid");
        const image = await product.client.uploadAttachment(sessionId, {
            fileName,
            mediaType: mediaType as "image/png" | "image/jpeg" | "image/gif" | "image/webp",
            bytes,
            ...(typeof altText === "string" && altText.length > 0 ? { altText } : {}),
        });
        return image as unknown as Record<string, unknown>;
    }
    if (request.command === "review/resolve") {
        const reviewId = requiredString(request.params, "reviewId");
        const pending = reviews.get(reviewId);
        if (pending === undefined) throw new Error("qualification review is not pending");
        const decision = request.params.decision;
        const reviewHash = requiredString(request.params, "reviewHash");
        if (decision !== "accept" && decision !== "reject") throw new TypeError("review decision is invalid");
        if (reviewHash !== pending.proposal.reviewHash) throw new Error("review hash does not match proposal");
        reviews.delete(reviewId);
        pending.resolve({ decision, reviewHash });
        return { resolved: true };
    }
    throw new Error(`unsupported qualification control command: ${request.command}`);
}

async function serve(): Promise<void> {
    installSourceRootGuard();
    const input = createInterface({ input: process.stdin, crlfDelay: Infinity });
    for await (const line of input) {
        if (line.trim().length === 0) continue;
        let request: DriverRequest;
        try {
            request = driverRequest(JSON.parse(line));
        } catch (error) {
            writeDriver({
                id: null,
                ok: false,
                error: error instanceof Error ? error.message : String(error),
            });
            continue;
        }
        if (request.command === "stop") {
            const result = await stopProduct();
            writeDriver({ id: request.id, ok: true, result });
            input.close();
            break;
        }
        try {
            writeDriver({ id: request.id, ok: true, result: await executeDriverCommand(request) });
        } catch (error) {
            writeDriver({ id: request.id, ok: false, error: errorMessage(error) });
        }
    }
}

const command = process.argv[2];
if (command === "probe") {
    probe();
} else if (command === "smoke") {
    void smoke().catch((error: unknown) => {
        process.stderr.write(`${error instanceof Error ? error.stack ?? error.message : String(error)}\n`);
        process.exitCode = 1;
    });
} else if (command === "serve") {
    void serve().catch((error: unknown) => {
        process.stderr.write(`${error instanceof Error ? error.stack ?? error.message : String(error)}\n`);
        process.exitCode = 1;
    });
} else {
    throw new Error("qualification driver command is unsupported");
}
