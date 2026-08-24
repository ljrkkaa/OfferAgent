import { Notice, Setting } from "obsidian";

import type { ProtocolCommandParams, ProtocolCommandResult } from "../runtime/generated_protocol";
import {
    ExecutableRegistrationView,
    ExtensionCommandMethod,
    ExtensionRequestScope,
    ExtensionRuntimeManager,
    ExtensionSnapshot,
    HookInstallDraft,
    ProcessExecutableDraft,
    ProcessEnvironmentDraft,
    ProcessProbeView,
    ShellInstallDraft,
} from "./extension_settings";

const HOOK_EVENTS = [
    "SessionStart", "TurnStart", "BeforeModel", "AfterModel", "PreToolUse", "PostToolUse",
    "ApprovalRequired", "SubagentStart", "SubagentStop", "BeforeCompact", "TurnStop", "RuntimeShutdown",
] as const;

export interface ExtensionSettingsPanelHost {
    extensionRequest<Method extends ExtensionCommandMethod>(
        method: Method,
        params: ProtocolCommandParams<Method>,
        options?: { signal?: AbortSignal; timeoutMs?: number },
    ): Promise<ProtocolCommandResult<Method>>;
    extensionExecutionEnabled(kind: "shell" | "hooks"): boolean;
    setExtensionExecutionEnabled(kind: "shell" | "hooks", enabled: boolean): Promise<void>;
}

export class ExtensionSettingsPanel {
    private readonly manager: ExtensionRuntimeManager;
    private readonly scope = new ExtensionRequestScope();
    private root: HTMLDivElement | null = null;
    private statusEl: HTMLDivElement | null = null;
    private snapshot: ExtensionSnapshot | null = null;
    private pendingProcessProbe: ProcessProbeView | null = null;
    private processProbeEl: HTMLDivElement | null = null;

    constructor(private readonly host: ExtensionSettingsPanelHost) {
        this.manager = new ExtensionRuntimeManager({
            request: (method, params, options) => host.extensionRequest(method, params, options),
        });
    }

    mount(containerEl: HTMLElement): void {
        this.dispose();
        this.root = containerEl.createDiv({ cls: "offeragent-extension-settings" });
        void this.refresh();
    }

    dispose(): void {
        this.scope.cancel();
        this.root?.remove();
        this.root = null;
        this.statusEl = null;
        this.snapshot = null;
        this.pendingProcessProbe = null;
        this.processProbeEl = null;
    }

    private async refresh(): Promise<void> {
        this.setStatus("正在读取 Skills / Shell / Hooks…");
        try {
            this.snapshot = await this.scope.run((signal) => this.manager.snapshot(signal));
            this.render();
            this.setStatus("扩展状态来自当前 Windows Worker");
        } catch (error) {
            this.showError(error, "无法读取扩展管理状态");
        }
    }

    private render(): void {
        if (!this.root) return;
        this.root.empty();
        this.root.createEl("h3", { text: "Skills、Processes、Shell 与 Hooks" });
        this.root.createEl("p", {
            text: "读取同一 Worker 的持久目录；所有变更均经私有 stdio RPC、Workspace trust、revision/contentHash 与 clientRequestId 回执。命令参数逐项填写，不接受 raw shell command 或 secret。",
            cls: "setting-item-description",
        });
        const toolbar = this.root.createDiv({ cls: "offeragent-extension-actions" });
        toolbar.append(
            this.button("刷新", () => this.refresh()),
            this.button("取消请求", () => {
                this.scope.cancel();
                this.setStatus("扩展管理请求已取消");
            }),
        );
        this.statusEl = this.root.createDiv({ cls: "offeragent-extension-status" });
        if (!this.snapshot) return;
        this.renderSkills(this.root, this.snapshot);
        this.renderProcesses(this.root, this.snapshot);
        this.renderShell(this.root, this.snapshot);
        this.renderHooks(this.root, this.snapshot);
    }

