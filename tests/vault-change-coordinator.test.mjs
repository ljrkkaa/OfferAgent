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
const {
  GitCheckpointStore,
  ObsidianVaultChangeFileApi,
  VaultChangeCoordinator,
  VaultChangeCrashInjectionError,
} = require(
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
    if (this.failPath === vaultPath || this.failWhen?.(vaultPath, content)) {
      throw new Error(`Injected modify failure for ${vaultPath}`);
    }
    await writeFile(path.join(this.root, vaultPath), content, "utf8");
  }

  async remove(vaultPath) {
    await unlink(path.join(this.root, vaultPath));
  }
}

class MemoryJournal {
  constructor() {
    this.entries = new Map();
  }

  async list(states) {
    return [...this.entries.values()]
      .filter((entry) => states.includes(entry.state))
      .map((entry) => structuredClone(entry));
  }

  async markApplying(batchId, checkpointRef, targets) {
    this.entries.set(batchId, {
      batchId,
      checkpointRef,
      state: "applying",
      targets: structuredClone(targets),
    });
  }

  async markState(batchId, state) {
    if (this.failState === state) throw new Error(`Injected journal failure for ${state}`);
    const entry = this.entries.get(batchId);
    if (!entry) throw new Error(`Journal batch '${batchId}' does not exist.`);
    if (entry.state === state) return;
    entry.state = state;
    if (this.commitThenThrowState === state) {
      this.commitThenThrowState = undefined;
      throw new Error(`Injected lost response after committing ${state}`);
    }
  }
}

class FailOnceDecisionStore {
  records = new Map();
  failed = false;

  async savePending(toolCallId, pending) {
    if (pending.result && !this.failed) {
      this.failed = true;
      throw new Error("Injected pending-result persistence failure");
    }
    this.records.set(toolCallId, structuredClone(pending));
  }

  async loadPending(toolCallId) {
    const value = this.records.get(toolCallId);
    return value ? structuredClone(value) : undefined;
  }

  async deletePending(toolCallId) {
    this.records.delete(toolCallId);
  }
}

class MemoryPendingStore {
  records = new Map();

  async savePending(toolCallId, pending) {
    this.records.set(toolCallId, structuredClone(pending));
  }

  async loadPending(toolCallId) {
    const value = this.records.get(toolCallId);
    return value ? structuredClone(value) : undefined;
  }

  async deletePending(toolCallId) {
    this.records.delete(toolCallId);
  }
}

class FailOnceAutomaticResultStore extends MemoryPendingStore {
  failed = false;

