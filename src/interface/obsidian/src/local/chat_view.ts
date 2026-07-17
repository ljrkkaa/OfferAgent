import { Component, ItemView, MarkdownRenderer, MarkdownView, Menu, Notice, WorkspaceLeaf, setIcon } from "obsidian";

import { BootstrapSnapshot } from "../runtime/bootstrap";
import { ChatStore, ChatStoreSnapshot } from "../runtime/chat_store";
import type { ArtifactRef, ImageContentBlock, PinnedContextReference } from "../runtime/generated_protocol";
import type {
    ApprovalTimelineItem,
    RunViewState,
    SubagentTimelineItem,
    TimelineItem,
    ToolCallTimelineItem,
    UserMessageTimelineItem,
} from "../runtime/event_reducer";
import type { EventEnvelope } from "../runtime/event_reducer";
import { JsonObject, RemoteRpcError } from "../runtime/json_rpc";
import {
    VaultReferenceTarget,
    sourceReferenceLabel,
    vaultReferenceTarget,
    webReferenceTarget,
} from "../runtime/source_references";
import { LocalOfferAgentSettings, effectivePermissionMode, runConfig } from "./settings";

export const LOCAL_CHAT_VIEW = "offeragent-local-chat";

export interface ChatModelChoice {
    readonly provider: string;
    readonly model: string;
    readonly displayName: string;
    readonly supportsStreaming: boolean;
    readonly supportsStructuredOutput: boolean;
    readonly visionStatus: "supported" | "unsupported" | "unverified";
}

export interface LocalChatHost {
    settings: LocalOfferAgentSettings;
    runtimeSnapshot(): BootstrapSnapshot;
    subscribeRuntime(listener: (snapshot: BootstrapSnapshot) => void): () => void;
    ensureChatStore(): Promise<ChatStore>;
    readArtifactText(artifactId: string): Promise<string>;
    uploadConversationAttachment(sessionId: string, file: File): Promise<ImageContentBlock>;
    readConversationAttachment(sessionId: string, artifact: ArtifactRef): Promise<Uint8Array>;
    discardConversationAttachment(sessionId: string, artifactId: string): Promise<boolean>;
    listSessions(): Promise<readonly { sessionId: string; title: string }[]>;
    listModels(): Promise<readonly ChatModelChoice[]>;
    selectModel(model: string): Promise<void>;
    openSettings(): void;
    openDiagnostics(): Promise<void>;
    openLocalWeb(): Promise<void>;
}

export class LocalChatView extends ItemView {
    private readonly host: LocalChatHost;
    private store: ChatStore | null = null;
    private snapshot: ChatStoreSnapshot | null = null;
    private unsubscribeStore: (() => void) | null = null;
    private unsubscribeRuntime: (() => void) | null = null;
    private boundRuntimeAttempt = "";
    private latestRuntimeAttempt = "";
    private bindingStore = false;
    private renderScheduled = false;
    private renderDeferredForComposition = false;
    private draftTimer: ReturnType<typeof setTimeout> | null = null;
    private composing = false;
    private sendPending = false;
    private clearComposerTabId: string | null = null;
    private models: readonly ChatModelChoice[] = [];
    private modelError: string | null = null;
    private modelsLoaded = false;
    private markdownComponents = new Map<HTMLElement, Component>();
    private markdownRenderTasks: Promise<void>[] = [];
    private renderGeneration = 0;
    private streamPatchScheduled = false;
    private pendingStreamRunId: string | null = null;
    private streamPatchGeneration = 0;
    private closed = true;
    private attachmentPending = false;
    private readonly composerAttachments = new Map<
        string,
        { block: ImageContentBlock; previewUrl: string | null }[]
    >();
    private readonly composerPins = new Map<string, PinnedContextReference[]>();
    private readonly objectUrls = new Set<string>();
    private readonly renderObjectUrls = new Set<string>();

    constructor(leaf: WorkspaceLeaf, host: LocalChatHost) {
        super(leaf);
        this.host = host;
    }

    getViewType(): string {
        return LOCAL_CHAT_VIEW;
    }

    getDisplayText(): string {
        return "OfferAgent";
    }

    getIcon(): string {
        return "bot";
    }

    async onOpen(): Promise<void> {
        this.closed = false;
        this.unsubscribeRuntime = this.host.subscribeRuntime((snapshot) => {
            this.scheduleRender();
            const attempt = `${snapshot.generation}:${snapshot.attempt}`;
            this.latestRuntimeAttempt = attempt;
            if (snapshot.state === "ready" && attempt !== this.boundRuntimeAttempt && !this.bindingStore) {
                void this.bindCurrentStore(attempt);
            }
        });
        this.scheduleRender();
        const runtime = this.host.runtimeSnapshot();
        this.latestRuntimeAttempt = `${runtime.generation}:${runtime.attempt}`;
        if (runtime.state === "ready") await this.bindCurrentStore(this.latestRuntimeAttempt);
    }

    async onClose(): Promise<void> {
        this.closed = true;
        if (this.draftTimer) clearTimeout(this.draftTimer);
        this.draftTimer = null;
        this.composing = false;
        this.renderDeferredForComposition = false;
        this.unsubscribeStore?.();
        this.unsubscribeRuntime?.();
        this.unsubscribeStore = null;
        this.unsubscribeRuntime = null;
        this.releaseMarkdownComponents();
        this.renderGeneration += 1;
        this.streamPatchGeneration += 1;
        this.pendingStreamRunId = null;
        this.revokeUrls(this.objectUrls);
        this.revokeUrls(this.renderObjectUrls);
    }

    private async bindCurrentStore(runtimeAttempt: string): Promise<void> {
        if (this.closed || this.bindingStore) return;
        this.bindingStore = true;
        try {
            const store = await this.host.ensureChatStore();
            if (this.closed || this.latestRuntimeAttempt !== runtimeAttempt) return;
            this.unsubscribeStore?.();
            this.store = store;
            this.boundRuntimeAttempt = runtimeAttempt;
            this.models = [];
            this.modelError = null;
            this.modelsLoaded = false;
            this.unsubscribeStore = store.subscribe((snapshot, event) => {
                this.snapshot = snapshot;
                if (event?.type === "assistant.delta" && this.scheduleStreamingAssistantPatch(event)) return;
                this.scheduleRender();
            });
            void this.refreshModels(runtimeAttempt);
        } catch (error) {
            if (!this.closed && this.latestRuntimeAttempt === runtimeAttempt) {
                new Notice(actionableMessage(error));
                this.scheduleRender();
            }
        } finally {
            this.bindingStore = false;
            const runtime = this.host.runtimeSnapshot();
            const latest = `${runtime.generation}:${runtime.attempt}`;
            this.latestRuntimeAttempt = latest;
            if (!this.closed && runtime.state === "ready" && latest !== runtimeAttempt &&
                latest !== this.boundRuntimeAttempt) void this.bindCurrentStore(latest);
        }
    }