    private renderSkills(container: HTMLElement, snapshot: ExtensionSnapshot): void {
        const section = container.createDiv({ cls: "offeragent-extension-section" });
        section.createEl("h4", { text: "Skills" });
        const status = snapshot.skillStatus;
        section.createEl("p", {
            text: `revision ${status.revision} · ${status.enabledCount}/${status.discoveredCount} 可用；工作区受信任后，Skill 元数据自动提供给模型，正文仅在调用 Skill 时按需读取${status.partial ? " · partial（保持旧快照）" : ""}`,
            cls: status.partial ? "offeragent-extension-warning" : "setting-item-description",
        });
        if (!snapshot.skills.length) section.createEl("p", { text: "没有发现 Skill。", cls: "setting-item-description" });
        for (const skill of snapshot.skills) {
            const row = section.createDiv({ cls: "offeragent-extension-card" });
            new Setting(row)
                .setName(skill.name)
                .setDesc(`${skill.layer} · ${skill.description}`);
        }
        for (const diagnostic of status.diagnostics) {
            section.createEl("p", {
                text: `${diagnostic.severity}: ${diagnostic.code} — ${diagnostic.message}`,
                cls: diagnostic.severity === "error" ? "offeragent-extension-error" : "offeragent-extension-warning",
            });
        }
    }

