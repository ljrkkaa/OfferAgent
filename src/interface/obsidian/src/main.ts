import { randomBytes } from "node:crypto";
import { isAbsolute, relative, resolve } from "node:path";

import {
    FileSystemAdapter,
    Modal,
    Notice,
    Plugin,
    WorkspaceLeaf,
    moment,
} from "obsidian";

import { LocalChatView, LOCAL_CHAT_VIEW } from "./local/chat_view";
import type { ChatModelChoice } from "./local/chat_view";
import type { ExtensionCommandMethod } from "./local/extension_settings";
import {
    DEFAULT_LOCAL_SETTINGS,
    LocalOfferAgentSettings,
    LocalOfferAgentSettingTab,
    SerializedOperationQueue,
    modelRuntimePatch,
    parseLocalSettings,
    snapshotLocalSettings,
} from "./local/settings";
import { canBackgroundStartRuntime, RuntimeBootstrap } from "./runtime/bootstrap";
import { ChatStore, PersistedChatTabs, parsePersistedTabs } from "./runtime/chat_store";
import { HarnessClient, REQUIRED_RUNTIME_CAPABILITIES } from "./runtime/harness_client";
import { JsonObject, JsonValue, requireJsonObject } from "./runtime/json_rpc";
import { LocalDevelopmentRuntimeInstaller } from "./runtime/local_development_installer";
import { StdioWorkerTransport } from "./runtime/stdio_worker";
import {
    observePluginToolEvents,
    PluginToolEventObserver,
    SerializedPluginToolExecutionFence,
    VaultToolAdapter,
} from "./runtime/vault_tool_adapter";
import { createHostResearchBrowser, ResearchBrowserAdapter } from "./runtime/research_browser";
import {
    FileVaultChangeJournal,
    GitCheckpointStore,
    ObsidianVaultChangePort,
    VaultChangeAuthorizationDecision,
    VaultChangeAuthorizationProposal,
    VaultChangeCoordinator,
    assertContainedStateDirectory,
    interviewReviewBadges,
} from "./runtime/vault_changes";
import {
    PROTOCOL_SCHEMA_HASH,
    PROTOCOL_VERSION,
} from "./runtime/generated_protocol_identity";
import type {
    ArtifactRef,
    ImageContentBlock,
    ProtocolCommandParams,
    ProtocolCommandResult,
} from "./runtime/generated_protocol";
import { loadOrCreatePortableWorkspaceIdentity } from "./runtime/workspace_identity";

const localMoment = moment as unknown as {
    (): { format(format: string): string };
    (date: string, format: string, strict: boolean): { format(format: string): string };
};

interface LocalPluginData {
    readonly schemaVersion: 2;
    readonly settings: LocalOfferAgentSettings;
    readonly chatTabs: PersistedChatTabs | null;
}

const EXTENSION_ADMIN_METHODS: ReadonlySet<ExtensionCommandMethod> = new Set<ExtensionCommandMethod>([
    "skills/list",
    "skills/status",
    "shell/list",
    "shell/install",
    "shell/confirm",
    "shell/set-enabled",
    "process/registrations/list",
    "process/registrations/probe",
    "process/registrations/confirm",
    "process/registrations/delete",
    "hooks/list",
    "hooks/install",
    "hooks/confirm-layer",
    "hooks/confirm-workspace-command",
]);

export default class OfferAgentPlugin extends Plugin {
    settings: LocalOfferAgentSettings = { ...DEFAULT_LOCAL_SETTINGS };
    private data: LocalPluginData = { schemaVersion: 2, settings: this.settings, chatTabs: null };
    private runtime: RuntimeBootstrap | null = null;
    private chatStore: ChatStore | null = null;
    private chatClient: HarnessClient | null = null;
    private vaultToolClient: HarnessClient | null = null;
    private vaultToolObserver: PluginToolEventObserver | null = null;
    private vaultToolFence: SerializedPluginToolExecutionFence | null = null;
    private vaultToolRetirement: Promise<void> = Promise.resolve();
    private vaultChanges: VaultChangeCoordinator | null = null;
    private researchBrowser: ResearchBrowserAdapter | null = null;
    private runtimeStart: Promise<void> | null = null;
    private vaultRoot = "";
    private workspaceId = "";
    private runtimeRestartTimer: ReturnType<typeof setTimeout> | null = null;
    private runtimeRestartOperation: Promise<void> | null = null;
    private runtimeRestartPending = false;
    private runtimeRestartNoticeShown = false;
    private runtimeExplicitlyStopped = false;
    private unloading = false;
    private settingsGeneration = 0;
    private readonly localDataWrites = new SerializedOperationQueue();
    private readonly runtimeSettingsWrites = new SerializedOperationQueue();

    async onload(): Promise<void> {
        this.vaultRoot = desktopVaultRoot(this);
        await this.loadLocalData();
        const portable = await loadOrCreatePortableWorkspaceIdentity(this.vaultRoot);
        this.workspaceId = portable.portableWorkspaceId;
        this.runtime = this.createRuntime();

        this.registerView(LOCAL_CHAT_VIEW, (leaf) => new LocalChatView(leaf, this));
        this.addRibbonIcon("bot", "打开 OfferAgent", () => void this.activateChat());
        this.addCommand({ id: "open-local-chat", name: "打开本地聊天", callback: () => void this.activateChat() });
        this.addCommand({ id: "new-local-chat", name: "新建本地会话", callback: () => void this.newChat() });
        this.addCommand({ id: "runtime-diagnostics", name: "查看本地 Runtime 诊断", callback: () => void this.openDiagnostics() });
        this.addCommand({
            id: "stop-local-runtime",
            name: "停止当前 Vault 的 OfferAgent Runtime",
            callback: () => void this.stopLocalRuntime(),
        });
        this.addSettingTab(new LocalOfferAgentSettingTab(this.app, this));

        void this.beginRuntimeStart().catch((error) => new Notice(actionableError(error)));
    }

