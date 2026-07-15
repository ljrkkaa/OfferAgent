import { randomBytes } from "node:crypto";

import { EventEnvelope, ProjectionState } from "./event_reducer";
import type { ContentBlock } from "./generated_protocol";
import { HarnessClient } from "./harness_client";
import { JsonObject, JsonValue, requireJsonObject } from "./json_rpc";

export interface ChatTab {
    readonly tabId: string;
    sessionId: string | null;
    title: string;
    draft: string;
    selectedRunId: string | null;
}

export interface PersistedChatTabs {
    readonly schemaVersion: 1;
    readonly activeTabId: string;
    readonly tabs: readonly {
        tabId: string;
        sessionId: string | null;
        title: string;
        draft: string;
        selectedRunId: string | null;
    }[];
}

export interface ChatStorePersistence {
    load(): Promise<unknown>;
    save(value: PersistedChatTabs): Promise<void>;
}

export interface TurnRunConfig {
    provider: string;
    model: string;
    reasoningEffort: "minimal" | "low" | "medium" | "high" | "max";
    permissionMode: "read-only" | "normal" | "trusted-workspace" | "plan" | "bypass";
}

export interface SendTurnOptions {
    readonly attachments?: readonly ContentBlock[];
    readonly runConfig: TurnRunConfig;
    readonly deadline?: string | null;
}

export interface ChatStoreSnapshot {
    readonly tabs: readonly ChatTab[];
    readonly activeTabId: string;
    readonly projection: ProjectionState;
    /**
     * Transient, local submissions that have not yet been confirmed by the
     * durable `turn.started` event.  They are keyed and reconciled by the
     * exact Turn id; they are never persisted as conversation history.
     */
    readonly pendingSubmissions: readonly PendingSubmission[];
    readonly busy: boolean;
    readonly lastError: string | null;
}

export interface PendingSubmission {
    readonly tabId: string;
    readonly turnId: string;
    readonly text: string;
}

interface HydratedSession {
    readonly sessionId: string;
    readonly title: string;
    readonly activeRunId: string | null;
}

interface SessionHydrationSnapshot extends HydratedSession {
    readonly runHeads: Readonly<Record<string, number>>;
}

const MAX_SESSION_HYDRATION_STABILIZATION_PASSES = 8;

export class ChatStore {
    private readonly client: HarnessClient;
    private readonly persistence: ChatStorePersistence;
    private readonly listeners = new Set<(snapshot: ChatStoreSnapshot) => void>();
    private tabs: ChatTab[] = [];
    private activeTabId = "";
    private operationCount = 0;
    private lastError: string | null = null;
    private disposed = false;
    private unsubscribeEvents: (() => void) | null = null;
    private readonly sessionHydrations = new Map<string, Promise<HydratedSession>>();
    private readonly sendsInFlight = new Set<string>();
    private readonly pendingSubmissions = new Map<string, PendingSubmission>();

    constructor(client: HarnessClient, persistence: ChatStorePersistence) {
        this.client = client;
        this.persistence = persistence;
    }

    get snapshot(): ChatStoreSnapshot {
        return {
            tabs: this.tabs.map((tab) => ({ ...tab })),
            activeTabId: this.activeTabId,
            projection: this.client.reducer.state,
            pendingSubmissions: [...this.pendingSubmissions.values()].map((submission) => ({ ...submission })),
            busy: this.operationCount > 0,
            lastError: this.lastError,
        };
    }

    get activeTab(): ChatTab {
        const tab = this.tabs.find((candidate) => candidate.tabId === this.activeTabId);
        if (!tab) throw new Error("active OfferAgent tab is missing");
        return tab;
    }