    private renderProcesses(container: HTMLElement, snapshot: ExtensionSnapshot): void {
        const section = container.createDiv({ cls: "offeragent-extension-section" });
        section.createEl("h4", { text: "本机 Process 注册" });
        section.createEl("p", {
            text: `catalog revision ${snapshot.processes.catalogRevision} · 当前 Worker revision ` +
                `${snapshot.processes.activeCatalogRevision}`,
            cls: "setting-item-description",
        });
        if (snapshot.processes.restartRequired) {
            section.createEl("p", {
                text: "注册已持久化；需要重启当前 Vault 的 Worker 后，Shell / Hook 才会使用新快照。",
                cls: "offeragent-extension-warning",
            });
        }
        for (const executable of snapshot.processes.executables) {
            const row = new Setting(section)
                .setName(executable.executableId)
                .setDesc(`${executable.trust} · ${executable.available ? "可用" : "不可用"} · ` +
                    `revision ${executable.revision} · ${executable.canonicalPath}`);
            row.addButton((button) => button.setButtonText("删除").setWarning().onClick(() => void this.mutate(
                "Process executable 注册已删除；重启 Worker 后生效",
                (signal) => this.manager.deleteProcess("executable", executable, snapshot.processes, signal),
            )));
        }
        for (const environment of snapshot.processes.environments) {
            const row = new Setting(section)
                .setName(environment.profileId)
                .setDesc(`环境 Profile · ${environment.available ? "可用" : "不可用"} · revision ${environment.revision}`);
            row.addButton((button) => button.setButtonText("删除").setWarning().onClick(() => void this.mutate(
                "Process environment 注册已删除；重启 Worker 后生效",
                (signal) => this.manager.deleteProcess("environment", environment, snapshot.processes, signal),
            )));
        }

        const environmentForm = section.createEl("details", { cls: "offeragent-extension-form" });
        environmentForm.createEl("summary", { text: "注册环境 Profile（先探测，再确认）" });
        const environmentId = this.input(environmentForm, "Profile ID", "local_environment");
        const plainNames = this.textarea(environmentForm, "普通变量名：每行一个（PATH/PROXY/credential-like 会被拒绝）");
        const secretNames = this.textarea(environmentForm, "Secret 变量名：每行一个（仅注册名称，不输入明文）");
        environmentForm.appendChild(this.button("探测环境 Profile", async () => {
            const draft: ProcessEnvironmentDraft = {
                profileId: environmentId.value.trim(),
                allowedNames: lines(plainNames.value),
                allowedSecretNames: lines(secretNames.value),
            };
            await this.probeProcess((signal) =>
                this.manager.probeProcessEnvironment(draft, snapshot.processes, signal));
        }));

        const executableForm = section.createEl("details", { cls: "offeragent-extension-form" });
        executableForm.createEl("summary", { text: "注册绝对 .exe（先离线探测，再确认）" });
        const executableId = this.input(executableForm, "Executable ID", "local_tool");
        const executablePath = this.input(executableForm, "绝对 .exe 路径", "C:\\Program Files\\Vendor\\tool.exe");
        executablePath.maxLength = 32_767;
        const fixedArguments = this.textarea(executableForm, "固定参数：每行一个（不进行命令行解析）");
        const minimumArguments = this.input(executableForm, "可变参数最少数量", "0");
        minimumArguments.type = "number";
        minimumArguments.min = "0";
        minimumArguments.max = "128";
        minimumArguments.value = "0";
        const maximumArguments = this.input(executableForm, "可变参数最多数量", "0");
        maximumArguments.type = "number";
        maximumArguments.min = "0";
        maximumArguments.max = "128";
        maximumArguments.value = "0";
        const argumentPattern = this.input(executableForm, "单个可变参数 regex", "^[A-Za-z0-9_.-]{1,128}$");
        argumentPattern.value = "^[A-Za-z0-9_.-]{1,128}$";
        const environmentIds = [...new Set([
            ...snapshot.shell.environments.map((item) => item.profileId),
            ...snapshot.processes.environments.filter((item) => item.available).map((item) => item.profileId),
        ])].sort();
        const environments = this.select(executableForm, environmentIds.map((item) => [item, item]));
        const stdinMode = this.select(executableForm, [
            ["all", "Shell + Hook（closed / fixed_payload）"],
            ["closed", "Shell：closed"],
            ["fixed_payload", "Hook：fixed_payload"],
        ]);
        const cwdRoot = this.select(executableForm, [["vault", "vault"], ["process-scratch", "process-scratch"]]);
        const appContainerPath = this.input(executableForm, "AppContainer 窄授权相对路径", ".offeragent");
        appContainerPath.value = ".offeragent";
        cwdRoot.addEventListener("change", () => {
            appContainerPath.value = cwdRoot.value === "process-scratch" ? "working" : ".offeragent";
        });
        executableForm.createEl("p", {
            text: "网络权限固定为关闭。UNC、device path、reparse point、硬链接和命令解释器会被拒绝。",
            cls: "setting-item-description",
        });
        executableForm.appendChild(this.button("离线探测 executable", async () => {
            const draft: ProcessExecutableDraft = {
                executableId: executableId.value.trim(),
                executablePath: executablePath.value.trim(),
                fixedArguments: lines(fixedArguments.value),
                minimumVariableArguments: Number(minimumArguments.value),
                maximumVariableArguments: Number(maximumArguments.value),
                variableArgumentPattern: argumentPattern.value,
                environmentProfileIds: [environments.value],
                allowedStdinModes: stdinMode.value === "all"
                    ? ["closed", "fixed_payload", "duplex"]
                    : [stdinMode.value as ProcessExecutableDraft["allowedStdinModes"][number]],
                cwdRootId: cwdRoot.value,
                appContainerRelativePath: appContainerPath.value.trim(),
            };
            await this.probeProcess((signal) =>
                this.manager.probeProcessExecutable(draft, snapshot.processes, signal));
        }));

        this.processProbeEl = section.createDiv({ cls: "offeragent-extension-card" });
        this.renderProcessProbe(snapshot);
    }

    private async probeProcess(operation: (signal: AbortSignal) => Promise<ProcessProbeView>): Promise<void> {
        this.setStatus("正在离线探测 Process 注册…");
        try {
            this.pendingProcessProbe = await this.scope.run(operation);
            this.renderProcessProbe(this.snapshot as ExtensionSnapshot);
            this.setStatus("探测完成；请核对 SHA-256 / Authenticode 后显式确认");
        } catch (error) {
            this.showError(error, "Process 注册探测失败");
        }
    }

