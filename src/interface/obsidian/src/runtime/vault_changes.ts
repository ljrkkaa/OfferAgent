import { Buffer } from "node:buffer";
import { execFile } from "node:child_process";
import { createHash } from "node:crypto";
import { mkdir, mkdtemp, open, readFile, realpath, rename, rm } from "node:fs/promises";
import { dirname, isAbsolute, join, relative, resolve, sep } from "node:path";
import { promisify } from "node:util";
import * as os from "node:os";

import type { TFile, Vault } from "obsidian";

import type {
    ErrorCode,
    ExecutableToolCallDescriptor,
    SideEffect,
    ToolResultDescriptor,
} from "./generated_protocol";

const execFileAsync = promisify(execFile);
const MAX_ACTIONS = 20;
const MAX_BATCH_BYTES = 131_072;
const MAX_FILE_BYTES = 262_144;
const MAX_DIFF_BYTES = 32_768;
const MAX_PATH_LENGTH = 512;
const MAX_TASK_BYTES = 512;
const BATCH_ID = /^[A-Za-z0-9][A-Za-z0-9_-]{0,127}$/u;
const DIGEST = /^sha256:[0-9a-f]{64}$/u;

export type VaultPermissionMode = "ask_every_time" | "read_only" | "trusted_vault";

export interface VaultChangePort {
    read(path: string): Promise<string | undefined>;
    write(path: string, content: string): Promise<void>;
    remove(path: string): Promise<void>;
}

export interface VaultCheckpointStore {
    create(batchId: string, existingPaths: readonly string[]): Promise<string>;
    read(checkpointRef: string, path: string): Promise<string | undefined>;
}

export interface VaultChangeJournalStore {
    load(batchId: string): Promise<VaultChangeJournalRecord | undefined>;
    save(record: VaultChangeJournalRecord): Promise<void>;
    listUnresolved(): Promise<VaultChangeJournalRecord[]>;
}

export interface VaultChangeAuthorizationProposal {
    readonly batchId: string;
    readonly task: string;
    readonly paths: readonly string[];
    readonly diff: string;
    readonly controlFiles: boolean;
    readonly memoryDelete: boolean;
}

export interface VaultChangeCoordinatorOptions {
    readonly vault: VaultChangePort;
    readonly checkpoints: VaultCheckpointStore;
    readonly journal: VaultChangeJournalStore;
    readonly permissionMode: () => VaultPermissionMode;
    readonly authorize?: (proposal: VaultChangeAuthorizationProposal) => Promise<boolean>;
    readonly injectCrash?: (point: VaultChangeCrashPoint, path?: string) => void;
}

export type VaultChangeCrashPoint =
    | "after-prepared-journal"
    | "after-checkpoint"
    | "after-applying-journal"
    | "after-target-write"
    | "after-target-journal"
    | "after-applied-journal";

type Operation =
    | { readonly op: "create"; readonly path: string; readonly content: string; readonly expectedContentHash: "absent" }
    | { readonly op: "append"; readonly path: string; readonly content: string; readonly expectedContentHash: string }
    | {
        readonly op: "replace";
        readonly path: string;
        readonly find: string;
        readonly replacement: string;
        readonly expectedContentHash: string;
    }
    | {
        readonly op: "patch";
        readonly path: string;
        readonly edits: readonly { readonly startLine: number; readonly endLine: number; readonly replacement: string }[];
        readonly expectedContentHash: string;
    }
    | { readonly op: "delete"; readonly path: string; readonly expectedContentHash: string };

interface PreparedTarget {
    readonly operation: Operation["op"];
    readonly path: string;
    readonly beforeHash: string;
    readonly afterHash: string;
    readonly beforeContent: string | undefined;
    readonly afterContent: string | undefined;
}

interface PreparedBatch {
    readonly batchId: string;
    readonly task: string;
    readonly targets: readonly PreparedTarget[];
    readonly diff: string;
    readonly controlFiles: boolean;
    readonly memoryDelete: boolean;
}

export interface VaultChangeJournalTarget {
    readonly operation: Operation["op"];
    readonly path: string;
    readonly beforeHash: string;
    readonly afterHash: string;
}

export type VaultChangeJournalState =
    | "prepared"
    | "applying"
    | "applied"
    | "rolled_back"
    | "undone"
    | "recovery_failed";

export interface VaultChangeJournalRecord {
    readonly version: 1;
    readonly batchId: string;
    readonly toolCallId: string;
    readonly workspaceId: string;
    readonly runId: string;
    readonly argsHash: string;
    readonly idempotencyKey: string;
    readonly state: VaultChangeJournalState;
    readonly checkpointRef: string | null;
    readonly targets: readonly VaultChangeJournalTarget[];
    readonly appliedPaths: readonly string[];
    readonly manualReviewPaths: readonly string[];
}

export interface VaultChangeReconciliation {
    readonly batchId: string;
    readonly state: "applied" | "rolled_back" | "recovery_failed";
    readonly manualReviewPaths: readonly string[];
}

