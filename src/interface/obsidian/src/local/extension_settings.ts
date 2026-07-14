import { randomBytes } from "node:crypto";

import { JsonObject, JsonValue, requireJsonObject } from "../runtime/json_rpc";
import type {
    HookDefinitionInput,
    HookCommandInput,
    ProtocolCommandMethod,
    ProtocolCommandParams,
    ProtocolCommandResult,
} from "../runtime/generated_protocol";

const DIGEST = /^sha256:[0-9a-f]{64}$/;
const SKILL_NAME = /^[a-z][a-z0-9-]{0,63}$/;
const PROFILE_ID = /^[a-z][a-z0-9_-]{0,63}$/;
const PROCESS_ID = /^[a-z][a-z0-9_.-]{0,127}$/;
const ENVIRONMENT_NAME = /^[A-Za-z_][A-Za-z0-9_]{0,127}$/;
const CREDENTIAL_ARGUMENT = /(?:^|[-_/])(?:api[-_]?key|auth(?:orization)?|cookie|credential|password|secret|token)(?:$|[=:])/i;

export type ExtensionCommandMethod = Extract<
    ProtocolCommandMethod,
    `skills/${string}` | `shell/${string}` | `hooks/${string}` | `process/registrations/${string}`
>;

export interface ExtensionRequestClient {
    request<Method extends ExtensionCommandMethod>(
        method: Method,
        params: ProtocolCommandParams<Method>,
        options?: { signal?: AbortSignal; timeoutMs?: number },
    ): Promise<ProtocolCommandResult<Method>>;
}

export interface SkillView {
    readonly rootId: string;
    readonly packagePath: string;
    readonly layer: "builtin" | "user" | "workspace";
    readonly name: string;
    readonly description: string;
    readonly metadataHash: string;
    readonly trustState: "verified" | "confirmed" | "confirmation_required";
    readonly enabled: boolean;
}

export interface SkillStatusView {
    readonly revision: number;
    readonly snapshotHash: string;
    readonly discoveredCount: number;
    readonly enabledCount: number;
    readonly partial: boolean;
    readonly diagnostics: readonly { readonly severity: string; readonly code: string; readonly message: string }[];
}

export interface ShellProfileView {
    readonly profileId: string;
    readonly description: string;
    readonly executableId: string;
    readonly executableProfileFingerprint: string;
    readonly source: "signed_builtin" | "user";
    readonly trust: "signed" | "confirmed" | "confirmation_required";
    readonly enabled: boolean;
    readonly revision: number;
    readonly contentHash: string;
}

export interface ExecutableRegistrationView {
    readonly executableId: string;
    readonly fingerprint: string;
    readonly fixedArguments: readonly string[];
    readonly minimumVariableArguments: number;
    readonly maximumVariableArguments: number;
    readonly variableArgumentPattern: string;
    readonly allowedStdinModes: readonly ("closed" | "fixed_payload" | "duplex")[];
    readonly allowedCwdRootIds: readonly string[];
    readonly environmentProfileIds: readonly string[];
    readonly allowNetwork: boolean;
}

export interface EnvironmentRegistrationView {
    readonly profileId: string;
    readonly allowedNames: readonly string[];
}

export interface UserProcessExecutableView {
    readonly executableId: string;
    readonly revision: number;
    readonly contentHash: string;
    readonly canonicalPath: string;
    readonly trust: "fixed_hash" | "os_authenticode";
    readonly authenticodeVerified: boolean;
    readonly fileSha256: string;
    readonly profileFingerprint: string;
    readonly fixedArguments: readonly string[];
    readonly environmentProfileIds: readonly string[];
    readonly allowedStdinModes: readonly ("closed" | "fixed_payload" | "duplex")[];
    readonly allowedCwdRootIds: readonly string[];
    readonly available: boolean;
    readonly unavailableReason: string | null;
}

export interface UserProcessEnvironmentView {
    readonly profileId: string;
    readonly revision: number;
    readonly contentHash: string;
    readonly allowedNames: readonly string[];
    readonly allowedSecretNames: readonly string[];
    readonly available: boolean;
    readonly unavailableReason: string | null;
}

export interface ProcessCatalogView {
    readonly workspaceId: string;
    readonly catalogRevision: number;
    readonly activeCatalogRevision: number;
    readonly snapshotHash: string;
    readonly restartRequired: boolean;
    readonly executables: readonly UserProcessExecutableView[];
    readonly environments: readonly UserProcessEnvironmentView[];
}