    private renderProcessProbe(snapshot: ExtensionSnapshot): void {
        if (!this.processProbeEl) return;
        this.processProbeEl.empty();
        const probe = this.pendingProcessProbe;
        if (!probe) {
            this.processProbeEl.createEl("p", { text: "尚无待确认探测。", cls: "setting-item-description" });
            return;
        }
        this.processProbeEl.createEl("strong", { text: `待确认：${probe.registrationId}` });
        this.processProbeEl.createEl("p", {
            text: `contentHash ${probe.contentHash} · 过期 ${probe.expiresAt}`,
            cls: "setting-item-description",
        });
        if (probe.kind === "executable") {
            this.processProbeEl.createEl("p", { text: "待固定可执行文件（仅本次 challenge；不会写入插件设置）：" });
            this.processProbeEl.createEl("code", { text: probe.canonicalPath ?? "" });
            this.processProbeEl.createEl("p", {
                text: `固定根 ${probe.fixedRoot} · ${probe.trust} · ${probe.fileSize} bytes · file identity ${probe.fileIdentity}`,
                cls: "setting-item-description",
            });
            this.processProbeEl.createEl("p", {
                text: `fileSha256 ${probe.fileSha256} · profileFingerprint ${probe.profileFingerprint}`,
                cls: "setting-item-description",
            });
            this.processProbeEl.createEl("p", {
                text: `固定 argv ${JSON.stringify(probe.fixedArguments)} · 可变参数 ${probe.minimumVariableArguments}–${probe.maximumVariableArguments}`,
                cls: "setting-item-description",
            });
            this.processProbeEl.createEl("code", { text: probe.variableArgumentPattern ?? "" });
            this.processProbeEl.createEl("p", {
                text: `stdin [${probe.allowedStdinModes.join(", ")}] · cwd roots [${probe.allowedCwdRootIds.join(", ")}] · env profiles [${probe.environmentProfileIds.join(", ")}] · network=false`,
                cls: "setting-item-description",
            });
            this.processProbeEl.createEl("p", {
                text: `AppContainer grants: ${probe.appcontainerFilesystem.map((item) =>
                    `${item.rootId}/${item.relativePath}:${item.access}`).join(" · ")}`,
                cls: "setting-item-description",
            });
            this.processProbeEl.createEl("p", {
                text: probe.authenticodeVerified
                    ? "已通过离线 Authenticode；确认后仍固定文件身份与配置指纹。"
                    : "未通过 Authenticode，仅固定当前 SHA-256",
                cls: probe.authenticodeVerified ? "setting-item-description" : "offeragent-extension-warning",
            });
        } else {
            this.processProbeEl.createEl("p", {
                text: `普通环境变量名 [${probe.allowedEnvironmentNames.join(", ")}] · SecretStore 注入名 [${probe.allowedSecretEnvironmentNames.join(", ")}]`,
                cls: "setting-item-description",
            });
        }
        this.processProbeEl.appendChild(this.button("确认并注册", () => this.mutate(
            "Process 注册已保存；重启 Worker 后生效",
            async (signal) => {
                await this.manager.confirmProcess(probe, snapshot.processes, signal);
                this.pendingProcessProbe = null;
            },
        )));
    }

