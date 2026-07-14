import { createHash } from "node:crypto";

import { App, MarkdownView, TFile } from "obsidian";

import { JsonObject, JsonValue, requireJsonObject } from "../runtime/json_rpc";
import {
    canonicalJson,
    ClientInvocationJournal,
    InvocationJournalConflict,
    InvocationJournalRecord,
} from "./client_invocation_journal";
import { ObsidianContextBridge } from "./obsidian_context";

const ABSENT_HASH = "absent";
const MAX_CONTENT_CHARS = 1_048_576;
const MAX_PREVIEW_DIFF_CHARS = 4 * 1_048_576;
const MAX_OPERATIONS = 20;

type Operation =
    | { op: "create"; path: string; content: string; expectedHash: "absent" }
    | { op: "append"; path: string; content: string; expectedHash: string }
    | { op: "replace"; path: string; find: string; replace: string; expectedHash: string }
    | { op: "patch"; path: string; edits: LineEdit[]; expectedHash: string }
    | { op: "rename"; path: string; destination: string; expectedHash: string; expectedDestinationHash: string }
    | { op: "trash"; path: string; expectedHash: string };

interface LineEdit { startLine: number; endLine: number; replacement: string; }
interface EditorHandle { view: MarkdownView; }
interface FileState {
    path: string;
    exists: boolean;
    content: string | null;
    hash: string;
    diskContent: string | null;
    diskHash: string;
    editor: EditorHandle | null;
    unsaved: boolean;
}
interface PreviewFile {
    path: string;
    beforeContent: string | null;
    afterContent: string | null;
    unsaved: boolean;
    open: boolean;
}
interface PreviewPathState extends JsonObject {
    path: string;
    beforeHash: string;
    afterHash: string;
    unsavedEditor: boolean;
    openEditor: boolean;
}
interface TransactionPlan {
    beforeState: JsonObject;
    afterState: JsonObject;
    beforeHash: string;
    afterHash: string;
    previewFiles: PreviewFile[];
    pathStates: PreviewPathState[];
}
interface TransactionIdentity {
    runId: string;
    toolCallId: string;
}

export interface ApprovalPresenter {
    present(params: JsonObject, signal: AbortSignal): Promise<boolean>;
}

/**
 * Read-only Obsidian proof adapter.
 *
 * The Worker owns every durable Vault mutation.  This bridge contributes the
 * authoritative live-editor snapshots used by Worker preflight and its
 * rollback-capable post-CAS observation, but deliberately rejects effectful
 * reverse invocations.  Keeping that boundary explicit prevents an editor
 * debounce, a path race, or a plugin crash from being mistaken for a committed
 * transaction.
 */
export class ObsidianClientToolBridge {
    private readonly app: App;
    private readonly journal: ClientInvocationJournal;
    private readonly approvalPresenter: ApprovalPresenter;

    constructor(
        app: App,
        context: ObsidianContextBridge,
        journal: ClientInvocationJournal,
        approvalPresenter: ApprovalPresenter,
    ) {
        this.app = app;
        this.journal = journal;
        this.approvalPresenter = approvalPresenter;
        void context;
    }

    async invoke(params: JsonObject, transportSignal: AbortSignal): Promise<JsonObject> {
        transportSignal.throwIfAborted();
        const invocation = parseInvocation(params);
        const requestHash = digest(canonicalJson({
            invocationId: invocation.invocationId,
            toolCallId: invocation.toolCallId,
            runId: invocation.runId,
            name: invocation.name,
            arguments: invocation.arguments,
            argsHash: invocation.argsHash,
            idempotencyKey: invocation.idempotencyKey,
        }));
        const existing = await this.journal.lookup(invocation.invocationId);
        if (existing) {
            requireRecordBinding(existing, invocation, requestHash);
            return existing.state === "completed" ? existing.result : this.reconcileRecord(existing);
        }
        if (digest(canonicalJson(invocation.arguments)) !== invocation.argsHash) {
            return this.persistWithoutEffect(
                invocation, requestHash, "failed", "args_hash_mismatch", "工具参数哈希不匹配。", false,
            );
        }
        return this.persistWithoutEffect(
            invocation,
            requestHash,
            "failed",
            "client_vault_mutation_worker_owned",
            "Vault 持久化只能由 Worker 的原子事务协调器执行；插件 Client Tool 仅提供只读编辑器证明。",
            false,
        );
    }