    onunload(): void {
        this.unloading = true;
        if (this.runtimeRestartTimer) clearTimeout(this.runtimeRestartTimer);
        this.runtimeRestartTimer = null;
        const pendingStart = this.runtimeStart;
        const pendingRestart = this.runtimeRestartOperation;
        const chatDisposal = this.chatStore?.dispose().catch(() => undefined);
        this.chatStore = null;
        this.chatClient = null;
        const vaultToolRetirement = this.disposeVaultToolAdapter();
        // Obsidian ignores a Promise returned from onunload. beginUnload aborts
        // lifecycle work and starts stdio EOF synchronously; its normal stop gate
        // continues tracking the child-process join in the background.
        const retirement = this.runtime?.beginUnload();
        void Promise.all([
            chatDisposal,
            vaultToolRetirement,
            retirement?.catch(() => undefined),
            pendingStart?.catch(() => undefined),
            pendingRestart?.catch(() => undefined),
        ]).catch(() => undefined);
    }

    runtimeSnapshot() {
        if (!this.runtime) throw new Error("OfferAgent Runtime 尚未初始化");
        return this.runtime.snapshot;
    }

    subscribeRuntime(listener: (snapshot: ReturnType<OfferAgentPlugin["runtimeSnapshot"]>) => void): () => void {
        if (!this.runtime) throw new Error("OfferAgent Runtime 尚未初始化");
        return this.runtime.subscribe(listener);
    }

    async ensureChatStore(): Promise<ChatStore> {
        await this.ensureReady();
        const client = (this.runtime as RuntimeBootstrap).harness;
        if (this.chatStore && this.chatClient === client) return this.chatStore;
        await this.chatStore?.dispose();
        const store = new ChatStore(client, {
            load: async () => this.data.chatTabs,
            save: async (value) => {
                this.data = { schemaVersion: 2, settings: snapshotLocalSettings(this.settings), chatTabs: value };
                await this.persistLocalData();
            },
        });
        await store.initialize();
        this.chatStore = store;
        this.chatClient = client;
        return store;
    }

    async readArtifactText(artifactId: string): Promise<string> {
        await this.ensureReady();
        if (!/^art_[A-Za-z0-9][A-Za-z0-9_-]{0,123}$/.test(artifactId)) throw new Error("Artifact 标识无效");
        const chunks: string[] = [];
        let offset = 0;
        for (;;) {
            const result = requireJsonObject(await (this.runtime as RuntimeBootstrap).harness.request("artifact/read", {
                artifactId, offset, maxBytes: 262_144,
            }));
            const artifact = requireJsonObject(result.artifact);
            if (artifact.sensitivity === "secret") throw new Error("Secret Artifact 不可在聊天 UI 中展开");
            if (result.encoding !== "utf8") throw new Error("该 Artifact 不是可显示文本");
            chunks.push(requireArtifactContent(result.content));
            const next = requireInteger(result.nextOffset, "Artifact offset");
            if (next <= offset && result.eof !== true) throw new Error("Artifact 分页未前进");
            offset = next;
            if (offset > 4 * 1_048_576) throw new Error("Diff Artifact 超过 4 MiB UI 上限");
            if (result.eof === true) break;
        }
        return chunks.join("");
    }

    async uploadConversationAttachment(sessionId: string, file: File): Promise<ImageContentBlock> {
        await this.ensureReady();
        const mediaTypes = new Set(["image/png", "image/jpeg", "image/gif", "image/webp"]);
        if (!mediaTypes.has(file.type)) throw new Error("仅支持 PNG、JPEG、WebP 或静态 GIF 图片");
        return (this.runtime as RuntimeBootstrap).harness.uploadAttachment(sessionId, {
            fileName: file.name,
            mediaType: file.type as "image/png" | "image/jpeg" | "image/gif" | "image/webp",
            bytes: new Uint8Array(await file.arrayBuffer()),
            altText: file.name,
        });
    }

    async readConversationAttachment(sessionId: string, artifact: ArtifactRef): Promise<Uint8Array> {
        await this.ensureReady();
        return (this.runtime as RuntimeBootstrap).harness.readAttachment(sessionId, artifact);
    }

    async discardConversationAttachment(sessionId: string, artifactId: string): Promise<boolean> {
        await this.ensureReady();
        return (this.runtime as RuntimeBootstrap).harness.discardUploadedAttachment(sessionId, artifactId);
    }

    async deleteConversation(sessionId: string): Promise<boolean> {
        return (await this.ensureChatStore()).deleteSession(sessionId);
    }

    async undoVaultChange(batchId: string): Promise<string> {
        await this.ensureReady();
        if (!/^[A-Za-z0-9][A-Za-z0-9_-]{0,127}$/u.test(batchId)) throw new Error("Vault Change Batch 标识无效");
        const changes = this.vaultChanges;
        const fence = this.vaultToolFence;
        if (changes === null || fence === null) throw new Error("Vault Change Coordinator 尚未就绪");
        const result = await fence.runExclusive(() => changes.undo(batchId));
        if (result.status === "undone") return `已安全撤销 ${result.paths.length} 个文件`;
        if (result.status === "conflict") {
            throw new Error(`撤销已阻止：${result.paths.join(", ")} 已在应用后发生变化。\n${result.diff}`);
        }
        throw new Error("该 Vault Change Batch 不存在、尚未应用或已经撤销");
    }

