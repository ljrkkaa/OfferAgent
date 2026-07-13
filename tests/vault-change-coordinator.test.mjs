import assert from "node:assert/strict";
import { execFile } from "node:child_process";
import { createHash } from "node:crypto";
import { mkdir, mkdtemp, readFile, realpath, rm, stat, unlink, writeFile } from "node:fs/promises";
import { promisify } from "node:util";
import os from "node:os";
import path from "node:path";
import test from "node:test";
import { createRequire } from "node:module";
import { fileURLToPath } from "node:url";

const execFileAsync = promisify(execFile);
const repositoryRoot = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..");
const require = createRequire(import.meta.url);
const { GitCheckpointStore, VaultChangeCoordinator } = require(
  path.join(repositoryRoot, "packages", "plugin", "dist", "vault-change-coordinator.js"),
);

function digest(content) {
  return `sha256:${createHash("sha256").update(content, "utf8").digest("hex")}`;
}

async function git(root, ...args) {
  const { stdout } = await execFileAsync("git", ["-C", root, ...args], {
    encoding: "utf8",
    windowsHide: true,
  });
  return stdout.trimEnd();
}

class FileVaultApi {
  constructor(root) {
    this.root = root;
    this.failPath = undefined;
  }

  async read(vaultPath) {
    try {
      const content = await readFile(path.join(this.root, vaultPath), "utf8");
      const info = await stat(path.join(this.root, vaultPath));
      return {
        content,
        modifiedVersion: `mtime:${info.mtimeMs}:size:${info.size}:hash:${digest(content)}`,
      };
    } catch (error) {
      if (error?.code === "ENOENT") return undefined;
      throw error;
    }
  }

  async canonicalize(vaultPath, exists) {
    const root = await realpath(this.root);
    const target = path.join(root, vaultPath);
    if (exists) return { root, target: await realpath(target) };
    const parent = await realpath(path.dirname(target));
    return { root, target: path.join(parent, path.basename(target)) };
  }

  async create(vaultPath, content) {
    if (this.failPath === vaultPath) throw new Error(`Injected create failure for ${vaultPath}`);
    await writeFile(path.join(this.root, vaultPath), content, { encoding: "utf8", flag: "wx" });
  }

  async modify(vaultPath, content) {
    if (this.failPath === vaultPath) throw new Error(`Injected modify failure for ${vaultPath}`);
    await writeFile(path.join(this.root, vaultPath), content, "utf8");
  }

  async remove(vaultPath) {
    await unlink(path.join(this.root, vaultPath));
  }
}

function toolCall(toolCallId, batch) {
  return {
    type: "tool_call.requested",
    protocolVersion: 1,
    eventId: `event-${toolCallId}`,
    conversationId: "conversation-change",
    agentRunId: "run-change",
    sequence: 2,
    toolCallId,
    tool: { kind: "local", name: "vault_propose_changes", arguments: batch },
  };
}

function batch(id, actions) {
  return {
    batchId: id,
    idempotencyKey: `batch-key-${id}`,
    task: `Apply logical task ${id}`,
    actions: actions.map((action, index) => ({
      actionId: `${id}-action-${index + 1}`,
      idempotencyKey: `${id}-action-key-${index + 1}`,
      ...action,
    })),
  };
}

async function fixture(t) {
  const root = await mkdtemp(path.join(os.tmpdir(), "offeragent-change-batch-"));
  await mkdir(path.join(root, "notes"), { recursive: true });
  await writeFile(path.join(root, "notes", "a.md"), "alpha\n", "utf8");
  await writeFile(path.join(root, "notes", "b.md"), "hello world\n", "utf8");
  await writeFile(path.join(root, "staged.md"), "staged base\n", "utf8");
  await writeFile(path.join(root, "unstaged.md"), "unstaged base\n", "utf8");
  await git(root, "init", "-q");
  await git(root, "config", "user.name", "OfferAgent Test");
  await git(root, "config", "user.email", "offeragent@example.invalid");
  await git(root, "add", ".");
  await git(root, "commit", "-qm", "fixture");
  await writeFile(path.join(root, "staged.md"), "staged user change\n", "utf8");
  await git(root, "add", "staged.md");
  await writeFile(path.join(root, "unstaged.md"), "unstaged user change\n", "utf8");
  const vault = new FileVaultApi(root);
  const checkpoints = new GitCheckpointStore(root);
  const coordinator = new VaultChangeCoordinator(vault, checkpoints);
  t.after(async () => rm(root, { recursive: true, force: true }));
  return { checkpoints, coordinator, root, vault };
}

