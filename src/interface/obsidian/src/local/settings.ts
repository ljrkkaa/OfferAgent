import { createHash } from "node:crypto";

import { App, Notice, Plugin, PluginSettingTab, Setting } from "obsidian";
import type { ToggleComponent } from "obsidian";

import { TurnRunConfig } from "../runtime/chat_store";
import { JsonObject } from "../runtime/json_rpc";
import { ExtensionSettingsPanel, ExtensionSettingsPanelHost } from "./extension_settings_panel";

export interface LocalOfferAgentSettings {
    schemaVersion: 2;
    provider: "deepseek" | "codex-subscription-experimental" | "codex" | "openai" | "openai-compatible" | "local";
    wireApi: "chat-completions" | "responses" | "ollama-chat";
    baseUrl: string;
    proxyUrl: string;
    approvedRemoteHttpsEndpoint: string | null;
    model: string;
    reasoningEffort: "minimal" | "low" | "medium" | "high" | "max";
    permissionMode: "read-only" | "normal" | "trusted-workspace" | "plan" | "bypass";
    workspaceTrusted: boolean;
    autoApproveVaultWrites: boolean;
    shellEnabled: boolean;
    subagentsEnabled: boolean;
    hooksEnabled: boolean;
    keepWorkerInBackground: boolean;
    telemetryEnabled: false;
}

export const DEFAULT_LOCAL_SETTINGS: LocalOfferAgentSettings = {
    schemaVersion: 2,
    provider: "deepseek",
    wireApi: "chat-completions",
    baseUrl: "",
    proxyUrl: "",
    approvedRemoteHttpsEndpoint: null,
    model: "deepseek-v4-flash",
    reasoningEffort: "medium",
    permissionMode: "normal",
    workspaceTrusted: false,
    autoApproveVaultWrites: false,
    shellEnabled: false,
    subagentsEnabled: false,
    hooksEnabled: false,
    keepWorkerInBackground: true,
    telemetryEnabled: false,
};

export const LOCAL_OLLAMA_BASE_URL = "http://127.0.0.1:11434/api";
const DEFAULT_RESPONSES_MODEL = "gpt-5.6-luna";
const UNCONFIGURED_COMPATIBLE_PROVIDER = "codex";
export const CODEX_SUBSCRIPTION_PROVIDER = "codex-subscription-experimental" as const;
export const DEEPSEEK_PROVIDER = "deepseek" as const;
export const DEEPSEEK_DEFAULT_MODEL = "deepseek-v4-flash" as const;

export interface SettingsHost extends ExtensionSettingsPanelHost {
    settings: LocalOfferAgentSettings;
    saveLocalSettings(): Promise<void>;
    applyRuntimeSettings(): Promise<void>;
    openDiagnostics(): Promise<void>;
    saveProviderCredential(secret: string): Promise<void>;
    deleteProviderCredential(): Promise<void>;
    checkModelHealth(): Promise<string>;
}

export class LocalOfferAgentSettingTab extends PluginSettingTab {
    private extensionPanel: ExtensionSettingsPanel | null = null;

    constructor(app: App, private readonly host: SettingsHost & Plugin) {
        super(app, host);
    }

