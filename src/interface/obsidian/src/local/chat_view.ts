import { createHash } from "node:crypto";

import { ItemView, Menu, Notice, Vault, WorkspaceLeaf, setIcon } from "obsidian";

import { BootstrapSnapshot } from "../runtime/bootstrap";
import {
    ChatStore,
    ChatStoreSnapshot,
    DraftAttachment,
    DraftAttachmentMediaType,
    MAX_DRAFT_ATTACHMENTS,
    MAX_DRAFT_ATTACHMENT_BYTES,
    managedAttachmentPath,
} from "../runtime/chat_store";
import type {
    ApprovalTimelineItem,
    RunViewState,
    SubagentTimelineItem,
    TimelineItem,
    ToolCallTimelineItem,
} from "../runtime/event_reducer";
import { JsonObject, RemoteRpcError } from "../runtime/json_rpc";
import { sourceReferenceLabel, vaultReferenceTarget } from "../runtime/source_references";
import { LocalOfferAgentSettings, effectivePermissionMode, runConfig } from "./settings";

export const LOCAL_CHAT_VIEW = "offeragent-local-chat";

export interface LocalChatHost {
    settings: LocalOfferAgentSettings;
    runtimeSnapshot(): BootstrapSnapshot;
    subscribeRuntime(listener: (snapshot: BootstrapSnapshot) => void): () => void;
    ensureChatStore(): Promise<ChatStore>;
    readArtifactText(artifactId: string): Promise<string>;
    listSessions(): Promise<readonly { sessionId: string; title: string }[]>;
    openDiagnostics(): Promise<void>;
}