    private renderShell(container: HTMLElement, snapshot: ExtensionSnapshot): void {
        const section = container.createDiv({ cls: "offeragent-extension-section" });
        section.createEl("h4", { text: "Shell profiles" });
        new Setting(section)
            .setName("允许 Run 使用已启用 Shell profile")
            .setDesc("仅影响执行开关；profile 仍需独立 contentHash 确认和 Policy/Approval。")
            .addToggle((toggle) => toggle
                .setValue(this.host.extensionExecutionEnabled("shell"))
                .onChange((value) => this.host.setExtensionExecutionEnabled("shell", value).catch((error) =>
                    this.showError(error, "无法更新 Shell 执行开关"))));
        for (const profile of snapshot.shell.profiles) {
            const row = new Setting(section)
                .setName(profile.profileId)
                .setDesc(`${profile.executableId} · ${profile.source} · ${profile.trust} · revision ${profile.revision}`);
            if (profile.trust === "confirmation_required") {
                row.addButton((button) => button.setButtonText("确认 profile hash").setCta().onClick(() =>
                    void this.mutate("Shell profile 已确认", (signal) => this.manager.confirmShell(profile, signal))));
            }
            row.addToggle((toggle) => toggle
                .setValue(profile.enabled)
                .setDisabled(profile.trust === "confirmation_required")
                .onChange((enabled) => this.mutate(
                    enabled ? "Shell profile 已启用" : "Shell profile 已停用",
                    (signal) => this.manager.setShellEnabled(profile, enabled, signal),
                )));
        }
        this.renderShellInstaller(section, snapshot);
    }

    private renderShellInstaller(container: HTMLElement, snapshot: ExtensionSnapshot): void {
        const details = container.createEl("details", { cls: "offeragent-extension-form" });
        details.createEl("summary", { text: "安装结构化 Shell profile" });
        const executables = snapshot.shell.executables.filter((item) => item.allowedStdinModes.includes("closed"));
        if (!executables.length) {
            details.createEl("p", { text: "没有允许 closed stdin 的 executable。", cls: "setting-item-description" });
            return;
        }
        const id = this.input(details, "Profile ID", "lowercase-id");
        const description = this.input(details, "说明", "用途和审批语义");
        const executable = this.select(details, executables.map((item) => [item.executableId, item.executableId]));
        const cwd = this.select(details, []);
        const environment = this.select(details, []);
        const argumentsInput = this.textarea(details, "固定参数：每行一个参数（不会按命令行解析）");
        const risk = this.select(details, [
            ["execute", "execute"], ["write", "write"], ["network", "network"], ["destructive", "destructive"],
        ]);
        const effect = this.select(details, [
            ["execute", "execute"], ["write", "write"], ["network", "network"], ["destructive", "destructive"], ["unknown", "unknown"],
        ]);
        const synchronize = () => {
            const registration = executables.find((item) => item.executableId === executable.value);
            this.options(cwd, (registration?.allowedCwdRootIds ?? []).map((item) => [item, item]));
            this.options(environment, (registration?.environmentProfileIds ?? []).map((item) => [item, item]));
        };
        executable.addEventListener("change", synchronize);
        synchronize();
        details.appendChild(this.button("安装（随后需确认 hash）", () => {
            const registration = executables.find((item) => item.executableId === executable.value);
            if (!registration) throw new Error("请选择已注册 executable");
            const existing = snapshot.shell.profiles.find((item) => item.profileId === id.value.trim());
            const draft: ShellInstallDraft = {
                profileId: id.value.trim(), description: description.value.trim(),
                executableId: registration.executableId,
                executableProfileFingerprint: registration.fingerprint,
                executableFixedArguments: registration.fixedArguments,
                executableVariableArgumentPattern: registration.variableArgumentPattern,
                fixedArguments: lines(argumentsInput.value), cwdRootId: cwd.value,
                environmentProfileId: environment.value,
                risk: risk.value as ShellInstallDraft["risk"],
                sideEffectClass: effect.value as ShellInstallDraft["sideEffectClass"],
                allowNetwork: risk.value === "network" && registration.allowNetwork,
                expectedRevision: existing?.revision ?? 0,
            };
            return this.mutate("Shell profile 已安装，等待 hash 确认", (signal) => this.manager.installShell(draft, signal));
        }));
    }