export interface ProcessExecutableDraft {
    readonly executableId: string;
    readonly executablePath: string;
    readonly fixedArguments: readonly string[];
    readonly minimumVariableArguments: number;
    readonly maximumVariableArguments: number;
    readonly variableArgumentPattern: string;
    readonly environmentProfileIds: readonly string[];
    readonly allowedStdinModes: readonly ("closed" | "fixed_payload" | "duplex")[];
    readonly cwdRootId: string;
    readonly appContainerRelativePath: string;
}

export interface ProcessEnvironmentDraft {
    readonly profileId: string;
    readonly allowedNames: readonly string[];
    readonly allowedSecretNames: readonly string[];
}

export interface ProcessProbeView {
    readonly challengeId: string;
    readonly kind: "executable" | "environment";
    readonly registrationId: string;
    readonly contentHash: string;
    readonly expiresAt: string;
    readonly authenticodeVerified: boolean | null;
    readonly fileSha256: string | null;
    readonly canonicalPath: string | null;
    readonly fixedRoot: string | null;
    readonly trust: "fixed_hash" | "os_authenticode" | null;
    readonly fileIdentity: string | null;
    readonly fileSize: number | null;
    readonly profileFingerprint: string | null;
    readonly fixedArguments: readonly string[];
    readonly minimumVariableArguments: number | null;
    readonly maximumVariableArguments: number | null;
    readonly variableArgumentPattern: string | null;
    readonly environmentProfileIds: readonly string[];
    readonly allowedStdinModes: readonly ("closed" | "fixed_payload" | "duplex")[];
    readonly allowedCwdRootIds: readonly string[];
    readonly appcontainerFilesystem: readonly {
        readonly rootId: string;
        readonly relativePath: string;
        readonly access: "read" | "read_write";
    }[];
    readonly allowedEnvironmentNames: readonly string[];
    readonly allowedSecretEnvironmentNames: readonly string[];
}

export interface ShellCatalogView {
    readonly revision: number;
    readonly snapshotHash: string;
    readonly profiles: readonly ShellProfileView[];
    readonly executables: readonly ExecutableRegistrationView[];
    readonly environments: readonly EnvironmentRegistrationView[];
}

export interface HookDefinitionView {
    readonly hookId: string;
    readonly event: HookDefinitionInput["event"];
    readonly implementation: "builtin" | "command";
    readonly definitionHash: string;
}

export interface HookLayerView {
    readonly scope: "managed" | "user" | "workspace" | "session";
    readonly ownerId: string;
    readonly revision: number;
    readonly recordRevision: number;
    readonly trust: "signed" | "confirmed" | "confirmation_required" | "workspace_trust";
    readonly contentHash: string;
    readonly hooks: readonly HookDefinitionView[];
    readonly commandConfirmations: Readonly<Record<string, string>>;
}

export interface HookCatalogView {
    readonly workspaceId: string;
    readonly profileId: string;
    readonly revision: number;
    readonly snapshotHash: string;
    readonly layers: readonly HookLayerView[];
    readonly builtinHandlerIds: readonly string[];
}

export interface ExtensionSnapshot {
    readonly skills: readonly SkillView[];
    readonly skillStatus: SkillStatusView;
    readonly shell: ShellCatalogView;
    readonly hooks: HookCatalogView;
    readonly processes: ProcessCatalogView;
}

export interface ShellInstallDraft {
    readonly profileId: string;
    readonly description: string;
    readonly executableId: string;
    readonly executableProfileFingerprint: string;
    readonly executableFixedArguments: readonly string[];
    readonly executableVariableArgumentPattern: string;
    readonly fixedArguments: readonly string[];
    readonly cwdRootId: string;
    readonly environmentProfileId: string;
    readonly risk: "network" | "write" | "execute" | "destructive";
    readonly sideEffectClass: "network" | "write" | "execute" | "destructive" | "unknown";
    readonly allowNetwork: boolean;
    readonly expectedRevision: number;
}

export interface HookInstallDraft {
    readonly scope: "user" | "workspace" | "session";
    readonly ownerId: string;
    readonly hookId: string;
    readonly event: HookDefinitionInput["event"];
    readonly implementation: "builtin" | "command";
    readonly handlerId: string | null;
    readonly executableId: string | null;
    readonly executableProfileFingerprint: string | null;
    readonly executableFixedArguments: readonly string[];
    readonly arguments: readonly string[];
    readonly cwdRootId: string;
    readonly environmentProfileId: string;
    readonly expectedRevision: number;
    readonly layerRevision: number;
}