    async initialize(): Promise<void> {
        this.requireAlive();
        if (this.unsubscribeEvents) return;
        const persisted = parsePersistedTabs(await this.persistence.load());
        if (persisted) {
            this.tabs = persisted.tabs.map((tab) => ({ ...tab }));
            this.activeTabId = persisted.activeTabId;
        } else {
            const tab = newTab();
            this.tabs = [tab];
            this.activeTabId = tab.tabId;
            await this.persist();
        }
        this.unsubscribeEvents = this.client.reducer.subscribe((event) => this.onEvent(event));
        try {
            const sessionIds = [...new Set(this.tabs.map((tab) => tab.sessionId).filter(isText))];
            const failedSessions = new Map<string, string>();
            for (const sessionId of sessionIds) {
                try {
                    this.applyHydratedSession(await this.hydrateSession(sessionId));
                } catch (error) {
                    failedSessions.set(sessionId, errorMessage(error));
                }
            }
            if (failedSessions.size > 0) {
                const active = this.tabs.find((tab) => tab.tabId === this.activeTabId);
                if (typeof active?.sessionId === "string" && failedSessions.has(active.sessionId)) {
                    let fallback = this.tabs.find((tab) => tab.sessionId === null);
                    if (!fallback) {
                        fallback = newTab();
                        this.tabs.push(fallback);
                    }
                    this.activeTabId = fallback.tabId;
                }
                const details = [...failedSessions.values()].slice(0, 3).join("；");
                const suffix = failedSessions.size > 3 ? `；另有 ${failedSessions.size - 3} 个会话` : "";
                this.lastError = `部分历史会话恢复失败，可选择原标签重试：${details}${suffix}`;
            }
            await this.persistAndEmit();
        } catch (error) {
            this.unsubscribeEvents();
            this.unsubscribeEvents = null;
            throw error;
        }
    }

    subscribe(listener: (snapshot: ChatStoreSnapshot) => void): () => void {
        this.listeners.add(listener);
        listener(this.snapshot);
        return () => this.listeners.delete(listener);
    }

    async createTab(): Promise<ChatTab> {
        this.requireInitialized();
        const tab = newTab();
        this.tabs.push(tab);
        this.activeTabId = tab.tabId;
        await this.persistAndEmit();
        return { ...tab };
    }

    async openSession(sessionId: string): Promise<ChatTab> {
        requireId(sessionId, "ses_");
        return this.operation(async () => {
            const hydrated = await this.hydrateSession(sessionId);
            this.requireAlive();
            this.applyHydratedSession(hydrated);
            let tab = this.tabs.find((candidate) => candidate.sessionId === sessionId);
            if (!tab) {
                tab = {
                    ...newTab(),
                    sessionId,
                    title: hydrated.title,
                    selectedRunId: hydrated.activeRunId,
                };
                this.tabs.push(tab);
            }
            this.activeTabId = tab.tabId;
            await this.persistAndEmit();
            return { ...tab };
        });
    }

    async selectTab(tabId: string): Promise<void> {
        this.requireInitialized();
        const tab = this.tabs.find((candidate) => candidate.tabId === tabId);
        if (!tab) throw new Error("OfferAgent tab does not exist");
        if (tab.sessionId === null) {
            this.activeTabId = tabId;
            await this.persistAndEmit();
            return;
        }
        const sessionId = tab.sessionId;
        await this.operation(async () => {
            this.applyHydratedSession(await this.hydrateSession(sessionId));
            this.requireAlive();
            const current = this.tabs.find((candidate) => candidate.tabId === tabId);
            if (!current || current.sessionId !== sessionId) return;
            this.activeTabId = tabId;
            await this.persistAndEmit();
        });
    }

    async closeTab(tabId: string): Promise<void> {
        this.requireInitialized();
        const index = this.tabs.findIndex((tab) => tab.tabId === tabId);
        if (index < 0) return;
        // Closing a UI tab intentionally does not cancel its background Run.
        this.tabs.splice(index, 1);
        if (this.tabs.length === 0) this.tabs.push(newTab());
        if (this.activeTabId === tabId) this.activeTabId = this.tabs[Math.min(index, this.tabs.length - 1)].tabId;
        await this.persistAndEmit();
    }