    async listSessions(): Promise<readonly { sessionId: string; title: string }[]> {
        await this.ensureReady();
        const sessions: { sessionId: string; title: string }[] = [];
        let cursor: string | null = null;
        do {
            const result = requireJsonObject(await (this.runtime as RuntimeBootstrap).harness.request("session/list", {
                cursor, limit: 100, includeDeleted: false,
            }));
            if (!Array.isArray(result.sessions)) throw new Error("Session 列表响应无效");
            for (const raw of result.sessions) {
                const item = requireJsonObject(raw);
                sessions.push({
                    sessionId: requireText(item.sessionId, "Session ID"),
                    title: typeof item.title === "string" && item.title ? item.title : "未命名会话",
                });
            }
            cursor = result.nextCursor === null ? null : requireText(result.nextCursor, "Session cursor");
            if (sessions.length > 10_000) throw new Error("Session 列表超过 UI 上限");
        } while (cursor !== null);
        return sessions;
    }

    async listModels(): Promise<readonly ChatModelChoice[]> {
        await this.ensureReady();
        const result = requireJsonObject(await (this.runtime as RuntimeBootstrap).harness.request("models/list", {}));
        if (!Array.isArray(result.models) || result.models.length > 256) throw new Error("Worker 模型列表无效");
        const freshness = String(result.catalogFreshness);
        if (!["fresh", "stale", "unavailable"].includes(freshness)) throw new Error("Worker 模型目录状态无效");
        if (freshness === "unavailable") {
            const error = result.error === null || result.error === undefined ? null : requireJsonObject(result.error);
            const message = error === null ? "Codex 模型目录当前不可用" : requireText(error.userVisibleMessage, "目录错误");
            throw new Error(message);
        }
        const accountBinding = typeof result.accountBinding === "string" &&
            /^sha256:[0-9a-f]{64}$/.test(result.accountBinding)
            ? result.accountBinding
            : null;
        if (freshness === "fresh" && accountBinding === null) {
            throw new Error("Worker 返回了无效的 Codex 账户绑定");
        }
        return result.models.map((raw) => {
            const model = requireJsonObject(raw);
            const modelId = requireText(model.model, "Model id");
            const displayName = requireText(model.displayName, "Model display name");
            const inputModalities = requireBoundedTextArray(model.inputModalities, "Model input modalities", 16);
            if (modelId.length > 256 || displayName.length > 512 ||
                typeof model.supportsImageDetailOriginal !== "boolean" ||
                typeof model.supportsHostedSearch !== "boolean" || typeof model.supportsFastMode !== "boolean") {
                throw new Error("Worker 返回了无效的 Codex 模型能力");
            }
            return {
                model: modelId,
                accountBinding,
                displayName,
                inputModalities,
                supportsImageDetailOriginal: model.supportsImageDetailOriginal,
                supportsHostedSearch: model.supportsHostedSearch,
                supportsFastMode: model.supportsFastMode,
                contextWindow: model.contextWindow === null || model.contextWindow === undefined
                    ? null : requireInteger(model.contextWindow, "Model context window"),
                catalogFreshness: freshness as ChatModelChoice["catalogFreshness"],
            };
        });
    }

    async selectModel(model: string): Promise<void> {
        const candidate = model.trim();
        if (!candidate || candidate.length > 256 || candidate.includes("\0")) throw new Error("模型标识无效");
        const available = await this.listModels();
        const selected = available.find((item) =>
            item.model === candidate && item.catalogFreshness === "fresh" &&
            item.accountBinding !== null);
        if (!selected) {
            throw new Error("所选模型不在当前有效的 Codex 模型目录中");
        }
        const previousModel = this.settings.model;
        const previousAccountBinding = this.settings.modelAccountBinding;
        this.settings.model = candidate;
        this.settings.modelAccountBinding = selected.accountBinding;
        try {
            await this.saveLocalSettings();
            await this.applyRuntimeSettings();
        } catch (error) {
            this.settings.model = previousModel;
            this.settings.modelAccountBinding = previousAccountBinding;
            await this.saveLocalSettings().catch(() => undefined);
            throw error;
        }
    }

    openSettings(): void {
        const commands = (this.app as unknown as {
            commands: { executeCommandById(commandId: string): boolean };
        }).commands;
        commands.executeCommandById("app:open-settings");
    }

    async saveLocalSettings(): Promise<void> {
        const settings = snapshotLocalSettings(this.settings);
        this.settingsGeneration += 1;
        this.data = { schemaVersion: 2, settings, chatTabs: this.data.chatTabs };
        await this.persistLocalData();
    }

    async applyRuntimeSettings(): Promise<void> {
        await this.ensureReady();
        const settings = snapshotLocalSettings(this.settings);
        const generation = this.settingsGeneration;
        await this.enqueueRuntimeSettingsApply(settings, generation);
    }