    display(): void {
        const { containerEl } = this;
        this.extensionPanel?.dispose();
        containerEl.empty();
        containerEl.createEl("h2", { text: "OfferAgent 本地 Runtime" });
        containerEl.createEl("p", {
            text: "Agent、工具、Session、知识库与审批均在本机 Worker 中运行。此处不存在服务器 URL 或远程会话库。",
            cls: "offeragent-settings-description",
        });
        let remoteEndpointToggle: ToggleComponent | null = null;
        new Setting(containerEl)
            .setName("模型 Provider")
            .setDesc("仅模型推理会发送到所选 Provider；使用 local 可完全断网运行。")
            .addDropdown((dropdown) => dropdown
                .addOption(DEEPSEEK_PROVIDER, "DeepSeek API（API Key）")
                .addOption(CODEX_SUBSCRIPTION_PROVIDER, "Codex 订阅（实验，本机登录）")
                .addOption("codex", "Codex/OpenAI API（API Key）")
                .addOption("openai", "OpenAI API（API Key）")
                .addOption("openai-compatible", "OpenAI-compatible")
                .addOption("local", "本地模型")
                .setValue(this.host.settings.provider)
                .onChange(async (value) => {
                    const provider = value as LocalOfferAgentSettings["provider"];
                    this.host.settings.provider = provider;
                    this.host.settings.approvedRemoteHttpsEndpoint = null;
                    if (value === DEEPSEEK_PROVIDER) {
                        this.host.settings.wireApi = "chat-completions";
                        this.host.settings.baseUrl = "";
                        this.host.settings.proxyUrl = "";
                        this.host.settings.model = DEEPSEEK_DEFAULT_MODEL;
                    } else if (value === "local") {
                        this.host.settings.wireApi = "ollama-chat";
                        this.host.settings.baseUrl = LOCAL_OLLAMA_BASE_URL;
                    } else if (value === CODEX_SUBSCRIPTION_PROVIDER || value === "codex" || value === "openai") {
                        this.host.settings.wireApi = "responses";
                        this.host.settings.baseUrl = "";
                    } else {
                        this.host.settings.wireApi = "responses";
                        this.host.settings.baseUrl = "";
                    }
                    remoteEndpointToggle?.setValue(false).setDisabled(true);
                    if (provider === "openai-compatible") {
                        // Commit a valid, credential-free fallback immediately.  A compatible
                        // endpoint draft must never leave the previously configured endpoint live.
                        await this.persistAndApply();
                        this.display();
                        new Notice("请先填写并确认远程 HTTPS 模型端点，再应用设置");
                        return;
                    }
                    await this.persistAndApply();
                    this.display();
                }));
        new Setting(containerEl)
            .setName("模型协议")
            .setDesc("DeepSeek 固定使用 Chat Completions；本地 Ollama 可切换原生 Chat 或 Responses；其余 Provider 使用 Responses。")
            .addDropdown((dropdown) => dropdown
                .addOption("chat-completions", "Chat Completions")
                .addOption("responses", "Responses")
                .addOption("ollama-chat", "Ollama Chat")
                .setValue(this.host.settings.wireApi)
                .setDisabled(this.host.settings.provider !== "local")
                .onChange(async (value) => {
                    if (this.host.settings.provider !== "local") {
                        this.host.settings.wireApi = fixedWireApi(this.host.settings.provider);
                        await this.host.saveLocalSettings();
                        return;
                    }
                    this.host.settings.wireApi = value as LocalOfferAgentSettings["wireApi"];
                    await this.persistAndApply();
                }));
        new Setting(containerEl)
            .setName("模型端点")
            .setDesc("官方 DeepSeek/Codex/OpenAI 留空；本地端点必须是 127.0.0.1/::1，远程兼容端点必须使用 HTTPS。")
            .addText((text) => text
                .setPlaceholder(LOCAL_OLLAMA_BASE_URL)
                .setValue(this.host.settings.baseUrl)
                .setDisabled([DEEPSEEK_PROVIDER, CODEX_SUBSCRIPTION_PROVIDER, "codex", "openai"]
                    .includes(this.host.settings.provider))
                .onChange(async (value) => {
                    const wasApproved = isRemoteHttpsEndpointApproved(this.host.settings);
                    const endpoint = safeBaseUrl(value, this.host.settings.provider);
                    const changed = endpoint !== this.host.settings.baseUrl;
                    if (changed) {
                        this.host.settings.approvedRemoteHttpsEndpoint = null;
                    }
                    this.host.settings.baseUrl = endpoint;
                    remoteEndpointToggle
                        ?.setValue(isRemoteHttpsEndpointApproved(this.host.settings))
                        .setDisabled(!requiresRemoteHttpsApproval(this.host.settings.provider, endpoint));
                    if (wasApproved && changed) {
                        // The first byte of an endpoint change revokes the old Runtime endpoint.
                        // Later keystrokes are an inert draft until the new URL is confirmed.
                        await this.persistAndApply().catch((error) => {
                            new Notice(error instanceof Error ? error.message : "远程模型端点授权撤销失败");
                        });
                    } else {
                        await this.host.saveLocalSettings();
                    }
                }));
        if (usesCodexSubscription(this.host.settings)) {
            new Setting(containerEl)
                .setName("本机 HTTP 代理（可选）")
                .setDesc("仅接受带显式端口的 127.0.0.1 或 ::1 HTTP 代理；留空则由 Worker 直接连接。")
                .addText((text) => text
                    .setPlaceholder("http://127.0.0.1:<端口>")
                    .setValue(this.host.settings.proxyUrl)
                    .onChange(async (value) => {
                        this.host.settings.proxyUrl = safeProxyUrl(value);
                        await this.host.saveLocalSettings();
                    }));
        }
        new Setting(containerEl)
            .setName("允许远程兼容模型端点")
            .setDesc("仅适用于 OpenAI-compatible 的非回环 HTTPS 端点；确认绑定到当前 URL，端点一旦改变即自动撤销。")
            .addToggle((toggle) => {
                remoteEndpointToggle = toggle;
                toggle
                    .setValue(isRemoteHttpsEndpointApproved(this.host.settings))
                    .setDisabled(!requiresRemoteHttpsApproval(
                        this.host.settings.provider,
                        this.host.settings.baseUrl,
                    ))
                    .onChange(async (enabled) => {
                        if (!enabled) {
                            this.host.settings.approvedRemoteHttpsEndpoint = null;
                            await this.persistAndApply().catch((error) => {
                                new Notice(error instanceof Error ? error.message : "远程模型端点授权撤销失败");
                            });
                            return;
                        }
                        const endpoint = this.host.settings.baseUrl;
                        if (!requiresRemoteHttpsApproval(this.host.settings.provider, endpoint)) {
                            toggle.setValue(false);
                            new Notice("请先选择 OpenAI-compatible 并填写非回环 HTTPS 端点");
                            return;
                        }
                        const confirmed = window.confirm(
                            `模型请求将发送到以下远程端点：\n\n${endpoint}\n\n` +
                            "该授权只绑定当前规范化 URL；修改端点后必须重新确认。是否继续？",
                        );
                        if (!confirmed) {
                            toggle.setValue(false);
                            return;
                        }
                        this.host.settings.approvedRemoteHttpsEndpoint = endpoint;
                        await this.persistAndApply().catch(async (error) => {
                            // Never leave the UI/run configuration approved when the Runtime
                            // did not accept the endpoint.  The fallback also blocks old endpoints.
                            this.host.settings.approvedRemoteHttpsEndpoint = null;
                            toggle.setValue(false);
                            await this.host.saveLocalSettings().catch(() => undefined);
                            await this.host.applyRuntimeSettings().catch(() => undefined);
                            new Notice(error instanceof Error ? error.message : "远程模型端点授权失败");
                        });
                    });
            });
        new Setting(containerEl)
            .setName("模型")
            .setDesc(usesCodexSubscription(this.host.settings)
                ? "模型清单与健康状态由本地 Runtime 查询；登录凭据由 Worker 从本机 Codex 登录只读使用。"
                : "模型清单与健康状态由本地 Runtime 查询。凭据只保存在 Windows Secret Store。")
            .addText((text) => text
                .setPlaceholder("模型标识")
                .setValue(this.host.settings.model)
                .onChange(async (value) => {
                    const model = value.trim();
                    if (model) this.host.settings.model = model.slice(0, 256);
                    await this.host.saveLocalSettings();
                }));
        new Setting(containerEl)
            .setName("模型连接")
            .setDesc("应用当前 Provider/端点配置并执行不携带用户正文的健康检查。")
            .addButton((button) => button.setButtonText("应用并检查").setCta().onClick(async () => {
                if (usesUnconfiguredCompatibleFallback(this.host.settings)) {
                    await this.persistAndApply().catch((error) => {
                        new Notice(error instanceof Error ? error.message : "安全停用未确认模型端点失败");
                    });
                    new Notice("请先填写并确认 OpenAI-compatible 远程 HTTPS 模型端点");
                    return;
                }
                try {
                    await this.persistAndApply();
                    new Notice(await this.host.checkModelHealth());
                } catch (error) {
                    new Notice(error instanceof Error ? error.message : "模型健康检查失败");
                }
            }));
        if (usesProviderSecretStore(this.host.settings)) {
            let credentialValue = "";
            new Setting(containerEl)
                .setName("Provider 凭据")
                .setDesc("仅经认证 Named Pipe 写入 Windows DPAPI SecretStore；不会保存到 Vault、插件 data.json 或日志。")
                .addText((text) => {
                    text.setPlaceholder("输入后点击安全保存").onChange((value) => { credentialValue = value; });
                    text.inputEl.type = "password";
                    text.inputEl.autocomplete = "new-password";
                })
                .addButton((button) => button.setButtonText("安全保存").setCta().onClick(async () => {
                    if (!credentialValue) return;
                    const secret = credentialValue;
                    credentialValue = "";
                    const input = containerEl.querySelector<HTMLInputElement>('input[type="password"]');
                    if (input) input.value = "";
                    await this.host.saveProviderCredential(secret).catch((error) => {
                        new Notice(error instanceof Error ? error.message : "Provider 凭据保存失败");
                    });
                }))
                .addButton((button) => button.setButtonText("删除").setWarning().onClick(() =>
                    this.host.deleteProviderCredential().catch((error) => {
                        new Notice(error instanceof Error ? error.message : "Provider 凭据删除失败");
                    })));
        } else {
            new Setting(containerEl)
                .setName("Codex 登录")
                .setDesc("使用本机 Codex CLI 的 ChatGPT 登录；插件不读取、不保存登录凭据，也不会调用通用 SecretStore。需要登录时请在终端运行 codex login。");
        }
        new Setting(containerEl)
            .setName("推理强度")
            .addDropdown((dropdown) => dropdown
                .addOptions({ minimal: "最小", low: "低", medium: "中", high: "高", max: "最高" })
                .setValue(this.host.settings.reasoningEffort)
                .onChange(async (value) => {
                    this.host.settings.reasoningEffort = value as LocalOfferAgentSettings["reasoningEffort"];
                    await this.persistAndApply();
                }));
        this.extensionPanel = new ExtensionSettingsPanel(this.host);
        this.extensionPanel.mount(containerEl);
        new Setting(containerEl)
            .setName("信任当前 Workspace")
            .setDesc(this.host.settings.workspaceTrusted
                ? "已显式信任。标准模式中的写操作仍需按 Policy 审批；Shell、Hooks 与 Skills 仍分别受控。"
                : "尚未信任：当前有效权限为只读。确认信任后，标准模式才会把写操作送入 Diff 与审批流程。")
            .addToggle((toggle) => toggle
                .setValue(this.host.settings.workspaceTrusted)
                .onChange(async (value) => {
                    const previousTrust = this.host.settings.workspaceTrusted;
                    const previousMode = this.host.settings.permissionMode;
                    if (value && !window.confirm(
                        "信任当前 Workspace 会允许标准模式中的写操作进入 Diff 与逐次审批，也可显式启用本地扩展。高风险操作仍不会自动放行。是否继续？",
                    )) {
                        toggle.setValue(false);
                        return;
                    }
                    this.host.settings.workspaceTrusted = value;
                    if (!value && ["trusted-workspace", "bypass"].includes(this.host.settings.permissionMode)) {
                        this.host.settings.permissionMode = "normal";
                    }
                    if (!value) this.host.settings.autoApproveVaultWrites = false;
                    try {
                        await this.persistAndApply();
                        this.display();
                    } catch (error) {
                        this.host.settings.workspaceTrusted = previousTrust;
                        this.host.settings.permissionMode = previousMode;
                        toggle.setValue(previousTrust);
                        await this.host.saveLocalSettings().catch(() => undefined);
                        await this.host.applyRuntimeSettings().catch(() => undefined);
                        new Notice(error instanceof Error ? error.message : "Workspace 信任更新失败");
                    }
                }));
        new Setting(containerEl)
            .setName("Vault 写入无需逐次审批")
            .setDesc(this.host.settings.workspaceTrusted
                ? "启用后，当前本地 Vault 的 create/append/replace/patch 仍会经过 Schema、Diff、expectedHash、Journal 和审计，但不再逐次弹出批准卡。Shell 与网络权限不会因此开放。"
                : "请先信任当前 Workspace；未信任时所有写入仍保持只读。")
            .addToggle((toggle) => toggle
                .setValue(this.host.settings.autoApproveVaultWrites)
                .setDisabled(!this.host.settings.workspaceTrusted)
                .onChange(async (value) => {
                    if (value && !window.confirm(
                        "你将允许 OfferAgent 在这个受信任的本地 Vault 中自动执行文件写入。" +
                        "Diff、哈希冲突检测、幂等 Journal 和审计仍会保留；Shell 与网络权限不受影响。是否继续？",
                    )) {
                        toggle.setValue(false);
                        return;
                    }
                    this.host.settings.autoApproveVaultWrites = value;
                    await this.persistAndApply();
                }));
        new Setting(containerEl)
            .setName("权限模式")
            .setDesc(this.host.settings.permissionMode === "bypass"
                ? "当前受信任 Workspace 跳过逐工具审批；工具范围、参数校验、预算、审计和原子写入保护仍然有效。"
                : this.host.settings.workspaceTrusted
                ? "所有模式仍经过 Tool Schema、Policy、预算与审计；受信任工作区不会绕过高风险审批。"
                : "Workspace 尚未信任；选择“标准”时有效权限仍为只读，“受信任工作区”不可用。")
            .addDropdown((dropdown) => dropdown
                .addOption("read-only", "只读")
                .addOption("normal", "标准")
                .addOption("trusted-workspace", "受信任工作区")
                .addOption("plan", "仅规划")
                .addOption("bypass", "免审批执行")
                .setValue(this.host.settings.permissionMode)
                .onChange(async (value) => {
                    if (["trusted-workspace", "bypass"].includes(value) && !this.host.settings.workspaceTrusted) {
                        dropdown.setValue(this.host.settings.permissionMode);
                        new Notice("请先显式信任当前 Workspace");
                        return;
                    }
                    this.host.settings.permissionMode = value as LocalOfferAgentSettings["permissionMode"];
                    await this.persistAndApply();
                }));
        new Setting(containerEl)
            .setName("子 Agent")
            .setDesc("启用后才允许当前 Run 创建隔离的子 Agent；子 Agent 仍只能继承更小的工具、权限和预算范围。")
            .addToggle((toggle) => toggle
                .setValue(this.host.settings.subagentsEnabled)
                .onChange(async (value) => {
                    this.host.settings.subagentsEnabled = value;
                    await this.persistAndApply();
                }));
        new Setting(containerEl)
            .setName("后台继续运行")
            .setDesc("关闭标签或热重载插件不会取消活动 Run；无客户端且空闲后由 Host 决定退出。")
            .addToggle((toggle) => toggle
                .setValue(this.host.settings.keepWorkerInBackground)
                .onChange(async (value) => {
                    this.host.settings.keepWorkerInBackground = value;
                    await this.host.saveLocalSettings();
                }));
        new Setting(containerEl)
            .setName("本地诊断")
            .setDesc("查看 Runtime、文件工具状态、模型认证、后台进程和脱敏错误。")
            .addButton((button) => button.setButtonText("打开诊断").onClick(() => void this.host.openDiagnostics()));
        containerEl.createEl("p", {
            text: "遥测默认关闭且当前发行不可在 UI 中开启；诊断不会自动上传。",
            cls: "setting-item-description",
        });
    }

