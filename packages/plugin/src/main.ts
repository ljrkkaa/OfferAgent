import path from "node:path";
import {
  type App,
  FileSystemAdapter,
  ItemView,
  Notice,
  Platform,
  Plugin,
  PluginSettingTab,
  Setting,
  type WorkspaceLeaf,
} from "obsidian";
import {
  RuntimeSupervisor,
  SidebarController,
  type SidebarViewModel,
} from "./sidebar-controller";
import { ObsidianVaultToolAdapter } from "./vault-tool-adapter";
import {
  GitCheckpointStore,
  ObsidianVaultChangeFileApi,
  VaultChangeCoordinator,
  type VaultPermissionMode,
} from "./vault-change-coordinator";

const SIDEBAR_VIEW_TYPE = "offeragent-sidebar";
const DEFAULT_SETTINGS: OfferAgentPluginSettings = { vaultPermissionMode: "trusted_vault" };

interface OfferAgentPluginSettings {
  vaultPermissionMode: VaultPermissionMode;
}

function permissionMode(value: unknown): VaultPermissionMode {
  return value === "ask_every_time" || value === "read_only" || value === "trusted_vault"
    ? value
    : DEFAULT_SETTINGS.vaultPermissionMode;
}

class OfferAgentSettingTab extends PluginSettingTab {
  readonly #owner: OfferAgentPlugin;

  constructor(app: App, owner: OfferAgentPlugin) {
    super(app, owner);
    this.#owner = owner;
  }

  display(): void {
    this.containerEl.empty();
    new Setting(this.containerEl)
      .setName("Vault Permission Mode")
      .setDesc("Controls Agent-requested writes for this Vault. Control files always require confirmation.")
      .addDropdown((dropdown) =>
        dropdown
          .addOption("trusted_vault", "Trusted Vault")
          .addOption("ask_every_time", "Ask Every Time")
          .addOption("read_only", "Read Only")
          .setValue(this.#owner.getVaultPermissionMode())
          .onChange(async (value) => {
            await this.#owner.setVaultPermissionMode(permissionMode(value));
          }),
      );
  }
}

class OfferAgentSidebarView extends ItemView {
  readonly #controller: SidebarController;
  #unsubscribe?: () => void;

  constructor(leaf: WorkspaceLeaf, controller: SidebarController) {
    super(leaf);
    this.#controller = controller;
  }

  getViewType(): string {
    return SIDEBAR_VIEW_TYPE;
  }

  getDisplayText(): string {
    return "OfferAgent";
  }

  getIcon(): string {
    return "sparkles";
  }

