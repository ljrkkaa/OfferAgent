import { createHash, randomUUID } from "node:crypto";
import {
  cp,
  mkdir,
  readFile,
  realpath,
  rename,
  rm,
  stat,
  unlink,
  writeFile,
} from "node:fs/promises";
import { createRequire } from "node:module";
import os from "node:os";
import path from "node:path";
import { fileURLToPath } from "node:url";

const repositoryRoot = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..");
const require = createRequire(import.meta.url);
const { GitCheckpointStore, VaultChangeCoordinator } = require(
  path.join(repositoryRoot, "packages", "plugin", "dist", "vault-change-coordinator.js"),
);
const packageDirectory = path.join(repositoryRoot, "packages", "plugin", "dist");
const migrationDirectory = path.join(repositoryRoot, "migration", "target-vault");
const packageFiles = ["main.js", "manifest.json", "styles.css", "runtime.js"];

function argumentsFrom(argv) {
  let vault;
  let confirmed = false;
  for (let index = 0; index < argv.length; index += 1) {
    if (argv[index] === "--vault") vault = argv[++index];
    else if (argv[index] === "--confirm-control-migration") confirmed = true;
    else throw new Error(`Unknown deployment argument '${argv[index]}'.`);
  }
  if (!vault) throw new Error("Use --vault <absolute-vault-path>.");
  if (!path.isAbsolute(vault)) throw new Error("The Vault path must be absolute.");
  return { confirmed, vault: path.resolve(vault) };
}

async function exists(target) {
  try {
    await stat(target);
    return true;
  } catch (error) {
    if (error?.code === "ENOENT") return false;
    throw error;
  }
}

class NodeVaultFileApi {
  #root;

  constructor(root) {
    this.#root = root;
  }

  #target(vaultPath) {
    return path.resolve(this.#root, ...vaultPath.split("/"));
  }