    private async persistAndApply(): Promise<void> {
        await this.host.saveLocalSettings();
        await this.host.applyRuntimeSettings();
    }

    hide(): void {
        this.extensionPanel?.dispose();
        this.extensionPanel = null;
    }

}

export function parseLocalSettings(raw: unknown): LocalOfferAgentSettings {
    if (!raw || typeof raw !== "object" || Array.isArray(raw)) return { ...DEFAULT_LOCAL_SETTINGS };
    const value = raw as Record<string, unknown>;
    if (value.schemaVersion !== undefined && value.schemaVersion !== 2) {
        return { ...DEFAULT_LOCAL_SETTINGS };
    }
    const provider = [DEEPSEEK_PROVIDER, CODEX_SUBSCRIPTION_PROVIDER, "codex", "openai", "openai-compatible", "local"]
        .includes(String(value.provider))
        ? value.provider as LocalOfferAgentSettings["provider"] : DEFAULT_LOCAL_SETTINGS.provider;
    const wireApi = provider === "local" && ["responses", "ollama-chat"].includes(String(value.wireApi))
        ? value.wireApi as LocalOfferAgentSettings["wireApi"]
        : fixedWireApi(provider);
    const rawBaseUrl = typeof value.baseUrl === "string"
        ? value.baseUrl
        : provider === "local" ? LOCAL_OLLAMA_BASE_URL : "";
    const baseUrl = safeBaseUrl(rawBaseUrl, provider);
    const proxyUrl = provider === CODEX_SUBSCRIPTION_PROVIDER && typeof value.proxyUrl === "string"
        ? safeProxyUrl(value.proxyUrl)
        : "";
    const approvedRemoteHttpsEndpoint =
        provider === "openai-compatible" &&
        requiresRemoteHttpsApproval(provider, baseUrl) &&
        typeof value.approvedRemoteHttpsEndpoint === "string" &&
        safeBaseUrl(value.approvedRemoteHttpsEndpoint, provider) === baseUrl
            ? baseUrl
            : null;
    const reasoning = ["minimal", "low", "medium", "high", "max"].includes(String(value.reasoningEffort))
        ? value.reasoningEffort as LocalOfferAgentSettings["reasoningEffort"] : DEFAULT_LOCAL_SETTINGS.reasoningEffort;
    const parsedPermission = ["read-only", "normal", "trusted-workspace", "plan", "bypass"].includes(String(value.permissionMode))
        ? value.permissionMode as LocalOfferAgentSettings["permissionMode"] : DEFAULT_LOCAL_SETTINGS.permissionMode;
    const workspaceTrusted = value.workspaceTrusted === true;
    const permission = ["trusted-workspace", "bypass"].includes(parsedPermission) && !workspaceTrusted
        ? "normal"
        : parsedPermission;
    const defaultModel = provider === DEEPSEEK_PROVIDER ? DEEPSEEK_DEFAULT_MODEL : DEFAULT_RESPONSES_MODEL;
    return {
        schemaVersion: 2,
        provider,
        wireApi,
        baseUrl,
        proxyUrl,
        approvedRemoteHttpsEndpoint,
        model: typeof value.model === "string" && value.model.trim()
            ? value.model.trim().slice(0, 256)
            : defaultModel,
        reasoningEffort: reasoning,
        permissionMode: permission,
        workspaceTrusted,
        autoApproveVaultWrites: workspaceTrusted && value.autoApproveVaultWrites === true,
        shellEnabled: value.shellEnabled === true,
        subagentsEnabled: value.subagentsEnabled === true,
        hooksEnabled: value.hooksEnabled === true,
        keepWorkerInBackground: value.keepWorkerInBackground !== false,
        telemetryEnabled: false,
    };
}