  async savePending(toolCallId, pending) {
    if (pending.result && !this.failed) {
      this.failed = true;
      throw new Error("Injected automatic-result persistence failure");
    }
    await super.savePending(toolCallId, pending);
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

async function fixture(t, journal, injectCrash, getPermissionMode) {
  const root = await mkdtemp(path.join(os.tmpdir(), "offeragent-change-batch-"));
  await mkdir(path.join(root, "notes"), { recursive: true });
  await mkdir(path.join(root, ".obsidian"), { recursive: true });
  await mkdir(path.join(root, ".codex", "skills", "study"), { recursive: true });
  await writeFile(path.join(root, "agent.md"), "# Agent contract\n", "utf8");
  await writeFile(path.join(root, ".obsidian", "app.json"), "{}\n", "utf8");
  await writeFile(
    path.join(root, ".codex", "skills", "study", "SKILL.md"),
    "# Study skill\n",
    "utf8",
  );
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
  const coordinator = new VaultChangeCoordinator(
    vault,
    checkpoints,
    journal,
    injectCrash,
    getPermissionMode,
    checkpoints,
  );
  t.after(async () => rm(root, { recursive: true, force: true }));
  return { checkpoints, coordinator, root, vault };
}

test("Planning Memory delete and index update apply atomically and undo restores both", async (t) => {
  const journal = new MemoryJournal();
  const { checkpoints, coordinator, root, vault } = await fixture(t, journal, () => {}, () => "trusted_vault");
  await mkdir(path.join(root, "memory", "study"), { recursive: true });
  const topicPath = "memory/study/old.md";
  const indexPath = "memory/MEMORY.md";
  const topic = "---\nname: \"Old\"\ndescription: \"Superseded\"\ntype: study\n---\nOld direction.\n";
  const index = "# Planning Memory\n\n- [Old](study/old.md) - Superseded\n";
  await writeFile(path.join(root, topicPath), topic, "utf8");
  await writeFile(path.join(root, indexPath), index, "utf8");
  const proposal = batch("memory-delete", [
    {
      operation: "delete",
      path: topicPath,
      expectedVersion: (await vault.read(topicPath)).modifiedVersion,
    },
    {
      operation: "exact_replace",
      path: indexPath,
      expectedVersion: (await vault.read(indexPath)).modifiedVersion,
      expectedContent: index,
      replacement: "# Planning Memory\n",
    },
  ]);
  const execution = coordinator.execute(toolCall("memory-delete-tool", proposal));
  await coordinator.waitUntilPending("memory-delete-tool");
  const applied = await coordinator.decide("memory-delete-tool", "apply");
  await execution;
  assert.equal(applied.ok, true);
  assert.equal(applied.value.decision, "applied");
  await assert.rejects(readFile(path.join(root, topicPath), "utf8"), { code: "ENOENT" });
  assert.equal(await readFile(path.join(root, indexPath), "utf8"), "# Planning Memory\n");
  assert.equal((await coordinator.undo(proposal.batchId)).ok, true);
  assert.equal(await readFile(path.join(root, topicPath), "utf8"), topic);
  assert.equal(await readFile(path.join(root, indexPath), "utf8"), index);

  const invalid = await coordinator.execute(toolCall("memory-no-index-tool", batch("memory-no-index", [{
    operation: "delete",
    path: topicPath,
    expectedVersion: (await vault.read(topicPath)).modifiedVersion,
  }])));
  assert.equal(invalid.ok, false);
  assert.equal(invalid.error.code, "invalid_change");
  const staleIndex = await coordinator.execute(toolCall("memory-stale-index-tool", batch("memory-stale-index", [
    {
      operation: "delete",
      path: topicPath,
      expectedVersion: (await vault.read(topicPath)).modifiedVersion,
    },
    {
      operation: "exact_replace",
      path: indexPath,
      expectedVersion: (await vault.read(indexPath)).modifiedVersion,
      expectedContent: index,
      replacement: index,
    },
  ])));
  assert.equal(staleIndex.ok, false);
  assert.equal(staleIndex.error.code, "invalid_change");

  const malformedTopic = await coordinator.execute(toolCall("memory-malformed-tool", batch("memory-malformed", [
    {
      operation: "create",
      path: "memory/study/malformed.md",
      expectedVersion: "missing",
      content: "---\nname: \"\"\ndescription: \"\"\ntype: study\n---\nInvisible.\n",
    },
    {
      operation: "exact_replace",
      path: indexPath,
      expectedVersion: (await vault.read(indexPath)).modifiedVersion,
      expectedContent: index,
      replacement: `${index}- [Malformed](study/malformed.md) - Invalid\n`,
    },
  ])));
  assert.equal(malformedTopic.ok, false);
  assert.equal(malformedTopic.error.code, "invalid_change");

  const staleMetadataIndex = await coordinator.execute(toolCall("memory-stale-metadata-index-tool", batch("memory-stale-metadata-index", [
    {
      operation: "create",
      path: "memory/study/current.md",
      expectedVersion: "missing",
      content: "---\nname: \"Current\"\ndescription: \"Current direction\"\ntype: study\n---\nCurrent direction.\n",
    },
    {
      operation: "exact_replace",
      path: indexPath,
      expectedVersion: (await vault.read(indexPath)).modifiedVersion,
      expectedContent: index,
      replacement: `${index}- [Obsolete](study/current.md) - Superseded direction\n`,
    },
  ])));
  assert.equal(staleMetadataIndex.ok, false);
  assert.equal(staleMetadataIndex.error.code, "invalid_change");

  const multilineMetadata = await coordinator.execute(toolCall("memory-multiline-metadata-tool", batch("memory-multiline-metadata", [
    {
      operation: "create",
      path: "memory/study/multiline.md",
      expectedVersion: "missing",
      content: "---\nname: \"Line\\nBreak\"\ndescription: \"Current direction\"\ntype: study\n---\nCurrent direction.\n",
    },
    {
      operation: "exact_replace",
      path: indexPath,
      expectedVersion: (await vault.read(indexPath)).modifiedVersion,
      expectedContent: index,
      replacement: `${index}- [Line Break](study/multiline.md) - Current direction\n`,
    },
  ])));
  assert.equal(multilineMetadata.ok, false);
  assert.equal(multilineMetadata.error.code, "invalid_change");

  vault.failPath = indexPath;
  const rollbackProposal = batch("memory-delete-rollback", [
    {
      operation: "delete",
      path: topicPath,
      expectedVersion: (await vault.read(topicPath)).modifiedVersion,
    },
    {
      operation: "exact_replace",
      path: indexPath,
      expectedVersion: (await vault.read(indexPath)).modifiedVersion,
      expectedContent: index,
      replacement: "# Planning Memory\n",
    },
  ]);
  const rollbackExecution = coordinator.execute(toolCall("memory-delete-rollback-tool", rollbackProposal));
  await coordinator.waitUntilPending("memory-delete-rollback-tool");
  const rolledBack = await coordinator.decide("memory-delete-rollback-tool", "apply");
  await rollbackExecution;
  assert.equal(rolledBack.ok, false);
  assert.equal(rolledBack.error.code, "tool_error");
  assert.equal(await readFile(path.join(root, topicPath), "utf8"), topic);
  assert.equal(await readFile(path.join(root, indexPath), "utf8"), index);
  assert.equal(journal.entries.get(rollbackProposal.batchId).state, "rolled_back");
  vault.failPath = undefined;

  const validNewTopic = "---\nname: \"New\"\ndescription: \"Valid topic\"\ntype: study\n---\nNew direction.\n";
  const plainTextLink = await coordinator.execute(toolCall("memory-plain-link-tool", batch("memory-plain-link", [
    {
      operation: "create",
      path: "memory/study/new.md",
      expectedVersion: "missing",
      content: validNewTopic,
    },
    {
      operation: "exact_replace",
      path: indexPath,
      expectedVersion: (await vault.read(indexPath)).modifiedVersion,
      expectedContent: index,
      replacement: `${index}\n- ![New](study/new.md) - An image is not an index link.\n`,
    },
  ])));
  assert.equal(plainTextLink.ok, false);
  assert.equal(plainTextLink.error.code, "invalid_change");

  const otherPath = "memory/study/other.md";
  const other = "---\nname: \"Other\"\ndescription: \"Unchanged topic\"\ntype: study\n---\nOther direction.\n";
  await writeFile(path.join(root, otherPath), other, "utf8");
  const indexWithOther = `${index.trimEnd()}\n- [Other](study/other.md) - Unchanged topic\n`;
  await writeFile(path.join(root, indexPath), indexWithOther, "utf8");
  const dropsUnchanged = await coordinator.execute(toolCall("memory-drop-other-tool", batch("memory-drop-other", [
    {
      operation: "exact_replace",
      path: topicPath,
      expectedVersion: (await vault.read(topicPath)).modifiedVersion,
      expectedContent: topic,
      replacement: topic.replace("Old direction.", "Updated direction."),
    },
    {
      operation: "exact_replace",
      path: indexPath,
      expectedVersion: (await vault.read(indexPath)).modifiedVersion,
      expectedContent: indexWithOther,
      replacement: index,
    },
  ])));
  assert.equal(dropsUnchanged.ok, false);
  assert.equal(dropsUnchanged.error.code, "invalid_change");

  const aliasingVault = Object.create(vault);
  aliasingVault.canonicalize = async (vaultPath, exists) => {
    if (vaultPath === topicPath) {
      const rootPath = await realpath(root);
      return { root: rootPath, target: await realpath(path.join(root, "notes", "a.md")) };
    }
    return vault.canonicalize(vaultPath, exists);
  };
  const aliasCoordinator = new VaultChangeCoordinator(
    aliasingVault,
    checkpoints,
    journal,
    () => {},
    () => "trusted_vault",
    checkpoints,
  );
  const canonicalEscape = await aliasCoordinator.execute(toolCall("memory-canonical-delete-tool", batch("memory-canonical-delete", [
    {
      operation: "delete",
      path: topicPath,
      expectedVersion: (await vault.read(topicPath)).modifiedVersion,
    },
    {
      operation: "exact_replace",
      path: indexPath,
      expectedVersion: (await vault.read(indexPath)).modifiedVersion,
      expectedContent: indexWithOther,
      replacement: "# Planning Memory\n\n- [Other](study/other.md) - Unchanged topic\n",
    },
  ])));
  assert.equal(canonicalEscape.ok, false);
  assert.equal(canonicalEscape.error.code, "invalid_path");
});

test("a failed Daily and Study Memory batch rolls every target back atomically", async (t) => {
  const journal = new MemoryJournal();
  const { coordinator, root, vault } = await fixture(t, journal, () => {}, () => "trusted_vault");
  const dailyPath = "daily/2026-07-15.md";
  const topicPath = "memory/study/retrieval-evaluation.md";
  const indexPath = "memory/MEMORY.md";
  const daily = "# 2026-07-15\n\n## 今日学习计划\n";
  const topic = "---\nname: \"Retrieval Evaluation\"\ndescription: \"Current cross-day direction\"\ntype: study\n---\n\nCurrent direction: Retrieval Evaluation.\n";
  const index = "# Planning Memory\n\n- [Retrieval Evaluation](study/retrieval-evaluation.md) - Current cross-day direction\n";
  await mkdir(path.join(root, "daily"), { recursive: true });
  await mkdir(path.join(root, "memory", "study"), { recursive: true });
  await writeFile(path.join(root, dailyPath), daily, "utf8");
  await writeFile(path.join(root, topicPath), topic, "utf8");
  await writeFile(path.join(root, indexPath), index, "utf8");
  vault.failPath = topicPath;

  const proposal = batch("daily-study-rollback", [
    {
      operation: "exact_replace",
      path: dailyPath,
      expectedVersion: (await vault.read(dailyPath)).modifiedVersion,
      expectedContent: daily,
      replacement: `${daily}\n- [ ] Retrieval Evaluation\n`,
    },
    {
      operation: "exact_replace",
      path: topicPath,
      expectedVersion: (await vault.read(topicPath)).modifiedVersion,
      expectedContent: topic,
      replacement: topic.replace("Current direction: Retrieval Evaluation.", "Current direction: RAG evaluation."),
    },
    {
      operation: "exact_replace",
      path: indexPath,
      expectedVersion: (await vault.read(indexPath)).modifiedVersion,
      expectedContent: index,
      replacement: index,
    },
  ]);
  const result = await coordinator.execute(toolCall("daily-study-rollback-tool", proposal));

  assert.equal(result.ok, false);
  assert.equal(result.error.code, "tool_error");
  assert.equal(await readFile(path.join(root, dailyPath), "utf8"), daily);
  assert.equal(await readFile(path.join(root, topicPath), "utf8"), topic);
  assert.equal(await readFile(path.join(root, indexPath), "utf8"), index);
  assert.equal(journal.entries.get(proposal.batchId).state, "rolled_back");
});

test("a pending confirmation is rehydrated from plugin-owned durable storage", async (t) => {
  const journal = new MemoryJournal();
  const { checkpoints, coordinator, root, vault } = await fixture(
    t,
    journal,
    () => {},
    () => "ask_every_time",
  );
  const proposal = batch("rehydrated-pending", [{
    operation: "append",
    path: "notes/a.md",
    expectedVersion: (await vault.read("notes/a.md")).modifiedVersion,
    content: "restored\n",
  }]);
  const execution = coordinator.execute(toolCall("rehydrated-tool-call", proposal));
  await coordinator.waitUntilPending("rehydrated-tool-call");
  assert.equal(await settledWithin(execution, 25), "still-pending");
  await writeFile(path.join(root, "notes", "a.md"), "user edit while offline\n", "utf8");

  const restarted = new VaultChangeCoordinator(
    vault,
    checkpoints,
    journal,
    () => {},
    () => "ask_every_time",
    checkpoints,
  );
  assert.deepEqual(await restarted.rehydrate("rehydrated-tool-call"), { proposal });
  const result = await restarted.decide("rehydrated-tool-call", "reject");
  assert.equal(result.ok, true);
  assert.equal(result.value.decision, "rejected");
  assert.equal(
    await readFile(path.join(root, "notes", "a.md"), "utf8"),
    "user edit while offline\n",
  );
  const handoffRestart = new VaultChangeCoordinator(
    vault,
    checkpoints,
    journal,
    () => {},
    () => "ask_every_time",
    checkpoints,
  );
  assert.deepEqual(await handoffRestart.rehydrate("rehydrated-tool-call"), {
    proposal,
    result,
  });
  assert.deepEqual(await handoffRestart.decide("rehydrated-tool-call", "apply"), result);
  await handoffRestart.acknowledge("rehydrated-tool-call");
  assert.equal(await checkpoints.loadPending("rehydrated-tool-call"), undefined);
});

test("a requested proposal without plugin state becomes an explicit-Resume failure", async (t) => {
  const { checkpoints, vault } = await fixture(t, new MemoryJournal());
  const restarted = new VaultChangeCoordinator(
    vault,
    checkpoints,
    new MemoryJournal(),
    () => {},
    () => "ask_every_time",
    new MemoryPendingStore(),
  );
  assert.deepEqual(await restarted.rehydrate("commit-before-publish-tool"), {
    result: {
      ok: false,
      error: {
        code: "plugin_disconnected",
        message: "The Vault Change proposal was not durably prepared before the plugin disconnected.",
      },
    },
  });
});

test("a failed decision-result save retries without reapplying or stranding the live Run", async (t) => {
  const journal = new MemoryJournal();
  const { checkpoints, root, vault } = await fixture(t, journal, () => {}, () => "ask_every_time");
  const pendingStore = new FailOnceDecisionStore();
  const coordinator = new VaultChangeCoordinator(
    vault,
    checkpoints,
    journal,
    () => {},
    () => "ask_every_time",
    pendingStore,
  );
  const proposal = batch("retry-decision", [{
    operation: "append",
    path: "notes/a.md",
    expectedVersion: (await vault.read("notes/a.md")).modifiedVersion,
    content: "must not apply\n",
  }]);
  const execution = coordinator.execute(toolCall("retry-decision-tool", proposal));
  await coordinator.waitUntilPending("retry-decision-tool");
  await assert.rejects(
    coordinator.decide("retry-decision-tool", "reject"),
    /Injected pending-result persistence failure/,
  );
  assert.equal(await settledWithin(execution, 25), "still-pending");
  const result = await coordinator.decide("retry-decision-tool", "apply");
  assert.equal(result.ok, true);
  assert.equal(result.value.decision, "rejected");
  assert.deepEqual(await execution, result);
  assert.deepEqual(pendingStore.records.get("retry-decision-tool").result, result);
  assert.equal(await readFile(path.join(root, "notes", "a.md"), "utf8"), "alpha\n");
});

async function settledWithin(promise, milliseconds = 100) {
  return Promise.race([
    promise,
    new Promise((resolve) => setTimeout(() => resolve("still-pending"), milliseconds)),
  ]);
}

test("the Obsidian adapter handles hidden control configuration through the official adapter", async (t) => {
  const root = await mkdtemp(path.join(os.tmpdir(), "offeragent-control-adapter-"));
  t.after(() => rm(root, { recursive: true, force: true }));
  await mkdir(path.join(root, ".obsidian"), { recursive: true });
  await writeFile(path.join(root, ".obsidian", "app.json"), "{}\n", "utf8");
  const adapter = {
    async exists(vaultPath) {
      try {
        await stat(path.join(root, vaultPath));
        return true;
      } catch (error) {
        if (error?.code === "ENOENT") return false;
        throw error;
      }
    },
    async read(vaultPath) {
      return readFile(path.join(root, vaultPath), "utf8");
    },
    async remove(vaultPath) {
      await unlink(path.join(root, vaultPath));
    },
    async stat(vaultPath) {
      try {
        const info = await stat(path.join(root, vaultPath));
        return { type: info.isFile() ? "file" : "folder", mtime: info.mtimeMs, size: info.size };
      } catch (error) {
        if (error?.code === "ENOENT") return null;
        throw error;
      }
    },
    async write(vaultPath, content) {
      await writeFile(path.join(root, vaultPath), content, "utf8");
    },
  };
  const api = new ObsidianVaultChangeFileApi({
    adapter,
    async cachedRead() { throw new Error("hidden control files must use the adapter"); },
    async create() { throw new Error("hidden control files must use the adapter"); },
    async delete() { throw new Error("hidden control files must use the adapter"); },
    getFiles() { return []; },
    async modify() { throw new Error("hidden control files must use the adapter"); },
  }, root);
  const original = await api.read(".obsidian/app.json");
  assert.equal(original.content, "{}\n");
  assert.match(original.modifiedVersion, /^mtime:/);
  await api.modify(".obsidian/app.json", '{"alwaysUpdateLinks":true}\n');
  assert.equal(await readFile(path.join(root, ".obsidian", "app.json"), "utf8"), '{"alwaysUpdateLinks":true}\n');
  await api.create(".obsidian/new-config.json", "{}\n");
  assert.equal(await readFile(path.join(root, ".obsidian", "new-config.json"), "utf8"), "{}\n");
  await api.remove(".obsidian/new-config.json");
  assert.equal(await adapter.exists(".obsidian/new-config.json"), false);
});

test("plugin-owned permission modes enforce the complete Vault mutation matrix", async (t) => {
  await t.test("Trusted Vault auto-applies normal notes with checkpoint and undo", async (t) => {
    const journal = new MemoryJournal();
    const { coordinator, root, vault } = await fixture(
      t,
      journal,
      undefined,
      () => "trusted_vault",
    );
    const original = await vault.read("notes/a.md");
    const proposal = batch("batch-trusted-normal", [{
      operation: "append",
      path: "notes/a.md",
      expectedVersion: original.modifiedVersion,
      content: "trusted\n",
    }]);
    const result = await settledWithin(
      coordinator.execute(toolCall("call-trusted-normal", proposal)),
      3_000,
    );
    assert.notEqual(result, "still-pending");
    assert.equal(result.ok, true);
    assert.equal(result.value.decision, "applied");
    assert.match(result.value.checkpointRef, /batch-trusted-normal$/);
    assert.equal(await readFile(path.join(root, "notes", "a.md"), "utf8"), "alpha\ntrusted\n");
    assert.equal((await coordinator.undo(proposal.batchId)).ok, true);
  });

  await t.test("Trusted Vault persists proposal and result around the automatic side effect", async (t) => {
    const journal = new MemoryJournal();
    const pendingStore = new MemoryPendingStore();
    const { checkpoints, root, vault } = await fixture(t, journal);
    const originalMarkApplying = journal.markApplying.bind(journal);
    journal.markApplying = async (...arguments_) => {
      const persisted = pendingStore.records.get("call-trusted-durable");
      assert.equal(persisted?.proposal.batchId, "batch-trusted-durable");
      assert.equal(persisted?.result, undefined);
      await originalMarkApplying(...arguments_);
    };
    const coordinator = new VaultChangeCoordinator(
      vault,
      checkpoints,
      journal,
      undefined,
      () => "trusted_vault",
      pendingStore,
    );
    const original = await vault.read("notes/a.md");
    const proposal = batch("batch-trusted-durable", [{
      operation: "append",
      path: "notes/a.md",
      expectedVersion: original.modifiedVersion,
      content: "durable\n",
    }]);

    const result = await coordinator.execute(toolCall("call-trusted-durable", proposal));
    assert.equal(result.ok, true);
    assert.deepEqual(pendingStore.records.get("call-trusted-durable")?.result, result);
    assert.equal(await readFile(path.join(root, "notes", "a.md"), "utf8"), "alpha\ndurable\n");
  });

  await t.test("Trusted Vault reports the applied result when its handoff overwrite fails", async (t) => {
    const journal = new MemoryJournal();
    const pendingStore = new FailOnceAutomaticResultStore();
    const { checkpoints, root, vault } = await fixture(t, journal);
    const coordinator = new VaultChangeCoordinator(
      vault,
      checkpoints,
      journal,
      undefined,
      () => "trusted_vault",
      pendingStore,
    );
    const original = await vault.read("notes/a.md");
    const proposal = batch("batch-trusted-handoff-failure", [{
      operation: "append",
      path: "notes/a.md",
      expectedVersion: original.modifiedVersion,
      content: "applied once\n",
    }]);

    const result = await coordinator.execute(
      toolCall("call-trusted-handoff-failure", proposal),
    );
    assert.equal(result.ok, true);
    assert.equal(result.value.decision, "applied");
    assert.equal(pendingStore.failed, true);
    assert.equal(pendingStore.records.get("call-trusted-handoff-failure")?.result, undefined);
    assert.deepEqual(
      await coordinator.decide("call-trusted-handoff-failure", "apply"),
      result,
    );
    assert.equal(
      await readFile(path.join(root, "notes", "a.md"), "utf8"),
      "alpha\napplied once\n",
    );

    const restarted = new VaultChangeCoordinator(
      vault,
      checkpoints,
      journal,
      undefined,
      () => "trusted_vault",
      pendingStore,
    );
    assert.deepEqual(await restarted.rehydrate("call-trusted-handoff-failure"), {
      proposal,
      result,
    });
  });

  await t.test("a concurrent Apply click observes the successful Trusted auto-apply", async (t) => {
    let targetReads = 0;
    let releaseFinalRead;
    let reportFinalRead;
    const finalReadStarted = new Promise((resolve) => (reportFinalRead = resolve));
    const finalReadRelease = new Promise((resolve) => (releaseFinalRead = resolve));
    const journal = new MemoryJournal();
    const { checkpoints, root, vault } = await fixture(t, journal);
    const guardedVault = {
      canonicalize: (...arguments_) => vault.canonicalize(...arguments_),
      create: (...arguments_) => vault.create(...arguments_),
      modify: (...arguments_) => vault.modify(...arguments_),
      async read(vaultPath) {
        if (vaultPath === "notes/a.md" && ++targetReads === 2) {
          reportFinalRead();
          await finalReadRelease;
        }
        return vault.read(vaultPath);
      },
      remove: (...arguments_) => vault.remove(...arguments_),
    };
    const coordinator = new VaultChangeCoordinator(
      guardedVault,
      checkpoints,
      journal,
      undefined,
      () => "trusted_vault",
    );
    const original = await vault.read("notes/a.md");
    const proposal = batch("batch-trusted-click-race", [{
      operation: "append",
      path: "notes/a.md",
      expectedVersion: original.modifiedVersion,
      content: "exactly once\n",
    }]);
    const execution = coordinator.execute(toolCall("call-trusted-click-race", proposal));
    await finalReadStarted;
    const decision = coordinator.decide("call-trusted-click-race", "apply");
    releaseFinalRead();

    const [executionResult, decisionResult] = await Promise.all([execution, decision]);
    assert.equal(executionResult.ok, true);
    assert.equal(executionResult.value.decision, "applied");
    assert.deepEqual(decisionResult, executionResult);
    assert.deepEqual(
      await coordinator.decide("call-trusted-click-race", "apply"),
      executionResult,
    );
    assert.equal(
      await readFile(path.join(root, "notes", "a.md"), "utf8"),
      "alpha\nexactly once\n",
    );
  });

  await t.test("Trusted Vault still requires one confirmation for a mixed control batch", async (t) => {
    const journal = new MemoryJournal();
    const { coordinator, root, vault } = await fixture(
      t,
      journal,
      undefined,
      () => "trusted_vault",
    );
    const note = await vault.read("notes/a.md");
    const contract = await vault.read("agent.md");
    const skill = await vault.read(".codex/skills/study/SKILL.md");
    const proposal = batch("batch-trusted-control", [
      {
        operation: "append",
        path: "notes/a.md",
        expectedVersion: note.modifiedVersion,
        content: "normal\n",
      },
      {
        operation: "append",
        path: "agent.md",
        expectedVersion: contract.modifiedVersion,
        content: "control\n",
      },
      {
        operation: "append",
        path: ".codex/skills/study/SKILL.md",
        expectedVersion: skill.modifiedVersion,
        content: "control\n",
      },
    ]);
    const execution = coordinator.execute(toolCall("call-trusted-control", proposal));
    await coordinator.waitUntilPending("call-trusted-control");
    assert.equal(await settledWithin(execution, 25), "still-pending");
    assert.equal((await coordinator.decide("call-trusted-control", "apply")).ok, true);
    assert.equal((await execution).ok, true);
    assert.equal(await readFile(path.join(root, "agent.md"), "utf8"), "# Agent contract\ncontrol\n");
    assert.equal(
      await readFile(path.join(root, ".codex", "skills", "study", "SKILL.md"), "utf8"),
      "# Study skill\ncontrol\n",
    );
  });

  await t.test("Obsidian configuration is a confirm-only control target", async (t) => {
    const { coordinator, root, vault } = await fixture(
      t,
      new MemoryJournal(),
      undefined,
      () => "trusted_vault",
    );
    const config = await vault.read(".obsidian/app.json");
    const proposal = batch("batch-obsidian-control", [{
      operation: "exact_replace",
      path: ".obsidian/app.json",
      expectedVersion: config.modifiedVersion,
      expectedContent: "{}",
      replacement: '{"alwaysUpdateLinks":true}',
    }]);
    const execution = coordinator.execute(toolCall("call-obsidian-control", proposal));
    await coordinator.waitUntilPending("call-obsidian-control");
    assert.equal(await settledWithin(execution, 25), "still-pending");
    assert.equal((await coordinator.decide("call-obsidian-control", "apply")).ok, true);
    assert.equal((await execution).ok, true);
    assert.equal(
      await readFile(path.join(root, ".obsidian", "app.json"), "utf8"),
      '{"alwaysUpdateLinks":true}\n',
    );
  });

  await t.test("a normal-looking alias resolving to a control file still requires confirmation", async (t) => {
    const journal = new MemoryJournal();
    const { checkpoints, root, vault } = await fixture(t, journal);
    await writeFile(path.join(root, "notes", "alias.md"), "# Agent contract\n", "utf8");
    const aliasingVault = {
      canonicalize: async (vaultPath, exists) =>
        vaultPath === "notes/alias.md"
          ? { root: await realpath(root), target: await realpath(path.join(root, "agent.md")) }
          : vault.canonicalize(vaultPath, exists),
      create: (...arguments_) => vault.create(...arguments_),
      modify: (...arguments_) => vault.modify(...arguments_),
      read: (...arguments_) => vault.read(...arguments_),
      remove: (...arguments_) => vault.remove(...arguments_),
    };
    const coordinator = new VaultChangeCoordinator(
      aliasingVault,
      checkpoints,
      journal,
      undefined,
      () => "trusted_vault",
    );
    const alias = await aliasingVault.read("notes/alias.md");
    const proposal = batch("batch-control-alias", [{
      operation: "append",
      path: "notes/alias.md",
      expectedVersion: alias.modifiedVersion,
      content: "control alias\n",
    }]);
    const execution = coordinator.execute(toolCall("call-control-alias", proposal));
    await coordinator.waitUntilPending("call-control-alias");
    assert.equal(await settledWithin(execution, 25), "still-pending");
    assert.equal((await coordinator.decide("call-control-alias", "reject")).ok, true);
    assert.equal(await readFile(path.join(root, "notes", "alias.md"), "utf8"), "# Agent contract\n");
  });

  await t.test("Ask Every Time keeps a normal note pending", async (t) => {
    const { coordinator, vault } = await fixture(
      t,
      new MemoryJournal(),
      undefined,
      () => "ask_every_time",
    );
    const original = await vault.read("notes/a.md");
    const proposal = batch("batch-ask-normal", [{
      operation: "append",
      path: "notes/a.md",
      expectedVersion: original.modifiedVersion,
      content: "ask\n",
    }]);
    const execution = coordinator.execute(toolCall("call-ask-normal", proposal));
    await coordinator.waitUntilPending("call-ask-normal");
    assert.equal(await settledWithin(execution, 25), "still-pending");
    assert.equal((await coordinator.decide("call-ask-normal", "reject")).ok, true);
  });

  await t.test("Read Only rejects mutation and ignores an Agent escalation field", async (t) => {
    const { coordinator, root, vault } = await fixture(
      t,
      new MemoryJournal(),
      undefined,
      () => "read_only",
    );
    const original = await vault.read("notes/a.md");
    const proposal = {
      ...batch("batch-read-only", [{
        operation: "append",
        path: "notes/a.md",
        expectedVersion: original.modifiedVersion,
        content: "forbidden\n",
      }]),
      permissionMode: "trusted_vault",
    };
    const result = await settledWithin(coordinator.execute(toolCall("call-read-only", proposal)));
    assert.notEqual(result, "still-pending");
    assert.equal(result.ok, false);
    assert.equal(result.error.code, "permission_denied");
    assert.match(result.error.message, /Read Only/i);
    assert.equal(await readFile(path.join(root, "notes", "a.md"), "utf8"), "alpha\n");
  });

  await t.test("switching to Read Only invalidates an already pending Apply", async (t) => {
    let mode = "ask_every_time";
    const { coordinator, root, vault } = await fixture(
      t,
      new MemoryJournal(),
      undefined,
      () => mode,
    );
    const original = await vault.read("notes/a.md");
    const proposal = batch("batch-mode-switch", [{
      operation: "append",
      path: "notes/a.md",
      expectedVersion: original.modifiedVersion,
      content: "forbidden after switch\n",
    }]);
    const execution = coordinator.execute(toolCall("call-mode-switch", proposal));
    await coordinator.waitUntilPending("call-mode-switch");
    mode = "read_only";
    const decision = await coordinator.decide("call-mode-switch", "apply");
    assert.equal(decision.ok, false);
    assert.equal(decision.error.code, "permission_denied");
    assert.deepEqual(await execution, decision);
    assert.equal(await readFile(path.join(root, "notes", "a.md"), "utf8"), "alpha\n");
  });

  for (const tightenedMode of ["ask_every_time", "read_only"]) {
    await t.test(`a switch to ${tightenedMode} during final validation blocks Trusted auto-apply`, async (t) => {
      let mode = "trusted_vault";
      let targetReads = 0;
      let releaseFinalRead;
      let reportFinalRead;
      const finalReadStarted = new Promise((resolve) => (reportFinalRead = resolve));
      const finalReadRelease = new Promise((resolve) => (releaseFinalRead = resolve));
      const journal = new MemoryJournal();
      const { checkpoints, root, vault } = await fixture(t, journal);
      const guardedVault = {
        canonicalize: (...arguments_) => vault.canonicalize(...arguments_),
        create: (...arguments_) => vault.create(...arguments_),
        modify: (...arguments_) => vault.modify(...arguments_),
        async read(vaultPath) {
          if (vaultPath === "notes/a.md" && ++targetReads === 2) {
            reportFinalRead();
            await finalReadRelease;
          }
          return vault.read(vaultPath);
        },
        remove: (...arguments_) => vault.remove(...arguments_),
      };
      const coordinator = new VaultChangeCoordinator(
        guardedVault,
        checkpoints,
        journal,
        undefined,
        () => mode,
      );
      const original = await vault.read("notes/a.md");
      const proposal = batch(`batch-final-${tightenedMode}`, [{
        operation: "append",
        path: "notes/a.md",
        expectedVersion: original.modifiedVersion,
        content: "must not auto-apply\n",
      }]);
      const execution = coordinator.execute(toolCall(`call-final-${tightenedMode}`, proposal));
      await finalReadStarted;
      mode = tightenedMode;
      releaseFinalRead();
      if (tightenedMode === "ask_every_time") {
        await new Promise((resolve) => setImmediate(resolve));
        await coordinator.waitUntilPending(`call-final-${tightenedMode}`);
        assert.equal(await settledWithin(execution, 25), "still-pending");
        assert.equal((await coordinator.decide(`call-final-${tightenedMode}`, "reject")).ok, true);
      } else {
        const result = await execution;
        assert.equal(result.ok, false);
        assert.equal(result.error.code, "permission_denied");
      }
      assert.equal(await readFile(path.join(root, "notes", "a.md"), "utf8"), "alpha\n");
    });
  }

  await t.test("policy never authorizes external paths or unsupported operations", async (t) => {
    const { coordinator } = await fixture(
      t,
      new MemoryJournal(),
      undefined,
      () => "trusted_vault",
    );
    const external = await coordinator.execute(toolCall("call-policy-external", batch("batch-policy-external", [{
      operation: "create",
      path: "../outside.md",
      expectedVersion: "missing",
      content: "forbidden\n",
    }])));
    assert.equal(external.ok, false);
    assert.ok(["invalid_change", "invalid_path"].includes(external.error.code));
    const unsupported = await coordinator.execute(toolCall("call-policy-delete", batch("batch-policy-delete", [{
      operation: "delete",
      path: "notes/a.md",
      expectedVersion: "anything",
    }])));
    assert.equal(unsupported.ok, false);
    assert.equal(unsupported.error.code, "invalid_change");
    const permissionStore = await coordinator.execute(toolCall(
      "call-policy-settings",
      batch("batch-policy-settings", [{
        operation: "create",
        path: ".obsidian/plugins/offeragent/data.json",
        expectedVersion: "missing",
        content: '{"vaultPermissionMode":"trusted_vault"}',
      }]),
    ));
    assert.equal(permissionStore.ok, false);
    assert.ok(["invalid_change", "invalid_path"].includes(permissionStore.error.code));
  });
});

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
  assert.equal(conflict.error.conflicts[0].path, "notes/a.md");
  assert.match(conflict.error.conflicts[0].diff, /later user edit/);
  assert.equal(await readFile(path.join(root, "notes", "a.md"), "utf8"), "later user edit\n");
});

