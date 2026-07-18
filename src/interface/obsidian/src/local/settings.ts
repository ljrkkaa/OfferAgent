import { App, Notice, Plugin, PluginSettingTab, Setting } from "obsidian";

import { TurnRunConfig } from "../runtime/chat_store";
import { JsonObject } from "../runtime/json_rpc";
import { ExtensionSettingsPanel, ExtensionSettingsPanelHost } from "./extension_settings_panel";

export interface LocalOfferAgentSettings {
    schemaVersion: 3;
    proxyUrl: string;
    model: string;
    modelAccountBinding: string | null;
    reasoningEffort: "minimal" | "low" | "medium" | "high" | "max";
    permissionMode: "read-only" | "normal" | "trusted-workspace" | "plan" | "bypass";
    workspaceTrusted: boolean;
    autoApproveVaultWrites: boolean;
    shellEnabled: boolean;
    subagentsEnabled: boolean;
    hooksEnabled: boolean;
    telemetryEnabled: false;
}

export const DEFAULT_LOCAL_SETTINGS: LocalOfferAgentSettings = {
    schemaVersion: 3,
    proxyUrl: "",
    model: "",
    modelAccountBinding: null,
    reasoningEffort: "medium",
    permissionMode: "normal",
    workspaceTrusted: false,
    autoApproveVaultWrites: false,
    shellEnabled: false,
    subagentsEnabled: false,
    hooksEnabled: false,
    telemetryEnabled: false,
};

export const CODEX_SUBSCRIPTION_PROVIDER = "codex-subscription-experimental" as const;

export interface SettingsHost extends ExtensionSettingsPanelHost {
    settings: LocalOfferAgentSettings;
    saveLocalSettings(): Promise<void>;
    applyRuntimeSettings(): Promise<void>;
    openDiagnostics(): Promise<void>;
    checkModelCatalog(): Promise<string>;
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
        new Setting(containerEl)
            .setName("模型")
            .setDesc(this.host.settings.model
                ? `当前目录选择：${this.host.settings.model}。请在 Agent 面板的实时 Codex 模型目录中更改。`
                : "尚未选择模型。请在 Agent 面板的实时 Codex 模型目录中选择后再开始新 Run。");
        new Setting(containerEl)
            .setName("Codex 登录")
            .setDesc("模型推理固定使用本机 Codex CLI 的 ChatGPT 登录；插件不会读取或保存登录凭据。需要登录时请在终端运行 codex login。");
        new Setting(containerEl)
            .setName("本机 HTTP 代理（可选）")
            .setDesc("仅接受带显式端口的 127.0.0.1 或 ::1 HTTP 代理；留空则由 Worker 直接连接 Codex。")
            .addText((text) => text
                .setPlaceholder("http://127.0.0.1:<端口>")
                .setValue(this.host.settings.proxyUrl)
                .onChange(async (value) => {
                    this.host.settings.proxyUrl = safeProxyUrl(value);
                    await this.host.saveLocalSettings();
                }));
        new Setting(containerEl)
            .setName("Codex 模型目录")
            .setDesc("检查当前本机登录与实时目录状态；不会切换模型或回退到其他模型来源。")
            .addButton((button) => button.setButtonText("应用并检查").setCta().onClick(async () => {
                try {
                    await this.persistAndApply();
                    new Notice(await this.host.checkModelCatalog());
                } catch (error) {
                    new Notice(error instanceof Error ? error.message : "模型目录检查失败");
                }
            }));
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
            .setName("本地诊断")
            .setDesc("查看 Runtime、文件工具状态、模型认证、Worker 进程和脱敏错误。")
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
    if (value.schemaVersion !== 2 && value.schemaVersion !== 3) {
        return { ...DEFAULT_LOCAL_SETTINGS };
    }
    const codexSettings = value.schemaVersion !== 2 || value.provider === CODEX_SUBSCRIPTION_PROVIDER;
    const proxyUrl = codexSettings && typeof value.proxyUrl === "string"
        ? safeProxyUrl(value.proxyUrl)
        : "";
    const reasoning = ["minimal", "low", "medium", "high", "max"].includes(String(value.reasoningEffort))
        ? value.reasoningEffort as LocalOfferAgentSettings["reasoningEffort"] : DEFAULT_LOCAL_SETTINGS.reasoningEffort;
    const parsedPermission = ["read-only", "normal", "trusted-workspace", "plan", "bypass"].includes(String(value.permissionMode))
        ? value.permissionMode as LocalOfferAgentSettings["permissionMode"] : DEFAULT_LOCAL_SETTINGS.permissionMode;
    const workspaceTrusted = value.workspaceTrusted === true;
    const permission = ["trusted-workspace", "bypass"].includes(parsedPermission) && !workspaceTrusted
        ? "normal"
        : parsedPermission;
    const model = typeof value.model === "string" ? value.model.trim() : "";
    const modelIsValid = model.length > 0 && model.length <= 256 && !model.includes("\0");
    const accountBinding = typeof value.modelAccountBinding === "string" &&
        /^sha256:[0-9a-f]{64}$/.test(value.modelAccountBinding)
        ? value.modelAccountBinding
        : null;
    const hasCurrentCatalogSelection = value.schemaVersion === 3 && modelIsValid && accountBinding !== null;
    return {
        schemaVersion: 3,
        proxyUrl,
        model: hasCurrentCatalogSelection ? model : "",
        modelAccountBinding: hasCurrentCatalogSelection ? accountBinding : null,
        reasoningEffort: reasoning,
        permissionMode: permission,
        workspaceTrusted,
        autoApproveVaultWrites: workspaceTrusted && value.autoApproveVaultWrites === true,
        shellEnabled: value.shellEnabled === true,
        subagentsEnabled: value.subagentsEnabled === true,
        hooksEnabled: value.hooksEnabled === true,
        telemetryEnabled: false,
    };
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
    if (!settings.model || !settings.modelAccountBinding) {
        throw new Error("请先从当前 Codex 模型目录选择模型");
    }
    return {
        provider: CODEX_SUBSCRIPTION_PROVIDER,
        model: settings.model,
        reasoningEffort: settings.reasoningEffort,
        permissionMode: settings.permissionMode,
    };
}

export function modelRuntimePatch(settings: LocalOfferAgentSettings): JsonObject {
    return {
        model: settings.model,
        account_binding: settings.modelAccountBinding,
        reasoning_effort: settings.reasoningEffort,
        proxy_url: settings.proxyUrl || null,
    };
}

export function snapshotLocalSettings(settings: LocalOfferAgentSettings): LocalOfferAgentSettings {
    const snapshot = parseLocalSettings(settings);
    return Object.freeze(snapshot);
}

export class SerializedOperationQueue {
    private tail: Promise<void> = Promise.resolve();

    run<T>(operation: () => Promise<T>): Promise<T> {
        const result = this.tail.then(operation, operation);
        this.tail = result.then(() => undefined, () => undefined);
        return result;
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
