import assert from "node:assert/strict";
import { execFile } from "node:child_process";
import { cp, mkdir, mkdtemp, readFile, rm, writeFile } from "node:fs/promises";
import { createRequire } from "node:module";
import Module from "node:module";
import os from "node:os";
import path from "node:path";
import { fileURLToPath } from "node:url";
import test from "node:test";

const repositoryRoot = path.resolve(
  path.dirname(fileURLToPath(import.meta.url)),
  "..",
);
const builtPlugin = path.join(repositoryRoot, "packages", "plugin", "dist");

function git(root, ...args) {
  return new Promise((resolve, reject) => {
    execFile("git", ["-C", root, ...args], { windowsHide: true }, (error, stdout, stderr) => {
      if (error) reject(new Error(stderr || error.message));
      else resolve(stdout.toString("utf8").trim());
    });
  });
}

class StubElement {
  constructor(className = "", tagName = "div") {
    this.className = className;
    this.children = [];
    this.dataset = {};
    this.disabled = false;
    this.listeners = new Map();
    this.tagName = tagName;
    this.text = "";
    this.value = "";
  }

  empty() {
    this.children = [];
  }

  addClass(className) {
    this.className = `${this.className} ${className}`.trim();
  }

  createDiv(options = {}) {
    return this.#createChild(options);
  }

  createEl(_tagName, options = {}) {
    return this.#createChild(options, _tagName);
  }

  addEventListener(type, listener) {
    this.listeners.set(type, listener);
  }

  dispatch(type) {
    this.listeners.get(type)?.({ preventDefault() {} });
  }

  findByClass(className) {
    if (this.className.split(" ").includes(className)) return this;
    for (const child of this.children) {
      const match = child.findByClass(className);
      if (match) return match;
    }
    return undefined;
  }

  findAllByClass(className) {
    const matches = this.className.split(" ").includes(className) ? [this] : [];
    for (const child of this.children) matches.push(...child.findAllByClass(className));
    return matches;
  }

  #createChild(options, tagName = "div") {
    const child = new StubElement(options.cls ?? "", tagName);
    child.text = options.text ?? "";
    child.value = options.value ?? "";
    this.children.push(child);
    return child;
  }
}

async function waitUntil(predicate, message, timeoutMs = 10_000) {
  const deadline = Date.now() + timeoutMs;
  while (Date.now() < deadline) {
    const value = predicate();
    if (value) return value;
    await new Promise((resolve) => setTimeout(resolve, 25));
  }
  throw new Error(message);
}