    async lookup(params: JsonObject, signal: AbortSignal): Promise<JsonObject> {
        signal.throwIfAborted();
        exactKeys(params, ["invocationId", "runId"]);
        const invocationId = identifier(params.invocationId, "inv_");
        const runId = identifier(params.runId, "run_");
        const record = await this.journal.lookup(invocationId);
        if (!record) return { invocationId, found: false, result: null };
        if (record.runId !== runId) throw new InvocationJournalConflict("lookup Run does not own invocation");
        if (record.state === "completed") return { invocationId, found: true, result: record.result };
        return { invocationId, found: true, result: await this.reconcileRecord(record) };
    }

    async preview(params: JsonObject, signal: AbortSignal): Promise<JsonObject> {
        signal.throwIfAborted();
        exactKeys(params, [
            "invocationId", "toolCallId", "runId", "name", "arguments", "argsHash", "deadline", "traceId",
        ]);
        const invocationId = identifier(params.invocationId, "inv_");
        const toolCallId = identifier(params.toolCallId, "call_");
        const runId = identifier(params.runId, "run_");
        opaqueIdentifier(params.traceId, "traceId");
        if (params.name !== "obsidian.vault.transaction") throw new TypeError("unsupported Client Tool preview");
        const argumentsValue = requireJsonObject(params.arguments);
        const argsHash = sha256(params.argsHash);
        if (digest(canonicalJson(argumentsValue)) !== argsHash) throw new TypeError("Client Tool preview argsHash mismatch");
        if (typeof params.deadline !== "string" || !Number.isFinite(Date.parse(params.deadline)) ||
            Date.now() >= Date.parse(params.deadline)) throw timeoutError();
        const plan = await this.planTransaction(argumentsValue, { runId, toolCallId });
        signal.throwIfAborted();
        const diff = transactionDiff(plan.previewFiles);
        return {
            invocationId,
            toolCallId,
            stateHash: plan.beforeHash,
            afterStateHash: plan.afterHash,
            paths: plan.pathStates.map((item) => item.path),
            diff,
            diffSha256: digest(diff),
            hasUnsavedEditors: plan.pathStates.some((item) => item.unsavedEditor),
            hasOpenEditors: plan.pathStates.some((item) => item.openEditor),
            pathStates: plan.pathStates,
        };
    }

    async observeCommit(params: JsonObject, signal: AbortSignal): Promise<JsonObject> {
        signal.throwIfAborted();
        exactKeys(params, ["invocationId", "toolCallId", "runId", "paths", "deadline", "traceId"]);
        const invocationId = identifier(params.invocationId, "inv_");
        const toolCallId = identifier(params.toolCallId, "call_");
        identifier(params.runId, "run_");
        opaqueIdentifier(params.traceId, "traceId");
        if (typeof params.deadline !== "string" || !Number.isFinite(Date.parse(params.deadline)) ||
            Date.now() >= Date.parse(params.deadline)) throw timeoutError();
        if (!Array.isArray(params.paths) || params.paths.length < 1 || params.paths.length > 64) {
            throw new TypeError("Client Tool commit observation paths are invalid");
        }
        const paths = params.paths.map((value) => safePath(value));
        if (new Set(paths.map((path) => path.toLocaleLowerCase("en-US"))).size !== paths.length) {
            throw new TypeError("Client Tool commit observation paths are not unique");
        }
        const states = await Promise.all(paths.map(async (path) => await this.readState(path)));
        signal.throwIfAborted();
        const pathStates = states.map((state) => ({
            path: state.path,
            observedHash: state.diskHash,
            unsavedEditor: state.unsaved,
            openEditor: state.editor !== null,
        }));
        return {
            invocationId,
            toolCallId,
            paths,
            hasUnsavedEditors: pathStates.some((item) => item.unsavedEditor),
            hasOpenEditors: pathStates.some((item) => item.openEditor),
            pathStates,
        };
    }

    async cancel(params: JsonObject, signal: AbortSignal): Promise<JsonObject> {
        signal.throwIfAborted();
        exactKeys(params, ["invocationId", "runId", "reason"]);
        const invocationId = identifier(params.invocationId, "inv_");
        const runId = identifier(params.runId, "run_");
        if (typeof params.reason !== "string" || !params.reason || params.reason.length > 4096) {
            throw new TypeError("client cancellation reason is invalid");
        }
        const record = await this.journal.lookup(invocationId);
        if (record && record.runId !== runId) throw new InvocationJournalConflict("cancel Run does not own invocation");
        return { invocationId, accepted: false, alreadyTerminal: record?.state === "completed" };
    }

