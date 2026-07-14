import { randomBytes } from "node:crypto";
import { Buffer } from "node:buffer";
import { isAbsolute, join, relative, resolve } from "node:path";

import {
    FileSystemAdapter,
    Modal,
    Notice,
    Plugin,
    TAbstractFile,
    WorkspaceLeaf,
} from "obsidian";

import { ClientInvocationJournal } from "./local/client_invocation_journal";
import { ObsidianClientToolBridge } from "./local/client_tool_bridge";
import { LocalChatView, LOCAL_CHAT_VIEW } from "./local/chat_view";
import type { ExtensionCommandMethod } from "./local/extension_settings";
import { ObsidianContextBridge } from "./local/obsidian_context";
import {
    DEFAULT_LOCAL_SETTINGS,
    HeadlessVaultWriteStatusView,
    LocalOfferAgentSettings,
    LocalOfferAgentSettingTab,
    SerializedOperationQueue,
    modelCredentialProviderId,
    modelHealthMessage,
    modelRuntimePatch,
    parseHeadlessVaultWriteStatus,
    parseLocalSettings,
    snapshotLocalSettings,
    usesProviderSecretStore,
    usesUnconfiguredCompatibleFallback,
} from "./local/settings";
import { canBackgroundStartRuntime, RuntimeBootstrap } from "./runtime/bootstrap";
import { ChatStore, PersistedChatTabs, parsePersistedTabs } from "./runtime/chat_store";
import { RELEASE_PUBLIC_KEYS } from "./runtime/generated_release_keyring";
import {
    ClientReverseHandlers,
    HarnessClient,
    REQUIRED_RUNTIME_CAPABILITIES,
} from "./runtime/harness_client";
import type { RuntimePrivilegeApprovalRequest } from "./runtime/installer";
import { createRuntimeInstaller } from "./runtime/installer_mode";
import { JsonObject, JsonValue, requireJsonObject } from "./runtime/json_rpc";
import { HostDiscoveryLoader, NamedPipeClient, requestHostStop } from "./runtime/named_pipe";
import {
    PROTOCOL_SCHEMA_HASH,
    PROTOCOL_VERSION,
} from "./runtime/generated_protocol_identity";
import type {
    ClientApprovalPresentParams,
    ClientApprovalPresentResult,
    ClientContextGetResult,
    ClientContextSnapshot,
    ClientToolCancelResult,
    ClientToolCommitObserveResult,
    ClientToolInvokeResult,
    ClientToolLookupResult,
    ClientToolPreviewResult,
    ProtocolCommandParams,
    ProtocolCommandResult,
    SecretsPutParams,
} from "./runtime/generated_protocol";
import { loadOrCreatePortableWorkspaceIdentity } from "./runtime/workspace_identity";

interface LocalPluginData {
    readonly schemaVersion: 2;
    readonly settings: LocalOfferAgentSettings;
    readonly chatTabs: PersistedChatTabs | null;
}

