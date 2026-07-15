import assert from "node:assert/strict";
import { execFile } from "node:child_process";
import { mkdir, mkdtemp, readFile, rm, writeFile } from "node:fs/promises";
import os from "node:os";
import path from "node:path";
import test from "node:test";
import { fileURLToPath } from "node:url";

const repositoryRoot = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..");
const deployScript = path.join(repositoryRoot, "scripts", "deploy-offeragent.mjs");
const pluginPackage = path.join(repositoryRoot, "packages", "plugin", "dist");

function command(executable, args, options = {}) {
  return new Promise((resolve, reject) => {
    execFile(
      executable,
      args,
      { encoding: "utf8", windowsHide: true, ...options },
      (error, stdout, stderr) => {
        if (error) {
          error.message = `${error.message}\n${stderr}`;
          reject(error);
        } else resolve(stdout.trim());
      },
    );
  });
}

function git(root, ...args) {
  return command("git", ["-C", root, ...args]);
}

async function deploy(vault, localAppData, confirmed = false) {
  const output = await command(
    process.execPath,
    [
      deployScript,
      "--vault",
      vault,
      ...(confirmed ? ["--confirm-control-migration"] : []),
    ],
    { env: { ...process.env, LOCALAPPDATA: localAppData } },
  );
  return JSON.parse(output);
}