    async presentApproval(params: JsonObject, signal: AbortSignal): Promise<JsonObject> {
        signal.throwIfAborted();
        const approvalId = identifier(params.approvalId, "apr_");
        const presented = await this.approvalPresenter.present(params, signal);
        return { approvalId, presented };
    }

    private async planTransaction(
        argumentsValue: JsonObject,
        identity: TransactionIdentity,
    ): Promise<TransactionPlan> {
        const parsed = parseTransaction(argumentsValue);
        const base = new Map<string, FileState>();
        const virtual = new Map<string, FileState>();
        const load = async (path: string): Promise<FileState> => {
            const existing = virtual.get(path);
            if (existing) return cloneState(existing);
            const state = await this.readState(path);
            base.set(path, cloneState(state));
            virtual.set(path, cloneState(state));
            return cloneState(state);
        };
        for (let index = 0; index < parsed.operations.length; index += 1) {
            const operation = parsed.operations[index];
            const before = await load(operation.path);
            requireExpected(operation.path, before.hash, operation.expectedHash);
            if (operation.op === "create") {
                if (before.exists) throw conflict(`文件已存在：${operation.path}`);
                virtual.set(operation.path, virtualContent(operation.path, operation.content, before));
            } else if (operation.op === "append") {
                virtual.set(operation.path, virtualContent(operation.path, requireContent(before) + operation.content, before));
            } else if (operation.op === "replace") {
                const current = requireContent(before);
                const matches = current.split(operation.find).length - 1;
                if (matches !== 1) throw conflict(`replace 预期唯一匹配，实际 ${matches}：${operation.path}`);
                virtual.set(operation.path, virtualContent(operation.path, current.replace(operation.find, operation.replace), before));
            } else if (operation.op === "patch") {
                virtual.set(
                    operation.path,
                    virtualContent(operation.path, applyLineEdits(operation.path, requireContent(before), operation.edits), before),
                );
            } else if (operation.op === "rename") {
                const destination = await load(operation.destination);
                requireExpected(operation.destination, destination.hash, operation.expectedDestinationHash);
                if (!before.exists) throw conflict(`rename 源不存在：${operation.path}`);
                virtual.set(operation.path, absentState(operation.path));
                virtual.set(operation.destination, virtualContent(operation.destination, requireContent(before), destination));
            } else {
                if (!before.exists) throw conflict(`trash 源不存在：${operation.path}`);
                const trashPath = internalTrashPath(identity, parsed.publicArgsHash, index, operation.path);
                const destination = await load(trashPath);
                requireExpected(trashPath, destination.hash, ABSENT_HASH);
                virtual.set(trashPath, virtualContent(trashPath, requireContent(before), destination));
                virtual.set(operation.path, absentState(operation.path));
            }
        }
        const paths = [...new Set([...base.keys(), ...virtual.keys()])].sort((left, right) =>
            left.localeCompare(right, "en", { sensitivity: "base" }));
        if (paths.length > 64) throw new Error("Client transaction touches too many paths");
        for (const path of paths) if (!base.has(path)) base.set(path, await this.readState(path));
        const changedPaths = paths.filter((path) => {
            const before = base.get(path) as FileState;
            const after = virtual.get(path) ?? before;
            return before.hash !== after.hash;
        });
        if (changedPaths.length === 0) throw new Error("Client Tool preview has no effective change");
        const beforeState = stateMap(base, changedPaths);
        const afterState = stateMap(virtual, changedPaths, base);
        const pathStates = changedPaths.map((path): PreviewPathState => {
            const before = base.get(path) as FileState;
            const after = virtual.get(path) ?? before;
            return {
                path,
                beforeHash: before.hash,
                afterHash: after.hash,
                unsavedEditor: before.unsaved,
                openEditor: before.editor !== null,
            };
        });
        return {
            beforeState,
            afterState,
            beforeHash: digest(canonicalJson(beforeState)),
            afterHash: digest(canonicalJson(afterState)),
            previewFiles: changedPaths.map((path) => {
                const before = base.get(path) as FileState;
                const after = virtual.get(path) ?? before;
                return {
                    path,
                    beforeContent: before.content,
                    afterContent: after.content,
                    unsaved: before.unsaved,
                    open: before.editor !== null,
                };
            }),
            pathStates,
        };
    }