export type VaultUndoResult =
    | { readonly status: "undone"; readonly batchId: string; readonly paths: readonly string[] }
    | { readonly status: "conflict"; readonly batchId: string; readonly paths: readonly string[]; readonly diff: string }
    | { readonly status: "not_found"; readonly batchId: string };

export class VaultChangeCrashInjectionError extends Error {
    constructor(point: string) {
        super(`Injected Vault Change crash after '${point}'.`);
        this.name = "VaultChangeCrashInjectionError";
    }
}

class ChangeValidationError extends Error {
    constructor(readonly code: ErrorCode, message: string) {
        super(message);
        this.name = "ChangeValidationError";
    }
}

export class VaultChangeCoordinator {
    private recoveryGate: Promise<void> | null = null;

    constructor(private readonly options: VaultChangeCoordinatorOptions) {}

    beginRecovery(): Promise<void> {
        this.recoveryGate = this.reconcile().then((reports) => {
            const manual = reports.flatMap((report) => report.state === "recovery_failed" ? report.manualReviewPaths : []);
            if (manual.length > 0) throw new Error(`Vault Change recovery requires manual review: ${manual.join(", ")}`);
        });
        return this.recoveryGate;
    }

    async execute(call: ExecutableToolCallDescriptor): Promise<ToolResultDescriptor> {
        try {
            await this.recoveryGate;
        } catch {
            return failed(
                call,
                "tool.unknown_outcome",
                "Vault Change recovery requires manual review before another write can run.",
                false,
                "unknown_outcome",
            );
        }
        if (call.name !== "vault.changes.apply" || call.version !== "1" || call.executorLocation !== "plugin") {
            throw new Error(`unsupported Vault Change Tool: ${call.name}@${call.version}`);
        }
        const batchId = typeof call.arguments.batchId === "string" ? call.arguments.batchId : "";
        if (!BATCH_ID.test(batchId)) return failed(call, "protocol.invalid_params", "Vault Change Batch ID is invalid.");
        const prior = await this.options.journal.load(batchId);
        if (prior !== undefined) return this.replay(call, prior);
        if (this.options.permissionMode() === "read_only") {
            return failed(call, "policy.denied", "The plugin-owned Vault permission mode is read-only.", false, "denied");
        }

        let prepared: PreparedBatch;
        try {
            prepared = await this.prepare(call.arguments);
        } catch (error) {
            return validationFailure(call, error);
        }
        const confirmationRequired = this.options.permissionMode() === "ask_every_time" ||
            prepared.controlFiles || prepared.memoryDelete;
        if (confirmationRequired) {
            const accepted = this.options.authorize === undefined ? false : await this.options.authorize({
                batchId: prepared.batchId,
                task: prepared.task,
                paths: prepared.targets.map((target) => target.path),
                diff: prepared.diff,
                controlFiles: prepared.controlFiles,
                memoryDelete: prepared.memoryDelete,
            });
            if (!accepted) return failed(call, "policy.denied", "Vault Change Batch was not approved by the plugin.", false, "denied");
            try {
                await this.revalidate(prepared);
            } catch (error) {
                return validationFailure(call, error);
            }
        }

        let record: VaultChangeJournalRecord = {
            version: 1,
            batchId,
            toolCallId: call.toolCallId,
            workspaceId: call.workspaceId,
            runId: call.runId,
            argsHash: call.argsHash,
            idempotencyKey: call.idempotencyKey,
            state: "prepared",
            checkpointRef: null,
            targets: prepared.targets.map(({ operation, path, beforeHash, afterHash }) => ({
                operation, path, beforeHash, afterHash,
            })),
            appliedPaths: [],
            manualReviewPaths: [],
        };
        await this.options.journal.save(record);
        this.inject("after-prepared-journal");
        try {
            const checkpointRef = await this.options.checkpoints.create(
                batchId,
                prepared.targets.filter((target) => target.beforeContent !== undefined).map((target) => target.path),
            );
            record = { ...record, checkpointRef };
            await this.options.journal.save(record);
            this.inject("after-checkpoint");
            record = { ...record, state: "applying" };
            await this.options.journal.save(record);
            this.inject("after-applying-journal");
            for (const target of prepared.targets) {
                if (target.afterContent === undefined) await this.options.vault.remove(target.path);
                else await this.options.vault.write(target.path, target.afterContent);
                this.inject("after-target-write", target.path);
                record = { ...record, appliedPaths: [...record.appliedPaths, target.path] };
                await this.options.journal.save(record);
                this.inject("after-target-journal", target.path);
            }
            record = { ...record, state: "applied" };
            await this.options.journal.save(record);
            this.inject("after-applied-journal");
            return success(call, record);
        } catch (error) {
            if (error instanceof VaultChangeCrashInjectionError) throw error;
            try {
                await this.rollback(record);
                record = { ...record, state: "rolled_back", appliedPaths: [] };
                await this.options.journal.save(record);
                return failed(call, "tool.failed", "Vault Change Batch failed and was rolled back.");
            } catch {
                const manualReviewPaths = await this.unexpectedPaths(record);
                record = { ...record, state: "recovery_failed", manualReviewPaths };
                await this.options.journal.save(record);
                return failed(
                    call,
                    "tool.unknown_outcome",
                    "Vault Change Batch requires manual recovery review.",
                    false,
                    "unknown_outcome",
                );
            }
        }
    }

