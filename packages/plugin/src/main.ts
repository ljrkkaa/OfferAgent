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
  moment,
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
import { resolveLocalToday } from "./daily-note-context";
import { createHostResearchBrowser, type ResearchBrowser } from "./research-browser";

const SIDEBAR_VIEW_TYPE = "offeragent-sidebar";
const localMoment = moment as unknown as {
  (): { format(format: string): string };
  (date: string, format: string, strict: boolean): { format(format: string): string };
};
const DEFAULT_SETTINGS: OfferAgentPluginSettings = {
  fastMode: false,
  vaultPermissionMode: "trusted_vault",
};

interface OfferAgentPluginSettings {
  fastMode: boolean;
  vaultPermissionMode: VaultPermissionMode;
}

function permissionMode(value: unknown): VaultPermissionMode {
  return value === "ask_every_time" || value === "read_only" || value === "trusted_vault"
    ? value
    : DEFAULT_SETTINGS.vaultPermissionMode;
}

const PERMISSION_LABELS: Record<VaultPermissionMode, string> = {
  ask_every_time: "Ask Every Time",
  read_only: "Read Only",
  trusted_vault: "Trusted Vault",
};

class OfferAgentSettingTab extends PluginSettingTab {
  readonly #owner: OfferAgentPlugin;

  constructor(app: App, owner: OfferAgentPlugin) {
    super(app, owner);
    this.#owner = owner;
  }

  display(): void {
    this.containerEl.empty();
    const viewModel = this.#owner.getSidebarViewModel();
    const presentation = viewModel.presentation.settings;
    this.containerEl.createEl("h2", { text: "OfferAgent" });
    new Setting(this.containerEl)
      .setName("Runtime status")
      .setDesc(
        presentation.runtimeStatus === "connected"
          ? "Connected to the local OfferAgent Runtime."
          : presentation.advanced.diagnostics,
      );
    new Setting(this.containerEl)
      .setName("Provider status")
      .setDesc(
        presentation.providerStatus === "connected"
          ? "Codex subscription Provider is available."
          : "Provider is unavailable; check authentication and Runtime diagnostics.",
      );
    new Setting(this.containerEl)
      .setName("Model")
      .setDesc("Model used for this Conversation.")
      .addDropdown((dropdown) => {
        for (const model of viewModel.conversation.models) {
          dropdown.addOption(model.id, model.label);
        }
        dropdown
          .setValue(viewModel.conversation.selectedModelId ?? "")
          .onChange(async (value) => this.#owner.selectModel(value));
      });
    if (presentation.fastMode) {
      new Setting(this.containerEl)
        .setName("Fast Mode")
        .setDesc("Uses the Provider's faster priority processing and may consume more credits.")
        .addToggle((toggle) =>
          toggle
            .setValue(presentation.fastMode!.enabled)
            .onChange(async (value) => this.#owner.setFastMode(value)),
        );
    }
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
    const advanced = this.containerEl.createEl("details", {
      cls: "offeragent-settings__advanced",
    });
    advanced.createEl("summary", { text: "Advanced" });
    const advancedContent = advanced.createDiv({ cls: "offeragent-settings__advanced-content" });
    new Setting(advancedContent)
      .setName("Git Checkpoint retention")
      .setDesc(presentation.advanced.gitRetention);
    new Setting(advancedContent)
      .setName("Diagnostics")
      .setDesc(presentation.advanced.diagnostics);
    const hostedSearch = new Setting(advancedContent)
      .setName("Hosted Web Search")
      .setDesc("Capability status: unknown. Status is discovered from the active backend and model.")
      .addButton((button) =>
        button.setButtonText("Reprobe").onClick(async () => {
          button.setDisabled(true);
          hostedSearch.setDesc("Capability status: probing…");
          try {
            const result = await this.#owner.reprobeHostedWebSearch();
            hostedSearch.setDesc(`Capability status for ${result.modelId}: ${result.status}.`);
          } catch (error) {
            hostedSearch.setDesc(
              `Capability probe failed: ${error instanceof Error ? error.message : String(error)}`,
            );
          } finally {
            button.setDisabled(false);
          }
        }),
      );
    void this.#owner.getHostedWebSearchCapability().then((result) => {
      hostedSearch.setDesc(`Capability status for ${result.modelId}: ${result.status}.`);
    }).catch((error: unknown) => {
      hostedSearch.setDesc(
        `Capability status unavailable: ${error instanceof Error ? error.message : String(error)}`,
      );
    });
  }
}