test("deployment preserves Runtime State, plugin data, branch, index, and unrelated dirty work", async (t) => {
  const root = await mkdtemp(path.join(os.tmpdir(), "offeragent-deploy-vault-"));
  const localAppData = await mkdtemp(path.join(os.tmpdir(), "offeragent-deploy-state-"));
  t.after(() => Promise.all([
    rm(root, { recursive: true, force: true }),
    rm(localAppData, { recursive: true, force: true }),
  ]));

  await mkdir(path.join(root, ".codex", "skills", "obsidian-cli"), { recursive: true });
  await mkdir(path.join(root, ".obsidian", "plugins", "offeragent"), { recursive: true });
  await mkdir(path.join(localAppData, "OfferAgent"), { recursive: true });
  await writeFile(path.join(root, "agent.md"), "# Legacy Agent\nUse shell commands.\n", "utf8");
  await writeFile(
    path.join(root, ".codex", "skills", "obsidian-cli", "SKILL.md"),
    "# Legacy CLI\nRun `obsidian read file=Note`.\n",
    "utf8",
  );
  await writeFile(
    path.join(root, ".obsidian", "plugins", "offeragent", "data.json"),
    '{"vaultPermissionMode":"read_only","conversation":"keep-me"}',
    "utf8",
  );
  await writeFile(
    path.join(root, ".obsidian", "community-plugins.json"),
    '["existing-plugin","keep-this-plugin"]',
    "utf8",
  );
  await writeFile(path.join(root, "dirty.md"), "baseline\n", "utf8");
  await writeFile(path.join(root, "staged.md"), "baseline\n", "utf8");
  const statePath = path.join(localAppData, "OfferAgent", "state.db");
  await writeFile(statePath, "runtime-state-must-survive", "utf8");

  await git(root, "init", "-q", "-b", "migration-branch");
  await git(root, "config", "user.name", "Deployment Test");
  await git(root, "config", "user.email", "deployment@example.invalid");
  await git(root, "add", ".");
  await git(root, "commit", "-qm", "baseline");
  await writeFile(path.join(root, "dirty.md"), "user dirty work\n", "utf8");
  await writeFile(path.join(root, "staged.md"), "user staged work\n", "utf8");
  await git(root, "add", "staged.md");

  const beforeBranch = await git(root, "branch", "--show-current");
  const beforeIndex = await git(root, "diff", "--cached", "--binary");
  const beforeDirty = await readFile(path.join(root, "dirty.md"), "utf8");
  const beforeState = await readFile(statePath, "utf8");

  const plan = await deploy(root, localAppData);
  assert.equal(plan.confirmationRequired, true);
  assert.deepEqual(plan.controlFiles, [
    ".codex/skills/obsidian-cli/SKILL.md",
    "agent.md",
    ".obsidian/community-plugins.json",
  ]);
  assert.equal(await readFile(path.join(root, "agent.md"), "utf8"), "# Legacy Agent\nUse shell commands.\n");
  assert.deepEqual(
    JSON.parse(await readFile(path.join(root, ".obsidian", "community-plugins.json"), "utf8")),
    ["existing-plugin", "keep-this-plugin"],
  );

  const result = await deploy(root, localAppData, true);
  assert.equal(result.installed, true);
  assert.equal(result.controlMigration.decision, "applied");
  assert.match(result.controlMigration.checkpointRef, /^refs\/offeragent\/checkpoints\//);

  const installation = path.join(root, ".obsidian", "plugins", "offeragent");
  for (const file of ["main.js", "manifest.json", "styles.css", "runtime.js"]) {
    assert.deepEqual(
      await readFile(path.join(installation, file)),
      await readFile(path.join(pluginPackage, file)),
    );
  }
  assert.equal(
    await readFile(path.join(installation, "data.json"), "utf8"),
    '{"vaultPermissionMode":"read_only","conversation":"keep-me"}',
  );
  assert.equal(await readFile(statePath, "utf8"), beforeState);
  assert.equal(await git(root, "branch", "--show-current"), beforeBranch);
  assert.equal(await git(root, "diff", "--cached", "--binary"), beforeIndex);
  assert.equal(await readFile(path.join(root, "dirty.md"), "utf8"), beforeDirty);
  assert.deepEqual(
    JSON.parse(await readFile(path.join(root, ".obsidian", "community-plugins.json"), "utf8")),
    ["existing-plugin", "keep-this-plugin", "offeragent"],
  );

  const contract = await readFile(path.join(root, "agent.md"), "utf8");
  assert.match(contract, /OfferAgent General Contract/);
  assert.match(contract, /Daily Study Plan/);
  assert.match(contract, /explicit company, position, interview date, and current study goal take priority/);
  assert.match(contract, /without a numeric score or fixed schedule/);
  assert.match(contract, /Registered-project relevance and resume deep-dive risk/);
  assert.match(contract, /reduce low-value repetition/);
  assert.match(contract, /Planning never advances Answer State or Learning State/);
  assert.match(contract, /普通笔记写作/);
  assert.match(contract, /Study-State Synchronization 是独立/);
  assert.match(contract, /vault_read/);
  assert.match(contract, /interview_catalog/);
  assert.match(contract, /Interview Submission/);
  assert.match(contract, /Source Fingerprint/);
  assert.match(contract, /duplicate Interview Experience/);
  assert.match(contract, /recurring Interview Question/);
  assert.match(contract, /user-supplied Interview Submission URL/);
  assert.match(contract, /final canonical URL and bounded Source Fingerprint/);
  assert.match(contract, /page is inaccessible or does not contain enough interview evidence/);
  assert.match(contract, /needs-research/);
  assert.match(contract, /Interview Question Answer Research/);
  assert.match(contract, /needs-research -> draft -> verified/);
  assert.match(contract, /Never perform background Answer research/);
  assert.match(contract, /Answer State is independent from Learning State/);
  assert.match(contract, /Missing, unreadable, insufficient, stale, or conflicting evidence leaves Answer State unchanged/);
  assert.match(contract, /原始.*(?:全文|文本).*不.*(?:复制|写入).*Vault/);
  assert.match(contract, /vault_propose_changes/);
  assert.match(contract, /Trusted Vault/);
  assert.match(contract, /explicit Resume/i);
  assert.match(contract, /Run Attachments and Vision/);
  assert.match(contract, /local evidence for its owning Agent Run/);
  assert.match(contract, /Do not invoke OCR/);
  assert.match(contract, /sent image belongs to its Conversation message and remains available across restart/);
  assert.match(contract, /Deleting a Conversation removes only that Conversation's attachment bytes/);
  assert.match(contract, /terminal Runs do not discard images from sent message history/);
  assert.match(contract, /later text-only Run must remain usable/);
  assert.match(contract, /Project Evidence and Project Interview Training/);
  assert.match(contract, /project_list.*project_search.*project_read/);
  assert.match(contract, /每次只提出一道问题并等待用户回答/);
  assert.match(contract, /不得计算总分、排名/);
  assert.match(contract, /Evidence.*Coaching suggestion/);
  assert.match(contract, /用户明确确认前不得调用 `vault_propose_changes`/);
  assert.match(contract, /projects\/\{project-id\}\/answers\/\{question-slug\}\.md/);
  assert.match(contract, /完整逐轮回答.*Conversation history/);
  const skill = await readFile(
    path.join(root, ".codex", "skills", "obsidian-cli", "SKILL.md"),
    "utf8",
  );
  assert.match(skill, /vault_list/);
  assert.match(skill, /skill_read/);
  assert.doesNotMatch(skill, /\bobsidian\s+(?:read|create|append|search|plugin:|dev:|eval)/i);

  await git(root, "cat-file", "-e", `${result.controlMigration.checkpointRef}^{commit}`);
  await writeFile(path.join(installation, "data.json"), "preserve-on-upgrade", "utf8");
  const obsidianNormalizedPlugins =
    '["existing-plugin","keep-this-plugin","offeragent"]';
  await writeFile(
    path.join(root, ".obsidian", "community-plugins.json"),
    obsidianNormalizedPlugins,
    "utf8",
  );
  const upgrade = await deploy(root, localAppData, true);
  assert.equal(upgrade.installed, true);
  assert.equal(upgrade.controlMigration, "unchanged");
  assert.equal(await readFile(path.join(installation, "data.json"), "utf8"), "preserve-on-upgrade");
  assert.equal(
    await readFile(path.join(root, ".obsidian", "community-plugins.json"), "utf8"),
    obsidianNormalizedPlugins,
  );
});

test("a package installation failure cannot enable the plugin or migrate controls", async (t) => {
  const root = await mkdtemp(path.join(os.tmpdir(), "offeragent-deploy-failure-vault-"));
  const localAppData = await mkdtemp(path.join(os.tmpdir(), "offeragent-deploy-failure-state-"));
  t.after(() => Promise.all([
    rm(root, { recursive: true, force: true }),
    rm(localAppData, { recursive: true, force: true }),
  ]));

  await mkdir(path.join(root, ".codex", "skills", "obsidian-cli"), { recursive: true });
  await mkdir(path.join(root, ".obsidian"), { recursive: true });
  await writeFile(path.join(root, "agent.md"), "# Keep legacy contract\n", "utf8");
  await writeFile(
    path.join(root, ".codex", "skills", "obsidian-cli", "SKILL.md"),
    "# Keep legacy skill\n",
    "utf8",
  );
  const enabledPlugins = '["existing-plugin"]';
  await writeFile(
    path.join(root, ".obsidian", "community-plugins.json"),
    enabledPlugins,
    "utf8",
  );
  // A file at the plugins-directory path injects a deterministic staging failure.
  await writeFile(path.join(root, ".obsidian", "plugins"), "not a directory", "utf8");
  await git(root, "init", "-q", "-b", "failure-branch");
  await git(root, "config", "user.name", "Deployment Test");
  await git(root, "config", "user.email", "deployment@example.invalid");
  await git(root, "add", ".");
  await git(root, "commit", "-qm", "baseline");

  await assert.rejects(deploy(root, localAppData, true));
  assert.equal(await readFile(path.join(root, "agent.md"), "utf8"), "# Keep legacy contract\n");
  assert.equal(
    await readFile(path.join(root, ".codex", "skills", "obsidian-cli", "SKILL.md"), "utf8"),
    "# Keep legacy skill\n",
  );
  assert.equal(
    await readFile(path.join(root, ".obsidian", "community-plugins.json"), "utf8"),
    enabledPlugins,
  );
});