    async updateDraft(tabId: string, text: string): Promise<void> {
        this.requireInitialized();
        requireId(tabId, "tab_");
        if (text.length > 1_048_576) throw new RangeError("draft exceeds 1 MiB");
        const tab = this.tabs.find((candidate) => candidate.tabId === tabId);
        // A delayed composer save may outlive a closed tab.  It must never
        // fall through to whichever tab happens to be active at that point.
        if (!tab) return;
        tab.draft = text;
        // Draft persistence is intentionally silent: rebuilding the focused
        // textarea here aborts an active Windows IME composition.
        await this.persist();
    }

    async bindSession(sessionId: string, title?: string): Promise<void> {
        requireId(sessionId, "ses_");
        const tab = this.activeTab;
        tab.sessionId = sessionId;
        if (title) tab.title = normalizeTitle(title);
        await this.persistAndEmit();
    }

    async createSession(title?: string): Promise<string> {
        const tabId = this.activeTab.tabId;
        return this.operation(async () => await this.createSessionForTab(tabId, title));
    }

    async send(message: string, options: SendTurnOptions): Promise<{ turnId: string; runId: string }> {
        this.requireInitialized();
        const normalized = message.trim();
        if (!normalized) throw new TypeError("message cannot be blank");
        if (normalized.length > 1_048_576) throw new RangeError("message exceeds 1 MiB");
        const tabId = this.activeTab.tabId;
        const initialTab = this.tabs.find((candidate) => candidate.tabId === tabId);
        if (!initialTab) throw new Error("active OfferAgent tab is missing");
        const tabSendKey = `tab:${tabId}`;
        let sessionSendKey = initialTab.sessionId === null ? null : `session:${initialTab.sessionId}`;
        if (this.sendsInFlight.has(tabSendKey) ||
            (sessionSendKey !== null && this.sendsInFlight.has(sessionSendKey))) {
            return Promise.reject(new Error("当前会话正在发送，请等待本次请求被接收"));
        }
        if (initialTab.sessionId !== null && this.sessionHasActiveRun(initialTab.sessionId)) {
            return Promise.reject(new Error("当前会话仍在运行，请使用转向、取消或等待完成"));
        }
        this.sendsInFlight.add(tabSendKey);
        if (sessionSendKey !== null) this.sendsInFlight.add(sessionSendKey);
        const turnId = opaqueId("turn_");
        this.pendingSubmissions.set(turnId, { tabId, turnId, text: normalized });
        const operation = this.operation(async () => {
            let accepted = false;
            const tab = this.tabs.find((candidate) => candidate.tabId === tabId);
            try {
                if (!tab) throw new Error("OfferAgent tab was closed before send started");
                const sessionId = tab.sessionId ?? await this.createSessionForTab(tabId);
                sessionSendKey = `session:${sessionId}`;
                this.sendsInFlight.add(sessionSendKey);
                const idempotencyKey = opaqueId("turn_");
                const input: ContentBlock[] = [{ type: "text", text: normalized }, ...(options.attachments ?? [])];
                const result = requireJsonObject(await this.client.request("turn/start", {
                    sessionId,
                    turnId,
                    idempotencyKey,
                    input,
                    runConfig: {
                        provider: options.runConfig.provider,
                        model: options.runConfig.model,
                        reasoningEffort: options.runConfig.reasoningEffort,
                        permissionMode: options.runConfig.permissionMode,
                    },
                    deadline: options.deadline ?? null,
                }));
                const returnedTurnId = textField(result, "turnId");
                if (returnedTurnId !== turnId) throw new Error("Worker returned a different Turn id");
                const runId = textField(result, "runId");
                requireId(runId, "run_");
                accepted = true;
                tab.selectedRunId = runId;
                tab.draft = "";
                await this.persistAndEmit();
                return { turnId, runId };
            } finally {
                // A successful command remains visible until the exact durable
                // event arrives.  Failed pre-acceptance attempts never become
                // conversation history and disappear from the transient view.
                if (!accepted) this.pendingSubmissions.delete(turnId);
            }
        });
        return operation.finally(() => {
            this.sendsInFlight.delete(tabSendKey);
            if (sessionSendKey !== null) this.sendsInFlight.delete(sessionSendKey);
        });
    }

