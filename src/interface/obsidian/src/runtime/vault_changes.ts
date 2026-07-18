import { Buffer } from "node:buffer";
import { execFile } from "node:child_process";
import { createHash } from "node:crypto";
import { mkdir, mkdtemp, open, readFile, readdir, realpath, rename, rm } from "node:fs/promises";
import { isIP } from "node:net";
import { dirname, isAbsolute, join, relative, resolve, sep } from "node:path";
import { isDeepStrictEqual, promisify } from "node:util";
import * as os from "node:os";

import type { TFile, Vault } from "obsidian";

import type {
    ErrorCode,
    ExecutableToolCallDescriptor,
    SideEffect,
    ToolResultDescriptor,
} from "./generated_protocol";
import { failed, hasExtraKeys } from "./plugin_tool_results";

const execFileAsync = promisify(execFile);
const MAX_ACTIONS = 20;
const MAX_BATCH_BYTES = 131_072;
const MAX_FILE_BYTES = 262_144;
const MAX_DIFF_BYTES = 32_768;
const MAX_PATH_LENGTH = 512;
const MAX_TASK_BYTES = 512;
const BATCH_ID = /^[A-Za-z0-9][A-Za-z0-9_-]{0,127}$/u;
const DIGEST = /^sha256:[0-9a-f]{64}$/u;
const MODIFIED_VERSION = /^[^\0\r\n]{1,128}$/u;
const EXPERIENCE_PATH = /^experiences\/(?!index\.md$)[^/.][^/]*\.md$/u;
const QUESTION_PATH = /^interview\/(?!index\.md$)[^/.][^/]*\.md$/u;
const EXPERIENCE_INDEX_PATH = "experiences/index.md";
const QUESTION_INDEX_PATH = "interview/index.md";
const LEGACY_EXPERIENCE_PATH = /^interviews\/experiences\/[^/.][^/]*\.md$/u;
const LEGACY_QUESTION_PATH = /^interviews\/questions\/[^/.][^/]*\.md$/u;
const LEGACY_INTERVIEW_INDEX_PATH = /^interviews\/(?:INDEX|index)\.md$/u;
const PERSONAL_IDENTITY_FRONTMATTER =
    /(?:candidate|name|account|handle|username|avatar|contact|email|phone|mobile|telephone|social|linkedin|github|wechat|qq)/u;

export type VaultPermissionMode = "ask_every_time" | "read_only" | "trusted_vault";
export type VaultChangeKind = "general" | "interview_submission";

export interface VaultChangeSnapshot {
    readonly content: string | undefined;
    readonly modifiedVersion: string;
}

export interface VaultIdentity {
    readonly contentHash: string;
    readonly modifiedVersion: string;
}

export type ConditionalVaultMutation =
    | {
        readonly kind: "create";
        readonly path: string;
        readonly expected: VaultIdentity & {
            readonly contentHash: "absent";
            readonly modifiedVersion: "missing";
        };
        readonly afterContent: string;
    }
    | {
        readonly kind: "modify";
        readonly path: string;
        readonly expected: VaultIdentity;
        readonly afterContent: string;
    }
    | {
        readonly kind: "delete";
        readonly path: string;
        readonly expected: VaultIdentity;
    };

export type ConditionalMutationOutcome =
    | { readonly status: "applied" }
    | { readonly status: "conflict"; readonly observed: VaultIdentity }
    | { readonly status: "unknown"; readonly observed: VaultIdentity | null }
    | { readonly status: "unsupported"; readonly operation: "delete" };

export interface VaultChangePort {
    read(path: string): Promise<string | undefined>;
    snapshot(path: string): Promise<VaultChangeSnapshot>;
    applyConditional(mutation: ConditionalVaultMutation): Promise<ConditionalMutationOutcome>;
    restore(path: string, content: string | undefined): Promise<void>;
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
    readonly argsHash: string;
    readonly changeKind: VaultChangeKind;
    readonly task: string;
    readonly paths: readonly string[];
    readonly categorizedTargets: readonly VaultChangeCategorizedTarget[];
    readonly sourceBindings: readonly VaultChangeSourceBinding[];
    readonly diff: string;
    readonly controlFiles: boolean;
    readonly memoryDelete: boolean;
}

export interface VaultChangeCategorizedTarget {
    readonly path: string;
    readonly category: "experience" | "question" | "index" | "other";
    readonly operation: Operation["op"];
    readonly expectedModifiedVersion: string;
    readonly expectedContentHash: string;
}

export interface VaultChangeSourceBinding {
    readonly path: string;
    readonly expectedModifiedVersion: string;
    readonly expectedContentHash: string;
}

interface InterviewSubmissionMetadata {
    readonly sourceKind: "text" | "public_url" | "ordered_images" | "mixed";
    readonly capturedOn: string;
    readonly canonicalUrls: readonly string[];
    readonly orderedImageContentHashes: readonly string[];
    readonly sourceFingerprint: string | null;
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
    | "after-applied-journal"
    | "after-undoing-journal"
    | "after-undo-target-write"
    | "after-undo-target-journal";

type Operation =
    | {
        readonly op: "create";
        readonly path: string;
        readonly content: string;
        readonly expectedContentHash: "absent";
        readonly expectedModifiedVersion: string;
    }
    | {
        readonly op: "append";
        readonly path: string;
        readonly content: string;
        readonly expectedContentHash: string;
        readonly expectedModifiedVersion: string;
    }
    | {
        readonly op: "replace";
        readonly path: string;
        readonly find: string;
        readonly replacement: string;
        readonly expectedContentHash: string;
        readonly expectedModifiedVersion: string;
    }
    | {
        readonly op: "patch";
        readonly path: string;
        readonly edits: readonly { readonly startLine: number; readonly endLine: number; readonly replacement: string }[];
        readonly expectedContentHash: string;
        readonly expectedModifiedVersion: string;
    }
    | {
        readonly op: "delete";
        readonly path: string;
        readonly expectedContentHash: string;
        readonly expectedModifiedVersion: string;
    };

interface PreparedTarget {
    readonly operation: Operation["op"];
    readonly path: string;
    readonly beforeHash: string;
    readonly afterHash: string;
    readonly beforeContent: string | undefined;
    readonly afterContent: string | undefined;
    readonly beforeModifiedVersion: string;
    readonly expectedModifiedVersion: string;
}

interface PreparedBatch {
    readonly batchId: string;
    readonly changeKind: VaultChangeKind;
    readonly task: string;
    readonly targets: readonly PreparedTarget[];
    readonly categorizedTargets: readonly VaultChangeCategorizedTarget[];
    readonly sourceBindings: readonly VaultChangeSourceBinding[];
    readonly interviewSubmission?: InterviewSubmissionMetadata;
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
    | "undoing"
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
    readonly state: "applied" | "rolled_back" | "undone" | "recovery_failed";
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

class AmbiguousWriteOutcomeError extends Error {
    constructor(readonly path: string) {
        super(`Vault write ownership is ambiguous for '${path}'.`);
        this.name = "AmbiguousWriteOutcomeError";
    }
}

class UndoConflictError extends Error {
    constructor(readonly paths: readonly string[]) {
        super(`Vault targets changed during guarded undo: ${paths.join(", ")}`);
        this.name = "UndoConflictError";
    }
}

export class VaultChangeCoordinator {
    private recoveryGate: Promise<void> | null = null;
    private writesLatched = false;