    private scheduleRender(): void {
        if (this.closed) return;
        if (this.composing) {
            this.renderDeferredForComposition = true;
            return;
        }
        if (this.renderScheduled) return;
        this.renderScheduled = true;
        window.requestAnimationFrame(() => {
            this.renderScheduled = false;
            if (this.closed) return;
            if (this.composing) {
                this.renderDeferredForComposition = true;
                return;
            }
            this.renderDeferredForComposition = false;
            this.render();
        });
    }

    private render(): void {
        const renderGeneration = ++this.renderGeneration;
        this.streamPatchGeneration += 1;
        this.releaseMarkdownComponents();
        this.revokeUrls(this.renderObjectUrls);
        this.markdownRenderTasks = [];
        const root = this.contentEl;
        const previousTimeline = root.querySelector<HTMLElement>(".offeragent-timeline");
        const previousMarker = previousTimeline?.querySelector<HTMLElement>(".offeragent-new-content");
        const scroll = previousTimeline === null
            ? null
            : timelineScrollIntent(previousTimeline, previousMarker?.getBoundingClientRect().height ?? 0);
        const focused = root.querySelector<HTMLTextAreaElement>(".offeragent-composer-input");
        const clearSubmittedDraft = this.clearComposerTabId !== null &&
            this.clearComposerTabId === this.snapshot?.activeTabId;
        const selection = !clearSubmittedDraft && focused !== null && focused === document.activeElement
            ? { start: focused.selectionStart, end: focused.selectionEnd, value: focused.value }
            : null;
        if (clearSubmittedDraft) this.clearComposerTabId = null;
        root.empty();
        root.addClass("offeragent-local-root");
        this.renderHeader(root);
        const runtime = this.host.runtimeSnapshot();
        if (runtime.state !== "ready" || !this.snapshot || !this.store) {
            this.renderRuntimeState(root, runtime);
            return;
        }
        this.renderTabs(root, this.snapshot);
        const timeline = this.renderTimeline(root, this.snapshot, scroll);
        this.renderComposer(root, this.snapshot, selection);
        const markdownTasks = [...this.markdownRenderTasks];
        if (markdownTasks.length > 0) {
            void Promise.all(markdownTasks).then(() => {
                if (renderGeneration !== this.renderGeneration || !timeline.isConnected) return;
                this.restoreTimelineScroll(timeline, scroll);
            });
        }
    }

    private renderHeader(root: HTMLElement): void {
        const header = root.createDiv({ cls: "offeragent-local-header" });
        const title = header.createDiv({ cls: "offeragent-local-title" });
        const icon = title.createSpan({ cls: "offeragent-local-title-icon" });
        setIcon(icon, "bot");
        title.createSpan({ text: "OfferAgent" });
        const actions = header.createDiv({ cls: "offeragent-local-header-actions" });
        const web = actions.createEl("button", { attr: { "aria-label": "打开本地 Web UI" } });
        setIcon(web, "external-link");
        web.onclick = () => void this.host.openLocalWeb().catch((error) => new Notice(actionableMessage(error)));
        const diagnostics = actions.createEl("button", { attr: { "aria-label": "运行诊断" } });
        setIcon(diagnostics, "activity");
        diagnostics.onclick = () => void this.host.openDiagnostics().catch((error) => new Notice(actionableMessage(error)));
        const settings = actions.createEl("button", { attr: { "aria-label": "打开 OfferAgent 设置" } });
        setIcon(settings, "settings");
        settings.onclick = () => this.host.openSettings();
    }

    private renderRuntimeState(root: HTMLElement, snapshot: BootstrapSnapshot): void {
        const panel = root.createDiv({ cls: "offeragent-runtime-state" });
        panel.createEl("h3", { text: runtimeTitle(snapshot.state) });
        panel.createEl("p", { text: runtimeDescription(snapshot) });
        if (snapshot.error) {
            const code = snapshot.error.causeCode
                ? `${snapshot.error.code} · ${snapshot.error.causeCode}`
                : snapshot.error.code;
            panel.createEl("code", { text: code });
        }
    }

    private renderTabs(root: HTMLElement, snapshot: ChatStoreSnapshot): void {
        const bar = root.createDiv({ cls: "offeragent-tabs", attr: { role: "tablist" } });
        for (const tab of snapshot.tabs) {
            const wrapper = bar.createDiv({ cls: `offeragent-tab ${tab.tabId === snapshot.activeTabId ? "is-active" : ""}` });
            const button = wrapper.createEl("button", { text: tab.title, attr: { role: "tab" } });
            button.onclick = () => void this.store?.selectTab(tab.tabId);
            const close = wrapper.createEl("button", { cls: "offeragent-tab-close", attr: { "aria-label": "关闭标签" } });
            setIcon(close, "x");
            close.onclick = (event) => {
                event.stopPropagation();
                void this.store?.closeTab(tab.tabId);
            };
        }
        const add = bar.createEl("button", { cls: "offeragent-tab-add", attr: { "aria-label": "新建标签" } });
        setIcon(add, "plus");
        add.onclick = () => void this.store?.createTab();
        const history = bar.createEl("button", { cls: "offeragent-tab-history", attr: { "aria-label": "会话历史" } });
        setIcon(history, "history");
        history.onclick = (event) => void this.openSessionMenu(event);
    }

    private async openSessionMenu(event: MouseEvent): Promise<void> {
        try {
            const sessions = await this.host.listSessions();
            const menu = new Menu();
            if (sessions.length === 0) menu.addItem((item) => item.setTitle("暂无历史会话").setDisabled(true));
            for (const session of sessions) {
                menu.addItem((item) => item.setTitle(session.title).onClick(() => {
                    void this.store?.openSession(session.sessionId)
                        .catch((error) => new Notice(actionableMessage(error)));
                }));
            }
            menu.showAtMouseEvent(event);
        } catch (error) {
            new Notice(actionableMessage(error));
        }
    }

    private renderTimeline(
        root: HTMLElement,
        snapshot: ChatStoreSnapshot,
        scroll: TimelineScrollState | null,
    ): HTMLElement {
        const timeline = root.createDiv({ cls: "offeragent-timeline" });
        const tab = snapshot.tabs.find((candidate) => candidate.tabId === snapshot.activeTabId);
        if (!tab) return timeline;
        const pending = snapshot.pendingSubmissions.filter((submission) => submission.tabId === tab.tabId);
        if (!tab.sessionId && pending.length === 0) {
            const empty = timeline.createDiv({ cls: "offeragent-empty" });
            empty.createEl("h3", { text: "从本地知识库开始" });
            empty.createEl("p", { text: "可以引用活动笔记、选区和 Vault 内容；写入会显示 Diff 并等待相应审批。" });
            this.restoreTimelineScroll(timeline, scroll);
            return timeline;
        }
        const runs = tab.sessionId === null
            ? []
            : [...snapshot.projection.runs.values()].filter(
                (run) => run.sessionId === tab.sessionId && run.parentRunId === null,
            );
        if (runs.length === 0 && pending.length === 0) {
            timeline.createEl("p", { text: "此会话尚无消息。", cls: "offeragent-empty" });
        }
        for (const run of runs) this.renderRun(timeline, run);
        for (const submission of pending) this.renderPendingSubmission(timeline, submission);
        this.restoreTimelineScroll(timeline, scroll);
        return timeline;
    }

