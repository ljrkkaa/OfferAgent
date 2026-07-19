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
import { PlanningMemoryAdapter } from "./planning_memory";
import { InterviewCatalogAdapter } from "./interview_catalog";
import type { ResearchBrowserAdapter } from "./research_browser";
import { VaultChangeCoordinator } from "./vault_changes";
import type { EventDeliveryOrigin } from "./event_reducer";

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
    subscribe(listener: (event: {
        readonly type: string;
        readonly payload: unknown;
        readonly runId?: unknown;
    }, state: unknown, origin: EventDeliveryOrigin) => void): () => void;
}

export interface PluginToolExecutionPort {
    execute(call: ExecutableToolCallDescriptor): Promise<PluginToolCompleteResult>;
    cancelRun?(runId: string): void;
}

export interface PluginToolEventObserver {
    /** Stops accepting events synchronously, then resolves after every accepted execution settles. */
    dispose(): Promise<void>;
}

const VAULT_FENCE_REGISTRY_KEY = "__offerAgentVaultToolExecutionFences_v1";
type VaultFenceRegistry = Map<string, Promise<void>>;

function vaultFenceRegistry(): VaultFenceRegistry {
    const host = globalThis as typeof globalThis & {
        [VAULT_FENCE_REGISTRY_KEY]?: VaultFenceRegistry;
    };
    host[VAULT_FENCE_REGISTRY_KEY] ??= new Map<string, Promise<void>>();
    return host[VAULT_FENCE_REGISTRY_KEY];
}

function canonicalFenceScope(scope: string): string {
    const canonical = scope.replace(/\\/gu, "/").replace(/\/+$/gu, "").toLocaleLowerCase();
    if (!canonical) throw new TypeError("Vault execution fence scope is required");
    return canonical;
}

function enqueueVaultExecution<T>(scope: string, operation: () => Promise<T>): Promise<T> {
    const registry = vaultFenceRegistry();
    const previous = registry.get(scope) ?? Promise.resolve();
    const execution = previous.catch(() => undefined).then(operation);
    const tail = execution.then(() => undefined, () => undefined);
    registry.set(scope, tail);
    void tail.then(() => {
        if (registry.get(scope) === tail) registry.delete(scope);
    });
    return execution;
}

/**
 * Serializes plugin Tool work per Vault across adapter replacement and plugin reloads in one Obsidian process.
 * Initialization is in the same queue, so recovery always follows the retiring adapter and precedes new work.
 */
export class SerializedPluginToolExecutionFence implements PluginToolExecutionPort {
    private readonly scope: string;
    private readonly initialization: Promise<void>;
    private readonly inFlight = new Set<Promise<unknown>>();

    constructor(
        scope: string,
        private readonly executor: PluginToolExecutionPort,
        initialize: () => Promise<void> = () => Promise.resolve(),
    ) {
        this.scope = canonicalFenceScope(scope);
        this.initialization = enqueueVaultExecution(this.scope, initialize);
    }

    ready(): Promise<void> {
        return this.initialization;
    }

    execute(call: ExecutableToolCallDescriptor): Promise<PluginToolCompleteResult> {
        return this.runExclusive(() => this.executor.execute(call));
    }

    runExclusive<T>(operation: () => Promise<T>): Promise<T> {
        const execution = enqueueVaultExecution(this.scope, async () => {
            await this.initialization;
            return operation();
        });
        this.inFlight.add(execution);
        const forget = (): void => { this.inFlight.delete(execution); };
        void execution.then(forget, forget);
        return execution;
    }

    async drain(): Promise<void> {
        await this.initialization.catch(() => undefined);
        await Promise.allSettled([...this.inFlight]);
    }

    cancelRun(runId: string): void {
        this.executor.cancelRun?.(runId);
    }
}

/** The sole Obsidian-API boundary for capabilities owned by the plugin. */
export class VaultToolAdapter {
    constructor(
        private readonly vault: VaultReadPort,
        private readonly client: PluginToolCompletionClient,
        private readonly workspaceId: string,
        metadata?: MetadataReadPort,
        controls?: VaultControlOptions,
        private readonly changes?: VaultChangeCoordinator,
        private readonly research?: ResearchBrowserAdapter,
    ) {
        if (!workspaceId) throw new TypeError("workspaceId is required");
        this.evidence = new VaultEvidenceAdapter(vault, workspaceId, metadata);
        this.control = new VaultControlAdapter(vault, workspaceId, controls);
        this.projects = new ProjectEvidenceAdapter(vault);
        this.planningMemory = new PlanningMemoryAdapter(vault, workspaceId);
        this.interviewCatalog = new InterviewCatalogAdapter(vault);
    }

    private readonly evidence: VaultEvidenceAdapter;
    private readonly control: VaultControlAdapter;
    private readonly projects: ProjectEvidenceAdapter;
    private readonly planningMemory: PlanningMemoryAdapter;
    private readonly interviewCatalog: InterviewCatalogAdapter;

    cancelRun(runId: string): void {
        this.research?.cancelRun(runId);
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
        if (![
            "agent_contract.read", "skill.read", "daily_note.context",
            "planning_memory.list", "planning_memory.read",
            "interview_catalog.search", "research_browser.navigate",
            "vault.list", "vault.search", "vault.read",
            "project.list", "project.search", "project.read",
            "vault.changes.apply",
        ].includes(call.name) ||
            call.version !== "1") {
            throw new Error(`unsupported plugin Tool: ${call.name}@${call.version}`);
        }
        if (call.name === "agent_contract.read" && Object.keys(call.arguments).length !== 0) {
            throw new Error("agent_contract.read accepts no arguments");
        }
    }

    private async executeBound(call: ExecutableToolCallDescriptor): Promise<ToolResultDescriptor> {
        if (call.name === "vault.changes.apply") {
            if (this.changes === undefined) throw new Error("Vault Change Coordinator is unavailable");
            return this.changes.execute(call);
        }
        if (call.name.startsWith("vault.")) return this.evidence.execute(call);
        if (call.name.startsWith("project.")) return this.projects.execute(call);
        if (call.name.startsWith("planning_memory.")) return this.planningMemory.execute(call);
        if (call.name.startsWith("interview_catalog.")) return this.interviewCatalog.execute(call);
        if (call.name.startsWith("research_browser.")) {
            if (this.research === undefined) throw new Error("Research Browser is unavailable");
            return this.research.execute(call);
        }
        return this.control.execute(call);
    }
}

export function observePluginToolEvents(
    source: PluginToolEventSource,
    executor: PluginToolExecutionPort,
    onError: (error: Error) => void,
): PluginToolEventObserver {
    const inFlight = new Set<Promise<void>>();
    let disposed = false;
    const unsubscribe = source.subscribe((event, _state, origin) => {
        if (origin === "replay") return;
        if (["turn.cancelled", "turn.failed", "turn.interrupted"].includes(event.type)) {
            if (typeof event.runId === "string") executor.cancelRun?.(event.runId);
            return;
        }
        if (event.type !== "tool.started") return;
        try {
            const call = executablePluginCall(event.payload);
            if (call === null) return;
            const execution = executor.execute(call)
                .catch((error) => onError(asError(error)))
                .then(() => undefined);
            inFlight.add(execution);
            const forget = (): void => { inFlight.delete(execution); };
            void execution.then(forget, forget);
        } catch (error) {
            onError(asError(error));
        }
    });
    return {
        dispose: async (): Promise<void> => {
            if (!disposed) {
                disposed = true;
                unsubscribe();
            }
            await Promise.allSettled([...inFlight]);
        },
    };
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
