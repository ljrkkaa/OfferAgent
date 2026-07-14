import { randomBytes } from "node:crypto";
import { mkdir, open, readFile, rename, rm } from "node:fs/promises";
import { dirname, isAbsolute, join } from "node:path";

import { JsonObject, JsonValue, requireJsonObject, requireJsonValue } from "../runtime/json_rpc";
import { parseStrictJson } from "../runtime/strict_json";

const SCHEMA_VERSION = 1;
const MAX_JOURNAL_BYTES = 16 * 1024 * 1024;
const MAX_RECORDS = 10_000;

export interface ClientRecoveryState {
    readonly path: string;
    readonly beforeHash: string;
    readonly afterHash: string;
}

export interface StartedInvocation {
    readonly schemaVersion: 1;
    readonly invocationId: string;
    readonly toolCallId: string;
    readonly runId: string;
    readonly requestHash: string;
    readonly state: "started";
    readonly startedAt: string;
    readonly recovery: readonly ClientRecoveryState[];
}

export interface CompletedInvocation {
    readonly schemaVersion: 1;
    readonly invocationId: string;
    readonly toolCallId: string;
    readonly runId: string;
    readonly requestHash: string;
    readonly state: "completed";
    readonly startedAt: string;
    readonly completedAt: string;
    readonly recovery: readonly ClientRecoveryState[];
    readonly result: JsonObject;
}

export type InvocationJournalRecord = StartedInvocation | CompletedInvocation;

export class InvocationJournalConflict extends Error {
    constructor(message: string) {
        super(message);
        this.name = "InvocationJournalConflict";
    }
}

export class ClientInvocationJournal {
    private readonly path: string;
    private readonly workspaceInstanceId: string;
    private queue: Promise<void> = Promise.resolve();

    constructor(path: string, workspaceInstanceId: string) {
        if (!isAbsolute(path) || path.includes("\0")) throw new TypeError("client invocation journal path must be absolute");
        if (!/^[A-Za-z0-9][A-Za-z0-9_-]{0,127}$/.test(workspaceInstanceId)) {
            throw new TypeError("invalid Workspace instance identity");
        }
        this.path = path;
        this.workspaceInstanceId = workspaceInstanceId;
    }

    lookup(invocationId: string): Promise<InvocationJournalRecord | null> {
        return this.serial(async () => (await this.load()).get(invocationId) ?? null);
    }

    begin(record: Omit<StartedInvocation, "schemaVersion" | "state" | "startedAt">): Promise<StartedInvocation> {
        return this.serial(async () => {
            const records = await this.load();
            const existing = records.get(record.invocationId);
            if (existing) {
                requireSameRequest(existing, record);
                return existing.state === "started" ? existing : (() => { throw new InvocationJournalConflict("invocation is already complete"); })();
            }
            const started: StartedInvocation = {
                schemaVersion: 1,
                invocationId: record.invocationId,
                toolCallId: record.toolCallId,
                runId: record.runId,
                requestHash: record.requestHash,
                state: "started",
                startedAt: new Date().toISOString(),
                recovery: record.recovery.map(validateRecoveryState),
            };
            records.set(started.invocationId, started);
            await this.save(records);
            return started;
        });
    }

    complete(invocationId: string, requestHash: string, result: JsonObject): Promise<CompletedInvocation> {
        return this.serial(async () => {
            const records = await this.load();
            const existing = records.get(invocationId);
            if (!existing) throw new InvocationJournalConflict("invocation was not durably started");
            if (existing.requestHash !== requestHash) throw new InvocationJournalConflict("invocation request binding changed");
            if (existing.state === "completed") {
                if (canonicalJson(existing.result) !== canonicalJson(result)) {
                    throw new InvocationJournalConflict("completed invocation result changed");
                }
                return existing;
            }
            const completed: CompletedInvocation = {
                ...existing,
                state: "completed",
                completedAt: new Date().toISOString(),
                result: requireJsonObject(result),
            };
            records.set(invocationId, completed);
            await this.save(records);
            return completed;
        });
    }