    private restoreTimelineScroll(timeline: HTMLElement, previous: TimelineScrollState | null): void {
        timeline.querySelector(".offeragent-new-content")?.remove();
        if (previous === null || previous.follow) {
            timeline.scrollTop = timeline.scrollHeight;
            return;
        }
        timeline.scrollTop = Math.min(previous.scrollTop, Math.max(0, timeline.scrollHeight - timeline.clientHeight));
        if (timeline.scrollHeight <= previous.scrollHeight) return;
        const resume = timeline.createEl("button", { text: "新内容", cls: "offeragent-new-content mod-cta" });
        resume.onclick = () => {
            timeline.scrollTop = timeline.scrollHeight;
            resume.remove();
        };
    }

    private renderPendingSubmission(
        container: HTMLElement,
        submission: ChatStoreSnapshot["pendingSubmissions"][number],
    ): void {
        const article = container.createEl("article", {
            cls: "offeragent-run offeragent-run-pending",
            attr: { "data-turn-id": submission.turnId },
        });
        const user = article.createDiv({ cls: "offeragent-message offeragent-user" });
        user.createDiv({ cls: "offeragent-message-label", text: "你" });
        user.createDiv({ cls: "offeragent-message-body", text: submission.text });
        article.createDiv({ cls: "offeragent-thinking", text: "正在提交给 OfferAgent…" });
    }

    private renderRun(container: HTMLElement, run: RunViewState): void {
        const article = container.createEl("article", { cls: "offeragent-run", attr: { "data-run-id": run.runId } });
        for (let index = 0; index < run.timeline.length;) {
            const item = run.timeline[index];
            if (item.kind === "tool_call" && ordinaryToolActivity(item)) {
                const tools: ToolCallTimelineItem[] = [];
                while (index < run.timeline.length) {
                    const candidate = run.timeline[index];
                    if (candidate.kind !== "tool_call" || !ordinaryToolActivity(candidate)) break;
                    tools.push(candidate);
                    index += 1;
                }
                this.renderToolGroup(article, tools);
                continue;
            }
            this.renderTimelineItem(article, item, run.parentRunId !== null, run.runId);
            index += 1;
        }
        if (!isTerminal(run.status)) article.createDiv({ cls: "offeragent-thinking", text: phaseLabel(run.phase) });
        if (run.references.length > 0) {
            const references = article.createEl("details", { cls: "offeragent-references" });
            references.createEl("summary", { text: `本轮实际使用的证据 ${run.references.length}` });
            const list = references.createEl("ul");
            for (const reference of run.references) {
                const item = list.createEl("li");
                const label = sourceReferenceLabel(reference);
                const target = vaultReferenceTarget(reference);
                const webTarget = webReferenceTarget(reference);
                if (target && safeVaultPath(target.path)) {
                    const link = item.createEl("button", { text: label, cls: "offeragent-reference-link" });
                    const linkText = target.heading ? `${target.path}#${target.heading}` : target.path;
                    link.onclick = () => void this.openVaultReference(linkText, target);
                } else if (webTarget) {
                    const link = item.createEl("button", { text: label, cls: "offeragent-reference-link" });
                    link.onclick = () => window.open(webTarget, "_blank", "noopener,noreferrer");
                } else item.setText(label);
            }
        }
        const footer = article.createDiv({ cls: "offeragent-run-footer" });
        if (!isTerminal(run.status)) footer.createSpan({ text: phaseLabel(run.phase) });
        footer.createSpan({ text: statusLabel(run.status) });
        if (run.usage) footer.createSpan({ text: usageLabel(run.usage) });
        if (isTerminal(run.status)) {
            const actions = footer.createDiv({ cls: "offeragent-run-actions" });
            const snapshot = this.snapshot;
            const blocked = snapshot === null || snapshot.busy || this.sendPending ||
                activeRunForTab(snapshot, run.sessionId, null) !== undefined;
            const retry = actions.createEl("button", {
                text: explicitContinuationStatus(run.status) ? "继续" : "重试",
            });
            retry.disabled = blocked;
            retry.onclick = () => void this.store?.retry(
                run.sessionId, run.turnId, run.runId, runConfig(this.host.settings),
            ).catch((error) => new Notice(actionableMessage(error)));
            const fork = actions.createEl("button", { text: "Fork" });
            fork.disabled = blocked;
            fork.onclick = () => void this.store?.fork(run.sessionId, run.turnId, run.runId)
                .catch((error) => new Notice(actionableMessage(error)));
            const compact = actions.createEl("button", { text: "压缩至此" });
            compact.disabled = blocked;
            compact.onclick = () => void this.store?.compact(run.sessionId, run.turnId)
                .catch((error) => new Notice(actionableMessage(error)));
            const prompt = originalTurnPrompt(run.timeline);
            if (prompt !== null) {
                const revise = actions.createEl("button", { text: "放入输入框" });
                revise.disabled = blocked;
                revise.onclick = () => void this.replaceDraftWithPrompt(prompt);
            }
        }
    }

    private async openVaultReference(linkText: string, target: VaultReferenceTarget): Promise<void> {
        await this.app.workspace.openLinkText(linkText, "", false);
        if (target.lineStart === null) return;
        const view = this.app.workspace.getActiveViewOfType(MarkdownView);
        if (view?.file?.path !== target.path) return;
        const lineStart = Math.max(0, target.lineStart - 1);
        const lineEnd = Math.max(lineStart, (target.lineEnd ?? target.lineStart) - 1);
        const from = { line: lineStart, ch: 0 };
        const to = { line: lineEnd, ch: view.editor.getLine(lineEnd).length };
        view.editor.setSelection(from, to);
        view.editor.scrollIntoView({ from, to }, true);
    }