  async onOpen(): Promise<void> {
    this.#unsubscribe = this.#controller.subscribe((viewModel) => {
      this.#render(viewModel);
    });
  }

  async onClose(): Promise<void> {
    this.#unsubscribe?.();
    this.#unsubscribe = undefined;
  }

  #render(viewModel: SidebarViewModel): void {
    const container = this.contentEl;
    container.empty();
    container.addClass("offeragent-sidebar");

    const header = container.createDiv({ cls: "offeragent-sidebar__header" });
    header.createEl("h2", {
      cls: "offeragent-sidebar__title",
      text: viewModel.title,
    });
    const status = header.createDiv({
      cls: "offeragent-sidebar__status",
      text: viewModel.runtime.state,
    });
    status.dataset.state = viewModel.runtime.state;

    if (viewModel.runtime.message) {
      container.createDiv({
        cls: "offeragent-sidebar__diagnostic",
        text: viewModel.runtime.message,
      });
    }

    const conversationRow = container.createDiv({ cls: "offeragent-sidebar__conversations" });
    const conversationSelect = conversationRow.createEl("select", {
      cls: "offeragent-sidebar__conversation-select",
    });
    for (const conversation of viewModel.conversation.conversations) {
      const option = conversationSelect.createEl("option", { text: conversation.title });
      option.value = conversation.id;
    }
    conversationSelect.value = viewModel.conversation.activeConversationId ?? "";
    conversationSelect.disabled = viewModel.conversation.runState === "streaming";
    conversationSelect.addEventListener("change", () => {
      void this.#controller.openConversation(conversationSelect.value);
    });
    const newConversation = conversationRow.createEl("button", {
      cls: "offeragent-sidebar__new-conversation",
      text: "New",
    });
    newConversation.type = "button";
    newConversation.disabled = viewModel.conversation.runState === "streaming";
    newConversation.addEventListener("click", () => {
      void this.#controller.createConversation();
    });
    const deleteConversation = conversationRow.createEl("button", {
      cls: "offeragent-sidebar__delete-conversation",
      text: "Delete",
    });
    deleteConversation.type = "button";
    deleteConversation.disabled =
      !viewModel.conversation.activeConversationId ||
      viewModel.conversation.runState === "streaming";
    deleteConversation.addEventListener("click", () => {
      void this.#controller.deleteCurrentConversation();
    });

    const modelRow = container.createDiv({ cls: "offeragent-sidebar__models" });
    modelRow.createEl("label", { text: "Model" });
    const modelSelect = modelRow.createEl("select", {
      cls: "offeragent-sidebar__model-select",
    });
    for (const model of viewModel.conversation.models) {
      const option = modelSelect.createEl("option", { text: model.label });
      option.value = model.id;
    }
    modelSelect.value = viewModel.conversation.selectedModelId ?? "";
    modelSelect.disabled =
      viewModel.conversation.models.length === 0 ||
      viewModel.conversation.runState === "streaming";
    modelSelect.addEventListener("change", () => {
      void this.#controller.selectModel(modelSelect.value);
    });

    const transcript = container.createDiv({ cls: "offeragent-sidebar__transcript" });
    if (viewModel.conversation.messages.length === 0) {
      container.createDiv({
        cls: "offeragent-sidebar__empty",
        text: "OfferAgent is ready for a conversation.",
      });
    } else {
      for (const message of viewModel.conversation.messages) {
        transcript.createDiv({
          cls: `offeragent-sidebar__message offeragent-sidebar__message--${message.role}`,
          text: message.text,
        });
      }
    }

    if (viewModel.conversation.error) {
      const diagnostic = container.createDiv({
        cls: "offeragent-sidebar__diagnostic offeragent-sidebar__provider-error",
        text: viewModel.conversation.error.message,
      });
      diagnostic.dataset.code = viewModel.conversation.error.code;
    }

    for (const batch of viewModel.conversation.vaultChanges) {
      const card = container.createDiv({ cls: "offeragent-sidebar__change-batch" });
      card.dataset.status = batch.status;
      card.createEl("h3", { text: batch.task });
      card.createDiv({
        cls: "offeragent-sidebar__change-batch-status",
        text: `Vault Change Batch: ${batch.status}`,
      });
      if (batch.message) {
        card.createDiv({
          cls: "offeragent-sidebar__change-policy-decision",
          text: batch.message,
        });
      }
      const actions = card.createEl("ul", { cls: "offeragent-sidebar__change-actions" });
      for (const action of batch.actions) {
        actions.createEl("li", { text: `${action.operation}: ${action.path}` });
      }
      for (const conflict of batch.conflicts ?? []) {
        card.createEl("pre", {
          cls: "offeragent-sidebar__change-conflict",
          text: conflict.diff,
        });
      }
      if (batch.status === "pending") {
        const apply = card.createEl("button", {
          cls: "offeragent-sidebar__change-apply",
          text: "Apply all",
        });
        apply.type = "button";
        apply.addEventListener("click", () => {
          void this.#controller.decideVaultChange(batch.toolCallId, "apply");
        });
        const reject = card.createEl("button", {
          cls: "offeragent-sidebar__change-reject",
          text: "Reject all",
        });
        reject.type = "button";
        reject.addEventListener("click", () => {
          void this.#controller.decideVaultChange(batch.toolCallId, "reject");
        });
      } else if (batch.status === "applied") {
        const undo = card.createEl("button", {
          cls: "offeragent-sidebar__change-undo",
          text: "Undo",
        });
        undo.type = "button";
        undo.addEventListener("click", () => {
          void this.#controller.undoVaultChange(batch.batchId);
        });
      }
    }

    if (viewModel.conversation.toolCalls.length > 0) {
      const activities = container.createDiv({ cls: "offeragent-sidebar__tool-activities" });
      for (const call of viewModel.conversation.toolCalls) {
        if (call.name === "vault_propose_changes") continue;
        const activity = activities.createEl("details", {
          cls: "offeragent-sidebar__tool-activity",
        });
        activity.dataset.status = call.status;
        activity.createEl("summary", {
          text: `${call.name} · ${call.status}`,
        });
        activity.createEl("pre", {
          text: JSON.stringify(call.arguments, null, 2),
        });
      }
    }

    if (viewModel.conversation.agentRuns.length > 0) {
      const runList = container.createDiv({ cls: "offeragent-sidebar__run-list" });
      for (const [index, run] of viewModel.conversation.agentRuns.entries()) {
        const runStatus = runList.createDiv({
          cls: "offeragent-sidebar__run-status",
          text: `Run ${index + 1}: ${run.status}`,
        });
        runStatus.dataset.agentRunId = run.id;
        runStatus.dataset.status = run.status;
      }
    }

    const composer = container.createEl("form", { cls: "offeragent-sidebar__composer" });
    const input = composer.createEl("textarea", { cls: "offeragent-sidebar__input" });
    input.placeholder = "Ask OfferAgent…";
    input.disabled = viewModel.conversation.runState === "streaming";
    const submit = composer.createEl("button", {
      cls: "offeragent-sidebar__send",
      text: viewModel.conversation.runState === "streaming" ? "Working…" : "Send",
    });
    submit.type = "submit";
    submit.disabled =
      viewModel.runtime.state !== "connected" ||
      !viewModel.conversation.selectedModelId ||
      viewModel.conversation.runState === "streaming";
    if (viewModel.conversation.runState === "streaming") {
      const stop = composer.createEl("button", {
        cls: "offeragent-sidebar__stop",
        text: "Stop",
      });
      stop.type = "button";
      stop.addEventListener("click", () => this.#controller.stopAgentRun());
    }
    composer.addEventListener("submit", (event) => {
      event.preventDefault();
      const text = input.value;
      if (!text.trim()) return;
      input.value = "";
      void this.#controller.sendMessage(text);
    });
  }
}

