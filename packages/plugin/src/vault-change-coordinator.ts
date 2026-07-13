import { createHash } from "node:crypto";
import { execFile } from "node:child_process";
import { mkdtemp, realpath, rm } from "node:fs/promises";
import os from "node:os";
import path from "node:path";
import type { TFile, Vault } from "obsidian";
import type {
  AgentRunEvent,
  LocalToolResultPayload,
  VaultAction,
  VaultChangeBatchProposal,
  VaultChangeResult,
  VaultToolErrorCode,
  VaultUndoResultPayload,
} from "@offeragent/protocol";

const MAX_ACTIONS = 20;
const MAX_BATCH_BYTES = 131_072;
const MAX_FILE_BYTES = 262_144;
const MAX_ID_LENGTH = 128;
const MAX_PATH_LENGTH = 512;
const MAX_TASK_BYTES = 512;
const ID_PATTERN = /^[A-Za-z0-9][A-Za-z0-9_-]{0,127}$/;
const BLOCKED_SEGMENTS = new Set([".git", ".obsidian", "node_modules"]);

type ChangeToolCall = Extract<AgentRunEvent, { type: "tool_call.requested" }>;
type Failure = Extract<LocalToolResultPayload, { ok: false }>;

export interface VaultChangeFileApi {
  canonicalize(vaultPath: string, exists: boolean): Promise<{ root: string; target: string }>;
  create(vaultPath: string, content: string): Promise<void>;
  modify(vaultPath: string, content: string): Promise<void>;
  read(vaultPath: string): Promise<{ content: string; modifiedVersion: string } | undefined>;
  remove(vaultPath: string): Promise<void>;
}

export interface CheckpointStore {
  create(batchId: string, existingPaths: string[]): Promise<string>;
  read(checkpointRef: string, vaultPath: string): Promise<string | undefined>;
}

interface PreparedAction {
  action: VaultAction;
  afterContent: string;
  afterHash: string;
  beforeContent?: string;
  beforeHash: string;
}

interface PreparedBatch {
  actions: PreparedAction[];
  proposal: VaultChangeBatchProposal;
}

interface PendingBatch {
  decision?: Promise<LocalToolResultPayload>;
  prepared: PreparedBatch;
  promise: Promise<LocalToolResultPayload>;
  resolve(result: LocalToolResultPayload): void;
}

interface AppliedBatch {
  checkpointRef: string;
  prepared: PreparedBatch;
}

function failure(code: VaultToolErrorCode, message: string): Failure {
  return { ok: false, error: { code, message } };
}

function digest(content: string): string {
  return `sha256:${createHash("sha256").update(content, "utf8").digest("hex")}`;
}

function isFailure(value: PreparedBatch | Failure): value is Failure {
  return "ok" in value && value.ok === false;
}

function isContained(root: string, target: string): boolean {
  const relative = path.relative(root, target);
  return (
    relative === "" ||
    (!path.isAbsolute(relative) && relative !== ".." && !relative.startsWith(`..${path.sep}`))
  );
}

function safeVaultPath(value: unknown): string | undefined {
  if (typeof value !== "string") return undefined;
  const candidate = value.trim();
  if (
    !candidate ||
    candidate.length > MAX_PATH_LENGTH ||
    candidate.includes("\\") ||
    candidate.includes(":") ||
    candidate.includes("\0") ||
    candidate.startsWith("/")
  ) {
    return undefined;
  }
  const segments = candidate.split("/");
  if (
    segments.some(
      (segment) =>
        !segment ||
        segment === "." ||
        segment === ".." ||
        BLOCKED_SEGMENTS.has(segment.toLowerCase()),
    )
  ) {
    return undefined;
  }
  const extension = path.posix.extname(candidate).toLowerCase();
  return extension === ".md" || extension === ".txt" ? segments.join("/") : undefined;
}

function validId(value: unknown): value is string {
  return typeof value === "string" && value.length <= MAX_ID_LENGTH && ID_PATTERN.test(value);
}

function occurrences(content: string, expected: string): number {
  let count = 0;
  let offset = 0;
  while ((offset = content.indexOf(expected, offset)) !== -1) {
    count += 1;
    offset += Math.max(expected.length, 1);
  }
  return count;
}