    private renderHooks(container: HTMLElement, snapshot: ExtensionSnapshot): void {
        const section = container.createDiv({ cls: "offeragent-extension-section" });
        section.createEl("h4", { text: "Hooks" });
        new Setting(section)
            .setName("允许 Run 执行有效 Hook layers")
            .setDesc("Workspace command Hook 还需逐 definitionHash 确认。")
            .addToggle((toggle) => toggle
                .setValue(this.host.extensionExecutionEnabled("hooks"))
                .onChange((value) => this.host.setExtensionExecutionEnabled("hooks", value).catch((error) =>
                    this.showError(error, "无法更新 Hooks 执行开关"))));
        for (const layer of snapshot.hooks.layers) {
            const card = section.createDiv({ cls: "offeragent-extension-card" });
            card.createEl("strong", { text: `${layer.scope}:${layer.ownerId}` });
            card.createEl("p", {
                text: `${layer.trust} · layer revision ${layer.revision} · record revision ${layer.recordRevision}`,
                cls: "setting-item-description",
            });
            if (layer.trust === "confirmation_required" && (layer.scope === "user" || layer.scope === "session")) {
                card.appendChild(this.button("确认 layer contentHash", () => this.mutate(
                    "Hook layer 已确认", (signal) => this.manager.confirmHookLayer(layer, signal),
                )));
            }
            for (const hook of layer.hooks) {
                const line = card.createDiv({ cls: "offeragent-extension-hook" });
                line.createSpan({ text: `${hook.event} · ${hook.hookId} · ${hook.implementation}` });
                if (layer.scope === "workspace" && hook.implementation === "command" &&
                    layer.commandConfirmations[hook.hookId] !== hook.definitionHash) {
                    line.appendChild(this.button("确认 definitionHash", () => this.mutate(
                        "Workspace command Hook 已确认",
                        (signal) => this.manager.confirmWorkspaceCommand(
                            snapshot.hooks.workspaceId, layer, hook, signal,
                        ),
                    )));
                }
            }
        }
        this.renderHookInstaller(section, snapshot);
    }

    private renderHookInstaller(container: HTMLElement, snapshot: ExtensionSnapshot): void {
        const details = container.createEl("details", { cls: "offeragent-extension-form" });
        details.createEl("summary", { text: "安装单个结构化 Hook layer" });
        const scope = this.select(details, [["workspace", "Workspace"], ["user", "User"], ["session", "Session"]]);
        const owner = this.input(details, "Owner ID", snapshot.hooks.workspaceId);
        const hookId = this.input(details, "Hook ID", "hook-id");
        const event = this.select(details, HOOK_EVENTS.map((value) => [value, value]));
        const implementationOptions: [string, string][] = [];
        const commandExecutables = snapshot.shell.executables.filter((item) =>
            item.allowedStdinModes.includes("fixed_payload"));
        if (snapshot.hooks.builtinHandlerIds.length) implementationOptions.push(["builtin", "Builtin handler"]);
        if (commandExecutables.length) implementationOptions.push(["command", "Registered executable"]);
        if (!implementationOptions.length) {
            details.createEl("p", { text: "没有注册的 builtin handler 或 executable。", cls: "setting-item-description" });
            return;
        }
        const implementation = this.select(details, implementationOptions);
        const handler = this.select(details, snapshot.hooks.builtinHandlerIds.map((value) => [value, value]));
        const executable = this.select(details, commandExecutables.map((value) => [value.executableId, value.executableId]));
        const cwd = this.select(details, []);
        const environment = this.select(details, []);
        const argumentsInput = this.textarea(details, "参数：每行一个参数（不会按命令行解析）");
        const syncOwner = () => {
            if (scope.value === "workspace") owner.value = snapshot.hooks.workspaceId;
            else if (scope.value === "user") owner.value = snapshot.hooks.profileId;
            else owner.value = "";
        };
        const syncExecutable = () => {
            const registration = this.registration(snapshot, executable.value);
            this.options(cwd, (registration?.allowedCwdRootIds ?? []).map((value) => [value, value]));
            this.options(environment, (registration?.environmentProfileIds ?? []).map((value) => [value, value]));
        };
        scope.addEventListener("change", syncOwner);
        executable.addEventListener("change", syncExecutable);
        syncOwner();
        syncExecutable();
        details.appendChild(this.button("安装 layer", () => {
            const layer = snapshot.hooks.layers.find((item) => item.scope === scope.value && item.ownerId === owner.value.trim());
            const registration = this.registration(snapshot, executable.value);
            const command = implementation.value === "command";
            const draft: HookInstallDraft = {
                scope: scope.value as HookInstallDraft["scope"], ownerId: owner.value.trim(), hookId: hookId.value.trim(),
                event: hookEvent(event.value), implementation: implementation.value as HookInstallDraft["implementation"],
                handlerId: command ? null : handler.value,
                executableId: command ? registration?.executableId ?? null : null,
                executableProfileFingerprint: command ? registration?.fingerprint ?? null : null,
                executableFixedArguments: command ? registration?.fixedArguments ?? [] : [],
                arguments: command ? lines(argumentsInput.value) : [],
                cwdRootId: command ? cwd.value : "vault",
                environmentProfileId: command ? environment.value : "minimal",
                expectedRevision: layer?.recordRevision ?? 0,
                layerRevision: (layer?.revision ?? 0) + 1,
            };
            return this.mutate("Hook layer 已安装，等待相应信任确认", (signal) => this.manager.installHook(draft, signal));
        }));
    }