    private enqueueRuntimeSettingsApply(
        settings: LocalOfferAgentSettings,
        generation: number,
    ): Promise<void> {
        return this.runtimeSettingsWrites.run(async () => {
            let candidate = settings;
            let candidateGeneration = generation;
            for (;;) {
                if (candidateGeneration !== this.settingsGeneration) {
                    candidate = snapshotLocalSettings(this.settings);
                    candidateGeneration = this.settingsGeneration;
                }
                if (await this.applyRuntimeSettingsToReadyRuntime(candidate, candidateGeneration)) return;
                // Settings changed across an awaited stdio request. Coalesce to one
                // fresh immutable snapshot rather than allowing the stale patch to win.
                candidate = snapshotLocalSettings(this.settings);
                candidateGeneration = this.settingsGeneration;
            }
        });
    }

    private async applyRuntimeSettingsToReadyRuntime(
        settings: LocalOfferAgentSettings,
        generation: number,
    ): Promise<boolean> {
        const client = (this.runtime as RuntimeBootstrap).harness;
        const current = requireJsonObject(await client.request("config/get", { scope: "workspace", sessionId: null }));
        if (generation !== this.settingsGeneration) return false;
        const revision = requireInteger(current.revision, "config revision");
        const patch: JsonObject = {
            model: modelRuntimePatch(settings),
            policy: {
                read_only: settings.permissionMode === "read-only" || settings.permissionMode === "plan",
                workspace_trusted: settings.workspaceTrusted,
                approve_vault_writes: !settings.autoApproveVaultWrites,
                allow_bypass: settings.permissionMode === "bypass",
            },
            execution: {
                shell_enabled: settings.shellEnabled,
                subagents_enabled: settings.subagentsEnabled,
                max_subagents_per_vault: settings.subagentsEnabled ? 8 : 0,
            },
            extensibility: {
                hooks_enabled: settings.hooksEnabled,
            },
            telemetry: { enabled: false, include_content: false },
        };
        const result = requireJsonObject(await client.request("config/update", {
            scope: "workspace",
            sessionId: null,
            expectedRevision: revision,
            patch,
        }));
        if (result.status === "rejected") throw new Error("Runtime 拒绝了设置；请查看字段错误与 Codex 模型目录状态");
        if (result.status !== "applied" && result.status !== "restart_required") {
            throw new Error("Runtime 返回了未知的设置状态");
        }
        if (generation !== this.settingsGeneration) return false;
        const snapshot = requireJsonObject(result.snapshot);
        const restartPending = snapshot.restartPending === true || result.status === "restart_required";
        if (restartPending) {
            this.runtimeRestartPending = true;
            await this.restartRuntimeIfIdle();
        } else {
            this.runtimeRestartPending = false;
            this.runtimeRestartNoticeShown = false;
        }
        return generation === this.settingsGeneration;
    }

    async checkModelCatalog(): Promise<string> {
        const models = await this.listModels();
        if (models.some((model) => model.catalogFreshness !== "fresh")) {
            throw new Error("Codex 模型目录已陈旧，仅供展示；请恢复登录或网络后重新检查");
        }
        if (models.length === 0) throw new Error("当前 Codex 订阅账户没有可见模型");
        const selected = models.find((model) =>
            model.model === this.settings.model &&
            model.accountBinding === this.settings.modelAccountBinding);
        return selected
            ? `Codex 模型目录可用；当前选择 ${selected.displayName}`
            : `Codex 模型目录可用（${models.length} 个模型）；请在 Agent 面板选择模型`;
    }

    async extensionRequest<Method extends ExtensionCommandMethod>(
        method: Method,
        params: ProtocolCommandParams<Method>,
        options?: { signal?: AbortSignal; timeoutMs?: number },
    ): Promise<ProtocolCommandResult<Method>> {
        if (!EXTENSION_ADMIN_METHODS.has(method)) throw new Error("扩展面板请求了未授权的方法");
        await this.ensureReady();
        return (this.runtime as RuntimeBootstrap).harness.request(method, params, options);
    }

    extensionExecutionEnabled(kind: "shell" | "hooks"): boolean {
        return kind === "shell" ? this.settings.shellEnabled : this.settings.hooksEnabled;
    }

    async setExtensionExecutionEnabled(kind: "shell" | "hooks", enabled: boolean): Promise<void> {
        const previous = kind === "shell" ? this.settings.shellEnabled : this.settings.hooksEnabled;
        if (kind === "shell") this.settings.shellEnabled = enabled;
        else this.settings.hooksEnabled = enabled;
        try {
            await this.saveLocalSettings();
            await this.applyRuntimeSettings();
        } catch (error) {
            if (kind === "shell") this.settings.shellEnabled = previous;
            else this.settings.hooksEnabled = previous;
            await this.saveLocalSettings().catch(() => undefined);
            throw error;
        }
    }

    async openDiagnostics(): Promise<void> {
        await this.ensureReady();
        const snapshot = requireJsonObject(await (this.runtime as RuntimeBootstrap).harness.request(
            "diagnostics/snapshot", { includeRecentErrors: true },
        ));
        new DiagnosticsModal(this, snapshot).open();
    }

    private async stopLocalRuntime(): Promise<void> {
        const confirmed = window.confirm(
            "这会停止当前 Vault 的 OfferAgent Worker，并取消其中正在运行的任务。是否继续？",
        );
        if (!confirmed) return;
        const runtime = this.runtime;
        if (!runtime) {
            new Notice("OfferAgent Runtime 尚未初始化，无需停止");
            return;
        }
        this.runtimeExplicitlyStopped = true;
        if (this.runtimeRestartTimer) clearTimeout(this.runtimeRestartTimer);
        this.runtimeRestartTimer = null;
        const pendingStart = this.runtimeStart;
        const pendingRestart = this.runtimeRestartOperation;
        await this.chatStore?.dispose().catch(() => undefined);
        this.chatStore = null;
        this.chatClient = null;
        await this.disposeVaultToolAdapter();
        await runtime.stop();
        await Promise.all([
            pendingStart?.catch(() => undefined),
            pendingRestart?.catch(() => undefined),
        ]);
        new Notice("OfferAgent Runtime 已停止");
    }