export class ExtensionRequestScope {
    private active: AbortController | null = null;

    async run<T>(operation: (signal: AbortSignal) => Promise<T>): Promise<T> {
        this.cancel();
        const controller = new AbortController();
        this.active = controller;
        try {
            return await operation(controller.signal);
        } finally {
            if (this.active === controller) this.active = null;
        }
    }

    cancel(): void {
        this.active?.abort();
        this.active = null;
    }
}

export class ExtensionRuntimeManager {
    constructor(private readonly client: ExtensionRequestClient) {}

    async snapshot(signal?: AbortSignal): Promise<ExtensionSnapshot> {
        const [skillsRaw, statusRaw, shellRaw, hooksRaw, processesRaw] = await Promise.all([
            this.client.request("skills/list", {}, { signal }),
            this.client.request("skills/status", {}, { signal }),
            this.client.request("shell/list", { includeDisabled: true }, { signal }),
            this.client.request("hooks/list", {}, { signal }),
            this.client.request("process/registrations/list", {}, { signal }),
        ]);
        const skillsResult = requireJsonObject(skillsRaw);
        const statusResult = requireJsonObject(statusRaw);
        return {
            skills: array(skillsResult.skills, "Skill list").map(parseSkill),
            skillStatus: parseSkillStatus(requireJsonObject(statusResult.status)),
            shell: parseShellCatalog(requireJsonObject(shellRaw)),
            hooks: parseHookCatalog(requireJsonObject(hooksRaw)),
            processes: parseProcessCatalog(requireJsonObject(processesRaw)),
        };
    }

    async probeProcessExecutable(
        draft: ProcessExecutableDraft,
        catalog: ProcessCatalogView,
        signal?: AbortSignal,
    ): Promise<ProcessProbeView> {
        validateProcessExecutableDraft(draft);
        const existing = catalog.executables.find((item) => item.executableId === draft.executableId);
        const result = requireJsonObject(await this.client.request("process/registrations/probe", {
            executable: {
                executableId: draft.executableId,
                executablePath: draft.executablePath,
                fixedArguments: [...draft.fixedArguments],
                minimumVariableArguments: draft.minimumVariableArguments,
                maximumVariableArguments: draft.maximumVariableArguments,
                variableArgumentPattern: draft.variableArgumentPattern,
                environmentProfileIds: [...draft.environmentProfileIds],
                allowedStdinModes: [...draft.allowedStdinModes],
                allowedCwdRootIds: [draft.cwdRootId],
                appcontainerFilesystem: [{
                    rootId: draft.cwdRootId,
                    relativePath: draft.appContainerRelativePath,
                    access: "read_write",
                }],
                allowNetwork: false,
                expectedRevision: existing?.revision ?? 0,
                expectedContentHash: existing?.contentHash ?? null,
            },
            environment: null,
        }, { signal, timeoutMs: 120_000 }));
        return parseProcessProbe(result);
    }

    async probeProcessEnvironment(
        draft: ProcessEnvironmentDraft,
        catalog: ProcessCatalogView,
        signal?: AbortSignal,
    ): Promise<ProcessProbeView> {
        validateProcessEnvironmentDraft(draft);
        const existing = catalog.environments.find((item) => item.profileId === draft.profileId);
        const result = requireJsonObject(await this.client.request("process/registrations/probe", {
            executable: null,
            environment: {
                profileId: draft.profileId,
                allowedNames: [...draft.allowedNames],
                allowedSecretNames: [...draft.allowedSecretNames],
                expectedRevision: existing?.revision ?? 0,
                expectedContentHash: existing?.contentHash ?? null,
            },
        }, { signal, timeoutMs: 120_000 }));
        return parseProcessProbe(result);
    }

    async confirmProcess(
        probe: ProcessProbeView,
        catalog: ProcessCatalogView,
        signal?: AbortSignal,
    ): Promise<void> {
        await this.client.request("process/registrations/confirm", {
            challengeId: probe.challengeId,
            expectedProbeContentHash: probe.contentHash,
            expectedCatalogRevision: catalog.catalogRevision,
            clientRequestId: requestId(),
        }, { signal, timeoutMs: 120_000 });
    }