    async reconcile(): Promise<VaultChangeReconciliation[]> {
        const reports: VaultChangeReconciliation[] = [];
        for (let record of await this.options.journal.listUnresolved()) {
            const states = await this.observedTargetStates(record);
            const allAfter = states.every((state) => state === "after");
            const allKnown = states.every((state) => state === "before" || state === "after");
            if (allAfter) {
                record = { ...record, state: "applied", appliedPaths: record.targets.map((target) => target.path) };
                await this.options.journal.save(record);
                reports.push({ batchId: record.batchId, state: "applied", manualReviewPaths: [] });
                continue;
            }
            if (allKnown) {
                try {
                    await this.rollback(record);
                    record = { ...record, state: "rolled_back", appliedPaths: [], manualReviewPaths: [] };
                    await this.options.journal.save(record);
                    reports.push({ batchId: record.batchId, state: "rolled_back", manualReviewPaths: [] });
                    continue;
                } catch {
                    // Fall through to a bounded manual-review report.
                }
            }
            const manualReviewPaths = record.targets
                .filter((_target, index) => states[index] === "unexpected")
                .map((target) => target.path);
            record = { ...record, state: "recovery_failed", manualReviewPaths };
            await this.options.journal.save(record);
            reports.push({ batchId: record.batchId, state: "recovery_failed", manualReviewPaths });
        }
        return reports;
    }

    async undo(batchId: string): Promise<VaultUndoResult> {
        const record = await this.options.journal.load(batchId);
        if (record === undefined || record.state !== "applied" || record.checkpointRef === null) {
            return { status: "not_found", batchId };
        }
        const conflicts: string[] = [];
        const diffs: string[] = [];
        for (const target of record.targets) {
            const current = await this.options.vault.read(target.path);
            if (contentIdentity(current) !== target.afterHash) {
                conflicts.push(target.path);
                const before = await this.options.checkpoints.read(record.checkpointRef, target.path);
                diffs.push(conflictDiff(target.path, current, before));
            }
        }
        if (conflicts.length > 0) {
            return { status: "conflict", batchId, paths: conflicts, diff: truncateUtf8(diffs.join("\n"), MAX_DIFF_BYTES) };
        }
        await this.rollback(record, true);
        await this.options.journal.save({ ...record, state: "undone", appliedPaths: [] });
        return { status: "undone", batchId, paths: record.targets.map((target) => target.path) };
    }

    private async prepare(input: Readonly<Record<string, unknown>>): Promise<PreparedBatch> {
        if (hasExtraKeys(input, ["batchId", "task", "operations"])) invalid("Vault Change Batch fields are invalid.");
        const batchId = typeof input.batchId === "string" && BATCH_ID.test(input.batchId) ? input.batchId : undefined;
        const task = typeof input.task === "string" && input.task.trim() ? input.task.trim() : undefined;
        if (!batchId || !task || Buffer.byteLength(task, "utf8") > MAX_TASK_BYTES ||
            !Array.isArray(input.operations) || input.operations.length < 1 || input.operations.length > MAX_ACTIONS) {
            invalid("Vault Change Batch identity, task, or operation count is invalid.");
        }
        const operations = input.operations.map(parseOperation);
        const paths = operations.map((operation) => operation.path);
        if (new Set(paths.map((path) => path.toLocaleLowerCase())).size !== paths.length) {
            invalid("Each Vault Change Batch path may appear only once.");
        }
        const deletes = operations.filter((operation) => operation.op === "delete");
        if (deletes.some((operation) => !isPlanningMemoryTopic(operation.path)) ||
            (deletes.length > 0 && !operations.some((operation) => operation.path === "memory/MEMORY.md" && operation.op !== "delete"))) {
            invalid("Delete is restricted to Planning Memory topics and must synchronize memory/MEMORY.md.");
        }
        const targets: PreparedTarget[] = [];
        let batchBytes = 0;
        for (const operation of operations) {
            const beforeContent = await this.options.vault.read(operation.path);
            if (beforeContent !== undefined && Buffer.byteLength(beforeContent, "utf8") > MAX_FILE_BYTES) {
                throw new ChangeValidationError("protocol.message_too_large", "Vault target exceeds the file limit.");
            }
            const beforeHash = contentIdentity(beforeContent);
            if (operation.expectedContentHash !== beforeHash) {
                throw new ChangeValidationError("resource.conflict", `Vault target '${operation.path}' changed before apply.`);
            }
            const afterContent = applyOperation(operation, beforeContent);
            if (afterContent !== undefined && Buffer.byteLength(afterContent, "utf8") > MAX_FILE_BYTES) {
                throw new ChangeValidationError("protocol.message_too_large", "Vault result exceeds the file limit.");
            }
            if (contentIdentity(afterContent) === beforeHash) {
                invalid(`Vault Change operation for '${operation.path}' has no effect.`);
            }
            batchBytes += Buffer.byteLength(afterContent ?? "", "utf8");
            if (batchBytes > MAX_BATCH_BYTES) {
                throw new ChangeValidationError("protocol.message_too_large", "Vault Change Batch exceeds its byte limit.");
            }
            targets.push({
                operation: operation.op,
                path: operation.path,
                beforeHash,
                afterHash: contentIdentity(afterContent),
                beforeContent,
                afterContent,
            });
        }
        validatePlanningMemoryDeletes(targets);
        const diff = batchDiff(targets);
        return {
            batchId,
            task,
            targets,
            diff,
            controlFiles: paths.some(isControlPath),
            memoryDelete: deletes.length > 0,
        };
    }