    private renderTimelineItem(container: HTMLElement, item: TimelineItem, childRun: boolean, runId: string): void {
        switch (item.kind) {
            case "user_message": {
                const user = container.createDiv({ cls: "offeragent-message offeragent-user" });
                user.createDiv({ cls: "offeragent-message-label", text: item.source === "steer" ? "你（追加）" : "你" });
                for (const block of item.blocks) user.createDiv({ cls: "offeragent-message-body", text: block });
                if (item.pinnedContext.length > 0) {
                    const pins = user.createDiv({ cls: "offeragent-pinned-context" });
                    pins.createSpan({ text: "固定上下文（偏好，不是已用证据）" });
                    for (const reference of item.pinnedContext) {
                        pins.createSpan({ cls: "offeragent-context-chip", text: pinnedContextLabel(reference) });
                    }
                }
                for (const image of item.images) this.renderHistoryImage(user, item, image);
                return;
            }
            case "reasoning": {
                const reasoning = container.createEl("details", { cls: "offeragent-reasoning" });
                reasoning.createEl("summary", { text: item.partial ? "推理摘要（生成中）" : "推理摘要" });
                reasoning.createEl("p", { text: item.summary });
                return;
            }
            case "assistant_message": {
                const assistant = container.createDiv({
                    cls: "offeragent-message offeragent-assistant",
                    attr: { "data-assistant-run-id": runId },
                });
                assistant.createDiv({ cls: "offeragent-message-label", text: childRun ? "子任务" : "OfferAgent" });
                for (const block of item.blocks) this.renderMarkdown(assistant, block);
                if (!item.completed) assistant.createDiv({ cls: "offeragent-thinking", text: "正在生成回答…" });
                return;
            }
            case "tool_call":
                this.renderToolCard(container, item);
                return;
            case "approval":
                this.renderApproval(container, item);
                return;
            case "subagent":
                this.renderSubagent(container, item);
                return;
        }
    }

    private renderMarkdown(container: HTMLElement, markdown: string, trackFrame = true): Promise<void> {
        const body = container.createDiv({ cls: "offeragent-message-body markdown-rendered" });
        const component = new Component();
        component.load();
        this.markdownComponents.set(body, component);
        const task = MarkdownRenderer.render(this.app, markdown, body, "", component)
            .catch((error) => {
                if (body.isConnected) {
                    body.setText(markdown);
                    new Notice(actionableMessage(error));
                }
            })
            .finally(() => {
                if (this.markdownComponents.get(body) !== component) component.unload();
            });
        if (trackFrame) this.markdownRenderTasks.push(task);
        return task;
    }

    private releaseMarkdownComponents(): void {
        for (const component of this.markdownComponents.values()) component.unload();
        this.markdownComponents.clear();
    }

    private releaseMarkdownBody(body: HTMLElement): void {
        this.markdownComponents.get(body)?.unload();
        this.markdownComponents.delete(body);
        body.remove();
    }

    private scheduleStreamingAssistantPatch(event: EventEnvelope): boolean {
        if (this.closed || event.runId === null || !this.renderedRun(event.runId)) return false;
        this.pendingStreamRunId = event.runId;
        if (this.streamPatchScheduled) return true;
        this.streamPatchScheduled = true;
        window.requestAnimationFrame(() => {
            this.streamPatchScheduled = false;
            if (this.closed) return;
            const runId = this.pendingStreamRunId;
            this.pendingStreamRunId = null;
            if (runId !== null) void this.patchStreamingAssistant(runId);
        });
        return true;
    }

    private renderedRun(runId: string): HTMLElement | null {
        return Array.from(this.contentEl.querySelectorAll<HTMLElement>(".offeragent-run[data-run-id]"))
            .find((candidate) => candidate.dataset.runId === runId) ?? null;
    }

    private async patchStreamingAssistant(runId: string): Promise<void> {
        if (this.closed) return;
        const patchGeneration = ++this.streamPatchGeneration;
        const snapshot = this.snapshot;
        const run = snapshot?.projection.runs.get(runId);
        const article = this.renderedRun(runId);
        const assistant = article?.querySelector<HTMLElement>(".offeragent-assistant");
        const item = run?.timeline.find((candidate) => candidate.kind === "assistant_message");
        if (!snapshot || !run || !article || !assistant || !item || item.kind !== "assistant_message") {
            this.scheduleRender();
            return;
        }
        const timeline = article.closest<HTMLElement>(".offeragent-timeline");
        const marker = timeline?.querySelector<HTMLElement>(".offeragent-new-content");
        const scroll = timeline === null || timeline === undefined
            ? null
            : timelineScrollIntent(timeline, marker?.getBoundingClientRect().height ?? 0);
        for (const body of Array.from(assistant.querySelectorAll<HTMLElement>(".offeragent-message-body"))) {
            this.releaseMarkdownBody(body);
        }
        assistant.querySelector(".offeragent-thinking")?.remove();
        const tasks = item.blocks.map((block) => this.renderMarkdown(assistant, block, false));
        if (!item.completed) assistant.createDiv({ cls: "offeragent-thinking", text: "正在生成回答…" });
        await Promise.all(tasks);
        if (patchGeneration === this.streamPatchGeneration && timeline?.isConnected) {
            this.restoreTimelineScroll(timeline, scroll);
        }
    }

    private renderToolGroup(container: HTMLElement, tools: readonly ToolCallTimelineItem[]): void {
        if (tools.length === 0) return;
        const group = container.createEl("details", { cls: "offeragent-tool-group" });
        group.createEl("summary", {
            text: `${tools.length} 个工具活动`,
            attr: { "aria-label": `${tools.length} 个工具活动，按回车展开详情` },
        });
        const body = group.createDiv({ cls: "offeragent-tool-group-body" });
        for (const tool of tools) this.renderToolCard(body, tool);
    }

    private renderToolCard(container: HTMLElement, tool: ToolCallTimelineItem): void {
        const card = container.createDiv({ cls: `offeragent-tool-card status-${cssToken(tool.status)}` });
        const header = card.createDiv({ cls: "offeragent-tool-header" });
        header.createSpan({ text: tool.name });
        header.createSpan({ text: statusLabel(tool.status), cls: "offeragent-tool-status" });
        const details = card.createEl("details");
        details.createEl("summary", { text: "参数与结果" });
        details.createEl("pre", { text: safeJson(tool.arguments) });
        if (tool.result) details.createEl("pre", { text: safeJson(tool.result) });
        if (tool.error) details.createEl("pre", { text: safeJson(tool.error) });
        if (tool.sideEffects.length > 0) details.createEl("pre", { text: safeJson({ sideEffects: tool.sideEffects }) });
        if (tool.artifactIds.length > 0) card.createDiv({ text: `Artifact: ${tool.artifactIds.join(", ")}` });
    }