    async deleteProcess(
        kind: "executable" | "environment",
        registration: UserProcessExecutableView | UserProcessEnvironmentView,
        catalog: ProcessCatalogView,
        signal?: AbortSignal,
    ): Promise<void> {
        await this.client.request("process/registrations/delete", {
            kind,
            registrationId: kind === "executable"
                ? (registration as UserProcessExecutableView).executableId
                : (registration as UserProcessEnvironmentView).profileId,
            expectedCatalogRevision: catalog.catalogRevision,
            expectedRevision: registration.revision,
            expectedContentHash: registration.contentHash,
            clientRequestId: requestId(),
        }, { signal });
    }

    async rescanSkills(status: SkillStatusView, signal?: AbortSignal): Promise<SkillStatusView> {
        const result = requireJsonObject(await this.client.request("skills/rescan", {
            clientRequestId: requestId(), expectedRevision: status.revision,
        }, { signal, timeoutMs: 120_000 }));
        return parseSkillStatus(requireJsonObject(result.status));
    }

    async confirmSkill(skill: SkillView, expectedRevision: number, confirmed: boolean, signal?: AbortSignal): Promise<void> {
        await this.client.request("skills/confirm-trust", {
            clientRequestId: requestId(),
            rootId: skill.rootId,
            packagePath: skill.packagePath,
            expectedMetadataHash: skill.metadataHash,
            expectedRevision,
            confirmed,
        }, { signal, timeoutMs: 120_000 });
    }

    async installShell(draft: ShellInstallDraft, signal?: AbortSignal): Promise<void> {
        validateShellDraft(draft);
        await this.client.request("shell/install", {
            clientRequestId: requestId(),
            expectedRevision: draft.expectedRevision,
            profile: {
                profileId: draft.profileId,
                description: draft.description,
                executableId: draft.executableId,
                executableProfileFingerprint: draft.executableProfileFingerprint,
                fixedArguments: [...draft.executableFixedArguments, ...draft.fixedArguments],
                minimumVariableArguments: 0,
                maximumVariableArguments: 0,
                variableArgumentPattern: draft.executableVariableArgumentPattern,
                cwdRootId: draft.cwdRootId,
                environmentProfileId: draft.environmentProfileId,
                timeoutMs: 30_000,
                inlineOutputLimitBytes: 65_536,
                artifactOutputLimitBytes: 16_777_216,
                allowNetwork: draft.allowNetwork,
                risk: draft.risk,
                sideEffectClass: draft.sideEffectClass,
                concurrencySafe: false,
                idempotent: false,
                retryable: false,
                version: "1.0.0",
            },
        }, { signal });
    }

    async confirmShell(profile: ShellProfileView, signal?: AbortSignal): Promise<void> {
        await this.client.request("shell/confirm", {
            clientRequestId: requestId(), profileId: profile.profileId,
            contentHash: profile.contentHash, expectedRevision: profile.revision,
        }, { signal });
    }

    async setShellEnabled(profile: ShellProfileView, enabled: boolean, signal?: AbortSignal): Promise<void> {
        await this.client.request("shell/set-enabled", {
            clientRequestId: requestId(), profileId: profile.profileId,
            enabled, expectedRevision: profile.revision,
        }, { signal });
    }

    async installHook(draft: HookInstallDraft, signal?: AbortSignal): Promise<void> {
        validateHookDraft(draft);
        let command: HookCommandInput | null = null;
        if (draft.implementation === "command") {
            if (!draft.executableId || !draft.executableProfileFingerprint) {
                throw new Error("Command Hook 需要注册 executable fingerprint");
            }
            command = {
                executableId: draft.executableId,
                arguments: [...draft.executableFixedArguments, ...draft.arguments],
                allowedEnvironment: [],
                executableProfileFingerprint: draft.executableProfileFingerprint,
                cwdRootId: draft.cwdRootId,
                cwd: "",
                environmentProfileId: draft.environmentProfileId,
                artifactOutputLimitBytes: 1_048_576,
            };
        }
        const definition: HookDefinitionInput = {
            hookId: draft.hookId,
            event: draft.event,
            implementation: draft.implementation,
            priority: 0,
            timeoutMs: 5_000,
            outputLimitBytes: 65_536,
            enabled: true,
            handlerId: draft.implementation === "builtin" ? draft.handlerId : null,
            command,
        };
        await this.client.request("hooks/install", {
            clientRequestId: requestId(), expectedRevision: draft.expectedRevision,
            layer: { scope: draft.scope, ownerId: draft.ownerId, revision: draft.layerRevision, hooks: [definition] },
        }, { signal });
    }