    async cancel(runId: string, sessionId: string, turnId: string, reason = "用户取消"): Promise<JsonObject> {
        requireId(runId, "run_");
        requireId(sessionId, "ses_");
        requireId(turnId, "turn_");
        return this.operation(async () => requireJsonObject(await this.client.request("turn/cancel", {
            sessionId,
            turnId,
            runId,
            reason,
        })));
    }

    async retry(sessionId: string, turnId: string, sourceRunId: string, runConfig?: TurnRunConfig): Promise<JsonObject> {
        return this.operation(async () => requireJsonObject(await this.client.request("turn/retry", {
            sessionId,
            turnId,
            sourceRunId,
            idempotencyKey: opaqueId("retry_"),
            runConfig: runConfig ? {
                provider: runConfig.provider,
                model: runConfig.model,
                reasoningEffort: runConfig.reasoningEffort,
                permissionMode: runConfig.permissionMode,
            } : null,
        })));
    }

    async steer(runId: string, text: string, mode: "append" | "steer" = "steer"): Promise<JsonObject> {
        requireId(runId, "run_");
        if (!text.trim()) throw new TypeError("steer message cannot be blank");
        return this.operation(async () => requireJsonObject(await this.client.request("turn/steer", {
            runId,
            messageId: opaqueId("msg_"),
            input: [{ type: "text", text }],
            mode,
        })));
    }

    async resolveApproval(
        approvalId: string,
        decision: "deny" | "allow_once" | "allow_run" | "allow_session" | "allow_persistent",
        scope: "once" | "run" | "session" | "persistent",
        expectedArgsHash: string,
        includeDescendants = false,
        comment: string | null = null,
    ): Promise<JsonObject> {
        requireId(approvalId, "apr_");
        if (!/^sha256:[0-9a-f]{64}$/.test(expectedArgsHash)) throw new TypeError("invalid approval args hash");
        return this.operation(async () => requireJsonObject(await this.client.request("approval/resolve", {
            approvalId,
            decision,
            scope,
            expectedArgsHash,
            includeDescendants,
            comment,
        })));
    }

    async compact(sessionId: string, throughTurnId: string | null = null, force = false): Promise<JsonObject> {
        requireId(sessionId, "ses_");
        if (throughTurnId !== null) requireId(throughTurnId, "turn_");
        return this.operation(async () => requireJsonObject(await this.client.request("session/compact", {
            sessionId,
            throughTurnId,
            force,
        })));
    }

    async fork(sessionId: string, turnId: string, runId: string | null = null, title: string | null = null): Promise<string> {
        return this.operation(async () => {
            const result = requireJsonObject(await this.client.request("session/fork", {
                sessionId,
                forkTurnId: turnId,
                forkRunId: runId,
                title,
                clientRequestId: opaqueId("req_"),
            }));
            const forked = objectField(result, "session");
            const forkedId = textField(forked, "sessionId");
            await this.openSession(forkedId);
            return forkedId;
        });
    }

    async dispose(): Promise<void> {
        if (this.disposed) return;
        this.disposed = true;
        this.unsubscribeEvents?.();
        this.unsubscribeEvents = null;
        this.listeners.clear();
    }

    private async operation<T>(action: () => Promise<T>): Promise<T> {
        this.requireInitialized();
        this.operationCount += 1;
        this.lastError = null;
        this.emit();
        try {
            return await action();
        } catch (error) {
            this.lastError = error instanceof Error ? error.message : "OfferAgent 命令失败";
            throw error;
        } finally {
            this.operationCount -= 1;
            this.emit();
        }
    }