test("Obsidian loads the packaged plugin and opens its connected sidebar", async (t) => {
  const previousProvider = process.env.OFFERAGENT_RUNTIME_PROVIDER;
  const previousStatePath = process.env.OFFERAGENT_RUNTIME_STATE_PATH;
  const previousLocalAppData = process.env.LOCALAPPDATA;
  process.env.OFFERAGENT_RUNTIME_PROVIDER = "fake";
  const temporaryVault = await mkdtemp(path.join(os.tmpdir(), "offeragent-vault-"));
  const temporaryRuntimeHome = await mkdtemp(
    path.join(os.tmpdir(), "offeragent-plugin-runtime-home-"),
  );
  process.env.LOCALAPPDATA = temporaryRuntimeHome;
  process.env.OFFERAGENT_RUNTIME_STATE_PATH = path.join(temporaryRuntimeHome, "state.db");
  await mkdir(path.join(temporaryVault, "notes"), { recursive: true });
  await writeFile(path.join(temporaryVault, "agent.md"), "# Test Agent Contract", "utf8");
  await writeFile(path.join(temporaryVault, "notes", "example.md"), "line one\nline two", "utf8");
  await git(temporaryVault, "init", "-q");
  const installation = path.join(
    temporaryVault,
    ".obsidian",
    "plugins",
    "offeragent",
  );
  await cp(builtPlugin, installation, { recursive: true });

  let activeView;
  let plugin;
  let persistedPluginData;

  class FileSystemAdapter {
    constructor(basePath) {
      this.basePath = basePath;
    }

    getBasePath() {
      return this.basePath;
    }
  }

  class ItemView {
    constructor(leaf) {
      this.leaf = leaf;
      this.contentEl = new StubElement();
    }
  }

  class Plugin {
    constructor(app, manifest) {
      this.app = app;
      this.manifest = manifest;
      this.commands = new Map();
      this.ribbonActions = [];
      this.savedData = persistedPluginData;
      this.settingTabs = [];
      this.views = new Map();
    }

    registerView(type, factory) {
      this.views.set(type, factory);
    }

    addRibbonIcon(_icon, _label, callback) {
      this.ribbonActions.push(callback);
    }

    addCommand(command) {
      this.commands.set(command.id, command);
    }

    addSettingTab(settingTab) {
      this.settingTabs.push(settingTab);
    }

    async loadData() {
      return persistedPluginData;
    }

    async saveData(data) {
      persistedPluginData = structuredClone(data);
      this.savedData = persistedPluginData;
    }
  }

  class PluginSettingTab {
    constructor(app, owner) {
      this.app = app;
      this.plugin = owner;
      this.containerEl = new StubElement();
    }
  }

  class Setting {
    constructor(container) {
      this.settingEl = container.createDiv({ cls: "setting-item" });
    }

    setName(name) {
      this.settingEl.createDiv({ cls: "setting-item-name", text: name });
      return this;
    }

    setDesc(description) {
      this.settingEl.createDiv({ cls: "setting-item-description", text: description });
      return this;
    }

    addDropdown(configure) {
      const selectEl = this.settingEl.createEl("select", { cls: "dropdown" });
      const dropdown = {
        addOption(value, label) {
          const option = selectEl.createEl("option", { text: label });
          option.value = value;
          return dropdown;
        },
        onChange(listener) {
          selectEl.addEventListener("change", () => listener(selectEl.value));
          return dropdown;
        },
        setValue(value) {
          selectEl.value = value;
          return dropdown;
        },
      };
      configure(dropdown);
      return this;
    }

    addButton(configure) {
      const buttonEl = this.settingEl.createEl("button", { cls: "setting-item-button" });
      const button = {
        onClick(listener) {
          buttonEl.addEventListener("click", listener);
          return button;
        },
        setButtonText(text) {
          buttonEl.text = text;
          return button;
        },
        setDisabled(disabled) {
          buttonEl.disabled = disabled;
          return button;
        },
      };
      configure(button);
      return this;
    }
  }

  class Notice {
    constructor(message) {
      this.message = message;
    }
  }

  const leaf = {
    async setViewState(viewState) {
      activeView = plugin.views.get(viewState.type)(leaf);
      await activeView.onOpen();
    },
  };
  const workspace = {
    detachLeavesOfType() {
      void activeView?.onClose();
      activeView = undefined;
    },
    getLeavesOfType() {
      return activeView ? [leaf] : [];
    },
    getRightLeaf() {
      return leaf;
    },
    async revealLeaf() {},
  };
  const obsidianStub = {
    FileSystemAdapter,
    ItemView,
    Notice,
    Platform: { isDesktopApp: true },
    Plugin,
    PluginSettingTab,
    Setting,
  };

  const require = createRequire(import.meta.url);
  const entryPath = path.join(builtPlugin, "main.js");
  const originalLoad = Module._load;
  Module._load = function loadWithObsidianStub(request, parent, isMain) {
    if (request === "obsidian") return obsidianStub;
    return originalLoad.call(this, request, parent, isMain);
  };
  let loaded;
  try {
    delete require.cache[require.resolve(entryPath)];
    loaded = require(entryPath);
  } finally {
    Module._load = originalLoad;
  }

  const manifest = JSON.parse(
    await readFile(path.join(installation, "manifest.json"), "utf8"),
  );
  manifest.dir = path.join(".obsidian", "plugins", "offeragent");
  const vaultFiles = [
    {
      path: "agent.md",
      extension: "md",
      stat: { mtime: 1234, size: 26 },
      content: "# Test Agent Contract",
    },
    {
      path: "notes/example.md",
      extension: "md",
      stat: { mtime: 1234, size: 17 },
      content: "line one\nline two",
    },
  ];
  const app = {
    vault: {
      adapter: new FileSystemAdapter(temporaryVault),
      configDir: ".obsidian",
      getFiles() {
        return vaultFiles;
      },
      async cachedRead(file) {
        return file.content;
      },
      async create(vaultPath, content) {
        const file = {
          path: vaultPath,
          extension: vaultPath.split(".").at(-1),
          stat: { mtime: Date.now(), size: Buffer.byteLength(content, "utf8") },
          content,
        };
        vaultFiles.push(file);
        await writeFile(path.join(temporaryVault, vaultPath), content, { encoding: "utf8", flag: "wx" });
        return file;
      },
      async modify(file, content) {
        file.content = content;
        file.stat = { mtime: Date.now(), size: Buffer.byteLength(content, "utf8") };
        await writeFile(path.join(temporaryVault, file.path), content, "utf8");
      },
      async delete(file) {
        vaultFiles.splice(vaultFiles.indexOf(file), 1);
        await rm(path.join(temporaryVault, file.path), { force: true });
      },
    },
    workspace,
  };
  const OfferAgentPlugin = loaded.default ?? loaded;
  plugin = new OfferAgentPlugin(app, manifest);
  t.after(async () => {
    await plugin?.onunload();
    if (previousProvider === undefined) delete process.env.OFFERAGENT_RUNTIME_PROVIDER;
    else process.env.OFFERAGENT_RUNTIME_PROVIDER = previousProvider;
    if (previousStatePath === undefined) delete process.env.OFFERAGENT_RUNTIME_STATE_PATH;
    else process.env.OFFERAGENT_RUNTIME_STATE_PATH = previousStatePath;
    if (previousLocalAppData === undefined) delete process.env.LOCALAPPDATA;
    else process.env.LOCALAPPDATA = previousLocalAppData;
    await rm(temporaryVault, { recursive: true, force: true });
    await rm(temporaryRuntimeHome, { recursive: true, force: true });
  });

  await plugin.onload();
  assert.ok(plugin.views.has("offeragent-sidebar"));
  assert.ok(plugin.commands.has("open-offeragent-sidebar"));
  assert.equal(plugin.ribbonActions.length, 1);
  assert.equal(plugin.settingTabs.length, 1);
  plugin.settingTabs[0].display();
  const permissionSelect = plugin.settingTabs[0].containerEl.findByClass("dropdown");
  assert.equal(permissionSelect.value, "trusted_vault");

  plugin.commands.get("open-offeragent-sidebar").callback();
  await waitUntil(() => activeView, "OfferAgent did not open its sidebar");
  const connectedStatus = await waitUntil(
    () => {
      const status = activeView.contentEl.findByClass("offeragent-sidebar__status");
      return status?.dataset.state === "connected" ? status : undefined;
    },
    "OfferAgent sidebar did not report a connected Runtime",
  );
  assert.equal(connectedStatus.text, "connected");

  const modelSelect = await waitUntil(
    () => {
      const select = activeView.contentEl.findByClass("offeragent-sidebar__model-select");
      return select?.value === "fake-interview-model" ? select : undefined;
    },
    "OfferAgent did not render its model selector",
  );
  assert.equal(modelSelect.value, "fake-interview-model");
  plugin.settingTabs[0].display();
  const capabilityStatus = await waitUntil(
    () => plugin.settingTabs[0].containerEl
      .findAllByClass("setting-item-description")
      .find((description) => description.text.includes("fake-interview-model: unknown")),
    "OfferAgent settings did not show the unknown Hosted Web Search capability",
  );
  assert.match(capabilityStatus.text, /unknown/);
  const reprobe = plugin.settingTabs[0].containerEl
    .findAllByClass("setting-item-button")
    .find((button) => button.text === "Reprobe");
  assert.ok(reprobe);
  reprobe.dispatch("click");
  await waitUntil(
    () => reprobe.disabled === false && plugin.settingTabs[0].containerEl
      .findAllByClass("setting-item-description")
      .some((description) => description.text.includes("fake-interview-model: available")),
    "OfferAgent settings did not complete a Hosted Web Search reprobe",
  );
  const composer = activeView.contentEl.findByClass("offeragent-sidebar__composer");
  const input = activeView.contentEl.findByClass("offeragent-sidebar__input");
  assert.ok(composer);
  assert.ok(input);
  input.value = "Practice my introduction.";
  composer.dispatch("submit");

  const assistantMessage = await waitUntil(
    () => {
      const message = activeView.contentEl.findByClass("offeragent-sidebar__message--assistant");
      return message?.text === "OfferAgent received: Practice my introduction." ? message : undefined;
    },
    "OfferAgent did not stream the fake Provider response into the Sidebar",
  );
  assert.equal(assistantMessage.text, "OfferAgent received: Practice my introduction.");
  const completedRun = await waitUntil(
    () => {
      const status = activeView.contentEl.findByClass("offeragent-sidebar__run-status");
      return status?.dataset.status === "completed" ? status : undefined;
    },
    "OfferAgent did not render the completed first Agent Run",
  );
  assert.equal(completedRun.dataset.status, "completed");

  const nextComposer = activeView.contentEl.findByClass("offeragent-sidebar__composer");
  const nextInput = activeView.contentEl.findByClass("offeragent-sidebar__input");
  nextInput.value = "Practice a second answer.";
  nextComposer.dispatch("submit");
  const visibleRunStatuses = await waitUntil(
    () => {
      const statuses = activeView.contentEl.findAllByClass("offeragent-sidebar__run-status");
      return statuses.length === 2 && statuses.every((status) => status.dataset.status === "completed")
        ? statuses
        : undefined;
    },
    "OfferAgent did not keep both Agent Run statuses visible",
  );
  assert.deepEqual(
    visibleRunStatuses.map((status) => status.dataset.status),
    ["completed", "completed"],
  );

  const toolComposer = activeView.contentEl.findByClass("offeragent-sidebar__composer");
  const toolInput = activeView.contentEl.findByClass("offeragent-sidebar__input");
  toolInput.value = "vault_read notes/example.md 1-2";
  toolComposer.dispatch("submit");
  const completedTool = await waitUntil(
    () => {
      const activities = activeView.contentEl.findAllByClass("offeragent-sidebar__tool-activity");
      return activities.find(
        (activity) =>
          activity.dataset.status === "completed" &&
          activity.children[0]?.text.includes("vault_read"),
      );
    },
    "OfferAgent did not execute and render the Vault tool activity",
  );
  assert.match(completedTool.children[0].text, /vault_read · completed/);

  await waitUntil(
    () => {
      const statuses = activeView.contentEl.findAllByClass("offeragent-sidebar__run-status");
      return statuses.length === 3 &&
        statuses.every((status) => status.dataset.status === "completed");
    },
    "OfferAgent did not finish the Vault read Agent Run",
  );

  const proposal = {
    batchId: "smoke-batch-reject",
    idempotencyKey: "smoke-batch-reject-key",
    task: "Append one smoke-test line",
    actions: [
      {
        actionId: "smoke-action-reject",
        idempotencyKey: "smoke-action-reject-key",
        operation: "append",
        path: "notes/example.md",
        expectedVersion: "mtime:1234:size:17",
        content: "\nrejected",
      },
    ],
  };
  const autoProposal = {
    ...proposal,
    batchId: "smoke-batch-auto",
    idempotencyKey: "smoke-batch-auto-key",
    task: "Auto-apply one trusted smoke-test line",
    actions: [{
      ...proposal.actions[0],
      actionId: "smoke-action-auto",
      idempotencyKey: "smoke-action-auto-key",
      operation: "create",
      path: "notes/auto-applied.md",
      expectedVersion: "missing",
      content: "auto applied\n",
    }],
  };
  const autoComposer = activeView.contentEl.findByClass("offeragent-sidebar__composer");
  const autoInput = activeView.contentEl.findByClass("offeragent-sidebar__input");
  autoInput.value = `vault_propose_changes ${JSON.stringify(autoProposal)}`;
  autoComposer.dispatch("submit");
  await waitUntil(
    () =>
      activeView.contentEl
        .findAllByClass("offeragent-sidebar__change-batch")
        .find(
          (card) =>
            card.dataset.status === "applied" &&
            card.children[0]?.text.includes("Auto-apply one trusted smoke-test line"),
        ),
    "Trusted Vault did not auto-apply the normal batch",
  );
  assert.equal(
    await readFile(path.join(temporaryVault, "notes", "auto-applied.md"), "utf8"),
    "auto applied\n",
  );
  await waitUntil(
    () => activeView.contentEl.findByClass("offeragent-sidebar__send")?.disabled === false,
    "Trusted Vault Agent Run did not finish after auto-apply",
  );
  permissionSelect.value = "ask_every_time";
  permissionSelect.dispatch("change");
  await waitUntil(
    () => plugin.savedData?.vaultPermissionMode === "ask_every_time",
    "OfferAgent did not persist Ask Every Time for this Vault",
  );
  const changeComposer = activeView.contentEl.findByClass("offeragent-sidebar__composer");
  const changeInput = activeView.contentEl.findByClass("offeragent-sidebar__input");
  changeInput.value = `vault_propose_changes ${JSON.stringify(proposal)}`;
  changeComposer.dispatch("submit");
  const pendingBatch = await waitUntil(
    () =>
      activeView.contentEl
        .findAllByClass("offeragent-sidebar__change-batch")
        .find(
          (card) =>
            card.dataset.status === "pending" &&
            card.children[0]?.text.includes("Append one smoke-test line"),
        ),
    "OfferAgent did not render the pending whole-batch confirmation card",
  );
  assert.match(pendingBatch.children[0].text, /Append one smoke-test line/);
  const applyAll = activeView.contentEl.findByClass("offeragent-sidebar__change-apply");
  const rejectAll = activeView.contentEl.findByClass("offeragent-sidebar__change-reject");
  assert.equal(applyAll.text, "Apply all");
  assert.equal(rejectAll.text, "Reject all");
  rejectAll.dispatch("click");
  const rejectedBatch = await waitUntil(
    () =>
      activeView.contentEl
        .findAllByClass("offeragent-sidebar__change-batch")
        .find(
          (card) =>
            card.dataset.status === "rejected" &&
            card.children[0]?.text.includes("Append one smoke-test line"),
        ),
    "OfferAgent did not reject the whole batch and continue the Agent Run",
  );
  assert.equal(rejectedBatch.dataset.status, "rejected");
  assert.equal(
    await readFile(path.join(temporaryVault, "notes", "example.md"), "utf8"),
    "line one\nline two",
  );
  await waitUntil(
    () => {
      const statuses = activeView.contentEl.findAllByClass("offeragent-sidebar__run-status");
      return statuses.length === 5 &&
        statuses.every((status) => status.dataset.status === "completed");
    },
    "OfferAgent did not continue the rejected batch Agent Run",
  );

  const appliedProposal = {
    ...proposal,
    batchId: "smoke-batch-apply",
    idempotencyKey: "smoke-batch-apply-key",
    task: "Apply one smoke-test line",
    actions: [
      {
        ...proposal.actions[0],
        actionId: "smoke-action-apply",
        idempotencyKey: "smoke-action-apply-key",
        operation: "create",
        path: "notes/applied.md",
        expectedVersion: "missing",
        content: "applied\n",
      },
    ],
  };
  const applyComposer = activeView.contentEl.findByClass("offeragent-sidebar__composer");
  const applyInput = activeView.contentEl.findByClass("offeragent-sidebar__input");
  applyInput.value = `vault_propose_changes ${JSON.stringify(appliedProposal)}`;
  applyComposer.dispatch("submit");
  await waitUntil(
    () =>
      activeView.contentEl
        .findAllByClass("offeragent-sidebar__change-batch")
        .find(
          (card) =>
            card.dataset.status === "pending" &&
            card.children[0]?.text.includes("Apply one smoke-test line"),
        ),
    "OfferAgent did not render the applied batch confirmation card",
  );
  activeView.contentEl.findByClass("offeragent-sidebar__change-apply").dispatch("click");
  await waitUntil(
    () =>
      activeView.contentEl
        .findAllByClass("offeragent-sidebar__change-batch")
        .find(
          (card) =>
            card.dataset.status === "applied" &&
            card.children[0]?.text.includes("Apply one smoke-test line"),
        ),
    "OfferAgent did not apply the whole batch",
  );
  assert.equal(
    await readFile(path.join(temporaryVault, "notes", "applied.md"), "utf8"),
    "applied\n",
  );
  await git(
    temporaryVault,
    "cat-file",
    "-e",
    "refs/offeragent/checkpoints/smoke-batch-apply^{commit}",
  );
  await waitUntil(
    () => {
      const statuses = activeView.contentEl.findAllByClass("offeragent-sidebar__run-status");
      return statuses.length === 6 &&
        statuses.every((status) => status.dataset.status === "completed");
    },
    "OfferAgent did not continue the applied batch Agent Run",
  );
  permissionSelect.value = "read_only";
  permissionSelect.dispatch("change");
  await waitUntil(
    () => plugin.savedData?.vaultPermissionMode === "read_only",
    "OfferAgent did not persist Read Only for this Vault",
  );
  const deniedProposal = {
    ...appliedProposal,
    batchId: "smoke-batch-read-only",
    idempotencyKey: "smoke-batch-read-only-key",
    task: "Reject one read-only smoke-test line",
    actions: [{
      ...appliedProposal.actions[0],
      actionId: "smoke-action-read-only",
      idempotencyKey: "smoke-action-read-only-key",
      path: "notes/read-only.md",
    }],
  };
  const deniedComposer = activeView.contentEl.findByClass("offeragent-sidebar__composer");
  const deniedInput = activeView.contentEl.findByClass("offeragent-sidebar__input");
  deniedInput.value = `vault_propose_changes ${JSON.stringify(deniedProposal)}`;
  deniedComposer.dispatch("submit");
  await waitUntil(
    () =>
      activeView.contentEl
        .findAllByClass("offeragent-sidebar__change-batch")
        .find(
          (card) =>
            card.dataset.status === "failed" &&
            card.children[0]?.text.includes("Reject one read-only smoke-test line"),
        ),
    "Read Only did not visibly reject the mutation",
  );
  const deniedPolicy = activeView.contentEl.findByClass("offeragent-sidebar__change-policy-decision");
  assert.match(deniedPolicy.text, /Read Only/i);
  await assert.rejects(readFile(path.join(temporaryVault, "notes", "read-only.md"), "utf8"));
  await waitUntil(
    () => activeView.contentEl.findByClass("offeragent-sidebar__send")?.disabled === false,
    "Read Only Agent Run did not finish after policy rejection",
  );
  const readActivityCount = activeView.contentEl
    .findAllByClass("offeragent-sidebar__tool-activity")
    .filter((activity) => activity.children[0]?.text.includes("vault_read")).length;
  const readOnlyComposer = activeView.contentEl.findByClass("offeragent-sidebar__composer");
  const readOnlyInput = activeView.contentEl.findByClass("offeragent-sidebar__input");
  readOnlyInput.value = "vault_read notes/example.md 1-2";
  readOnlyComposer.dispatch("submit");
  await waitUntil(
    () =>
      activeView.contentEl
        .findAllByClass("offeragent-sidebar__tool-activity")
        .filter(
          (activity) =>
            activity.dataset.status === "completed" &&
            activity.children[0]?.text.includes("vault_read"),
        ).length === readActivityCount + 1,
    "Read Only blocked a permitted Vault read",
  );
  await waitUntil(
    () => activeView.contentEl.findByClass("offeragent-sidebar__send")?.disabled === false,
    "Read Only Vault read Agent Run did not finish",
  );
  await plugin.onunload();
  plugin = new OfferAgentPlugin(app, manifest);
  await plugin.onload();
  plugin.settingTabs[0].display();
  assert.equal(
    plugin.settingTabs[0].containerEl.findByClass("dropdown").value,
    "read_only",
  );
  plugin.commands.get("open-offeragent-sidebar").callback();
  const rehydratedDeniedBatch = await waitUntil(
    () =>
      activeView?.contentEl
        .findAllByClass("offeragent-sidebar__change-batch")
        .find(
          (card) =>
            card.dataset.status === "failed" &&
            card.children[0]?.text.includes("Reject one read-only smoke-test line"),
        ),
    "OfferAgent did not preserve the rejected Read Only decision after restart",
  );
  assert.match(
    rehydratedDeniedBatch.findByClass("offeragent-sidebar__change-policy-decision").text,
    /Read Only/i,
  );
  const rehydratedAppliedBatch = await waitUntil(
    () =>
      activeView?.contentEl
        .findAllByClass("offeragent-sidebar__change-batch")
        .find(
          (card) =>
            card.dataset.status === "applied" &&
            card.children[0]?.text.includes("Apply one smoke-test line"),
        ),
    "OfferAgent did not rehydrate the applied batch after plugin restart",
  );
  rehydratedAppliedBatch.findByClass("offeragent-sidebar__change-undo").dispatch("click");
  await waitUntil(
    () =>
      activeView.contentEl
        .findAllByClass("offeragent-sidebar__change-batch")
        .find(
          (card) =>
            card.dataset.status === "undone" &&
            card.children[0]?.text.includes("Apply one smoke-test line"),
        ),
    "OfferAgent did not undo the applied batch",
  );
  await assert.rejects(readFile(path.join(temporaryVault, "notes", "applied.md"), "utf8"));

  const citationComposer = activeView.contentEl.findByClass("offeragent-sidebar__composer");
  const citationInput = activeView.contentEl.findByClass("offeragent-sidebar__input");
  citationInput.value = "hosted_search_demo";
  citationComposer.dispatch("submit");
  const citation = await waitUntil(
    () => activeView.contentEl.findByClass("offeragent-sidebar__citation"),
    "OfferAgent did not render the hosted search citation",
  );
  assert.equal(citation.text, "[1] Example source");
  assert.equal(citation.href, "https://example.com/source");
  assert.equal(citation.target, "_blank");
  assert.equal(citation.rel, "noopener noreferrer");
  await waitUntil(
    () => activeView.contentEl.findByClass("offeragent-sidebar__send")?.disabled === false,
    "Hosted search citation Agent Run did not finish",
  );

  await plugin.onunload();
  plugin = undefined;
  assert.equal(activeView, undefined);
});