test("restart reconciliation converges every injected crash boundary and rehydrates undo", async (t) => {
  for (const crashPoint of ["applying", "action:0", "action:1", "action:2", "applied"]) {
    await t.test(crashPoint, async (t) => {
      const journal = new MemoryJournal();
      const { checkpoints, coordinator, root, vault } = await fixture(
        t,
        journal,
        (point) => {
          if (point === crashPoint) throw new VaultChangeCrashInjectionError(point);
        },
      );
      const a = await vault.read("notes/a.md");
      const b = await vault.read("notes/b.md");
      const proposal = batch(`batch-crash-${crashPoint.replace(":", "-")}`, [
        {
          operation: "append",
          path: "notes/a.md",
          expectedVersion: a.modifiedVersion,
          content: "after-a\n",
        },
        {
          operation: "exact_replace",
          path: "notes/b.md",
          expectedVersion: b.modifiedVersion,
          expectedContent: "world",
          replacement: "after-b",
        },
        {
          operation: "create",
          path: "notes/crash.md",
          expectedVersion: "missing",
          content: "after-c\n",
        },
      ]);
      const execution = coordinator.execute(toolCall(`call-crash-${crashPoint}`, proposal));
      void execution;
      await coordinator.waitUntilPending(`call-crash-${crashPoint}`);
      await assert.rejects(
        coordinator.decide(`call-crash-${crashPoint}`, "apply"),
        VaultChangeCrashInjectionError,
      );
      if (crashPoint === "applied") {
        for (const target of journal.entries.get(proposal.batchId).targets) {
          if (target.beforeHash !== "missing") target.beforeHash = "";
        }
      }
      const restarted = new VaultChangeCoordinator(vault, checkpoints, journal);
      const outcomes = await restarted.reconcile();
      const state = journal.entries.get(proposal.batchId).state;
      assert.ok(state === "applied" || state === "rolled_back");
      assert.ok(outcomes.some((outcome) => outcome.batchId === proposal.batchId));
      const contents = [
        await readFile(path.join(root, "notes", "a.md"), "utf8"),
        await readFile(path.join(root, "notes", "b.md"), "utf8"),
        await vault.read("notes/crash.md"),
      ];
      if (state === "applied") {
        assert.deepEqual(contents, ["alpha\nafter-a\n", "hello after-b\n", {
          content: "after-c\n",
          modifiedVersion: contents[2].modifiedVersion,
        }]);
        assert.equal((await restarted.undo(proposal.batchId)).ok, true);
        assert.equal(await readFile(path.join(root, "notes", "a.md"), "utf8"), "alpha\n");
        assert.equal(await vault.read("notes/crash.md"), undefined);
      } else {
        assert.equal(contents[0], "alpha\n");
        assert.equal(contents[1], "hello world\n");
        assert.equal(contents[2], undefined);
      }
    });
  }
});