    private async createSessionForTab(tabId: string, title?: string): Promise<string> {
        const requestId = opaqueId("req_");
        const result = requireJsonObject(await this.client.request("session/create", {
            title: title ? normalizeTitle(title) : null,
            clientRequestId: requestId,
        }));
        const session = objectField(result, "session");
        const sessionId = textField(session, "sessionId");
        requireId(sessionId, "ses_");
        const tab = this.tabs.find((candidate) => candidate.tabId === tabId);
        if (!tab) throw new Error("OfferAgent tab was closed before Session creation completed");
        tab.sessionId = sessionId;
        tab.title = normalizeTitle(optionalText(session, "title") ?? title ?? "新对话");
        await this.persistAndEmit();
        return sessionId;
    }

    private sessionHasActiveRun(sessionId: string): boolean {
        for (const run of this.client.reducer.state.runs.values()) {
            if (run.sessionId === sessionId && !isTerminalRunStatus(run.status)) return true;
        }
        return false;
    }

    private onEvent(event: EventEnvelope): void {
        if (event.type === "turn.started" && event.turnId !== null) {
            this.pendingSubmissions.delete(event.turnId);
        }
        if (event.runId && event.sessionId && !this.sessionHydrations.has(event.sessionId)) {
            for (const tab of this.tabs) {
                if (tab.sessionId === event.sessionId && tab.selectedRunId === null) tab.selectedRunId = event.runId;
            }
        }
        if (event.type === "session.updated") {
            const summary = objectField(event.payload, "session");
            const sessionId = textField(summary, "sessionId");
            const title = optionalText(summary, "title");
            if (title) for (const tab of this.tabs) if (tab.sessionId === sessionId) tab.title = title;
        }
        this.emit();
    }

    private hydrateSession(sessionId: string): Promise<HydratedSession> {
        const existing = this.sessionHydrations.get(sessionId);
        if (existing) return existing;
        const hydration = this.hydrateSessionOnce(sessionId).finally(() => {
            if (this.sessionHydrations.get(sessionId) === hydration) this.sessionHydrations.delete(sessionId);
        });
        this.sessionHydrations.set(sessionId, hydration);
        return hydration;
    }

    private async hydrateSessionOnce(sessionId: string): Promise<HydratedSession> {
        let snapshot = await this.loadSessionHydrationSnapshot(sessionId);
        for (let pass = 0; pass < MAX_SESSION_HYDRATION_STABILIZATION_PASSES; pass += 1) {
            await this.replayToSessionHeads(snapshot);
            const latest = await this.loadSessionHydrationSnapshot(sessionId);
            requireMonotonicRunHeads(snapshot.runHeads, latest.runHeads);
            if (sameHydrationWatermark(snapshot, latest)) {
                return {
                    sessionId: latest.sessionId,
                    title: latest.title,
                    activeRunId: latest.activeRunId,
                };
            }
            snapshot = latest;
        }
        throw new Error("Session 在恢复期间持续变化，未能在有界重试内取得稳定持久水位");
    }

    private async loadSessionHydrationSnapshot(sessionId: string): Promise<SessionHydrationSnapshot> {
        const result = requireJsonObject(await this.client.request("session/get", { sessionId, includeTurns: true }));
        const detail = objectField(result, "session");
        const summary = objectField(detail, "summary");
        const returnedSessionId = textField(summary, "sessionId");
        requireId(returnedSessionId, "ses_");
        if (returnedSessionId !== sessionId) throw new Error("Worker returned another Session during hydration");
        const title = normalizeTitle(textField(summary, "title"));
        const activeRunId = optionalText(summary, "activeRunId");
        if (activeRunId !== null) requireId(activeRunId, "run_");
        const runHeads = persistentRunHeads(detail, sessionId);
        if (activeRunId !== null && !(activeRunId in runHeads)) {
            throw new Error("Session active Run is absent from its persistent turns");
        }
        return { sessionId, title, activeRunId, runHeads };
    }