test("an approved batch applies atomically through the Vault and undo restores its checkpoint", async (t) => {
  const { coordinator, root, vault } = await fixture(t);
  const a = await vault.read("notes/a.md");
  const b = await vault.read("notes/b.md");
  const proposal = batch("batch-apply", [
    {
      operation: "append",
      path: "notes/a.md",
      expectedVersion: a.modifiedVersion,
      content: "beta\n",
    },
    {
      operation: "exact_replace",
      path: "notes/b.md",
      expectedVersion: b.modifiedVersion,
      expectedContent: "world",
      replacement: "vault",
    },
    {
      operation: "create",
      path: "notes/new.md",
      expectedVersion: "missing",
      content: "new note\n",
    },
  ]);
  const branchBefore = await git(root, "branch", "--show-current");
  const headBefore = await git(root, "rev-parse", "HEAD");
  const indexBefore = await git(root, "diff", "--cached", "--binary");
  const execution = coordinator.execute(toolCall("call-apply", proposal));
  await coordinator.waitUntilPending("call-apply");
  const decision = await coordinator.decide("call-apply", "apply");
  assert.deepEqual(await execution, decision);
  assert.equal(decision.ok, true);
  assert.equal(decision.value.decision, "applied");
  assert.equal(await readFile(path.join(root, "notes", "a.md"), "utf8"), "alpha\nbeta\n");
  assert.equal(await readFile(path.join(root, "notes", "b.md"), "utf8"), "hello vault\n");
  assert.equal(await readFile(path.join(root, "notes", "new.md"), "utf8"), "new note\n");
  assert.equal(await git(root, "branch", "--show-current"), branchBefore);
  assert.equal(await git(root, "rev-parse", "HEAD"), headBefore);
  assert.equal(await git(root, "diff", "--cached", "--binary"), indexBefore);
  await git(root, "cat-file", "-e", "refs/offeragent/checkpoints/batch-apply^{commit}");
  assert.deepEqual(
    (await git(root, "ls-tree", "-r", "--name-only", "refs/offeragent/checkpoints/batch-apply"))
      .split(/\r?\n/),
    ["notes/a.md", "notes/b.md"],
  );
  assert.equal((await git(root, "status", "--short")).includes("unstaged.md"), true);
  assert.equal(
    await readFile(path.join(root, "unstaged.md"), "utf8"),
    "unstaged user change\n",
  );

  const undone = await coordinator.undo("batch-apply");
  assert.equal(undone.ok, true);
  assert.equal(undone.value.status, "undone");
  assert.equal(await readFile(path.join(root, "notes", "a.md"), "utf8"), "alpha\n");
  assert.equal(await readFile(path.join(root, "notes", "b.md"), "utf8"), "hello world\n");
  assert.equal(await vault.read("notes/new.md"), undefined);
  assert.equal(await git(root, "diff", "--cached", "--binary"), indexBefore);
});