    private async readState(path: string): Promise<FileState> {
        safePath(path);
        const editor = this.editorFor(path);
        const entry = this.app.vault.getAbstractFileByPath(path);
        if (!entry) {
            if (editor !== null) {
                throw conflict(`Vault 路径不存在，但仍由 Obsidian 编辑器占用：${path}`);
            }
            return absentState(path);
        }
        if (!(entry instanceof TFile)) throw conflict(`目标不是文件：${path}`);
        if (editor !== null && editor.view.file !== entry) {
            throw conflict(`Obsidian 编辑器与当前 Vault 文件身份不一致：${path}`);
        }
        const diskContent = await this.app.vault.read(entry);
        const observedEntry = this.app.vault.getAbstractFileByPath(path);
        const observedEditor = this.editorFor(path);
        if (observedEntry !== entry || observedEditor?.view !== editor?.view) {
            throw conflict(`Obsidian 文件或编辑器在读取期间发生变化：${path}`);
        }
        if (observedEditor !== null && observedEditor.view.file !== entry) {
            throw conflict(`Obsidian 编辑器与读取后的 Vault 文件身份不一致：${path}`);
        }
        const liveContent = observedEditor?.view.editor.getValue() ?? diskContent;
        return stateFromContent(path, liveContent, observedEditor, diskContent);
    }

    private editorFor(path: string): EditorHandle | null {
        for (const leaf of this.app.workspace.getLeavesOfType("markdown")) {
            const view = leaf.view;
            if (!(view instanceof MarkdownView)) continue;
            const statePath = leaf.getViewState().state?.file;
            if (view.file?.path === path || statePath === path) return { view };
        }
        return null;
    }

    private async reconcileRecord(record: Extract<InvocationJournalRecord, { state: "started" }>): Promise<JsonObject> {
        if (record.recovery.length === 0) {
            const result = resultEnvelope(recoveryInvocation(record), {
                status: "failed",
                output: null,
                summary: "Client Tool 在持久化终态前中断，且没有任何可证明已提交的恢复状态。",
                beforeHash: null,
                afterHash: null,
                beforeState: null,
                afterState: null,
                sideEffectFacts: [],
                error: {
                    code: "interrupted_before_apply",
                    message: "空 recovery 记录不能证明成功；未观察到需要恢复的 Vault 副作用。",
                    retryable: false,
                    cancelled: false,
                    details: { recoveredEmptyClientInvocation: true },
                },
            });
            return (await this.journal.complete(record.invocationId, record.requestHash, result)).result;
        }
        const ambiguousPaths = record.recovery
            .filter((state) => state.beforeHash === state.afterHash)
            .map((state) => state.path);
        if (ambiguousPaths.length > 0) {
            const beforeState = Object.fromEntries(record.recovery.map((item) => [item.path, item.beforeHash]));
            const result = resultEnvelope(recoveryInvocation(record), {
                status: "unknown_outcome",
                output: null,
                summary: "旧版 Client Tool recovery 证明包含相同的前后哈希，无法判定是否提交，需要人工复核。",
                beforeHash: digest(canonicalJson(beforeState)),
                afterHash: null,
                beforeState,
                afterState: null,
                sideEffectFacts: record.recovery.map((item) => ({
                    kind: "file_write",
                    state: "unknown",
                    resourceId: item.path,
                    beforeState: { hash: item.beforeHash },
                    afterState: null,
                    metadata: { ambiguousLegacyRecoveryProof: true },
                })),
                error: {
                    code: "manual_review_required",
                    message: "Recovery 的 beforeHash 与 afterHash 必须不同，歧义记录不能证明成功。",
                    retryable: false,
                    cancelled: false,
                    details: { ambiguousPaths },
                },
            });
            return (await this.journal.complete(record.invocationId, record.requestHash, result)).result;
        }
        const observed = await Promise.all(record.recovery.map(async (state) => ({
            state,
            actual: (await this.readState(state.path)).diskHash,
        })));
        const allAfter = observed.every((item) => item.actual === item.state.afterHash);
        const allBefore = observed.every((item) => item.actual === item.state.beforeHash);
        const status = allAfter ? "succeeded" : allBefore ? "failed" : "unknown_outcome";
        const result = resultEnvelope({
            invocationId: record.invocationId,
            toolCallId: record.toolCallId,
            runId: record.runId,
            name: "obsidian.vault.transaction",
            arguments: {},
            argsHash: record.requestHash,
            idempotencyKey: record.invocationId,
            deadline: new Date(Date.now() + 1_000).toISOString(),
            traceId: "trace_recovery",
        }, {
            status,
            output: allAfter ? {
                paths: record.recovery.map((item) => item.path),
                stateHash: digest(canonicalJson(Object.fromEntries(observed.map((item) => [item.state.path, item.actual])))),
                recoveryArtifactIds: [],
            } : null,
            summary: allAfter
                ? "已通过磁盘哈希只读恢复旧版 Client Tool 的成功结果。"
                : allBefore ? "旧版 Client Tool 在提交前中断，未观察到磁盘副作用。"
                    : "旧版 Client Tool 中断后磁盘状态不一致，需要人工复核。",
            beforeHash: digest(canonicalJson(Object.fromEntries(record.recovery.map((item) => [item.path, item.beforeHash])))),
            afterHash: allAfter
                ? digest(canonicalJson(Object.fromEntries(record.recovery.map((item) => [item.path, item.afterHash]))))
                : null,
            beforeState: Object.fromEntries(record.recovery.map((item) => [item.path, item.beforeHash])),
            afterState: Object.fromEntries(observed.map((item) => [item.state.path, item.actual])),
            sideEffectFacts: record.recovery.map((item) => ({
                kind: "file_write",
                state: allAfter ? "committed" : allBefore ? "observed" : "unknown",
                resourceId: item.path,
                beforeState: { hash: item.beforeHash },
                afterState: { hash: item.afterHash },
                metadata: { recoveredLegacyClientInvocation: true },
            })),
            error: allAfter ? undefined : {
                code: allBefore ? "interrupted_before_apply" : "manual_review_required",
                message: allBefore ? "未观察到文件修改。" : "部分路径处于预期前态，部分处于预期后态。",
                retryable: false,
                cancelled: false,
                details: {},
            },
        });
        return (await this.journal.complete(record.invocationId, record.requestHash, result)).result;
    }