    constructor(private readonly options: VaultChangeCoordinatorOptions) {}

    beginRecovery(): Promise<void> {
        this.writesLatched = true;
        this.recoveryGate = this.reconcile()
            .then((reports) => {
                const blocked = reports.filter((report) => report.state === "recovery_failed");
                if (blocked.length > 0) {
                    const details = blocked.flatMap((report) => report.manualReviewPaths).join(", ") ||
                        blocked.map((report) => report.batchId).join(", ");
                    throw new Error(`Vault Change recovery requires manual review: ${details}`);
                }
                this.writesLatched = false;
            })
            .catch((error: unknown) => {
                this.writesLatched = true;
                throw error;
            });
        return this.recoveryGate;
    }

    async execute(call: ExecutableToolCallDescriptor): Promise<ToolResultDescriptor> {
        if (!await this.writeGateOpen()) {
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
        if (prior !== undefined) {
            const replay = this.replay(call, prior);
            if (replay.status === "unknown_outcome") this.writesLatched = true;
            return replay;
        }
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
            prepared.changeKind === "interview_submission" || prepared.controlFiles || prepared.memoryDelete;
        if (confirmationRequired) {
            const accepted = this.options.authorize === undefined ? false : await this.options.authorize({
                batchId: prepared.batchId,
                argsHash: call.argsHash,
                changeKind: prepared.changeKind,
                task: prepared.task,
                paths: prepared.targets.map((target) => target.path),
                categorizedTargets: prepared.categorizedTargets,
                sourceBindings: prepared.sourceBindings,
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
            try {
                await this.revalidate(prepared);
            } catch (error) {
                record = { ...record, state: "rolled_back" };
                await this.options.journal.save(record);
                return validationFailure(call, error);
            }
            record = { ...record, state: "applying" };
            await this.options.journal.save(record);
            this.inject("after-applying-journal");
            for (const target of prepared.targets) {
                await this.revalidateSources(prepared, new Set(record.appliedPaths.map((path) => path.toLocaleLowerCase())));
                await this.revalidateTarget(target);
                await this.applyConditional(target);
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
            if (error instanceof VaultChangeCrashInjectionError) {
                this.writesLatched = true;
                throw error;
            }
            if (error instanceof AmbiguousWriteOutcomeError) {
                let manualReviewPaths = [error.path];
                try {
                    await this.rollback(record, true);
                    record = { ...record, appliedPaths: [] };
                } catch {
                    manualReviewPaths = [...new Set([
                        error.path,
                        ...record.appliedPaths,
                        ...await this.unexpectedPaths(record),
                    ])];
                }
                this.writesLatched = true;
                record = { ...record, state: "recovery_failed", manualReviewPaths };
                await this.options.journal.save(record);
                return failed(
                    call,
                    "tool.unknown_outcome",
                    "Vault Change Batch has an ambiguous write outcome requiring manual review.",
                    false,
                    "unknown_outcome",
                );
            }
            try {
                await this.rollback(record, true);
                record = { ...record, state: "rolled_back", appliedPaths: [] };
                await this.options.journal.save(record);
                return error instanceof ChangeValidationError
                    ? validationFailure(call, error)
                    : failed(call, "tool.failed", "Vault Change Batch failed and was rolled back.");
            } catch {
                this.writesLatched = true;
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
            if (record.state === "recovery_failed") {
                reports.push({
                    batchId: record.batchId,
                    state: "recovery_failed",
                    manualReviewPaths: record.manualReviewPaths,
                });
                continue;
            }
            if (record.state === "prepared") {
                record = { ...record, state: "rolled_back", appliedPaths: [], manualReviewPaths: [] };
                await this.options.journal.save(record);
                reports.push({ batchId: record.batchId, state: "rolled_back", manualReviewPaths: [] });
                continue;
            }
            if (record.state === "undoing") {
                try {
                    record = await this.completeUndo(record);
                    reports.push({ batchId: record.batchId, state: "undone", manualReviewPaths: [] });
                    continue;
                } catch (error) {
                    if (error instanceof VaultChangeCrashInjectionError) throw error;
                    const manualReviewPaths = error instanceof UndoConflictError
                        ? [...error.paths]
                        : await this.unexpectedPaths(record);
                    record = { ...record, manualReviewPaths };
                    await this.options.journal.save(record);
                    reports.push({ batchId: record.batchId, state: "recovery_failed", manualReviewPaths });
                    continue;
                }
            }
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
        this.writesLatched = reports.some((report) => report.state === "recovery_failed");
        return reports;
    }

    async undo(batchId: string): Promise<VaultUndoResult> {
        if (!await this.writeGateOpen()) {
            throw new Error("Vault Change recovery requires manual review before another write can run.");
        }
        let record = await this.options.journal.load(batchId);
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
        record = { ...record, state: "undoing", appliedPaths: record.targets.map((target) => target.path) };
        await this.options.journal.save(record);
        this.inject("after-undoing-journal");
        try {
            record = await this.completeUndo(record);
            return { status: "undone", batchId, paths: record.targets.map((target) => target.path) };
        } catch (error) {
            this.writesLatched = true;
            if (error instanceof VaultChangeCrashInjectionError) throw error;
            if (record === undefined || record.checkpointRef === null) throw new Error("undo journal identity was lost");
            const checkpointRef = record.checkpointRef;
            const manualReviewPaths = error instanceof UndoConflictError
                ? [...error.paths]
                : await this.unexpectedPaths(record);
            record = { ...record, manualReviewPaths };
            await this.options.journal.save(record);
            if (!(error instanceof UndoConflictError)) throw error;
            const targets = record.targets;
            const conflictDiffs = await Promise.all(error.paths.map(async (path) => {
                const target = targets.find((candidate) => candidate.path === path);
                const current = await this.options.vault.read(path);
                const before = await this.options.checkpoints.read(checkpointRef, path);
                return conflictDiff(target?.path ?? path, current, before);
            }));
            return {
                status: "conflict",
                batchId,
                paths: error.paths,
                diff: truncateUtf8(conflictDiffs.join("\n"), MAX_DIFF_BYTES),
            };
        }
    }

    private async prepare(input: Readonly<Record<string, unknown>>): Promise<PreparedBatch> {
        if (hasExtraKeys(input, [
            "batchId", "task", "changeKind", "sourceBindings", "interviewSubmission", "operations",
        ])) invalid("Vault Change Batch fields are invalid.");
        const batchId = typeof input.batchId === "string" && BATCH_ID.test(input.batchId) ? input.batchId : undefined;
        const task = typeof input.task === "string" && input.task.trim() ? input.task.trim() : undefined;
        const changeKind = input.changeKind === "general" || input.changeKind === "interview_submission"
            ? input.changeKind : undefined;
        if (!batchId || !task || Buffer.byteLength(task, "utf8") > MAX_TASK_BYTES ||
            changeKind === undefined || !Array.isArray(input.sourceBindings) ||
            !Object.prototype.hasOwnProperty.call(input, "interviewSubmission") ||
            !Array.isArray(input.operations) || input.operations.length < 1 || input.operations.length > MAX_ACTIONS) {
            invalid("Vault Change Batch identity, task, or operation count is invalid.");
        }
        const sourceBindings = input.sourceBindings.map(parseSourceBinding);
        const sourcePaths = sourceBindings.map((binding) => binding.path.toLocaleLowerCase());
        if (new Set(sourcePaths).size !== sourcePaths.length) {
            invalid("Each Vault Change source path may appear only once.");
        }
        const interviewSubmission = input.interviewSubmission === null
            ? undefined : parseInterviewSubmission(input.interviewSubmission);
        if ((changeKind === "interview_submission") !== (interviewSubmission !== undefined)) {
            invalid("Interview Submission metadata must appear exactly on Interview Submission batches.");
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
        await this.revalidateOriginalSources(sourceBindings);
        let batchBytes = 0;
        for (const operation of operations) {
            const before = await this.options.vault.snapshot(operation.path);
            const beforeContent = before.content;
            if (beforeContent !== undefined && Buffer.byteLength(beforeContent, "utf8") > MAX_FILE_BYTES) {
                throw new ChangeValidationError("protocol.message_too_large", "Vault target exceeds the file limit.");
            }
            const beforeHash = contentIdentity(beforeContent);
            if (operation.expectedContentHash !== beforeHash) {
                throw new ChangeValidationError("resource.conflict", `Vault target '${operation.path}' changed before apply.`);
            }
            if (operation.expectedModifiedVersion !== undefined &&
                operation.expectedModifiedVersion !== before.modifiedVersion) {
                throw new ChangeValidationError("resource.conflict", `Vault target '${operation.path}' version changed before apply.`);
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
                beforeModifiedVersion: before.modifiedVersion,
                expectedModifiedVersion: operation.expectedModifiedVersion,
            });
        }
        validatePlanningMemoryDeletes(targets);
        if (changeKind === "interview_submission") {
            assertInterviewSubmission(targets, interviewSubmission as InterviewSubmissionMetadata);
        } else if (targets.some(isInterviewExperienceTarget)) {
            invalid("Interview Experience ingestion must use changeKind 'interview_submission'.");
        }
        const diff = batchDiff(targets);
        return {
            batchId,
            changeKind,
            task,
            targets,
            categorizedTargets: targets.map(({ path, operation, beforeHash, expectedModifiedVersion }) => ({
                path,
                operation,
                category: categorizeTarget(path),
                expectedModifiedVersion,
                expectedContentHash: beforeHash,
            })),
            sourceBindings,
            interviewSubmission,
            diff,
            controlFiles: paths.some(isControlPath),
            memoryDelete: deletes.length > 0,
        };
    }

    private async revalidate(prepared: PreparedBatch): Promise<void> {
        await this.revalidateOriginalSources(prepared.sourceBindings);
        for (const target of prepared.targets) {
            await this.revalidateTarget(target);
        }
    }

    private async revalidateTarget(target: Pick<
        PreparedTarget, "path" | "beforeHash" | "beforeModifiedVersion" | "expectedModifiedVersion"
    >): Promise<void> {
        const current = await this.options.vault.snapshot(target.path);
        if (contentIdentity(current.content) !== target.beforeHash ||
            current.modifiedVersion !== target.beforeModifiedVersion) {
            throw new ChangeValidationError("resource.conflict", `Vault target '${target.path}' changed before apply.`);
        }
    }

    private async classifyMutationFailure(target: PreparedTarget, cause: unknown): Promise<never> {
        let current: VaultChangeSnapshot;
        try {
            current = await this.options.vault.snapshot(target.path);
        } catch {
            throw new AmbiguousWriteOutcomeError(target.path);
        }
        const identity = contentIdentity(current.content);
        if (identity === target.afterHash) throw new AmbiguousWriteOutcomeError(target.path);
        if (identity === target.beforeHash) throw cause;
        throw new ChangeValidationError(
            "resource.conflict",
            `Vault target '${target.path}' changed during apply.`,
        );
    }

    private async applyConditional(target: PreparedTarget): Promise<void> {
        const expected = {
            contentHash: target.beforeHash,
            modifiedVersion: target.beforeModifiedVersion,
        };
        const mutation: ConditionalVaultMutation = target.afterContent === undefined
            ? { kind: "delete", path: target.path, expected }
            : target.operation === "create"
                ? {
                    kind: "create",
                    path: target.path,
                    expected: { contentHash: "absent", modifiedVersion: "missing" },
                    afterContent: target.afterContent,
                }
                : { kind: "modify", path: target.path, expected, afterContent: target.afterContent };
        let outcome: ConditionalMutationOutcome;
        try {
            outcome = await this.options.vault.applyConditional(mutation);
        } catch (error) {
            return this.classifyMutationFailure(target, error);
        }
        if (outcome.status === "applied") return;
        if (outcome.status === "unknown") throw new AmbiguousWriteOutcomeError(target.path);
        if (outcome.status === "unsupported") {
            throw new ChangeValidationError(
                "tool.failed",
                "This Vault adapter cannot safely apply a conditional delete.",
            );
        }
        throw new ChangeValidationError(
            "resource.conflict",
            `Vault target '${target.path}' changed during conditional apply.`,
        );
    }

    private async revalidateOriginalSources(bindings: readonly VaultChangeSourceBinding[]): Promise<void> {
        for (const binding of bindings) {
            const current = await this.options.vault.snapshot(binding.path);
            if (current.modifiedVersion !== binding.expectedModifiedVersion ||
                contentIdentity(current.content) !== binding.expectedContentHash) {
                throw new ChangeValidationError("resource.conflict", `Vault source '${binding.path}' changed before apply.`);
            }
        }
    }

    private async revalidateSources(prepared: PreparedBatch, appliedPaths: ReadonlySet<string>): Promise<void> {
        for (const binding of prepared.sourceBindings) {
            const target = prepared.targets.find((candidate) =>
                candidate.path.toLocaleLowerCase() === binding.path.toLocaleLowerCase(),
            );
            if (target !== undefined && appliedPaths.has(target.path.toLocaleLowerCase())) {
                if (contentIdentity(await this.options.vault.read(binding.path)) !== target.afterHash) {
                    throw new ChangeValidationError("resource.conflict", `Applied Vault source '${binding.path}' changed during apply.`);
                }
                continue;
            }
            const current = await this.options.vault.snapshot(binding.path);
            if (current.modifiedVersion !== binding.expectedModifiedVersion ||
                contentIdentity(current.content) !== binding.expectedContentHash) {
                throw new ChangeValidationError("resource.conflict", `Vault source '${binding.path}' changed during apply.`);
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
        if (["prepared", "applying", "undoing", "recovery_failed"].includes(record.state)) {
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

    private async completeUndo(record: VaultChangeJournalRecord): Promise<VaultChangeJournalRecord> {
        if (record.state !== "undoing" || record.checkpointRef === null) throw new Error("undo journal is not recoverable");
        let currentRecord = record;
        for (const target of [...record.targets].reverse()) {
            const current = await this.options.vault.read(target.path);
            const identity = contentIdentity(current);
            if (identity !== target.beforeHash && identity !== target.afterHash) {
                throw new UndoConflictError([target.path]);
            }
            if (identity === target.afterHash) {
                const before = await this.options.checkpoints.read(record.checkpointRef, target.path);
                if (target.beforeHash !== "absent" &&
                    (before === undefined || contentIdentity(before) !== target.beforeHash)) {
                    throw new Error(`checkpoint mismatch: ${target.path}`);
                }
                await this.options.vault.restore(target.path, before);
                this.inject("after-undo-target-write", target.path);
                if (contentIdentity(await this.options.vault.read(target.path)) !== target.beforeHash) {
                    throw new Error(`undo verification failed: ${target.path}`);
                }
            }
            currentRecord = {
                ...currentRecord,
                appliedPaths: currentRecord.appliedPaths.filter((path) => path !== target.path),
            };
            await this.options.journal.save(currentRecord);
            this.inject("after-undo-target-journal", target.path);
        }
        currentRecord = { ...currentRecord, state: "undone", appliedPaths: [], manualReviewPaths: [] };
        await this.options.journal.save(currentRecord);
        return currentRecord;
    }

    private async rollback(record: VaultChangeJournalRecord, recordedOnly = false): Promise<void> {
        if (record.checkpointRef === null) {
            const states = await this.observedTargetStates(record);
            if (states.some((state) => state !== "before")) throw new Error("checkpoint missing after mutation");
            return;
        }
        const applied = new Set(record.appliedPaths.map((path) => path.toLocaleLowerCase()));
        for (const target of [...record.targets].reverse()) {
            const current = await this.options.vault.read(target.path);
            const identity = contentIdentity(current);
            if (identity === target.beforeHash) continue;
            if (identity !== target.afterHash) {
                if (!applied.has(target.path.toLocaleLowerCase())) continue;
                throw new Error(`unexpected target state: ${target.path}`);
            }
            if (recordedOnly && !applied.has(target.path.toLocaleLowerCase())) continue;
            const before = await this.options.checkpoints.read(record.checkpointRef, target.path);
            if (target.beforeHash !== "absent" &&
                (before === undefined || contentIdentity(before) !== target.beforeHash)) {
                throw new Error(`checkpoint mismatch: ${target.path}`);
            }
            await this.options.vault.restore(target.path, before);
            if (contentIdentity(await this.options.vault.read(target.path)) !== target.beforeHash) {
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

    private async writeGateOpen(): Promise<boolean> {
        try {
            await this.recoveryGate;
        } catch {
            return false;
        }
        return !this.writesLatched;
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
    const expectedModifiedVersion = value.expectedModifiedVersion;
    if (typeof expectedModifiedVersion !== "string" || !MODIFIED_VERSION.test(expectedModifiedVersion)) {
        invalid("Vault expected modified version is invalid.");
    }
    if (op === "create") {
        if (expected !== "absent" || expectedModifiedVersion !== "missing" ||
            !boundedContent(value.content) || hasExtraKeys(value, [
                "op", "path", "content", "expectedContentHash", "expectedModifiedVersion",
            ])) {
            invalid("Vault create operation is invalid.");
        }
        return { op, path, content: value.content, expectedContentHash: expected, expectedModifiedVersion };
    }
    if (typeof expected !== "string" || !DIGEST.test(expected)) invalid("Vault expected content hash is invalid.");
    if (expectedModifiedVersion === "missing" || expectedModifiedVersion === "absent") {
        invalid("Existing Vault targets require an observed modified version.");
    }
    if (op === "append") {
        if (!boundedContent(value.content) || hasExtraKeys(value, [
            "op", "path", "content", "expectedContentHash", "expectedModifiedVersion",
        ])) {
            invalid("Vault append operation is invalid.");
        }
        return { op, path, content: value.content, expectedContentHash: expected, expectedModifiedVersion };
    }
    if (op === "replace") {
        if (typeof value.find !== "string" || !value.find || !boundedContent(value.replacement) ||
            Buffer.byteLength(value.find, "utf8") > MAX_FILE_BYTES ||
            hasExtraKeys(value, [
                "op", "path", "find", "replacement", "expectedContentHash", "expectedModifiedVersion",
            ])) {
            invalid("Vault exact replace operation is invalid.");
        }
        return {
            op, path, find: value.find, replacement: value.replacement,
            expectedContentHash: expected, expectedModifiedVersion,
        };
    }
    if (op === "patch") {
        if (!Array.isArray(value.edits) || value.edits.length < 1 || value.edits.length > 256 ||
            hasExtraKeys(value, [
                "op", "path", "edits", "expectedContentHash", "expectedModifiedVersion",
            ])) invalid("Vault patch operation is invalid.");
        const edits = value.edits.map((edit) => {
            if (!isRecord(edit) || !positiveInteger(edit.startLine) || !positiveInteger(edit.endLine) ||
                Number(edit.endLine) < Number(edit.startLine) || !boundedContent(edit.replacement) ||
                hasExtraKeys(edit, ["startLine", "endLine", "replacement"])) invalid("Vault patch edit is invalid.");
            return { startLine: Number(edit.startLine), endLine: Number(edit.endLine), replacement: edit.replacement };
        });
        return { op, path, edits, expectedContentHash: expected, expectedModifiedVersion };
    }
    if (hasExtraKeys(value, ["op", "path", "expectedContentHash", "expectedModifiedVersion"])) {
        invalid("Vault delete operation is invalid.");
    }
    return { op: "delete", path, expectedContentHash: expected, expectedModifiedVersion };
}

function parseSourceBinding(value: unknown): VaultChangeSourceBinding {
    if (!isRecord(value) || hasExtraKeys(value, [
        "path", "expectedModifiedVersion", "expectedContentHash",
    ])) invalid("Vault Change source binding is invalid.");
    const path = safeVaultPath(value.path);
    if (path === undefined || typeof value.expectedModifiedVersion !== "string" ||
        !MODIFIED_VERSION.test(value.expectedModifiedVersion) ||
        value.expectedModifiedVersion === "missing" || value.expectedModifiedVersion === "absent" ||
        typeof value.expectedContentHash !== "string" || !DIGEST.test(value.expectedContentHash)) {
        invalid("Vault Change source binding identity is invalid.");
    }
    return {
        path,
        expectedModifiedVersion: value.expectedModifiedVersion,
        expectedContentHash: value.expectedContentHash,
    };
}

function parseInterviewSubmission(value: unknown): InterviewSubmissionMetadata {
    if (!isRecord(value) || hasExtraKeys(value, [
        "sourceKind", "capturedOn", "canonicalUrls", "orderedImageContentHashes", "sourceFingerprint",
    ])) invalid("Interview Submission metadata is invalid.");
    const sourceKind = value.sourceKind;
    if (!["text", "public_url", "ordered_images", "mixed"].includes(String(sourceKind)) ||
        typeof value.capturedOn !== "string" || !calendarDate(value.capturedOn) ||
        !Array.isArray(value.canonicalUrls) || value.canonicalUrls.length > 20 ||
        !Array.isArray(value.orderedImageContentHashes) || value.orderedImageContentHashes.length > 20 ||
        value.canonicalUrls.some((url) => !canonicalPublicUrl(url)) ||
        value.orderedImageContentHashes.some((hash) => typeof hash !== "string" || !DIGEST.test(hash)) ||
        new Set(value.canonicalUrls).size !== value.canonicalUrls.length ||
        (value.sourceFingerprint !== null &&
            (typeof value.sourceFingerprint !== "string" || !DIGEST.test(value.sourceFingerprint)))) {
        invalid("Interview Submission source manifest is invalid.");
    }
    const canonicalUrls = value.canonicalUrls as string[];
    const orderedImageContentHashes = value.orderedImageContentHashes as string[];
    if ((sourceKind === "text" && (canonicalUrls.length > 0 || orderedImageContentHashes.length > 0)) ||
        (sourceKind === "public_url" && (canonicalUrls.length < 1 || orderedImageContentHashes.length > 0)) ||
        (sourceKind === "ordered_images" && (canonicalUrls.length > 0 || orderedImageContentHashes.length < 1 ||
            value.sourceFingerprint === null)) ||
        (sourceKind === "mixed" && canonicalUrls.length + orderedImageContentHashes.length < 1)) {
        invalid("Interview Submission source kind does not match its manifest.");
    }
    if ((orderedImageContentHashes.length === 0 && value.sourceFingerprint !== null) ||
        (orderedImageContentHashes.length > 0 &&
            value.sourceFingerprint !== orderedImageFingerprint(orderedImageContentHashes))) {
        invalid("Interview Submission image fingerprint does not match its ordered image manifest.");
    }
    return {
        sourceKind: sourceKind as InterviewSubmissionMetadata["sourceKind"],
        capturedOn: value.capturedOn,
        canonicalUrls,
        orderedImageContentHashes,
        sourceFingerprint: value.sourceFingerprint as string | null,
    };
}

function orderedImageFingerprint(contentHashes: readonly string[]): string {
    const hash = createHash("sha256");
    contentHashes.forEach((contentHash, order) => {
        hash.update(`${order}\0${contentHash}\n`, "utf8");
    });
    return `sha256:${hash.digest("hex")}`;
}

function canonicalPublicUrl(value: unknown): boolean {
    if (typeof value !== "string" || !value || Buffer.byteLength(value, "utf8") > 2_048) return false;
    try {
        const url = new URL(value);
        if (!["http:", "https:"].includes(url.protocol) || url.username || url.password ||
            !publicHostname(url.hostname)) return false;
        url.hash = "";
        url.hostname = url.hostname.toLocaleLowerCase();
        const retained = [...url.searchParams.entries()]
            .map(([key, item], index) => ({ key, item, index }))
            .filter(({ key }) => !isDiscardedQueryParameter(key))
            .sort((left, right) => compareCodePoints(left.key, right.key) ||
                compareCodePoints(left.item, right.item) || left.index - right.index);
        url.search = "";
        for (const { key, item } of retained) url.searchParams.append(key, item);
        return url.href === value;
    } catch {
        return false;
    }
}

function compareCodePoints(left: string, right: string): number {
    return left < right ? -1 : left > right ? 1 : 0;
}

function publicHostname(value: string): boolean {
    const hostname = value.toLocaleLowerCase().replace(/^\[|\]$/gu, "");
    const version = isIP(hostname);
    if (version === 4) return publicIpv4(hostname);
    if (version === 6) return publicIpv6(hostname);
    if (!hostname.includes(".") || hostname.startsWith(".") || hostname.endsWith(".")) return false;
    return !/\.(?:home|internal|invalid|lan|local|localhost|test|example)$/iu.test(hostname);
}

function publicIpv4(value: string): boolean {
    const [first, second, third] = value.split(".").map(Number);
    return first !== 0 && first !== 10 && first !== 127 && first < 224 &&
        !(first === 100 && second >= 64 && second <= 127) &&
        !(first === 169 && second === 254) &&
        !(first === 172 && second >= 16 && second <= 31) &&
        !(first === 192 && (second === 0 || second === 168)) &&
        !(first === 198 && (second === 18 || second === 19 || (second === 51 && third === 100))) &&
        !(first === 203 && second === 0 && third === 113);
}

function publicIpv6(value: string): boolean {
    if (value === "::" || value === "::1") return false;
    if (value.startsWith("::ffff:")) {
        const tail = value.slice("::ffff:".length);
        if (isIP(tail) === 4) return publicIpv4(tail);
        const mapped = /^([0-9a-f]{1,4}):([0-9a-f]{1,4})$/iu.exec(tail);
        if (mapped === null) return false;
        const high = Number.parseInt(mapped[1], 16);
        const low = Number.parseInt(mapped[2], 16);
        return publicIpv4(`${high >>> 8}.${high & 0xff}.${low >>> 8}.${low & 0xff}`);
    }
    if (value.startsWith("::")) return false;
    const first = Number.parseInt(value.split(":", 1)[0] || "0", 16);
    return !(first >= 0xfc00 && first <= 0xfdff) &&
        !(first >= 0xfe80 && first <= 0xfebf) &&
        !(first >= 0xff00) &&
        !value.startsWith("2001:db8:");
}

function isDiscardedQueryParameter(value: string): boolean {
    return /^(utm_.+|spm|from|source|ref|fbclid|gclid|dclid|yclid|mc_cid|mc_eid|igshid|msclkid|ttclid|twclid)$/iu.test(value) ||
        /^(token|(?:access|refresh|id|session|security)[_-]?token|auth(?:orization)?|api[_-]?key|credential|signature|sig|expires?|expiry|awsaccesskeyid|googleaccessid|key-pair-id|policy|x-amz-.+|x-goog-.+)$/iu.test(value);
}

function assertInterviewSubmission(
    targets: readonly PreparedTarget[],
    submission: InterviewSubmissionMetadata,
): void {
    const experiences = targets.filter(isInterviewExperienceTarget);
    if (experiences.length !== 1 || experiences[0].operation !== "create" ||
        experiences[0].beforeContent !== undefined || experiences[0].afterContent === undefined ||
        !EXPERIENCE_PATH.test(experiences[0].path)) {
        invalid("An Interview Submission must create exactly one new Interview Experience.");
    }
    const experienceMetadata = markdownFrontmatter(experiences[0].afterContent);
    if (experienceMetadata === null || experienceMetadata.get("type") !== "interview-experience" ||
        !BATCH_ID.test(experienceMetadata.get("experience-id") ?? "") ||
        experienceMetadata.get("source-kind") !== submission.sourceKind ||
        experienceMetadata.get("captured-on") !== submission.capturedOn ||
        experienceMetadata.get("source-url") !== submission.canonicalUrls[0] ||
        (submission.canonicalUrls.length === 0 && experienceMetadata.has("source-url")) ||
        experienceMetadata.get("source-fingerprint") !== (submission.sourceFingerprint ?? undefined) ||
        (submission.sourceFingerprint === null && experienceMetadata.has("source-fingerprint"))) {
        invalid("Interview Experience Source Metadata does not match the submission manifest.");
    }
    if (!["company", "role", "event-date", "round"].every((key) => Boolean(experienceMetadata.get(key))) ||
        !calendarDateOrUnknown(experienceMetadata.get("event-date"))) {
        invalid("Interview Experience identity metadata must be present and non-empty.");
    }
    if ([...experienceMetadata.keys()].some(isPersonalIdentityFrontmatter)) {
        invalid("Interview Experience frontmatter must not retain candidate personal information.");
    }
    const questions = targets.filter((target) => QUESTION_PATH.test(target.path));
    if (questions.length < 1 || targets.some((target) => !isPrimaryInterviewTarget(target.path))) {
        invalid("An Interview Submission must contain only Experience, Question, and index targets.");
    }
    for (const question of questions) {
        if (question.operation === "delete" || question.afterContent === undefined) {
            invalid("An Interview Submission cannot delete an Interview Question.");
        }
        const metadata = markdownFrontmatter(question.afterContent);
        if (metadata === null || metadata.get("type") !== "interview-question" ||
            !BATCH_ID.test(metadata.get("question-id") ?? "") ||
            !boundedMetadata(metadata.get("title"), 512) ||
            !positiveFrontmatterInteger(metadata.get("frequency")) ||
            !["needs-research", "draft", "verified"].includes(metadata.get("answer-state") ?? "")) {
            invalid("Every Interview Question target must remain structurally discoverable by the Catalog.");
        }
        if (!hasVaultWikiLink(question.afterContent, experiences[0].path, question.path)) {
            invalid("Every Interview Question target must link this Interview Experience occurrence.");
        }
        if (question.operation === "create" &&
            (metadata.get("frequency") !== "1" || metadata.get("answer-state") !== "needs-research" ||
                [...metadata.keys()].some(isAnswerContentFrontmatter) ||
                hasStandardAnswerSection(question.afterContent))) {
            invalid("A new Interview Question must start needs-research without a standard answer.");
        }
    }
    if (questions.some((question) =>
        !hasVaultWikiLink(experiences[0].afterContent as string, question.path, experiences[0].path))) {
        invalid("The Interview Experience must link every Question target in its batch.");
    }
    if (!targets.some((target) => target.path === EXPERIENCE_INDEX_PATH) ||
        !targets.some((target) => target.path === QUESTION_INDEX_PATH)) {
        invalid("An Interview Submission must update both primary Interview indexes in the same batch.");
    }
    const experienceIndex = targets.find((target) => target.path === EXPERIENCE_INDEX_PATH);
    const questionIndex = targets.find((target) => target.path === QUESTION_INDEX_PATH);
    if (experienceIndex?.afterContent === undefined || questionIndex?.afterContent === undefined ||
        !hasVaultWikiLink(experienceIndex.afterContent, experiences[0].path, EXPERIENCE_INDEX_PATH) ||
        questions.some((question) => question.operation === "create" &&
            !hasVaultWikiLink(questionIndex.afterContent as string, question.path, QUESTION_INDEX_PATH))) {
        invalid("Interview primary indexes must link every newly created Interview target.");
    }
}

function isInterviewExperienceTarget(target: Pick<PreparedTarget, "path" | "afterContent">): boolean {
    return EXPERIENCE_PATH.test(target.path) || LEGACY_EXPERIENCE_PATH.test(target.path) ||
        (target.afterContent !== undefined && markdownFrontmatter(target.afterContent)?.get("type") === "interview-experience");
}

function categorizeTarget(path: string): VaultChangeCategorizedTarget["category"] {
    if (path === EXPERIENCE_INDEX_PATH || path === QUESTION_INDEX_PATH || LEGACY_INTERVIEW_INDEX_PATH.test(path)) {
        return "index";
    }
    if (EXPERIENCE_PATH.test(path) || LEGACY_EXPERIENCE_PATH.test(path)) return "experience";
    if (QUESTION_PATH.test(path) || LEGACY_QUESTION_PATH.test(path)) return "question";
    return "other";
}

function isPrimaryInterviewTarget(path: string): boolean {
    return EXPERIENCE_PATH.test(path) || QUESTION_PATH.test(path) ||
        path === EXPERIENCE_INDEX_PATH || path === QUESTION_INDEX_PATH;
}

function isPersonalIdentityFrontmatter(key: string): boolean {
    return PERSONAL_IDENTITY_FRONTMATTER.test(key.replace(/-/gu, ""));
}

function isAnswerContentFrontmatter(key: string): boolean {
    return /^(?:(?:standard|model|reference|suggested|sample|draft)-)?answer$/u.test(key);
}

function boundedMetadata(value: string | undefined, maximumBytes: number): boolean {
    return value !== undefined && Boolean(value) && Buffer.byteLength(value, "utf8") <= maximumBytes;
}

function positiveFrontmatterInteger(value: string | undefined): boolean {
    const parsed = Number(value);
    return value !== undefined && Number.isSafeInteger(parsed) && parsed >= 1;
}

function calendarDateOrUnknown(value: string | undefined): boolean {
    return value === "unknown" || (value !== undefined && calendarDate(value));
}

function calendarDate(value: string): boolean {
    if (!/^\d{4}-\d{2}-\d{2}$/u.test(value)) return false;
    const instant = new Date(`${value}T00:00:00.000Z`);
    return !Number.isNaN(instant.getTime()) && instant.toISOString().slice(0, 10) === value;
}

function hasVaultWikiLink(content: string, targetPath: string, sourcePath: string): boolean {
    const withoutExtension = targetPath.replace(/\.md$/u, "");
    for (const match of content.matchAll(/\[\[([^\]|#]+)(?:#[^\]|]*)?(?:\|[^\]]*)?\]\]/gu)) {
        const linked = match[1].trim().replace(/^\.\//u, "").replace(/\.md$/u, "");
        if (linked === withoutExtension || resolveVaultWikiLink(sourcePath, linked) === withoutExtension) return true;
    }
    return false;
}

function resolveVaultWikiLink(sourcePath: string, linked: string): string {
    const resolved = sourcePath.split("/").slice(0, -1);
    for (const segment of linked.split("/")) {
        if (!segment || segment === ".") continue;
        if (segment === "..") resolved.pop();
        else resolved.push(segment);
    }
    return resolved.join("/");
}

function markdownFrontmatter(content: string): Map<string, string> | null {
    const normalized = content.replace(/\r\n/gu, "\n");
    if (!normalized.startsWith("---\n")) return null;
    const closing = normalized.indexOf("\n---\n", 4);
    if (closing < 0) return null;
    const metadata = new Map<string, string>();
    for (const line of normalized.slice(4, closing).split("\n")) {
        const separator = line.indexOf(":");
        if (separator <= 0) return null;
        const key = line.slice(0, separator).trim().toLocaleLowerCase();
        let item = line.slice(separator + 1).trim();
        if (!/^[a-z][a-z0-9-]*$/u.test(key) || metadata.has(key) || /[{}\r\n]/u.test(item) ||
            (key !== "source-url" && /[\[\]]/u.test(item))) return null;
        if ((item.startsWith('"') && item.endsWith('"')) || (item.startsWith("'") && item.endsWith("'"))) {
            item = item.slice(1, -1);
        }
        metadata.set(key, item);
    }
    return metadata;
}

function hasStandardAnswerSection(content: string): boolean {
    return /^#{1,6}\s*(?:(?:standard|model|reference|suggested|sample|draft)\s+answer|answer|标准答案|参考答案|示例答案|答案)\s*$/imu.test(content);
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

function invalid(message: string): never {
    throw new ChangeValidationError("protocol.invalid_params", message);
}

function isRecord(value: unknown): value is Record<string, unknown> {
    return value !== null && typeof value === "object" && !Array.isArray(value);
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

    /** Idempotently relocates recovery records preserved from the replaceable plugin tree. */
    async migrateLegacyDirectory(legacyDirectory: string): Promise<void> {
        const source = resolve(legacyDirectory);
        if (samePath(source, resolve(this.directory))) return;
        let entries;
        try {
            entries = await readdir(source, { withFileTypes: true });
        } catch (error) {
            if (isNodeError(error) && error.code === "ENOENT") return;
            throw error;
        }
        for (const entry of entries.sort((left, right) => left.name.localeCompare(right.name))) {
            if (entry.isFile() && /^[A-Za-z0-9][A-Za-z0-9_-]{0,127}\.json\.\d+\.\d+\.tmp$/u.test(entry.name)) {
                continue;
            }
            if (!entry.isFile() || !/^[A-Za-z0-9][A-Za-z0-9_-]{0,127}\.json$/u.test(entry.name)) {
                throw new Error(`legacy Vault Change journal contains an unexpected entry: ${entry.name}`);
            }
            const batchId = entry.name.slice(0, -5);
            const record = parseJournal(JSON.parse(await readFile(join(source, entry.name), "utf8")));
            if (record.batchId !== batchId) throw new Error("legacy Vault Change journal filename does not match its batch");
            const existing = await this.load(batchId);
            if (existing !== undefined && !isDeepStrictEqual(existing, record)) {
                throw new Error(`legacy batch '${batchId}' conflicts with stable journal`);
            }
            if (existing === undefined) await this.save(record);
        }
        await rm(source, { recursive: true });
    }

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
            if (record !== undefined && ["prepared", "applying", "undoing", "recovery_failed"].includes(record.state)) {
                records.push(record);
            }
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
        !["prepared", "applying", "applied", "undoing", "rolled_back", "undone", "recovery_failed"].includes(String(record.state)) ||
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
        (["applying", "applied", "undoing", "undone"].includes(String(record.state)) && record.checkpointRef === null)) malformed();
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
        "cachedRead" | "create" | "createFolder" | "delete" | "getAbstractFileByPath" | "getFileByPath" |
        "modify" | "process"
    >) {}

    async read(path: string): Promise<string | undefined> {
        const file = this.vault.getFileByPath(path);
        return file === null ? undefined : this.vault.cachedRead(file);
    }

    async snapshot(path: string): Promise<VaultChangeSnapshot> {
        const file = this.vault.getFileByPath(path);
        if (file === null) return { content: undefined, modifiedVersion: "missing" };
        const before = modifiedVersion(file);
        const content = await this.vault.cachedRead(file);
        const current = this.vault.getFileByPath(path);
        if (current === null || modifiedVersion(current) !== before) {
            throw new ChangeValidationError("resource.conflict", `Vault target '${path}' changed while being read.`);
        }
        return { content, modifiedVersion: before };
    }

    async create(path: string, content: string): Promise<void> {
        await this.ensureParentFolders(path);
        await this.vault.create(path, content);
    }

    async applyConditional(mutation: ConditionalVaultMutation): Promise<ConditionalMutationOutcome> {
        if (mutation.kind === "delete") return { status: "unsupported", operation: "delete" };
        if (mutation.kind === "create") {
            try {
                await this.create(mutation.path, mutation.afterContent);
                return { status: "applied" };
            } catch (error) {
                return this.classifyConditionalFailure(mutation, error);
            }
        }

        const file = this.vault.getFileByPath(mutation.path);
        if (file === null) {
            return {
                status: "conflict",
                observed: { contentHash: "absent", modifiedVersion: "missing" },
            };
        }
        let callbackConflict: VaultIdentity | null = null;
        const conflictSentinel = Object.freeze({ conditionalConflict: mutation.path });
        try {
            const written = await this.vault.process(file, (currentContent) => {
                const currentFile = this.vault.getFileByPath(mutation.path);
                const observed: VaultIdentity = currentFile === null
                    ? { contentHash: "absent", modifiedVersion: "missing" }
                    : { contentHash: contentIdentity(currentContent), modifiedVersion: modifiedVersion(currentFile) };
                if (!sameVaultIdentity(observed, mutation.expected)) {
                    callbackConflict = observed;
                    throw conflictSentinel;
                }
                return mutation.afterContent;
            });
            if (contentIdentity(written) !== contentIdentity(mutation.afterContent)) {
                return { status: "unknown", observed: await this.observedIdentity(mutation.path) };
            }
            return { status: "applied" };
        } catch (error) {
            if (error === conflictSentinel && callbackConflict !== null) {
                return { status: "conflict", observed: callbackConflict };
            }
            return this.classifyConditionalFailure(mutation, error);
        }
    }

    async modify(path: string, content: string): Promise<void> {
        const file = this.vault.getFileByPath(path);
        if (file === null) throw new Error(`Vault modify target is missing: ${path}`);
        await this.vault.modify(file, content);
    }

    async write(path: string, content: string): Promise<void> {
        const file = this.vault.getFileByPath(path);
        if (file !== null) {
            await this.vault.modify(file, content);
            return;
        }
        await this.ensureParentFolders(path);
        await this.vault.create(path, content);
    }

    async restore(path: string, content: string | undefined): Promise<void> {
        if (content === undefined) await this.remove(path);
        else await this.write(path, content);
    }

    private async ensureParentFolders(path: string): Promise<void> {
        const segments = path.split("/").slice(0, -1);
        let current = "";
        for (const segment of segments) {
            current = current ? `${current}/${segment}` : segment;
            if (this.vault.getAbstractFileByPath(current) === null) await this.vault.createFolder(current);
        }
    }

    async remove(path: string): Promise<void> {
        const file = this.vault.getFileByPath(path);
        if (file !== null) await this.vault.delete(file, true);
    }

    private async observedIdentity(path: string): Promise<VaultIdentity | null> {
        try {
            const snapshot = await this.snapshot(path);
            return {
                contentHash: contentIdentity(snapshot.content),
                modifiedVersion: snapshot.modifiedVersion,
            };
        } catch {
            return null;
        }
    }

    private async classifyConditionalFailure(
        mutation: ConditionalVaultMutation,
        cause: unknown,
    ): Promise<ConditionalMutationOutcome> {
        const observed = await this.observedIdentity(mutation.path);
        if (observed === null) return { status: "unknown", observed: null };
        if (sameVaultIdentity(observed, mutation.expected)) throw cause;
        if (mutation.kind !== "delete" &&
            observed.contentHash === contentIdentity(mutation.afterContent)) {
            return { status: "unknown", observed };
        }
        return { status: "conflict", observed };
    }
}

function sameVaultIdentity(left: VaultIdentity, right: VaultIdentity): boolean {
    return left.contentHash === right.contentHash && left.modifiedVersion === right.modifiedVersion;
}

function modifiedVersion(file: TFile): string {
    return `mtime:${file.stat.mtime}:size:${file.stat.size}`;
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