test("missing or damaged checkpoints fail recovery without overwriting unknown content", async (t) => {
  const journal = new MemoryJournal();
  const { checkpoints, coordinator, root, vault } = await fixture(
    t,
    journal,
    (point) => {
      if (point === "action:0") throw new VaultChangeCrashInjectionError(point);
    },
  );
  const a = await vault.read("notes/a.md");
  const b = await vault.read("notes/b.md");
  const proposal = batch("batch-missing-checkpoint", [
    { operation: "append", path: "notes/a.md", expectedVersion: a.modifiedVersion, content: "partial\n" },
    { operation: "append", path: "notes/b.md", expectedVersion: b.modifiedVersion, content: "not-run\n" },
  ]);
  void coordinator.execute(toolCall("call-missing-checkpoint", proposal));
  await coordinator.waitUntilPending("call-missing-checkpoint");
  await assert.rejects(
    coordinator.decide("call-missing-checkpoint", "apply"),
    VaultChangeCrashInjectionError,
  );
  await git(root, "update-ref", "-d", `refs/offeragent/checkpoints/${proposal.batchId}`);
  await writeFile(path.join(root, "notes", "a.md"), "later unknown edit\n", "utf8");

  const restarted = new VaultChangeCoordinator(vault, checkpoints, journal);
  const outcomes = await restarted.reconcile();
  assert.deepEqual(outcomes, [{ batchId: proposal.batchId, state: "recovery_failed" }]);
  assert.equal(journal.entries.get(proposal.batchId).state, "recovery_failed");
  assert.equal(await readFile(path.join(root, "notes", "a.md"), "utf8"), "later unknown edit\n");
  assert.equal(await readFile(path.join(root, "notes", "b.md"), "utf8"), "hello world\n");
});

