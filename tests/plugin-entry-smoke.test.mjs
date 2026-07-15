import assert from "node:assert/strict";
import { execFile } from "node:child_process";
import { mkdir, mkdtemp, readFile, rm, writeFile } from "node:fs/promises";
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
const deployScript = path.join(repositoryRoot, "scripts", "deploy-offeragent.mjs");

function git(root, ...args) {
  return new Promise((resolve, reject) => {
    execFile("git", ["-C", root, ...args], { windowsHide: true }, (error, stdout, stderr) => {
      if (error) reject(new Error(stderr || error.message));
      else resolve(stdout.toString("utf8").trim());
    });
  });
}

function execute(file, args, options = {}) {
  return new Promise((resolve, reject) => {
    execFile(file, args, { windowsHide: true, ...options }, (error, stdout, stderr) => {
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
    this.attributes = new Map();
    this.listeners = new Map();
    this.open = false;
    this.parentElement = undefined;
    this.tagName = tagName;
    this._text = "";
    this.value = "";
  }

  empty() {
    for (const child of this.children) child.parentElement = undefined;
    this.children = [];
  }

  get text() {
    return `${this._text}${this.children.map(({ text }) => text).join("")}`;
  }

  set text(value) {
    this._text = `${value}`;
  }

  get childNodes() {
    return this.children;
  }

  get textContent() {
    return this.text;
  }

  appendChild(child) {
    child.remove();
    child.parentElement = this;
    this.children.push(child);
    return child;
  }

  cloneNode(deep = false) {
    const clone = new StubElement(this.className, this.tagName);
    clone._text = this._text;
    if (deep) clone.replaceChildren(...this.children.map((child) => child.cloneNode(true)));
    return clone;
  }

  remove() {
    if (!this.parentElement) return;
    const index = this.parentElement.children.indexOf(this);
    if (index >= 0) this.parentElement.children.splice(index, 1);
    this.parentElement = undefined;
  }

  replaceChildren(...children) {
    for (const child of this.children) child.parentElement = undefined;
    this.children = [];
    for (const child of children) this.appendChild(child);
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

  setAttribute(name, value) {
    this.attributes.set(name, `${value}`);
  }

  setText(text) {
    this.text = `${text}`;
    this.children = [];
  }

  getAttribute(name) {
    return this.attributes.get(name);
  }

  dispatch(type, event = {}) {
    const dispatched = {
      defaultPrevented: false,
      preventDefault() {
        this.defaultPrevented = true;
      },
      ...event,
    };
    this.listeners.get(type)?.(dispatched);
    this[`on${type}`]?.(dispatched);
    return dispatched;
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

  querySelector(selector) {
    return this.querySelectorAll(selector)[0] ?? undefined;
  }

  querySelectorAll(selector) {
    const descendants = this.children.flatMap(function collect(element) {
      return [element, ...element.children.flatMap(collect)];
    });
    if (selector === "pre > code") {
      return descendants.filter(
        (element) => element.tagName === "code" && element.parentElement?.tagName === "pre",
      );
    }
    if (selector === 'a[href^="http://"], a[href^="https://"]') {
      return descendants.filter((element) => {
        const href = element.getAttribute("href") ?? "";
        return element.tagName === "a" && /^https?:\/\//.test(href);
      });
    }
    if (selector.startsWith(".")) return this.findAllByClass(selector.slice(1));
    return [];
  }

  #createChild(options, tagName = "div") {
    const child = new StubElement(options.cls ?? "", tagName);
    child.text = options.text ?? "";
    child.value = options.value ?? "";
    this.appendChild(child);
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
  const previousSetInterval = globalThis.setInterval;
  const previousClearInterval = globalThis.clearInterval;
  const previousSetTimeout = globalThis.setTimeout;
  const previousClearTimeout = globalThis.clearTimeout;
  const previousDate = globalThis.Date;
  globalThis.Date = class extends previousDate {
    constructor(...args) {
      super(...(args.length > 0 ? args : ["2026-07-14T12:00:00"]));
    }

    static now() {
      return previousDate.now();
    }
  };
  const browserIntervals = new Map();
  const browserTimeouts = new Map();
  let nextBrowserInterval = 0;
  let nextBrowserTimeout = 10_000;
  globalThis.setInterval = (callback, milliseconds, ...args) => {
    const id = ++nextBrowserInterval;
    browserIntervals.set(id, previousSetInterval(callback, milliseconds, ...args));
    return id;
  };
  globalThis.clearInterval = (id) => {
    const handle = browserIntervals.get(id);
    if (handle) {
      browserIntervals.delete(id);
      previousClearInterval(handle);
      return;
    }
    previousClearInterval(id);
  };
  globalThis.setTimeout = (callback, milliseconds, ...args) => {
    const id = ++nextBrowserTimeout;
    browserTimeouts.set(id, previousSetTimeout(callback, milliseconds, ...args));
    return id;
  };
  globalThis.clearTimeout = (id) => {
    const handle = browserTimeouts.get(id);
    if (handle) {
      browserTimeouts.delete(id);
      previousClearTimeout(handle);
      return;
    }
    previousClearTimeout(id);
  };
  t.after(() => {
    for (const handle of browserIntervals.values()) previousClearInterval(handle);
    for (const handle of browserTimeouts.values()) previousClearTimeout(handle);
    globalThis.setInterval = previousSetInterval;
    globalThis.clearInterval = previousClearInterval;
    globalThis.setTimeout = previousSetTimeout;
    globalThis.clearTimeout = previousClearTimeout;
    globalThis.Date = previousDate;
  });
  process.env.OFFERAGENT_RUNTIME_PROVIDER = "fake";
  const temporaryVault = await mkdtemp(path.join(os.tmpdir(), "offeragent-vault-"));
  const temporaryRuntimeHome = await mkdtemp(
    path.join(os.tmpdir(), "offeragent-plugin-runtime-home-"),
  );
  process.env.LOCALAPPDATA = temporaryRuntimeHome;
  process.env.OFFERAGENT_RUNTIME_STATE_PATH = path.join(temporaryRuntimeHome, "state.db");
  await mkdir(path.join(temporaryVault, "notes"), { recursive: true });
  await mkdir(path.join(temporaryVault, ".obsidian"), { recursive: true });
  await mkdir(path.join(temporaryVault, "templates"), { recursive: true });
  await mkdir(path.join(temporaryVault, "interview"), { recursive: true });
  await mkdir(path.join(temporaryVault, "daily"), { recursive: true });
  await mkdir(path.join(temporaryVault, "memory", "study"), { recursive: true });
  await writeFile(path.join(temporaryVault, "agent.md"), "# Test Agent Contract", "utf8");
  await writeFile(path.join(temporaryVault, "notes", "example.md"), "line one\nline two", "utf8");
  await writeFile(
    path.join(temporaryVault, ".obsidian", "daily-notes.json"),
    JSON.stringify({ folder: "daily", format: "YYYY-MM-DD", template: "templates/daily.md" }),
    "utf8",
  );
  const dailyTemplate = "---\nkind: daily\nowner: user\n---\n# {{date}}\n\n## 今日学习计划\n\n## 完成记录\n- [x] Keep this checked record\n";
  const interviewQueue = "# 面试八股学习进度\n\n- [x] Existing completed topic\n- [ ] Self-Attention\n";
  await writeFile(path.join(temporaryVault, "templates", "daily.md"), dailyTemplate, "utf8");
  await writeFile(path.join(temporaryVault, "interview", "面试八股学习进度.md"), interviewQueue, "utf8");
  await git(temporaryVault, "init", "-q");
  const installation = path.join(
    temporaryVault,
    ".obsidian",
    "plugins",
    "offeragent",
  );
  const deployment = JSON.parse(await execute(
    process.execPath,
    [deployScript, "--vault", temporaryVault, "--confirm-control-migration"],
    { env: process.env },
  ));
  assert.equal(deployment.installed, true);
  assert.equal(deployment.controlMigration.decision, "applied");
  const migratedContract = await readFile(path.join(temporaryVault, "agent.md"), "utf8");
  assert.match(migratedContract, /OfferAgent General Contract/);

  let activeView;
  let plugin;
  let releaseStaleMarkdownRender;
  let deferredStreamingMarkdown = false;
  let persistedPluginData;

  class FileSystemAdapter {
    constructor(basePath) {
      this.basePath = basePath;
    }

    getBasePath() {
      return this.basePath;
    }

    async exists(vaultPath) {
      try {
        await readFile(path.join(this.basePath, vaultPath));
        return true;
      } catch {
        return false;
      }
    }

    read(vaultPath) {
      return readFile(path.join(this.basePath, vaultPath), "utf8");
    }
  }

  class ItemView {
    constructor(leaf) {
      this.leaf = leaf;
      this.contentEl = new StubElement();
    }
  }

  class Component {
    load() {}
    unload() {}
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

    addToggle(configure) {
      const toggleEl = this.settingEl.createEl("input", { cls: "checkbox-container" });
      toggleEl.type = "checkbox";
      toggleEl.checked = false;
      const toggle = {
        onChange(listener) {
          toggleEl.addEventListener("change", () => listener(toggleEl.checked));
          return toggle;
        },
        setValue(value) {
          toggleEl.checked = value;
          return toggle;
        },
      };
      configure(toggle);
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
    Component,
    FileSystemAdapter,
    ItemView,
    MarkdownRenderer: {
      async render(_app, markdown, element) {
        if (
          markdown === "OfferAgent received: Practice my introduction." &&
          !deferredStreamingMarkdown
        ) {
          deferredStreamingMarkdown = true;
          await new Promise((resolve) => {
            releaseStaleMarkdownRender = resolve;
          });
        }
        if (markdown.includes("MARKDOWN_RENDER_FAILURE")) {
          throw new Error("synthetic Markdown renderer failure");
        }
        element.createDiv({ text: markdown });
        if (markdown.includes("Practice my introduction.")) {
          const link = element.createEl("a", { text: "External source" });
          link.setAttribute("href", "https://example.com/source");
          const nativePre = element.createEl("pre");
          nativePre.createEl("code", { text: "const answer = 42;" });
          nativePre.createEl("button", { cls: "copy-code-button" });
          const fallbackPre = element.createEl("pre");
          fallbackPre.createEl("code", { text: "const fallback = 7;" });
        }
      },
    },
    Notice,
    Platform: { isDesktopApp: true },
    Plugin,
    PluginSettingTab,
    Setting,
    moment(date) {
      return {
        format(format) {
          const [year, month, day] = date.split("-");
          return format.replaceAll("YYYY", year).replaceAll("MM", month).replaceAll("DD", day);
        },
      };
    },
  };

  const require = createRequire(import.meta.url);
  const entryPath = path.join(installation, "main.js");
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
      stat: { mtime: 1234, size: Buffer.byteLength(migratedContract, "utf8") },
      content: migratedContract,
    },
    {
      path: "notes/example.md",
      extension: "md",
      stat: { mtime: 1234, size: 17 },
      content: "line one\nline two",
    },
    {
      path: "templates/daily.md",
      extension: "md",
      stat: { mtime: 1235, size: Buffer.byteLength(dailyTemplate, "utf8") },
      content: dailyTemplate,
    },
    {
      path: "interview/面试八股学习进度.md",
      extension: "md",
      stat: { mtime: 1236, size: Buffer.byteLength(interviewQueue, "utf8") },
      content: interviewQueue,
    },
  ];
  const contractReads = [];
  let settingsOpened = 0;
  const app = {
    metadataCache: {
      getFileCache(file) {
        if (!/^memory\/(?:user|feedback|project|study)\/.+\.md$/.test(file.path)) return undefined;
        const value = (key) => {
          const raw = new RegExp(`^${key}:\\s*(.+)$`, "m").exec(file.content)?.[1]?.trim();
          if (!raw) return undefined;
          try {
            return raw.startsWith('"') ? JSON.parse(raw) : raw;
          } catch {
            return undefined;
          }
        };
        return { frontmatter: { name: value("name"), description: value("description"), type: value("type") } };
      },
    },
    setting: {
      open() { settingsOpened += 1; },
      openTabById() {},
    },
    vault: {
      adapter: new FileSystemAdapter(temporaryVault),
      configDir: ".obsidian",
      getFiles() {
        return vaultFiles;
      },
      async cachedRead(file) {
        if (file.path === "agent.md") contractReads.push(file.content);
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
  const permissionSelect = plugin.settingTabs[0].containerEl
    .findAllByClass("dropdown")
    .find(({ value }) => value === "trusted_vault");
  assert.equal(permissionSelect.value, "trusted_vault");
  const commonSettingNames = plugin.settingTabs[0].containerEl
    .findAllByClass("setting-item-name")
    .map(({ text }) => text);
  assert.ok(commonSettingNames.includes("Runtime status"));
  assert.ok(commonSettingNames.includes("Provider status"));
  assert.ok(commonSettingNames.includes("Model"));
  assert.ok(commonSettingNames.includes("Vault Permission Mode"));
  assert.ok(plugin.settingTabs[0].containerEl.findByClass("offeragent-settings__advanced"));

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
  const settingsButton = activeView.contentEl.findByClass("offeragent-sidebar__settings");
  assert.ok(settingsButton);
  settingsButton.dispatch("click");
  assert.equal(settingsOpened, 1);

  const modelSelect = await waitUntil(
    () => {
      const select = activeView.contentEl.findByClass("offeragent-sidebar__model-select");
      return select?.value === "fake-interview-model" ? select : undefined;
    },
    "OfferAgent did not render its model selector",
  );
  assert.equal(modelSelect.value, "fake-interview-model");
  assert.equal(
    activeView.contentEl.findByClass("offeragent-sidebar__conversation-select")
      .getAttribute("aria-label"),
    "Conversation history",
  );
  assert.equal(
    activeView.contentEl.findByClass("offeragent-sidebar__input").getAttribute("aria-label"),
    "给 OfferAgent 的消息",
  );
  plugin.settingTabs[0].display();
  assert.ok(
    plugin.settingTabs[0].containerEl
      .findAllByClass("setting-item-name")
      .some(({ text }) => text === "Fast Mode"),
  );
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
  assert.equal(input.listeners.has("paste"), true);
  assert.equal(composer.listeners.has("dragover"), true);
  assert.equal(composer.listeners.has("drop"), true);
  assert.equal(
    activeView.contentEl.findByClass("offeragent-sidebar__file-picker")?.listeners.has("change"),
    true,
  );
  assert.equal(activeView.contentEl.findByClass("offeragent-sidebar__attach")?.text, "添加图片");
  assert.equal(
    activeView.contentEl.findByClass("offeragent-sidebar__attach")?.getAttribute("aria-label"),
    "添加图片（最多 20 张）",
  );
  assert.equal(modelSelect.getAttribute("aria-label"), "选择对话模型");
  const imagePicker = activeView.contentEl.findByClass("offeragent-sidebar__file-picker");
  let releaseFirstImageImport;
  const firstImageImport = new Promise((resolve) => {
    releaseFirstImageImport = () => resolve(new Uint8Array([1, 2, 3]).buffer);
  });
  imagePicker.files = [
    {
      name: "first-preview.png",
      size: 3,
      type: "image/png",
      async arrayBuffer() { return firstImageImport; },
    },
    {
      name: "second-preview.png",
      size: 3,
      type: "image/png",
      async arrayBuffer() { return new Uint8Array([4, 5, 6]).buffer; },
    },
  ];
  imagePicker.dispatch("change");
  const pendingImportInput = activeView.contentEl.findByClass("offeragent-sidebar__input");
  pendingImportInput.value = "Do not send while images are preparing.";
  pendingImportInput.dispatch("input");
  const messagesBeforePendingImportEnter = activeView.contentEl
    .findAllByClass("offeragent-sidebar__message").length;
  assert.equal(activeView.contentEl.findByClass("offeragent-sidebar__send")?.disabled, true);
  const pendingImportEnter = pendingImportInput.dispatch("keydown", {
    isComposing: false,
    key: "Enter",
    keyCode: 13,
    shiftKey: false,
  });
  assert.equal(pendingImportEnter.defaultPrevented, true);
  assert.equal(
    activeView.contentEl.findAllByClass("offeragent-sidebar__message").length,
    messagesBeforePendingImportEnter,
  );
  releaseFirstImageImport();
  const imagePreviews = await waitUntil(
    () => {
      const previews = activeView.contentEl.findAllByClass("offeragent-sidebar__attachment-preview");
      return previews.length === 2 ? previews : undefined;
    },
    "OfferAgent did not render ordered image thumbnails",
  );
  assert.deepEqual(imagePreviews.map(({ tagName }) => tagName), ["img", "img"]);
  assert.deepEqual(
    imagePreviews.map((preview) => preview.getAttribute("alt")),
    ["Preview 1: first-preview.png", "Preview 2: second-preview.png"],
  );
  assert.ok(imagePreviews.every(({ src }) => /^blob:/.test(src)));
  activeView.contentEl.findByClass("offeragent-sidebar__attachment-remove").dispatch("click");
  activeView.contentEl.findByClass("offeragent-sidebar__attachment-remove").dispatch("click");
  assert.equal(activeView.contentEl.findAllByClass("offeragent-sidebar__attachment-preview").length, 0);
  assert.equal(
    activeView.contentEl.findByClass("offeragent-sidebar__context-chip")?.text,
    "Vault context",
  );
  assert.match(
    activeView.contentEl.findByClass("offeragent-sidebar__permission")?.text ?? "",
    /Trusted Vault/,
  );
  await plugin.setFastMode(true);
  await waitUntil(
    () => activeView.contentEl.findByClass("offeragent-sidebar__context-chip--fast"),
    "Fast Mode setting did not refresh the idle Sidebar presentation",
  );
  await plugin.setFastMode(false);
  await plugin.setVaultPermissionMode("read_only");
  await waitUntil(
    () => activeView.contentEl.findByClass("offeragent-sidebar__permission")?.text === "Read Only",
    "Vault Permission setting did not refresh the idle Sidebar presentation",
  );
  await plugin.setVaultPermissionMode("trusted_vault");
  assert.equal(activeView.contentEl.findByClass("offeragent-sidebar__send")?.text, "Send");
  assert.equal(
    activeView.contentEl.findByClass("offeragent-sidebar__permission")?.getAttribute("aria-label"),
    "Vault 权限模式：信任 Vault",
  );
  assert.equal(
    activeView.contentEl.findByClass("offeragent-sidebar__permission")?.getAttribute("role"),
    "status",
  );
  assert.equal(
    activeView.contentEl.findByClass("offeragent-sidebar__send")?.getAttribute("aria-label"),
    "发送消息",
  );
  const keyboardInput = activeView.contentEl.findByClass("offeragent-sidebar__input");
  const messageCountBeforeKeyboard = activeView.contentEl
    .findAllByClass("offeragent-sidebar__message").length;
  keyboardInput.value = "中文输入法确认不应发送";
  keyboardInput.dispatch("compositionstart");
  const composingEnter = keyboardInput.dispatch("keydown", {
    isComposing: true,
    key: "Enter",
    keyCode: 229,
    shiftKey: false,
  });
  keyboardInput.dispatch("compositionend");
  const postCompositionEnter = keyboardInput.dispatch("keydown", {
    isComposing: false,
    key: "Enter",
    keyCode: 13,
    shiftKey: false,
  });
  assert.equal(composingEnter.defaultPrevented, false);
  assert.equal(postCompositionEnter.defaultPrevented, false);
  assert.equal(
    activeView.contentEl.findAllByClass("offeragent-sidebar__message").length,
    messageCountBeforeKeyboard,
  );
  await new Promise((resolve) => setTimeout(resolve, 5));
  const shiftedEnter = keyboardInput.dispatch("keydown", {
    isComposing: false,
    key: "Enter",
    keyCode: 13,
    shiftKey: true,
  });
  assert.equal(shiftedEnter.defaultPrevented, false);
  assert.equal(
    activeView.contentEl.findAllByClass("offeragent-sidebar__message").length,
    messageCountBeforeKeyboard,
  );
  keyboardInput.value = "Practice my introduction.";
  keyboardInput.dispatch("input");
  const sendEnter = keyboardInput.dispatch("keydown", {
    isComposing: false,
    key: "Enter",
    keyCode: 13,
    shiftKey: false,
  });
  assert.equal(sendEnter.defaultPrevented, true);
  const activeInput = activeView.contentEl.findByClass("offeragent-sidebar__input");
  assert.equal(activeInput.disabled, false);
  assert.equal(
    activeView.contentEl.findByClass("offeragent-sidebar__stop")?.getAttribute("aria-label"),
    "停止当前运行",
  );
  activeInput.value = "Draft retained during the active Run";
  activeInput.dispatch("input");
  const blockedSecondEnter = activeInput.dispatch("keydown", {
    isComposing: false,
    key: "Enter",
    keyCode: 13,
    shiftKey: false,
  });
  assert.equal(blockedSecondEnter.defaultPrevented, true);
  await waitUntil(
    () => releaseStaleMarkdownRender,
    "OfferAgent did not start the deferred Markdown render",
  );
  await waitUntil(
    () => activeView.contentEl.findByClass("offeragent-sidebar__send")?.disabled === false,
    "OfferAgent did not finish the first Agent Run",
  );
  assert.equal(
    activeView.contentEl.findByClass("offeragent-sidebar__input").value,
    "Draft retained during the active Run",
  );
  await plugin.setFastMode(true);
  const assistantMessage = await waitUntil(
    () => {
      const message = activeView.contentEl.findByClass("offeragent-sidebar__message--assistant");
      return message?.findByClass("offeragent-sidebar__message-body")?.text.startsWith(
          "OfferAgent received: Practice my introduction.",
        )
        ? message
        : undefined;
    },
    "OfferAgent did not stream the fake Provider response into the Sidebar",
  );
  assert.equal(
    assistantMessage.findByClass("offeragent-sidebar__message-body")?.text,
    "OfferAgent received: Practice my introduction.External sourceconst answer = 42;const fallback = 7;复制代码",
  );
  const externalLink = assistantMessage
    .findByClass("offeragent-sidebar__message-body")
    ?.querySelectorAll('a[href^="http://"], a[href^="https://"]')[0];
  assert.equal(externalLink?.target, "_blank");
  assert.equal(externalLink?.rel, "noopener noreferrer");
  assert.equal(
    assistantMessage.findByClass("offeragent-sidebar__code-copy")?.getAttribute("aria-label"),
    "复制代码块",
  );
  assert.equal(assistantMessage.findAllByClass("copy-code-button").length, 1);
  assert.equal(assistantMessage.findAllByClass("offeragent-sidebar__code-copy").length, 1);
  assert.equal(
    assistantMessage.findByClass("offeragent-sidebar__message-copy")?.getAttribute("aria-label"),
    "复制完整回答",
  );
  releaseStaleMarkdownRender();
  await new Promise((resolve) => setTimeout(resolve, 75));
  assert.equal(
    activeView.contentEl
      .findByClass("offeragent-sidebar__message--assistant")
      ?.findByClass("offeragent-sidebar__message-body")?.text,
    "OfferAgent received: Practice my introduction.External sourceconst answer = 42;const fallback = 7;复制代码",
    "A stale Markdown render replaced the latest completed response",
  );
  await plugin.setFastMode(false);
  assert.ok(
    activeView.contentEl
      .findAllByClass("offeragent-sidebar__tool-activity")
      .some(
        (activity) =>
          activity
            .findAllByClass("offeragent-sidebar__tool-activity-label")
            .some((label) => label.text.includes("Read contract agent.md")),
      ),
    "The installed Runtime did not read the migrated Agent Contract",
  );
  assert.deepEqual(contractReads, [migratedContract]);
  assert.equal(activeView.contentEl.findAllByClass("offeragent-sidebar__run-status").length, 0);

  const nextComposer = activeView.contentEl.findByClass("offeragent-sidebar__composer");
  const nextInput = activeView.contentEl.findByClass("offeragent-sidebar__input");
  nextInput.value = "MARKDOWN_RENDER_FAILURE";
  nextComposer.dispatch("submit");
  await waitUntil(
    () => activeView.contentEl.findByClass("offeragent-sidebar__send")?.disabled === false,
    "OfferAgent did not finish the second Agent Run",
  );
  assert.ok(
    activeView.contentEl
      .findAllByClass("offeragent-sidebar__message-body")
      .some(
        (body) =>
          body.dataset.renderStatus === "plain-text-fallback" &&
          body.text.includes("MARKDOWN_RENDER_FAILURE"),
      ),
    "A Markdown renderer failure did not retain the complete plain-text answer",
  );
  assert.equal(activeView.contentEl.findAllByClass("offeragent-sidebar__run-status").length, 0);

  const toolComposer = activeView.contentEl.findByClass("offeragent-sidebar__composer");
  const toolInput = activeView.contentEl.findByClass("offeragent-sidebar__input");
  toolInput.value = "vault_read notes/example.md 1-2";
  toolComposer.dispatch("submit");
  await waitUntil(
    () => activeView.contentEl.findByClass("offeragent-sidebar__stop"),
    "OfferAgent did not enter the active Tool Run",
  );
  const stableToolRunElements = {
    composer: activeView.contentEl.findByClass("offeragent-sidebar__composer"),
    header: activeView.contentEl.findByClass("offeragent-sidebar__header"),
    input: activeView.contentEl.findByClass("offeragent-sidebar__input"),
    transcript: activeView.contentEl.findByClass("offeragent-sidebar__transcript"),
  };
  const completedTool = await waitUntil(
    () => {
      const activities = activeView.contentEl.findAllByClass("offeragent-sidebar__tool-activity");
      return activities.find(
        (activity) =>
          activity.dataset.status === "completed" &&
          activity
            .findAllByClass("offeragent-sidebar__tool-activity-label")
            .some((label) => label.text.includes("Read notes/example.md")),
      );
    },
    "OfferAgent did not execute and render the Vault tool activity",
  );
  assert.equal(
    activeView.contentEl.findByClass("offeragent-sidebar__header"),
    stableToolRunElements.header,
  );
  assert.equal(
    activeView.contentEl.findByClass("offeragent-sidebar__transcript"),
    stableToolRunElements.transcript,
  );
  assert.equal(
    activeView.contentEl.findByClass("offeragent-sidebar__composer"),
    stableToolRunElements.composer,
  );
  assert.equal(
    activeView.contentEl.findByClass("offeragent-sidebar__input"),
    stableToolRunElements.input,
  );
  assert.ok(
    completedTool
      .findAllByClass("offeragent-sidebar__tool-activity-label")
      .some((label) => /Read notes\/example\.md · completed/.test(label.text)),
  );
  assert.equal(completedTool.children[0].getAttribute("aria-expanded"), "false");
  completedTool.open = true;
  completedTool.dispatch("toggle");
  assert.equal(completedTool.children[0].getAttribute("aria-expanded"), "true");

  await waitUntil(
    () => activeView.contentEl.findByClass("offeragent-sidebar__send")?.disabled === false,
    "OfferAgent did not finish the Vault read Agent Run",
  );

  const planComposer = activeView.contentEl.findByClass("offeragent-sidebar__composer");
  const planInput = activeView.contentEl.findByClass("offeragent-sidebar__input");
  planInput.value = "帮我做一个今天的学习日记";
  planComposer.dispatch("submit");
  await waitUntil(
    () => activeView.contentEl
      .findAllByClass("offeragent-sidebar__message--assistant")
      .some(({ text }) => text.includes("DAILY_STUDY_PLAN_PROPOSAL")),
    "OfferAgent did not propose the Daily Study Plan",
  );
  await waitUntil(
    () => activeView.contentEl.findByClass("offeragent-sidebar__send")?.disabled === false,
    "OfferAgent did not finish the Daily Study Plan proposal Run",
  );
  const confirmComposer = activeView.contentEl.findByClass("offeragent-sidebar__composer");
  const confirmInput = activeView.contentEl.findByClass("offeragent-sidebar__input");
  confirmInput.value = "可以的";
  confirmComposer.dispatch("submit");
  await waitUntil(
    () => activeView.contentEl.findByClass("offeragent-sidebar__send")?.disabled === false,
    "OfferAgent did not finish the confirmed Daily Study Plan Run",
    20_000,
  );
  assert.ok(
    vaultFiles.some(({ path }) => path === "daily/2026-07-14.md"),
    JSON.stringify({
      messages: activeView.contentEl
        .findAllByClass("offeragent-sidebar__message--assistant")
        .map(({ text }) => text),
      activities: activeView.contentEl
        .findAllByClass("offeragent-sidebar__tool-activity")
        .map((activity) => ({ status: activity.dataset.status, text: activity.children[0]?.text })),
    }),
  );
  const dailyPlan = await readFile(path.join(temporaryVault, "daily", "2026-07-14.md"), "utf8");
  assert.match(dailyPlan, /^---\nkind: daily\nowner: user\n---/);
  assert.match(dailyPlan, /# 2026-07-14/);
  assert.match(dailyPlan, /- \[ \] Self-Attention/);
  assert.match(dailyPlan, /来源：interview\/面试八股学习进度\.md/);
  assert.match(dailyPlan, /前瞻计划，不是学习完成证据/);
  assert.doesNotMatch(dailyPlan, /- \[x\] Self-Attention/i);
  assert.equal(
    await readFile(path.join(temporaryVault, "interview", "面试八股学习进度.md"), "utf8"),
    interviewQueue,
  );
  assert.match(await git(temporaryVault, "for-each-ref", "--format=%(refname)", "refs/offeragent"), /refs\/offeragent/);

  const existingDaily = vaultFiles.find(({ path }) => path === "daily/2026-07-14.md");
  const proposalCount = activeView.contentEl
    .findAllByClass("offeragent-sidebar__message--assistant")
    .filter(({ text }) => text.includes("DAILY_STUDY_PLAN_PROPOSAL")).length;
  const existingPlanInput = activeView.contentEl.findByClass("offeragent-sidebar__input");
  existingPlanInput.value = "帮我做一个今天的学习日记";
  activeView.contentEl.findByClass("offeragent-sidebar__composer").dispatch("submit");
  await waitUntil(
    () => activeView.contentEl
      .findAllByClass("offeragent-sidebar__message--assistant")
      .filter(({ text }) => text.includes("DAILY_STUDY_PLAN_PROPOSAL")).length === proposalCount + 1,
    "OfferAgent did not produce the existing-note proposal",
  );
  await waitUntil(
    () => activeView.contentEl.findByClass("offeragent-sidebar__send")?.disabled === false,
    "OfferAgent did not finish the existing-note proposal Run",
  );
  const existingConfirmInput = activeView.contentEl.findByClass("offeragent-sidebar__input");
  const unchangedPlanMessageCount = activeView.contentEl
    .findAllByClass("offeragent-sidebar__message--assistant")
    .filter(({ text }) => text.includes("无需重复写入")).length;
  existingConfirmInput.value = "可以的";
  activeView.contentEl.findByClass("offeragent-sidebar__composer").dispatch("submit");
  try {
    await waitUntil(
      () => activeView.contentEl
        .findAllByClass("offeragent-sidebar__message--assistant")
        .filter(({ text }) => text.includes("无需重复写入")).length ===
        unchangedPlanMessageCount + 1,
      "OfferAgent did not answer the existing-note confirmation",
      20_000,
    );
  } catch (error) {
    assert.fail(JSON.stringify({
      error: error.message,
      messages: activeView.contentEl
        .findAllByClass("offeragent-sidebar__message--assistant")
        .slice(-5)
        .map(({ text }) => text),
      activities: activeView.contentEl
        .findAllByClass("offeragent-sidebar__tool-activity")
        .slice(-12)
        .map((activity) => ({ status: activity.dataset.status, text: activity.children[0]?.text })),
      runStatuses: activeView.contentEl
        .findAllByClass("offeragent-sidebar__run-status")
        .map(({ dataset, text }) => ({ state: dataset.state, text })),
    }));
  }
  await waitUntil(
    () => activeView.contentEl.findByClass("offeragent-sidebar__send")?.disabled === false,
    "OfferAgent did not finish the existing-note confirmation Run",
    20_000,
  );
  assert.ok(
    existingDaily.content.includes("- [ ] Self-Attention"),
    JSON.stringify({
      messages: activeView.contentEl
        .findAllByClass("offeragent-sidebar__message--assistant")
        .slice(-4)
        .map(({ text }) => text),
      activities: activeView.contentEl
        .findAllByClass("offeragent-sidebar__tool-activity")
        .slice(-10)
        .map((activity) => ({ status: activity.dataset.status, text: activity.children[0]?.text })),
    }),
  );
  const filledExisting = await readFile(path.join(temporaryVault, existingDaily.path), "utf8");
  assert.match(filledExisting, /^---\nkind: daily\nowner: user\n---/);
  assert.match(filledExisting, /- \[x\] Keep this checked record/);
  assert.match(filledExisting, /## 今日学习计划\n\n- \[ \] Self-Attention/);
  assert.equal((filledExisting.match(/## 今日学习计划/g) ?? []).length, 1);
  assert.equal((filledExisting.match(/- \[ \] Self-Attention/g) ?? []).length, 1);
  assert.equal((filledExisting.match(/本节是前瞻计划，不是学习完成证据。/g) ?? []).length, 1);

  const optionalQueue = vaultFiles.find(
    ({ path: vaultPath }) => vaultPath === "interview/面试八股学习进度.md",
  );
  const acceptAnotherDailyPlan = async (scenario, requestText = "帮我做一个今天的学习日记") => {
    const proposalCountBefore = activeView.contentEl
      .findAllByClass("offeragent-sidebar__message--assistant")
      .filter(({ text }) => text.includes("DAILY_STUDY_PLAN_PROPOSAL")).length;
    activeView.contentEl.findByClass("offeragent-sidebar__input").value = requestText;
    activeView.contentEl.findByClass("offeragent-sidebar__composer").dispatch("submit");
    await waitUntil(
      () => activeView.contentEl
        .findAllByClass("offeragent-sidebar__message--assistant")
        .filter(({ text }) => text.includes("DAILY_STUDY_PLAN_PROPOSAL")).length ===
        proposalCountBefore + 1,
      `OfferAgent did not propose the ${scenario} plan`,
    );
    await waitUntil(
      () => activeView.contentEl.findByClass("offeragent-sidebar__send")?.disabled === false,
      `OfferAgent did not finish the ${scenario} proposal Run`,
    );
    const appliedCountBefore = activeView.contentEl
      .findAllByClass("offeragent-sidebar__message--assistant")
      .filter(({ text }) => text.includes("今日学习计划已写入")).length;
    activeView.contentEl.findByClass("offeragent-sidebar__input").value = "可以的";
    activeView.contentEl.findByClass("offeragent-sidebar__composer").dispatch("submit");
    try {
      await waitUntil(
        () => activeView.contentEl
          .findAllByClass("offeragent-sidebar__message--assistant")
          .filter(({ text }) => text.includes("今日学习计划已写入")).length ===
          appliedCountBefore + 1,
        `OfferAgent did not create the ${scenario} grounded plan`,
        20_000,
      );
    } catch (error) {
      assert.fail(JSON.stringify({
        error: error.message,
        messages: activeView.contentEl.findAllByClass("offeragent-sidebar__message--assistant")
          .slice(-6).map(({ text }) => text),
        activities: activeView.contentEl.findAllByClass("offeragent-sidebar__tool-activity")
          .slice(-30).map((activity) => ({ status: activity.dataset.status, text: activity.children[0]?.text })),
        runs: activeView.contentEl.findAllByClass("offeragent-sidebar__run-status")
          .map(({ dataset, text }) => ({ state: dataset.state, text })),
        changes: activeView.contentEl.findAllByClass("offeragent-sidebar__change-batch")
          .slice(-6).map((card) => ({ status: card.dataset.status, text: card.text })),
      }));
    }
    await waitUntil(
      () => activeView.contentEl.findByClass("offeragent-sidebar__send")?.disabled === false,
      `OfferAgent did not finish the ${scenario} confirmation Run`,
      20_000,
    );
  };

  await app.vault.modify(
    optionalQueue,
    "# 面试八股学习进度\n\n- [x] Existing completed topic\n- [x] Another completed topic\n",
  );
  await acceptAnotherDailyPlan("all-completed-queue fallback");
  const allCompletedFallback = await readFile(
    path.join(temporaryVault, existingDaily.path),
    "utf8",
  );
  assert.match(allCompletedFallback, /- \[ \] line one（来源：notes\/example\.md）/);
  assert.doesNotMatch(allCompletedFallback, /- \[ \] Existing completed topic/);

  await app.vault.delete(optionalQueue);
  const fallbackNote = vaultFiles.find(({ path: vaultPath }) => vaultPath === "notes/example.md");
  await app.vault.modify(fallbackNote, "line two");
  await acceptAnotherDailyPlan("missing-optional-source fallback");
  const fallbackPlan = await readFile(path.join(temporaryVault, existingDaily.path), "utf8");
  assert.match(fallbackPlan, /- \[ \] line two（来源：notes\/example\.md）/);
  assert.doesNotMatch(fallbackPlan, /复习最近的学习主题/);
  await app.vault.modify(fallbackNote, "line one\nline two");

  const checkpointRefsBeforeFill = (await git(
    temporaryVault,
    "for-each-ref",
    "--format=%(refname)",
    "refs/offeragent/checkpoints",
  )).trim().split(/\r?\n/).filter(Boolean).length;
  await app.vault.modify(
    existingDaily,
    dailyTemplate.replace("{{date}}", "2026-07-14"),
  );
  await acceptAnotherDailyPlan("pre-existing empty placeholder");
  const filledPlaceholder = await readFile(path.join(temporaryVault, existingDaily.path), "utf8");
  assert.match(filledPlaceholder, /^---\nkind: daily\nowner: user\n---/);
  assert.match(filledPlaceholder, /- \[x\] Keep this checked record/);
  assert.match(filledPlaceholder, /## 今日学习计划\n\n- \[ \] line one/);
  assert.equal((filledPlaceholder.match(/## 今日学习计划/g) ?? []).length, 1);
  const checkpointRefsAfterFill = (await git(
    temporaryVault,
    "for-each-ref",
    "--format=%(refname)",
    "refs/offeragent/checkpoints",
  )).trim().split(/\r?\n/).filter(Boolean).length;
  assert.ok(checkpointRefsAfterFill > checkpointRefsBeforeFill);

  const studyMemoryContent = "---\nname: \"今天学习日记 Retrieval Evaluation\"\ndescription: \"今天学习日记的跨天 Retrieval Evaluation 方向\"\ntype: study\n---\n\n未来三天学习 Retrieval Evaluation，并根据每日结果调整顺序。\n";
  const otherStudyMemory = "---\nname: \"健身饮食记录\"\ndescription: \"每周力量训练和营养安排\"\ntype: study\n---\n\n安排力量训练和营养摄入。\n";
  const projectMemory = "---\nname: \"家庭旅行预算\"\ndescription: \"夏季旅行的交通住宿预算\"\ntype: project\n---\n\n控制家庭旅行支出。\n";
  const memoryIndexContent = [
    "# Planning Memory",
    "",
    "- [今天学习日记 Retrieval Evaluation](study/retrieval-evaluation.md) - 今天学习日记的跨天 Retrieval Evaluation 方向",
    "- [健身饮食记录](study/css-grid.md) - 每周力量训练和营养安排",
    "- [家庭旅行预算](project/plugin-deployment.md) - 夏季旅行的交通住宿预算",
    "",
  ].join("\n");
  await mkdir(path.join(temporaryVault, "memory", "project"), { recursive: true });
  const studyMemoryFile = await app.vault.create("memory/study/retrieval-evaluation.md", studyMemoryContent);
  const otherStudyMemoryFile = await app.vault.create("memory/study/css-grid.md", otherStudyMemory);
  const projectMemoryFile = await app.vault.create("memory/project/plugin-deployment.md", projectMemory);
  const memoryIndexFile = await app.vault.create("memory/MEMORY.md", memoryIndexContent);
  await acceptAnotherDailyPlan("memory-informed cross-day continuity");
  const memoryInformedPlan = await readFile(path.join(temporaryVault, existingDaily.path), "utf8");
  assert.match(
    memoryInformedPlan,
    /未来三天学习 Retrieval Evaluation，并根据每日结果调整顺序。（来源：memory\/study\/retrieval-evaluation\.md）/,
  );
  const updatedStudyMemory = await readFile(
    path.join(temporaryVault, "memory", "study", "retrieval-evaluation.md"),
    "utf8",
  );
  assert.match(updatedStudyMemory, /Current direction: .*Retrieval Evaluation/);
  assert.match(updatedStudyMemory, /Last planned for 2026-07-14/);
  assert.doesNotMatch(updatedStudyMemory, /整理 3 个核心问答/);
  assert.equal(
    await readFile(path.join(temporaryVault, "memory", "MEMORY.md"), "utf8"),
    memoryIndexContent,
  );
  globalThis.Date = class extends previousDate {
    constructor(...args) {
      super(...(args.length > 0 ? args : ["2026-07-15T12:00:00"]));
    }

    static now() {
      return previousDate.now();
    }
  };
  await acceptAnotherDailyPlan("next-day Study Memory continuity");
  const nextDayPlan = await readFile(path.join(temporaryVault, "daily", "2026-07-15.md"), "utf8");
  assert.match(nextDayPlan, /Retrieval Evaluation/);
  assert.doesNotMatch(nextDayPlan, /健身饮食记录|家庭旅行预算/);
  const nextDayStudyMemory = await readFile(
    path.join(temporaryVault, "memory", "study", "retrieval-evaluation.md"),
    "utf8",
  );
  assert.match(nextDayStudyMemory, /Last planned for 2026-07-15/);
  assert.doesNotMatch(nextDayStudyMemory, /Last planned for 2026-07-14/);
  assert.equal((nextDayStudyMemory.match(/Current direction:/g) ?? []).length, 1);
  await acceptAnotherDailyPlan("current-request direction correction", "今天改学 RAG evaluation");
  const redirectedPlan = await readFile(path.join(temporaryVault, "daily", "2026-07-15.md"), "utf8");
  assert.match(redirectedPlan, /- \[ \] RAG evaluation（来源：当前用户明确请求）/);
  assert.doesNotMatch(redirectedPlan, /- \[ \].*Retrieval Evaluation/);
  const correctedStudyMemory = await readFile(
    path.join(temporaryVault, "memory", "study", "retrieval-evaluation.md"),
    "utf8",
  );
  assert.match(correctedStudyMemory, /Current direction: RAG evaluation/);
  assert.doesNotMatch(correctedStudyMemory, /未来三天学习 Retrieval Evaluation/);
  assert.doesNotMatch(correctedStudyMemory, /整理 3 个核心问答/);
  const correctedMemoryIndex = await readFile(path.join(temporaryVault, "memory", "MEMORY.md"), "utf8");
  assert.match(correctedMemoryIndex, /\[RAG evaluation\]\(study\/retrieval-evaluation\.md\) - Current cross-day RAG evaluation direction/);
  assert.doesNotMatch(correctedMemoryIndex, /\[今天学习日记 Retrieval Evaluation\]/);
  assert.match(correctedMemoryIndex, /\[健身饮食记录\]/);
  assert.match(correctedMemoryIndex, /\[家庭旅行预算\]/);

  await app.vault.modify(
    projectMemoryFile,
    "---\nname: \"今天学习日记 Agentic RL Project\"\ndescription: \"今天学习日记的跨天 Agentic RL 项目方向\"\ntype: project\n---\n\n推进 Agentic RL evaluation 项目并验证训练指标。\n",
  );
  await app.vault.modify(
    memoryIndexFile,
    correctedMemoryIndex.replace(
      "- [家庭旅行预算](project/plugin-deployment.md) - 夏季旅行的交通住宿预算",
      "- [今天学习日记 Agentic RL Project](project/plugin-deployment.md) - 今天学习日记的跨天 Agentic RL 项目方向",
    ),
  );
  globalThis.Date = class extends previousDate {
    constructor(...args) {
      super(...(args.length > 0 ? args : ["2026-07-16T12:00:00"]));
    }

    static now() {
      return previousDate.now();
    }
  };
  await acceptAnotherDailyPlan(
    "relevant Project Memory drives the next day",
    "帮我按 Agentic RL Project 做今天的学习日记",
  );
  const projectInformedPlan = await readFile(path.join(temporaryVault, "daily", "2026-07-16.md"), "utf8");
  assert.match(projectInformedPlan, /推进 Agentic RL evaluation 项目并验证训练指标。（来源：memory\/project\/plugin-deployment\.md）/);
  assert.doesNotMatch(projectInformedPlan, /RAG evaluation（来源：memory\/study/);
  const projectInformedMemoryFile = vaultFiles.find(
    ({ path: vaultPath }) => vaultPath === "memory/study/project-informed-direction.md",
  );
  assert.ok(projectInformedMemoryFile);
  assert.match(
    await readFile(path.join(temporaryVault, projectInformedMemoryFile.path), "utf8"),
    /Current direction: 推进 Agentic RL evaluation 项目并验证训练指标。/,
  );
  assert.match(
    await readFile(path.join(temporaryVault, "memory", "MEMORY.md"), "utf8"),
    /\[Project-informed daily study direction\]\(study\/project-informed-direction\.md\)/,
  );
  globalThis.Date = class extends previousDate {
    constructor(...args) {
      super(...(args.length > 0 ? args : ["2026-07-17T12:00:00"]));
    }

    static now() {
      return previousDate.now();
    }
  };
  await acceptAnotherDailyPlan(
    "same Project Memory continues without duplicate derived memory",
    "帮我按 Agentic RL Project 做今天的学习日记",
  );
  assert.match(
    await readFile(path.join(temporaryVault, "daily", "2026-07-17.md"), "utf8"),
    /推进 Agentic RL evaluation 项目并验证训练指标。（来源：memory\/project\/plugin-deployment\.md）/,
  );
  const continuedProjectMemory = await readFile(
    path.join(temporaryVault, projectInformedMemoryFile.path),
    "utf8",
  );
  assert.match(continuedProjectMemory, /Last planned for 2026-07-17/);
  assert.equal((continuedProjectMemory.match(/Current direction:/g) ?? []).length, 1);
  const continuedProjectIndex = await readFile(path.join(temporaryVault, "memory", "MEMORY.md"), "utf8");
  assert.equal((continuedProjectIndex.match(/study\/project-informed-direction\.md/g) ?? []).length, 1);

  await app.vault.delete(studyMemoryFile);
  await app.vault.delete(otherStudyMemoryFile);
  await app.vault.delete(projectMemoryFile);
  await app.vault.delete(projectInformedMemoryFile);
  await app.vault.delete(memoryIndexFile);
  globalThis.Date = class extends previousDate {
    constructor(...args) {
      super(...(args.length > 0 ? args : ["2026-07-18T12:00:00"]));
    }

    static now() {
      return previousDate.now();
    }
  };
  await acceptAnotherDailyPlan("current-request direction without existing memory", "今天改学 RAG evaluation");
  const noMemoryDirectionPlan = await readFile(path.join(temporaryVault, "daily", "2026-07-18.md"), "utf8");
  assert.match(noMemoryDirectionPlan, /- \[ \] RAG evaluation（来源：当前用户明确请求）/);
  assert.match(
    await readFile(path.join(temporaryVault, "memory", "study", "rag-evaluation.md"), "utf8"),
    /Current direction: RAG evaluation/,
  );
  assert.match(
    await readFile(path.join(temporaryVault, "memory", "MEMORY.md"), "utf8"),
    /\[RAG evaluation\]\(study\/rag-evaluation\.md\) - Current cross-day RAG evaluation direction/,
  );
  const recreatedRagMemory = vaultFiles.find(({ path: vaultPath }) => vaultPath === "memory/study/rag-evaluation.md");
  const recreatedMemoryIndex = vaultFiles.find(({ path: vaultPath }) => vaultPath === "memory/MEMORY.md");
  assert.ok(recreatedRagMemory && recreatedMemoryIndex);
  await app.vault.modify(
    recreatedRagMemory,
    "---\nname: \"今天学习日记 Graph Evaluation\"\ndescription: \"今天学习日记的跨天 Graph Evaluation 方向\"\ntype: study\n---\n\nCurrent direction: Graph evaluation\n",
  );
  await app.vault.modify(
    recreatedMemoryIndex,
    "# Planning Memory\n\n- [今天学习日记 Graph Evaluation](study/rag-evaluation.md) - 今天学习日记的跨天 Graph Evaluation 方向\n",
  );
  globalThis.Date = class extends previousDate {
    constructor(...args) {
      super(...(args.length > 0 ? args : ["2026-07-19T12:00:00"]));
    }

    static now() {
      return previousDate.now();
    }
  };
  await acceptAnotherDailyPlan("historical direction does not override current memory");
  const postCorrectionPlan = await readFile(path.join(temporaryVault, "daily", "2026-07-19.md"), "utf8");
  assert.match(postCorrectionPlan, /Graph evaluation（来源：memory\/study\/rag-evaluation\.md）/);
  assert.doesNotMatch(postCorrectionPlan, /当前用户明确请求/);

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
        expectedVersion: `mtime:${fallbackNote.stat.mtime}:size:${fallbackNote.stat.size}`,
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
    () => activeView.contentEl.findByClass("offeragent-sidebar__send")?.disabled === false,
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
    () => activeView.contentEl.findByClass("offeragent-sidebar__send")?.disabled === false,
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
    .filter((activity) =>
      activity
        .findAllByClass("offeragent-sidebar__tool-activity-label")
        .some((label) => label.text.includes("Read notes/example.md"))
    ).length;
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
            activity
              .findAllByClass("offeragent-sidebar__tool-activity-label")
              .some((label) => label.text.includes("Read notes/example.md")),
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
    plugin.settingTabs[0].containerEl
      .findAllByClass("dropdown")
      .find(({ value }) => value === "read_only")?.value,
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