function fixedWireApi(provider: LocalOfferAgentSettings["provider"]): LocalOfferAgentSettings["wireApi"] {
    if (provider === DEEPSEEK_PROVIDER) return "chat-completions";
    if (provider === "local") return "ollama-chat";
    return "responses";
}

export function effectivePermissionMode(
    settings: LocalOfferAgentSettings,
): LocalOfferAgentSettings["permissionMode"] {
    if (settings.permissionMode === "plan") return "plan";
    if (settings.permissionMode === "read-only" || !settings.workspaceTrusted) return "read-only";
    return settings.permissionMode;
}

export function vaultWriteAvailable(settings: LocalOfferAgentSettings): boolean {
    const mode = effectivePermissionMode(settings);
    return mode === "normal" || mode === "trusted-workspace" || mode === "bypass";
}

export function runConfig(settings: LocalOfferAgentSettings): TurnRunConfig {
    if (usesUnconfiguredCompatibleFallback(settings)) {
        throw new Error("OpenAI-compatible 模型端点尚未确认，不能开始对话");
    }
    return {
        provider: settings.provider,
        model: settings.model,
        reasoningEffort: settings.reasoningEffort,
        permissionMode: settings.permissionMode,
    };
}

export function modelRuntimePatch(settings: LocalOfferAgentSettings, credentialHandle: string | null): JsonObject {
    if (usesUnconfiguredCompatibleFallback(settings)) {
        return {
            provider: UNCONFIGURED_COMPATIBLE_PROVIDER,
            wire_api: "responses",
            model: settings.model,
            reasoning_effort: settings.reasoningEffort,
            base_url: "",
            proxy_url: null,
            credential_handle: null,
            allow_remote_https: false,
        };
    }
    const subscription = usesCodexSubscription(settings);
    return {
        provider: settings.provider,
        wire_api: subscription ? "responses" : settings.wireApi,
        model: settings.model,
        reasoning_effort: settings.reasoningEffort,
        base_url: subscription ? "" : settings.baseUrl,
        proxy_url: subscription && settings.proxyUrl ? settings.proxyUrl : null,
        credential_handle: subscription ? null : credentialHandle,
        allow_remote_https: isRemoteHttpsEndpointApproved(settings),
    };
}