    private async revalidate(prepared: PreparedBatch): Promise<void> {
        for (const target of prepared.targets) {
            if (contentIdentity(await this.options.vault.read(target.path)) !== target.beforeHash) {
                throw new ChangeValidationError("resource.conflict", `Vault target '${target.path}' changed during approval.`);
            }
        }
    }

    private replay(call: ExecutableToolCallDescriptor, record: VaultChangeJournalRecord): ToolResultDescriptor {
        const binding = [record.toolCallId, record.workspaceId, record.runId, record.argsHash, record.idempotencyKey];
        const expected = [call.toolCallId, call.workspaceId, call.runId, call.argsHash, call.idempotencyKey];
        if (JSON.stringify(binding) !== JSON.stringify(expected)) {
            return failed(call, "resource.conflict", "Vault Change Batch identity conflicts with a durable record.");
        }
        if (record.state === "applied") return success(call, record);
        if (record.state === "prepared" || record.state === "applying" || record.state === "recovery_failed") {
            return failed(
                call,
                "tool.unknown_outcome",
                "Vault Change Batch has an unresolved durable outcome.",
                false,
                "unknown_outcome",
            );
        }
        return failed(call, "resource.conflict", `Vault Change Batch is already ${record.state}.`);
    }

    private async rollback(record: VaultChangeJournalRecord, undo = false): Promise<void> {
        if (record.checkpointRef === null) {
            const states = await this.observedTargetStates(record);
            if (states.some((state) => state !== "before")) throw new Error("checkpoint missing after mutation");
            return;
        }
        for (const target of [...record.targets].reverse()) {
            const current = await this.options.vault.read(target.path);
            const identity = contentIdentity(current);
            if (identity === target.beforeHash) continue;
            if (identity !== target.afterHash) throw new Error(`unexpected target state: ${target.path}`);
            const before = await this.options.checkpoints.read(record.checkpointRef, target.path);
            if (target.beforeHash === "absent") await this.options.vault.remove(target.path);
            else {
                if (before === undefined || contentIdentity(before) !== target.beforeHash) {
                    throw new Error(`checkpoint mismatch: ${target.path}`);
                }
                await this.options.vault.write(target.path, before);
            }
            if (!undo && contentIdentity(await this.options.vault.read(target.path)) !== target.beforeHash) {
                throw new Error(`rollback verification failed: ${target.path}`);
            }
        }
    }

    private async observedTargetStates(record: VaultChangeJournalRecord): Promise<("before" | "after" | "unexpected")[]> {
        return Promise.all(record.targets.map(async (target) => {
            const identity = contentIdentity(await this.options.vault.read(target.path));
            return identity === target.beforeHash ? "before" : identity === target.afterHash ? "after" : "unexpected";
        }));
    }

    private async unexpectedPaths(record: VaultChangeJournalRecord): Promise<string[]> {
        const states = await this.observedTargetStates(record);
        return record.targets.filter((_target, index) => states[index] === "unexpected").map((target) => target.path);
    }

    private inject(point: VaultChangeCrashPoint, path?: string): void {
        this.options.injectCrash?.(point, path);
    }
}

function parseOperation(value: unknown): Operation {
    if (!isRecord(value)) invalid("Vault Change operation must be an object.");
    const path = safeVaultPath(value.path);
    const op = value.op;
    if (!path || !["create", "append", "replace", "patch", "delete"].includes(String(op))) {
        invalid("Vault Change operation or path is invalid.");
    }
    const expected = value.expectedContentHash;
    if (op === "create") {
        if (expected !== "absent" || !boundedContent(value.content) || hasExtraKeys(value, ["op", "path", "content", "expectedContentHash"])) {
            invalid("Vault create operation is invalid.");
        }
        return { op, path, content: value.content, expectedContentHash: expected };
    }
    if (typeof expected !== "string" || !DIGEST.test(expected)) invalid("Vault expected content hash is invalid.");
    if (op === "append") {
        if (!boundedContent(value.content) || hasExtraKeys(value, ["op", "path", "content", "expectedContentHash"])) {
            invalid("Vault append operation is invalid.");
        }
        return { op, path, content: value.content, expectedContentHash: expected };
    }
    if (op === "replace") {
        if (typeof value.find !== "string" || !value.find || !boundedContent(value.replacement) ||
            Buffer.byteLength(value.find, "utf8") > MAX_FILE_BYTES ||
            hasExtraKeys(value, ["op", "path", "find", "replacement", "expectedContentHash"])) {
            invalid("Vault exact replace operation is invalid.");
        }
        return { op, path, find: value.find, replacement: value.replacement, expectedContentHash: expected };
    }
    if (op === "patch") {
        if (!Array.isArray(value.edits) || value.edits.length < 1 || value.edits.length > 256 ||
            hasExtraKeys(value, ["op", "path", "edits", "expectedContentHash"])) invalid("Vault patch operation is invalid.");
        const edits = value.edits.map((edit) => {
            if (!isRecord(edit) || !positiveInteger(edit.startLine) || !positiveInteger(edit.endLine) ||
                Number(edit.endLine) < Number(edit.startLine) || !boundedContent(edit.replacement) ||
                hasExtraKeys(edit, ["startLine", "endLine", "replacement"])) invalid("Vault patch edit is invalid.");
            return { startLine: Number(edit.startLine), endLine: Number(edit.endLine), replacement: edit.replacement };
        });
        return { op, path, edits, expectedContentHash: expected };
    }
    if (hasExtraKeys(value, ["op", "path", "expectedContentHash"])) invalid("Vault delete operation is invalid.");
    return { op: "delete", path, expectedContentHash: expected };
}