    private serial<T>(action: () => Promise<T>): Promise<T> {
        const result = this.queue.then(action, action);
        this.queue = result.then(() => undefined, () => undefined);
        return result;
    }

    private async load(): Promise<Map<string, InvocationJournalRecord>> {
        let bytes: Buffer;
        try {
            const raw = await readFile(this.path);
            if (raw.length === 0 || raw.length > MAX_JOURNAL_BYTES) throw new InvocationJournalConflict("journal size is invalid");
            bytes = raw;
        } catch (error) {
            if ((error as NodeJS.ErrnoException).code === "ENOENT") return new Map();
            throw error;
        }
        let raw: unknown;
        try {
            raw = parseStrictJson(new TextDecoder("utf-8", { fatal: true }).decode(bytes), {
                maximumCharacters: MAX_JOURNAL_BYTES,
                maximumNodes: MAX_RECORDS * 16,
            });
        } catch (error) {
            throw new InvocationJournalConflict("client invocation journal is corrupt");
        }
        const root = requireJsonObject(raw);
        if (Object.keys(root).length !== 3 || root.schemaVersion !== SCHEMA_VERSION ||
            root.workspaceInstanceId !== this.workspaceInstanceId || !Array.isArray(root.records) ||
            root.records.length > MAX_RECORDS) {
            throw new InvocationJournalConflict("client invocation journal identity/schema is invalid");
        }
        const records = new Map<string, InvocationJournalRecord>();
        for (const item of root.records) {
            const record = parseRecord(item);
            if (records.has(record.invocationId)) throw new InvocationJournalConflict("duplicate invocation journal record");
            records.set(record.invocationId, record);
        }
        return records;
    }

    private async save(records: Map<string, InvocationJournalRecord>): Promise<void> {
        while (records.size > MAX_RECORDS) {
            const oldestCompleted = [...records.values()]
                .filter((record): record is CompletedInvocation => record.state === "completed")
                .sort((left, right) => left.completedAt.localeCompare(right.completedAt))[0];
            if (!oldestCompleted) throw new InvocationJournalConflict("journal contains too many unresolved invocations");
            records.delete(oldestCompleted.invocationId);
        }
        const content = Buffer.from(canonicalJson({
            records: [...records.values()].sort((left, right) => left.invocationId.localeCompare(right.invocationId)),
            schemaVersion: SCHEMA_VERSION,
            workspaceInstanceId: this.workspaceInstanceId,
        }) + "\n", "utf8");
        if (content.length > MAX_JOURNAL_BYTES) throw new InvocationJournalConflict("journal exceeds its hard size limit");
        const directory = dirname(this.path);
        await mkdir(directory, { recursive: true, mode: 0o700 });
        const temporary = join(directory, `.client-invocations.${randomBytes(16).toString("hex")}.tmp`);
        let handle;
        try {
            handle = await open(temporary, "wx", 0o600);
            await handle.writeFile(content);
            await handle.sync();
            await handle.close();
            handle = undefined;
            await rename(temporary, this.path);
        } finally {
            await handle?.close().catch(() => undefined);
            await rm(temporary, { force: true }).catch(() => undefined);
        }
    }
}

export function canonicalJson(value: unknown): string {
    return JSON.stringify(canonicalValue(value));
}

function canonicalValue(value: unknown, depth = 0): JsonValue {
    if (depth > 64) throw new TypeError("canonical JSON nesting limit exceeded");
    const checked = requireJsonValue(value);
    if (checked === null || typeof checked !== "object") return checked;
    if (Array.isArray(checked)) return checked.map((item) => canonicalValue(item, depth + 1));
    const result: JsonObject = {};
    for (const key of Object.keys(checked).sort()) result[key] = canonicalValue(checked[key], depth + 1);
    return result;
}