    async confirmHookLayer(layer: HookLayerView, signal?: AbortSignal): Promise<void> {
        if (layer.scope !== "user" && layer.scope !== "session") throw new Error("该 Hook layer 不使用整层确认");
        await this.client.request("hooks/confirm-layer", {
            clientRequestId: requestId(), scope: layer.scope, ownerId: layer.ownerId,
            contentHash: layer.contentHash, expectedRevision: layer.recordRevision,
        }, { signal });
    }

    async confirmWorkspaceCommand(
        workspaceId: string, layer: HookLayerView, hook: HookDefinitionView, signal?: AbortSignal,
    ): Promise<void> {
        if (layer.scope !== "workspace" || layer.ownerId !== workspaceId || hook.implementation !== "command") {
            throw new Error("Hook definition 不是当前 Workspace command");
        }
        await this.client.request("hooks/confirm-workspace-command", {
            clientRequestId: requestId(), ownerId: workspaceId, hookId: hook.hookId,
            definitionHash: hook.definitionHash, expectedRevision: layer.recordRevision,
        }, { signal });
    }
}

function parseSkill(raw: JsonValue): SkillView {
    const value = requireJsonObject(raw);
    const name = text(value.name, "Skill name");
    if (!SKILL_NAME.test(name)) throw new Error("Runtime returned invalid Skill name");
    return {
        rootId: text(value.rootId, "Skill root"),
        packagePath: text(value.packagePath, "Skill package path"),
        layer: oneOf(value.layer, ["builtin", "user", "workspace"] as const, "Skill layer"),
        name,
        description: text(value.description, "Skill description"),
        metadataHash: digest(value.metadataHash, "Skill metadata hash"),
        trustState: oneOf(value.trustState,
            ["verified", "confirmed", "confirmation_required"] as const, "Skill trust"),
        enabled: value.enabled === true,
    };
}

function parseSkillStatus(value: JsonObject): SkillStatusView {
    return {
        revision: integer(value.revision, "Skill revision", 0),
        snapshotHash: digest(value.snapshotHash, "Skill snapshot hash"),
        discoveredCount: integer(value.discoveredCount, "Skill discovered count", 0),
        enabledCount: integer(value.enabledCount, "Skill enabled count", 0),
        partial: value.partial === true,
        diagnostics: array(value.diagnostics, "Skill diagnostics").map((raw) => {
            const item = requireJsonObject(raw);
            return { severity: text(item.severity, "severity"), code: text(item.code, "code"), message: text(item.message, "message") };
        }),
    };
}

function parseShellCatalog(value: JsonObject): ShellCatalogView {
    return {
        revision: integer(value.revision, "Shell catalog revision", 1),
        snapshotHash: digest(value.snapshotHash, "Shell snapshot hash"),
        profiles: array(value.profiles, "Shell profiles").map((raw) => {
            const record = requireJsonObject(raw);
            const profile = requireJsonObject(record.profile);
            return {
                profileId: text(profile.profileId, "Shell profile ID"),
                description: text(profile.description, "Shell description"),
                executableId: text(profile.executableId, "Shell executable ID"),
                executableProfileFingerprint: digest(profile.executableProfileFingerprint, "Shell executable fingerprint"),
                source: oneOf(record.source, ["signed_builtin", "user"] as const, "Shell source"),
                trust: oneOf(record.trust, ["signed", "confirmed", "confirmation_required"] as const, "Shell trust"),
                enabled: record.enabled === true,
                revision: integer(record.revision, "Shell profile revision", 1),
                contentHash: digest(record.contentHash, "Shell content hash"),
            };
        }),
        executables: array(value.executables, "Shell executables").map((raw) => {
            const item = requireJsonObject(raw);
            return {
                executableId: text(item.executableId, "executable ID"),
                fingerprint: digest(item.fingerprint, "executable fingerprint"),
                fixedArguments: stringArray(item.fixedArguments, "executable fixed arguments"),
                minimumVariableArguments: integer(item.minimumVariableArguments, "minimum arguments", 0),
                maximumVariableArguments: integer(item.maximumVariableArguments, "maximum arguments", 0),
                variableArgumentPattern: text(item.variableArgumentPattern, "argument pattern"),
                allowedStdinModes: array(item.allowedStdinModes, "stdin modes").map((mode) =>
                    oneOf(mode, ["closed", "fixed_payload", "duplex"] as const, "stdin mode")),
                allowedCwdRootIds: stringArray(item.allowedCwdRootIds, "cwd roots"),
                environmentProfileIds: stringArray(item.environmentProfileIds, "environment profiles"),
                allowNetwork: item.allowNetwork === true,
            };
        }),
        environments: array(value.environments, "Shell environments").map((raw) => {
            const item = requireJsonObject(raw);
            return { profileId: text(item.profileId, "environment ID"), allowedNames: stringArray(item.allowedNames, "allowed names") };
        }),
    };
}