    private registration(snapshot: ExtensionSnapshot, executableId: string): ExecutableRegistrationView | undefined {
        return snapshot.shell.executables.find((item) =>
            item.executableId === executableId && item.allowedStdinModes.includes("fixed_payload"));
    }

    private async mutate(message: string, operation: (signal: AbortSignal) => Promise<void>): Promise<void> {
        this.setStatus("正在提交扩展管理变更…");
        try {
            await this.scope.run(operation);
            new Notice(message);
            await this.refresh();
        } catch (error) {
            this.showError(error, "扩展管理变更失败");
        }
    }

    private setStatus(message: string): void {
        if (this.statusEl) this.statusEl.textContent = message;
    }

    private showError(error: unknown, fallback: string): void {
        const message = error instanceof Error ? error.message : fallback;
        this.setStatus(message);
        new Notice(message);
    }

    private button(label: string, action: () => void | Promise<void>): HTMLButtonElement {
        const button = document.createElement("button");
        button.type = "button";
        button.textContent = label;
        button.addEventListener("click", () => {
            try {
                void Promise.resolve(action()).catch((error) => this.showError(error, `${label}失败`));
            } catch (error) {
                this.showError(error, `${label}失败`);
            }
        });
        return button;
    }

    private input(container: HTMLElement, label: string, placeholder: string): HTMLInputElement {
        const wrapper = container.createEl("label", { text: label, cls: "offeragent-extension-field" });
        const input = wrapper.createEl("input");
        input.type = "text";
        input.placeholder = placeholder;
        input.maxLength = 512;
        return input;
    }

    private textarea(container: HTMLElement, placeholder: string): HTMLTextAreaElement {
        const value = container.createEl("textarea", { cls: "offeragent-extension-arguments" });
        value.rows = 3;
        value.placeholder = placeholder;
        value.maxLength = 32_768;
        return value;
    }

    private select(container: HTMLElement, values: readonly (readonly [string, string])[]): HTMLSelectElement {
        const select = container.createEl("select");
        this.options(select, values);
        return select;
    }

    private options(select: HTMLSelectElement, values: readonly (readonly [string, string])[]): void {
        const previous = select.value;
        select.empty();
        for (const [value, label] of values) {
            const option = select.createEl("option", { text: label });
            option.value = value;
        }
        if (values.some(([value]) => value === previous)) select.value = previous;
    }
}

function hookEvent(value: string): HookInstallDraft["event"] {
    if (!(HOOK_EVENTS as readonly string[]).includes(value)) throw new Error("Hook event 无效");
    return value as HookInstallDraft["event"];
}

function lines(value: string): string[] {
    return value.split(/\r?\n/).map((item) => item.trim()).filter(Boolean);
}