    private async persistWithoutEffect(
        invocation: Invocation,
        requestHash: string,
        status: string,
        code: string,
        message: string,
        retryable: boolean,
    ): Promise<JsonObject> {
        const existing = await this.journal.lookup(invocation.invocationId);
        if (existing) {
            requireRecordBinding(existing, invocation, requestHash);
            if (existing.state === "completed") return existing.result;
        } else {
            await this.journal.begin({
                invocationId: invocation.invocationId,
                toolCallId: invocation.toolCallId,
                runId: invocation.runId,
                requestHash,
                recovery: [],
            });
        }
        const result = resultEnvelope(invocation, {
            status,
            output: null,
            summary: message,
            beforeHash: null,
            afterHash: null,
            beforeState: null,
            afterState: null,
            sideEffectFacts: [],
            error: { code, message, retryable, cancelled: status === "cancelled", details: {} },
        });
        return (await this.journal.complete(invocation.invocationId, requestHash, result)).result;
    }
}

interface Invocation {
    invocationId: string;
    toolCallId: string;
    runId: string;
    name: string;
    arguments: JsonObject;
    argsHash: string;
    idempotencyKey: string;
    deadline: string;
    traceId: string;
}

function parseInvocation(params: JsonObject): Invocation {
    exactKeys(params, [
        "invocationId", "toolCallId", "runId", "name", "arguments", "argsHash", "idempotencyKey", "deadline", "traceId",
    ]);
    return {
        invocationId: identifier(params.invocationId, "inv_"),
        toolCallId: identifier(params.toolCallId, "call_"),
        runId: identifier(params.runId, "run_"),
        name: toolName(params.name),
        arguments: requireJsonObject(params.arguments),
        argsHash: sha256(params.argsHash),
        idempotencyKey: text(params.idempotencyKey, "idempotencyKey", 256),
        deadline: timestamp(params.deadline),
        traceId: opaqueIdentifier(params.traceId, "traceId"),
    };
}