function parseHookCatalog(value: JsonObject): HookCatalogView {
    return {
        workspaceId: text(value.workspaceId, "Hook workspace"),
        profileId: text(value.profileId, "Hook profile"),
        revision: integer(value.revision, "Hook catalog revision", 1),
        snapshotHash: digest(value.snapshotHash, "Hook snapshot hash"),
        layers: array(value.layers, "Hook layers").map((raw) => {
            const item = requireJsonObject(raw);
            const confirmations = requireJsonObject(item.commandConfirmations);
            const commandConfirmations: Record<string, string> = {};
            for (const [key, rawDigest] of Object.entries(confirmations)) commandConfirmations[key] = digest(rawDigest, "Hook confirmation");
            return {
                scope: oneOf(item.scope, ["managed", "user", "workspace", "session"] as const, "Hook scope"),
                ownerId: text(item.ownerId, "Hook owner"),
                revision: integer(item.revision, "Hook layer revision", 0),
                recordRevision: integer(item.recordRevision, "Hook record revision", 1),
                trust: oneOf(item.trust, ["signed", "confirmed", "confirmation_required", "workspace_trust"] as const, "Hook trust"),
                contentHash: digest(item.contentHash, "Hook content hash"),
                hooks: array(item.hooks, "Hook definitions").map((rawHook) => {
                    const hook = requireJsonObject(rawHook);
                    return {
                        hookId: text(hook.hookId, "Hook ID"),
                        event: oneOf(hook.event, [
                            "SessionStart", "TurnStart", "BeforeModel", "AfterModel", "PreToolUse",
                            "PostToolUse", "ApprovalRequired", "SubagentStart", "SubagentStop",
                            "BeforeCompact", "TurnStop", "RuntimeShutdown",
                        ] as const, "Hook event"),
                        implementation: oneOf(hook.implementation, ["builtin", "command"] as const, "Hook implementation"),
                        definitionHash: digest(hook.definitionHash, "Hook definition hash"),
                    };
                }),
                commandConfirmations,
            };
        }),
        builtinHandlerIds: stringArray(value.builtinHandlerIds, "builtin Hook handlers"),
    };
}

function parseProcessCatalog(value: JsonObject): ProcessCatalogView {
    return {
        workspaceId: text(value.workspaceId, "Process workspace"),
        catalogRevision: integer(value.catalogRevision, "Process catalog revision", 0),
        activeCatalogRevision: integer(value.activeCatalogRevision, "active Process revision", 0),
        snapshotHash: digest(value.snapshotHash, "Process snapshot hash"),
        restartRequired: value.restartRequired === true,
        executables: array(value.executables, "Process executables").map((raw) => {
            const item = requireJsonObject(raw);
            return {
                executableId: text(item.executableId, "Process executable ID"),
                revision: integer(item.revision, "Process executable revision", 1),
                contentHash: digest(item.contentHash, "Process executable content hash"),
                canonicalPath: text(item.canonicalPath, "Process executable path"),
                trust: oneOf(item.trust, ["fixed_hash", "os_authenticode"] as const, "Process trust"),
                authenticodeVerified: item.authenticodeVerified === true,
                fileSha256: digest(item.fileSha256, "Process executable file hash"),
                profileFingerprint: digest(item.profileFingerprint, "Process profile fingerprint"),
                fixedArguments: stringArray(item.fixedArguments, "Process fixed arguments"),
                environmentProfileIds: stringArray(item.environmentProfileIds, "Process environments"),
                allowedStdinModes: array(item.allowedStdinModes, "Process stdin modes").map((mode) =>
                    oneOf(mode, ["closed", "fixed_payload", "duplex"] as const, "Process stdin mode")),
                allowedCwdRootIds: stringArray(item.allowedCwdRootIds, "Process cwd roots"),
                available: item.available === true,
                unavailableReason: item.unavailableReason === null ? null : text(item.unavailableReason, "unavailable reason"),
            };
        }),
        environments: array(value.environments, "Process environments").map((raw) => {
            const item = requireJsonObject(raw);
            return {
                profileId: text(item.profileId, "Process environment ID"),
                revision: integer(item.revision, "Process environment revision", 1),
                contentHash: digest(item.contentHash, "Process environment content hash"),
                allowedNames: stringArray(item.allowedNames, "Process plain environment names"),
                allowedSecretNames: stringArray(item.allowedSecretNames, "Process secret environment names"),
                available: item.available === true,
                unavailableReason: item.unavailableReason === null ? null : text(item.unavailableReason, "unavailable reason"),
            };
        }),
    };
}