function applyOperation(operation: Operation, before: string | undefined): string | undefined {
    if (operation.op === "create") {
        if (before !== undefined) throw new ChangeValidationError("resource.conflict", "Vault create target already exists.");
        return operation.content;
    }
    if (before === undefined) throw new ChangeValidationError("resource.not_found", "Vault Change target is missing.");
    if (operation.op === "append") return before + operation.content;
    if (operation.op === "replace") {
        if (before.split(operation.find).length - 1 !== 1) {
            invalid("Vault exact replace text must occur exactly once.");
        }
        return before.replace(operation.find, operation.replacement);
    }
    if (operation.op === "delete") return undefined;
    const lines = before.replace(/\r\n/g, "\n").split("\n");
    const ordered = [...operation.edits].sort((left, right) => left.startLine - right.startLine);
    for (let index = 0; index < ordered.length; index += 1) {
        const edit = ordered[index];
        if (edit.endLine > lines.length || (index > 0 && edit.startLine <= ordered[index - 1].endLine)) {
            invalid("Vault patch edits overlap or exceed the current file.");
        }
    }
    for (const edit of [...ordered].reverse()) {
        lines.splice(edit.startLine - 1, edit.endLine - edit.startLine + 1, ...edit.replacement.split("\n"));
    }
    return lines.join("\n");
}

function safeVaultPath(value: unknown): string | undefined {
    if (typeof value !== "string") return undefined;
    const path = value.trim();
    if (!path || path.length > MAX_PATH_LENGTH || path.includes("\\") || path.includes(":") || path.includes("\0") || path.startsWith("/")) {
        return undefined;
    }
    const segments = path.split("/");
    if (segments.some((segment) => !segment || segment === "." || segment === ".." || segment.toLocaleLowerCase() === ".git" ||
        segment.toLocaleLowerCase() === "node_modules")) return undefined;
    const lower = path.toLocaleLowerCase();
    if (lower.startsWith(".obsidian/plugins/offeragent")) return undefined;
    if (segments.some((segment) => segment.startsWith(".")) &&
        !lower.startsWith(".codex/") && !lower.startsWith(".obsidian/")) return undefined;
    const extension = lower.slice(lower.lastIndexOf("."));
    return [".md", ".txt"].includes(extension) ||
        (lower.startsWith(".obsidian/") && [".json", ".css"].includes(extension)) ? path : undefined;
}

function isControlPath(path: string): boolean {
    const lower = path.toLocaleLowerCase();
    return lower === "agent.md" || lower.startsWith(".codex/") || lower.startsWith(".obsidian/");
}

function isPlanningMemoryTopic(path: string): boolean {
    return /^memory\/(?:user|feedback|project|study)\/[^/.][^/]*\.md$/u.test(path);
}

function validatePlanningMemoryDeletes(targets: readonly PreparedTarget[]): void {
    const deletions = targets.filter((target) => target.operation === "delete");
    if (deletions.length === 0) return;
    const index = targets.find((target) => target.path === "memory/MEMORY.md");
    if (index?.beforeContent === undefined || index.afterContent === undefined) {
        invalid("Planning Memory deletion requires an updated memory/MEMORY.md index.");
    }
    for (const deletion of deletions) {
        if (deletion.beforeContent === undefined || !validMemoryTopicMetadata(deletion.path, deletion.beforeContent)) {
            invalid(`Planning Memory topic '${deletion.path}' has invalid or mismatched metadata.`);
        }
        const destination = deletion.path.slice(0, -3);
        const link = new RegExp(
            `\\[\\[${escapeRegularExpression(destination)}(?:\\.md)?(?:\\|[^\\]\\r\\n]+)?\\]\\]`,
            "iu",
        );
        if (!link.test(index.beforeContent) || link.test(index.afterContent)) {
            invalid(`Planning Memory index relationship for '${deletion.path}' is not removed by the batch.`);
        }
    }
}

