import path from "node:path";
import {
  FileSystemAdapter,
  ItemView,
  Notice,
  Platform,
  Plugin,
  type WorkspaceLeaf,
} from "obsidian";
import {
  RuntimeSupervisor,
  SidebarController,
  type SidebarViewModel,
} from "./sidebar-controller";

const SIDEBAR_VIEW_TYPE = "offeragent-sidebar";

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

  async onload(): Promise<void> {
    if (!Platform.isDesktopApp) {
      throw new Error("OfferAgent requires the desktop version of Obsidian.");
    }

    const runtimePath = this.#runtimePath();
    this.#controller = new SidebarController(
      new RuntimeSupervisor({ runtimePath }),
    );

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

    void this.#controller.start().catch((error: unknown) => {
      const message = error instanceof Error ? error.message : String(error);
      new Notice(message, 10_000);
    });
  }

  async onunload(): Promise<void> {
    await this.#controller?.stop();
    this.app.workspace.detachLeavesOfType(SIDEBAR_VIEW_TYPE);
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
    const adapter = this.app.vault.adapter;
    if (!(adapter instanceof FileSystemAdapter)) {
      throw new Error("OfferAgent requires a local filesystem Vault.");
    }
    const pluginDirectory =
      this.manifest.dir ??
      path.join(this.app.vault.configDir, "plugins", this.manifest.id);
    return path.join(adapter.getBasePath(), pluginDirectory, "runtime.js");
  }
}