export function snapshotLocalSettings(settings: LocalOfferAgentSettings): LocalOfferAgentSettings {
    const snapshot = parseLocalSettings(settings);
    return Object.freeze(snapshot);
}

export function usesUnconfiguredCompatibleFallback(settings: LocalOfferAgentSettings): boolean {
    if (settings.provider !== "openai-compatible") return false;
    const endpoint = safeBaseUrl(settings.baseUrl, settings.provider);
    if (!endpoint) return true;
    return requiresRemoteHttpsApproval(settings.provider, endpoint) &&
        settings.approvedRemoteHttpsEndpoint !== endpoint;
}

export function modelCredentialProviderId(settings: LocalOfferAgentSettings): string {
    if (usesCodexSubscription(settings)) {
        throw new Error("Codex 订阅使用本机登录，不使用 Provider SecretStore");
    }
    if (settings.provider !== "openai-compatible") return settings.provider;
    const endpoint = safeBaseUrl(settings.baseUrl, settings.provider);
    if (!endpoint) throw new Error("请先配置有效的 OpenAI-compatible 模型端点");
    const digest = createHash("sha256").update(endpoint, "utf8").digest("hex").slice(0, 32);
    return `openai-compatible.${digest}`;
}

export function usesCodexSubscription(settings: LocalOfferAgentSettings): boolean {
    return settings.provider === CODEX_SUBSCRIPTION_PROVIDER;
}