    private createRuntime(): RuntimeBootstrap {
        const pluginDirectory = pluginInstallDirectory(this, this.vaultRoot);
        const installer = new LocalDevelopmentRuntimeInstaller({
            pluginDirectory,
            pluginVersion: this.manifest.version,
            protocolVersion: PROTOCOL_VERSION,
            schemaHash: PROTOCOL_SCHEMA_HASH,
        });
        return new RuntimeBootstrap(installer, {
            create: (installed, onDisconnected) => {
                const journalDirectory = assertContainedStateDirectory(
                    this.vaultRoot,
                    resolve(this.vaultRoot, this.app.vault.configDir, "offeragent", "vault-change-journal"),
                );
                const recoveryToken = randomBytes(32).toString("hex");
                const client = new HarnessClient(
                    new StdioWorkerTransport(
                        installed.workerExecutable,
                        this.vaultRoot,
                        installed.version,
                        journalDirectory,
                        recoveryToken,
                    ),
                    {
                        workspaceId: this.workspaceId,
                        identity: {
                            protocolVersion: PROTOCOL_VERSION,
                            minimumProtocolVersion: PROTOCOL_VERSION,
                            maximumProtocolVersion: PROTOCOL_VERSION,
                            schemaHash: PROTOCOL_SCHEMA_HASH,
                            clientVersion: this.manifest.version,
                        },
                        requiredCapabilities: REQUIRED_RUNTIME_CAPABILITIES,
                    },
                    {
                        onDisconnected,
                        beforeConnect: async () => {
                            const fence = this.vaultToolClient === client ? this.vaultToolFence : null;
                            if (fence === null) throw new Error("Vault Change recovery fence is unavailable");
                            await fence.ready();
                        },
                    },
                );
                this.attachVaultToolAdapter(client, journalDirectory, recoveryToken);
                return client;
            },
        });
    }

    private attachVaultToolAdapter(
        client: HarnessClient,
        journalDirectory: string,
        recoveryToken: string,
    ): void {
        if (this.vaultToolClient === client) return;
        const previousRetirement = this.disposeVaultToolAdapter();
        const journal = new FileVaultChangeJournal(journalDirectory);
        const changes = new VaultChangeCoordinator({
            vault: new ObsidianVaultChangePort(this.app.vault),
            checkpoints: new GitCheckpointStore(this.vaultRoot),
            journal,
            permissionMode: () => {
                if (!this.settings.workspaceTrusted || ["read-only", "plan"].includes(this.settings.permissionMode)) {
                    return "read_only";
                }
                return this.settings.autoApproveVaultWrites ? "trusted_vault" : "ask_every_time";
            },
            authorize: async (proposal) => this.authorizeVaultChange(proposal),
        });
        const researchBrowser = createHostResearchBrowser();
        const adapter = new VaultToolAdapter(this.app.vault, client, this.workspaceId, this.app.metadataCache, {
            dailyNotes: {
                readConfiguration: async () => {
                    const path = `${this.app.vault.configDir}/daily-notes.json`;
                    if (!(await this.app.vault.adapter.exists(path))) return undefined;
                    return JSON.parse(await this.app.vault.adapter.read(path)) as {
                        folder?: string;
                        format?: string;
                        template?: string;
                    };
                },
                resolveToday: () => localMoment().format("YYYY-MM-DD"),
                formatDate: (date, format) => localMoment(date, "YYYY-MM-DD", true).format(format),
            },
        }, changes, researchBrowser);
        const fencedAdapter = new SerializedPluginToolExecutionFence(this.vaultRoot, adapter, async () => {
            await previousRetirement;
            await journal.migrateLegacyDirectory(resolve(pluginInstallDirectory(this, this.vaultRoot), "vault-change-journal"));
            await changes.beginRecovery();
            await journal.markRecoveryReady(recoveryToken);
        });
        void fencedAdapter.ready().catch((error) => {
            if (!this.unloading) new Notice(actionableError(error));
        });
        this.vaultToolObserver = observePluginToolEvents(client.reducer, fencedAdapter, (error) => {
            if (!this.unloading) new Notice(actionableError(error));
        });
        this.vaultToolClient = client;
        this.vaultToolFence = fencedAdapter;
        this.vaultChanges = changes;
        this.researchBrowser = researchBrowser;
    }

    private authorizeVaultChange(
        proposal: VaultChangeAuthorizationProposal,
    ): Promise<VaultChangeAuthorizationDecision> {
        return new VaultChangeReviewModal(this, proposal).openAndWait();
    }

    private disposeVaultToolAdapter(): Promise<void> {
        const observer = this.vaultToolObserver;
        this.vaultToolObserver = null;
        const fence = this.vaultToolFence;
        this.vaultToolFence = null;
        this.vaultToolClient = null;
        this.vaultChanges = null;
        const researchBrowser = this.researchBrowser;
        this.researchBrowser = null;
        const currentRetirement = (observer?.dispose() ?? Promise.resolve())
            .catch(() => undefined)
            .then(() => fence?.drain())
            .then(() => researchBrowser?.close().catch(() => undefined));
        const previousRetirement = this.vaultToolRetirement ?? Promise.resolve();
        this.vaultToolRetirement = Promise.all([
            previousRetirement.catch(() => undefined),
            currentRetirement,
        ]).then(() => undefined);
        return this.vaultToolRetirement;
    }