test("a damaged non-commit checkpoint is reported without further Vault mutation", async (t) => {
  const journal = new MemoryJournal();
  const { checkpoints, coordinator, root, vault } = await fixture(
    t,
    journal,
    (point) => {
      if (point === "action:0") throw new VaultChangeCrashInjectionError(point);
    },
  );
  const a = await vault.read("notes/a.md");
  const b = await vault.read("notes/b.md");
  const proposal = batch("batch-damaged-checkpoint", [
    { operation: "append", path: "notes/a.md", expectedVersion: a.modifiedVersion, content: "partial\n" },
    { operation: "append", path: "notes/b.md", expectedVersion: b.modifiedVersion, content: "not-run\n" },
  ]);
  void coordinator.execute(toolCall("call-damaged-checkpoint", proposal));
  await coordinator.waitUntilPending("call-damaged-checkpoint");
  await assert.rejects(
    coordinator.decide("call-damaged-checkpoint", "apply"),
    VaultChangeCrashInjectionError,
  );
  const blob = await git(root, "hash-object", "-w", "notes/a.md");
  await git(root, "update-ref", `refs/offeragent/checkpoints/${proposal.batchId}`, blob);
  const beforeRecovery = await readFile(path.join(root, "notes", "a.md"), "utf8");

  const restarted = new VaultChangeCoordinator(vault, checkpoints, journal);
  assert.deepEqual(await restarted.reconcile(), [
    { batchId: proposal.batchId, state: "recovery_failed" },
  ]);
  assert.equal(await readFile(path.join(root, "notes", "a.md"), "utf8"), beforeRecovery);
  assert.equal(await readFile(path.join(root, "notes", "b.md"), "utf8"), "hello world\n");
});

