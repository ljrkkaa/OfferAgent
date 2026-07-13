import { createHash } from "node:crypto";
import { execFile } from "node:child_process";
import { mkdtemp, realpath, rm, writeFile } from "node:fs/promises";
import os from "node:os";
import path from "node:path";
import type { TFile, Vault } from "obsidian";
import type {
  AgentRunEvent,
  LocalToolResultPayload,
  VaultAction,
  VaultChangeBatchProposal,
  VaultChangeJournalRecord,
  VaultChangeResult,
  VaultChangeTargetResult,
  VaultChangeTransactionState,
  VaultToolErrorCode,
  VaultUndoResultPayload,
} from "@offeragent/protocol";

const MAX_ACTIONS = 20;
const MAX_BATCH_BYTES = 131_072;
const MAX_PENDING_BYTES = MAX_BATCH_BYTES + 16_384;
const MAX_FILE_BYTES = 262_144;
const MAX_ID_LENGTH = 128;
const MAX_PATH_LENGTH = 512;
const MAX_TASK_BYTES = 512;
const ID_PATTERN = /^[A-Za-z0-9][A-Za-z0-9_-]{0,127}$/;
const BLOCKED_SEGMENTS = new Set([".git", "node_modules"]);

type ChangeToolCall = Extract<AgentRunEvent, { type: "tool_call.requested" }>;
type Failure = Extract<LocalToolResultPayload, { ok: false }>;

export type VaultPermissionMode = "ask_every_time" | "read_only" | "trusted_vault";

export interface VaultChangeFileApi {
  canonicalize(vaultPath: string, exists: boolean): Promise<{ root: string; target: string }>;
  create(vaultPath: string, content: string): Promise<void>;
  modify(vaultPath: string, content: string): Promise<void>;
  read(vaultPath: string): Promise<{ content: string; modifiedVersion: string } | undefined>;
  remove(vaultPath: string): Promise<void>;
}

export interface CheckpointStore {
  cleanup?(now?: Date): Promise<string[]>;
  create(batchId: string, existingPaths: string[]): Promise<string>;
  read(checkpointRef: string, vaultPath: string): Promise<string | undefined>;
  verify?(checkpointRef: string): Promise<boolean>;
}

export interface PendingProposalStore {
  deletePending(toolCallId: string): Promise<void>;
  loadPending(toolCallId: string): Promise<PersistedPendingProposal | undefined>;
  savePending(toolCallId: string, pending: PersistedPendingProposal): Promise<void>;
}

export interface PersistedPendingProposal {
  proposal: VaultChangeBatchProposal;
  result?: LocalToolResultPayload;
  targets: VaultChangeTargetResult[];
  version: 1;
}

export interface VaultChangeJournal {
  list(states: VaultChangeTransactionState[]): Promise<VaultChangeJournalRecord[]>;
  markApplying(
    batchId: string,
    checkpointRef: string,
    targets: VaultChangeTargetResult[],
  ): Promise<void>;
  markState(
    batchId: string,
    state: Extract<
      VaultChangeTransactionState,
      "applied" | "expired" | "recovery_failed" | "rolled_back" | "undone"
    >,
  ): Promise<void>;
}

interface PreparedAction {
  action: VaultAction;
  afterContent: string;
  afterHash: string;
  beforeContent?: string;
  beforeHash: string;
  controlFile: boolean;
}

interface PreparedBatch {
  actions: PreparedAction[];
  proposal: VaultChangeBatchProposal;
}

interface PendingBatch {
  completedResult?: LocalToolResultPayload;
  decision?: Promise<LocalToolResultPayload>;
  prepared: PreparedBatch;
  promise: Promise<LocalToolResultPayload>;
  resolve(result: LocalToolResultPayload): void;
}

interface ConfirmationRequired {
  confirmationRequired: true;
  prepared: PreparedBatch;
}

type AppliedBatch = VaultChangeJournalRecord;

const NOOP_JOURNAL: VaultChangeJournal = {
  async list() {
    return [];
  },
  async markApplying() {},
  async markState() {},
};

export class VaultChangeCrashInjectionError extends Error {
  constructor(point: string) {
    super(`Injected Vault Change crash after '${point}'.`);
    this.name = "VaultChangeCrashInjectionError";
  }
}

class VaultChangeRestoreError extends Error {
  readonly rollbackFailed: boolean;

  constructor(message: string, rollbackFailed: boolean) {
    super(message);
    this.name = "VaultChangeRestoreError";
    this.rollbackFailed = rollbackFailed;
  }
}

function failure(code: VaultToolErrorCode, message: string): Failure {
  return { ok: false, error: { code, message } };
}

function digest(content: string): string {
  return `sha256:${createHash("sha256").update(content, "utf8").digest("hex")}`;
}

function conflictDiff(
  vaultPath: string,
  current: string | undefined,
  before: string | undefined,
): string {
  const currentLines = (current ?? "").split(/\r?\n/).slice(0, 100);
  const beforeLines = (before ?? "").split(/\r?\n/).slice(0, 100);
  const lines = [
    `--- current/${vaultPath}`,
    `+++ checkpoint/${vaultPath}`,
    "@@ guarded undo @@",
    ...currentLines.map((line) => `-${line}`),
    ...beforeLines.map((line) => `+${line}`),
  ];
  const diff = lines.join("\n");
  return diff.length <= 16_384 ? diff : `${diff.slice(0, 16_370)}\n...truncated`;
}