    private renderApproval(container: HTMLElement, approval: ApprovalTimelineItem): void {
        const card = container.createDiv({ cls: `offeragent-approval status-${cssToken(approval.status)}` });
        card.createEl("strong", { text: approval.status === "pending" ? "需要审批" : `审批：${approval.status}` });
        card.createEl("p", { text: approval.explanation });
        if (approval.diffArtifactIds.length > 0) {
            const diff = card.createEl("details", { cls: "offeragent-approval-diff" });
            const summary = diff.createEl("summary", { text: `查看 Diff（${approval.diffArtifactIds.length}）` });
            summary.onclick = () => {
                if (diff.dataset.loaded === "true") return;
                diff.dataset.loaded = "true";
                summary.setText("正在读取本地 Diff…");
                void Promise.all(approval.diffArtifactIds.map((artifactId) => this.host.readArtifactText(artifactId)))
                    .then((values) => {
                        summary.setText(`Diff（${approval.diffArtifactIds.length}）`);
                        diff.createEl("pre", { text: values.join("\n") });
                    })
                    .catch((error) => {
                        diff.dataset.loaded = "false";
                        summary.setText("Diff 读取失败，点击重试");
                        new Notice(actionableMessage(error));
                    });
            };
        }
        if (approval.status !== "pending" || !approval.expectedArgsHash) return;
        const actions = card.createDiv({ cls: "offeragent-approval-actions" });
        this.approvalButton(actions, "拒绝", approval, "deny", "once");
        this.approvalButton(actions, "允许一次", approval, "allow_once", "once");
        this.approvalButton(actions, "本轮允许", approval, "allow_run", "run");
        this.approvalButton(actions, "本会话允许", approval, "allow_session", "session");
    }

    private renderSubagent(container: HTMLElement, subagent: SubagentTimelineItem): void {
        const card = container.createEl("details", { cls: `offeragent-subagent status-${cssToken(subagent.status)}` });
        card.createEl("summary", { text: `子 Agent · ${subagent.agentName} · ${statusLabel(subagent.status)}` });
        card.createEl("p", { text: subagent.task });
        if (subagent.message) card.createEl("p", { text: subagent.message });
        if (subagent.summary) card.createEl("pre", { text: subagent.summary });
    }

    private approvalButton(
        container: HTMLElement,
        label: string,
        approval: { approvalId: string; expectedArgsHash: string | null },
        decision: "deny" | "allow_once" | "allow_run" | "allow_session",
        scope: "once" | "run" | "session",
    ): void {
        const button = container.createEl("button", { text: label });
        button.onclick = () => {
            if (!approval.expectedArgsHash) return;
            button.disabled = true;
            void this.store?.resolveApproval(approval.approvalId, decision, scope, approval.expectedArgsHash)
                .catch((error) => { button.disabled = false; new Notice(actionableMessage(error)); });
        };
    }

    private renderComposer(
        root: HTMLElement,
        snapshot: ChatStoreSnapshot,
        selection: { start: number; end: number; value: string } | null,
    ): void {
        const tab = snapshot.tabs.find((candidate) => candidate.tabId === snapshot.activeTabId);
        if (!tab) return;
        const activeRun = activeRunForTab(snapshot, tab.sessionId, tab.selectedRunId);
        const hasActiveRun = activeRun !== undefined;
        const effectiveMode = effectivePermissionMode(this.host.settings);
        const composer = root.createDiv({ cls: "offeragent-composer" });
        if (!this.host.settings.workspaceTrusted &&
            !["read-only", "plan"].includes(this.host.settings.permissionMode)) {
            composer.createEl("p", {
                cls: "offeragent-permission-warning",
                text: "当前 Workspace 尚未显式信任，有效权限为只读。请在 OfferAgent 设置中确认信任后再启用写入。",
            });
        } else {
            composer.createEl("p", {
                cls: "offeragent-permission-status",
                text: `当前有效权限：${permissionModeLabel(effectiveMode)}`,
            });
        }
        const draftAttachments = this.composerAttachments.get(tab.tabId) ?? [];
        const draftPins = this.composerPins.get(tab.tabId) ?? [];
        if (draftAttachments.length > 0) {
            const previews = composer.createDiv({ cls: "offeragent-attachment-previews" });
            for (const attachment of draftAttachments) {
                const card = previews.createDiv({ cls: "offeragent-attachment-preview" });
                if (attachment.previewUrl) card.createEl("img", {
                    attr: { src: attachment.previewUrl, alt: attachment.block.altText ?? "待发送图片" },
                });
                card.createSpan({ text: attachment.block.artifact.title ?? "图片" });
                const remove = card.createEl("button", { attr: { "aria-label": "移除图片" } });
                setIcon(remove, "x");
                remove.disabled = this.attachmentPending || this.sendPending;
                remove.onclick = () => void this.removeComposerAttachment(tab.tabId, attachment);
            }
        }
        if (draftPins.length > 0) {
            const pins = composer.createDiv({ cls: "offeragent-composer-pins" });
            pins.createSpan({ text: "固定上下文" });
            for (const reference of draftPins) {
                const chip = pins.createDiv({ cls: "offeragent-context-chip" });
                chip.createSpan({ text: pinnedContextLabel(reference) });
                const remove = chip.createEl("button", { attr: { "aria-label": "移除固定上下文" } });
                setIcon(remove, "x");
                remove.onclick = () => {
                    this.composerPins.set(tab.tabId, draftPins.filter((item) => item !== reference));
                    this.scheduleRender();
                };
            }
        }
        const input = composer.createEl("textarea", {
            cls: "offeragent-composer-input",
            attr: { placeholder: "询问你的笔记，或交给 OfferAgent 一个任务…", rows: "3" },
        });
        input.value = selection?.value ?? tab.draft;
        if (selection) {
            window.requestAnimationFrame(() => {
                input.focus();
                input.setSelectionRange(selection.start, selection.end);
            });
        }
        input.addEventListener("compositionstart", () => {
            this.composing = true;
        });
        input.addEventListener("compositionend", () => {
            this.composing = false;
            this.scheduleDraftSave(tab.tabId, input.value, 0);
            if (this.renderDeferredForComposition) this.scheduleRender();
        });
        input.oninput = () => {
            if (!this.composing) this.scheduleDraftSave(tab.tabId, input.value);
        };
        input.ondragover = (event) => {
            if (imageFiles(event.dataTransfer?.files).length > 0) event.preventDefault();
        };
        input.ondrop = (event) => {
            const files = imageFiles(event.dataTransfer?.files);
            if (files.length === 0) return;
            event.preventDefault();
            void this.addAttachmentFiles(tab.tabId, files);
        };
        input.onpaste = (event) => {
            const files = imageFiles(event.clipboardData?.files);
            if (files.length === 0) return;
            event.preventDefault();
            void this.addAttachmentFiles(tab.tabId, files);
        };
        input.onkeydown = (event) => {
            const blocked = snapshot.busy || this.sendPending || this.attachmentPending || hasActiveRun;
            if (shouldSendComposerInput(event, this.composing, blocked)) {
                event.preventDefault();
                void this.send(input.value);
            }
        };
        const controls = composer.createDiv({ cls: "offeragent-composer-controls" });
        const modelControl = controls.createDiv({ cls: "offeragent-model-control" });
        const model = modelControl.createEl("select", { attr: { "aria-label": "选择模型" } });
        const choices = this.models.some((choice) => choice.model === this.host.settings.model)
            ? this.models
            : [{
                provider: this.host.settings.provider,
                model: this.host.settings.model,
                displayName: this.host.settings.model,
                supportsStreaming: false,
                supportsStructuredOutput: false,
                visionStatus: "unverified" as const,
            }, ...this.models];
        for (const choice of choices) {
            model.createEl("option", { value: choice.model, text: choice.displayName });
        }
        model.value = this.host.settings.model;
        model.disabled = snapshot.busy || this.sendPending || hasActiveRun || !this.modelsLoaded ||
            this.modelError !== null || choices.length === 0;
        model.onchange = () => void this.chooseModel(model.value);
        const selectedModel = choices.find((choice) => choice.model === model.value);
        model.title = this.modelError ?? (!this.modelsLoaded ? "正在从 Worker 查询模型能力" : selectedModel
            ? `${selectedModel.provider} · ${selectedModel.supportsStreaming ? "支持流式" : "不支持流式"} · ${selectedModel.supportsStructuredOutput ? "支持结构化输出" : "不支持结构化输出"} · ${visionStatusLabel(selectedModel.visionStatus)}`
            : "模型能力尚不可用");
        const capability = modelControl.createSpan({ cls: "offeragent-model-capability", attr: { "aria-live": "polite" } });
        capability.setText(this.modelError ? "模型不可用" : !this.modelsLoaded ? "能力查询中" : selectedModel
            ? [selectedModel.supportsStreaming ? "流式" : "非流式", selectedModel.supportsStructuredOutput ? "结构化" : "文本", visionStatusLabel(selectedModel.visionStatus)]
                .join(" · ")
            : "能力未知");
        capability.title = model.title;
        if (activeRun) {
            const cancel = controls.createEl("button", { text: "停止" });
            cancel.onclick = () => void this.store?.cancel(activeRun.runId, activeRun.sessionId, activeRun.turnId)
                .catch((error) => new Notice(actionableMessage(error)));
            const steer = controls.createEl("button", { text: "转向" });
            steer.onclick = () => {
                const text = input.value.trim();
                if (!text) return;
                void this.store?.steer(activeRun.runId, text).catch((error) => new Notice(actionableMessage(error)));
            };
        }
        const attach = controls.createEl("button", { attr: { "aria-label": "添加图片" } });
        setIcon(attach, "paperclip");
        attach.disabled = snapshot.busy || this.sendPending || this.attachmentPending || hasActiveRun;
        const fileInput = composer.createEl("input", {
            cls: "offeragent-hidden-file-input",
            attr: { type: "file", accept: "image/png,image/jpeg,image/webp,image/gif", multiple: "true" },
        });
        attach.onclick = () => fileInput.click();
        fileInput.onchange = () => {
            const files = imageFiles(fileInput.files);
            fileInput.value = "";
            if (files.length > 0) void this.addAttachmentFiles(tab.tabId, files);
        };
        const pin = controls.createEl("button", { text: "固定当前上下文" });
        pin.disabled = snapshot.busy || this.sendPending || hasActiveRun || draftPins.length >= 8;
        pin.onclick = () => this.pinCurrentContext(tab.tabId);
        const send = controls.createEl("button", { text: "发送", cls: "mod-cta" });
        send.disabled = snapshot.busy || this.sendPending || this.attachmentPending || hasActiveRun;
        send.onclick = () => void this.send(input.value);
    }