function parseTransaction(value: JsonObject): {
    transactionId: string;
    operations: Operation[];
    publicArgsHash: string;
} {
    exactKeys(value, ["transactionId", "operations"]);
    const transactionId = identifier(value.transactionId, "tx_");
    if (!Array.isArray(value.operations) || value.operations.length < 1 || value.operations.length > MAX_OPERATIONS) {
        throw new TypeError("Obsidian transaction operation count is invalid");
    }
    return {
        transactionId,
        operations: value.operations.map(parseOperation),
        // Worker trash destinations bind to the model-facing call args, whose
        // only member is `operations`; the private transactionId is an IPC
        // journal identity and must never influence that physical path.
        publicArgsHash: digest(canonicalJson({ operations: value.operations })),
    };
}

function parseOperation(raw: JsonValue, index: number): Operation {
    const value = requireJsonObject(raw);
    const op = value.op;
    if (op === "create") {
        operationKeys(value, ["op", "path", "content", "expectedHash"]);
        if (value.expectedHash !== ABSENT_HASH) throw new TypeError("create expectedHash must be absent");
        return { op, path: safePath(value.path), content: content(value.content), expectedHash: ABSENT_HASH };
    }
    if (op === "append") {
        operationKeys(value, ["op", "path", "content", "expectedHash"]);
        return { op, path: safePath(value.path), content: content(value.content), expectedHash: sha256(value.expectedHash) };
    }
    if (op === "replace") {
        operationKeys(value, ["op", "path", "find", "replace", "expectedHash"]);
        return {
            op,
            path: safePath(value.path),
            find: text(value.find, "find", MAX_CONTENT_CHARS),
            replace: content(value.replace),
            expectedHash: sha256(value.expectedHash),
        };
    }
    if (op === "patch") {
        operationKeys(value, ["op", "path", "edits", "expectedHash"]);
        if (!Array.isArray(value.edits) || value.edits.length < 1 || value.edits.length > 256) {
            throw new TypeError("invalid patch edits");
        }
        return {
            op,
            path: safePath(value.path),
            expectedHash: sha256(value.expectedHash),
            edits: value.edits.map((edit) => {
                const item = requireJsonObject(edit);
                operationKeys(item, ["startLine", "endLine", "replacement"]);
                const startLine = integer(item.startLine, "startLine", 1);
                return {
                    startLine,
                    endLine: integer(item.endLine, "endLine", startLine),
                    replacement: content(item.replacement),
                };
            }),
        };
    }
    if (op === "rename") {
        operationKeys(value, ["op", "path", "destination", "expectedHash", "expectedDestinationHash"]);
        const path = safePath(value.path);
        const destination = safePath(value.destination);
        if (path === destination) throw new TypeError("rename source equals destination");
        return {
            op,
            path,
            destination,
            expectedHash: sha256(value.expectedHash),
            expectedDestinationHash: value.expectedDestinationHash === ABSENT_HASH
                ? ABSENT_HASH : sha256(value.expectedDestinationHash),
        };
    }
    if (op === "trash") {
        operationKeys(value, ["op", "path", "expectedHash"]);
        return { op, path: safePath(value.path), expectedHash: sha256(value.expectedHash) };
    }
    throw new TypeError(`unsupported transaction operation at index ${index}`);
}

function applyLineEdits(path: string, contentValue: string, edits: LineEdit[]): string {
    const lines = contentValue.match(/[^\n]*\n|[^\n]+$/g) ?? [];
    const sorted = [...edits].sort((left, right) => left.startLine - right.startLine || left.endLine - right.endLine);
    for (let index = 0; index < sorted.length; index += 1) {
        const edit = sorted[index];
        if (edit.endLine > lines.length) throw conflict(`patch 行范围超出文件：${path}`);
        if (index > 0 && edit.startLine <= sorted[index - 1].endLine) throw conflict(`patch edits 重叠：${path}`);
    }
    for (const edit of [...sorted].reverse()) {
        const replacement = edit.replacement.match(/[^\n]*\n|[^\n]+$/g) ?? [];
        lines.splice(edit.startLine - 1, edit.endLine - edit.startLine + 1, ...replacement);
    }
    return lines.join("");
}