export default class OfferAgentPlugin extends Plugin {
  #controller?: SidebarController;
  #settings: OfferAgentPluginSettings = { ...DEFAULT_SETTINGS };

  async onload(): Promise<void> {
    if (!Platform.isDesktopApp) {
      throw new Error("OfferAgent requires the desktop version of Obsidian.");
    }
    const stored = (await this.loadData()) as Partial<OfferAgentPluginSettings> | null;
    this.#settings = {
      vaultPermissionMode: permissionMode(stored?.vaultPermissionMode),
    };
    this.addSettingTab(new OfferAgentSettingTab(this.app, this));

    const runtimePath = this.#runtimePath();
    const vaultRoot = this.#vaultRoot();
    const readTools = new ObsidianVaultToolAdapter(this.app.vault, this.app.metadataCache);
    let runtime!: RuntimeSupervisor;
    const changeCoordinator = new VaultChangeCoordinator(
      new ObsidianVaultChangeFileApi(this.app.vault, vaultRoot),
      new GitCheckpointStore(vaultRoot),
      {
        list: (states) => runtime.listVaultChangeBatches(states),
        markApplying: (batchId, checkpointRef, targets) =>
          runtime.markVaultChangeApplying(batchId, checkpointRef, targets),
        markState: (batchId, state) => runtime.markVaultChangeState(batchId, state),
      },
      () => {},
      () => this.#settings.vaultPermissionMode,
    );
    runtime = new RuntimeSupervisor({
      runtimePath,
      statePath: process.env.OFFERAGENT_RUNTIME_STATE_PATH,
      toolExecutor: {
        execute: (event) =>
          event.tool.name === "vault_propose_changes"
            ? changeCoordinator.execute(event)
            : readTools.execute(event),
      },
    });
    this.#controller = new SidebarController(runtime, changeCoordinator);

    this.registerView(
      SIDEBAR_VIEW_TYPE,
      (leaf) => new OfferAgentSidebarView(leaf, this.#requiredController()),
    );
    this.addRibbonIcon("sparkles", "Open OfferAgent", () => {
      void this.#openSidebar();
    });
    this.addCommand({
      id: "open-offeragent-sidebar",
      name: "Open OfferAgent sidebar",
      callback: () => {
        void this.#openSidebar();
      },
    });

    void (async () => {
      await runtime.start();
      await changeCoordinator.reconcile();
      await this.#controller?.start();
    })().catch((error: unknown) => {
      const message = error instanceof Error ? error.message : String(error);
      new Notice(message, 10_000);
    });
  }

  async onunload(): Promise<void> {
    await this.#controller?.stop();
    this.app.workspace.detachLeavesOfType(SIDEBAR_VIEW_TYPE);
  }

  getVaultPermissionMode(): VaultPermissionMode {
    return this.#settings.vaultPermissionMode;
  }

  async setVaultPermissionMode(vaultPermissionMode: VaultPermissionMode): Promise<void> {
    this.#settings = { vaultPermissionMode };
    await this.saveData(this.#settings);
  }

  async #openSidebar(): Promise<void> {
    const existing = this.app.workspace.getLeavesOfType(SIDEBAR_VIEW_TYPE)[0];
    if (existing) {
      await this.app.workspace.revealLeaf(existing);
      return;
    }

    const leaf = this.app.workspace.getRightLeaf(false);
    if (!leaf) {
      new Notice("OfferAgent could not open the right sidebar.");
      return;
    }
    await leaf.setViewState({ type: SIDEBAR_VIEW_TYPE, active: true });
    await this.app.workspace.revealLeaf(leaf);
  }

  #requiredController(): SidebarController {
    if (!this.#controller) throw new Error("OfferAgent controller is not initialized.");
    return this.#controller;
  }

  #runtimePath(): string {
    const vaultRoot = this.#vaultRoot();
    const pluginDirectory =
      this.manifest.dir ?? path.join(this.app.vault.configDir, "plugins", this.manifest.id);
    return path.join(vaultRoot, pluginDirectory, "runtime.js");
  }

  #vaultRoot(): string {
    const adapter = this.app.vault.adapter;
    if (!(adapter instanceof FileSystemAdapter)) {
      throw new Error("OfferAgent requires a local filesystem Vault.");
    }
    return adapter.getBasePath();
  }
}
