import type { TFile } from "obsidian";

import type {
    ExecutableToolCallDescriptor,
    PluginToolCompleteParams,
    PluginToolCompleteResult,
    ProtocolCommandParams,
    ProtocolCommandResult,
    ToolResultDescriptor,
} from "./generated_protocol";
import { MetadataReadPort, VaultEvidenceAdapter } from "./vault_evidence";
import { VaultControlAdapter, VaultControlOptions } from "./vault_control";
import { ProjectEvidenceAdapter } from "./project_evidence";

export interface VaultReadPort {
    getFiles(): TFile[];
    getFileByPath(path: string): TFile | null;
    cachedRead(file: TFile): Promise<string>;
}

export interface PluginToolCompletionClient {
    request(
        method: "plugin-tools/complete",
        params: ProtocolCommandParams<"plugin-tools/complete">,
        options?: { signal?: AbortSignal; timeoutMs?: number },
    ): Promise<ProtocolCommandResult<"plugin-tools/complete">>;
}

export interface PluginToolEventSource {
    subscribe(listener: (event: { readonly type: string; readonly payload: unknown }) => void): () => void;
}

export interface PluginToolExecutionPort {
    execute(call: ExecutableToolCallDescriptor): Promise<PluginToolCompleteResult>;
}

/** The sole Obsidian-API boundary for capabilities owned by the plugin. */
export class VaultToolAdapter {
    constructor(
        private readonly vault: VaultReadPort,
        private readonly client: PluginToolCompletionClient,
        private readonly workspaceId: string,
        metadata?: MetadataReadPort,
        controls?: VaultControlOptions,
    ) {
        if (!workspaceId) throw new TypeError("workspaceId is required");
        this.evidence = new VaultEvidenceAdapter(vault, workspaceId, metadata);
        this.control = new VaultControlAdapter(vault, workspaceId, controls);
        this.projects = new ProjectEvidenceAdapter(vault);
    }

    private readonly evidence: VaultEvidenceAdapter;
    private readonly control: VaultControlAdapter;
    private readonly projects: ProjectEvidenceAdapter;

    async execute(call: ExecutableToolCallDescriptor): Promise<PluginToolCompleteResult> {
        this.validateBinding(call);
        const result = await this.executeBound(call);
        const params: PluginToolCompleteParams = {
            workspaceId: call.workspaceId,
            runId: call.runId,
            definitionFingerprint: call.definitionFingerprint,
            argsHash: call.argsHash,
            idempotencyKey: call.idempotencyKey,
            result,
        };
        return this.client.request("plugin-tools/complete", params);
    }

    private validateBinding(call: ExecutableToolCallDescriptor): void {
        if (call.workspaceId !== this.workspaceId) throw new Error("plugin Tool call belongs to another Vault");
        if (call.executorLocation !== "plugin") throw new Error("Vault Tool Adapter accepts only plugin calls");
        if (![
            "agent_contract.read", "skill.read", "daily_note.context",
            "vault.list", "vault.search", "vault.read",
            "project.list", "project.search", "project.read",
        ].includes(call.name) ||
            call.version !== "1") {
            throw new Error(`unsupported plugin Tool: ${call.name}@${call.version}`);
        }
        if (call.name === "agent_contract.read" && Object.keys(call.arguments).length !== 0) {
            throw new Error("agent_contract.read accepts no arguments");
        }
    }

    private async executeBound(call: ExecutableToolCallDescriptor): Promise<ToolResultDescriptor> {
        if (call.name.startsWith("vault.")) return this.evidence.execute(call);
        if (call.name.startsWith("project.")) return this.projects.execute(call);
        return this.control.execute(call);
    }
}

export function observePluginToolEvents(
    source: PluginToolEventSource,
    executor: PluginToolExecutionPort,
    onError: (error: Error) => void,
): () => void {
    return source.subscribe((event) => {
        if (event.type !== "tool.started") return;
        try {
            const call = executablePluginCall(event.payload);
            if (call === null) return;
            void executor.execute(call).catch((error) => onError(asError(error)));
        } catch (error) {
            onError(asError(error));
        }
    });
}

function executablePluginCall(payload: unknown): ExecutableToolCallDescriptor | null {
    const envelope = record(payload, "tool.started payload");
    const candidate = record(envelope.call, "tool.started call");
    if (candidate.executorLocation !== "plugin") return null;
    for (const field of [
        "toolCallId", "workspaceId", "runId", "name", "version", "argsHash", "idempotencyKey",
        "definitionFingerprint", "resultSensitivity", "risk",
    ]) {
        if (typeof candidate[field] !== "string" || candidate[field] === "") {
            throw new TypeError(`tool.started ${field} is invalid`);
        }
    }
    record(candidate.arguments, "tool.started arguments");
    if (!Array.isArray(candidate.agentLineage) || candidate.agentLineage.length === 0 ||
        candidate.agentLineage.some((item) => typeof item !== "string" || item === "")) {
        throw new TypeError("tool.started agentLineage is invalid");
    }
    if (candidate.deadline !== null && candidate.deadline !== undefined && typeof candidate.deadline !== "string") {
        throw new TypeError("tool.started deadline is invalid");
    }
    return candidate as unknown as ExecutableToolCallDescriptor;
}

function record(value: unknown, label: string): Record<string, unknown> {
    if (value === null || typeof value !== "object" || Array.isArray(value)) {
        throw new TypeError(`${label} is not an object`);
    }
    return value as Record<string, unknown>;
}

function asError(value: unknown): Error {
    return value instanceof Error ? value : new Error(String(value));
}