test("ambiguous applied and undone responses are retried idempotently", async (t) => {
  const journal = new MemoryJournal();
  journal.commitThenThrowState = "applied";
  const { coordinator, root, vault } = await fixture(t, journal);
  const original = await vault.read("notes/a.md");
  const proposal = batch("batch-ambiguous-response", [{
    operation: "append",
    path: "notes/a.md",
    expectedVersion: original.modifiedVersion,
    content: "agent edit\n",
  }]);
  const execution = coordinator.execute(toolCall("call-ambiguous-response", proposal));
  await coordinator.waitUntilPending("call-ambiguous-response");
  assert.equal((await coordinator.decide("call-ambiguous-response", "apply")).ok, true);
  assert.equal((await execution).ok, true);
  assert.equal(journal.entries.get(proposal.batchId).state, "applied");
  assert.equal(await readFile(path.join(root, "notes", "a.md"), "utf8"), "alpha\nagent edit\n");

  journal.commitThenThrowState = "undone";
  assert.equal((await coordinator.undo(proposal.batchId)).ok, true);
  assert.equal(journal.entries.get(proposal.batchId).state, "undone");
  assert.equal(await readFile(path.join(root, "notes", "a.md"), "utf8"), "alpha\n");
});

test("an unconfirmed undo state write is reconciled without destructive compensation", async (t) => {
  const journal = new MemoryJournal();
  const { checkpoints, coordinator, root, vault } = await fixture(t, journal);
  const original = await vault.read("notes/a.md");
  const proposal = batch("batch-undo-journal-failure", [{
    operation: "append",
    path: "notes/a.md",
    expectedVersion: original.modifiedVersion,
    content: "agent edit\n",
  }]);
  const execution = coordinator.execute(toolCall("call-undo-journal-failure", proposal));
  await coordinator.waitUntilPending("call-undo-journal-failure");
  await coordinator.decide("call-undo-journal-failure", "apply");
  assert.equal((await execution).ok, true);
  journal.failState = "undone";
  const result = await coordinator.undo(proposal.batchId);
  assert.equal(result.ok, false);
  assert.equal(result.error.code, "tool_error");
  assert.equal(await readFile(path.join(root, "notes", "a.md"), "utf8"), "alpha\n");
  assert.equal(journal.entries.get(proposal.batchId).state, "applied");
  journal.failState = undefined;
  const restarted = new VaultChangeCoordinator(vault, checkpoints, journal);
  assert.deepEqual(await restarted.reconcile(), [
    { batchId: proposal.batchId, state: "undone" },
  ]);
  assert.equal(journal.entries.get(proposal.batchId).state, "undone");
});