    private async send(text: string): Promise<void> {
        const message = text.trim();
        const store = this.store;
        const snapshot = this.snapshot;
        const tab = snapshot?.tabs.find((candidate) => candidate.tabId === snapshot.activeTabId);
        if (!message || !store || !snapshot || !tab || this.sendPending || this.attachmentPending || snapshot.busy ||
            activeRunForTab(snapshot, tab.sessionId, tab.selectedRunId) !== undefined) return;
        // Set this before the first await so Enter and click in the same frame
        // cannot start two Turns.
        this.sendPending = true;
        if (this.draftTimer) clearTimeout(this.draftTimer);
        this.draftTimer = null;
        this.scheduleRender();
        try {
            const attachments = this.composerAttachments.get(tab.tabId) ?? [];
            const pinnedContext = this.composerPins.get(tab.tabId) ?? [];
            await store.send(message, {
                runConfig: runConfig(this.host.settings),
                ...(attachments.length ? { attachments: attachments.map((item) => item.block) } : {}),
                ...(pinnedContext.length ? { pinnedContext } : {}),
            });
            for (const attachment of attachments) this.releaseComposerPreview(attachment.previewUrl);
            this.composerAttachments.delete(tab.tabId);
            this.composerPins.delete(tab.tabId);
            this.clearComposerTabId = tab.tabId;
        } catch (error) {
            new Notice(actionableMessage(error));
        } finally {
            this.sendPending = false;
            this.scheduleRender();
        }
    }

    private async addAttachmentFiles(tabId: string, files: readonly File[]): Promise<void> {
        const store = this.store;
        if (!store || this.attachmentPending || files.length === 0) return;
        const existing = this.composerAttachments.get(tabId) ?? [];
        if (existing.length + files.length > 20) {
            new Notice("一次最多添加 20 张图片");
            return;
        }
        const total = existing.reduce((size, item) => size + item.block.artifact.sizeBytes, 0) +
            files.reduce((size, file) => size + file.size, 0);
        if (files.some((file) => file.size < 1 || file.size > 10 * 1024 * 1024) || total > 50 * 1024 * 1024) {
            new Notice("每张图片不得超过 10 MiB，一次提交合计不得超过 50 MiB");
            return;
        }
        this.attachmentPending = true;
        this.scheduleRender();
        try {
            const sessionId = await store.ensureSessionForTab(tabId);
            const selected = [...existing];
            for (const file of files) {
                const block = await this.host.uploadConversationAttachment(sessionId, file);
                const previewUrl = URL.createObjectURL(file);
                this.objectUrls.add(previewUrl);
                selected.push({ block, previewUrl });
                this.composerAttachments.set(tabId, selected);
                this.scheduleRender();
            }
        } catch (error) {
            new Notice(actionableMessage(error));
        } finally {
            this.attachmentPending = false;
            this.scheduleRender();
        }
    }

    private async removeComposerAttachment(
        tabId: string,
        attachment: { block: ImageContentBlock; previewUrl: string | null },
    ): Promise<void> {
        const tab = this.snapshot?.tabs.find((candidate) => candidate.tabId === tabId);
        try {
            if (tab?.sessionId) {
                await this.host.discardConversationAttachment(tab.sessionId, attachment.block.artifact.artifactId);
            }
            const current = this.composerAttachments.get(tabId) ?? [];
            this.composerAttachments.set(tabId, current.filter((item) => item !== attachment));
            this.releaseComposerPreview(attachment.previewUrl);
            this.scheduleRender();
        } catch (error) {
            new Notice(actionableMessage(error));
        }
    }