function resultEnvelope(invocation: Invocation, options: {
    status: string;
    output: JsonValue | null;
    summary: string;
    beforeHash: string | null;
    afterHash: string | null;
    beforeState: JsonValue | null;
    afterState: JsonValue | null;
    sideEffectFacts: JsonObject[];
    error?: JsonObject;
}): JsonObject {
    return {
        invocationId: invocation.invocationId,
        toolCallId: invocation.toolCallId,
        status: options.status,
        output: options.output,
        userVisibleSummary: options.summary,
        beforeHash: options.beforeHash,
        afterHash: options.afterHash,
        beforeState: options.beforeState,
        afterState: options.afterState,
        workspaceRevision: 0,
        artifactIds: [],
        sourceReferenceIds: [],
        sideEffectFacts: options.sideEffectFacts,
        actualOperations: [],
        error: options.error ?? null,
    };
}

function recoveryInvocation(record: Extract<InvocationJournalRecord, { state: "started" }>): Invocation {
    return {
        invocationId: record.invocationId,
        toolCallId: record.toolCallId,
        runId: record.runId,
        name: "obsidian.vault.transaction",
        arguments: {},
        argsHash: record.requestHash,
        idempotencyKey: record.invocationId,
        deadline: new Date(Date.now() + 1_000).toISOString(),
        traceId: "trace_recovery",
    };
}

function stateMap(primary: Map<string, FileState>, paths: string[], fallback?: Map<string, FileState>): JsonObject {
    const result: JsonObject = {};
    for (const path of paths) {
        const state = primary.get(path) ?? fallback?.get(path);
        if (!state) throw new Error(`transaction state missing: ${path}`);
        result[path] = { hash: state.hash, exists: state.exists };
    }
    return result;
}

function cloneState(state: FileState): FileState { return { ...state }; }
function absentState(path: string): FileState {
    return {
        path,
        exists: false,
        content: null,
        hash: ABSENT_HASH,
        diskContent: null,
        diskHash: ABSENT_HASH,
        editor: null,
        unsaved: false,
    };
}
function stateFromContent(path: string, value: string, editor: EditorHandle | null, diskContent: string): FileState {
    if (value.length > MAX_CONTENT_CHARS || diskContent.length > MAX_CONTENT_CHARS) {
        throw new Error(`Client transaction file exceeds 1 MiB: ${path}`);
    }
    const hash = digest(value);
    const diskHash = digest(diskContent);
    return {
        path,
        exists: true,
        content: value,
        hash,
        diskContent,
        diskHash,
        editor,
        unsaved: editor !== null && hash !== diskHash,
    };
}
function virtualContent(path: string, value: string, before: FileState): FileState {
    if (before.exists && before.diskContent !== null) return stateFromContent(path, value, before.editor, before.diskContent);
    if (value.length > MAX_CONTENT_CHARS) throw new Error(`Client transaction file exceeds 1 MiB: ${path}`);
    return {
        path,
        exists: true,
        content: value,
        hash: digest(value),
        diskContent: null,
        diskHash: ABSENT_HASH,
        editor: before.editor,
        unsaved: before.editor !== null,
    };
}

function transactionDiff(files: PreviewFile[]): string {
    const sections: string[] = [];
    let length = 0;
    const push = (value: string) => {
        length += value.length;
        if (length > MAX_PREVIEW_DIFF_CHARS) throw new Error("Client Tool preview diff exceeds 4 MiB");
        sections.push(value);
    };
    for (const file of files) {
        if (file.beforeContent === file.afterContent) continue;
        const before = file.beforeContent === null ? [] : diffLines(file.beforeContent);
        const after = file.afterContent === null ? [] : diffLines(file.afterContent);
        push(`--- ${file.beforeContent === null ? "/dev/null" : `a/${file.path}`}\n`);
        push(`+++ ${file.afterContent === null ? "/dev/null" : `b/${file.path}`}\n`);
        push(`@@ -${before.length === 0 ? "0,0" : `1,${before.length}`} +${after.length === 0 ? "0,0" : `1,${after.length}`} @@\n`);
        for (const line of before) push(`-${line}`);
        for (const line of after) push(`+${line}`);
    }
    const diff = sections.join("");
    if (!diff) throw new Error("Client Tool preview has no effective change");
    return diff;
}