test("restart preserves guarded undo and conflict diff after a later user edit", async (t) => {
  const journal = new MemoryJournal();
  const { checkpoints, coordinator, root, vault } = await fixture(t, journal);
  const original = await vault.read("notes/a.md");
  const proposal = batch("batch-restart-user-edit", [{
    operation: "append",
    path: "notes/a.md",
    expectedVersion: original.modifiedVersion,
    content: "agent edit\n",
  }]);
  const execution = coordinator.execute(toolCall("call-restart-user-edit", proposal));
  await coordinator.waitUntilPending("call-restart-user-edit");
  assert.equal((await coordinator.decide("call-restart-user-edit", "apply")).ok, true);
  assert.equal((await execution).ok, true);
  await writeFile(path.join(root, "notes", "a.md"), "later user edit\n", "utf8");

  const restarted = new VaultChangeCoordinator(vault, checkpoints, journal);
  assert.deepEqual(await restarted.reconcile(), [
    { batchId: proposal.batchId, state: "applied" },
  ]);
  assert.equal(journal.entries.get(proposal.batchId).state, "applied");
  const undo = await restarted.undo(proposal.batchId);
  assert.equal(undo.ok, false);
  assert.equal(undo.error.code, "undo_conflict");
  assert.match(undo.error.conflicts[0].diff, /later user edit/);
  assert.equal(await readFile(path.join(root, "notes", "a.md"), "utf8"), "later user edit\n");
});