function validMemoryTopicMetadata(path: string, content: string): boolean {
    const normalized = content.replace(/\r\n/g, "\n");
    if (!normalized.startsWith("---\n")) return false;
    const closing = normalized.indexOf("\n---\n", 4);
    if (closing < 0) return false;
    const metadata = new Map<string, string>();
    for (const line of normalized.slice(4, closing).split("\n")) {
        const separator = line.indexOf(":");
        if (separator <= 0) continue;
        const key = line.slice(0, separator).trim().toLocaleLowerCase();
        let value = line.slice(separator + 1).trim();
        if ((value.startsWith('"') && value.endsWith('"')) || (value.startsWith("'") && value.endsWith("'"))) {
            value = value.slice(1, -1).trim();
        }
        metadata.set(key, value);
    }
    const expectedType = path.split("/")[1];
    return Boolean(metadata.get("name")) && Boolean(metadata.get("description")) && metadata.get("type") === expectedType;
}

function escapeRegularExpression(value: string): string {
    return value.replace(/[.*+?^${}()|[\]\\]/gu, "\\$&");
}

function boundedContent(value: unknown): value is string {
    return typeof value === "string" && Buffer.byteLength(value, "utf8") <= MAX_FILE_BYTES && !value.includes("\0");
}

function contentIdentity(content: string | undefined): string {
    return content === undefined ? "absent" : digest(content);
}

function digest(content: string | Buffer): string {
    return `sha256:${createHash("sha256").update(content).digest("hex")}`;
}

function batchDiff(targets: readonly PreparedTarget[]): string {
    const sections = targets.map((target) => [
        `--- before/${target.path}`,
        `+++ after/${target.path}`,
        `@@ ${target.operation} @@`,
        ...(target.beforeContent ?? "").split(/\r?\n/u).slice(0, 100).map((line) => `-${line}`),
        ...(target.afterContent ?? "").split(/\r?\n/u).slice(0, 100).map((line) => `+${line}`),
    ].join("\n"));
    return truncateUtf8(sections.join("\n"), MAX_DIFF_BYTES);
}

function conflictDiff(path: string, current: string | undefined, before: string | undefined): string {
    return truncateUtf8([
        `--- current/${path}`,
        `+++ checkpoint/${path}`,
        "@@ guarded undo @@",
        ...(current ?? "").split(/\r?\n/u).slice(0, 100).map((line) => `-${line}`),
        ...(before ?? "").split(/\r?\n/u).slice(0, 100).map((line) => `+${line}`),
    ].join("\n"), MAX_DIFF_BYTES);
}

function stateHash(targets: readonly VaultChangeJournalTarget[], side: "before" | "after"): string {
    return digest([...targets]
        .sort((left, right) => left.path.localeCompare(right.path))
        .map((target) => `${target.path}\0${side === "before" ? target.beforeHash : target.afterHash}`)
        .join("\n"));
}

function success(call: ExecutableToolCallDescriptor, record: VaultChangeJournalRecord): ToolResultDescriptor {
    const paths = record.targets.map((target) => target.path);
    const sideEffects: SideEffect[] = record.targets.map((target) => ({
        kind: target.operation === "create" ? "file_created" : target.operation === "delete" ? "file_trashed" : "file_modified",
        resource: target.path,
        beforeHash: target.beforeHash === "absent" ? null : target.beforeHash,
        afterHash: target.afterHash === "absent" ? null : target.afterHash,
        confirmed: true,
    }));
    return {
        toolCallId: call.toolCallId,
        status: "succeeded",
        summary: `Applied Vault Change Batch '${record.batchId}'.`,
        data: {
            batchId: record.batchId,
            state: "applied",
            checkpointRef: record.checkpointRef ?? "unavailable",
            paths,
            beforeStateHash: stateHash(record.targets, "before"),
            afterStateHash: stateHash(record.targets, "after"),
            undoAvailable: record.checkpointRef !== null,
        },
        sideEffects,
        retryable: false,
    };
}

function validationFailure(call: ExecutableToolCallDescriptor, error: unknown): ToolResultDescriptor {
    return error instanceof ChangeValidationError
        ? failed(call, error.code, error.message, error.code === "resource.conflict")
        : failed(call, "tool.failed", "Vault Change Batch validation failed.");
}

function failed(
    call: ExecutableToolCallDescriptor,
    code: ErrorCode,
    message: string,
    retryable = false,
    status: "failed" | "denied" | "unknown_outcome" = "failed",
): ToolResultDescriptor {
    return {
        toolCallId: call.toolCallId,
        status,
        summary: message,
        data: {},
        retryable,
        error: { code, retryable, cancelled: false, userVisibleMessage: message, details: {} },
    };
}

function invalid(message: string): never {
    throw new ChangeValidationError("protocol.invalid_params", message);
}

function isRecord(value: unknown): value is Record<string, unknown> {
    return value !== null && typeof value === "object" && !Array.isArray(value);
}

function hasExtraKeys(value: Readonly<Record<string, unknown>>, allowed: readonly string[]): boolean {
    return Object.keys(value).some((key) => !allowed.includes(key));
}

function positiveInteger(value: unknown): boolean {
    return Number.isSafeInteger(value) && Number(value) >= 1;
}