function gitCommand(
  root: string,
  args: string[],
  environment: NodeJS.ProcessEnv = process.env,
  trimOutput = true,
): Promise<string> {
  return new Promise((resolve, reject) => {
    execFile(
      "git",
      ["-C", root, "--literal-pathspecs", ...args],
      { encoding: "utf8", env: environment, windowsHide: true, maxBuffer: 4 * 1024 * 1024 },
      (error, stdout, stderr) => {
        if (error) {
          reject(new Error(stderr.trim() || error.message));
          return;
        }
        resolve(trimOutput ? stdout.trimEnd() : stdout);
      },
    );
  });
}

export class GitCheckpointStore implements CheckpointStore {
  readonly #root: string;

  constructor(vaultRoot: string) {
    this.#root = vaultRoot;
  }

  async create(batchId: string, existingPaths: string[]): Promise<string> {
    if (!validId(batchId)) throw new Error("The checkpoint batch identifier is invalid.");
    const repositoryRoot = await gitCommand(this.#root, ["rev-parse", "--show-toplevel"]);
    if (path.resolve(repositoryRoot) !== path.resolve(this.#root)) {
      throw new Error("The Vault root must be the Git repository root.");
    }
    const temporaryDirectory = await mkdtemp(path.join(os.tmpdir(), "offeragent-git-index-"));
    const temporaryIndex = path.join(temporaryDirectory, "index");
    const environment = {
      ...process.env,
      GIT_INDEX_FILE: temporaryIndex,
      GIT_AUTHOR_NAME: "OfferAgent",
      GIT_AUTHOR_EMAIL: "offeragent@local.invalid",
      GIT_COMMITTER_NAME: "OfferAgent",
      GIT_COMMITTER_EMAIL: "offeragent@local.invalid",
    };
    const checkpointRef = `refs/offeragent/checkpoints/${batchId}`;
    try {
      await gitCommand(this.#root, ["read-tree", "--empty"], environment);
      if (existingPaths.length > 0) {
        await gitCommand(this.#root, ["add", "--force", "--", ...existingPaths], environment);
      }
      const tree = await gitCommand(this.#root, ["write-tree"], environment);
      const commit = await gitCommand(
        this.#root,
        ["commit-tree", tree, "-m", `OfferAgent checkpoint ${batchId}`],
        environment,
      );
      await gitCommand(this.#root, ["update-ref", checkpointRef, commit], environment);
      await gitCommand(this.#root, ["cat-file", "-e", `${checkpointRef}^{commit}`], environment);
      return checkpointRef;
    } finally {
      await rm(temporaryDirectory, { recursive: true, force: true });
    }
  }

  async read(checkpointRef: string, vaultPath: string): Promise<string | undefined> {
    try {
      await gitCommand(this.#root, ["cat-file", "-e", `${checkpointRef}:${vaultPath}`]);
    } catch {
      return undefined;
    }
    return gitCommand(this.#root, ["show", `${checkpointRef}:${vaultPath}`], process.env, false);
  }
}

export class ObsidianVaultChangeFileApi implements VaultChangeFileApi {
  readonly #basePath: string;
  readonly #vault: Pick<Vault, "cachedRead" | "create" | "delete" | "getFiles" | "modify">;

  constructor(
    vault: Pick<Vault, "cachedRead" | "create" | "delete" | "getFiles" | "modify">,
    basePath: string,
  ) {
    this.#vault = vault;
    this.#basePath = basePath;
  }

  async read(vaultPath: string): Promise<{ content: string; modifiedVersion: string } | undefined> {
    const file = this.#file(vaultPath);
    if (!file) return undefined;
    return {
      content: await this.#vault.cachedRead(file),
      modifiedVersion: `mtime:${file.stat.mtime}:size:${file.stat.size}`,
    };
  }

  async canonicalize(
    vaultPath: string,
    exists: boolean,
  ): Promise<{ root: string; target: string }> {
    const root = await realpath(this.#basePath);
    const unresolved = path.resolve(root, vaultPath);
    if (exists) return { root, target: await realpath(unresolved) };
    const parent = await realpath(path.dirname(unresolved));
    return { root, target: path.join(parent, path.basename(unresolved)) };
  }

  async create(vaultPath: string, content: string): Promise<void> {
    await this.#vault.create(vaultPath, content);
  }

  async modify(vaultPath: string, content: string): Promise<void> {
    const file = this.#file(vaultPath);
    if (!file) throw new Error(`Vault file '${vaultPath}' disappeared before modification.`);
    await this.#vault.modify(file, content);
  }

  async remove(vaultPath: string): Promise<void> {
    const file = this.#file(vaultPath);
    if (!file) return;
    await this.#vault.delete(file, true);
  }

  #file(vaultPath: string): TFile | undefined {
    return this.#vault.getFiles().find((candidate) => candidate.path === vaultPath);
  }
}

export class VaultChangeCoordinator {
  readonly #applied = new Map<string, AppliedBatch>();
  readonly #cancelled = new Set<string>();
  readonly #checkpoints: CheckpointStore;
  readonly #pending = new Map<string, PendingBatch>();
  readonly #preparing = new Map<string, Promise<Failure | PreparedBatch>>();
  readonly #vault: VaultChangeFileApi;

  constructor(vault: VaultChangeFileApi, checkpoints: CheckpointStore) {
    this.#vault = vault;
    this.#checkpoints = checkpoints;
  }

  async execute(event: ChangeToolCall): Promise<LocalToolResultPayload> {
    if (event.tool.name !== "vault_propose_changes") {
      return failure("invalid_change", "The Vault Change Coordinator received the wrong tool.");
    }
    const preparation = this.#prepare(event.tool.arguments);
    this.#preparing.set(event.toolCallId, preparation);
    const prepared = await preparation;
    if (this.#cancelled.delete(event.toolCallId)) {
      this.#preparing.delete(event.toolCallId);
      return failure("tool_error", "The pending Vault Change Batch was cancelled with its Agent Run.");
    }
    if (isFailure(prepared)) {
      this.#preparing.delete(event.toolCallId);
      return prepared;
    }
    let resolve!: (result: LocalToolResultPayload) => void;
    const promise = new Promise<LocalToolResultPayload>((resolveResult) => {
      resolve = resolveResult;
    });
    this.#pending.set(event.toolCallId, { prepared, promise, resolve });
    return promise;
  }

  cancel(toolCallId: string): void {
    const pending = this.#pending.get(toolCallId);
    if (pending && !pending.decision) {
      pending.resolve(
        failure("tool_error", "The pending Vault Change Batch was cancelled with its Agent Run."),
      );
      this.#pending.delete(toolCallId);
      this.#preparing.delete(toolCallId);
      return;
    }
    if (this.#preparing.has(toolCallId) && !pending) this.#cancelled.add(toolCallId);
  }

  async waitUntilPending(toolCallId: string): Promise<VaultChangeBatchProposal> {
    const preparation = this.#preparing.get(toolCallId);
    if (!preparation) throw new Error(`Vault Change '${toolCallId}' is unknown.`);
    const prepared = await preparation;
    if (isFailure(prepared)) throw new Error(prepared.error.message);
    await Promise.resolve();
    if (!this.#pending.has(toolCallId)) throw new Error(`Vault Change '${toolCallId}' is not pending.`);
    return prepared.proposal;
  }

  async decide(
    toolCallId: string,
    decision: "apply" | "reject",
  ): Promise<LocalToolResultPayload> {
    const preparation = this.#preparing.get(toolCallId);
    if (!preparation) return failure("not_found", `Vault Change '${toolCallId}' is not pending.`);
    await preparation;
    const pending = this.#pending.get(toolCallId);
    if (!pending) return failure("not_found", `Vault Change '${toolCallId}' is not pending.`);
    if (!pending.decision) {
      pending.decision = this.#finishDecision(toolCallId, pending, decision);
    }
    return pending.decision;
  }

  async undo(batchId: string): Promise<VaultUndoResultPayload> {
    const applied = this.#applied.get(batchId);
    if (!applied) return failure("not_found", `Applied Vault Change Batch '${batchId}' was not found.`);
    const current = new Map<string, string>();
    for (const prepared of applied.prepared.actions) {
      const file = await this.#vault.read(prepared.action.path);
      if (!file || digest(file.content) !== prepared.afterHash) {
        return failure(
          "undo_conflict",
          `Vault file '${prepared.action.path}' changed after the batch was applied.`,
        );
      }
      current.set(prepared.action.path, file.content);
    }
    const restored = new Map<string, string | undefined>();
    for (const prepared of applied.prepared.actions) {
      restored.set(
        prepared.action.path,
        await this.#checkpoints.read(applied.checkpointRef, prepared.action.path),
      );
    }
    const changed: string[] = [];
    try {
      for (const prepared of applied.prepared.actions) {
        const vaultPath = prepared.action.path;
        const before = restored.get(vaultPath);
        if (before === undefined) await this.#vault.remove(vaultPath);
        else await this.#vault.modify(vaultPath, before);
        changed.push(vaultPath);
      }
    } catch (error) {
      for (const vaultPath of changed.reverse()) {
        const postApplication = current.get(vaultPath);
        if (postApplication !== undefined) {
          const existing = await this.#vault.read(vaultPath);
          if (existing) await this.#vault.modify(vaultPath, postApplication);
          else await this.#vault.create(vaultPath, postApplication);
        }
      }
      return failure(
        "tool_error",
        error instanceof Error ? error.message : "Vault Change undo failed and was rolled back.",
      );
    }
    this.#applied.delete(batchId);
    return { ok: true, value: { type: "vault_change_undo", batchId, status: "undone" } };
  }

  async #finishDecision(
    toolCallId: string,
    pending: PendingBatch,
    decision: "apply" | "reject",
  ): Promise<LocalToolResultPayload> {
    let result: LocalToolResultPayload;
    try {
      result =
        decision === "reject"
          ? {
              ok: true,
              value: this.#result(pending.prepared, "rejected"),
            }
          : await this.#apply(pending.prepared);
    } catch (error) {
      result = failure(
        "tool_error",
        error instanceof Error ? error.message : "The Vault Change Batch could not be applied.",
      );
    }
    pending.resolve(result);
    this.#pending.delete(toolCallId);
    this.#preparing.delete(toolCallId);
    return result;
  }

  async #apply(original: PreparedBatch): Promise<LocalToolResultPayload> {
    const currentPreparation = await this.#prepare(original.proposal);
    if (isFailure(currentPreparation)) return currentPreparation;
    const existingPaths = currentPreparation.actions
      .filter((prepared) => prepared.beforeContent !== undefined)
      .map((prepared) => prepared.action.path);
    const checkpointRef = await this.#checkpoints.create(
      currentPreparation.proposal.batchId,
      existingPaths,
    );
    const changed: PreparedAction[] = [];
    try {
      for (const prepared of currentPreparation.actions) {
        if (prepared.beforeContent === undefined) {
          await this.#vault.create(prepared.action.path, prepared.afterContent);
        } else {
          await this.#vault.modify(prepared.action.path, prepared.afterContent);
        }
        changed.push(prepared);
      }
      for (const prepared of currentPreparation.actions) {
        const applied = await this.#vault.read(prepared.action.path);
        if (!applied || digest(applied.content) !== prepared.afterHash) {
          throw new Error(`Vault file '${prepared.action.path}' did not match its applied hash.`);
        }
      }
    } catch (error) {
      let rollbackError: unknown;
      for (const prepared of changed.reverse()) {
        try {
          if (prepared.beforeContent === undefined) await this.#vault.remove(prepared.action.path);
          else await this.#vault.modify(prepared.action.path, prepared.beforeContent);
        } catch (caught) {
          rollbackError ??= caught;
        }
      }
      const message = error instanceof Error ? error.message : "Vault Change application failed.";
      const rollbackMessage = rollbackError instanceof Error ? ` Rollback failed: ${rollbackError.message}` : "";
      return failure("tool_error", `${message}${rollbackMessage}`);
    }
    this.#applied.set(currentPreparation.proposal.batchId, {
      checkpointRef,
      prepared: currentPreparation,
    });
    return {
      ok: true,
      value: this.#result(currentPreparation, "applied", checkpointRef),
    };
  }

  #result(
    prepared: PreparedBatch,
    decision: VaultChangeResult["decision"],
    checkpointRef?: string,
  ): VaultChangeResult {
    return {
      type: "vault_propose_changes",
      batchId: prepared.proposal.batchId,
      decision,
      ...(checkpointRef ? { checkpointRef } : {}),
      targets: prepared.actions.map((item) => ({
        path: item.action.path,
        beforeHash: item.beforeHash,
        afterHash: item.afterHash,
      })),
    };
  }

  async #prepare(value: unknown): Promise<Failure | PreparedBatch> {
    if (!value || typeof value !== "object" || Array.isArray(value)) {
      return failure("invalid_change", "A Vault Change Batch must be an object.");
    }
    const proposal = value as Partial<VaultChangeBatchProposal>;
    if (
      !validId(proposal.batchId) ||
      !validId(proposal.idempotencyKey) ||
      typeof proposal.task !== "string" ||
      !proposal.task.trim() ||
      Buffer.byteLength(proposal.task, "utf8") > MAX_TASK_BYTES ||
      !Array.isArray(proposal.actions) ||
      proposal.actions.length === 0 ||
      proposal.actions.length > MAX_ACTIONS
    ) {
      return failure("invalid_change", "The Vault Change Batch metadata is invalid or unbounded.");
    }
    let encoded: string;
    try {
      encoded = JSON.stringify(value);
    } catch {
      return failure("invalid_change", "The Vault Change Batch is not serializable.");
    }
    if (Buffer.byteLength(encoded, "utf8") > MAX_BATCH_BYTES) {
      return failure("request_too_large", "The Vault Change Batch exceeds its size limit.");
    }
    const paths = new Set<string>();
    const actionIds = new Set<string>();
    const idempotencyKeys = new Set<string>();
    const actions: PreparedAction[] = [];
    for (const candidate of proposal.actions) {
      if (!candidate || typeof candidate !== "object" || Array.isArray(candidate)) {
        return failure("invalid_change", "Every Vault Action must be an object.");
      }
      const action = candidate as Partial<VaultAction>;
      const vaultPath = safeVaultPath(action.path);
      if (
        !vaultPath ||
        !validId(action.actionId) ||
        !validId(action.idempotencyKey) ||
        typeof action.expectedVersion !== "string" ||
        action.expectedVersion.length > 256 ||
        paths.has(vaultPath) ||
        actionIds.has(action.actionId) ||
        idempotencyKeys.has(action.idempotencyKey) ||
        !["append", "create", "exact_replace"].includes(action.operation as string)
      ) {
        return failure("invalid_change", "A Vault Action has an invalid or duplicate field.");
      }
      paths.add(vaultPath);
      actionIds.add(action.actionId);
      idempotencyKeys.add(action.idempotencyKey);
      const current = await this.#vault.read(vaultPath);
      try {
        const canonical = await this.#vault.canonicalize(vaultPath, current !== undefined);
        if (!isContained(canonical.root, canonical.target)) {
          return failure("invalid_path", `Vault path '${vaultPath}' resolves outside the Vault.`);
        }
      } catch {
        return failure("invalid_path", `Vault path '${vaultPath}' could not be resolved safely.`);
      }
      let afterContent: string;
      if (action.operation === "create") {
        if (
          current ||
          action.expectedVersion !== "missing" ||
          typeof (action as { content?: unknown }).content !== "string"
        ) {
          return failure("stale_evidence", `Create target '${vaultPath}' is no longer missing.`);
        }
        afterContent = (action as { content: string }).content;
      } else {
        if (!current || current.modifiedVersion !== action.expectedVersion) {
          return failure("stale_evidence", `Vault source '${vaultPath}' changed before application.`);
        }
        if (action.operation === "append") {
          if (typeof (action as { content?: unknown }).content !== "string") {
            return failure("invalid_change", `Append action '${action.actionId}' has no content.`);
          }
          afterContent = current.content + (action as { content: string }).content;
        } else {
          const exact = action as { expectedContent?: unknown; replacement?: unknown };
          if (
            typeof exact.expectedContent !== "string" ||
            !exact.expectedContent ||
            typeof exact.replacement !== "string" ||
            occurrences(current.content, exact.expectedContent) !== 1
          ) {
            return failure(
              "stale_evidence",
              `Exact replacement for '${vaultPath}' no longer has one expected match.`,
            );
          }
          afterContent = current.content.replace(exact.expectedContent, exact.replacement);
        }
      }
      if (Buffer.byteLength(afterContent, "utf8") > MAX_FILE_BYTES) {
        return failure("request_too_large", `Vault result '${vaultPath}' exceeds its size limit.`);
      }
      actions.push({
        action: { ...action, path: vaultPath } as VaultAction,
        beforeContent: current?.content,
        beforeHash: current ? digest(current.content) : "missing",
        afterContent,
        afterHash: digest(afterContent),
      });
    }
    return { proposal: proposal as VaultChangeBatchProposal, actions };
  }
}

export { MAX_ACTIONS, MAX_BATCH_BYTES, MAX_FILE_BYTES };