test("a failed undo rollback is surfaced and journaled for recovery", async (t) => {
  const journal = new MemoryJournal();
  const { coordinator, root, vault } = await fixture(t, journal);
  const a = await vault.read("notes/a.md");
  const b = await vault.read("notes/b.md");
  const proposal = batch("batch-undo-rollback-failure", [
    { operation: "append", path: "notes/a.md", expectedVersion: a.modifiedVersion, content: "post-a\n" },
    { operation: "append", path: "notes/b.md", expectedVersion: b.modifiedVersion, content: "post-b\n" },
  ]);
  const execution = coordinator.execute(toolCall("call-undo-rollback-failure", proposal));
  await coordinator.waitUntilPending("call-undo-rollback-failure");
  await coordinator.decide("call-undo-rollback-failure", "apply");
  assert.equal((await execution).ok, true);
  vault.failWhen = (vaultPath, content) =>
    (vaultPath === "notes/b.md" && content === "hello world\n") ||
    (vaultPath === "notes/a.md" && content === "alpha\npost-a\n");
  const undone = await coordinator.undo(proposal.batchId);
  assert.equal(undone.ok, false);
  assert.match(undone.error.message, /rollback failed/i);
  assert.equal(journal.entries.get(proposal.batchId).state, "recovery_failed");
  assert.equal(await readFile(path.join(root, "notes", "a.md"), "utf8"), "alpha\n");
  assert.equal(await readFile(path.join(root, "notes", "b.md"), "utf8"), "hello world\npost-b\n");
});

test("Git checkpoint retention keeps at most one hundred recent refs and removes old refs", async (t) => {
  const { checkpoints, root } = await fixture(t);
  const head = await git(root, "rev-parse", "HEAD");
  for (let index = 0; index < 101; index += 1) {
    await git(root, "update-ref", `refs/offeragent/checkpoints/retention-${String(index).padStart(3, "0")}`, head);
  }
  const oldTree = await git(root, "rev-parse", "HEAD^{tree}");
  const { stdout: oldCommitOutput } = await execFileAsync(
    "git",
    ["-C", root, "commit-tree", oldTree, "-m", "old checkpoint"],
    {
      encoding: "utf8",
      windowsHide: true,
      env: {
        ...process.env,
        GIT_AUTHOR_NAME: "OfferAgent Test",
        GIT_AUTHOR_EMAIL: "offeragent@example.invalid",
        GIT_COMMITTER_NAME: "OfferAgent Test",
        GIT_COMMITTER_EMAIL: "offeragent@example.invalid",
        GIT_AUTHOR_DATE: "2020-01-01T00:00:00Z",
        GIT_COMMITTER_DATE: "2020-01-01T00:00:00Z",
      },
    },
  );
  await git(root, "update-ref", "refs/offeragent/checkpoints/retention-old", oldCommitOutput.trim());
  const deleted = await checkpoints.cleanup(new Date("2026-07-14T00:00:00Z"));
  const remaining = (await git(root, "for-each-ref", "--format=%(refname)", "refs/offeragent/checkpoints/"))
    .split(/\r?\n/)
    .filter(Boolean);
  assert.equal(remaining.length, 100);
  assert.ok(deleted.includes("refs/offeragent/checkpoints/retention-old"));
});

test("a successful apply enforces checkpoint retention without waiting for restart", async (t) => {
  const journal = new MemoryJournal();
  const { checkpoints, root, vault } = await fixture(t, journal);
  let cleanupCalls = 0;
  const trackedCheckpoints = {
    create: (...arguments_) => checkpoints.create(...arguments_),
    read: (...arguments_) => checkpoints.read(...arguments_),
    verify: (...arguments_) => checkpoints.verify(...arguments_),
    cleanup: (...arguments_) => {
      cleanupCalls += 1;
      return checkpoints.cleanup(...arguments_);
    },
  };
  const coordinator = new VaultChangeCoordinator(vault, trackedCheckpoints, journal);
  const original = await vault.read("notes/a.md");
  const proposal = batch("batch-live-retention", [{
    operation: "append",
    path: "notes/a.md",
    expectedVersion: original.modifiedVersion,
    content: "retained\n",
  }]);
  const execution = coordinator.execute(toolCall("call-live-retention", proposal));
  await coordinator.waitUntilPending("call-live-retention");
  assert.equal((await coordinator.decide("call-live-retention", "apply")).ok, true);
  assert.equal((await execution).ok, true);
  assert.equal(cleanupCalls, 1);
  assert.equal(await readFile(path.join(root, "notes", "a.md"), "utf8"), "alpha\nretained\n");
});
