import { ItemView, Menu, Notice, WorkspaceLeaf, setIcon } from "obsidian";

import { BootstrapSnapshot } from "../runtime/bootstrap";
import { ChatStore, ChatStoreSnapshot } from "../runtime/chat_store";
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
    openLocalWeb(): Promise<void>;
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
        const web = actions.createEl("button", { attr: { "aria-label": "打开本地 Web UI" } });
        setIcon(web, "external-link");
        web.onclick = () => void this.host.openLocalWeb().catch((error) => new Notice(actionableMessage(error)));
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
        input.onkeydown = (event) => {
            const blocked = snapshot.busy || this.sendPending || hasActiveRun;
            if (shouldSendComposerInput(event, this.composing, blocked)) {
                event.preventDefault();
                void this.send(input.value);
            }
        };
        const controls = composer.createDiv({ cls: "offeragent-composer-controls" });
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
        send.disabled = snapshot.busy || this.sendPending || hasActiveRun;
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
        this.scheduleRender();
        try {
            await store.send(message, {
                runConfig: runConfig(this.host.settings),
            });
            this.clearComposerTabId = tab.tabId;
        } catch (error) {
            new Notice(actionableMessage(error));
        } finally {
            this.sendPending = false;
            this.scheduleRender();
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