function parseRecord(raw: JsonValue): InvocationJournalRecord {
    const value = requireJsonObject(raw);
    const state = value.state;
    const expected = state === "completed"
        ? ["completedAt", "invocationId", "recovery", "requestHash", "result", "runId", "schemaVersion", "startedAt", "state", "toolCallId"]
        : ["invocationId", "recovery", "requestHash", "runId", "schemaVersion", "startedAt", "state", "toolCallId"];
    const keys = Object.keys(value).sort();
    if (keys.length !== expected.length || expected.some((key, index) => keys[index] !== key) || value.schemaVersion !== 1 ||
        (state !== "started" && state !== "completed")) {
        throw new InvocationJournalConflict("invocation record schema is invalid");
    }
    const base = {
        schemaVersion: 1 as const,
        invocationId: identifier(value.invocationId, "inv_"),
        toolCallId: identifier(value.toolCallId, "call_"),
        runId: identifier(value.runId, "run_"),
        requestHash: sha256(value.requestHash),
        startedAt: timestamp(value.startedAt),
        recovery: recoveryArray(value.recovery),
    };
    if (state === "started") return { ...base, state };
    return {
        ...base,
        state,
        completedAt: timestamp(value.completedAt),
        result: requireJsonObject(value.result),
    };
}

function recoveryArray(value: JsonValue | undefined): ClientRecoveryState[] {
    if (!Array.isArray(value) || value.length > 64) throw new InvocationJournalConflict("invalid recovery state array");
    const result = value.map((item) => {
        const raw = requireJsonObject(item);
        if (Object.keys(raw).length !== 3) throw new InvocationJournalConflict("invalid recovery state");
        return validateRecoveryState({
            path: typeof raw.path === "string" ? raw.path : "",
            beforeHash: typeof raw.beforeHash === "string" ? raw.beforeHash : "",
            afterHash: typeof raw.afterHash === "string" ? raw.afterHash : "",
        });
    });
    if (new Set(result.map((item) => item.path)).size !== result.length) {
        throw new InvocationJournalConflict("recovery paths must be unique");
    }
    return result;
}

function validateRecoveryState(value: ClientRecoveryState): ClientRecoveryState {
    if (!safePath(value.path)) throw new TypeError("invalid recovery path");
    if (value.beforeHash !== "absent") sha256(value.beforeHash);
    if (value.afterHash !== "absent") sha256(value.afterHash);
    if (value.beforeHash === value.afterHash) {
        throw new InvocationJournalConflict("recovery before/after hashes must differ");
    }
    return { ...value };
}

function requireSameRequest(
    existing: InvocationJournalRecord,
    incoming: { toolCallId: string; runId: string; requestHash: string },
): void {
    if (existing.toolCallId !== incoming.toolCallId || existing.runId !== incoming.runId ||
        existing.requestHash !== incoming.requestHash) {
        throw new InvocationJournalConflict("invocation id was reused with another request");
    }
}

function identifier(value: JsonValue | undefined, prefix: string): string {
    if (typeof value !== "string" || value.length > 128 || !new RegExp(`^${prefix}[A-Za-z0-9][A-Za-z0-9_-]*$`).test(value)) {
        throw new InvocationJournalConflict(`invalid ${prefix} identity`);
    }
    return value;
}

function sha256(value: JsonValue | undefined): string {
    if (typeof value !== "string" || !/^sha256:[0-9a-f]{64}$/.test(value)) {
        throw new InvocationJournalConflict("invalid sha256 digest");
    }
    return value;
}

function timestamp(value: JsonValue | undefined): string {
    if (typeof value !== "string" || !/(?:Z|[+-]\d{2}:\d{2})$/.test(value) || !Number.isFinite(Date.parse(value))) {
        throw new InvocationJournalConflict("invalid journal timestamp");
    }
    return value;
}

function safePath(value: string): boolean {
    return value.length > 0 && value.length <= 1024 && !value.startsWith("/") && !value.includes("\\") &&
        !value.includes("\0") && value.split("/").every((part) => part !== "" && part !== "." && part !== "..");
}