function parseProcessProbe(value: JsonObject): ProcessProbeView {
    const kind = oneOf(value.kind, ["executable", "environment"] as const, "Process probe kind");
    const executable = kind === "executable" ? requireJsonObject(value.executable) : null;
    const environment = kind === "environment" ? requireJsonObject(value.environment) : null;
    if (kind === "executable" && value.environment !== null) throw new Error("Process probe mixed executable/environment");
    if (kind === "environment" && value.executable !== null) throw new Error("Process probe mixed executable/environment");
    const filesystem = executable === null ? [] : array(
        executable.appcontainerFilesystem,
        "Process AppContainer filesystem",
    ).map((raw) => {
        const item = requireJsonObject(raw);
        return {
            rootId: text(item.rootId, "Process AppContainer root"),
            relativePath: text(item.relativePath, "Process AppContainer relative path"),
            access: oneOf(item.access, ["read", "read_write"] as const, "Process AppContainer access"),
        };
    });
    return {
        challengeId: text(value.challengeId, "Process probe challenge"),
        kind,
        registrationId: text(value.registrationId, "Process registration ID"),
        contentHash: digest(value.contentHash, "Process probe content hash"),
        expiresAt: text(value.expiresAt, "Process probe expiry"),
        authenticodeVerified: executable === null ? null : executable.authenticodeVerified === true,
        fileSha256: executable === null ? null : digest(executable.fileSha256, "Process executable file hash"),
        canonicalPath: executable === null ? null : text(executable.canonicalPath, "Process canonical path"),
        fixedRoot: executable === null ? null : text(executable.fixedRoot, "Process fixed root"),
        trust: executable === null ? null : oneOf(
            executable.trust,
            ["fixed_hash", "os_authenticode"] as const,
            "Process executable trust",
        ),
        fileIdentity: executable === null ? null : `${text(executable.fileDevice, "Process file device")}:${text(
            executable.fileIndex,
            "Process file index",
        )}`,
        fileSize: executable === null ? null : integer(executable.fileSize, "Process file size", 1),
        profileFingerprint: executable === null ? null : digest(
            executable.profileFingerprint,
            "Process profile fingerprint",
        ),
        fixedArguments: executable === null ? [] : stringArray(executable.fixedArguments, "Process fixed arguments"),
        minimumVariableArguments: executable === null ? null : integer(
            executable.minimumVariableArguments,
            "Process minimum variable arguments",
            0,
        ),
        maximumVariableArguments: executable === null ? null : integer(
            executable.maximumVariableArguments,
            "Process maximum variable arguments",
            0,
        ),
        variableArgumentPattern: executable === null ? null : text(
            executable.variableArgumentPattern,
            "Process variable argument pattern",
        ),
        environmentProfileIds: executable === null ? [] : stringArray(
            executable.environmentProfileIds,
            "Process environment profiles",
        ),
        allowedStdinModes: executable === null ? [] : array(
            executable.allowedStdinModes,
            "Process stdin modes",
        ).map((mode) => oneOf(mode, ["closed", "fixed_payload", "duplex"] as const, "Process stdin mode")),
        allowedCwdRootIds: executable === null ? [] : stringArray(
            executable.allowedCwdRootIds,
            "Process cwd roots",
        ),
        appcontainerFilesystem: filesystem,
        allowedEnvironmentNames: environment === null ? [] : stringArray(
            environment.allowedNames,
            "Process plain environment names",
        ),
        allowedSecretEnvironmentNames: environment === null ? [] : stringArray(
            environment.allowedSecretNames,
            "Process secret environment names",
        ),
    };
}