    private async startRuntime(): Promise<void> {
        await (this.runtime as RuntimeBootstrap).start();
        if (this.unloading || this.runtimeExplicitlyStopped) {
            await this.runtime?.stop().catch(() => undefined);
            return;
        }
        const settings = snapshotLocalSettings(this.settings);
        await this.enqueueRuntimeSettingsApply(settings, this.settingsGeneration)
            .catch((error) => new Notice(actionableError(error)));
        if (this.unloading || this.runtimeExplicitlyStopped) return;
    }

    private restartRuntimeIfIdle(): Promise<void> {
        if (!this.runtimeRestartPending || this.runtimeExplicitlyStopped || this.unloading) return Promise.resolve();
        if (this.runtimeRestartOperation) return this.runtimeRestartOperation;
        const operation = this.performRuntimeRestartIfIdle().finally(() => {
            if (this.runtimeRestartOperation === operation) this.runtimeRestartOperation = null;
        });
        this.runtimeRestartOperation = operation;
        return operation;
    }

    private async performRuntimeRestartIfIdle(): Promise<void> {
        if (this.unloading || this.runtimeExplicitlyStopped) return;
        if (!this.runtimeRestartPending || !this.runtime || this.runtime.snapshot.state !== "ready") {
            this.scheduleRuntimeRestartCheck(2_000);
            return;
        }
        const status = requireJsonObject(await this.runtime.harness.request("runtime/status", {}));
        if (this.unloading || this.runtimeExplicitlyStopped) return;
        const activeRunIds = requireRunIds(status.activeRunIds);
        // A Session create or turn/start can be in flight before the Worker has an
        // active Run ID.  Do not dispose its store/connection in that ACK window.
        const clientOperationActive = this.chatStore?.snapshot.busy === true;
        if (activeRunIds.length > 0 || clientOperationActive) {
            if (!this.runtimeRestartNoticeShown) {
                this.runtimeRestartNoticeShown = true;
                new Notice("设置已保存；当前对话结束后将重启本地 Runtime 使其生效");
            }
            this.scheduleRuntimeRestartCheck(1_000);
            return;
        }
        if (this.runtimeRestartTimer) clearTimeout(this.runtimeRestartTimer);
        this.runtimeRestartTimer = null;
        await this.chatStore?.dispose().catch(() => undefined);
        this.chatStore = null;
        this.chatClient = null;
        await this.disposeVaultToolAdapter();
        if (this.unloading || this.runtimeExplicitlyStopped) return;
        await this.runtime.stop();
        if (this.unloading || this.runtimeExplicitlyStopped) return;
        await this.runtime.start();
        if (this.unloading || this.runtimeExplicitlyStopped) {
            await this.runtime.stop().catch(() => undefined);
            return;
        }
        const refreshed = requireJsonObject(await this.runtime.harness.request(
            "config/get",
            { scope: "workspace", sessionId: null },
        ));
        if (refreshed.restartPending === true) {
            throw new Error("Runtime 重启后设置仍标记为待生效；请运行诊断");
        }
        this.runtimeRestartPending = false;
        this.runtimeRestartNoticeShown = false;
        new Notice("本地 Runtime 已安全重启，设置现已生效");
    }

    private scheduleRuntimeRestartCheck(delayMs: number): void {
        if (!this.runtimeRestartPending || this.runtimeRestartTimer || this.runtimeExplicitlyStopped || this.unloading) return;
        this.runtimeRestartTimer = setTimeout(() => {
            this.runtimeRestartTimer = null;
            void this.restartRuntimeIfIdle().catch((error) => new Notice(actionableError(error)));
        }, delayMs);
    }

    private async ensureReady(): Promise<void> {
        if (!this.runtime) throw new Error("OfferAgent Runtime 尚未初始化");
        if (this.runtime.snapshot.state !== "ready") {
            await (this.runtimeStart ?? this.beginRuntimeStart());
        }
        if (this.runtime.snapshot.state !== "ready") throw new Error("OfferAgent Runtime 尚未就绪");
    }

    private beginRuntimeStart(): Promise<void> {
        this.runtimeExplicitlyStopped = false;
        const operation = this.startRuntime();
        this.runtimeStart = operation;
        void operation.finally(() => {
            if (this.runtimeStart === operation) this.runtimeStart = null;
        }).catch(() => undefined);
        return operation;
    }

    private async loadLocalData(): Promise<void> {
        const raw = await this.loadData();
        const value = isObject(raw) ? raw : {};
        const settingsSource = isObject(value.settings) ? value.settings : raw;
        this.settings = parseLocalSettings(settingsSource);
        this.data = {
            schemaVersion: 2,
            settings: this.settings,
            chatTabs: parsePersistedTabs(value.chatTabs),
        };
        // Rewrites only the closed local schema, intentionally dropping every legacy URL/key/sync field.
        await this.persistLocalData();
    }

    private persistLocalData(): Promise<void> {
        const snapshot: LocalPluginData = {
            schemaVersion: 2,
            settings: snapshotLocalSettings(this.data.settings),
            chatTabs: parsePersistedTabs(this.data.chatTabs),
        };
        return this.localDataWrites.run(() => this.saveData(snapshot));
    }