    private pinCurrentContext(tabId: string): void {
        const view = this.app.workspace.getActiveViewOfType(MarkdownView);
        const path = view?.file?.path;
        if (!view || !path || !isPinnableVaultPath(path)) {
            new Notice("请先打开一篇可发现的 Markdown 或文本笔记（不支持隐藏路径和 AGENT.md）");
            return;
        }
        const from = view.editor.getCursor("from");
        const to = view.editor.getCursor("to");
        const reference: PinnedContextReference = view.editor.getSelection().trim()
            ? { kind: "selection", path, lineStart: from.line + 1, lineEnd: to.line + 1 }
            : { kind: "document", path };
        const current = this.composerPins.get(tabId) ?? [];
        const identity = JSON.stringify(reference);
        if (!current.some((item) => JSON.stringify(item) === identity)) {
            this.composerPins.set(tabId, [...current, reference]);
            this.scheduleRender();
        }
    }

    private renderHistoryImage(
        container: HTMLElement,
        message: UserMessageTimelineItem,
        image: UserMessageTimelineItem["images"][number],
    ): void {
        const run = [...(this.snapshot?.projection.runs.values() ?? [])].find(
            (candidate) => candidate.timeline.includes(message),
        );
        if (!run) return;
        const card = container.createDiv({ cls: "offeragent-history-image" });
        const preview = card.createEl("img", { attr: { alt: image.altText ?? image.artifact.title ?? "历史图片" } });
        card.createSpan({ text: image.artifact.title ?? "图片附件" });
        const reuse = card.createEl("button", { text: "再次使用" });
        reuse.onclick = () => void this.reuseHistoryAttachment(run.sessionId, image);
        void this.host.readConversationAttachment(run.sessionId, image.artifact)
            .then((content) => {
                if (!preview.isConnected) return;
                const url = URL.createObjectURL(new Blob([content], { type: image.artifact.mediaType }));
                this.renderObjectUrls.add(url);
                preview.src = url;
            })
            .catch((error) => {
                if (card.isConnected) card.createSpan({ cls: "offeragent-attachment-error", text: actionableMessage(error) });
            });
    }

    private async reuseHistoryAttachment(
        sessionId: string,
        image: UserMessageTimelineItem["images"][number],
    ): Promise<void> {
        const snapshot = this.snapshot;
        const tab = snapshot?.tabs.find((candidate) => candidate.tabId === snapshot.activeTabId);
        if (!tab || tab.sessionId !== sessionId) return;
        const current = this.composerAttachments.get(tab.tabId) ?? [];
        if (current.some((item) => item.block.artifact.artifactId === image.artifact.artifactId)) return;
        if (current.length >= 20 || current.reduce((size, item) => size + item.block.artifact.sizeBytes, 0) +
            image.artifact.sizeBytes > 50 * 1024 * 1024) {
            new Notice("再次使用会超过本次提交的图片限制");
            return;
        }
        try {
            const content = await this.host.readConversationAttachment(sessionId, image.artifact);
            const url = URL.createObjectURL(new Blob([content], { type: image.artifact.mediaType }));
            this.objectUrls.add(url);
            this.composerAttachments.set(tab.tabId, [
                ...current,
                { block: { type: "image", artifact: image.artifact, ...(image.altText ? { altText: image.altText } : {}) }, previewUrl: url },
            ]);
            this.scheduleRender();
        } catch (error) {
            new Notice(actionableMessage(error));
        }
    }

    private releaseComposerPreview(url: string | null): void {
        if (!url) return;
        URL.revokeObjectURL(url);
        this.objectUrls.delete(url);
    }

    private revokeUrls(values: Set<string>): void {
        for (const url of values) URL.revokeObjectURL(url);
        values.clear();
    }

    private async refreshModels(runtimeAttempt: string): Promise<void> {
        try {
            const models = await this.host.listModels();
            if (this.closed || this.boundRuntimeAttempt !== runtimeAttempt ||
                this.latestRuntimeAttempt !== runtimeAttempt) return;
            this.models = [...models];
            this.modelsLoaded = true;
            this.modelError = models.length === 0
                ? "Worker 当前没有可用模型；请在 OfferAgent 设置中检查模型、端点或登录凭据"
                : null;
        } catch (error) {
            if (this.closed || this.boundRuntimeAttempt !== runtimeAttempt ||
                this.latestRuntimeAttempt !== runtimeAttempt) return;
            this.models = [];
            this.modelsLoaded = true;
            this.modelError = actionableMessage(error);
        }
        this.scheduleRender();
    }

    private async chooseModel(model: string): Promise<void> {
        if (!model || model === this.host.settings.model) return;
        try {
            await this.host.selectModel(model);
            await this.refreshModels(this.boundRuntimeAttempt);
        } catch (error) {
            this.modelError = actionableMessage(error);
            new Notice(this.modelError);
            this.scheduleRender();
        }
    }

    private async replaceDraftWithPrompt(prompt: string): Promise<void> {
        const snapshot = this.snapshot;
        const store = this.store;
        const tab = snapshot?.tabs.find((candidate) => candidate.tabId === snapshot.activeTabId);
        if (!snapshot || !store || !tab) return;
        const input = this.contentEl.querySelector<HTMLTextAreaElement>(".offeragent-composer-input");
        const current = input?.value ?? tab.draft;
        if (current.trim() && current !== prompt &&
            !window.confirm("当前草稿会被该运行的原提示词替换。继续吗？")) return;
        if (input) {
            input.value = prompt;
            input.focus();
            input.setSelectionRange(prompt.length, prompt.length);
        }
        await store.updateDraft(tab.tabId, prompt);
        this.scheduleRender();
    }

    private scheduleDraftSave(tabId: string, value: string, delayMs = 250): void {
        if (this.draftTimer) clearTimeout(this.draftTimer);
        this.draftTimer = setTimeout(() => {
            this.draftTimer = null;
            const store = this.store;
            if (!store) return;
            void store.updateDraft(tabId, value).catch((error) => new Notice(actionableMessage(error)));
        }, delayMs);
    }
}

export interface ComposerKeyboardEvent {
    readonly key: string;
    readonly shiftKey: boolean;
    readonly isComposing: boolean;
    readonly keyCode: number;
}

export function shouldSendComposerInput(
    event: ComposerKeyboardEvent,
    composing: boolean,
    blocked: boolean,
): boolean {
    return event.key === "Enter" && !event.shiftKey && !blocked && !composing &&
        !event.isComposing && event.keyCode !== 229;
}

export interface TimelineScrollState {
    readonly follow: boolean;
    readonly scrollTop: number;
    readonly scrollHeight: number;
}

export function timelineScrollIntent(
    timeline: Pick<HTMLElement, "scrollTop" | "scrollHeight" | "clientHeight">,
    affordanceHeight = 0,
): TimelineScrollState {
    const contentHeight = Math.max(0, timeline.scrollHeight - Math.max(0, affordanceHeight));
    const remaining = Math.max(0, contentHeight - timeline.clientHeight - timeline.scrollTop);
    return {
        follow: remaining <= 48,
        scrollTop: timeline.scrollTop,
        scrollHeight: contentHeight,
    };
}