    private async replayToSessionHeads(snapshot: SessionHydrationSnapshot): Promise<void> {
        const { sessionId, runHeads } = snapshot;
        const localCursors = this.client.reducer.sessionRunCursors(sessionId);
        const replayCursors: Record<string, number> = { ...localCursors };
        for (const runId of Object.keys(runHeads)) replayCursors[runId] ??= 0;
        const replayedCursors = await this.client.replaySession(sessionId, replayCursors);
        for (const [runId, durableSequence] of Object.entries(runHeads)) {
            const replayedSequence = replayedCursors[runId];
            if (replayedSequence === undefined || replayedSequence < durableSequence ||
                this.client.reducer.runLastSequence(runId) < durableSequence) {
                throw new Error(`Session replay did not reach the durable Run head: ${runId}`);
            }
        }
    }

    private applyHydratedSession(session: HydratedSession): void {
        for (const tab of this.tabs) {
            if (tab.sessionId !== session.sessionId) continue;
            tab.title = session.title;
            if (session.activeRunId !== null) {
                tab.selectedRunId = session.activeRunId;
                continue;
            }
            const selected = tab.selectedRunId === null ? undefined : this.client.reducer.state.runs.get(tab.selectedRunId);
            if (!selected || selected.sessionId !== session.sessionId || isTerminalRunStatus(selected.status)) {
                tab.selectedRunId = null;
            }
        }
    }

    private async persistAndEmit(): Promise<void> {
        await this.persist();
        this.emit();
    }

    private persist(): Promise<void> {
        return this.persistence.save({
            schemaVersion: 1,
            activeTabId: this.activeTabId,
            tabs: this.tabs.map((tab) => ({ ...tab })),
        });
    }

    private emit(): void {
        const snapshot = this.snapshot;
        for (const listener of this.listeners) listener(snapshot);
    }

    private requireInitialized(): void {
        this.requireAlive();
        if (!this.unsubscribeEvents || this.tabs.length === 0) throw new Error("ChatStore is not initialized");
    }

    private requireAlive(): void {
        if (this.disposed) throw new Error("ChatStore is disposed");
    }
}

export function parsePersistedTabs(raw: unknown): PersistedChatTabs | null {
    if (!raw || typeof raw !== "object" || Array.isArray(raw)) return null;
    const value = raw as Record<string, unknown>;
    if (value.schemaVersion !== 1 || typeof value.activeTabId !== "string" || !Array.isArray(value.tabs) ||
        value.tabs.length < 1 || value.tabs.length > 64) return null;
    const tabs: ChatTab[] = [];
    const ids = new Set<string>();
    for (const rawTab of value.tabs) {
        if (!rawTab || typeof rawTab !== "object" || Array.isArray(rawTab)) return null;
        const tab = rawTab as Record<string, unknown>;
        if (Object.keys(tab).length !== 5 || typeof tab.tabId !== "string" || typeof tab.title !== "string" ||
            typeof tab.draft !== "string" || (tab.sessionId !== null && typeof tab.sessionId !== "string") ||
            (tab.selectedRunId !== null && typeof tab.selectedRunId !== "string")) return null;
        try {
            requireId(tab.tabId, "tab_");
            if (tab.sessionId !== null) requireId(tab.sessionId, "ses_");
            if (tab.selectedRunId !== null) requireId(tab.selectedRunId, "run_");
            if (ids.has(tab.tabId) || tab.title.length > 512 || tab.draft.length > 1_048_576) return null;
        } catch (error) {
            return null;
        }
        ids.add(tab.tabId);
        tabs.push({
            tabId: tab.tabId,
            sessionId: tab.sessionId,
            title: tab.title,
            draft: tab.draft,
            selectedRunId: tab.selectedRunId,
        });
    }
    if (!ids.has(value.activeTabId)) return null;
    return { schemaVersion: 1, activeTabId: value.activeTabId, tabs };
}

function newTab(): ChatTab {
    return { tabId: opaqueId("tab_"), sessionId: null, title: "新对话", draft: "", selectedRunId: null };
}

