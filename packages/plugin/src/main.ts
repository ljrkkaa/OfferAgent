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
    } else {
      container.createDiv({
        cls: "offeragent-sidebar__empty",
        text: "OfferAgent is ready for a conversation.",
      });
    }
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