function truncateUtf8(value: string, maximum: number): string {
    if (Buffer.byteLength(value, "utf8") <= maximum) return value;
    let end = Math.min(value.length, maximum);
    while (end > 0 && Buffer.byteLength(value.slice(0, end), "utf8") > maximum - 16) end -= 1;
    return `${value.slice(0, end)}\n...truncated`;
}

export class GitCheckpointStore implements VaultCheckpointStore {
    private readonly root: string;

    constructor(vaultRoot: string) {
        this.root = resolve(vaultRoot);
    }

    async create(batchId: string, existingPaths: readonly string[]): Promise<string> {
        if (!BATCH_ID.test(batchId) || existingPaths.some((path) => safeVaultPath(path) !== path)) {
            throw new Error("checkpoint identity or path is invalid");
        }
        const canonicalRoot = await realpath(this.root);
        if (!samePath(canonicalRoot, this.root)) throw new Error("Vault root is not canonical");
        const temporary = await mkdtemp(join(os.tmpdir(), "offeragent-index-"));
        const indexPath = join(temporary, "index");
        const environment = {
            ...process.env,
            GIT_INDEX_FILE: indexPath,
            GIT_AUTHOR_NAME: process.env.GIT_AUTHOR_NAME ?? "OfferAgent",
            GIT_AUTHOR_EMAIL: process.env.GIT_AUTHOR_EMAIL ?? "offeragent@localhost.invalid",
            GIT_COMMITTER_NAME: process.env.GIT_COMMITTER_NAME ?? "OfferAgent",
            GIT_COMMITTER_EMAIL: process.env.GIT_COMMITTER_EMAIL ?? "offeragent@localhost.invalid",
        };
        try {
            await this.git(["read-tree", "--empty"], environment);
            if (existingPaths.length > 0) await this.git(["add", "--", ...existingPaths], environment);
            const tree = (await this.git(["write-tree"], environment)).trim();
            const commit = (await this.git(["commit-tree", tree, "-m", `OfferAgent checkpoint ${batchId}`], environment)).trim();
            const ref = `refs/offeragent/checkpoints/${batchId}`;
            await this.git(["update-ref", ref, commit], environment);
            return ref;
        } finally {
            await rm(temporary, { recursive: true, force: true });
        }
    }

    async read(checkpointRef: string, path: string): Promise<string | undefined> {
        if (!/^refs\/offeragent\/checkpoints\/[A-Za-z0-9][A-Za-z0-9_-]{0,127}$/u.test(checkpointRef) ||
            safeVaultPath(path) !== path) throw new Error("checkpoint reference or path is invalid");
        try {
            return await this.git(["show", `${checkpointRef}:${path}`], process.env);
        } catch {
            return undefined;
        }
    }

    private async git(args: readonly string[], env: NodeJS.ProcessEnv): Promise<string> {
        const result = await execFileAsync("git", ["-C", this.root, ...args], {
            encoding: "utf8",
            env,
            windowsHide: true,
            maxBuffer: 2 * 1_048_576,
        });
        return result.stdout;
    }
}

export class FileVaultChangeJournal implements VaultChangeJournalStore {
    constructor(private readonly directory: string) {}

    async load(batchId: string): Promise<VaultChangeJournalRecord | undefined> {
        if (!BATCH_ID.test(batchId)) throw new Error("journal batch ID is invalid");
        try {
            return parseJournal(JSON.parse(await readFile(join(this.directory, `${batchId}.json`), "utf8")));
        } catch (error) {
            if (isNodeError(error) && error.code === "ENOENT") return undefined;
            throw error;
        }
    }

    async save(record: VaultChangeJournalRecord): Promise<void> {
        const parsed = parseJournal(record);
        await mkdir(this.directory, { recursive: true });
        const target = join(this.directory, `${parsed.batchId}.json`);
        const temporary = `${target}.${process.pid}.${Date.now()}.tmp`;
        const handle = await open(temporary, "wx", 0o600);
        try {
            await handle.writeFile(`${JSON.stringify(parsed)}\n`, "utf8");
            await handle.sync();
        } finally {
            await handle.close();
        }
        try {
            await rename(temporary, target);
        } finally {
            await rm(temporary, { force: true });
        }
    }

    async listUnresolved(): Promise<VaultChangeJournalRecord[]> {
        let names: string[];
        try {
            const { readdir } = await import("node:fs/promises");
            names = await readdir(this.directory);
        } catch (error) {
            if (isNodeError(error) && error.code === "ENOENT") return [];
            throw error;
        }
        const records: VaultChangeJournalRecord[] = [];
        for (const name of names.sort()) {
            if (!/^[A-Za-z0-9][A-Za-z0-9_-]{0,127}\.json$/u.test(name)) continue;
            const record = await this.load(name.slice(0, -5));
            if (record !== undefined && ["prepared", "applying", "recovery_failed"].includes(record.state)) records.push(record);
        }
        return records;
    }
}