export class LocalChatView extends ItemView {
    private readonly host: LocalChatHost;
    private store: ChatStore | null = null;
    private snapshot: ChatStoreSnapshot | null = null;
    private unsubscribeStore: (() => void) | null = null;
    private unsubscribeRuntime: (() => void) | null = null;
    private boundRuntimeAttempt = "";
    private bindingStore = false;
    private renderScheduled = false;
    private renderDeferredForComposition = false;
    private draftTimer: ReturnType<typeof setTimeout> | null = null;
    private composing = false;
    private sendPending = false;
    private clearComposerTabId: string | null = null;
    private readonly attachmentImportsInFlight = new Map<string, number>();

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
        this.unsubscribeRuntime = this.host.subscribeRuntime((snapshot) => {
            this.scheduleRender();
            const attempt = `${snapshot.generation}:${snapshot.attempt}`;
            if (snapshot.state === "ready" && attempt !== this.boundRuntimeAttempt && !this.bindingStore) {
                void this.bindCurrentStore(attempt);
            }
        });
        this.scheduleRender();
        const runtime = this.host.runtimeSnapshot();
        if (runtime.state === "ready") await this.bindCurrentStore(`${runtime.generation}:${runtime.attempt}`);
    }

    async onClose(): Promise<void> {
        if (this.draftTimer) clearTimeout(this.draftTimer);
        this.draftTimer = null;
        this.composing = false;
        this.renderDeferredForComposition = false;
        this.unsubscribeStore?.();
        this.unsubscribeRuntime?.();
        this.unsubscribeStore = null;
        this.unsubscribeRuntime = null;
    }

    private async bindCurrentStore(runtimeAttempt: string): Promise<void> {
        if (this.bindingStore) return;
        this.bindingStore = true;
        try {
            const store = await this.host.ensureChatStore();
            this.unsubscribeStore?.();
            this.store = store;
            this.boundRuntimeAttempt = runtimeAttempt;
            this.unsubscribeStore = store.subscribe((snapshot) => {
                this.snapshot = snapshot;
                this.scheduleRender();
            });
        } catch (error) {
            new Notice(actionableMessage(error));
            this.scheduleRender();
        } finally {
            this.bindingStore = false;
        }
    }

    private scheduleRender(): void {
        if (this.composing) {
            this.renderDeferredForComposition = true;
            return;
        }
        if (this.renderScheduled) return;
        this.renderScheduled = true;
        window.requestAnimationFrame(() => {
            this.renderScheduled = false;
            if (this.composing) {
                this.renderDeferredForComposition = true;
                return;
            }
            this.renderDeferredForComposition = false;
            this.render();
        });
    }

    private render(): void {
        const root = this.contentEl;
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
        this.renderTimeline(root, this.snapshot);
        this.renderComposer(root, this.snapshot, selection);
    }

    private renderHeader(root: HTMLElement): void {
        const header = root.createDiv({ cls: "offeragent-local-header" });
        const title = header.createDiv({ cls: "offeragent-local-title" });
        const icon = title.createSpan({ cls: "offeragent-local-title-icon" });
        setIcon(icon, "bot");
        title.createSpan({ text: "OfferAgent" });
        const actions = header.createDiv({ cls: "offeragent-local-header-actions" });
        const diagnostics = actions.createEl("button", { attr: { "aria-label": "运行诊断" } });
        setIcon(diagnostics, "activity");
        diagnostics.onclick = () => void this.host.openDiagnostics().catch((error) => new Notice(actionableMessage(error)));
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

    private renderTimeline(root: HTMLElement, snapshot: ChatStoreSnapshot): void {
        const timeline = root.createDiv({ cls: "offeragent-timeline" });
        const tab = snapshot.tabs.find((candidate) => candidate.tabId === snapshot.activeTabId);
        if (!tab) return;
        const pending = snapshot.pendingSubmissions.filter((submission) => submission.tabId === tab.tabId);
        if (!tab.sessionId && pending.length === 0) {
            const empty = timeline.createDiv({ cls: "offeragent-empty" });
            empty.createEl("h3", { text: "从本地知识库开始" });
            empty.createEl("p", { text: "可以引用活动笔记、选区和 Vault 内容；写入会显示 Diff 并等待相应审批。" });
            return;
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
        timeline.scrollTop = timeline.scrollHeight;
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
        if (submission.attachments.length > 0) {
            const attachments = user.createDiv({ cls: "offeragent-message-attachments" });
            for (const attachment of submission.attachments) {
                const chip = attachments.createDiv({ cls: "offeragent-message-attachment" });
                const icon = chip.createSpan();
                setIcon(icon, attachment.mediaType === "application/pdf" ? "file-text" : "image");
                chip.createSpan({ text: attachment.name });
            }
        }
        article.createDiv({ cls: "offeragent-thinking", text: "正在提交给 OfferAgent…" });
    }

    private renderRun(container: HTMLElement, run: RunViewState): void {
        const article = container.createEl("article", { cls: "offeragent-run", attr: { "data-run-id": run.runId } });
        for (const item of run.timeline) this.renderTimelineItem(article, item, run.parentRunId !== null);
        if (!isTerminal(run.status)) article.createDiv({ cls: "offeragent-thinking", text: phaseLabel(run.phase) });
        if (run.references.length > 0) {
            const references = article.createEl("details", { cls: "offeragent-references" });
            references.createEl("summary", { text: `引用 ${run.references.length}` });
            const list = references.createEl("ul");
            for (const reference of run.references) {
                const item = list.createEl("li");
                const label = sourceReferenceLabel(reference);
                const target = vaultReferenceTarget(reference);
                if (target && safeVaultPath(target.path)) {
                    const link = item.createEl("button", { text: label, cls: "offeragent-reference-link" });
                    const linkText = target.heading ? `${target.path}#${target.heading}` : target.path;
                    link.onclick = () => void this.app.workspace.openLinkText(linkText, "", false);
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
            const retry = actions.createEl("button", { text: "重试" });
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
        }
    }

    private renderTimelineItem(container: HTMLElement, item: TimelineItem, childRun: boolean): void {
        switch (item.kind) {
            case "user_message": {
                const user = container.createDiv({ cls: "offeragent-message offeragent-user" });
                user.createDiv({ cls: "offeragent-message-label", text: item.source === "steer" ? "你（追加）" : "你" });
                for (const block of item.blocks) user.createDiv({ cls: "offeragent-message-body", text: block });
                return;
            }
            case "reasoning": {
                const reasoning = container.createEl("details", { cls: "offeragent-reasoning" });
                reasoning.createEl("summary", { text: item.partial ? "推理摘要（生成中）" : "推理摘要" });
                reasoning.createEl("p", { text: item.summary });
                return;
            }
            case "assistant_message": {
                const assistant = container.createDiv({ cls: "offeragent-message offeragent-assistant" });
                assistant.createDiv({ cls: "offeragent-message-label", text: childRun ? "子任务" : "OfferAgent" });
                for (const block of item.blocks) assistant.createDiv({ cls: "offeragent-message-body", text: block });
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
        const importingAttachment = this.attachmentImportsInFlight.has(tab.tabId);
        const effectiveMode = effectivePermissionMode(this.host.settings);
        const composer = root.createDiv({ cls: "offeragent-composer" });
        composer.ondragover = (event) => {
            if (!event.dataTransfer || !Array.from(event.dataTransfer.types).includes("Files")) return;
            event.preventDefault();
            event.dataTransfer.dropEffect = "copy";
            composer.addClass("is-drag-over");
        };
        composer.ondragleave = (event) => {
            if (!(event.relatedTarget instanceof Node) || !composer.contains(event.relatedTarget)) {
                composer.removeClass("is-drag-over");
            }
        };
        composer.ondrop = (event) => {
            composer.removeClass("is-drag-over");
            const files = event.dataTransfer ? Array.from(event.dataTransfer.files) : [];
            if (files.length === 0) return;
            event.preventDefault();
            void this.importAttachments(tab.tabId, files, input.value);
        };
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
        const input = composer.createEl("textarea", {
            cls: "offeragent-composer-input",
            attr: { placeholder: "询问你的笔记，或交给 OfferAgent 一个任务…", rows: "3" },
        });
        input.value = selection?.value ?? tab.draft;
        input.disabled = this.sendPending;
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
        input.onpaste = (event) => {
            const files = clipboardAttachmentFiles(event.clipboardData);
            if (files.length === 0) return;
            event.preventDefault();
            void this.importAttachments(tab.tabId, files, input.value);
        };
        input.onkeydown = (event) => {
            const blocked = snapshot.busy || this.sendPending || hasActiveRun || importingAttachment;
            if (shouldSendComposerInput(event, this.composing, blocked)) {
                event.preventDefault();
                void this.send(input.value);
            }
        };
        if (tab.draftAttachments.length > 0 || importingAttachment || tab.attachmentError) {
            const panel = composer.createDiv({ cls: "offeragent-attachment-panel" });
            for (const attachment of tab.draftAttachments) {
                const card = panel.createDiv({ cls: "offeragent-attachment-card" });
                const icon = card.createSpan({ cls: "offeragent-attachment-icon" });
                setIcon(icon, attachment.mediaType === "application/pdf" ? "file-text" : "image");
                const details = card.createDiv({ cls: "offeragent-attachment-details" });
                details.createDiv({ cls: "offeragent-attachment-name", text: attachment.name });
                details.createDiv({
                    cls: "offeragent-attachment-meta",
                    text: `${attachmentTypeLabel(attachment.mediaType)} · ${formatFileSize(attachment.size)}`,
                });
                const remove = card.createEl("button", {
                    cls: "offeragent-attachment-remove",
                    attr: { "aria-label": `移除附件 ${attachment.name}` },
                });
                setIcon(remove, "x");
                remove.disabled = this.sendPending || importingAttachment;
                remove.onclick = () => void this.removeAttachment(tab.tabId, attachment, input.value);
            }
            if (importingAttachment) {
                panel.createDiv({ cls: "offeragent-attachment-progress", text: "正在校验并复制附件…" });
            }
            if (tab.attachmentError) {
                panel.createDiv({
                    cls: "offeragent-attachment-error",
                    text: tab.attachmentError,
                    attr: { role: "alert" },
                });
            }
        }
        const controls = composer.createDiv({ cls: "offeragent-composer-controls" });
        const attachmentPicker = composer.createEl("input", {
            cls: "offeragent-attachment-picker",
            attr: {
                type: "file",
                accept: "application/pdf,image/png,image/jpeg,image/webp,.pdf,.png,.jpg,.jpeg,.webp",
                multiple: "multiple",
                tabindex: "-1",
            },
        });
        attachmentPicker.onchange = () => {
            const files = attachmentPicker.files ? Array.from(attachmentPicker.files) : [];
            attachmentPicker.value = "";
            if (files.length > 0) void this.importAttachments(tab.tabId, files, input.value);
        };
        const attach = controls.createEl("button", {
            cls: "offeragent-attach-button",
            attr: { "aria-label": "附加 PDF 或图片", title: "附加 PDF 或图片" },
        });
        setIcon(attach, "paperclip");
        attach.disabled = this.sendPending || importingAttachment ||
            tab.draftAttachments.length >= MAX_DRAFT_ATTACHMENTS;
        attach.onclick = () => attachmentPicker.click();
        if (activeRun) {
            const cancel = controls.createEl("button", { text: "取消" });
            cancel.onclick = () => void this.store?.cancel(activeRun.runId, activeRun.sessionId, activeRun.turnId)
                .catch((error) => new Notice(actionableMessage(error)));
            const steer = controls.createEl("button", { text: "转向" });
            steer.onclick = () => {
                const text = input.value.trim();
                if (!text) return;
                void this.store?.steer(activeRun.runId, text).catch((error) => new Notice(actionableMessage(error)));
            };
        }
        const send = controls.createEl("button", { text: "发送", cls: "mod-cta" });
        send.disabled = snapshot.busy || this.sendPending || hasActiveRun || importingAttachment;
        send.onclick = () => void this.send(input.value);
    }

    private async send(text: string): Promise<void> {
        const message = text.trim();
        const store = this.store;
        const snapshot = this.snapshot;
        const tab = snapshot?.tabs.find((candidate) => candidate.tabId === snapshot.activeTabId);
        if (!message || !store || !snapshot || !tab || this.sendPending || snapshot.busy ||
            activeRunForTab(snapshot, tab.sessionId, tab.selectedRunId) !== undefined) return;
        // Set this before the first await so Enter and click in the same frame
        // cannot start two Turns.
        this.sendPending = true;
        if (this.draftTimer) clearTimeout(this.draftTimer);
        this.draftTimer = null;
        try {
            // Persist the exact visible value before submitting.  A rejected
            // turn/start must be able to reconstruct the unchanged draft.
            await store.updateDraft(tab.tabId, text);
            await store.send(message, {
                runConfig: runConfig(this.host.settings),
            });
            this.clearComposerTabId = tab.tabId;
        } catch (error) {
            // updateDraft intentionally does not emit during normal typing;
            // refresh from its authoritative state when a preflight rejection
            // occurs before ChatStore starts an emitting operation.
            this.snapshot = store.snapshot;
            new Notice(actionableMessage(error));
        } finally {
            this.sendPending = false;
            this.scheduleRender();
        }
    }

    private async importAttachments(tabId: string, files: readonly File[], draft: string): Promise<void> {
        const store = this.store;
        if (!store || files.length === 0) return;
        if (this.sendPending) {
            new Notice("OfferAgent：当前草稿正在发送，附件未导入。");
            return;
        }
        if (this.attachmentImportsInFlight.has(tabId)) {
            new Notice("OfferAgent：当前标签正在导入附件，请等待完成后重试。");
            return;
        }
        const tab = this.snapshot?.tabs.find((candidate) => candidate.tabId === tabId);
        if (!tab) return;
        const available = MAX_DRAFT_ATTACHMENTS - tab.draftAttachments.length;
        if (files.length > available) {
            const message = `每个草稿最多可附加 ${MAX_DRAFT_ATTACHMENTS} 个文件；当前还可添加 ${available} 个`;
            if (this.draftTimer) clearTimeout(this.draftTimer);
            this.draftTimer = null;
            try {
                await store.updateDraft(tabId, draft);
                await store.setAttachmentError(tabId, message);
                new Notice(`OfferAgent：${message}`);
            } catch (error) {
                this.snapshot = store.snapshot;
                new Notice(actionableMessage(error));
            }
            return;
        }
        this.attachmentImportsInFlight.set(tabId, 1);
        if (this.draftTimer) clearTimeout(this.draftTimer);
        this.draftTimer = null;
        const errors: string[] = [];
        try {
            await store.updateDraft(tabId, draft);
            this.scheduleRender();
            await store.setAttachmentError(tabId, null);
            const workspaceId = store.snapshot.projection.workspaceId;
            for (const file of files) {
                try {
                    const attachment = await importManagedAttachment(this.app.vault, file, workspaceId);
                    await store.addDraftAttachment(tabId, attachment);
                } catch (error) {
                    errors.push(`${safeAttachmentName(file.name)}：${plainErrorMessage(error)}`);
                }
            }
            if (errors.length > 0) {
                const message = errors.join("；").slice(0, 512);
                await store.setAttachmentError(tabId, message);
                new Notice(`OfferAgent：${message}`);
            }
        } catch (error) {
            const message = plainErrorMessage(error).slice(0, 512);
            await store.setAttachmentError(tabId, message).catch(() => undefined);
            new Notice(`OfferAgent：${message}`);
        } finally {
            this.attachmentImportsInFlight.delete(tabId);
            this.scheduleRender();
        }
    }

    private async removeAttachment(tabId: string, attachment: DraftAttachment, draft: string): Promise<void> {
        const store = this.store;
        if (!store) return;
        if (this.draftTimer) clearTimeout(this.draftTimer);
        this.draftTimer = null;
        try {
            await store.updateDraft(tabId, draft);
            await store.removeDraftAttachment(tabId, attachment.file.contentHash);
        } catch (error) {
            new Notice(actionableMessage(error));
        }
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

interface AttachmentSource {
    readonly name: string;
    readonly size: number;
    arrayBuffer(): Promise<ArrayBuffer>;
}

export async function importManagedAttachment(
    vault: Vault,
    source: AttachmentSource,
    workspaceId: string,
): Promise<DraftAttachment> {
    if (!/^ws_[A-Za-z0-9][A-Za-z0-9_-]{0,124}$/.test(workspaceId)) {
        throw new Error("Workspace 标识无效，附件未写入");
    }
    if (!Number.isSafeInteger(source.size) || source.size < 1) throw new Error("附件为空，已拒绝导入");
    if (source.size > MAX_DRAFT_ATTACHMENT_BYTES) {
        throw new Error(`附件超过 ${formatFileSize(MAX_DRAFT_ATTACHMENT_BYTES)} 上限，已拒绝导入`);
    }
    const bytes = await source.arrayBuffer();
    if (bytes.byteLength !== source.size) throw new Error("附件读取长度发生变化，已拒绝导入");
    const mediaType = detectAttachmentMediaType(bytes);
    const contentHash = sha256Digest(bytes);
    const path = managedAttachmentPath(contentHash, mediaType);
    const name = normalizedAttachmentName(source.name, mediaType);

    await ensureManagedFolder(vault, "OfferAgent");
    await ensureManagedFolder(vault, "OfferAgent/Attachments");
    let verified = await verifyExistingManagedAttachment(vault, path, contentHash);
    if (!verified) {
        try {
            await vault.createBinary(path, bytes);
        } catch (error) {
            // A concurrent import may win createBinary.  It is accepted only
            // after independently proving that the resulting bytes match.
            verified = await verifyExistingManagedAttachment(vault, path, contentHash);
            if (!verified) throw error;
        }
        if (!verified) verified = await verifyExistingManagedAttachment(vault, path, contentHash);
    }
    if (!verified) throw new Error("附件写入后完整性校验失败，已拒绝引用");
    return {
        name,
        mediaType,
        size: bytes.byteLength,
        file: { workspaceId, path, contentHash },
    };
}

export function detectAttachmentMediaType(bytes: ArrayBuffer): DraftAttachmentMediaType {
    const data = new Uint8Array(bytes);
    if (startsWithBytes(data, [0x25, 0x50, 0x44, 0x46, 0x2d])) return "application/pdf";
    if (startsWithBytes(data, [0x89, 0x50, 0x4e, 0x47, 0x0d, 0x0a, 0x1a, 0x0a])) return "image/png";
    if (startsWithBytes(data, [0xff, 0xd8, 0xff])) return "image/jpeg";
    if (data.length >= 12 && startsWithBytes(data, [0x52, 0x49, 0x46, 0x46]) &&
        data[8] === 0x57 && data[9] === 0x45 && data[10] === 0x42 && data[11] === 0x50) {
        return "image/webp";
    }
    throw new Error("只支持内容有效的 PDF、PNG、JPEG 或 WebP 文件");
}

export function sha256Digest(bytes: ArrayBuffer): string {
    return `sha256:${createHash("sha256").update(new Uint8Array(bytes)).digest("hex")}`;
}

async function ensureManagedFolder(vault: Vault, path: string): Promise<void> {
    if (vault.getFolderByPath(path)) return;
    if (vault.getAbstractFileByPath(path)) throw new Error(`附件管理目录被同名文件占用：${path}`);
    try {
        await vault.createFolder(path);
    } catch (error) {
        if (!vault.getFolderByPath(path)) throw error;
    }
}

async function verifyExistingManagedAttachment(
    vault: Vault,
    path: string,
    expectedHash: string,
): Promise<boolean> {
    const existing = vault.getFileByPath(path);
    if (!existing) {
        if (vault.getAbstractFileByPath(path)) throw new Error(`附件目标路径不是文件：${path}`);
        return false;
    }
    const bytes = await vault.readBinary(existing);
    if (sha256Digest(bytes) !== expectedHash) {
        throw new Error(`内容寻址附件已存在但校验不一致：${path}`);
    }
    return true;
}

function startsWithBytes(value: Uint8Array, expected: readonly number[]): boolean {
    return value.length >= expected.length && expected.every((byte, index) => value[index] === byte);
}

function clipboardAttachmentFiles(data: DataTransfer | null): File[] {
    if (!data) return [];
    const files = Array.from(data.files);
    if (files.length > 0) return files;
    return Array.from(data.items)
        .filter((item) => item.kind === "file")
        .map((item) => item.getAsFile())
        .filter((file): file is File => file !== null);
}

function normalizedAttachmentName(value: string, mediaType: DraftAttachmentMediaType): string {
    if (value.includes("\0")) throw new Error("附件名称包含无效字符");
    const trimmed = value.trim();
    if (trimmed.length > 255) throw new Error("附件名称超过 255 个字符");
    if (trimmed) return trimmed;
    const suffix: Record<DraftAttachmentMediaType, string> = {
        "application/pdf": "pdf",
        "image/png": "png",
        "image/jpeg": "jpg",
        "image/webp": "webp",
    };
    return `未命名附件.${suffix[mediaType]}`;
}

function safeAttachmentName(value: string): string {
    const cleaned = value.replace(/\0/g, "").trim();
    return (cleaned || "未命名附件").slice(0, 96);
}

function plainErrorMessage(error: unknown): string {
    return error instanceof Error && error.message.trim() ? error.message.trim() : "附件导入失败";
}

function attachmentTypeLabel(mediaType: DraftAttachmentMediaType): string {
    if (mediaType === "application/pdf") return "PDF";
    if (mediaType === "image/jpeg") return "JPEG";
    if (mediaType === "image/png") return "PNG";
    return "WebP";
}

function formatFileSize(bytes: number): string {
    if (bytes < 1024) return `${bytes} B`;
    if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`;
    return `${(bytes / (1024 * 1024)).toFixed(1)} MB`;
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