function diffLines(value: string): string[] {
    if (!value) return [];
    const lines = value.match(/[^\n]*\n|[^\n]+$/g) ?? [];
    if (!value.endsWith("\n")) lines[lines.length - 1] = `${lines[lines.length - 1]}\n\\ No newline at end of file\n`;
    return lines;
}
function requireContent(state: FileState): string {
    if (!state.exists || state.content === null) throw conflict(`文件不存在：${state.path}`);
    return state.content;
}
function requireExpected(path: string, actual: string, expected: string): void {
    if (actual !== expected) throw conflict(`expectedHash 冲突：${path}`);
}
function requireRecordBinding(record: InvocationJournalRecord, invocation: Invocation, requestHash: string): void {
    if (record.toolCallId !== invocation.toolCallId || record.runId !== invocation.runId || record.requestHash !== requestHash) {
        throw new InvocationJournalConflict("invocation identity/request binding changed");
    }
}
function exactKeys(value: JsonObject, expected: string[]): void { operationKeys(value, expected); }
function operationKeys(value: JsonObject, expected: string[]): void {
    const keys = Object.keys(value);
    if (keys.length !== expected.length || expected.some((key) => !keys.includes(key))) {
        throw new TypeError("object contains unexpected fields");
    }
}
function identifier(value: JsonValue | undefined, prefix: string): string {
    if (typeof value !== "string" || value.length > 128 || !new RegExp(`^${prefix}[A-Za-z0-9][A-Za-z0-9_-]*$`).test(value)) {
        throw new TypeError(`invalid ${prefix} identifier`);
    }
    return value;
}
function opaqueIdentifier(value: JsonValue | undefined, name: string): string {
    if (typeof value !== "string" || !/^[A-Za-z0-9][A-Za-z0-9_-]{0,127}$/.test(value)) {
        throw new TypeError(`invalid ${name}`);
    }
    return value;
}
function toolName(value: JsonValue | undefined): string {
    if (typeof value !== "string" || !/^[A-Za-z0-9][A-Za-z0-9_.-]{2,255}$/.test(value)) {
        throw new TypeError("invalid tool name");
    }
    return value;
}
function sha256(value: JsonValue | undefined): string {
    if (typeof value !== "string" || !/^sha256:[0-9a-f]{64}$/.test(value)) throw new TypeError("invalid sha256 digest");
    return value;
}
function safePath(value: JsonValue | undefined): string {
    if (typeof value !== "string" || value.length < 1 || value.length > 1024 || value.startsWith("/") ||
        value.includes("\\") || value.includes("\0") ||
        value.split("/").some((part) => part === "" || part === "." || part === ".." || part.includes(":"))) {
        throw new TypeError("unsafe Vault-relative path");
    }
    return value;
}
function content(value: JsonValue | undefined): string {
    if (typeof value !== "string" || value.length > MAX_CONTENT_CHARS) throw new TypeError("invalid transaction content");
    return value;
}
function text(value: JsonValue | undefined, name: string, max: number): string {
    if (typeof value !== "string" || !value || value.length > max || value.includes("\0")) {
        throw new TypeError(`invalid ${name}`);
    }
    return value;
}
function integer(value: JsonValue | undefined, name: string, minimum: number): number {
    if (typeof value !== "number" || !Number.isSafeInteger(value) || value < minimum) {
        throw new TypeError(`invalid ${name}`);
    }
    return value;
}
function timestamp(value: JsonValue | undefined): string {
    if (typeof value !== "string" || !/(?:Z|[+-]\d{2}:\d{2})$/.test(value) || !Number.isFinite(Date.parse(value))) {
        throw new TypeError("invalid deadline");
    }
    return value;
}
function digest(value: string): string { return `sha256:${createHash("sha256").update(value, "utf8").digest("hex")}`; }
function internalTrashPath(
    identity: TransactionIdentity,
    publicArgsHash: string,
    index: number,
    source: string,
): string {
    const basename = source.split("/").at(-1);
    if (!basename) throw new TypeError("trash source has no basename");
    const binding = `${identity.runId}:${identity.toolCallId}:${publicArgsHash}:${index}:${source}`;
    const suffix = createHash("sha256").update(binding, "utf8").digest("hex").slice(0, 20);
    return `.trash/offeragent/${suffix}-${basename}`;
}
function conflict(message: string): Error { const error = new Error(message); error.name = "ConflictError"; return error; }
function timeoutError(): Error { const error = new Error("Client Tool deadline expired"); error.name = "TimeoutError"; return error; }
