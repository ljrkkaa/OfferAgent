import { createHash } from "node:crypto";

import type { TFile } from "obsidian";

import type {
    ExecutableToolCallDescriptor,
    PluginToolCompleteParams,
    PluginToolCompleteResult,
    ProtocolCommandParams,
    ProtocolCommandResult,
    ToolResultDescriptor,
} from "./generated_protocol";

export interface VaultReadPort {
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
    ) {
        if (!workspaceId) throw new TypeError("workspaceId is required");
    }

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
        if (call.name !== "agent_contract.read" || call.version !== "1") {
            throw new Error(`unsupported plugin Tool: ${call.name}@${call.version}`);
        }
        if (Object.keys(call.arguments).length !== 0) throw new Error("agent_contract.read accepts no arguments");
    }

    private async executeBound(call: ExecutableToolCallDescriptor): Promise<ToolResultDescriptor> {
        const path = "agent.md";
        const file = this.vault.getFileByPath(path);
        if (file === null) {
            return failedResult(
                call.toolCallId,
                "resource.not_found",
                "当前 Vault 中没有 agent.md Agent Contract。",
                false,
                { path },
            );
        }
        try {
            const content = await this.vault.cachedRead(file);
            const contentHash = `sha256:${createHash("sha256").update(content, "utf8").digest("hex")}`;
            return {
                toolCallId: call.toolCallId,
                status: "succeeded",
                summary: "Read the Vault Agent Contract.",
                data: { path, content, contentHash },
                artifactRefs: [],
                sourceRefs: [{
                    type: "vault",
                    file: { workspaceId: this.workspaceId, path, contentHash },
                    freshness: "fresh",
                    label: "Vault Agent Contract",
                }],
                sideEffects: [],
                retryable: false,
                error: null,
            };
        } catch {
            return failedResult(
                call.toolCallId,
                "tool.failed",
                "无法通过 Obsidian Vault API 读取 agent.md。",
                true,
                { path },
            );
        }
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

function failedResult(
    toolCallId: string,
    code: "resource.not_found" | "tool.failed",
    message: string,
    retryable: boolean,
    details: Readonly<Record<string, string>>,
): ToolResultDescriptor {
    return {
        toolCallId,
        status: "failed",
        summary: message,
        data: {},
        artifactRefs: [],
        sourceRefs: [],
        sideEffects: [],
        retryable,
        error: {
            code,
            retryable,
            cancelled: false,
            userVisibleMessage: message,
            details,
            retryAfterMs: null,
            traceId: null,
        },
    };
}