    private async activateChat(): Promise<void> {
        let leaf = this.app.workspace.getLeavesOfType(LOCAL_CHAT_VIEW)[0];
        if (!leaf) {
            leaf = this.app.workspace.getRightLeaf(false) as WorkspaceLeaf;
            await leaf.setViewState({ type: LOCAL_CHAT_VIEW, active: true });
        }
        this.app.workspace.revealLeaf(leaf);
    }

    private async newChat(): Promise<void> {
        await this.activateChat();
        await (await this.ensureChatStore()).createTab();
    }
}

class VaultChangeReviewModal extends Modal {
    private resolveDecision: ((decision: VaultChangeAuthorizationDecision) => void) | null = null;

    constructor(
        private readonly plugin: OfferAgentPlugin,
        private readonly proposal: VaultChangeAuthorizationProposal,
    ) {
        super(plugin.app);
    }

    openAndWait(): Promise<VaultChangeAuthorizationDecision> {
        if (this.resolveDecision !== null) throw new Error("Vault Change Review is already open");
        return new Promise((resolveDecision) => {
            this.resolveDecision = resolveDecision;
            this.open();
        });
    }

    onOpen(): void {
        this.modalEl.addClass("offeragent-vault-review-modal");
        this.setTitle("确认完整 Vault Change Batch");
        const flags = [
            this.proposal.changeKind === "interview_submission" ? "Interview Submission（始终需要确认）" : "",
            this.proposal.controlFiles ? "包含控制文件" : "",
            this.proposal.memoryDelete ? "包含 Planning Memory 删除" : "",
        ].filter(Boolean).join("；");
        this.contentEl.createEl("p", {
            text: "以下是本批次的完整最终预览。只能整体应用或整体拒绝；关闭窗口等同于拒绝。",
        });
        const identity = this.contentEl.createEl("dl", { cls: "offeragent-vault-review-identity" });
        this.addIdentity(identity, "任务", this.proposal.task);
        this.addIdentity(identity, "批次", this.proposal.batchId);
        this.addIdentity(identity, "Review hash", this.proposal.reviewHash);
        this.addIdentity(identity, "参数绑定", this.proposal.argsHash);
        if (flags) this.addIdentity(identity, "安全提示", flags);

        const scroll = this.contentEl.createDiv({ cls: "offeragent-vault-review-scroll" });
        if (this.proposal.interviewSubmission !== null) {
            const source = scroll.createEl("section", { cls: "offeragent-vault-review-source" });
            source.createEl("h3", { text: "Interview Submission 来源" });
            const { reviewItems, ...sourceReceipt } = this.proposal.interviewSubmission;
            source.createEl("pre", {
                text: JSON.stringify(sourceReceipt, null, 2),
            });
            const reviewPlan = scroll.createEl("section", { cls: "offeragent-vault-review-plan" });
            reviewPlan.createEl("h3", { text: "结构化审阅结果" });
            for (const item of reviewItems) {
                const row = reviewPlan.createDiv({ cls: "offeragent-vault-review-plan-item" });
                row.createEl("code", { text: item.path });
                const badges = row.createDiv({ cls: "offeragent-vault-review-badges" });
                const [identityBadge, mutationBadge] = interviewReviewBadges(item);
                badges.createSpan({
                    text: identityBadge,
                    cls: "offeragent-vault-review-badge offeragent-vault-review-badge-identity",
                });
                badges.createSpan({
                    text: mutationBadge,
                    cls: `offeragent-vault-review-badge offeragent-vault-review-badge-${item.mutation}`,
                });
            }
        }
        if (this.proposal.sourceBindings.length > 0) {
            const sources = scroll.createEl("section", { cls: "offeragent-vault-review-source" });
            sources.createEl("h3", { text: "精确来源绑定" });
            sources.createEl("pre", {
                text: this.proposal.sourceBindings.map((source) =>
                    `${source.path}\n  version: ${source.expectedModifiedVersion}\n  hash: ${source.expectedContentHash}`,
                ).join("\n\n"),
            });
        }

        const categoryLabel = {
            experience: "Experience",
            question: "Question",
            index: "Index",
            other: "Other",
        } as const;
        for (const [index, target] of this.proposal.reviewTargets.entries()) {
            const categorized = this.proposal.categorizedTargets.find((candidate) => candidate.path === target.path);
            const category = categorized === undefined ? "Other" : categoryLabel[categorized.category];
            const section = scroll.createEl("section", { cls: "offeragent-vault-review-target" });
            section.createEl("h3", {
                text: `${index + 1}. ${category} · ${target.operation}: ${target.path}`,
            });
            section.createEl("p", {
                text: `before ${target.beforeModifiedVersion} · ${target.beforeContentHash} → ${target.afterContentHash}`,
                cls: "setting-item-description",
            });
            this.addContent(section, "Before（完整）", target.beforeContent);
            this.addContent(section, "After（完整）", target.afterContent);
        }

        const actions = this.contentEl.createDiv({ cls: "offeragent-vault-review-actions" });
        const reject = actions.createEl("button", { text: "拒绝整个批次" });
        reject.addEventListener("click", () => this.finish("reject"));
        const accept = actions.createEl("button", { text: "应用全部变更", cls: "mod-cta" });
        accept.addEventListener("click", () => this.finish("accept"));
    }