function parseJournal(value: unknown): VaultChangeJournalRecord {
    const malformed = (): never => { throw new Error("Vault Change journal record is malformed"); };
    if (!isRecord(value)) malformed();
    const record = value as Record<string, unknown>;
    if (hasExtraKeys(record, [
        "version", "batchId", "toolCallId", "workspaceId", "runId", "argsHash", "idempotencyKey", "state",
        "checkpointRef", "targets", "appliedPaths", "manualReviewPaths",
    ]) || record.version !== 1 || typeof record.batchId !== "string" || !BATCH_ID.test(record.batchId) ||
        !boundedIdentifier(record.toolCallId) || !boundedIdentifier(record.workspaceId) || !boundedIdentifier(record.runId) ||
        typeof record.argsHash !== "string" || !DIGEST.test(record.argsHash) || !boundedIdentifier(record.idempotencyKey) ||
        !["prepared", "applying", "applied", "rolled_back", "undone", "recovery_failed"].includes(String(record.state)) ||
        (record.checkpointRef !== null && (typeof record.checkpointRef !== "string" ||
            !/^refs\/offeragent\/checkpoints\/[A-Za-z0-9][A-Za-z0-9_-]{0,127}$/u.test(record.checkpointRef))) ||
        !Array.isArray(record.targets) || record.targets.length < 1 || record.targets.length > MAX_ACTIONS ||
        !Array.isArray(record.appliedPaths) || !Array.isArray(record.manualReviewPaths)) malformed();
    const targets = record.targets as unknown[];
    const targetPaths = new Set<string>();
    for (const candidate of targets) {
        if (!isRecord(candidate)) malformed();
        const target = candidate as Record<string, unknown>;
        if (hasExtraKeys(target, ["operation", "path", "beforeHash", "afterHash"]) ||
            !["create", "append", "replace", "patch", "delete"].includes(String(target.operation)) ||
            typeof target.path !== "string" || safeVaultPath(target.path) !== target.path ||
            typeof target.beforeHash !== "string" || typeof target.afterHash !== "string" ||
            !validStateIdentity(target.beforeHash) || !validStateIdentity(target.afterHash)) malformed();
        const path = target.path as string;
        const beforeHash = target.beforeHash as string;
        const afterHash = target.afterHash as string;
        const operation = String(target.operation);
        if ((operation === "create" && (beforeHash !== "absent" || afterHash === "absent")) ||
            (operation === "delete" && (beforeHash === "absent" || afterHash !== "absent")) ||
            (!["create", "delete"].includes(operation) &&
                (beforeHash === "absent" || afterHash === "absent"))) malformed();
        const folded = path.toLocaleLowerCase();
        if (targetPaths.has(folded)) malformed();
        targetPaths.add(folded);
    }
    const validJournalPaths = (paths: unknown[]): boolean => {
        const unique = new Set<string>();
        return paths.every((candidate) => {
            if (typeof candidate !== "string" || safeVaultPath(candidate) !== candidate) return false;
            const folded = candidate.toLocaleLowerCase();
            if (!targetPaths.has(folded) || unique.has(folded)) return false;
            unique.add(folded);
            return true;
        });
    };
    if (!validJournalPaths(record.appliedPaths as unknown[]) || !validJournalPaths(record.manualReviewPaths as unknown[]) ||
        (["applying", "applied", "undone"].includes(String(record.state)) && record.checkpointRef === null)) malformed();
    return record as unknown as VaultChangeJournalRecord;
}

function boundedIdentifier(value: unknown): value is string {
    return typeof value === "string" && value.length > 0 && value.length <= 256 && !value.includes("\0");
}

function validStateIdentity(value: string): boolean {
    return value === "absent" || DIGEST.test(value);
}

export class ObsidianVaultChangePort implements VaultChangePort {
    constructor(private readonly vault: Pick<
        Vault,
        "cachedRead" | "create" | "createFolder" | "delete" | "getAbstractFileByPath" | "getFileByPath" | "modify"
    >) {}

    async read(path: string): Promise<string | undefined> {
        const file = this.vault.getFileByPath(path);
        return file === null ? undefined : this.vault.cachedRead(file);
    }

    async write(path: string, content: string): Promise<void> {
        const file = this.vault.getFileByPath(path);
        if (file !== null) {
            await this.vault.modify(file, content);
            return;
        }
        const segments = path.split("/").slice(0, -1);
        let current = "";
        for (const segment of segments) {
            current = current ? `${current}/${segment}` : segment;
            if (this.vault.getAbstractFileByPath(current) === null) await this.vault.createFolder(current);
        }
        await this.vault.create(path, content);
    }

    async remove(path: string): Promise<void> {
        const file = this.vault.getFileByPath(path);
        if (file !== null) await this.vault.delete(file, true);
    }
}

function samePath(left: string, right: string): boolean {
    return process.platform === "win32" ? left.toLocaleLowerCase() === right.toLocaleLowerCase() : left === right;
}

function isNodeError(value: unknown): value is NodeJS.ErrnoException {
    return value instanceof Error && "code" in value;
}

export function assertContainedStateDirectory(vaultRoot: string, stateDirectory: string): string {
    const root = resolve(vaultRoot);
    const target = resolve(stateDirectory);
    const nested = relative(root, target);
    if (!nested || nested === ".." || nested.startsWith(`..${sep}`) || isAbsolute(nested)) {
        throw new Error("plugin journal directory escapes the Vault");
    }
    return target;
}