test("rejection, stale sources, rollback, invalid operations, and guarded undo are typed", async (t) => {
  const { coordinator, root, vault } = await fixture(t);
  const original = await vault.read("notes/a.md");
  const rejectedBatch = batch("batch-reject", [
    {
      operation: "append",
      path: "notes/a.md",
      expectedVersion: original.modifiedVersion,
      content: "rejected\n",
    },
  ]);
  const rejectedExecution = coordinator.execute(toolCall("call-reject", rejectedBatch));
  await coordinator.waitUntilPending("call-reject");
  await coordinator.decide("call-reject", "reject");
  const rejected = await rejectedExecution;
  assert.equal(rejected.ok, true);
  assert.equal(rejected.value.decision, "rejected");
  assert.equal(await readFile(path.join(root, "notes", "a.md"), "utf8"), "alpha\n");
  await assert.rejects(
    git(root, "cat-file", "-e", "refs/offeragent/checkpoints/batch-reject^{commit}"),
  );

  const cancelledBatch = batch("batch-cancelled", [
    {
      operation: "append",
      path: "notes/a.md",
      expectedVersion: original.modifiedVersion,
      content: "must not apply\n",
    },
  ]);
  const cancelledExecution = coordinator.execute(toolCall("call-cancelled", cancelledBatch));
  await coordinator.waitUntilPending("call-cancelled");
  coordinator.cancel("call-cancelled");
  const cancelled = await cancelledExecution;
  assert.equal(cancelled.ok, false);
  assert.equal(cancelled.error.code, "tool_error");
  assert.equal((await coordinator.decide("call-cancelled", "apply")).ok, false);
  assert.equal(await readFile(path.join(root, "notes", "a.md"), "utf8"), "alpha\n");
  await assert.rejects(
    git(root, "cat-file", "-e", "refs/offeragent/checkpoints/batch-cancelled^{commit}"),
  );

  const staleBatch = batch("batch-stale", [
    {
      operation: "append",
      path: "notes/a.md",
      expectedVersion: original.modifiedVersion,
      content: "stale\n",
    },
  ]);
  const staleExecution = coordinator.execute(toolCall("call-stale", staleBatch));
  await coordinator.waitUntilPending("call-stale");
  await writeFile(path.join(root, "notes", "a.md"), "manual edit\n", "utf8");
  await coordinator.decide("call-stale", "apply");
  const stale = await staleExecution;
  assert.equal(stale.ok, false);
  assert.equal(stale.error.code, "stale_evidence");
  assert.equal(await readFile(path.join(root, "notes", "a.md"), "utf8"), "manual edit\n");

  await writeFile(path.join(root, "notes", "a.md"), "alpha\n", "utf8");
  const currentA = await vault.read("notes/a.md");
  const currentB = await vault.read("notes/b.md");
  const rollbackBatch = batch("batch-rollback", [
    {
      operation: "append",
      path: "notes/a.md",
      expectedVersion: currentA.modifiedVersion,
      content: "temporary\n",
    },
    {
      operation: "append",
      path: "notes/b.md",
      expectedVersion: currentB.modifiedVersion,
      content: "must fail\n",
    },
  ]);
  vault.failPath = "notes/b.md";
  const rollbackExecution = coordinator.execute(toolCall("call-rollback", rollbackBatch));
  await coordinator.waitUntilPending("call-rollback");
  await coordinator.decide("call-rollback", "apply");
  const rolledBack = await rollbackExecution;
  assert.equal(rolledBack.ok, false);
  assert.equal(rolledBack.error.code, "tool_error");
  assert.equal(await readFile(path.join(root, "notes", "a.md"), "utf8"), "alpha\n");
  assert.equal(await readFile(path.join(root, "notes", "b.md"), "utf8"), "hello world\n");
  vault.failPath = undefined;

  for (const operation of ["delete", "move", "write", "shell"]) {
    const invalid = await coordinator.execute(
      toolCall(
        `call-invalid-${operation}`,
        batch(`batch-invalid-${operation}`, [
          {
            operation,
            path: "notes/a.md",
            expectedVersion: "anything",
          },
        ]),
      ),
    );
    assert.equal(invalid.ok, false);
    assert.equal(invalid.error.code, "invalid_change");
  }
  const escaped = await coordinator.execute(
    toolCall(
      "call-external",
      batch("batch-external", [
        {
          operation: "create",
          path: "../outside.md",
          expectedVersion: "missing",
          content: "outside",
        },
      ]),
    ),
  );
  assert.equal(escaped.ok, false);
  assert.equal(escaped.error.code, "invalid_change");

  await writeFile(path.join(root, "notes", "large.md"), "x".repeat(240_000), "utf8");
  const large = await vault.read("notes/large.md");
  const oversized = await coordinator.execute(
    toolCall(
      "call-oversized",
      batch("batch-oversized", [
        {
          operation: "append",
          path: "notes/large.md",
          expectedVersion: large.modifiedVersion,
          content: "y".repeat(30_000),
        },
      ]),
    ),
  );
  assert.equal(oversized.ok, false);
  assert.equal(oversized.error.code, "request_too_large");

  const canonicalRoot = await realpath(root);
  const escapingVault = {
    read: (vaultPath) => vault.read(vaultPath),
    create: (vaultPath, content) => vault.create(vaultPath, content),
    modify: (vaultPath, content) => vault.modify(vaultPath, content),
    remove: (vaultPath) => vault.remove(vaultPath),
    canonicalize: async () => ({
      root: canonicalRoot,
      target: path.resolve(canonicalRoot, "..", "escaped.md"),
    }),
  };
  const symlinkEscape = await new VaultChangeCoordinator(escapingVault, {
    create: async () => {
      throw new Error("checkpoint must not run");
    },
    read: async () => undefined,
  }).execute(
    toolCall(
      "call-symlink-escape",
      batch("batch-symlink-escape", [
        {
          operation: "append",
          path: "notes/a.md",
          expectedVersion: (await vault.read("notes/a.md")).modifiedVersion,
          content: "escaped",
        },
      ]),
    ),
  );
  assert.equal(symlinkEscape.ok, false);
  assert.equal(symlinkEscape.error.code, "invalid_path");

  const applyAgain = batch("batch-conflict", [
    {
      operation: "append",
      path: "notes/a.md",
      expectedVersion: (await vault.read("notes/a.md")).modifiedVersion,
      content: "agent edit\n",
    },
  ]);
  const conflictExecution = coordinator.execute(toolCall("call-conflict", applyAgain));
  await coordinator.waitUntilPending("call-conflict");
  await coordinator.decide("call-conflict", "apply");
  assert.equal((await conflictExecution).ok, true);
  await writeFile(path.join(root, "notes", "a.md"), "later user edit\n", "utf8");
  const conflict = await coordinator.undo("batch-conflict");
  assert.equal(conflict.ok, false);
  assert.equal(conflict.error.code, "undo_conflict");
  assert.equal(await readFile(path.join(root, "notes", "a.md"), "utf8"), "later user edit\n");
});