function validateProcessExecutableDraft(value: ProcessExecutableDraft): void {
    if (!PROCESS_ID.test(value.executableId) ||
        !/^[A-Za-z]:\\(?!\\|[.?]\\).+\.exe$/i.test(value.executablePath) ||
        value.executablePath.includes("\0")) {
        throw new Error("可执行文件必须是本地绝对 .exe 路径，不能是 UNC/device path");
    }
    if (!value.environmentProfileIds.length || value.environmentProfileIds.some((item) => !PROCESS_ID.test(item)) ||
        !value.allowedStdinModes.length || !PROCESS_ID.test(value.cwdRootId) ||
        !value.appContainerRelativePath || /(^|[\\/])\.\.([\\/]|$)/.test(value.appContainerRelativePath) ||
        value.appContainerRelativePath.includes("\\")) {
        throw new Error("Process profile 的环境、stdin、cwd 或 AppContainer 窄路径无效");
    }
    if (!Number.isSafeInteger(value.minimumVariableArguments) || !Number.isSafeInteger(value.maximumVariableArguments) ||
        value.minimumVariableArguments < 0 || value.maximumVariableArguments > 128 ||
        value.minimumVariableArguments > value.maximumVariableArguments || !value.variableArgumentPattern ||
        value.variableArgumentPattern.length > 4096) {
        throw new Error("Process variable argv schema 无效");
    }
    try {
        new RegExp(value.variableArgumentPattern);
    } catch {
        throw new Error("Process variable argv regex 无效");
    }
    validateArguments(value.fixedArguments, "Process fixed arguments");
}

function validateProcessEnvironmentDraft(value: ProcessEnvironmentDraft): void {
    const names = [...value.allowedNames, ...value.allowedSecretNames];
    if (!PROCESS_ID.test(value.profileId) || names.some((item) => !ENVIRONMENT_NAME.test(item)) ||
        new Set(names.map((item) => item.toUpperCase())).size !== names.length) {
        throw new Error("Process 环境 Profile ID 或变量名无效/重复");
    }
}

function validateShellDraft(value: ShellInstallDraft): void {
    if (!PROFILE_ID.test(value.profileId) || !value.description.trim() || value.description.length > 512) {
        throw new Error("Shell profile ID/description 无效");
    }
    if (!PROFILE_ID.test(value.executableId) || !DIGEST.test(value.executableProfileFingerprint) ||
        !PROFILE_ID.test(value.cwdRootId) || !PROFILE_ID.test(value.environmentProfileId)) {
        throw new Error("Shell profile 必须绑定已注册 executable/cwd/environment profile");
    }
    validateArguments(value.fixedArguments, "Shell fixed arguments");
}

function validateHookDraft(value: HookInstallDraft): void {
    if (!value.ownerId || !value.hookId || value.ownerId.includes("\0") || value.hookId.includes("\0")) {
        throw new Error("Hook owner/ID 无效");
    }
    if (value.implementation === "builtin" && !value.handlerId) throw new Error("Builtin Hook 需要注册 handler");
    if (value.implementation === "command") {
        if (!value.executableId || !value.executableProfileFingerprint || !DIGEST.test(value.executableProfileFingerprint)) {
            throw new Error("Command Hook 需要注册 executable fingerprint");
        }
        validateArguments(value.arguments, "Hook arguments");
    }
}

function validateArguments(values: readonly string[], label: string): void {
    if (values.length > 128 || values.some((item) =>
        item.length > 4096 || /[\x00-\x1f\x7f&|<>^;`]/.test(item) || CREDENTIAL_ARGUMENT.test(item))) {
        throw new Error(`${label} 必须逐项填写，且不能含 shell/parser 元字符`);
    }
}

function requestId(): string {
    return `req_extension_${randomBytes(16).toString("hex")}`;
}

function text(value: JsonValue | undefined, label: string): string {
    if (typeof value !== "string" || !value || value.includes("\0")) throw new Error(`${label} is invalid`);
    return value;
}

function digest(value: JsonValue | undefined, label: string): string {
    const result = text(value, label);
    if (!DIGEST.test(result)) throw new Error(`${label} is invalid`);
    return result;
}

function integer(value: JsonValue | undefined, label: string, minimum: number): number {
    if (!Number.isSafeInteger(value) || (value as number) < minimum) throw new Error(`${label} is invalid`);
    return value as number;
}

function array(value: JsonValue | undefined, label: string): JsonValue[] {
    if (!Array.isArray(value)) throw new Error(`${label} is invalid`);
    return value;
}

function stringArray(value: JsonValue | undefined, label: string): string[] {
    return array(value, label).map((item) => text(item, label));
}

function oneOf<T extends string>(value: JsonValue | undefined, values: readonly T[], label: string): T {
    if (typeof value !== "string" || !values.includes(value as T)) throw new Error(`${label} is invalid`);
    return value as T;
}