export function usesProviderSecretStore(settings: LocalOfferAgentSettings): boolean {
    return !usesCodexSubscription(settings);
}

export function modelHealthMessage(
    settings: LocalOfferAgentSettings,
    status: string,
    reason: string | null = null,
): string {
    if (settings.provider === DEEPSEEK_PROVIDER) {
        if (status === "healthy") return "DeepSeek 模型连接健康";
        if (status === "auth_required" && reason === "credential_unavailable") {
            return "DeepSeek API Key 尚未安全保存，或当前凭据绑定已失效";
        }
        if (status === "auth_required" && reason === "auth_required") {
            return "DeepSeek 拒绝当前 API Key；请创建新 Key 后重新安全保存";
        }
        if (status === "auth_required") return "DeepSeek API Key 认证失败";
        if (status === "unreachable") return "DeepSeek API 不可达；请检查本机网络";
        if (status === "unsupported") return "当前 Runtime 不支持 DeepSeek Chat Completions，请更新本地 Runtime";
        return `DeepSeek 模型状态：${status}`;
    }
    if (!usesCodexSubscription(settings)) {
        return status === "healthy" ? "模型连接健康" : `模型状态：${status}`;
    }
    if (status === "healthy") return "Codex 订阅连接健康（使用本机 Codex 登录）";
    if (status === "auth_required") return "未检测到有效的本机 Codex ChatGPT 登录；请在终端运行 codex login";
    if (status === "unreachable") {
        return settings.proxyUrl
            ? "Codex 订阅端点不可达；请确认本机 HTTP 代理正在运行"
            : "Codex 订阅端点不可达；若当前网络需要代理，请配置本机回环 HTTP 代理";
    }
    if (status === "unsupported") return "当前 Runtime 不支持 Codex 订阅 Provider，请更新本地 Runtime";
    return `Codex 订阅状态：${status}`;
}