    onClose(): void {
        this.contentEl.empty();
        this.resolve("reject");
    }

    private addIdentity(list: HTMLElement, label: string, value: string): void {
        list.createEl("dt", { text: label });
        list.createEl("dd", { text: value });
    }

    private addContent(parent: HTMLElement, label: string, content: string | null): void {
        const details = parent.createEl("details");
        details.open = true;
        details.createEl("summary", { text: label });
        details.createEl("pre", { text: content ?? "（不存在）" });
    }

    private finish(decision: VaultChangeAuthorizationDecision["decision"]): void {
        this.resolve(decision);
        this.close();
    }

    private resolve(decision: VaultChangeAuthorizationDecision["decision"]): void {
        const resolveDecision = this.resolveDecision;
        if (resolveDecision === null) return;
        this.resolveDecision = null;
        resolveDecision({ decision, reviewHash: this.proposal.reviewHash });
    }
}

class DiagnosticsModal extends Modal {
    constructor(private readonly plugin: OfferAgentPlugin, private readonly snapshot: JsonObject) {
        super(plugin.app);
    }

    onOpen(): void {
        this.setTitle("OfferAgent 本地诊断");
        const identity = this.plugin.runtimeSnapshot();
        this.contentEl.createEl("p", {
            text: `状态 ${identity.state} · Runtime ${identity.runtimeVersion ?? "未知"} · Worker PID ${identity.workerPid ?? "未知"}`,
        });
        const runtime = isObject(this.snapshot.runtime) ? this.snapshot.runtime : {};
        const list = this.contentEl.createEl("dl", { cls: "offeragent-diagnostics-list" });
        for (const [key, value] of Object.entries(runtime)) {
            list.createEl("dt", { text: key });
            list.createEl("dd", { text: typeof value === "string" ? value : JSON.stringify(value) });
        }
        const errors = Array.isArray(this.snapshot.recentErrors) ? this.snapshot.recentErrors : [];
        this.contentEl.createEl("p", { text: `最近脱敏错误：${errors.length}` });
        this.contentEl.createEl("p", { text: "遥测关闭；诊断不会自动上传。" });
    }
}

function desktopVaultRoot(plugin: Plugin): string {
    if (!(plugin.app.vault.adapter instanceof FileSystemAdapter)) throw new Error("OfferAgent 仅支持 Windows Desktop Vault");
    const root = resolve(plugin.app.vault.adapter.getBasePath());
    if (!/^[A-Za-z]:\\/.test(root) || root.includes("\0")) throw new Error("Vault 必须位于本机 Windows 路径");
    return root;
}

function pluginInstallDirectory(plugin: Plugin, vaultRoot: string): string {
    const configured = plugin.manifest.dir ?? `.obsidian/plugins/${plugin.manifest.id}`;
    if (configured.includes("\0") || configured.split(/[\\/]/).some((part) => part === "..")) {
        throw new Error("插件安装目录无效");
    }
    const candidate = resolve(vaultRoot, configured);
    const nested = relative(vaultRoot, candidate);
    if (nested === "" || nested.startsWith("..") || isAbsolute(nested)) throw new Error("插件安装目录逃逸 Vault");
    return candidate;
}

function safeVaultRelativePath(path: string): boolean {
    return path.length > 0 && path.length <= 1024 && !path.startsWith(".") && !path.includes("\\") &&
        !path.includes("\0") && path.split("/").every((part) => part !== "" && part !== "." && part !== "..");
}

function opaqueId(prefix: string): string {
    return `${prefix}${randomBytes(16).toString("hex")}`;
}

function actionableError(error: unknown): string {
    const message = error instanceof Error ? error.message : "未知错误";
    return `OfferAgent 本地 Runtime：${message}`.slice(0, 4096);
}

function isObject(value: unknown): value is Record<string, unknown> {
    return value !== null && typeof value === "object" && !Array.isArray(value);
}

function requireText(value: JsonValue | undefined, label: string): string {
    if (typeof value !== "string" || !value || value.length > 4096) throw new Error(`${label} 无效`);
    return value;
}

function requireInteger(value: JsonValue | undefined, label: string): number {
    if (typeof value !== "number" || !Number.isSafeInteger(value) || value < 0) throw new Error(`${label} 无效`);
    return value;
}

function requireBoundedTextArray(value: JsonValue | undefined, label: string, maximum: number): readonly string[] {
    if (!Array.isArray(value) || value.length === 0 || value.length > maximum ||
        value.some((item) => typeof item !== "string" || !/^[a-z][a-z0-9_-]{0,63}$/u.test(item))) {
        throw new Error(`${label} 无效`);
    }
    const result = value as string[];
    if (new Set(result).size !== result.length) throw new Error(`${label} 无效`);
    return result;
}

function requireRunIds(value: JsonValue | undefined): readonly string[] {
    if (value === undefined) return [];
    if (!Array.isArray(value) || value.length > 1_024) {
        throw new Error("Runtime activeRunIds 无效");
    }
    const runIds: string[] = [];
    for (const item of value) {
        if (typeof item !== "string" || item.length > 128 || !/^run_[A-Za-z0-9][A-Za-z0-9_-]*$/.test(item)) {
            throw new Error("Runtime activeRunIds 无效");
        }
        runIds.push(item);
    }
    return runIds;
}

function requireArtifactContent(value: JsonValue | undefined): string {
    if (typeof value !== "string" || value.length > 1_398_104) throw new Error("Artifact content 无效");
    return value;
}