const EXTENSION_ADMIN_METHODS: ReadonlySet<ExtensionCommandMethod> = new Set<ExtensionCommandMethod>([
    "skills/list",
    "skills/status",
    "skills/rescan",
    "skills/confirm-trust",
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
    private contextBridge: ObsidianContextBridge | null = null;
    private clientBridge: ObsidianClientToolBridge | null = null;
    private chatStore: ChatStore | null = null;
    private chatClient: HarnessClient | null = null;
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
        this.contextBridge = new ObsidianContextBridge(this.app);
        const portable = await loadOrCreatePortableWorkspaceIdentity(this.vaultRoot);
        this.workspaceId = portable.portableWorkspaceId;
        this.runtime = this.createRuntime();

        this.registerView(LOCAL_CHAT_VIEW, (leaf) => new LocalChatView(leaf, this));
        this.addRibbonIcon("bot", "打开 OfferAgent", () => void this.activateChat());
        this.addCommand({ id: "open-local-chat", name: "打开本地聊天", callback: () => void this.activateChat() });
        this.addCommand({ id: "new-local-chat", name: "新建本地会话", callback: () => void this.newChat() });
        this.addCommand({ id: "open-local-web", name: "打开本地 Web 界面", callback: () => void this.openLocalWeb() });
        this.addCommand({ id: "runtime-diagnostics", name: "查看本地 Runtime 诊断", callback: () => void this.openDiagnostics() });
        this.addCommand({
            id: "stop-all-local-runtime",
            name: "一键停止本机所有 OfferAgent Runtime",
            callback: () => void this.stopAllLocalRuntime(),
        });
        this.addSettingTab(new LocalOfferAgentSettingTab(this.app, this));
        this.registerContextEvents();

        void this.beginRuntimeStart().catch((error) => new Notice(actionableError(error)));
    }

    async onunload(): Promise<void> {
        this.unloading = true;
        if (this.runtimeRestartTimer) clearTimeout(this.runtimeRestartTimer);
        this.runtimeRestartTimer = null;
        const pendingStart = this.runtimeStart;
        const pendingRestart = this.runtimeRestartOperation;
        await this.chatStore?.dispose().catch(() => undefined);
        this.chatStore = null;
        this.chatClient = null;
        // Plugin reload/disable detaches only. Host owns idle shutdown and active Runs continue.
        await this.runtime?.stop({ shutdownWorker: false }).catch(() => undefined);
        // stop() aborts an installer, reconnect sleep, or configuration restart.
        // Await those operations only after the abort boundary, otherwise unload
        // can wait forever for the very operation it needs to cancel.
        await Promise.all([
            pendingStart?.catch(() => undefined),
            pendingRestart?.catch(() => undefined),
        ]);
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

    async captureClientContext(): Promise<ClientContextSnapshot> {
        return await this.requireContext().capture() as unknown as ClientContextSnapshot;
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
                // Settings changed across an awaited Pipe request.  Coalesce to one
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
        const credential = usesUnconfiguredCompatibleFallback(settings) || !usesProviderSecretStore(settings)
            ? null
            : await this.providerCredential(settings);
        if (generation !== this.settingsGeneration) return false;
        const patch: JsonObject = {
            model: modelRuntimePatch(settings, credential?.handle ?? null),
            policy: {
                read_only: settings.permissionMode === "read-only" || settings.permissionMode === "plan",
                workspace_trusted: settings.workspaceTrusted,
                approve_vault_writes: !settings.autoApproveVaultWrites,
            },
            execution: {
                shell_enabled: settings.shellEnabled,
                subagents_enabled: settings.subagentsEnabled,
                max_subagents_per_vault: settings.subagentsEnabled ? 8 : 0,
                max_subagent_depth: settings.subagentsEnabled ? 3 : 0,
            },
            extensibility: {
                skills_enabled: settings.enabledSkills.length > 0,
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
        if (result.status === "rejected") throw new Error("Runtime 拒绝了设置；请查看字段错误与模型端点配置");
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

    async saveProviderCredential(secret: string): Promise<void> {
        await this.ensureReady();
        if (!secret || secret.includes("\0") || Buffer.byteLength(secret, "utf8") > 1_048_576) {
            throw new Error("Provider 凭据为空或超过安全输入上限");
        }
        const settings = snapshotLocalSettings(this.settings);
        if (!usesProviderSecretStore(settings)) {
            throw new Error("Codex 订阅使用本机 Codex 登录，不接受 Provider SecretStore 凭据");
        }
        const generation = this.settingsGeneration;
        const providerId = modelCredentialProviderId(settings);
        const existing = await this.providerCredential(settings);
        const params: SecretsPutParams & { secret: string } = {
            providerId,
            kind: "model-provider",
            secret,
            handle: existing?.handle ?? null,
            expectedVersion: existing?.version ?? null,
        };
        try {
            if (generation !== this.settingsGeneration) {
                throw new Error("模型设置已变化；凭据未保存，请确认当前 Provider 后重试");
            }
            const result = requireJsonObject(
                await (this.runtime as RuntimeBootstrap).harness.request("secrets/put", params),
            );
            this.requireProviderCredentialMetadata(requireJsonObject(result.secret), providerId);
        } finally {
            params.secret = "";
            secret = "";
        }
        if (generation !== this.settingsGeneration) {
            throw new Error("凭据已绑定原模型端点安全保存，但当前设置已变化，因此未自动应用");
        }
        await this.enqueueRuntimeSettingsApply(settings, generation);
        new Notice("Provider 凭据已安全保存并应用；有效性需通过“应用并检查”验证");
    }

    async deleteProviderCredential(): Promise<void> {
        await this.ensureReady();
        const settings = snapshotLocalSettings(this.settings);
        if (!usesProviderSecretStore(settings)) {
            throw new Error("Codex 订阅使用本机 Codex 登录，没有可删除的 Provider SecretStore 凭据");
        }
        const generation = this.settingsGeneration;
        const existing = await this.providerCredential(settings);
        if (!existing) return;
        if (generation !== this.settingsGeneration) {
            throw new Error("模型设置已变化；凭据未删除，请确认当前 Provider 后重试");
        }
        await (this.runtime as RuntimeBootstrap).harness.request("secrets/delete", {
            handle: existing.handle,
            expectedVersion: existing.version,
        });
        if (generation !== this.settingsGeneration) {
            throw new Error("原模型端点凭据已删除，但当前设置已变化，因此未自动应用");
        }
        await this.enqueueRuntimeSettingsApply(settings, generation);
        new Notice("Provider 凭据已从 Windows SecretStore 删除");
    }

    async checkModelHealth(): Promise<string> {
        await this.ensureReady();
        const settings = snapshotLocalSettings(this.settings);
        const result = requireJsonObject(await (this.runtime as RuntimeBootstrap).harness.request("models/health", {
            provider: settings.provider,
            model: settings.model,
            deadline: new Date(Date.now() + 30_000).toISOString(),
            clientRequestId: opaqueId("req_model_health_"),
        }));
        const status = requireText(result.status, "Model health status");
        const error = isObject(result.error) ? result.error : null;
        const details = error && isObject(error.details) ? error.details : null;
        const reason = details && typeof details.reason === "string" ? details.reason : null;
        return modelHealthMessage(settings, status, reason);
    }

    async loadHeadlessVaultWriteStatus(signal?: AbortSignal): Promise<HeadlessVaultWriteStatusView> {
        await this.ensureReady();
        const result = await (this.runtime as RuntimeBootstrap).harness.request(
            "vault/headless/status",
            {},
            { signal },
        );
        return parseHeadlessVaultWriteStatus(result);
    }

    async revokeHeadlessVaultWrite(
        status: HeadlessVaultWriteStatusView,
        signal?: AbortSignal,
    ): Promise<HeadlessVaultWriteStatusView> {
        if (!status.canRevoke || status.approvalId === null || status.revision < 1) {
            throw new Error("当前没有可撤销的 Web headless Vault 写入授权");
        }
        await this.ensureReady();
        const result = await (this.runtime as RuntimeBootstrap).harness.request(
            "vault/headless/revoke",
            {
                clientRequestId: opaqueId("req_headless_revoke_"),
                approvalId: status.approvalId,
                expectedRevision: status.revision,
                reason: "用户从 Obsidian 设置页显式撤销 Web headless Vault 写入授权",
            },
            { signal },
        );
        return parseHeadlessVaultWriteStatus(result);
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

    selectedSkillNames(): readonly string[] {
        return [...this.settings.enabledSkills];
    }

    async setSkillSelected(name: string, selected: boolean): Promise<void> {
        if (!/^[a-z][a-z0-9-]{0,63}$/.test(name)) throw new Error("Skill 名称无效");
        const previous = [...this.settings.enabledSkills];
        const next = new Set(previous);
        if (selected) next.add(name);
        else next.delete(name);
        this.settings.enabledSkills = [...next].sort();
        try {
            await this.saveLocalSettings();
            await this.applyRuntimeSettings();
        } catch (error) {
            this.settings.enabledSkills = previous;
            await this.saveLocalSettings().catch(() => undefined);
            throw error;
        }
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

    async openLocalWeb(): Promise<void> {
        await this.ensureReady();
        const result = requireJsonObject(await (this.runtime as RuntimeBootstrap).harness.request("web/launch", {}));
        const url = requireText(result.url, "Web launch URL");
        const parsed = new URL(url);
        if (parsed.protocol !== "http:" || !["127.0.0.1", "[::1]"].includes(parsed.hostname) ||
            !parsed.hash || parsed.username || parsed.password) {
            throw new Error("Runtime 返回了不安全的本地 Web 地址");
        }
        window.open(url, "_blank", "noopener,noreferrer");
    }

    private async stopAllLocalRuntime(): Promise<void> {
        const confirmed = window.confirm(
            "这会停止当前 Windows 用户的所有 OfferAgent Worker，并取消所有 Vault 中正在运行的任务。是否继续？",
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
        await runtime.stop({ shutdownWorker: false });
        await Promise.all([
            pendingStart?.catch(() => undefined),
            pendingRestart?.catch(() => undefined),
        ]);
        const installed = runtime.installedRuntime;
        if (!installed) {
            new Notice("OfferAgent Runtime 当前未运行");
            return;
        }
        try {
            const result = await requestHostStop(installed.hostExecutable, {
                beforeLaunch: installed.beforeHostLaunch,
            });
            new Notice(result.status === "already_stopped" ? "OfferAgent Runtime 已停止" : "已停止所有 OfferAgent Runtime");
        } catch (error) {
            new Notice(`停止失败：${actionableError(error)}`);
        }
    }

    private createRuntime(): RuntimeBootstrap {
        const pluginDirectory = pluginInstallDirectory(this, this.vaultRoot);
        const installer = createRuntimeInstaller({
            pluginDirectory,
            vaultRoot: this.vaultRoot,
            pluginVersion: this.manifest.version,
            protocolVersion: PROTOCOL_VERSION,
            schemaHash: PROTOCOL_SCHEMA_HASH,
            releasePublicKeys: RELEASE_PUBLIC_KEYS,
            ownerId: `workspace:${this.workspaceId}`,
            legacyOwnerId: `obsidian-${process.pid}`,
            approvePrivilegeExpansion: (request, signal) => this.confirmRuntimePrivileges(request, signal),
        });
        const handlers: ClientReverseHandlers = {
            contextGet: async (params, signal) => await this.requireContext().handleContextGet(
                params as unknown as JsonObject,
                signal,
            ) as unknown as ClientContextGetResult,
            toolPreview: async (params, signal) => await this.requireClientBridge().preview(
                params as unknown as JsonObject,
                signal,
            ) as unknown as ClientToolPreviewResult,
            toolCommitObserve: async (params, signal) => await this.requireClientBridge().observeCommit(
                params as unknown as JsonObject,
                signal,
            ) as unknown as ClientToolCommitObserveResult,
            toolInvoke: async (params, signal) => await this.requireClientBridge().invoke(
                params as unknown as JsonObject,
                signal,
            ) as unknown as ClientToolInvokeResult,
            toolLookup: async (params, signal) => await this.requireClientBridge().lookup(
                params as unknown as JsonObject,
                signal,
            ) as unknown as ClientToolLookupResult,
            toolCancel: async (params, signal) => await this.requireClientBridge().cancel(
                params as unknown as JsonObject,
                signal,
            ) as unknown as ClientToolCancelResult,
            approvalPresent: (params, signal) => this.presentApproval(params, signal),
        };
        return new RuntimeBootstrap(installer, {
            create: (installed, onDisconnected) => new HarnessClient(
                new NamedPipeClient(new HostDiscoveryLoader(installed.hostExecutable, this.vaultRoot, {
                    beforeLaunch: installed.beforeHostLaunch,
                    timeoutMs: installed.hostDiscoveryTimeoutMs,
                })),
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
                handlers,
                { onDisconnected },
            ),
        });
    }

    private confirmRuntimePrivileges(request: RuntimePrivilegeApprovalRequest, signal: AbortSignal): Promise<boolean> {
        return new Promise<boolean>((resolvePromise) => {
            if (signal.aborted) {
                resolvePromise(false);
                return;
            }
            let modal: RuntimePrivilegeApprovalModal;
            const onAbort = () => modal.cancelForRuntimeShutdown();
            const settle = (approved: boolean) => {
                signal.removeEventListener("abort", onAbort);
                resolvePromise(approved);
            };
            modal = new RuntimePrivilegeApprovalModal(this, request, settle);
            signal.addEventListener("abort", onAbort, { once: true });
            if (signal.aborted) {
                onAbort();
                return;
            }
            modal.open();
        });
    }

    private async startRuntime(): Promise<void> {
        const identity = await (this.runtime as RuntimeBootstrap).start();
        if (this.unloading || this.runtimeExplicitlyStopped) {
            await this.runtime?.stop({ shutdownWorker: false }).catch(() => undefined);
            return;
        }
        const localAppData = process.env.LOCALAPPDATA;
        if (!localAppData) throw new Error("Windows LOCALAPPDATA 不可用");
        const journal = new ClientInvocationJournal(
            join(localAppData, "OfferAgent", "workspaces", identity.workspaceInstanceId, "client-invocations.json"),
            identity.workspaceInstanceId,
        );
        this.clientBridge = new ObsidianClientToolBridge(
            this.app,
            this.requireContext(),
            journal,
            {
                present: async () => {
                    new Notice("OfferAgent 有一项操作等待审批");
                    await this.activateChat();
                    return true;
                },
            },
        );
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
        if (this.unloading || this.runtimeExplicitlyStopped) return;
        await this.runtime.stop({ shutdownWorker: true });
        if (this.unloading || this.runtimeExplicitlyStopped) return;
        await this.runtime.start();
        if (this.unloading || this.runtimeExplicitlyStopped) {
            await this.runtime.stop({ shutdownWorker: false }).catch(() => undefined);
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

    private async providerCredential(
        settings: LocalOfferAgentSettings,
    ): Promise<{ handle: string; version: number } | null> {
        if (!usesProviderSecretStore(settings)) return null;
        const providerId = modelCredentialProviderId(settings);
        const result = requireJsonObject(await (this.runtime as RuntimeBootstrap).harness.request("secrets/list", {
            kind: "model-provider",
            providerId,
        }));
        if (!Array.isArray(result.secrets)) throw new Error("SecretStore 元数据响应无效");
        if (result.secrets.length > 1) throw new Error("同一 Provider 存在重复凭据；请在诊断中清理");
        if (result.secrets.length === 0) return null;
        const metadata = requireJsonObject(result.secrets[0]);
        return this.requireProviderCredentialMetadata(metadata, providerId);
    }

    private requireProviderCredentialMetadata(
        metadata: JsonObject,
        providerId: string,
    ): { handle: string; version: number } {
        if (metadata.kind !== "model-provider" || metadata.providerId !== providerId) {
            throw new Error("SecretStore 返回了与当前模型端点不匹配的凭据元数据");
        }
        const handle = requireText(metadata.handle, "Secret handle");
        if (!/^secret:v1:[0-9a-f]{32}$/.test(handle)) throw new Error("Secret handle 无效");
        return { handle, version: requireInteger(metadata.version, "Secret version") };
    }

    private persistLocalData(): Promise<void> {
        const snapshot: LocalPluginData = {
            schemaVersion: 2,
            settings: snapshotLocalSettings(this.data.settings),
            chatTabs: parsePersistedTabs(this.data.chatTabs),
        };
        return this.localDataWrites.run(() => this.saveData(snapshot));
    }

    private registerContextEvents(): void {
        const noteFileChanged = (file: TAbstractFile): void => {
            if (!safeVaultRelativePath(file.path)) return;
            this.requireContext().noteFileChanged(file.path);
        };
        this.registerEvent(this.app.vault.on("create", noteFileChanged));
        this.registerEvent(this.app.vault.on("modify", noteFileChanged));
        this.registerEvent(this.app.vault.on("delete", noteFileChanged));
        this.registerEvent(this.app.vault.on("rename", (file, oldPath) => {
            noteFileChanged(file);
            if (safeVaultRelativePath(oldPath)) this.requireContext().noteFileChanged(oldPath);
        }));
        this.registerEvent(this.app.workspace.on("editor-change", () => this.requireContext().noteEditorChanged()));
        this.registerEvent(this.app.workspace.on("active-leaf-change", () => this.requireContext().noteEditorChanged()));
        this.registerEvent(this.app.metadataCache.on("changed", (file) => {
            this.requireContext().noteMetadataChanged(file);
        }));
        this.registerEvent(this.app.metadataCache.on("resolved", () => this.requireContext().noteMetadataChanged()));
    }

    private async presentApproval(
        params: ClientApprovalPresentParams,
        signal: AbortSignal,
    ): Promise<ClientApprovalPresentResult> {
        signal.throwIfAborted();
        return await this.requireClientBridge().presentApproval(
            params as unknown as JsonObject,
            signal,
        ) as unknown as ClientApprovalPresentResult;
    }

    private requireContext(): ObsidianContextBridge {
        if (!this.contextBridge) throw new Error("Obsidian Context Bridge 尚未就绪");
        return this.contextBridge;
    }

    private requireClientBridge(): ObsidianClientToolBridge {
        if (!this.clientBridge) throw new Error("Obsidian Client Tool Bridge 尚未就绪");
        return this.clientBridge;
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

class RuntimePrivilegeApprovalModal extends Modal {
    private settled = false;

    constructor(
        private readonly plugin: OfferAgentPlugin,
        private readonly request: RuntimePrivilegeApprovalRequest,
        private readonly resolvePromise: (approved: boolean) => void,
    ) {
        super(plugin.app);
    }

    onOpen(): void {
        this.setTitle("OfferAgent Runtime 权限确认");
        this.contentEl.createEl("p", {
            text: this.request.currentRuntimeVersion === null
                ? `首次安装 Runtime ${this.request.candidateRuntimeVersion} 需要明确授予以下权限。`
                : `Runtime ${this.request.currentRuntimeVersion} → ${this.request.candidateRuntimeVersion} 包含新增或无法自动证明为收窄的权限。`,
        });
        const list = this.contentEl.createEl("ul", { cls: "offeragent-runtime-privilege-diff" });
        for (const line of this.request.summary.slice(0, 12)) list.createEl("li", { text: line.slice(0, 180) });
        this.contentEl.createEl("p", {
            text: `完整签名差异指纹：${this.request.diffHash}`,
            cls: "offeragent-runtime-privilege-hash",
        });
        this.contentEl.createEl("p", {
            text: "此确认仅对当前签名版本和上述完整差异有效，五分钟内一次性使用。",
        });
        const actions = this.contentEl.createDiv({ cls: "modal-button-container" });
        const cancel = actions.createEl("button", { text: "取消" });
        const approve = actions.createEl("button", { text: "确认授予并继续", cls: "mod-cta" });
        cancel.addEventListener("click", () => this.finish(false));
        approve.addEventListener("click", () => this.finish(true));
    }

    onClose(): void {
        this.contentEl.empty();
        if (!this.settled) {
            this.settled = true;
            this.resolvePromise(false);
        }
    }

    cancelForRuntimeShutdown(): void {
        this.finish(false);
    }

    private finish(approved: boolean): void {
        if (this.settled) return;
        this.settled = true;
        this.resolvePromise(approved);
        this.close();
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
