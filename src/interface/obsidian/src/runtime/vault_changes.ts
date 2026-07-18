import { Buffer } from "node:buffer";
import { execFile } from "node:child_process";
import { createHash } from "node:crypto";
import { lstat, mkdir, mkdtemp, open, readFile, readdir, realpath, rename, rm } from "node:fs/promises";
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
import { validateCanonicalInterviewSourceIdentity } from "./interview_source_identity";
import { failed, hasExtraKeys } from "./plugin_tool_results";

const execFileAsync = promisify(execFile);
const MAX_ACTIONS = 20;
const MAX_REVIEW_ITEMS = 40;
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
    | { readonly status: "applied"; readonly applied: VaultIdentity }
    | { readonly status: "conflict"; readonly observed: VaultIdentity }
    | { readonly status: "unknown"; readonly observed: VaultIdentity | null }
    | { readonly status: "unsupported"; readonly operation: "delete" };

export interface VaultChangePort {
    read(path: string): Promise<string | undefined>;
    snapshot(path: string): Promise<VaultChangeSnapshot>;
    applyConditional(mutation: ConditionalVaultMutation): Promise<ConditionalMutationOutcome>;
}

export interface VaultCheckpointStore {
    create(batchId: string, existingPaths: readonly string[]): Promise<string>;
    read(checkpointRef: string, path: string): Promise<string | undefined>;
}

export interface VaultChangeJournalStore {
    load(batchId: string): Promise<VaultChangeJournalRecord | undefined>;
    save(record: VaultChangeJournalRecord): Promise<void>;
    listUnresolved(): Promise<VaultChangeJournalRecord[]>;
    findInterviewSubmissionByRootRun(rootRunId: string): Promise<VaultChangeJournalRecord | undefined>;
}

export interface VaultChangeAuthorizationProposal {
    readonly batchId: string;
    readonly argsHash: string;
    readonly changeKind: VaultChangeKind;
    readonly task: string;
    readonly paths: readonly string[];
    readonly categorizedTargets: readonly VaultChangeCategorizedTarget[];
    readonly sourceBindings: readonly VaultChangeSourceBinding[];
    readonly interviewSubmission: VaultChangeInterviewSubmissionReceipt | null;
    readonly reviewTargets: readonly VaultChangeReviewTarget[];
    readonly reviewHash: string;
    readonly controlFiles: boolean;
    readonly memoryDelete: boolean;
}

export interface VaultChangeReviewTarget {
    readonly operation: Operation["op"];
    readonly path: string;
    readonly beforeContent: string | null;
    readonly afterContent: string | null;
    readonly beforeContentHash: string;
    readonly afterContentHash: string;
    readonly beforeModifiedVersion: string;
}

export type VaultChangeAuthorizationDecision = {
    readonly decision: "accept" | "reject";
    readonly reviewHash: string;
};

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

export interface VaultChangeInterviewSubmissionReceipt {
    readonly sourceKind: "text" | "public_url" | "ordered_images" | "mixed";
    readonly capturedOn: string;
    readonly canonicalUrls: readonly string[];
    readonly orderedImageContentHashes: readonly string[];
    readonly sourceFingerprint: string | null;
    readonly reviewItems: readonly VaultChangeInterviewReviewItem[];
}

export interface VaultChangeInterviewReviewItem {
    readonly kind: "experience" | "question" | "index";
    readonly path: string;
    readonly identity: "new" | "existing";
    readonly mutation: "create" | "modify" | "none";
}

export function interviewReviewBadges(
    item: VaultChangeInterviewReviewItem,
): readonly [identity: string, mutation: string] {
    const identity = item.identity === "new" ? "身份 · 新增" : "身份 · 既有";
    const mutation = item.mutation === "create"
        ? "新增"
        : item.mutation === "none"
            ? "无操作"
            : item.kind === "experience" ? "合并" : "修改";
    return [identity, mutation];
}

export interface VaultChangeCoordinatorOptions {
    readonly vault: VaultChangePort;
    readonly checkpoints: VaultCheckpointStore;
    readonly journal: VaultChangeJournalStore;
    readonly permissionMode: () => VaultPermissionMode;
    readonly authorize?: (
        proposal: VaultChangeAuthorizationProposal,
    ) => Promise<VaultChangeAuthorizationDecision>;
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
    readonly interviewSubmission?: VaultChangeInterviewSubmissionReceipt;
    readonly controlFiles: boolean;
    readonly memoryDelete: boolean;
}

export interface VaultChangeJournalTarget {
    readonly operation: Operation["op"];
    readonly path: string;
    readonly beforeHash: string;
    readonly afterHash: string;
    readonly beforeModifiedVersion?: string;
    readonly afterModifiedVersion?: string | null;
}

export type VaultChangeJournalState =
    | "prepared"
    | "applying"
    | "applied"
    | "undoing"
    | "rolled_back"
    | "rejected"
    | "undone"
    | "recovery_failed";

