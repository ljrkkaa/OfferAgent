import path from "node:path";
import type { ConversationSummary, PinnedContextReference } from "@offeragent/protocol";
import {
  Component,
  type App,
  FileSystemAdapter,
  ItemView,
  MarkdownRenderer,
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
import { ProjectEvidenceAdapter } from "./project-evidence-adapter";
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

interface SidebarContextSources {
  currentDocumentPath(): string | undefined;
  listDocuments(query: string): string[];
  openDocument(path: string, lineStart?: number, lineEnd?: number): Promise<void>;
}

function mentionQuery(
  text: string,
  cursor: number,
): { end: number; query: string; start: number } | undefined {
  const prefix = text.slice(0, cursor);
  const match = /(?:^|\s)@([^\s@]*)$/u.exec(prefix);
  if (!match) return undefined;
  return { end: cursor, query: match[1] ?? "", start: prefix.lastIndexOf("@") };
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

const PERMISSION_ACCESSIBLE_LABELS: Record<VaultPermissionMode, string> = {
  ask_every_time: "Vault 权限模式：每次询问",
  read_only: "Vault 权限模式：只读",
  trusted_vault: "Vault 权限模式：信任 Vault",
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
  readonly #contextSources: SidebarContextSources;
  readonly #openSettings: () => void;
  readonly #markdownRenders = new Map<string, {
    owner?: Component;
    timer?: ReturnType<typeof setTimeout>;
  }>();
  readonly #draftPreviewUrls: string[] = [];
  readonly #messagePreviewUrls = new Map<string, string>();
  readonly #messagePreviewElements = new Map<string, HTMLImageElement>();
  readonly #loadingMessagePreviews = new Set<string>();
  readonly #attachmentObservers = new Set<IntersectionObserver>();
  #attachmentLoadGeneration = 0;
  #attachmentLoadQueue: Array<() => Promise<void>> = [];
  #activeAttachmentLoads = 0;
  readonly #streamedMessageElements = new Map<string, HTMLDivElement>();
  readonly #transcriptItemElements = new Map<string, HTMLElement>();
  #composerInput?: HTMLTextAreaElement;
  #focusHistorySearch = false;
  #historyOpen = false;
  #addMenuOpen = false;
  #documentChooserOpen = false;
  #documentChooserQuery = "";
  #mentionDismissed = false;
  #mentionVisible = false;
  #mentionIndex = 0;
  #restoreHistoryFocus = false;
  #showArchived = false;
  #newContentButton?: HTMLButtonElement;
  #renderedViewModel?: SidebarViewModel;
  #transcriptElement?: HTMLDivElement;
  #unsubscribe?: () => void;

  constructor(
    leaf: WorkspaceLeaf,
    controller: SidebarController,
    openSettings: () => void,
    contextSources: SidebarContextSources,
  ) {
    super(leaf);
    this.#controller = controller;
    this.#openSettings = openSettings;
    this.#contextSources = contextSources;
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

  #tryAddPinnedContext(reference: PinnedContextReference): boolean {
    try {
      this.#controller.addPinnedContext(reference);
      return true;
    } catch (error) {
      new Notice(error instanceof Error ? error.message : String(error));
      return false;
    }
  }

  async onOpen(): Promise<void> {
    this.#unsubscribe = this.#controller.subscribe((viewModel) => {
      this.#render(viewModel);
    });
  }

  async onClose(): Promise<void> {
    this.#unsubscribe?.();
    this.#unsubscribe = undefined;
    this.#disposeMarkdownRenders();
    this.#resetAttachmentLoads();
    this.#revokeDraftPreviewUrls();
    for (const url of this.#messagePreviewUrls.values()) URL.revokeObjectURL(url);
    this.#messagePreviewUrls.clear();
  }

  #revokeDraftPreviewUrls(): void {
    for (const url of this.#draftPreviewUrls.splice(0)) URL.revokeObjectURL(url);
  }

  #messageAttachmentKey(
    message: SidebarViewModel["conversation"]["messages"][number],
    attachment: NonNullable<SidebarViewModel["conversation"]["messages"][number]["attachments"]>[number],
  ): string {
    return `${message.id ?? message.agentRunId}:${attachment.order}:${attachment.contentHash}`;
  }

  #resetAttachmentLoads(): void {
    this.#attachmentLoadGeneration += 1;
    this.#attachmentLoadQueue = [];
    for (const observer of this.#attachmentObservers) observer.disconnect();
    this.#attachmentObservers.clear();
    this.#messagePreviewElements.clear();
    this.#loadingMessagePreviews.clear();
  }

  #pruneMessagePreviewUrls(viewModel: SidebarViewModel): void {
    const retained = new Set<string>();
    for (const message of viewModel.conversation.messages) {
      for (const attachment of message.attachments ?? []) {
        retained.add(this.#messageAttachmentKey(message, attachment));
      }
    }
    for (const [key, url] of this.#messagePreviewUrls) {
      if (retained.has(key)) continue;
      URL.revokeObjectURL(url);
      this.#messagePreviewUrls.delete(key);
      this.#messagePreviewElements.delete(key);
    }
  }

  #releaseMessagePreview(key: string): void {
    const url = this.#messagePreviewUrls.get(key);
    if (!url) return;
    URL.revokeObjectURL(url);
    this.#messagePreviewUrls.delete(key);
    const element = this.#messagePreviewElements.get(key);
    if (element?.src === url) element.src = "";
  }

  #cacheMessagePreview(key: string, url: string): void {
    this.#releaseMessagePreview(key);
    this.#messagePreviewUrls.set(key, url);
  }

  #enqueueAttachmentLoad(load: () => Promise<void>): void {
    this.#attachmentLoadQueue.push(load);
    this.#drainAttachmentLoads();
  }

  #drainAttachmentLoads(): void {
    while (this.#activeAttachmentLoads < 4) {
      const load = this.#attachmentLoadQueue.shift();
      if (!load) return;
      this.#activeAttachmentLoads += 1;
      void load().finally(() => {
        this.#activeAttachmentLoads -= 1;
        this.#drainAttachmentLoads();
      });
    }
  }

  #observeAttachment(load: () => void, unload: () => void, element: HTMLImageElement): void {
    if (typeof IntersectionObserver === "undefined") {
      load();
      return;
    }
    const observer = new IntersectionObserver((entries) => {
      if (entries.some(({ isIntersecting }) => isIntersecting)) load();
      else unload();
    }, { root: this.#transcriptElement, rootMargin: "200px" });
    this.#attachmentObservers.add(observer);
    observer.observe(element);
  }

  #transcriptItemKey(
    item: SidebarViewModel["presentation"]["transcript"][number],
    index: number,
  ): string {
    if (item.kind === "message") {
      return `message:${item.key}`;
    }
    if (item.kind === "activity_error") return `activity-error:${item.activity.id}`;
    if (item.kind === "activity_summary") return `activity-summary:${item.agentRunId}`;
    if (item.kind === "vault_change") return `vault-change:${item.change.toolCallId}`;
    return `run-status:${item.agentRunId}:${index}`;
  }

  #streamingFrameSignature(viewModel: SidebarViewModel): string {
    const {
      agentRuns: _agentRuns,
      messages: _messages,
      toolCalls: _toolCalls,
      vaultChanges: _vaultChanges,
      ...conversation
    } = viewModel.conversation;
    return JSON.stringify({
      conversation,
      presentation: {
        composer: {
          ...viewModel.presentation.composer,
          draftText: undefined,
        },
        settings: viewModel.presentation.settings,
      },
      runtime: viewModel.runtime,
      title: viewModel.title,
    });
  }

  #appendMessage(
    transcript: HTMLDivElement,
    item: Extract<SidebarViewModel["presentation"]["transcript"][number], { kind: "message" }>,
    index: number,
  ): HTMLDivElement {
    const { message } = item;
    const messageElement = transcript.createDiv({
      cls: `offeragent-sidebar__message offeragent-sidebar__message--${message.role} offeragent-sidebar__message--${item.presentation.layout}`,
    });
    if (message.attachments?.length) {
      const gallery = messageElement.createDiv({ cls: "offeragent-sidebar__message-attachments" });
      for (const [attachmentIndex, attachment] of message.attachments.entries()) {
        const preview = gallery.createEl("img", {
          cls: "offeragent-sidebar__message-attachment",
        });
        preview.loading = "lazy";
        preview.setAttribute(
          "alt",
          `Attachment ${attachmentIndex + 1}: ${attachment.fileName}`,
        );
        const previewKey = this.#messageAttachmentKey(message, attachment);
        this.#messagePreviewElements.set(previewKey, preview);
        const generation = this.#attachmentLoadGeneration;
        let visible = false;
        const load = (): void => {
          visible = true;
          const cachedUrl = this.#messagePreviewUrls.get(previewKey);
          if (cachedUrl) {
            this.#messagePreviewUrls.delete(previewKey);
            this.#messagePreviewUrls.set(previewKey, cachedUrl);
            preview.src = cachedUrl;
            return;
          }
          if (this.#loadingMessagePreviews.has(previewKey)) return;
          this.#loadingMessagePreviews.add(previewKey);
          this.#enqueueAttachmentLoad(async () => {
            try {
              const bytes = await this.#controller.readMessageAttachment(message, attachment.order);
              if (generation !== this.#attachmentLoadGeneration || !visible) return;
              const previewBytes = Uint8Array.from(bytes);
              const previewUrl = URL.createObjectURL(new Blob([previewBytes.buffer], {
                type: attachment.mediaType,
              }));
              this.#cacheMessagePreview(previewKey, previewUrl);
              preview.src = previewUrl;
              if (message.id) {
                this.#controller.releaseOptimisticAttachmentPreview(message.agentRunId, attachment.order);
              }
            } catch {
              preview.addClass("offeragent-sidebar__message-attachment--unavailable");
            } finally {
              this.#loadingMessagePreviews.delete(previewKey);
            }
          });
        };
        this.#observeAttachment(load, () => {
          visible = false;
          this.#releaseMessagePreview(previewKey);
        }, preview);
      }
    }
    const messageBody = messageElement.createDiv({ cls: "offeragent-sidebar__message-body" });
    const key = this.#transcriptItemKey(item, index);
    this.#streamedMessageElements.set(key, messageBody);
    if (item.presentation.format === "markdown") {
      messageBody.addClass("markdown-rendered");
      this.#scheduleMarkdownRender(key, messageBody, message.text, 0);
    } else messageBody.setText(message.text);
    if (item.presentation.copyable && message.text) {
      const copy = messageElement.createEl("button", {
        cls: "offeragent-sidebar__message-copy",
        text: "复制回答",
      });
      copy.type = "button";
      copy.setAttribute("aria-label", "复制完整回答");
      copy.addEventListener("click", () => this.#copyText(message.text));
    }
    if (message.citations?.length) {
      const sources = messageElement.createDiv({ cls: "offeragent-sidebar__citations" });
      for (const [citationIndex, citation] of message.citations.entries()) {
        const link = sources.createEl("a", {
          cls: "offeragent-sidebar__citation",
          text: `[${citationIndex + 1}] ${citation.title}`,
        });
        link.href = citation.url;
        link.target = "_blank";
        link.rel = "noopener noreferrer";
      }
    }
    if (message.role === "assistant" && item.presentation.sourceLabel) {
      const usedSources = messageElement.createEl("details", {
        cls: "offeragent-sidebar__used-sources",
      });
      const summary = usedSources.createEl("summary", { text: item.presentation.sourceLabel });
      summary.setAttribute("aria-label", `${item.presentation.sourceLabel}，按回车展开来源`);
      for (const source of item.presentation.usedSources ?? []) {
        const sourceRow = usedSources.createDiv({ cls: "offeragent-sidebar__used-source" });
        const identity = sourceRow.createEl("button", {
          cls: "offeragent-sidebar__used-source-open",
          text: `${source.path}:${source.lineStart}-${source.lineEnd}`,
        });
        identity.type = "button";
        identity.setAttribute(
          "aria-label",
          `打开来源 ${source.path} 第 ${source.lineStart} 到 ${source.lineEnd} 行`,
        );
        identity.addEventListener("click", () => {
          void this.#contextSources.openDocument(source.path, source.lineStart, source.lineEnd);
        });
        if (source.stale) {
          sourceRow.createEl("span", {
            cls: "offeragent-sidebar__used-source-stale",
            text: "来源已变化",
          });
        }
        sourceRow.createDiv({
          cls: "offeragent-sidebar__used-source-snippet",
          text: source.snippet,
        });
        const pin = sourceRow.createEl("button", {
          cls: "offeragent-sidebar__used-source-pin",
          text: "固定到下一轮",
        });
        pin.type = "button";
        pin.setAttribute("aria-label", `固定来源 ${source.path} 到下一轮`);
        pin.addEventListener("click", () => {
          try {
            this.#controller.pinEvidenceSource(source);
          } catch (error) {
            new Notice(error instanceof Error ? error.message : String(error));
          }
        });
      }
    }
    return messageElement;
  }

  #populateActivitySummary(
    activity: HTMLDetailsElement,
    item: Extract<SidebarViewModel["presentation"]["transcript"][number], {
      kind: "activity_summary";
    }>,
  ): void {
    const wasOpen = activity.open;
    activity.empty();
    activity.open = wasOpen;
    activity.dataset.status = item.activities.every(({ status }) => status === "completed")
      ? "completed"
      : "requested";
    const summary = activity.createEl("summary", { text: item.label });
    summary.setAttribute("aria-expanded", wasOpen ? "true" : "false");
    summary.setAttribute("aria-label", `${item.label}，按回车展开详情`);
    activity.ontoggle = () => {
      summary.setAttribute("aria-expanded", activity.open ? "true" : "false");
    };
    for (const presented of item.activities) {
      const row = activity.createDiv({ cls: "offeragent-sidebar__tool-activity-row" });
      row.createDiv({ cls: "offeragent-sidebar__tool-activity-label", text: presented.label });
      row.createEl("pre", { text: presented.details });
    }
  }

  #appendActivitySummary(
    transcript: HTMLDivElement,
    item: Extract<SidebarViewModel["presentation"]["transcript"][number], {
      kind: "activity_summary";
    }>,
  ): HTMLDetailsElement {
    const activity = transcript.createEl("details", { cls: "offeragent-sidebar__tool-activity" });
    this.#populateActivitySummary(activity, item);
    return activity;
  }

  #appendActivityError(
    transcript: HTMLDivElement,
    item: Extract<SidebarViewModel["presentation"]["transcript"][number], {
      kind: "activity_error";
    }>,
  ): HTMLDivElement {
    const error = transcript.createDiv({ cls: "offeragent-sidebar__activity-error" });
    error.dataset.status = item.activity.status;
    error.createDiv({ text: item.activity.label });
    error.createEl("pre", { text: item.activity.details });
    return error;
  }

  #tryPatchStreamingUpdate(previous: SidebarViewModel, next: SidebarViewModel): boolean {
    const transcript = this.#transcriptElement;
    const input = this.#composerInput;
    if (
      !transcript ||
      !input ||
      previous.conversation.runState !== "streaming" ||
      next.conversation.runState !== "streaming" ||
      previous.conversation.activeConversationId !== next.conversation.activeConversationId ||
      this.#streamingFrameSignature(previous) !== this.#streamingFrameSignature(next)
    ) return false;

    const previousScrollTop = transcript.scrollTop;
    const previousItems = new Map(previous.presentation.transcript.map((item, index) => [
      this.#transcriptItemKey(item, index),
      item,
    ]));
    const nextKeys = new Set<string>();
    for (const [index, item] of next.presentation.transcript.entries()) {
      const key = this.#transcriptItemKey(item, index);
      nextKeys.add(key);
      const prior = previousItems.get(key);
      let itemElement = this.#transcriptItemElements.get(key);
      if (!itemElement) {
        if (item.kind === "message") itemElement = this.#appendMessage(transcript, item, index);
        else if (item.kind === "activity_summary") {
          itemElement = this.#appendActivitySummary(transcript, item);
        } else if (item.kind === "activity_error") {
          itemElement = this.#appendActivityError(transcript, item);
        } else return false;
        this.#transcriptItemElements.set(key, itemElement);
      }
      if (!prior || JSON.stringify(prior) === JSON.stringify(item)) continue;
      if (item.kind === "message" && prior.kind === "message") {
        const element = this.#streamedMessageElements.get(key);
        if (!element) return false;
        if (item.presentation.format === "markdown") {
          this.#scheduleMarkdownRender(key, element, item.message.text);
        } else element.setText(item.message.text);
      } else if (item.kind === "activity_summary" && prior.kind === "activity_summary") {
        this.#populateActivitySummary(itemElement as HTMLDetailsElement, item);
      } else if (item.kind === "activity_error" && prior.kind === "activity_error") {
        itemElement.empty();
        itemElement.dataset.status = item.activity.status;
        itemElement.createDiv({ text: item.activity.label });
        itemElement.createEl("pre", { text: item.activity.details });
      } else return false;
    }
    for (const [key, element] of this.#transcriptItemElements) {
      if (nextKeys.has(key)) continue;
      element.remove();
      this.#transcriptItemElements.delete(key);
      this.#streamedMessageElements.delete(key);
      const render = this.#markdownRenders.get(key);
      if (render?.timer) clearTimeout(render.timer);
      if (render) this.#unloadMarkdownOwner(render);
      this.#markdownRenders.delete(key);
    }
    for (const [index, item] of next.presentation.transcript.entries()) {
      const element = this.#transcriptItemElements.get(this.#transcriptItemKey(item, index));
      if (!element) return false;
      transcript.appendChild(element);
    }

    if (input.value === previous.presentation.composer.draftText) {
      input.value = next.presentation.composer.draftText;
    }
    this.#syncTranscriptScroll(next, previousScrollTop);
    return true;
  }

  #copyText(text: string): void {
    void navigator.clipboard.writeText(text).catch(() => {
      new Notice("无法复制到剪贴板。");
    });
  }

  #disposeMarkdownRenders(): void {
    for (const render of this.#markdownRenders.values()) {
      if (render.timer) clearTimeout(render.timer);
      this.#unloadMarkdownOwner(render);
    }
    this.#markdownRenders.clear();
  }

  #unloadMarkdownOwner(render: { owner?: Component }): void {
    const owner = render.owner;
    render.owner = undefined;
    owner?.unload();
  }

  #postprocessMarkdown(element: HTMLDivElement): void {
    if (typeof element.querySelectorAll !== "function") return;
    for (const link of element.querySelectorAll<HTMLAnchorElement>(
      'a[href^="http://"], a[href^="https://"]',
    )) {
      link.target = "_blank";
      link.rel = "noopener noreferrer";
    }
    for (const code of element.querySelectorAll<HTMLElement>("pre > code")) {
      const pre = code.parentElement;
      if (
        !pre ||
        pre.querySelector(".copy-code-button") ||
        pre.querySelector(".offeragent-sidebar__code-copy")
      ) continue;
      const copy = pre.createEl("button", {
        cls: "offeragent-sidebar__code-copy",
        text: "复制代码",
      });
      copy.type = "button";
      copy.setAttribute("aria-label", "复制代码块");
      copy.addEventListener("click", () => this.#copyText(code.textContent ?? ""));
    }
  }

  #scheduleMarkdownRender(
    key: string,
    element: HTMLDivElement,
    markdown: string,
    delayMs = 50,
  ): void {
    const previous = this.#markdownRenders.get(key);
    if (previous?.timer) clearTimeout(previous.timer);
    if (previous) this.#unloadMarkdownOwner(previous);
    const render: {
      owner?: Component;
      timer?: ReturnType<typeof setTimeout>;
    } = {};
    const start = async () => {
      render.timer = undefined;
      if (this.#markdownRenders.get(key) !== render) return;
      const owner = new Component();
      owner.load();
      render.owner = owner;
      const staging = element.cloneNode(false) as HTMLDivElement;
      try {
        await MarkdownRenderer.render(this.app, markdown, staging, "", owner);
        if (
          this.#markdownRenders.get(key) !== render ||
          this.#streamedMessageElements.get(key) !== element
        ) {
          this.#unloadMarkdownOwner(render);
          return;
        }
        this.#postprocessMarkdown(staging);
        const transcript = this.#transcriptElement;
        const frozenScrollTop = transcript?.scrollTop;
        element.replaceChildren(...Array.from(staging.childNodes));
        element.dataset.renderStatus = "rendered";
        if (transcript && this.#renderedViewModel) {
          this.#syncTranscriptScroll(this.#renderedViewModel, frozenScrollTop);
        }
      } catch {
        this.#unloadMarkdownOwner(render);
        if (
          this.#markdownRenders.get(key) !== render ||
          this.#streamedMessageElements.get(key) !== element
        ) return;
        const transcript = this.#transcriptElement;
        const frozenScrollTop = transcript?.scrollTop;
        element.setText(markdown);
        element.dataset.renderStatus = "plain-text-fallback";
        new Notice("Markdown 渲染失败，已显示纯文本回答。");
        if (transcript && this.#renderedViewModel) {
          this.#syncTranscriptScroll(this.#renderedViewModel, frozenScrollTop);
        }
      }
    };
    this.#markdownRenders.set(key, render);
    if (delayMs <= 0) void start();
    else render.timer = setTimeout(() => void start(), delayMs);
  }

  #syncTranscriptScroll(viewModel: SidebarViewModel, frozenScrollTop?: number): void {
    const transcript = this.#transcriptElement;
    if (!transcript) return;
    const { hasNewContent, mode } = viewModel.presentation.transcriptScroll;
    if (this.#newContentButton) this.#newContentButton.hidden = !hasNewContent;
    if (mode === "following") transcript.scrollTop = transcript.scrollHeight;
    else if (frozenScrollTop !== undefined) transcript.scrollTop = frozenScrollTop;
  }

  #render(viewModel: SidebarViewModel): void {
    const previous = this.#renderedViewModel;
    if (previous && this.#tryPatchStreamingUpdate(previous, viewModel)) {
      this.#renderedViewModel = viewModel;
      return;
    }
    const frozenScrollTop = previous?.presentation.transcriptScroll.mode === "frozen" &&
        viewModel.presentation.transcriptScroll.mode === "frozen"
      ? this.#transcriptElement?.scrollTop
      : undefined;
    const restoreComposerFocus = typeof document !== "undefined" &&
      this.#composerInput === document.activeElement;
    const selectionStart = restoreComposerFocus ? this.#composerInput?.selectionStart : undefined;
    const selectionEnd = restoreComposerFocus ? this.#composerInput?.selectionEnd : undefined;
    const container = this.contentEl;
    this.#resetAttachmentLoads();
    this.#revokeDraftPreviewUrls();
    this.#pruneMessagePreviewUrls(viewModel);
    this.#disposeMarkdownRenders();
    this.#streamedMessageElements.clear();
    this.#transcriptItemElements.clear();
    this.#composerInput = undefined;
    this.#newContentButton = undefined;
    this.#transcriptElement = undefined;
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
    const activeConversation = viewModel.conversation.conversations.find(
      ({ id }) => id === viewModel.conversation.activeConversationId,
    );
    const history = conversationRow.createEl("button", {
      cls: "offeragent-sidebar__history",
      text: activeConversation?.title ?? "对话历史",
    });
    history.type = "button";
    history.setAttribute("aria-label", "打开对话历史");
    history.setAttribute("aria-expanded", `${this.#historyOpen}`);
    const conversationActionsDisabled = viewModel.conversation.runState === "streaming" ||
      viewModel.presentation.composer.isPreparingAttachments ||
      viewModel.presentation.composer.isSending;
    history.disabled = conversationActionsDisabled;
    history.addEventListener("click", () => {
      this.#historyOpen = !this.#historyOpen;
      this.#focusHistorySearch = this.#historyOpen;
      this.#restoreHistoryFocus = !this.#historyOpen;
      this.#render(viewModel);
    });
    if (this.#restoreHistoryFocus && !this.#historyOpen) {
      this.#restoreHistoryFocus = false;
      history.focus();
    }
    const newConversation = conversationRow.createEl("button", {
      cls: "offeragent-sidebar__new-conversation",
      text: "新建",
    });
    newConversation.type = "button";
    newConversation.setAttribute("aria-label", "新建对话");
    newConversation.disabled = conversationActionsDisabled;
    newConversation.addEventListener("click", () => {
      void this.#controller.createConversation();
    });
    const settings = conversationRow.createEl("button", {
      cls: "offeragent-sidebar__settings",
      text: "Settings",
    });
    settings.type = "button";
    settings.setAttribute("aria-label", "Open OfferAgent settings");
    settings.addEventListener("click", this.#openSettings);

    if (this.#historyOpen) {
      const drawer = header.createDiv({ cls: "offeragent-sidebar__history-drawer" });
      const search = drawer.createEl("input", { cls: "offeragent-sidebar__history-search" });
      search.type = "search";
      search.placeholder = "搜索对话";
      search.setAttribute("aria-label", "搜索对话标题");
      if (this.#focusHistorySearch) {
        this.#focusHistorySearch = false;
        search.focus();
      }
      const archiveToggle = drawer.createEl("button", {
        cls: "offeragent-sidebar__history-archive-toggle",
        text: this.#showArchived ? "显示当前对话" : "显示已归档",
      });
      archiveToggle.type = "button";
      archiveToggle.addEventListener("click", () => {
        this.#showArchived = !this.#showArchived;
        this.#render(viewModel);
      });
      const now = new Date();
      const today = new Date(now.getFullYear(), now.getMonth(), now.getDate()).getTime();
      const yesterday = today - 86_400_000;
      const grouped = new Map<string, ConversationSummary[]>();
      for (const conversation of viewModel.conversation.conversations.filter(
        ({ archived }) => archived === this.#showArchived,
      )) {
        const updated = new Date(conversation.updatedAt).getTime();
        const label = updated >= today ? "今天" : updated >= yesterday ? "昨天" : "更早";
        const entries = grouped.get(label) ?? [];
        entries.push(conversation);
        grouped.set(label, entries);
      }
      const searchable: Array<{ element: HTMLElement; title: string }> = [];
      for (const label of ["今天", "昨天", "更早"]) {
        const entries = grouped.get(label);
        if (!entries?.length) continue;
        const group = drawer.createDiv({ cls: "offeragent-sidebar__history-group" });
        group.createDiv({ cls: "offeragent-sidebar__history-group-label", text: label });
        for (const conversation of entries) {
          const item = group.createDiv({ cls: "offeragent-sidebar__history-item" });
          const select = item.createEl("button", {
            cls: "offeragent-sidebar__history-select",
            text: conversation.title,
          });
          select.type = "button";
          select.dataset.titleOrigin = conversation.titleOrigin;
          select.disabled = conversationActionsDisabled;
          select.addEventListener("click", () => {
            this.#historyOpen = false;
            this.#restoreHistoryFocus = true;
            void this.#controller.openConversation(conversation.id);
          });
          const updated = item.createEl("time", {
            cls: "offeragent-sidebar__history-updated",
            text: new Intl.DateTimeFormat("zh-CN", {
              hour: "2-digit",
              minute: "2-digit",
              month: "numeric",
              day: "numeric",
            }).format(new Date(conversation.updatedAt)),
          });
          updated.dateTime = conversation.updatedAt;
          const rename = item.createEl("button", { cls: "offeragent-sidebar__history-rename", text: "重命名" });
          rename.type = "button";
          rename.disabled = conversationActionsDisabled;
          rename.addEventListener("click", () => {
            const title = window.prompt("重命名对话", conversation.title);
            if (title !== null) void this.#controller.renameConversation(conversation.id, title);
          });
          const archive = item.createEl("button", {
            cls: "offeragent-sidebar__history-archive",
            text: conversation.archived ? "恢复" : "归档",
          });
          archive.type = "button";
          archive.disabled = conversationActionsDisabled;
          archive.addEventListener("click", () => {
            void this.#controller.setConversationArchived(conversation.id, !conversation.archived);
          });
          const remove = item.createEl("button", { cls: "offeragent-sidebar__history-delete", text: "删除" });
          remove.type = "button";
          remove.disabled = conversationActionsDisabled;
          remove.addEventListener("click", () => {
            if (!window.confirm(`确定永久删除“${conversation.title}”吗？`)) return;
            if (conversation.id === viewModel.conversation.activeConversationId) {
              void this.#controller.deleteCurrentConversation();
            } else {
              void this.#controller.openConversation(conversation.id)
                .then(() => this.#controller.deleteCurrentConversation());
            }
          });
          searchable.push({ element: item, title: conversation.title.toLocaleLowerCase() });
        }
      }
      search.addEventListener("input", () => {
        const query = search.value.trim().toLocaleLowerCase();
        for (const entry of searchable) entry.element.hidden = !entry.title.includes(query);
      });
    }

    const transcript = container.createDiv({ cls: "offeragent-sidebar__transcript" });
    this.#transcriptElement = transcript;
    transcript.addEventListener("scroll", () => {
      const distanceFromBottom = transcript.scrollHeight - transcript.clientHeight - transcript.scrollTop;
      if (Number.isFinite(distanceFromBottom)) {
        this.#controller.setTranscriptNearBottom(distanceFromBottom <= 32);
      }
    });
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

    const stoppedRevisionButtons: Array<{ agentRunId: string; button: HTMLButtonElement }> = [];
    for (const [index, item] of viewModel.presentation.transcript.entries()) {
      let itemElement: HTMLElement;
      if (item.kind === "message") {
        itemElement = this.#appendMessage(transcript, item, index);
      } else if (item.kind === "vault_change") {
        const { change: batch } = item;
        const card = transcript.createDiv({ cls: "offeragent-sidebar__change-batch" });
        itemElement = card;
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
      } else if (item.kind === "activity_error") {
        itemElement = this.#appendActivityError(transcript, item);
      } else if (item.kind === "activity_summary") {
        itemElement = this.#appendActivitySummary(transcript, item);
      } else {
        const runStatus = transcript.createDiv({
          cls: "offeragent-sidebar__run-status",
          text: item.message ? `${item.label} ${item.message}` : item.label,
        });
        runStatus.dataset.agentRunId = item.agentRunId;
        runStatus.dataset.status = item.status;
        if (item.revision) {
          const revise = runStatus.createEl("button", {
            cls: "offeragent-sidebar__revise-stopped",
            text: item.revision.label,
          });
          revise.type = "button";
          revise.disabled = !item.revision.enabled;
          revise.setAttribute("aria-label", "将已停止运行的原提示词放入输入框");
          if (!item.revision.enabled) {
            revise.setAttribute("title", "请先清空当前草稿和图片，再放入原提示词");
          }
          stoppedRevisionButtons.push({ agentRunId: item.agentRunId, button: revise });
          revise.addEventListener("click", () => {
            this.#controller.reviseStoppedRun(item.agentRunId);
          });
        }
        itemElement = runStatus;
      }
      this.#transcriptItemElements.set(this.#transcriptItemKey(item, index), itemElement);
    }

    const newContentButton = container.createEl("button", {
      cls: "offeragent-sidebar__new-content",
      text: "新内容",
    });
    this.#newContentButton = newContentButton;
    newContentButton.type = "button";
    newContentButton.setAttribute("aria-label", "查看最新内容");
    newContentButton.addEventListener("click", () => {
      this.#controller.resumeTranscriptFollowing();
      transcript.scrollTop = transcript.scrollHeight;
    });

    const composer = container.createEl("form", { cls: "offeragent-sidebar__composer" });
    const context = composer.createDiv({ cls: "offeragent-sidebar__context" });
    for (const [index, chip] of viewModel.presentation.composer.contextChips.entries()) {
      const chipElement = context.createDiv({
        cls: "offeragent-sidebar__context-chip offeragent-sidebar__context-chip--pinned",
      });
      const open = chipElement.createEl("button", {
        cls: "offeragent-sidebar__context-open",
        text: chip.label,
      });
      open.type = "button";
      open.setAttribute("aria-label", `Open pinned source ${chip.label}`);
      open.addEventListener("click", () => void this.#contextSources.openDocument(chip.path));
      const remove = chipElement.createEl("button", {
        cls: "offeragent-sidebar__context-remove",
        text: "×",
      });
      remove.type = "button";
      remove.setAttribute("aria-label", `Remove pinned source ${chip.label}`);
      remove.addEventListener("click", () => this.#controller.removePinnedContext(index));
    }
    if (viewModel.presentation.settings.fastMode?.enabled) {
      context.createDiv({
        cls: "offeragent-sidebar__context-chip offeragent-sidebar__context-chip--fast",
        text: "Fast Mode",
      });
    }
    const input = composer.createEl("textarea", { cls: "offeragent-sidebar__input" });
    this.#composerInput = input;
    input.placeholder = "向 OfferAgent 提问…";
    input.setAttribute("aria-label", "给 OfferAgent 的消息");
    input.value = viewModel.presentation.composer.draftText;
    let isComposing = false;
    let mentionRenderPending = false;
    input.addEventListener("input", () => {
      this.#mentionDismissed = false;
      this.#mentionIndex = 0;
      this.#controller.setComposerDraft(input.value);
      for (const { agentRunId, button } of stoppedRevisionButtons) {
        button.disabled = !this.#controller.canReviseStoppedRun(agentRunId);
        button.setAttribute(
          "title",
          button.disabled ? "请先清空当前草稿和图片，再放入原提示词" : "",
        );
      }
      const activeMention = mentionQuery(input.value, input.selectionStart ?? input.value.length);
      const shouldReconcileMention = Boolean(activeMention) || this.#mentionVisible;
      this.#mentionVisible = Boolean(activeMention);
      if (isComposing) {
        mentionRenderPending ||= shouldReconcileMention;
      } else if (shouldReconcileMention) {
        this.#render(this.#controller.getViewModel());
      }
    });
    let suppressCompositionEnter = false;
    input.addEventListener("compositionstart", () => {
      isComposing = true;
      suppressCompositionEnter = false;
    });
    input.addEventListener("compositionend", () => {
      isComposing = false;
      suppressCompositionEnter = true;
      if (mentionRenderPending) {
        mentionRenderPending = false;
        this.#render(this.#controller.getViewModel());
      }
      setTimeout(() => {
        suppressCompositionEnter = false;
      }, 0);
    });
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
      const clipboardText = event.clipboardData?.getData("text/plain") ?? "";
      if (clipboardText) {
        const start = input.selectionStart ?? input.value.length;
        const end = input.selectionEnd ?? start;
        input.value = `${input.value.slice(0, start)}${clipboardText}${input.value.slice(end)}`;
        const cursor = start + clipboardText.length;
        input.setSelectionRange(cursor, cursor);
        this.#controller.setComposerDraft(input.value);
      }
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
      attachment.draggable = !viewModel.presentation.composer.isPreparingAttachments;
      attachment.addEventListener("dragstart", (event) => {
        event.dataTransfer?.setData("application/x-offeragent-attachment-index", `${index}`);
      });
      attachment.addEventListener("dragover", (event) => {
        if (event.dataTransfer?.types.includes("application/x-offeragent-attachment-index")) {
          event.preventDefault();
        }
      });
      attachment.addEventListener("drop", (event) => {
        const transferType = "application/x-offeragent-attachment-index";
        if (!event.dataTransfer?.types.includes(transferType)) return;
        const payload = event.dataTransfer.getData(transferType);
        if (!/^\d+$/.test(payload)) return;
        const source = Number(payload);
        if (!Number.isSafeInteger(source)) return;
        event.preventDefault();
        this.#controller.reorderDraftImage(source, index);
      });
      const previewBytes = Uint8Array.from(presented.previewBytes);
      const previewUrl = URL.createObjectURL(new Blob([previewBytes.buffer], {
        type: presented.mediaType,
      }));
      this.#draftPreviewUrls.push(previewUrl);
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
    filePicker.setAttribute("aria-label", "添加图片（最多 20 张）");
    filePicker.disabled = viewModel.presentation.composer.isPreparingAttachments;
    filePicker.addEventListener("change", () => {
      const files = [...(filePicker.files ?? [])];
      if (files.length > 0) void acceptImages(files);
    });
    const addButton = controls.createEl("button", {
      cls: "offeragent-sidebar__add",
      text: "+",
    });
    addButton.type = "button";
    addButton.setAttribute("aria-label", "Add context or images");
    addButton.disabled = viewModel.presentation.composer.primaryAction.kind !== "send" ||
      viewModel.presentation.composer.isPreparingAttachments;
    addButton.addEventListener("click", () => {
      this.#addMenuOpen = !this.#addMenuOpen;
      this.#documentChooserOpen = false;
      this.#render(this.#controller.getViewModel());
    });
    if (this.#addMenuOpen) {
      const addMenu = composer.createDiv({ cls: "offeragent-sidebar__add-menu" });
      const pinCurrent = addMenu.createEl("button", {
        cls: "offeragent-sidebar__pin-current",
        text: "Pin current note",
      });
      pinCurrent.type = "button";
      pinCurrent.addEventListener("click", () => {
        const path = this.#contextSources.currentDocumentPath();
        if (path) {
          if (this.#tryAddPinnedContext({ kind: "document", path })) {
            this.#addMenuOpen = false;
            this.#render(this.#controller.getViewModel());
          }
        }
        else new Notice("Open a Vault note before pinning the current note.");
      });
      const chooseDocument = addMenu.createEl("button", {
        cls: "offeragent-sidebar__choose-document",
        text: "Choose Vault document",
      });
      chooseDocument.type = "button";
      chooseDocument.addEventListener("click", () => {
        this.#addMenuOpen = false;
        this.#documentChooserOpen = true;
        this.#documentChooserQuery = "";
        this.#render(this.#controller.getViewModel());
      });
      const addImages = addMenu.createEl("button", {
        cls: "offeragent-sidebar__attach",
        text: "Add images",
      });
      addImages.type = "button";
      addImages.addEventListener("click", () => filePicker.click());
    }
    const mention = !this.#mentionDismissed
      ? mentionQuery(
          input.value,
          restoreComposerFocus ? selectionStart ?? input.value.length : input.value.length,
        )
      : undefined;
    this.#mentionVisible = Boolean(mention);
    if (this.#documentChooserOpen) {
      const chooser = composer.createDiv({ cls: "offeragent-sidebar__document-chooser" });
      chooser.setAttribute("role", "listbox");
      const search = chooser.createEl("input", {
        cls: "offeragent-sidebar__document-search",
      });
      search.type = "search";
      search.value = this.#documentChooserQuery;
      search.placeholder = "Search Vault documents";
      search.setAttribute("aria-label", "Search Vault documents to pin");
      const results = chooser.createDiv({ cls: "offeragent-sidebar__document-results" });
      const renderResults = (): void => {
        results.empty();
        for (const path of this.#contextSources.listDocuments(search.value)) {
          const choice = results.createEl("button", {
            cls: "offeragent-sidebar__document-choice",
            text: path,
          });
          choice.type = "button";
          choice.addEventListener("click", () => {
            if (!this.#tryAddPinnedContext({ kind: "document", path })) return;
            this.#documentChooserOpen = false;
            this.#documentChooserQuery = "";
            this.#render(this.#controller.getViewModel());
          });
        }
      };
      search.addEventListener("input", () => {
        this.#documentChooserQuery = search.value;
        renderResults();
      });
      renderResults();
    } else if (mention) {
      const chooser = composer.createDiv({ cls: "offeragent-sidebar__document-chooser" });
      chooser.setAttribute("role", "listbox");
      for (const [index, path] of this.#contextSources.listDocuments(mention.query).entries()) {
        const choice = chooser.createEl("button", {
          cls: index === this.#mentionIndex
            ? "offeragent-sidebar__document-choice is-selected"
            : "offeragent-sidebar__document-choice",
          text: path,
        });
        choice.type = "button";
        choice.addEventListener("click", () => {
          if (!this.#tryAddPinnedContext({ kind: "document", path })) return;
          const next = `${input.value.slice(0, mention.start)}${input.value.slice(mention.end)}`;
          this.#mentionDismissed = true;
          this.#mentionVisible = false;
          this.#controller.setComposerDraft(next);
          this.#render(this.#controller.getViewModel());
        });
      }
    }
    const modelSelect = controls.createEl("select", {
      cls: "offeragent-sidebar__model-select",
    });
    modelSelect.setAttribute("aria-label", "选择对话模型");
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
    const permission = controls.createDiv({
      cls: "offeragent-sidebar__permission",
      text: PERMISSION_LABELS[viewModel.presentation.composer.permissionMode],
    });
    permission.setAttribute(
      "aria-label",
      PERMISSION_ACCESSIBLE_LABELS[viewModel.presentation.composer.permissionMode],
    );
    permission.setAttribute("role", "status");
    const primary = viewModel.presentation.composer.primaryAction;
    const primaryButton = controls.createEl("button", {
      cls: `offeragent-sidebar__primary offeragent-sidebar__${primary.kind}`,
      text: primary.label,
    });
    primaryButton.type = primary.kind === "send" ? "submit" : "button";
    primaryButton.setAttribute(
      "aria-label",
      primary.kind === "send"
        ? "发送消息"
        : primary.kind === "stop"
          ? "停止当前运行"
          : "继续中断的运行",
    );
    const canSend =
      primary.kind === "send" &&
      viewModel.runtime.state === "connected" &&
      Boolean(viewModel.conversation.selectedModelId) &&
      !viewModel.presentation.composer.isPreparingAttachments &&
      !viewModel.presentation.composer.isSending;
    primaryButton.disabled =
      viewModel.runtime.state !== "connected" ||
      (primary.kind === "send" && !canSend);
    if (primary.kind === "stop") {
      primaryButton.addEventListener("click", () => this.#controller.stopAgentRun());
    } else if (primary.kind === "resume" && primary.agentRunId) {
      primaryButton.addEventListener("click", () => {
        void this.#controller.resumeAgentRun(primary.agentRunId!);
      });
    }
    const submitDraft = () => {
      if (!canSend) return;
      const text = input.value;
      if (!text.trim() && viewModel.presentation.composer.attachments.length === 0) return;
      this.#controller.setComposerDraft(text);
      void this.#controller.sendMessage(text).catch(() => undefined);
    };
    input.addEventListener("keydown", (event) => {
      const activeMention = !this.#mentionDismissed
        ? mentionQuery(input.value, input.selectionStart ?? input.value.length)
        : undefined;
      const matches = activeMention
        ? this.#contextSources.listDocuments(activeMention.query)
        : [];
      if (activeMention && matches.length > 0 && (event.key === "ArrowDown" || event.key === "ArrowUp")) {
        event.preventDefault();
        const direction = event.key === "ArrowDown" ? 1 : -1;
        this.#mentionIndex = (this.#mentionIndex + direction + matches.length) % matches.length;
        for (const [index, choice] of Array.from(
          composer.querySelectorAll<HTMLButtonElement>(".offeragent-sidebar__document-choice"),
        ).entries()) {
          choice.classList.toggle("is-selected", index === this.#mentionIndex);
        }
        return;
      }
      if (activeMention && matches.length > 0 && event.key === "Enter" && !event.shiftKey) {
        event.preventDefault();
        const selectedPath = matches[Math.min(this.#mentionIndex, matches.length - 1)]!;
        if (!this.#tryAddPinnedContext({ kind: "document", path: selectedPath })) return;
        const cursor = input.selectionStart ?? input.value.length;
        const next = `${input.value.slice(0, activeMention.start)}${input.value.slice(cursor)}`;
        this.#mentionDismissed = true;
        this.#mentionVisible = false;
        this.#controller.setComposerDraft(next);
        this.#render(this.#controller.getViewModel());
        return;
      }
      if (activeMention && event.key === "Escape") {
        event.preventDefault();
        this.#mentionDismissed = true;
        this.#mentionVisible = false;
        this.#render(this.#controller.getViewModel());
        return;
      }
      if (
        event.key !== "Enter" ||
        event.shiftKey ||
        event.isComposing ||
        event.keyCode === 229 ||
        isComposing ||
        suppressCompositionEnter
      ) return;
      event.preventDefault();
      submitDraft();
    });
    composer.addEventListener("submit", (event) => {
      event.preventDefault();
      submitDraft();
    });
    this.#renderedViewModel = viewModel;
    this.#syncTranscriptScroll(viewModel, frozenScrollTop);
    if (restoreComposerFocus && typeof input.focus === "function") {
      input.focus();
      if (
        selectionStart !== undefined &&
        selectionEnd !== undefined &&
        typeof input.setSelectionRange === "function"
      ) input.setSelectionRange(selectionStart, selectionEnd);
    }
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
    const projectEvidence = new ProjectEvidenceAdapter(this.app.vault);
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
            : event.tool.name === "project_list" ||
                event.tool.name === "project_search" ||
                event.tool.name === "project_read"
              ? projectEvidence.execute(event)
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
        {
          currentDocumentPath: () => this.app.workspace.getActiveFile()?.path,
          listDocuments: (query) => {
            const normalized = query.trim().toLocaleLowerCase();
            return this.app.vault.getFiles()
              .filter((file) => file.extension === "md" && file.path.toLocaleLowerCase() !== "agent.md")
              .map((file) => file.path)
              .filter((path) => !normalized || path.toLocaleLowerCase().includes(normalized))
              .sort((left, right) => left.localeCompare(right))
              .slice(0, 8);
          },
          openDocument: async (path, lineStart, lineEnd) => {
            await this.app.workspace.openLinkText(path, "", false);
            if (lineStart === undefined || lineEnd === undefined) return;
            const editor = this.app.workspace.activeEditor?.editor;
            if (!editor) return;
            const lastLine = Math.max(0, editor.lineCount() - 1);
            const from = { line: Math.min(lastLine, Math.max(0, lineStart - 1)), ch: 0 };
            const targetLine = Math.min(lastLine, Math.max(from.line, lineEnd - 1));
            const to = { line: targetLine, ch: editor.getLine(targetLine).length };
            editor.setSelection(from, to);
            editor.scrollIntoView({ from, to }, true);
          },
        },
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
    this.addCommand({
      id: "pin-selection-to-offeragent",
      name: "Pin selection to OfferAgent",
      editorCallback: (editor, view) => {
        const path = view.file?.path;
        const selectedText = editor.getSelection();
        if (!path || !selectedText.trim()) {
          new Notice("Select text in a Vault note before pinning it to OfferAgent.");
          return;
        }
        const from = editor.getCursor("from");
        const to = editor.getCursor("to");
        const inclusiveEndLine = to.ch === 0 && to.line > from.line ? to.line : to.line + 1;
        try {
          this.#requiredController().addPinnedContext({
            kind: "selection",
            path,
            lineStart: from.line + 1,
            lineEnd: inclusiveEndLine,
          });
        } catch (error) {
          new Notice(error instanceof Error ? error.message : String(error));
          return;
        }
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