function isFailure(value: PreparedBatch | Failure): value is Failure {
  return "ok" in value && value.ok === false;
}

function requiresConfirmation(
  value: LocalToolResultPayload | ConfirmationRequired,
): value is ConfirmationRequired {
  return "confirmationRequired" in value;
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
  const normalized = segments.join("/");
  if (isControlVaultPath(normalized) && normalized.toLowerCase().startsWith(".obsidian/")) {
    if (isProtectedPermissionPath(normalized)) return undefined;
    return [".css", ".json", ".md", ".txt"].includes(extension) ? normalized : undefined;
  }
  return extension === ".md" || extension === ".txt" ? normalized : undefined;
}

function isProtectedPermissionPath(vaultPath: string): boolean {
  return vaultPath.toLowerCase().startsWith(".obsidian/plugins/offeragent/");
}

function isControlVaultPath(vaultPath: string): boolean {
  const normalized = vaultPath.toLowerCase();
  return (
    normalized === "agent.md" ||
    normalized.startsWith(".codex/") ||
    normalized.startsWith(".obsidian/")
  );
}

function validId(value: unknown): value is string {
  return typeof value === "string" && value.length <= MAX_ID_LENGTH && ID_PATTERN.test(value);
}

function pendingProposalRef(toolCallId: string): string | undefined {
  if (typeof toolCallId !== "string" || toolCallId.length === 0 || toolCallId.length > MAX_ID_LENGTH) {
    return undefined;
  }
  return `refs/offeragent/pending/${createHash("sha256").update(toolCallId, "utf8").digest("hex")}`;
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

export class GitCheckpointStore implements CheckpointStore, PendingProposalStore {
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

  async verify(checkpointRef: string): Promise<boolean> {
    const match = /^refs\/offeragent\/checkpoints\/([A-Za-z0-9][A-Za-z0-9_-]{0,127})$/.exec(
      checkpointRef,
    );
    if (!match) return false;
    try {
      await gitCommand(this.#root, ["cat-file", "-e", `${checkpointRef}^{commit}`]);
      const subject = await gitCommand(this.#root, ["log", "-1", "--format=%s", checkpointRef]);
      return subject === `OfferAgent checkpoint ${match[1]}`;
    } catch {
      return false;
    }
  }

  async cleanup(now = new Date()): Promise<string[]> {
    const output = await gitCommand(this.#root, [
      "for-each-ref",
      "--format=%(refname)%09%(creatordate:unix)",
      "refs/offeragent/checkpoints/",
    ]);
    const entries = output
      .split(/\r?\n/)
      .filter(Boolean)
      .map((line) => {
        const [ref, timestamp] = line.split("\t");
        return { ref, timestamp: Number(timestamp) * 1_000 };
      })
      .sort((left, right) => right.timestamp - left.timestamp || left.ref.localeCompare(right.ref));
    const cutoff = now.getTime() - 30 * 24 * 60 * 60 * 1_000;
    const deleted = entries
      .filter(
        (entry, index) =>
          index >= 100 || !Number.isFinite(entry.timestamp) || entry.timestamp < cutoff,
      )
      .map((entry) => entry.ref);
    for (const checkpointRef of deleted) {
      await gitCommand(this.#root, ["update-ref", "-d", checkpointRef]);
    }
    return deleted;
  }

  async savePending(toolCallId: string, pending: PersistedPendingProposal): Promise<void> {
    const pendingRef = pendingProposalRef(toolCallId);
    if (!pendingRef) throw new Error("The pending Tool Call identifier is invalid.");
    const serialized = JSON.stringify(pending);
    if (Buffer.byteLength(serialized, "utf8") > MAX_PENDING_BYTES) {
      throw new Error("The pending Vault Change Batch is too large to persist.");
    }
    const repositoryRoot = await gitCommand(this.#root, ["rev-parse", "--show-toplevel"]);
    if (path.resolve(repositoryRoot) !== path.resolve(this.#root)) {
      throw new Error("The Vault root must be the Git repository root.");
    }
    const temporaryDirectory = await mkdtemp(path.join(os.tmpdir(), "offeragent-pending-"));
    const pendingPath = path.join(temporaryDirectory, "proposal.json");
    try {
      await writeFile(pendingPath, serialized, { encoding: "utf8", flag: "wx" });
      const blob = await gitCommand(this.#root, ["hash-object", "-w", pendingPath]);
      await gitCommand(this.#root, ["update-ref", pendingRef, blob]);
      await gitCommand(this.#root, ["cat-file", "-e", `${pendingRef}^{blob}`]);
    } finally {
      await rm(temporaryDirectory, { recursive: true, force: true });
    }
  }

  async loadPending(toolCallId: string): Promise<PersistedPendingProposal | undefined> {
    const pendingRef = pendingProposalRef(toolCallId);
    if (!pendingRef) return undefined;
    try {
      await gitCommand(this.#root, ["cat-file", "-e", `${pendingRef}^{blob}`]);
      const serialized = await gitCommand(this.#root, ["show", pendingRef], process.env, false);
      if (Buffer.byteLength(serialized, "utf8") > MAX_PENDING_BYTES) return undefined;
      const pending = JSON.parse(serialized) as PersistedPendingProposal;
      if (
        pending.version !== 1 ||
        !pending.proposal ||
        !Array.isArray(pending.proposal.actions) ||
        !Array.isArray(pending.targets) ||
        pending.targets.length !== pending.proposal.actions.length ||
        pending.targets.some((target, index) =>
          target.path !== pending.proposal.actions[index]?.path ||
          typeof target.beforeHash !== "string" ||
          typeof target.afterHash !== "string"
        )
      ) {
        return undefined;
      }
      return pending;
    } catch {
      return undefined;
    }
  }

  async deletePending(toolCallId: string): Promise<void> {
    const pendingRef = pendingProposalRef(toolCallId);
    if (!pendingRef) return;
    await gitCommand(this.#root, ["update-ref", "-d", pendingRef]);
  }
}

export class ObsidianVaultChangeFileApi implements VaultChangeFileApi {
  readonly #basePath: string;
  readonly #vault: Pick<
    Vault,
    "adapter" | "cachedRead" | "create" | "delete" | "getFiles" | "modify"
  >;

  constructor(
    vault: Pick<
      Vault,
      "adapter" | "cachedRead" | "create" | "delete" | "getFiles" | "modify"
    >,
    basePath: string,
  ) {
    this.#vault = vault;
    this.#basePath = basePath;
  }

  async read(vaultPath: string): Promise<{ content: string; modifiedVersion: string } | undefined> {
    const file = this.#file(vaultPath);
    if (file) {
      return {
        content: await this.#vault.cachedRead(file),
        modifiedVersion: `mtime:${file.stat.mtime}:size:${file.stat.size}`,
      };
    }
    if (!isControlVaultPath(vaultPath) || !(await this.#vault.adapter.exists(vaultPath, true))) {
      return undefined;
    }
    const [content, fileStat] = await Promise.all([
      this.#vault.adapter.read(vaultPath),
      this.#vault.adapter.stat(vaultPath),
    ]);
    if (!fileStat || fileStat.type !== "file") return undefined;
    return {
      content,
      modifiedVersion: `mtime:${fileStat.mtime}:size:${fileStat.size}`,
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
    if (isControlVaultPath(vaultPath) && !this.#file(vaultPath)) {
      if (await this.#vault.adapter.exists(vaultPath, true)) {
        throw new Error(`Vault file '${vaultPath}' already exists.`);
      }
      await this.#vault.adapter.write(vaultPath, content);
      return;
    }
    await this.#vault.create(vaultPath, content);
  }

  async modify(vaultPath: string, content: string): Promise<void> {
    const file = this.#file(vaultPath);
    if (file) {
      await this.#vault.modify(file, content);
      return;
    }
    if (
      isControlVaultPath(vaultPath) &&
      await this.#vault.adapter.exists(vaultPath, true)
    ) {
      await this.#vault.adapter.write(vaultPath, content);
      return;
    }
    throw new Error(`Vault file '${vaultPath}' disappeared before modification.`);
  }

  async remove(vaultPath: string): Promise<void> {
    const file = this.#file(vaultPath);
    if (file) {
      await this.#vault.delete(file, true);
      return;
    }
    if (
      isControlVaultPath(vaultPath) &&
      await this.#vault.adapter.exists(vaultPath, true)
    ) {
      await this.#vault.adapter.remove(vaultPath);
    }
  }

  #file(vaultPath: string): TFile | undefined {
    return this.#vault.getFiles().find((candidate) => candidate.path === vaultPath);
  }
}

export class VaultChangeCoordinator {
  readonly #applied = new Map<string, AppliedBatch>();
  readonly #automaticResults = new Map<string, LocalToolResultPayload>();
  readonly #authorizing = new Map<string, Promise<LocalToolResultPayload | undefined>>();
  readonly #cancelled = new Set<string>();
  readonly #completedDecisions = new Map<string, LocalToolResultPayload>();
  readonly #checkpoints: CheckpointStore;
  readonly #injectCrash: (point: string) => Promise<void> | void;
  readonly #journal: VaultChangeJournal;
  readonly #permissionMode: () => VaultPermissionMode;
  readonly #pending = new Map<string, PendingBatch>();
  readonly #pendingStore?: PendingProposalStore;
  readonly #persisting = new Map<string, Promise<void>>();
  readonly #preparing = new Map<string, Promise<Failure | PreparedBatch>>();
  readonly #vault: VaultChangeFileApi;

  constructor(
    vault: VaultChangeFileApi,
    checkpoints: CheckpointStore,
    journal: VaultChangeJournal = NOOP_JOURNAL,
    injectCrash: (point: string) => Promise<void> | void = () => {},
    permissionMode: () => VaultPermissionMode = () => "ask_every_time",
    pendingStore?: PendingProposalStore,
  ) {
    this.#vault = vault;
    this.#checkpoints = checkpoints;
    this.#journal = journal;
    this.#injectCrash = injectCrash;
    this.#permissionMode = permissionMode;
    this.#pendingStore = pendingStore;
  }

  async execute(event: ChangeToolCall): Promise<LocalToolResultPayload> {
    if (event.tool.name !== "vault_propose_changes") {
      return failure("invalid_change", "The Vault Change Coordinator received the wrong tool.");
    }
    if (this.#permissionMode() === "read_only") {
      return this.#permissionDenied();
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
    const permissionMode = this.#permissionMode();
    if (permissionMode === "read_only") {
      this.#preparing.delete(event.toolCallId);
      return this.#permissionDenied();
    }
    const automatic =
      permissionMode === "trusted_vault" &&
      prepared.actions.every(({ controlFile }) => !controlFile);
    const pending = automatic ? undefined : this.#pendingDecision(event.toolCallId, prepared);
    try {
      await this.#persistPending(event.toolCallId, prepared);
    } catch (error) {
      this.#pending.delete(event.toolCallId);
      this.#preparing.delete(event.toolCallId);
      throw error;
    }
    if (this.#cancelled.delete(event.toolCallId)) {
      const cancelled = failure(
        "tool_error",
        "The pending Vault Change Batch was cancelled with its Agent Run.",
      );
      this.#pending.get(event.toolCallId)?.resolve(cancelled);
      this.#pending.delete(event.toolCallId);
      this.#preparing.delete(event.toolCallId);
      await this.#pendingStore?.deletePending(event.toolCallId);
      return cancelled;
    }
    if (automatic) {
      let finishAuthorization!: (result: LocalToolResultPayload | undefined) => void;
      const authorization = new Promise<LocalToolResultPayload | undefined>((resolve) => {
        finishAuthorization = resolve;
      });
      this.#authorizing.set(event.toolCallId, authorization);
      let result: LocalToolResultPayload | ConfirmationRequired;
      try {
        result = await this.#apply(prepared, "automatic");
      } catch (error) {
        if (error instanceof VaultChangeCrashInjectionError) {
          finishAuthorization(undefined);
          this.#authorizing.delete(event.toolCallId);
          throw error;
        }
        result = failure(
          "tool_error",
          error instanceof Error ? error.message : "The Vault Change Batch could not be applied.",
        );
      }
      if (requiresConfirmation(result)) {
        if (this.#cancelled.delete(event.toolCallId)) {
          this.#preparing.delete(event.toolCallId);
          await this.#pendingStore?.deletePending(event.toolCallId);
          finishAuthorization(undefined);
          this.#authorizing.delete(event.toolCallId);
          return failure(
            "tool_error",
            "The pending Vault Change Batch was cancelled with its Agent Run.",
          );
        }
        const pending = this.#pendingDecision(event.toolCallId, result.prepared);
        try {
          await this.#persistPending(event.toolCallId, result.prepared);
        } catch (error) {
          this.#pending.delete(event.toolCallId);
          this.#preparing.delete(event.toolCallId);
          finishAuthorization(undefined);
          this.#authorizing.delete(event.toolCallId);
          throw error;
        }
        finishAuthorization(undefined);
        this.#authorizing.delete(event.toolCallId);
        return pending;
      }
      try {
        await this.#persistDecisionResult(event.toolCallId, prepared, result);
      } catch {
        // The proposal and apply journal already make the actual result recoverable. A failed
        // handoff overwrite must never turn a committed Vault mutation into a reported failure.
      }
      this.#cancelled.delete(event.toolCallId);
      this.#preparing.delete(event.toolCallId);
      this.#rememberAutomaticResult(event.toolCallId, result);
      finishAuthorization(result);
      this.#authorizing.delete(event.toolCallId);
      return result;
    }
    return pending!;
  }

  async #persistPending(toolCallId: string, prepared: PreparedBatch): Promise<void> {
    if (!this.#pendingStore) return;
    const persistence = this.#pendingStore.savePending(toolCallId, {
      version: 1,
      proposal: prepared.proposal,
      targets: prepared.actions.map((action) => ({
        path: action.action.path,
        beforeHash: action.beforeHash,
        afterHash: action.afterHash,
      })),
    });
    this.#persisting.set(toolCallId, persistence);
    try {
      await persistence;
    } finally {
      if (this.#persisting.get(toolCallId) === persistence) this.#persisting.delete(toolCallId);
    }
  }

  async rehydrate(toolCallId: string): Promise<{
    proposal?: VaultChangeBatchProposal;
    result?: LocalToolResultPayload;
  }> {
    await this.#preparing.get(toolCallId);
    await this.#persisting.get(toolCallId);
    const existing = this.#pending.get(toolCallId);
    if (existing) return { proposal: existing.prepared.proposal };
    const persisted = await this.#pendingStore?.loadPending(toolCallId);
    if (!persisted) {
      return {
        result: failure(
          "plugin_disconnected",
          "The Vault Change proposal was not durably prepared before the plugin disconnected.",
        ),
      };
    }
    if (!persisted.result) {
      const applied = (await this.#journal.list(["applied"])).find(
        (record) => record.batchId === persisted.proposal.batchId,
      );
      if (applied) {
        persisted.result = {
          ok: true,
          value: {
            type: "vault_propose_changes",
            batchId: applied.batchId,
            decision: "applied",
            checkpointRef: applied.checkpointRef,
            targets: applied.targets,
          },
        };
        await this.#pendingStore?.savePending(toolCallId, persisted);
      }
    }
    if (persisted.result) {
      this.#completedDecisions.set(toolCallId, persisted.result);
      return {
        proposal: persisted.proposal,
        result: persisted.result,
      };
    }
    const preparation = Promise.resolve<PreparedBatch>({
      proposal: persisted.proposal,
      actions: persisted.proposal.actions.map((action, index) => ({
        action,
        afterContent: "",
        afterHash: persisted.targets[index].afterHash,
        beforeHash: persisted.targets[index].beforeHash,
        controlFile: isControlVaultPath(action.path),
      })),
    });
    this.#preparing.set(toolCallId, preparation);
    const prepared = await preparation;
    if (isFailure(prepared)) {
      this.#preparing.delete(toolCallId);
      throw new Error(prepared.error.message);
    }
    void this.#pendingDecision(toolCallId, prepared);
    return { proposal: persisted.proposal };
  }

  #pendingDecision(toolCallId: string, prepared: PreparedBatch): Promise<LocalToolResultPayload> {
    let resolve!: (result: LocalToolResultPayload) => void;
    const promise = new Promise<LocalToolResultPayload>((resolveResult) => {
      resolve = resolveResult;
    });
    this.#pending.set(toolCallId, { prepared, promise, resolve });
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
      const persistence = this.#persisting.get(toolCallId);
      void (async () => {
        await persistence;
        await this.#pendingStore?.deletePending(toolCallId);
      })().catch(() => {});
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
    await this.#authorizing.get(toolCallId);
    await this.#persisting.get(toolCallId);
    if (!this.#pending.has(toolCallId)) throw new Error(`Vault Change '${toolCallId}' is not pending.`);
    return prepared.proposal;
  }

  async decide(
    toolCallId: string,
    decision: "apply" | "reject",
  ): Promise<LocalToolResultPayload> {
    const completedDecision = this.#completedDecisions.get(toolCallId);
    if (completedDecision) return completedDecision;
    const completedAutomaticResult = this.#automaticResults.get(toolCallId);
    if (completedAutomaticResult) return completedAutomaticResult;
    const preparation = this.#preparing.get(toolCallId);
    if (!preparation) return failure("not_found", `Vault Change '${toolCallId}' is not pending.`);
    await preparation;
    await this.#persisting.get(toolCallId);
    await Promise.resolve();
    const automaticResult = await this.#authorizing.get(toolCallId);
    if (automaticResult) return automaticResult;
    const pending = this.#pending.get(toolCallId);
    if (!pending) return failure("not_found", `Vault Change '${toolCallId}' is not pending.`);
    if (!pending.decision) {
      pending.decision =
        pending.completedResult
          ? this.#finishPendingResult(toolCallId, pending)
          : decision === "apply" && this.#permissionMode() === "read_only"
          ? this.#finishPermissionDenial(toolCallId, pending)
          : this.#finishDecision(toolCallId, pending, decision);
    }
    return pending.decision;
  }

  #rememberAutomaticResult(toolCallId: string, result: LocalToolResultPayload): void {
    this.#automaticResults.delete(toolCallId);
    this.#automaticResults.set(toolCallId, result);
    while (this.#automaticResults.size > 100) {
      const oldest = this.#automaticResults.keys().next().value as string | undefined;
      if (oldest === undefined) return;
      this.#automaticResults.delete(oldest);
    }
  }

  #permissionDenied(): Failure {
    return failure(
      "permission_denied",
      "Vault mutation was rejected because this Vault is in Read Only mode.",
    );
  }

  async #finishPermissionDenial(
    toolCallId: string,
    pending: PendingBatch,
  ): Promise<LocalToolResultPayload> {
    const result = this.#permissionDenied();
    pending.completedResult = result;
    return this.#finishPendingResult(toolCallId, pending);
  }

  async acknowledge(toolCallId: string): Promise<void> {
    await this.#persisting.get(toolCallId);
    this.#completedDecisions.delete(toolCallId);
    await this.#pendingStore?.deletePending(toolCallId);
  }

  async undo(batchId: string): Promise<VaultUndoResultPayload> {
    const applied = this.#applied.get(batchId);
    if (!applied) {
      return failure("not_found", `Applied Vault Change Batch '${batchId}' was not found.`);
    }
    const current = await this.#readCurrent(applied.targets);
    const checkpoint = await this.#readCheckpoint(applied);
    const conflicts = applied.targets.flatMap((target) => {
      const content = current.get(target.path);
      const currentHash = content === undefined ? "missing" : digest(content);
      if (currentHash === target.afterHash) return [];
      return [{
        path: target.path,
        appliedHash: target.afterHash,
        currentHash,
        diff: conflictDiff(target.path, content, checkpoint?.get(target.path)),
      }];
    });
    if (conflicts.length > 0) {
      return {
        ok: false,
        error: {
          code: "undo_conflict",
          message: "One or more Vault files changed after the batch was applied.",
          conflicts,
        },
      };
    }
    if (!checkpoint) {
      return failure("tool_error", `Git Checkpoint '${applied.checkpointRef}' is missing or damaged.`);
    }
    try {
      await this.#restoreContents(applied.targets, checkpoint, current);
    } catch (error) {
      if (error instanceof VaultChangeRestoreError && error.rollbackFailed) {
        try {
          await this.#markStateWithRetry(batchId, "recovery_failed");
        } catch {
          // The mixed file state is still reported; startup will retry journal reconciliation.
        }
        this.#applied.delete(batchId);
      }
      return failure(
        "tool_error",
        error instanceof Error ? error.message : "Vault Change undo failed.",
      );
    }
    try {
      await this.#markStateWithRetry(batchId, "undone");
    } catch (error) {
      const message = error instanceof Error ? error.message : "Vault Change undo failed.";
      return failure(
        "tool_error",
        `${message} Vault files were restored; startup recovery will confirm the journal state.`,
      );
    }
    this.#applied.delete(batchId);
    return { ok: true, value: { type: "vault_change_undo", batchId, status: "undone" } };
  }

  async reconcile(): Promise<Array<{ batchId: string; state: VaultChangeTransactionState }>> {
    const persisted = await this.#journal.list(["applying", "applied"]);
    const outcomes: Array<{ batchId: string; state: VaultChangeTransactionState }> = [];
    const records: VaultChangeJournalRecord[] = [];
    for (const record of persisted) {
      const normalized = await this.#normalizeLegacyRecord(record);
      if (normalized) {
        records.push(normalized);
        continue;
      }
      const state = record.state === "applied" ? "expired" : "recovery_failed";
      await this.#markStateWithRetry(record.batchId, state);
      outcomes.push({ batchId: record.batchId, state });
    }
    for (const record of records.filter((candidate) => candidate.state === "applied")) {
      const checkpoint = await this.#readCheckpoint(record);
      if (!checkpoint) {
        await this.#markStateWithRetry(record.batchId, "expired");
        outcomes.push({ batchId: record.batchId, state: "expired" });
        continue;
      }
      const current = await this.#readCurrent(record.targets);
      const hashes = record.targets.map((target) => {
        const content = current.get(target.path);
        return content === undefined ? "missing" : digest(content);
      });
      if (hashes.every((hash, index) => hash === record.targets[index].afterHash)) {
        this.#applied.set(record.batchId, record);
        outcomes.push({ batchId: record.batchId, state: "applied" });
      } else if (hashes.every((hash, index) => hash === record.targets[index].beforeHash)) {
        await this.#markStateWithRetry(record.batchId, "undone");
        outcomes.push({ batchId: record.batchId, state: "undone" });
      } else {
        this.#applied.set(record.batchId, record);
        outcomes.push({ batchId: record.batchId, state: "applied" });
      }
    }
    for (const record of records.filter((candidate) => candidate.state === "applying")) {
      const current = await this.#readCurrent(record.targets);
      const hashes = record.targets.map((target) => {
        const content = current.get(target.path);
        return content === undefined ? "missing" : digest(content);
      });
      const allBefore = hashes.every((hash, index) => hash === record.targets[index].beforeHash);
      if (allBefore) {
        await this.#markStateWithRetry(record.batchId, "rolled_back");
        outcomes.push({ batchId: record.batchId, state: "rolled_back" });
        continue;
      }
      const checkpoint = await this.#readCheckpoint(record);
      const knownState = hashes.every(
        (hash, index) =>
          hash === record.targets[index].beforeHash || hash === record.targets[index].afterHash,
      );
      if (!checkpoint || !knownState) {
        await this.#markStateWithRetry(record.batchId, "recovery_failed");
        outcomes.push({ batchId: record.batchId, state: "recovery_failed" });
        continue;
      }
      const allAfter = hashes.every((hash, index) => hash === record.targets[index].afterHash);
      if (allAfter) {
        await this.#markStateWithRetry(record.batchId, "applied");
        const applied = { ...record, state: "applied" as const };
        this.#applied.set(record.batchId, applied);
        outcomes.push({ batchId: record.batchId, state: "applied" });
        continue;
      }
      try {
        await this.#restoreContents(record.targets, checkpoint, current);
        await this.#markStateWithRetry(record.batchId, "rolled_back");
        outcomes.push({ batchId: record.batchId, state: "rolled_back" });
      } catch {
        await this.#markStateWithRetry(record.batchId, "recovery_failed");
        outcomes.push({ batchId: record.batchId, state: "recovery_failed" });
      }
    }
    await this.#cleanupCheckpoints(outcomes);
    return outcomes;
  }

  async #finishDecision(
    toolCallId: string,
    pending: PendingBatch,
    decision: "apply" | "reject",
  ): Promise<LocalToolResultPayload> {
    let result: LocalToolResultPayload | ConfirmationRequired;
    try {
      result =
        decision === "reject"
          ? {
              ok: true,
              value: this.#result(pending.prepared, "rejected"),
            }
          : await this.#apply(pending.prepared, "confirmed");
      if (requiresConfirmation(result)) {
        result = failure("tool_error", "A confirmed Vault Change unexpectedly lost authorization.");
      }
    } catch (error) {
      if (error instanceof VaultChangeCrashInjectionError) throw error;
      result = failure(
        "tool_error",
        error instanceof Error ? error.message : "The Vault Change Batch could not be applied.",
      );
    }
    pending.completedResult = result;
    return this.#finishPendingResult(toolCallId, pending);
  }

  async #finishPendingResult(
    toolCallId: string,
    pending: PendingBatch,
  ): Promise<LocalToolResultPayload> {
    const result = pending.completedResult;
    if (!result) throw new Error(`Vault Change '${toolCallId}' has no completed decision.`);
    try {
      await this.#persistDecisionResult(toolCallId, pending.prepared, result);
    } catch (error) {
      pending.decision = undefined;
      throw error;
    }
    pending.resolve(result);
    this.#pending.delete(toolCallId);
    this.#preparing.delete(toolCallId);
    return result;
  }

  async #persistDecisionResult(
    toolCallId: string,
    prepared: PreparedBatch,
    result: LocalToolResultPayload,
  ): Promise<void> {
    await this.#pendingStore?.savePending(toolCallId, {
      version: 1,
      proposal: prepared.proposal,
      result,
      targets: prepared.actions.map((action) => ({
        path: action.action.path,
        beforeHash: action.beforeHash,
        afterHash: action.afterHash,
      })),
    });
    this.#completedDecisions.set(toolCallId, result);
  }

  async #apply(
    original: PreparedBatch,
    authorization: "automatic" | "confirmed",
  ): Promise<LocalToolResultPayload | ConfirmationRequired> {
    const currentPreparation = await this.#prepare(original.proposal);
    if (isFailure(currentPreparation)) return currentPreparation;
    const permissionMode = this.#permissionMode();
    if (permissionMode === "read_only") return this.#permissionDenied();
    if (
      authorization === "automatic" &&
      (permissionMode !== "trusted_vault" ||
        currentPreparation.actions.some(({ controlFile }) => controlFile))
    ) {
      return { confirmationRequired: true, prepared: currentPreparation };
    }
    const existingPaths = currentPreparation.actions
      .filter((prepared) => prepared.beforeContent !== undefined)
      .map((prepared) => prepared.action.path);
    const checkpointRef = await this.#checkpoints.create(
      currentPreparation.proposal.batchId,
      existingPaths,
    );
    const targets = this.#targets(currentPreparation);
    await this.#journal.markApplying(currentPreparation.proposal.batchId, checkpointRef, targets);
    await this.#injectCrash("applying");
    const changed: PreparedAction[] = [];
    try {
      for (const [index, prepared] of currentPreparation.actions.entries()) {
        if (prepared.beforeContent === undefined) {
          await this.#vault.create(prepared.action.path, prepared.afterContent);
        } else {
          await this.#vault.modify(prepared.action.path, prepared.afterContent);
        }
        changed.push(prepared);
        await this.#injectCrash(`action:${index}`);
      }
      for (const prepared of currentPreparation.actions) {
        const applied = await this.#vault.read(prepared.action.path);
        if (!applied || digest(applied.content) !== prepared.afterHash) {
          throw new Error(`Vault file '${prepared.action.path}' did not match its applied hash.`);
        }
      }
    } catch (error) {
      if (error instanceof VaultChangeCrashInjectionError) throw error;
      let rollbackError: unknown;
      for (const prepared of changed.reverse()) {
        try {
          if (prepared.beforeContent === undefined) await this.#vault.remove(prepared.action.path);
          else await this.#vault.modify(prepared.action.path, prepared.beforeContent);
        } catch (caught) {
          rollbackError ??= caught;
        }
      }
      try {
        await this.#journal.markState(
          currentPreparation.proposal.batchId,
          rollbackError ? "recovery_failed" : "rolled_back",
        );
      } catch (caught) {
        rollbackError ??= caught;
      }
      const message = error instanceof Error ? error.message : "Vault Change application failed.";
      const rollbackMessage = rollbackError instanceof Error ? ` Rollback failed: ${rollbackError.message}` : "";
      return failure("tool_error", `${message}${rollbackMessage}`);
    }
    try {
      await this.#markStateWithRetry(currentPreparation.proposal.batchId, "applied");
    } catch (error) {
      const message = error instanceof Error ? error.message : "Vault Change journal update failed.";
      return failure(
        "tool_error",
        `${message} Vault files were applied; startup recovery will confirm the journal state.`,
      );
    }
    this.#applied.set(currentPreparation.proposal.batchId, {
      batchId: currentPreparation.proposal.batchId,
      checkpointRef,
      state: "applied",
      targets,
    });
    try {
      await this.#cleanupCheckpoints();
    } catch {
      // Retention is retried during startup reconciliation and every later successful apply.
    }
    await this.#injectCrash("applied");
    return {
      ok: true,
      value: this.#result(currentPreparation, "applied", checkpointRef),
    };
  }

  #targets(prepared: PreparedBatch): VaultChangeTargetResult[] {
    return prepared.actions.map((item) => ({
      path: item.action.path,
      beforeHash: item.beforeHash,
      afterHash: item.afterHash,
    }));
  }

  async #readCurrent(targets: VaultChangeTargetResult[]): Promise<Map<string, string | undefined>> {
    const current = new Map<string, string | undefined>();
    for (const target of targets) {
      current.set(target.path, (await this.#vault.read(target.path))?.content);
    }
    return current;
  }

  async #readCheckpoint(
    record: Pick<VaultChangeJournalRecord, "checkpointRef" | "targets">,
  ): Promise<Map<string, string | undefined> | undefined> {
    if (this.#checkpoints.verify && !(await this.#checkpoints.verify(record.checkpointRef))) {
      return undefined;
    }
    const contents = new Map<string, string | undefined>();
    for (const target of record.targets) {
      const content = await this.#checkpoints.read(record.checkpointRef, target.path);
      const hash = content === undefined ? "missing" : digest(content);
      if (hash !== target.beforeHash) return undefined;
      contents.set(target.path, content);
    }
    return contents;
  }

  async #normalizeLegacyRecord(
    record: VaultChangeJournalRecord,
  ): Promise<VaultChangeJournalRecord | undefined> {
    if (record.targets.every((target) => target.beforeHash)) return record;
    if (this.#checkpoints.verify && !(await this.#checkpoints.verify(record.checkpointRef))) {
      return undefined;
    }
    const targets: VaultChangeTargetResult[] = [];
    for (const target of record.targets) {
      if (target.beforeHash) {
        targets.push(target);
        continue;
      }
      const content = await this.#checkpoints.read(record.checkpointRef, target.path);
      if (content === undefined) return undefined;
      targets.push({ ...target, beforeHash: digest(content) });
    }
    return { ...record, targets };
  }

  async #markStateWithRetry(
    batchId: string,
    state: Extract<
      VaultChangeTransactionState,
      "applied" | "expired" | "recovery_failed" | "rolled_back" | "undone"
    >,
  ): Promise<void> {
    try {
      await this.#journal.markState(batchId, state);
    } catch {
      await this.#journal.markState(batchId, state);
    }
  }

  async #cleanupCheckpoints(
    outcomes?: Array<{ batchId: string; state: VaultChangeTransactionState }>,
  ): Promise<void> {
    const deleted = (await this.#checkpoints.cleanup?.()) ?? [];
    if (deleted.length === 0) return;
    const applied = await this.#journal.list(["applied"]);
    for (const record of applied.filter((candidate) => deleted.includes(candidate.checkpointRef))) {
      await this.#markStateWithRetry(record.batchId, "expired");
      this.#applied.delete(record.batchId);
      outcomes?.push({ batchId: record.batchId, state: "expired" });
    }
  }

  async #restoreContents(
    targets: VaultChangeTargetResult[],
    desired: Map<string, string | undefined>,
    rollback: Map<string, string | undefined>,
    expectedHash: "afterHash" | "beforeHash" = "beforeHash",
  ): Promise<void> {
    const changed: string[] = [];
    try {
      for (const target of targets) {
        await this.#writeContent(target.path, desired.get(target.path));
        changed.push(target.path);
      }
      const restored = await this.#readCurrent(targets);
      for (const target of targets) {
        const content = restored.get(target.path);
        const hash = content === undefined ? "missing" : digest(content);
        if (hash !== target[expectedHash]) {
          throw new Error(`Vault file '${target.path}' did not match its recovered hash.`);
        }
      }
    } catch (error) {
      let rollbackError: unknown;
      for (const vaultPath of changed.reverse()) {
        try {
          await this.#writeContent(vaultPath, rollback.get(vaultPath));
        } catch (caught) {
          rollbackError ??= caught;
        }
      }
      const message = error instanceof Error ? error.message : "Vault Change recovery failed.";
      if (rollbackError instanceof Error) {
        throw new VaultChangeRestoreError(
          `${message} Rollback failed: ${rollbackError.message}`,
          true,
        );
      }
      throw new VaultChangeRestoreError(message, false);
    }
  }

  async #writeContent(vaultPath: string, content: string | undefined): Promise<void> {
    const current = await this.#vault.read(vaultPath);
    if (content === undefined) {
      if (current) await this.#vault.remove(vaultPath);
    } else if (current) {
      await this.#vault.modify(vaultPath, content);
    } else {
      await this.#vault.create(vaultPath, content);
    }
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
      let controlFile = isControlVaultPath(vaultPath);
      try {
        const canonical = await this.#vault.canonicalize(vaultPath, current !== undefined);
        if (!isContained(canonical.root, canonical.target)) {
          return failure("invalid_path", `Vault path '${vaultPath}' resolves outside the Vault.`);
        }
        const canonicalVaultPath = path
          .relative(canonical.root, canonical.target)
          .split(path.sep)
          .join("/");
        if (isProtectedPermissionPath(canonicalVaultPath)) {
          return failure("invalid_path", `Vault path '${vaultPath}' resolves to protected settings.`);
        }
        controlFile ||= isControlVaultPath(canonicalVaultPath);
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
        controlFile,
        afterContent,
        afterHash: digest(afterContent),
      });
    }
    return { proposal: proposal as VaultChangeBatchProposal, actions };
  }
}

export { MAX_ACTIONS, MAX_BATCH_BYTES, MAX_FILE_BYTES };