export function explicitContinuationStatus(status: string): boolean {
    return status === "interrupted" || status === "orphaned";
}

export function originalTurnPrompt(
    timeline: readonly { readonly kind: string; readonly source?: string; readonly blocks?: readonly string[] }[],
): string | null {
    const message = timeline.find((item) => item.kind === "user_message" && item.source === "turn");
    const text = message?.blocks?.filter((block) => block.trim()).join("\n\n").trim() ?? "";
    return text || null;
}

function imageFiles(files: FileList | null | undefined): File[] {
    return files ? Array.from(files).filter((file) => file.type.startsWith("image/")) : [];
}

function pinnedContextLabel(pin: PinnedContextReference): string {
    return pin.kind === "selection"
        ? `${pin.path}:${pin.lineStart}-${pin.lineEnd}`
        : pin.path;
}

function isPinnableVaultPath(path: string): boolean {
    const lowered = path.toLowerCase();
    return safeVaultPath(path) && lowered !== "agent.md" && !path.split("/").some((segment) => segment.startsWith(".")) &&
        (lowered.endsWith(".md") || lowered.endsWith(".txt"));
}

function visionStatusLabel(value: ChatModelChoice["visionStatus"]): string {
    return value === "supported" ? "视觉已验证" : value === "unsupported" ? "不支持视觉" : "视觉待探测";
}

export function ordinaryToolActivity(tool: Pick<ToolCallTimelineItem, "name" | "status">): boolean {
    return tool.name !== "vault.changes.apply" &&
        !["failed", "conflict", "unknown_outcome", "timed_out", "denied", "cancelled"].includes(tool.status);
}

type ProjectedRun = ChatStoreSnapshot["projection"]["runs"] extends Map<string, infer Run> ? Run : never;

function activeRunForTab(
    snapshot: ChatStoreSnapshot,
    sessionId: string | null,
    selectedRunId: string | null,
): ProjectedRun | undefined {
    const selected = selectedRunId === null ? undefined : snapshot.projection.runs.get(selectedRunId);
    if (selected && !isTerminal(selected.status)) return selected;
    if (sessionId === null) return undefined;
    return [...snapshot.projection.runs.values()].find(
        (run) => run.sessionId === sessionId && run.parentRunId === null && !isTerminal(run.status),
    );
}

function permissionModeLabel(mode: LocalOfferAgentSettings["permissionMode"]): string {
    if (mode === "read-only") return "只读";
    if (mode === "normal") return "标准（写操作逐次审批）";
    if (mode === "trusted-workspace") return "受信任工作区";
    if (mode === "bypass") return "免审批执行";
    return "仅规划";
}

function runtimeTitle(state: BootstrapSnapshot["state"]): string {
    const labels: Record<BootstrapSnapshot["state"], string> = {
        uninitialized: "正在准备本地 Runtime",
        runtime_missing: "正在定位离线 Runtime",
        verifying: "正在验证 Runtime 完整性",
        starting_worker: "正在启动 Vault Worker",
        handshaking: "正在验证本地连接",
        ready: "Runtime 已就绪",
        degraded: "Runtime 连接不稳定",
        restarting: "正在恢复 Runtime",
        failed: "Runtime 启动失败",
        stopped: "Runtime 已停止",
    };
    return labels[state];
}

function runtimeDescription(snapshot: BootstrapSnapshot): string {
    if (snapshot.error) return snapshot.error.message;
    if (snapshot.state === "ready") return `Worker PID ${snapshot.workerPid ?? "-"}`;
    return "Agent Core、Session、工具与知识库均由当前 Windows 用户下的本地 Worker 承载。";
}

function phaseLabel(phase: string | null): string {
    const labels: Record<string, string> = {
        loading_context: "正在加载上下文…",
        selecting_memory: "正在加载 Vault 记忆…",
        planning: "正在规划…",
        validating_calls: "正在验证工具…",
        checking_policy: "正在检查权限…",
        awaiting_approval: "等待审批…",
        executing_tools: "正在执行工具…",
        waiting_children: "等待子任务…",
        responding: "正在组织回答…",
        persisting: "正在保存结果…",
        cancelling: "正在取消…",
    };
    return phase ? labels[phase] ?? phase : "正在开始…";
}

function statusLabel(status: string): string {
    const labels: Record<string, string> = {
        running: "运行中", completed: "已完成", cancelled: "已取消", failed: "失败", interrupted: "已中断",
        orphaned: "等待恢复", queued: "排队中", succeeded: "成功", conflict: "冲突", timed_out: "超时",
        unknown_outcome: "结果未知", pending: "待审批", approved: "已批准", denied: "已拒绝",
    };
    return labels[status] ?? status;
}

function usageLabel(usage: JsonObject): string {
    const input = typeof usage.inputTokens === "number" ? usage.inputTokens : 0;
    const output = typeof usage.outputTokens === "number" ? usage.outputTokens : 0;
    const tools = typeof usage.toolCalls === "number" ? usage.toolCalls : 0;
    return `${input + output} tokens · ${tools} tools`;
}

function safeJson(value: JsonObject): string {
    const text = JSON.stringify(value, null, 2);
    return text.length <= 16_384 ? text : `${text.slice(0, 16_384)}\n…（完整内容见 Artifact）`;
}

function actionableMessage(error: unknown): string {
    if (error instanceof RemoteRpcError && error.envelope.code === "resource.conflict") {
        const reason = error.envelope.details?.reason;
        if ([
            "session_active_or_mutating",
            "session_active_run",
            "session_operation_in_progress",
            "session_active_effectful_run",
            "session_active_run_unavailable",
        ].includes(typeof reason === "string" ? reason : "")) {
            return "OfferAgent：当前会话仍有任务运行或会话操作尚未完成；请等待完成，或先取消当前任务。";
        }
        if (reason === "session_revision_conflict") {
            return "OfferAgent：会话已被其他操作更新，请重新打开会话后再试。";
        }
        if (reason === "session_fork_rejected") {
            return "OfferAgent：当前节点不能 Fork；请选择已完成且仍存在的对话节点。";
        }
        return "OfferAgent：资源状态已变化，请刷新当前会话后重试。";
    }
    return error instanceof Error ? `OfferAgent：${error.message}` : "OfferAgent 操作失败，请打开诊断。";
}

function cssToken(value: string): string {
    return value.replace(/[^a-z0-9_-]/gi, "-").toLowerCase();
}

function safeVaultPath(value: string): boolean {
    return value.length > 0 && value.length <= 1024 && !value.startsWith("/") && !value.includes("\\") &&
        !value.includes("\0") && value.split("/").every((part) => part !== "" && part !== "." && part !== "..");
}

function isTerminal(status: string): boolean {
    return ["completed", "cancelled", "failed", "interrupted", "orphaned"].includes(status);
}