class OfferAgentSidebarView extends ItemView {
  readonly #controller: SidebarController;
  readonly #openSettings: () => void;
  readonly #previewUrls: string[] = [];
  #unsubscribe?: () => void;

  constructor(leaf: WorkspaceLeaf, controller: SidebarController, openSettings: () => void) {
    super(leaf);
    this.#controller = controller;
    this.#openSettings = openSettings;
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
    this.#revokePreviewUrls();
  }

  #revokePreviewUrls(): void {
    for (const url of this.#previewUrls.splice(0)) URL.revokeObjectURL(url);
  }

  #render(viewModel: SidebarViewModel): void {
    const container = this.contentEl;
    this.#revokePreviewUrls();
    container.empty();
    container.addClass("offeragent-sidebar");

    const header = container.createDiv({ cls: "offeragent-sidebar__header" });
    const brand = header.createDiv({ cls: "offeragent-sidebar__brand" });
    brand.createEl("h2", {
      cls: "offeragent-sidebar__title",
      text: viewModel.title,
    });
    const status = brand.createDiv({
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

    const conversationRow = header.createDiv({ cls: "offeragent-sidebar__conversations" });
    const conversationSelect = conversationRow.createEl("select", {
      cls: "offeragent-sidebar__conversation-select",
    });
    for (const conversation of viewModel.conversation.conversations) {
      const option = conversationSelect.createEl("option", { text: conversation.title });
      option.value = conversation.id;
    }
    conversationSelect.value = viewModel.conversation.activeConversationId ?? "";
    conversationSelect.setAttribute("aria-label", "Conversation history");
    conversationSelect.disabled = viewModel.conversation.runState === "streaming";
    conversationSelect.addEventListener("change", () => {
      void this.#controller.openConversation(conversationSelect.value);
    });
    const newConversation = conversationRow.createEl("button", {
      cls: "offeragent-sidebar__new-conversation",
      text: "New",
    });
    newConversation.type = "button";
    newConversation.setAttribute("aria-label", "New Conversation");
    newConversation.disabled = viewModel.conversation.runState === "streaming";
    newConversation.addEventListener("click", () => {
      void this.#controller.createConversation();
    });
    const deleteConversation = conversationRow.createEl("button", {
      cls: "offeragent-sidebar__delete-conversation",
      text: "Delete",
    });
    deleteConversation.type = "button";
    deleteConversation.setAttribute("aria-label", "Delete Conversation");
    deleteConversation.disabled =
      !viewModel.conversation.activeConversationId ||
      viewModel.conversation.runState === "streaming";
    deleteConversation.addEventListener("click", () => {
      void this.#controller.deleteCurrentConversation();
    });
    const settings = conversationRow.createEl("button", {
      cls: "offeragent-sidebar__settings",
      text: "Settings",
    });
    settings.type = "button";
    settings.setAttribute("aria-label", "Open OfferAgent settings");
    settings.addEventListener("click", this.#openSettings);

    const transcript = container.createDiv({ cls: "offeragent-sidebar__transcript" });
    if (viewModel.presentation.transcript.length === 0) {
      transcript.createDiv({
        cls: "offeragent-sidebar__empty",
        text: "OfferAgent is ready for a conversation.",
      });
    }

    const inlineRunDiagnostic = viewModel.presentation.transcript.some(
      (item) => item.kind === "run_status" && item.message,
    );
    if (viewModel.conversation.error && !inlineRunDiagnostic) {
      const diagnostic = transcript.createDiv({
        cls: "offeragent-sidebar__diagnostic offeragent-sidebar__provider-error",
        text: viewModel.conversation.error.message,
      });
      diagnostic.dataset.code = viewModel.conversation.error.code;
    }

    for (const item of viewModel.presentation.transcript) {
      if (item.kind === "message") {
        const { message } = item;
        const messageElement = transcript.createDiv({
          cls: `offeragent-sidebar__message offeragent-sidebar__message--${message.role}`,
          text: message.text,
        });
        if (message.citations?.length) {
          const sources = messageElement.createDiv({ cls: "offeragent-sidebar__citations" });
          for (const [index, citation] of message.citations.entries()) {
            const link = sources.createEl("a", {
              cls: "offeragent-sidebar__citation",
              text: `[${index + 1}] ${citation.title}`,
            });
            link.href = citation.url;
            link.target = "_blank";
            link.rel = "noopener noreferrer";
          }
        }
      } else if (item.kind === "vault_change") {
        const { change: batch } = item;
        const card = transcript.createDiv({ cls: "offeragent-sidebar__change-batch" });
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
      } else if (item.kind === "activity") {
        const presented = item.activity;
        const activity = transcript.createEl("details", {
          cls: "offeragent-sidebar__tool-activity",
        });
        activity.dataset.status = presented.status;
        const summary = activity.createEl("summary", {
          text: presented.label,
        });
        summary.setAttribute("aria-expanded", "false");
        summary.setAttribute("aria-label", `${presented.label}. Press Enter to show details.`);
        activity.addEventListener("toggle", () => {
          summary.setAttribute("aria-expanded", activity.open ? "true" : "false");
        });
        activity.createEl("pre", {
          text: presented.details,
        });
      } else {
        const runStatus = transcript.createDiv({
          cls: "offeragent-sidebar__run-status",
          text: item.message ? `${item.label} ${item.message}` : item.label,
        });
        runStatus.dataset.agentRunId = item.agentRunId;
        runStatus.dataset.status = item.status;
      }
    }

    const composer = container.createEl("form", { cls: "offeragent-sidebar__composer" });
    const context = composer.createDiv({ cls: "offeragent-sidebar__context" });
    for (const chip of viewModel.presentation.composer.contextChips) {
      context.createDiv({
        cls: "offeragent-sidebar__context-chip",
        text: chip.label,
      });
    }
    if (viewModel.presentation.settings.fastMode?.enabled) {
      context.createDiv({
        cls: "offeragent-sidebar__context-chip offeragent-sidebar__context-chip--fast",
        text: "Fast Mode",
      });
    }
    const input = composer.createEl("textarea", { cls: "offeragent-sidebar__input" });
    input.placeholder = "Ask OfferAgent…";
    input.setAttribute("aria-label", "Message OfferAgent");
    input.value = viewModel.presentation.composer.draftText;
    input.disabled = viewModel.presentation.composer.primaryAction.kind !== "send";
    input.addEventListener("input", () => this.#controller.setComposerDraft(input.value));
    const acceptImages = async (files: File[]): Promise<void> => {
      this.#controller.setComposerDraft(input.value);
      let finishImport: (() => void) | undefined;
      try {
        finishImport = this.#controller.beginAttachmentImport(files.map((file) => ({
          mediaType: file.type,
          size: file.size,
        })));
        const images = await Promise.all(files.map(async (file) => ({
          bytes: new Uint8Array(await file.arrayBuffer()),
          fileName: file.name,
          mediaType: file.type,
        })));
        this.#controller.attachImages(images);
      } catch {
        // The controller preserves the prior complete draft and publishes validation errors.
      } finally {
        finishImport?.();
      }
    };
    input.addEventListener("paste", (event) => {
      const files = [...(event.clipboardData?.files ?? [])];
      if (files.length === 0) return;
      event.preventDefault();
      void acceptImages(files);
    });
    composer.addEventListener("dragover", (event) => {
      if (event.dataTransfer?.files.length) event.preventDefault();
    });
    composer.addEventListener("drop", (event) => {
      const files = [...(event.dataTransfer?.files ?? [])];
      if (files.length === 0) return;
      event.preventDefault();
      void acceptImages(files);
    });
    for (const [index, presented] of viewModel.presentation.composer.attachments.entries()) {
      const attachment = composer.createDiv({ cls: "offeragent-sidebar__attachment" });
      const previewBytes = Uint8Array.from(presented.previewBytes);
      const previewUrl = URL.createObjectURL(new Blob([previewBytes.buffer], {
        type: presented.mediaType,
      }));
      this.#previewUrls.push(previewUrl);
      const preview = attachment.createEl("img", {
        cls: "offeragent-sidebar__attachment-preview",
      });
      preview.src = previewUrl;
      preview.setAttribute("alt", `Preview ${index + 1}: ${presented.fileName}`);
      attachment.createDiv({
        text: `${index + 1}. ${presented.fileName} (${Math.ceil(presented.size / 1024)} KiB)`,
      });
      const moveUp = attachment.createEl("button", { text: "Up" });
      moveUp.type = "button";
      moveUp.disabled = viewModel.presentation.composer.isPreparingAttachments || index === 0;
      moveUp.setAttribute("aria-label", `Move ${presented.fileName} earlier`);
      moveUp.addEventListener("click", () => this.#controller.moveDraftImage(index, -1));
      const moveDown = attachment.createEl("button", { text: "Down" });
      moveDown.type = "button";
      moveDown.disabled = viewModel.presentation.composer.isPreparingAttachments ||
        index === viewModel.presentation.composer.attachments.length - 1;
      moveDown.setAttribute("aria-label", `Move ${presented.fileName} later`);
      moveDown.addEventListener("click", () => this.#controller.moveDraftImage(index, 1));
      const removeAttachment = attachment.createEl("button", {
        cls: "offeragent-sidebar__attachment-remove",
        text: "Remove",
      });
      removeAttachment.type = "button";
      removeAttachment.disabled = viewModel.presentation.composer.isPreparingAttachments;
      removeAttachment.setAttribute("aria-label", `Remove ${presented.fileName}`);
      removeAttachment.addEventListener("click", () => this.#controller.removeDraftImage(index));
    }
    const controls = composer.createDiv({ cls: "offeragent-sidebar__composer-controls" });
    const filePicker = controls.createEl("input", { cls: "offeragent-sidebar__file-picker" });
    filePicker.type = "file";
    filePicker.multiple = true;
    filePicker.accept = "image/png,image/jpeg,image/webp,image/gif";
    filePicker.setAttribute("aria-label", "Choose up to 20 images");
    filePicker.disabled = viewModel.presentation.composer.isPreparingAttachments;
    filePicker.addEventListener("change", () => {
      const files = [...(filePicker.files ?? [])];
      if (files.length > 0) void acceptImages(files);
    });
    const attachButton = controls.createEl("button", {
      cls: "offeragent-sidebar__attach",
      text: "Attach images",
    });
    attachButton.type = "button";
    attachButton.disabled = viewModel.presentation.composer.primaryAction.kind !== "send" ||
      viewModel.presentation.composer.isPreparingAttachments;
    attachButton.addEventListener("click", () => filePicker.click());
    const modelSelect = controls.createEl("select", {
      cls: "offeragent-sidebar__model-select",
    });
    modelSelect.setAttribute("aria-label", "Conversation model");
    for (const model of viewModel.conversation.models) {
      const option = modelSelect.createEl("option", { text: model.label });
      option.value = model.id;
    }
    modelSelect.value = viewModel.conversation.selectedModelId ?? "";
    modelSelect.disabled =
      viewModel.conversation.models.length === 0 ||
      viewModel.presentation.composer.primaryAction.kind !== "send";
    modelSelect.addEventListener("change", () => {
      void this.#controller.selectModel(modelSelect.value);
    });
    controls.createDiv({
      cls: "offeragent-sidebar__permission",
      text: PERMISSION_LABELS[viewModel.presentation.composer.permissionMode],
    });
    const primary = viewModel.presentation.composer.primaryAction;
    const primaryButton = controls.createEl("button", {
      cls: `offeragent-sidebar__primary offeragent-sidebar__${primary.kind}`,
      text: primary.label,
    });
    primaryButton.type = primary.kind === "send" ? "submit" : "button";
    primaryButton.disabled =
      viewModel.runtime.state !== "connected" ||
      (primary.kind === "send" &&
        (!viewModel.conversation.selectedModelId ||
          viewModel.presentation.composer.isPreparingAttachments));
    if (primary.kind === "stop") {
      primaryButton.addEventListener("click", () => this.#controller.stopAgentRun());
    } else if (primary.kind === "resume" && primary.agentRunId) {
      primaryButton.addEventListener("click", () => {
        void this.#controller.resumeAgentRun(primary.agentRunId!);
      });
    }
    composer.addEventListener("submit", (event) => {
      event.preventDefault();
      if (primary.kind !== "send") return;
      const text = input.value;
      if (!text.trim() && viewModel.presentation.composer.attachments.length === 0) return;
      this.#controller.setComposerDraft(text);
      void this.#controller.sendMessage(text);
    });
  }
}

export default class OfferAgentPlugin extends Plugin {
  #controller?: SidebarController;
  #researchBrowser?: ResearchBrowser;
  #runtime?: RuntimeSupervisor;
  #settings: OfferAgentPluginSettings = { ...DEFAULT_SETTINGS };

  async onload(): Promise<void> {
    if (!Platform.isDesktopApp) {
      throw new Error("OfferAgent requires the desktop version of Obsidian.");
    }
    const stored = (await this.loadData()) as Partial<OfferAgentPluginSettings> | null;
    this.#settings = {
      fastMode: stored?.fastMode === true,
      vaultPermissionMode: permissionMode(stored?.vaultPermissionMode),
    };
    this.addSettingTab(new OfferAgentSettingTab(this.app, this));

    const runtimePath = this.#runtimePath();
    const vaultRoot = this.#vaultRoot();
    const readTools = new ObsidianVaultToolAdapter(
      this.app.vault,
      this.app.metadataCache,
      undefined,
      {
        readConfiguration: async () => {
          const configurationPath = `${this.app.vault.configDir}/daily-notes.json`;
          if (!(await this.app.vault.adapter.exists(configurationPath))) return undefined;
          return JSON.parse(await this.app.vault.adapter.read(configurationPath)) as unknown;
        },
        resolveToday: () => resolveLocalToday(),
        formatDate: (date, format) => localMoment(date, "YYYY-MM-DD", true).format(format),
      },
    );
    const researchBrowser = createHostResearchBrowser();
    this.#researchBrowser = researchBrowser;
    let runtime!: RuntimeSupervisor;
    const checkpointStore = new GitCheckpointStore(vaultRoot);
    const changeCoordinator = new VaultChangeCoordinator(
      new ObsidianVaultChangeFileApi(this.app.vault, vaultRoot),
      checkpointStore,
      {
        list: (states) => runtime.listVaultChangeBatches(states),
        markApplying: (batchId, checkpointRef, targets) =>
          runtime.markVaultChangeApplying(batchId, checkpointRef, targets),
        markState: (batchId, state) => runtime.markVaultChangeState(batchId, state),
      },
      () => {},
      () => this.#settings.vaultPermissionMode,
      checkpointStore,
    );
    runtime = new RuntimeSupervisor({
      runtimePath,
      statePath: process.env.OFFERAGENT_RUNTIME_STATE_PATH,
      onCancelAgentRun: (agentRunId) => researchBrowser.cancelRun(agentRunId),
      toolExecutor: {
        execute: (event) =>
          event.tool.name === "vault_propose_changes"
            ? changeCoordinator.execute(event)
            : event.tool.name === "research_browser"
              ? researchBrowser.execute(event)
            : readTools.execute(event),
      },
    });
    this.#runtime = runtime;
    this.#controller = new SidebarController(runtime, changeCoordinator, {
      getFastModeEnabled: () => this.#settings.fastMode,
      getVaultPermissionMode: () => this.#settings.vaultPermissionMode,
    });

    this.registerView(
      SIDEBAR_VIEW_TYPE,
      (leaf) => new OfferAgentSidebarView(
        leaf,
        this.#requiredController(),
        () => this.#openSettings(),
      ),
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
    await this.#researchBrowser?.close();
    this.app.workspace.detachLeavesOfType(SIDEBAR_VIEW_TYPE);
  }

  getVaultPermissionMode(): VaultPermissionMode {
    return this.#settings.vaultPermissionMode;
  }

  getSidebarViewModel(): SidebarViewModel {
    return this.#requiredController().getViewModel();
  }

  async selectModel(modelId: string): Promise<void> {
    await this.#requiredController().selectModel(modelId);
  }

  async setFastMode(fastMode: boolean): Promise<void> {
    this.#settings = { ...this.#settings, fastMode };
    await this.saveData(this.#settings);
    this.#requiredController().refreshPresentation();
  }

  async getHostedWebSearchCapability() {
    const modelId = this.#controller?.getViewModel().conversation.selectedModelId;
    if (!modelId) throw new Error("Select a model before checking Hosted Web Search.");
    if (!this.#runtime) throw new Error("OfferAgent Runtime is not connected.");
    return this.#runtime.getHostedWebSearchCapability(modelId);
  }

  async reprobeHostedWebSearch() {
    const modelId = this.#controller?.getViewModel().conversation.selectedModelId;
    if (!modelId) throw new Error("Select a model before probing Hosted Web Search.");
    if (!this.#runtime) throw new Error("OfferAgent Runtime is not connected.");
    return this.#runtime.reprobeHostedWebSearch(modelId);
  }

  async setVaultPermissionMode(vaultPermissionMode: VaultPermissionMode): Promise<void> {
    this.#settings = { ...this.#settings, vaultPermissionMode };
    await this.saveData(this.#settings);
    this.#requiredController().refreshPresentation();
  }

  #openSettings(): void {
    const setting = (this.app as App & {
      setting?: { open(): void; openTabById(id: string): void };
    }).setting;
    setting?.open();
    setting?.openTabById(this.manifest.id);
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