export interface VaultChangeJournalRecord {
    readonly version: 1 | 2;
    readonly batchId: string;
    readonly toolCallId: string;
    readonly workspaceId: string;
    readonly runId: string;
    readonly rootRunId?: string;
    readonly changeKind?: VaultChangeKind;
    readonly reviewHash?: string;
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
        const rootRunId = call.agentLineage[0];
        if (!boundedIdentifier(rootRunId)) {
            return failed(call, "protocol.invalid_params", "Vault Change root Run identity is invalid.");
        }
        const proposal = authorizationProposal(prepared, call.argsHash);
        if (prepared.changeKind === "interview_submission") {
            const reserved = await this.options.journal.findInterviewSubmissionByRootRun(rootRunId);
            if (reserved !== undefined) {
                return failed(
                    call,
                    "resource.conflict",
                    "This root Agent Run already proposed an Interview Submission batch.",
                );
            }
        }
        let record: VaultChangeJournalRecord = {
            version: 2,
            batchId,
            toolCallId: call.toolCallId,
            workspaceId: call.workspaceId,
            runId: call.runId,
            rootRunId,
            changeKind: prepared.changeKind,
            reviewHash: proposal.reviewHash,
            argsHash: call.argsHash,
            idempotencyKey: call.idempotencyKey,
            state: "prepared",
            checkpointRef: null,
            targets: prepared.targets.map(({
                operation, path, beforeHash, afterHash, beforeModifiedVersion,
            }) => ({
                operation,
                path,
                beforeHash,
                afterHash,
                beforeModifiedVersion,
                afterModifiedVersion: null,
            })),
            appliedPaths: [],
            manualReviewPaths: [],
        };
        await this.options.journal.save(record);
        this.inject("after-prepared-journal");
        const confirmationRequired = this.options.permissionMode() === "ask_every_time" ||
            prepared.changeKind === "interview_submission" || prepared.controlFiles || prepared.memoryDelete;
        if (confirmationRequired) {
            const decision = this.options.authorize === undefined
                ? { decision: "reject" as const, reviewHash: proposal.reviewHash }
                : await this.options.authorize(proposal);
            if (decision.reviewHash !== proposal.reviewHash) {
                record = { ...record, state: "rejected" };
                await this.options.journal.save(record);
                return failed(
                    call,
                    "resource.conflict",
                    "Vault Change authorization does not match the exact reviewed batch.",
                );
            }
            if (decision.decision !== "accept") {
                record = { ...record, state: "rejected" };
                await this.options.journal.save(record);
                return failed(call, "policy.denied", "Vault Change Batch was not approved by the plugin.", false, "denied");
            }
            try {
                await this.revalidate(prepared);
            } catch (error) {
                record = { ...record, state: "rolled_back" };
                await this.options.journal.save(record);
                return validationFailure(call, error);
            }
        }
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
                await this.revalidateSources(prepared, new Set(record.appliedPaths.map((path) => path.toLowerCase())));
                await this.revalidateTarget(target);
                const appliedIdentity = await this.applyConditional(target);
                record = {
                    ...record,
                    targets: record.targets.map((candidate) => candidate.path === target.path
                        ? { ...candidate, afterModifiedVersion: appliedIdentity.modifiedVersion }
                        : candidate),
                };
                await this.options.journal.save(record);
                this.inject("after-target-write", target.path);
                record = { ...record, appliedPaths: [...record.appliedPaths, target.path] };
                await this.options.journal.save(record);
                this.inject("after-target-journal", target.path);
            }
            await this.revalidateSources(
                prepared,
                new Set(record.appliedPaths.map((path) => path.toLowerCase())),
            );
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
                } catch (rollbackError) {
                    manualReviewPaths = [...new Set([
                        error.path,
                        ...(rollbackError instanceof UndoConflictError ? rollbackError.paths : record.appliedPaths),
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
            } catch (rollbackError) {
                this.writesLatched = true;
                const manualReviewPaths = [...new Set([
                    ...(rollbackError instanceof UndoConflictError ? rollbackError.paths : []),
                    ...await this.unexpectedPaths(record),
                ])];
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
            let rollbackConflicts: readonly string[] = [];
            if (allKnown) {
                try {
                    await this.rollback(record);
                    record = { ...record, state: "rolled_back", appliedPaths: [], manualReviewPaths: [] };
                    await this.options.journal.save(record);
                    reports.push({ batchId: record.batchId, state: "rolled_back", manualReviewPaths: [] });
                    continue;
                } catch (error) {
                    rollbackConflicts = error instanceof UndoConflictError ? error.paths : [];
                }
            }
            const manualReviewPaths = [...new Set([...rollbackConflicts, ...record.targets
                .filter((_target, index) => states[index] === "unexpected")
                .map((target) => target.path)])];
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
            const current = await this.options.vault.snapshot(target.path);
            if (contentIdentity(current.content) !== target.afterHash ||
                target.afterModifiedVersion === null || target.afterModifiedVersion === undefined ||
                current.modifiedVersion !== target.afterModifiedVersion) {
                conflicts.push(target.path);
                const before = await this.options.checkpoints.read(record.checkpointRef, target.path);
                diffs.push(conflictDiff(target.path, current.content, before));
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
        const sourcePaths = sourceBindings.map((binding) => binding.path.toLowerCase());
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
        if (new Set(paths.map((path) => path.toLowerCase())).size !== paths.length) {
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
            const submission = interviewSubmission as VaultChangeInterviewSubmissionReceipt;
            const noOpContents = await prepareInterviewReviewNoOps(
                this.options.vault,
                targets,
                sourceBindings,
                submission.reviewItems,
            );
            assertInterviewSubmission(targets, submission, noOpContents);
        } else if (targets.some(isInterviewExperienceTarget)) {
            invalid("Interview Experience ingestion must use changeKind 'interview_submission'.");
        }
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

    private async applyConditional(target: PreparedTarget): Promise<VaultIdentity> {
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
        if (outcome.status === "applied") {
            if (outcome.applied.contentHash !== target.afterHash ||
                !MODIFIED_VERSION.test(outcome.applied.modifiedVersion)) {
                throw new AmbiguousWriteOutcomeError(target.path);
            }
            return outcome.applied;
        }
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
                candidate.path.toLowerCase() === binding.path.toLowerCase(),
            );
            if (target !== undefined && appliedPaths.has(target.path.toLowerCase())) {
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
        if (record.state === "rejected") {
            return failed(call, "policy.denied", "Vault Change Batch was not approved by the plugin.", false, "denied");
        }
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
            const current = await this.options.vault.snapshot(target.path);
            const identity = contentIdentity(current.content);
            if (identity !== target.beforeHash && identity !== target.afterHash) {
                throw new UndoConflictError([target.path]);
            }
            if (identity === target.afterHash) {
                if (target.afterModifiedVersion === null || target.afterModifiedVersion === undefined ||
                    current.modifiedVersion !== target.afterModifiedVersion) {
                    throw new UndoConflictError([target.path]);
                }
                const before = await this.options.checkpoints.read(record.checkpointRef, target.path);
                if (target.beforeHash !== "absent" &&
                    (before === undefined || contentIdentity(before) !== target.beforeHash)) {
                    throw new Error(`checkpoint mismatch: ${target.path}`);
                }
                await this.reverseConditional(target, before);
                this.inject("after-undo-target-write", target.path);
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
        const applied = new Set(record.appliedPaths.map((path) => path.toLowerCase()));
        for (const target of [...record.targets].reverse()) {
            const current = await this.options.vault.snapshot(target.path);
            const identity = contentIdentity(current.content);
            if (identity === target.beforeHash) continue;
            if (identity !== target.afterHash) {
                if (!applied.has(target.path.toLowerCase())) continue;
                throw new UndoConflictError([target.path]);
            }
            if (recordedOnly && !applied.has(target.path.toLowerCase())) continue;
            if (target.afterModifiedVersion === null || target.afterModifiedVersion === undefined ||
                current.modifiedVersion !== target.afterModifiedVersion) {
                throw new UndoConflictError([target.path]);
            }
            const before = await this.options.checkpoints.read(record.checkpointRef, target.path);
            if (target.beforeHash !== "absent" &&
                (before === undefined || contentIdentity(before) !== target.beforeHash)) {
                throw new Error(`checkpoint mismatch: ${target.path}`);
            }
            await this.reverseConditional(target, before);
        }
    }

    private async reverseConditional(target: VaultChangeJournalTarget, before: string | undefined): Promise<void> {
        if (target.afterModifiedVersion === null || target.afterModifiedVersion === undefined) {
            throw new UndoConflictError([target.path]);
        }
        const expected = { contentHash: target.afterHash, modifiedVersion: target.afterModifiedVersion };
        const mutation: ConditionalVaultMutation = target.beforeHash === "absent"
            ? { kind: "delete", path: target.path, expected }
            : target.afterHash === "absent"
                ? {
                    kind: "create",
                    path: target.path,
                    expected: { contentHash: "absent", modifiedVersion: "missing" },
                    afterContent: before as string,
                }
                : { kind: "modify", path: target.path, expected, afterContent: before as string };
        let outcome: ConditionalMutationOutcome;
        try {
            outcome = await this.options.vault.applyConditional(mutation);
        } catch {
            throw new UndoConflictError([target.path]);
        }
        if (outcome.status !== "applied" || outcome.applied.contentHash !== target.beforeHash) {
            throw new UndoConflictError([target.path]);
        }
    }

    private async observedTargetStates(record: VaultChangeJournalRecord): Promise<("before" | "after" | "unexpected")[]> {
        return Promise.all(record.targets.map(async (target) => {
            const snapshot = await this.options.vault.snapshot(target.path);
            const identity = contentIdentity(snapshot.content);
            if (identity === target.beforeHash) return "before";
            return identity === target.afterHash && target.afterModifiedVersion !== null &&
                target.afterModifiedVersion !== undefined && snapshot.modifiedVersion === target.afterModifiedVersion
                ? "after" : "unexpected";
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

function parseInterviewSubmission(value: unknown): VaultChangeInterviewSubmissionReceipt {
    if (!isRecord(value) || hasExtraKeys(value, [
        "sourceKind", "capturedOn", "canonicalUrls", "orderedImageContentHashes", "sourceFingerprint", "reviewItems",
    ])) invalid("Interview Submission metadata is invalid.");
    const sourceKind = value.sourceKind;
    if (!["text", "public_url", "ordered_images", "mixed"].includes(String(sourceKind)) ||
        typeof value.capturedOn !== "string" || !calendarDate(value.capturedOn)) {
        invalid("Interview Submission source manifest is invalid.");
    }
    const identity = validateCanonicalInterviewSourceIdentity({
        canonicalUrls: value.canonicalUrls,
        orderedImageContentHashes: value.orderedImageContentHashes,
        sourceFingerprint: value.sourceFingerprint,
    });
    if (identity === null) {
        invalid("Interview Submission source manifest is invalid.");
    }
    if (!Array.isArray(value.reviewItems) || value.reviewItems.length < 1 ||
        value.reviewItems.length > MAX_REVIEW_ITEMS) {
        invalid("Interview Submission review plan is invalid.");
    }
    const reviewItems = value.reviewItems.map(parseInterviewReviewItem);
    const reviewPaths = reviewItems.map((item) => item.path.toLowerCase());
    if (new Set(reviewPaths).size !== reviewPaths.length) {
        invalid("Each Interview Submission review path may appear only once.");
    }
    const { canonicalUrls, orderedImageContentHashes, sourceFingerprint } = identity;
    if ((sourceKind === "text" && (canonicalUrls.length > 0 || orderedImageContentHashes.length > 0)) ||
        (sourceKind === "public_url" && (canonicalUrls.length < 1 || orderedImageContentHashes.length > 0)) ||
        (sourceKind === "ordered_images" && (canonicalUrls.length > 0 || orderedImageContentHashes.length < 1 ||
            sourceFingerprint === null)) ||
        (sourceKind === "mixed" && canonicalUrls.length + orderedImageContentHashes.length < 1)) {
        invalid("Interview Submission source kind does not match its manifest.");
    }
    return {
        sourceKind: sourceKind as VaultChangeInterviewSubmissionReceipt["sourceKind"],
        capturedOn: value.capturedOn,
        canonicalUrls,
        orderedImageContentHashes,
        sourceFingerprint,
        reviewItems,
    };
}

function parseInterviewReviewItem(value: unknown): VaultChangeInterviewReviewItem {
    if (!isRecord(value) || hasExtraKeys(value, ["kind", "path", "identity", "mutation"])) {
        invalid("Interview Submission review item is invalid.");
    }
    const path = safeVaultPath(value.path);
    if (!["experience", "question", "index"].includes(String(value.kind)) || path === undefined ||
        !["new", "existing"].includes(String(value.identity)) ||
        !["create", "modify", "none"].includes(String(value.mutation))) {
        invalid("Interview Submission review item is invalid.");
    }
    return {
        kind: value.kind as VaultChangeInterviewReviewItem["kind"],
        path,
        identity: value.identity as VaultChangeInterviewReviewItem["identity"],
        mutation: value.mutation as VaultChangeInterviewReviewItem["mutation"],
    };
}

function assertInterviewSubmission(
    targets: readonly PreparedTarget[],
    submission: VaultChangeInterviewSubmissionReceipt,
    noOpContents: ReadonlyMap<string, string>,
): void {
    const experienceItems = submission.reviewItems.filter((item) => item.kind === "experience");
    const questionItems = submission.reviewItems.filter((item) => item.kind === "question");
    if (experienceItems.length !== 1 || questionItems.length < 1 ||
        targets.some((target) => !isPrimaryInterviewTarget(target.path))) {
        invalid("An Interview Submission must review one Experience and its Questions using primary Catalog paths.");
    }
    const experienceItem = experienceItems[0];
    const experienceTarget = targetForReviewItem(targets, experienceItem);
    const experienceContent = experienceTarget?.afterContent ?? noOpContents.get(experienceItem.path.toLowerCase());
    if (experienceContent === undefined || !EXPERIENCE_PATH.test(experienceItem.path)) {
        invalid("An Interview Submission Experience review is invalid.");
    }
    const experienceMetadata = markdownFrontmatter(experienceContent);
    if (experienceMetadata === null || experienceMetadata.get("type") !== "interview-experience" ||
        !BATCH_ID.test(experienceMetadata.get("experience-id") ?? "") ||
        !validExperienceSourceMetadata(experienceMetadata)) {
        invalid("Interview Experience Source Metadata is invalid.");
    }
    if (experienceItem.identity === "new" &&
        (experienceMetadata.get("source-kind") !== submission.sourceKind ||
            experienceMetadata.get("captured-on") !== submission.capturedOn ||
            experienceMetadata.get("source-url") !== submission.canonicalUrls[0] ||
            (submission.canonicalUrls.length === 0 && experienceMetadata.has("source-url")) ||
            experienceMetadata.get("source-fingerprint") !== (submission.sourceFingerprint ?? undefined) ||
            (submission.sourceFingerprint === null && experienceMetadata.has("source-fingerprint")))) {
        invalid("New Interview Experience Source Metadata does not match the submission manifest.");
    }
    if (experienceItem.identity === "existing") {
        const beforeContent = experienceTarget?.beforeContent ?? noOpContents.get(experienceItem.path.toLowerCase());
        const beforeMetadata = beforeContent === undefined ? null : markdownFrontmatter(beforeContent);
        const stableIdentityKeys = [
            "experience-id", "source-kind", "captured-on", "source-url", "source-fingerprint",
        ];
        if (beforeMetadata?.get("type") !== "interview-experience" ||
            !validExperienceSourceMetadata(beforeMetadata) ||
            stableIdentityKeys.some((key) => beforeMetadata.get(key) !== experienceMetadata.get(key))) {
            invalid("An existing Interview Experience must preserve its Catalog and source identity.");
        }
    }
    if (!["company", "role", "event-date", "round"].every((key) => Boolean(experienceMetadata.get(key))) ||
        !calendarDateOrUnknown(experienceMetadata.get("event-date"))) {
        invalid("Interview Experience identity metadata must be present and non-empty.");
    }
    if ([...experienceMetadata.keys()].some(isPersonalIdentityFrontmatter)) {
        invalid("Interview Experience frontmatter must not retain candidate personal information.");
    }
    for (const questionItem of questionItems) {
        if (!QUESTION_PATH.test(questionItem.path)) {
            invalid("An Interview Submission Question review path is invalid.");
        }
        const question = targetForReviewItem(targets, questionItem);
        const afterContent = question?.afterContent ?? noOpContents.get(questionItem.path.toLowerCase());
        if (afterContent === undefined || question?.operation === "delete") {
            invalid("An Interview Submission cannot delete an Interview Question.");
        }
        const metadata = markdownFrontmatter(afterContent);
        if (metadata === null || metadata.get("type") !== "interview-question" ||
            !BATCH_ID.test(metadata.get("question-id") ?? "") ||
            !boundedMetadata(metadata.get("title"), 512) ||
            !positiveFrontmatterInteger(metadata.get("frequency")) ||
            !["needs-research", "draft", "verified"].includes(metadata.get("answer-state") ?? "")) {
            invalid("Every Interview Question target must remain structurally discoverable by the Catalog.");
        }
        if (!hasVaultWikiLink(afterContent, experienceItem.path, questionItem.path)) {
            invalid("Every Interview Question target must link this Interview Experience occurrence.");
        }
        const afterOccurrences = interviewExperienceOccurrences(afterContent, questionItem.path);
        const afterFrequency = Number(metadata.get("frequency"));
        if (new Set(afterOccurrences.map((path) => path.toLowerCase())).size !== afterOccurrences.length ||
            afterFrequency !== afterOccurrences.length) {
            invalid("Interview Question frequency must equal its unique Experience occurrences.");
        }
        if (question?.operation === "create" &&
            (metadata.get("frequency") !== "1" || metadata.get("answer-state") !== "needs-research" ||
                [...metadata.keys()].some(isAnswerContentFrontmatter) ||
                hasStandardAnswerSection(afterContent))) {
            invalid("A new Interview Question must start needs-research without a standard answer.");
        }
        if (question !== undefined && question.operation !== "create") {
            const beforeContent = question.beforeContent as string;
            const beforeMetadata = markdownFrontmatter(beforeContent);
            const beforeOccurrences = interviewExperienceOccurrences(beforeContent, questionItem.path);
            const beforeFrequency = Number(beforeMetadata?.get("frequency"));
            if (beforeMetadata?.get("type") !== "interview-question" ||
                beforeMetadata.get("question-id") !== metadata.get("question-id") ||
                new Set(beforeOccurrences.map((path) => path.toLowerCase())).size !== beforeOccurrences.length ||
                beforeFrequency !== beforeOccurrences.length) {
                invalid("Existing Interview Question frequency is inconsistent with its Experience occurrences.");
            }
            const currentExperience = experienceItem.path.toLowerCase();
            const expectedOccurrences = new Set(beforeOccurrences.map((path) => path.toLowerCase()));
            expectedOccurrences.add(currentExperience);
            const actualOccurrences = new Set(afterOccurrences.map((path) => path.toLowerCase()));
            if (expectedOccurrences.size !== actualOccurrences.size ||
                [...expectedOccurrences].some((path) => !actualOccurrences.has(path))) {
                invalid("An Interview Question update may add only the current Experience occurrence.");
            }
        }
    }
    if (questionItems.some((question) =>
        !hasVaultWikiLink(experienceContent, question.path, experienceItem.path))) {
        invalid("The Interview Experience must link every Question target in its batch.");
    }
    const experienceIndex = targets.find((target) => target.path === EXPERIENCE_INDEX_PATH);
    const questionIndex = targets.find((target) => target.path === QUESTION_INDEX_PATH);
    const newQuestions = questionItems.filter((item) => item.identity === "new");
    if ((experienceItem.identity === "new" && (experienceIndex?.afterContent === undefined ||
        !hasVaultWikiLink(experienceIndex.afterContent, experienceItem.path, EXPERIENCE_INDEX_PATH))) ||
        (experienceItem.identity === "existing" && experienceIndex !== undefined) ||
        (newQuestions.length > 0 && (questionIndex?.afterContent === undefined || newQuestions.some((question) =>
            !hasVaultWikiLink(questionIndex.afterContent as string, question.path, QUESTION_INDEX_PATH)))) ||
        (newQuestions.length === 0 && questionIndex !== undefined)) {
        invalid("Interview primary indexes must link every newly created Interview target.");
    }
}

async function prepareInterviewReviewNoOps(
    vault: VaultChangePort,
    targets: readonly PreparedTarget[],
    sourceBindings: readonly VaultChangeSourceBinding[],
    reviewItems: readonly VaultChangeInterviewReviewItem[],
): Promise<Map<string, string>> {
    const noOpContents = new Map<string, string>();
    for (const target of targets) {
        const item = reviewItems.find((candidate) => candidate.path.toLowerCase() === target.path.toLowerCase());
        const expectedKind = categorizeTarget(target.path);
        if (item === undefined || expectedKind === "other" || item.kind !== expectedKind ||
            (target.operation === "create" && (item.identity !== "new" || item.mutation !== "create")) ||
            (target.operation !== "create" && (item.identity !== "existing" || item.mutation !== "modify"))) {
            invalid(`Interview Submission review does not match '${target.path}'.`);
        }
    }
    for (const item of reviewItems) {
        const target = targets.find((candidate) => candidate.path.toLowerCase() === item.path.toLowerCase());
        if ((item.identity === "new") !== (item.mutation === "create") ||
            (item.mutation === "none" && item.identity !== "existing")) {
            invalid("Interview Submission review identity and mutation are inconsistent.");
        }
        if (item.mutation !== "none" && target === undefined) {
            invalid(`Interview Submission review mutation '${item.path}' has no operation.`);
        }
        if (item.mutation === "none") {
            if (target !== undefined || categorizeTarget(item.path) !== item.kind) {
                invalid(`Interview Submission no-op review '${item.path}' is invalid.`);
            }
            const binding = sourceBindings.find((candidate) =>
                candidate.path.toLowerCase() === item.path.toLowerCase(),
            );
            if (binding === undefined) {
                invalid(`Interview Submission no-op review '${item.path}' requires an exact source binding.`);
            }
            const snapshot = await vault.snapshot(binding.path);
            if (snapshot.content === undefined || snapshot.modifiedVersion !== binding.expectedModifiedVersion ||
                contentIdentity(snapshot.content) !== binding.expectedContentHash) {
                throw new ChangeValidationError(
                    "resource.conflict",
                    `Vault no-op review source '${binding.path}' changed before apply.`,
                );
            }
            noOpContents.set(item.path.toLowerCase(), snapshot.content);
        }
    }
    return noOpContents;
}

function targetForReviewItem(
    targets: readonly PreparedTarget[],
    item: VaultChangeInterviewReviewItem,
): PreparedTarget | undefined {
    return targets.find((target) => target.path.toLowerCase() === item.path.toLowerCase());
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

function validExperienceSourceMetadata(metadata: ReadonlyMap<string, string>): boolean {
    const sourceKind = metadata.get("source-kind");
    const capturedOn = metadata.get("captured-on");
    const sourceUrl = metadata.get("source-url");
    const sourceFingerprint = metadata.get("source-fingerprint");
    if (!sourceKind || !["text", "public_url", "ordered_images", "mixed"].includes(sourceKind) ||
        capturedOn === undefined || !calendarDate(capturedOn) ||
        (sourceFingerprint !== undefined && !DIGEST.test(sourceFingerprint))) return false;
    if (sourceUrl !== undefined && validateCanonicalInterviewSourceIdentity({
        canonicalUrls: [sourceUrl],
        orderedImageContentHashes: [],
        sourceFingerprint: null,
    }) === null) return false;
    return (sourceKind === "text" && sourceUrl === undefined && sourceFingerprint === undefined) ||
        (sourceKind === "public_url" && sourceUrl !== undefined && sourceFingerprint === undefined) ||
        (sourceKind === "ordered_images" && sourceUrl === undefined && sourceFingerprint !== undefined) ||
        (sourceKind === "mixed" && (sourceUrl !== undefined || sourceFingerprint !== undefined));
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

function interviewExperienceOccurrences(content: string, sourcePath: string): string[] {
    const occurrences: string[] = [];
    for (const match of content.matchAll(/\[\[([^\]|#]+)(?:#[^\]|]*)?(?:\|[^\]]*)?\]\]/gu)) {
        const linked = match[1].trim().replace(/^\.\//u, "").replace(/\.md$/u, "");
        const direct = `${linked}.md`;
        const resolved = `${resolveVaultWikiLink(sourcePath, linked)}.md`;
        if (EXPERIENCE_PATH.test(direct) || LEGACY_EXPERIENCE_PATH.test(direct)) occurrences.push(direct);
        else if (EXPERIENCE_PATH.test(resolved) || LEGACY_EXPERIENCE_PATH.test(resolved)) occurrences.push(resolved);
    }
    return occurrences;
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
        const key = line.slice(0, separator).trim().toLowerCase();
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
    const path = value;
    if (!path || path.trim() !== path || path.length > MAX_PATH_LENGTH || path.includes("\\") ||
        path.includes(":") || path.includes("\0") || path.startsWith("/")) {
        return undefined;
    }
    const segments = path.split("/");
    if (segments.some((segment) => !segment || segment === "." || segment === ".." || segment.toLowerCase() === ".git" ||
        segment.toLowerCase() === "node_modules")) return undefined;
    const lower = path.toLowerCase();
    if (lower.startsWith(".obsidian/plugins/offeragent")) return undefined;
    if (segments.some((segment) => segment.startsWith(".")) &&
        !lower.startsWith(".codex/") && !lower.startsWith(".obsidian/")) return undefined;
    const extension = lower.slice(lower.lastIndexOf("."));
    return [".md", ".txt"].includes(extension) ||
        (lower.startsWith(".obsidian/") && [".json", ".css"].includes(extension)) ? path : undefined;
}

function isControlPath(path: string): boolean {
    const lower = path.toLowerCase();
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
        const key = line.slice(0, separator).trim().toLowerCase();
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

function authorizationProposal(
    prepared: PreparedBatch,
    argsHash: string,
): VaultChangeAuthorizationProposal {
    const reviewTargets: VaultChangeReviewTarget[] = prepared.targets.map((target) => ({
        operation: target.operation,
        path: target.path,
        beforeContent: target.beforeContent ?? null,
        afterContent: target.afterContent ?? null,
        beforeContentHash: target.beforeHash,
        afterContentHash: target.afterHash,
        beforeModifiedVersion: target.beforeModifiedVersion,
    }));
    const reviewed = {
        version: 1,
        batchId: prepared.batchId,
        argsHash,
        changeKind: prepared.changeKind,
        task: prepared.task,
        paths: prepared.targets.map((target) => target.path),
        categorizedTargets: prepared.categorizedTargets,
        sourceBindings: prepared.sourceBindings,
        interviewSubmission: prepared.interviewSubmission ?? null,
        reviewTargets,
        controlFiles: prepared.controlFiles,
        memoryDelete: prepared.memoryDelete,
    } as const;
    return { ...reviewed, reviewHash: digest(JSON.stringify(reviewed)) };
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
        .sort((left, right) => compareCodePointOrder(left.path, right.path))
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

    async markRecoveryReady(recoveryToken: string): Promise<void> {
        if (!/^[0-9a-f]{64}$/u.test(recoveryToken)) {
            throw new TypeError("Worker recovery token is invalid");
        }
        await mkdir(this.directory, { recursive: true });
        await this.publishRecoverySeals(recoveryToken);
        const target = join(this.directory, ".recovery-ready.json");
        const temporary = `${target}.${process.pid}.${Date.now()}.tmp`;
        const handle = await open(temporary, "wx", 0o600);
        try {
            await handle.writeFile(`${JSON.stringify({ schemaVersion: 2, recoveryToken })}\n`, "utf8");
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

    private async publishRecoverySeals(recoveryToken: string): Promise<void> {
        const sealRoot = join(this.directory, ".recovery-seals");
        await mkdir(sealRoot, { recursive: true });
        for (const entry of await readdir(sealRoot, { withFileTypes: true })) {
            if (entry.name !== "current" || !entry.isDirectory()) {
                throw new Error(`Vault Change recovery seal contains an unexpected entry: ${entry.name}`);
            }
            await rm(join(sealRoot, entry.name), { recursive: true });
        }
        const sealDirectory = join(sealRoot, "current");
        await mkdir(sealDirectory);
        const entries = await readdir(this.directory, { withFileTypes: true });
        const names: string[] = [];
        for (const entry of entries) {
            if (entry.name === ".recovery-ready.json" || entry.name === ".recovery-seals" ||
                /^[A-Za-z0-9][A-Za-z0-9_-]{0,127}\.json\.\d+\.\d+\.tmp$/u.test(entry.name)) {
                continue;
            }
            if (!entry.isFile() || !/^[A-Za-z0-9][A-Za-z0-9_-]{0,127}\.json$/u.test(entry.name)) {
                throw new Error(`Vault Change journal contains an unexpected recovery entry: ${entry.name}`);
            }
            names.push(entry.name);
        }
        for (const name of names.sort(compareCodePointOrder)) {
            const raw = await readStableRecoveryRecord(join(this.directory, name));
            const batchId = name.slice(0, -5);
            const parsed = parseJournal(JSON.parse(new TextDecoder("utf-8", { fatal: true }).decode(raw)));
            if (parsed.batchId !== batchId) {
                throw new Error("Vault Change journal filename does not match its batch");
            }
            if (parsed.version !== 2 || parsed.changeKind !== "interview_submission") continue;
            const seal = {
                schemaVersion: 1,
                recoveryToken,
                batchId,
                contentHash: digest(raw),
                byteLength: raw.byteLength,
            };
            const sealName = `${digest(batchId).slice("sha256:".length)}.json`;
            const handle = await open(join(sealDirectory, sealName), "wx", 0o600);
            try {
                await handle.writeFile(`${JSON.stringify(seal)}\n`, "utf8");
                await handle.sync();
            } finally {
                await handle.close();
            }
        }
    }

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
        return (await this.listRecords()).filter((record) =>
            ["prepared", "applying", "undoing", "recovery_failed"].includes(record.state),
        );
    }

    async findInterviewSubmissionByRootRun(rootRunId: string): Promise<VaultChangeJournalRecord | undefined> {
        if (!boundedIdentifier(rootRunId)) throw new Error("journal root Run ID is invalid");
        return (await this.listRecords()).find((record) =>
            record.version === 2 && record.changeKind === "interview_submission" &&
            record.rootRunId === rootRunId,
        );
    }

    private async listRecords(): Promise<VaultChangeJournalRecord[]> {
        let names: string[];
        try {
            names = await readdir(this.directory);
        } catch (error) {
            if (isNodeError(error) && error.code === "ENOENT") return [];
            throw error;
        }
        const records: VaultChangeJournalRecord[] = [];
        for (const name of names.sort()) {
            if (!/^[A-Za-z0-9][A-Za-z0-9_-]{0,127}\.json$/u.test(name)) continue;
            const record = await this.load(name.slice(0, -5));
            if (record !== undefined) records.push(record);
        }
        return records;
    }
}

type FileSnapshot = Awaited<ReturnType<typeof lstat>>;

async function readStableRecoveryRecord(path: string): Promise<Buffer> {
    const before = await lstat(path);
    requireRecoveryRecordFile(before);
    const handle = await open(path, "r");
    let opened: FileSnapshot;
    let after: FileSnapshot;
    let raw: Buffer;
    try {
        opened = await handle.stat();
        requireRecoveryRecordFile(opened);
        if (!sameFileIdentity(before, opened)) {
            throw new Error("Vault Change journal record changed before it was opened");
        }
        raw = Buffer.alloc(opened.size);
        let offset = 0;
        while (offset < raw.byteLength) {
            const read = await handle.read(raw, offset, raw.byteLength - offset, offset);
            if (read.bytesRead === 0) break;
            offset += read.bytesRead;
        }
        if (offset !== raw.byteLength) throw new Error("Vault Change journal record changed while reading");
        after = await handle.stat();
    } finally {
        await handle.close();
    }
    const current = await lstat(path);
    requireRecoveryRecordFile(current);
    if (!sameFileSnapshot(before, opened) || !sameFileSnapshot(opened, after) ||
        !sameFileSnapshot(after, current)) {
        throw new Error("Vault Change journal record changed while reading");
    }
    return raw;
}

function requireRecoveryRecordFile(snapshot: FileSnapshot): void {
    if (!snapshot.isFile() || snapshot.isSymbolicLink() || snapshot.size < 2 || snapshot.size > MAX_BATCH_BYTES) {
        throw new Error("Vault Change journal recovery record is not a bounded real file");
    }
}

function sameFileIdentity(left: FileSnapshot, right: FileSnapshot): boolean {
    return left.dev === right.dev && left.ino === right.ino;
}

function sameFileSnapshot(left: FileSnapshot, right: FileSnapshot): boolean {
    return sameFileIdentity(left, right) && left.size === right.size && left.mtimeMs === right.mtimeMs &&
        left.ctimeMs === right.ctimeMs;
}

function compareCodePointOrder(left: string, right: string): number {
    const leftPoints = [...left];
    const rightPoints = [...right];
    const length = Math.min(leftPoints.length, rightPoints.length);
    for (let index = 0; index < length; index += 1) {
        const difference = (leftPoints[index].codePointAt(0) as number) - (rightPoints[index].codePointAt(0) as number);
        if (difference !== 0) return difference;
    }
    return leftPoints.length - rightPoints.length;
}

function parseJournal(value: unknown): VaultChangeJournalRecord {
    const malformed = (): never => { throw new Error("Vault Change journal record is malformed"); };
    if (!isRecord(value)) malformed();
    const record = value as Record<string, unknown>;
    const version = record.version;
    if (hasExtraKeys(record, [
        "version", "batchId", "toolCallId", "workspaceId", "runId", "argsHash", "idempotencyKey", "state",
        "checkpointRef", "targets", "appliedPaths", "manualReviewPaths", "rootRunId", "changeKind", "reviewHash",
    ]) || ![1, 2].includes(Number(version)) ||
        (version === 1 && ["rootRunId", "changeKind", "reviewHash"].some((key) =>
            Object.prototype.hasOwnProperty.call(record, key))) ||
        (version === 2 && (!boundedIdentifier(record.rootRunId) ||
            !["general", "interview_submission"].includes(String(record.changeKind)) ||
            typeof record.reviewHash !== "string" || !DIGEST.test(record.reviewHash))) ||
        typeof record.batchId !== "string" || !BATCH_ID.test(record.batchId) ||
        !boundedIdentifier(record.toolCallId) || !boundedIdentifier(record.workspaceId) || !boundedIdentifier(record.runId) ||
        typeof record.argsHash !== "string" || !DIGEST.test(record.argsHash) || !boundedIdentifier(record.idempotencyKey) ||
        !["prepared", "applying", "applied", "undoing", "rolled_back", "rejected", "undone", "recovery_failed"].includes(String(record.state)) ||
        (record.state === "rejected" && version !== 2) ||
        (record.checkpointRef !== null && (typeof record.checkpointRef !== "string" ||
            !/^refs\/offeragent\/checkpoints\/[A-Za-z0-9][A-Za-z0-9_-]{0,127}$/u.test(record.checkpointRef))) ||
        !Array.isArray(record.targets) || record.targets.length < 1 || record.targets.length > MAX_ACTIONS ||
        !Array.isArray(record.appliedPaths) || !Array.isArray(record.manualReviewPaths)) malformed();
    const targets = record.targets as unknown[];
    const targetPaths = new Set<string>();
    for (const candidate of targets) {
        if (!isRecord(candidate)) malformed();
        const target = candidate as Record<string, unknown>;
        if (hasExtraKeys(target, [
            "operation", "path", "beforeHash", "afterHash", "beforeModifiedVersion", "afterModifiedVersion",
        ]) ||
            (version === 1 && ["beforeModifiedVersion", "afterModifiedVersion"].some((key) =>
                Object.prototype.hasOwnProperty.call(target, key))) ||
            (version === 2 && (typeof target.beforeModifiedVersion !== "string" ||
                !MODIFIED_VERSION.test(target.beforeModifiedVersion) ||
                (target.afterModifiedVersion !== null && (typeof target.afterModifiedVersion !== "string" ||
                    !MODIFIED_VERSION.test(target.afterModifiedVersion))))) ||
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
        const folded = path.toLowerCase();
        if (targetPaths.has(folded)) malformed();
        targetPaths.add(folded);
    }
    const validJournalPaths = (paths: unknown[]): boolean => {
        const unique = new Set<string>();
        return paths.every((candidate) => {
            if (typeof candidate !== "string" || safeVaultPath(candidate) !== candidate) return false;
            const folded = candidate.toLowerCase();
            if (!targetPaths.has(folded) || unique.has(folded)) return false;
            unique.add(folded);
            return true;
        });
    };
    if (!validJournalPaths(record.appliedPaths as unknown[]) || !validJournalPaths(record.manualReviewPaths as unknown[]) ||
        (version === 2 && (record.appliedPaths as string[]).some((path) => {
            const target = targets.find((candidate) => isRecord(candidate) && candidate.path === path) as
                Record<string, unknown> | undefined;
            return typeof target?.afterModifiedVersion !== "string";
        })) ||
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
        "cachedRead" | "create" | "createFolder" | "getAbstractFileByPath" | "getFileByPath" | "process"
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
                const applied = await this.observedIdentity(mutation.path);
                return applied !== null && applied.contentHash === contentIdentity(mutation.afterContent)
                    ? { status: "applied", applied }
                    : { status: "unknown", observed: applied };
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
        try {
            const written = await this.vault.process(file, (currentContent) => {
                const currentFile = this.vault.getFileByPath(mutation.path);
                const observed: VaultIdentity = currentFile === null
                    ? { contentHash: "absent", modifiedVersion: "missing" }
                    : { contentHash: contentIdentity(currentContent), modifiedVersion: modifiedVersion(currentFile) };
                if (!sameVaultIdentity(observed, mutation.expected)) {
                    callbackConflict = observed;
                    // Vault.process documents its single-file read/modify/save
                    // boundary, but not exception-abort semantics. Returning the
                    // observed bytes preserves concurrent content even if the
                    // implementation performs a same-content write.
                    return currentContent;
                }
                return mutation.afterContent;
            });
            if (callbackConflict !== null) return { status: "conflict", observed: callbackConflict };
            if (contentIdentity(written) !== contentIdentity(mutation.afterContent)) {
                return { status: "unknown", observed: await this.observedIdentity(mutation.path) };
            }
            const applied = await this.observedIdentity(mutation.path);
            return applied !== null && applied.contentHash === contentIdentity(mutation.afterContent)
                ? { status: "applied", applied }
                : { status: "unknown", observed: applied };
        } catch (error) {
            if (callbackConflict !== null) return { status: "conflict", observed: callbackConflict };
            return this.classifyConditionalFailure(mutation, error);
        }
    }

    private async ensureParentFolders(path: string): Promise<void> {
        const segments = path.split("/").slice(0, -1);
        let current = "";
        for (const segment of segments) {
            current = current ? `${current}/${segment}` : segment;
            if (this.vault.getAbstractFileByPath(current) === null) await this.vault.createFolder(current);
        }
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
    return process.platform === "win32" ? left.toLowerCase() === right.toLowerCase() : left === right;
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