function opaqueId(prefix: string): string {
    return `${prefix}${randomBytes(16).toString("hex")}`;
}

function normalizeTitle(value: string): string {
    const title = value.trim();
    if (!title || title.length > 512 || title.includes("\0")) throw new TypeError("invalid Session title");
    return title;
}

function requireId(value: string, prefix: string): void {
    const pattern = new RegExp(`^${prefix}[A-Za-z0-9][A-Za-z0-9_-]*$`);
    if (value.length > 128 || !pattern.test(value)) throw new TypeError(`invalid ${prefix} identifier`);
}

function textField(value: JsonObject, key: string): string {
    const field = value[key];
    if (typeof field !== "string" || !field) throw new Error(`${key} is missing`);
    return field;
}

function optionalText(value: JsonObject, key: string): string | null {
    return value[key] === null || value[key] === undefined ? null : textField(value, key);
}

function persistentRunHeads(detail: JsonObject, sessionId: string): Readonly<Record<string, number>> {
    const turns = arrayField(detail, "turns", 10_000);
    const heads = new Map<string, number>();
    for (const rawTurn of turns) {
        const turn = requireJsonObject(rawTurn);
        if (textField(turn, "sessionId") !== sessionId) throw new Error("Session turn belongs to another Session");
        const turnId = textField(turn, "turnId");
        requireId(turnId, "turn_");
        for (const rawRun of arrayField(turn, "runs", 128)) {
            const run = requireJsonObject(rawRun);
            const runId = textField(run, "runId");
            requireId(runId, "run_");
            if (textField(run, "sessionId") !== sessionId || textField(run, "turnId") !== turnId) {
                throw new Error("Session Run lineage does not match its persistent turn");
            }
            if (heads.has(runId)) throw new Error("Session contains a duplicate persistent Run");
            heads.set(runId, optionalInteger(run, "lastSequence") ?? 0);
        }
    }
    return Object.fromEntries([...heads].sort(([left], [right]) => left < right ? -1 : left > right ? 1 : 0));
}

function sameHydrationWatermark(
    left: SessionHydrationSnapshot,
    right: SessionHydrationSnapshot,
): boolean {
    if (left.activeRunId !== right.activeRunId) return false;
    const leftHeads = Object.entries(left.runHeads);
    const rightHeads = Object.entries(right.runHeads);
    return leftHeads.length === rightHeads.length && leftHeads.every(
        ([runId, sequence], index) => rightHeads[index]?.[0] === runId && rightHeads[index]?.[1] === sequence,
    );
}

function requireMonotonicRunHeads(
    previous: Readonly<Record<string, number>>,
    latest: Readonly<Record<string, number>>,
): void {
    for (const [runId, sequence] of Object.entries(previous)) {
        if (!(runId in latest) || latest[runId] < sequence) {
            throw new Error(`Session persistent Run head regressed during hydration: ${runId}`);
        }
    }
}

function errorMessage(error: unknown): string {
    if (error instanceof Error && error.message.trim()) return error.message.trim().slice(0, 512);
    return "未知恢复错误";
}

function arrayField(value: JsonObject, key: string, maximum: number): JsonValue[] {
    const field = value[key];
    if (!Array.isArray(field) || field.length > maximum) throw new Error(`${key} is invalid`);
    return field;
}

function optionalInteger(value: JsonObject, key: string): number | null {
    const field = value[key];
    if (field === null || field === undefined) return null;
    if (typeof field !== "number" || !Number.isSafeInteger(field) || field < 0) throw new Error(`${key} is invalid`);
    return field;
}

function objectField(value: JsonObject, key: string): JsonObject {
    const field: JsonValue | undefined = value[key];
    if (field === null || typeof field !== "object" || Array.isArray(field)) throw new Error(`${key} is missing`);
    return field;
}

function isText(value: string | null): value is string {
    return typeof value === "string";
}

function isTerminalRunStatus(status: string): boolean {
    return ["completed", "cancelled", "failed", "interrupted", "orphaned"].includes(status);
}