export class SerializedOperationQueue {
    private tail: Promise<void> = Promise.resolve();

    run<T>(operation: () => Promise<T>): Promise<T> {
        const result = this.tail.then(operation, operation);
        this.tail = result.then(() => undefined, () => undefined);
        return result;
    }
}

export function isRemoteHttpsEndpointApproved(settings: LocalOfferAgentSettings): boolean {
    return requiresRemoteHttpsApproval(settings.provider, settings.baseUrl) &&
        settings.approvedRemoteHttpsEndpoint === settings.baseUrl;
}

export function requiresRemoteHttpsApproval(
    provider: LocalOfferAgentSettings["provider"],
    endpoint: string,
): boolean {
    if (provider !== "openai-compatible" || !endpoint) return false;
    try {
        const parsed = new URL(endpoint);
        return parsed.protocol === "https:" && !["127.0.0.1", "[::1]"].includes(parsed.hostname);
    } catch (error) {
        return false;
    }
}

function stringList(raw: unknown, limit: number): string[] {
    if (!Array.isArray(raw) || raw.length > limit) return [];
    const values: string[] = [];
    for (const item of raw) {
        if (typeof item !== "string" || !item || item.length > 256 || values.includes(item)) return [];
        values.push(item);
    }
    return values;
}

function safeBaseUrl(raw: string, provider: LocalOfferAgentSettings["provider"]): string {
    const value = raw.trim().replace(/\/+$/, "");
    if ([DEEPSEEK_PROVIDER, CODEX_SUBSCRIPTION_PROVIDER, "codex", "openai"].includes(provider)) return "";
    if (!value) return provider === "local" ? LOCAL_OLLAMA_BASE_URL : "";
    try {
        const parsed = new URL(value);
        if (parsed.username || parsed.password || parsed.search || parsed.hash) return "";
        if (provider === "local") {
            if (parsed.protocol !== "http:" || !["127.0.0.1", "[::1]"].includes(parsed.hostname) ||
                !parsed.port || parsed.pathname !== "/api") return "";
            return parsed.toString().replace(/\/$/, "");
        }
        if (parsed.protocol !== "https:" || parsed.pathname.includes("%") || parsed.pathname.includes("\\")) return "";
        const segments = parsed.pathname.split("/").filter(Boolean);
        if (segments.some((part) => part === "." || part === "..")) return "";
        const path = segments.length > 0 ? `/${segments.join("/")}` : "";
        // Keep this byte-for-byte aligned with Worker `_normalize_endpoint`;
        // the resulting UTF-8 bytes are the endpoint-scoped Secret identity.
        return `${parsed.protocol}//${parsed.host}${path}`;
    } catch (error) {
        return "";
    }
}

export function safeProxyUrl(raw: string): string {
    const value = raw.trim();
    if (!value) return "";
    const match = /^(http:\/\/)(127\.0\.0\.1|\[::1\]):([0-9]{1,5})\/?$/.exec(value);
    if (!match) return "";
    const port = Number(match[3]);
    if (!Number.isSafeInteger(port) || port < 1 || port > 65_535) return "";
    return `${match[1]}${match[2]}:${port}`;
}

function parseNames(raw: string, limit: number): string[] {
    const values = [...new Set(raw.split(",").map((item) => item.trim()).filter(Boolean))];
    if (values.length > limit || values.some((item) => item.length > 256 || !/^[A-Za-z0-9][A-Za-z0-9_.-]*$/.test(item))) {
        return [];
    }
    return values;
}