  async canonicalize(vaultPath, targetExists) {
    const root = await realpath(this.#root);
    const unresolved = this.#target(vaultPath);
    if (targetExists) return { root, target: await realpath(unresolved) };
    const missing = [path.basename(unresolved)];
    let ancestor = path.dirname(unresolved);
    while (!(await exists(ancestor))) {
      missing.unshift(path.basename(ancestor));
      const parent = path.dirname(ancestor);
      if (parent === ancestor) throw new Error(`Cannot resolve Vault path '${vaultPath}'.`);
      ancestor = parent;
    }
    return { root, target: path.join(await realpath(ancestor), ...missing) };
  }

  async create(vaultPath, content) {
    const target = this.#target(vaultPath);
    await mkdir(path.dirname(target), { recursive: true });
    await writeFile(target, content, { encoding: "utf8", flag: "wx" });
  }

  async modify(vaultPath, content) {
    await writeFile(this.#target(vaultPath), content, "utf8");
  }

  async read(vaultPath) {
    const target = this.#target(vaultPath);
    try {
      const details = await stat(target);
      if (!details.isFile()) return undefined;
      return {
        content: await readFile(target, "utf8"),
        modifiedVersion: `mtime:${details.mtimeMs}:size:${details.size}`,
      };
    } catch (error) {
      if (error?.code === "ENOENT") return undefined;
      throw error;
    }
  }

  async remove(vaultPath) {
    try {
      await unlink(this.#target(vaultPath));
    } catch (error) {
      if (error?.code !== "ENOENT") throw error;
    }
  }
}

class JsonMigrationJournal {
  #path;

  constructor(journalPath) {
    this.#path = journalPath;
  }

  async #read() {
    try {
      const value = JSON.parse(await readFile(this.#path, "utf8"));
      return Array.isArray(value) ? value : [];
    } catch (error) {
      if (error?.code === "ENOENT") {
        try {
          const backup = JSON.parse(await readFile(`${this.#path}.bak`, "utf8"));
          return Array.isArray(backup) ? backup : [];
        } catch (backupError) {
          if (backupError?.code === "ENOENT") return [];
          throw backupError;
        }
      }
      throw error;
    }
  }

  async #write(records) {
    await mkdir(path.dirname(this.#path), { recursive: true });
    const temporary = `${this.#path}.${process.pid}.tmp`;
    const backup = `${this.#path}.bak`;
    await writeFile(temporary, JSON.stringify(records, null, 2), "utf8");
    await rm(backup, { force: true });
    if (await exists(this.#path)) await rename(this.#path, backup);
    try {
      await rename(temporary, this.#path);
      await rm(backup, { force: true });
    } catch (error) {
      if (!(await exists(this.#path)) && await exists(backup)) await rename(backup, this.#path);
      throw error;
    } finally {
      await rm(temporary, { force: true });
    }
  }

  async list(states) {
    return (await this.#read()).filter((record) => states.includes(record.state));
  }

  async markApplying(batchId, checkpointRef, targets) {
    const records = (await this.#read()).filter((record) => record.batchId !== batchId);
    records.push({ batchId, checkpointRef, state: "applying", targets });
    await this.#write(records);
  }

  async markState(batchId, state) {
    const records = await this.#read();
    const record = records.find((candidate) => candidate.batchId === batchId);
    if (!record) throw new Error(`Migration journal '${batchId}' does not exist.`);
    record.state = state;
    await this.#write(records);
  }
}

async function enabledPluginsTemplate(vault) {
  const target = path.join(vault, ".obsidian", "community-plugins.json");
  let plugins = [];
  let currentContent;
  try {
    currentContent = await readFile(target, "utf8");
    plugins = JSON.parse(currentContent);
  } catch (error) {
    if (error?.code !== "ENOENT") {
      throw new Error(`Cannot read Obsidian's enabled-plugin list: ${error.message}`);
    }
  }
  if (!Array.isArray(plugins) || plugins.some((plugin) => typeof plugin !== "string")) {
    throw new Error("Obsidian's enabled-plugin list must be a JSON array of plugin IDs.");
  }
  if (plugins.includes("offeragent")) {
    return { path: ".obsidian/community-plugins.json", content: currentContent };
  }
  plugins.push("offeragent");
  return {
    path: ".obsidian/community-plugins.json",
    content: `${JSON.stringify(plugins, null, 2)}\n`,
  };
}

async function templates(vault) {
  return [
    {
      path: ".codex/skills/obsidian-cli/SKILL.md",
      content: await readFile(path.join(migrationDirectory, "obsidian-cli", "SKILL.md"), "utf8"),
    },
    {
      path: "agent.md",
      content: await readFile(path.join(migrationDirectory, "agent.md"), "utf8"),
    },
    await enabledPluginsTemplate(vault),
  ];
}

async function prepareActions(vault, files) {
  const api = new NodeVaultFileApi(vault);
  const actions = [];
  for (const [index, file] of files.entries()) {
    const current = await api.read(file.path);
    if (current?.content === file.content) continue;
    const base = {
      actionId: `migration-action-${index + 1}`,
      idempotencyKey: `migration-action-${index + 1}-${createHash("sha256").update(file.content).digest("hex").slice(0, 16)}`,
      path: file.path,
      expectedVersion: current?.modifiedVersion ?? "missing",
    };
    actions.push(
      current
        ? {
            ...base,
            operation: "exact_replace",
            expectedContent: current.content,
            replacement: file.content,
          }
        : { ...base, operation: "create", content: file.content },
    );
  }
  return { actions, api };
}

async function migrateControls(vault, files) {
  const { actions, api } = await prepareActions(vault, files);
  if (actions.length === 0) return "unchanged";
  const contentIdentity = createHash("sha256")
    .update(actions.map((action) => `${action.path}\0${action.idempotencyKey}`).join("\0"))
    .digest("hex")
    .slice(0, 16);
  const batchId = `target-vault-migration-${contentIdentity}`;
  const toolCallId = `migration-tool-${contentIdentity}`;
  const localAppData = process.env.LOCALAPPDATA || path.join(os.homedir(), "AppData", "Local");
  const vaultIdentity = createHash("sha256").update(vault.toLowerCase()).digest("hex").slice(0, 16);
  const journal = new JsonMigrationJournal(
    path.join(localAppData, "OfferAgent", `migration-${vaultIdentity}.json`),
  );
  const checkpoints = new GitCheckpointStore(vault);
  const coordinator = new VaultChangeCoordinator(
    api,
    checkpoints,
    journal,
    () => {},
    () => "ask_every_time",
    checkpoints,
  );
  await coordinator.reconcile();
  const proposal = {
    batchId,
    idempotencyKey: `target-vault-migration-${contentIdentity}`,
    task: "Migrate the OfferAgent Agent Contract and Vault-local Obsidian skill",
    actions,
  };
  const execution = coordinator.execute({
    type: "tool_call.requested",
    protocolVersion: 1,
    eventId: randomUUID(),
    conversationId: "target-vault-migration",
    agentRunId: "target-vault-migration",
    sequence: 1,
    toolCallId,
    tool: { name: "vault_propose_changes", arguments: proposal },
  });
  await coordinator.waitUntilPending(toolCallId);
  const result = await coordinator.decide(toolCallId, "apply");
  await execution;
  if (!result.ok) throw new Error(result.error.message);
  return result.value;
}

async function installPackage(vault) {
  for (const file of packageFiles) {
    if (!(await exists(path.join(packageDirectory, file)))) {
      throw new Error(`Build the production package before deployment; '${file}' is missing.`);
    }
  }
  const pluginsDirectory = path.join(vault, ".obsidian", "plugins");
  const target = path.join(pluginsDirectory, "offeragent");
  const suffix = `${process.pid}-${Date.now()}`;
  const staging = path.join(pluginsDirectory, `.offeragent-staging-${suffix}`);
  const backup = path.join(pluginsDirectory, `.offeragent-backup-${suffix}`);
  await mkdir(pluginsDirectory, { recursive: true });
  try {
    if (await exists(target)) await cp(target, staging, { recursive: true });
    else await mkdir(staging, { recursive: true });
    for (const file of packageFiles) {
      await cp(path.join(packageDirectory, file), path.join(staging, file));
    }
    if (await exists(target)) await rename(target, backup);
    try {
      await rename(staging, target);
    } catch (error) {
      if (await exists(backup)) await rename(backup, target);
      throw error;
    }
    await rm(backup, { recursive: true, force: true });
  } finally {
    await rm(staging, { recursive: true, force: true });
  }
}

async function main() {
  const { confirmed, vault } = argumentsFrom(process.argv.slice(2));
  await realpath(vault);
  const files = await templates(vault);
  const { actions } = await prepareActions(vault, files);
  if (!confirmed) {
    process.stdout.write(JSON.stringify({
      confirmationRequired: actions.length > 0,
      controlFiles: actions.map(({ path }) => path),
      installed: false,
    }));
    return;
  }
  await installPackage(vault);
  const controlMigration = await migrateControls(vault, files);
  process.stdout.write(JSON.stringify({ controlMigration, installed: true }));
}

main().catch((error) => {
  process.stderr.write(`${error instanceof Error ? error.message : String(error)}\n`);
  process.exitCode = 1;
});
