export { RuntimeSupervisor } from "./runtime-supervisor";

import { randomUUID } from "node:crypto";
import type {
  AgentRunRecord,
  ConversationSummary,
  ModelDescriptor,
  ProviderErrorCode,
  ToolCallRecord,
  VaultChangeBatchProposal,
  VaultUndoResultPayload,
  VaultUndoConflict,
  LocalToolResultPayload,
  VaultToolErrorCode,
  WebCitation,
} from "@offeragent/protocol";
import { RuntimeRequestError, type RuntimeClient } from "./runtime-supervisor";
import type { VaultPermissionMode } from "./vault-change-coordinator";

export type RuntimeViewState = "connected" | "idle" | "starting" | "unavailable";

export interface SidebarViewModel {
  conversation: {
    activeConversationId?: string;
    agentRuns: AgentRunRecord[];
    conversations: ConversationSummary[];
    error?: { code: ProviderErrorCode | VaultToolErrorCode; message: string };
    messages: Array<{
      agentRunId: string;
      citations?: WebCitation[];
      role: "assistant" | "user";
      text: string;
    }>;
    models: ModelDescriptor[];
    runState: "idle" | "streaming";
    selectedModelId?: string;
    toolCalls: ToolCallRecord[];
    vaultChanges: Array<{
      actions: Array<{ operation: string; path: string }>;
      batchId: string;
      conflicts?: VaultUndoConflict[];
      message?: string;
      status: "applied" | "applying" | "conflicted" | "expired" | "failed" | "pending" | "rejected" | "rejecting" | "undone";
      task: string;
      toolCallId: string;
    }>;
  };
  runtime: {
    message?: string;
    state: RuntimeViewState;
  };
  presentation: {
    activities: Array<{
      action: string;
      details: string;
      id: string;
      label: string;
      status: ToolCallRecord["status"];
      target?: string;
    }>;
    composer: {
      contextChips: Array<{ kind: "scope"; label: string }>;
      permissionMode: VaultPermissionMode;
      primaryAction: {
        agentRunId?: string;
        kind: "resume" | "send" | "stop";
        label: "Resume" | "Send" | "Stop";
      };
    };
    settings: {
      advanced: {
        diagnostics: string;
        gitRetention: string;
        hostedWebSearch: string;
      };
      fastMode?: { enabled: boolean };
      model?: { id: string; label: string };
      permissionMode: VaultPermissionMode;
      providerStatus: "connected" | "unavailable";
      runtimeStatus: RuntimeViewState;
    };
    transcript: Array<
      | {
          kind: "activity";
          activity: SidebarViewModel["presentation"]["activities"][number];
        }
      | {
          kind: "message";
          message: SidebarViewModel["conversation"]["messages"][number];
        }
      | {
          kind: "run_status";
          agentRunId: string;
          label: string;
          message?: string;
          status: Exclude<AgentRunRecord["status"], "completed" | "running">;
        }
      | {
          kind: "vault_change";
          change: SidebarViewModel["conversation"]["vaultChanges"][number];
        }
    >;
  };
  title: "OfferAgent";
}

type Subscriber = (viewModel: SidebarViewModel) => void;

export interface SidebarEnvironment {
  getFastModeEnabled(): boolean;
  getVaultPermissionMode(): VaultPermissionMode;
}

const DEFAULT_ENVIRONMENT: SidebarEnvironment = {
  getFastModeEnabled: () => false,
  getVaultPermissionMode: () => "trusted_vault",
};

const TOOL_ACTIONS: Record<ToolCallRecord["name"], string> = {
  agent_contract_read: "Read contract",
  daily_note_context: "Resolve Daily Note",
  planning_memory_list: "Scan Planning Memory",
  planning_memory_read: "Recall Planning Memory",
  hosted_web_search_probe: "Probe web search",
  skill_read: "Read skill",
  vault_list: "List",
  vault_propose_changes: "Change Vault",
  vault_read: "Read",
  vault_search: "Search",
  web_read: "Read web page",
};

function toolTarget(call: ToolCallRecord): string | undefined {
  if (!call.arguments || typeof call.arguments !== "object" || Array.isArray(call.arguments)) {
    return call.name === "agent_contract_read" ? "agent.md" : undefined;
  }
  const arguments_ = call.arguments as Record<string, unknown>;
  if (call.name === "skill_read" && typeof arguments_.skill === "string") {
    return typeof arguments_.resource === "string"
      ? `${arguments_.skill}/${arguments_.resource}`
      : arguments_.skill;
  }
  for (const key of ["path", "directory", "query", "url"]) {
    if (typeof arguments_[key] === "string" && arguments_[key]) return arguments_[key];
  }
  if (call.name === "agent_contract_read") return "agent.md";
  if (call.name === "vault_list") return "Vault";
  if (call.name === "hosted_web_search_probe") return "active model";
  return undefined;
}

function providerHealthFailure(code: ProviderErrorCode): boolean {
  return code === "auth_required" || code === "provider_error" || code === "transport_error";
}

export interface VaultChangeDecisionClient {
  acknowledge?(toolCallId: string): Promise<void>;
  cancel(toolCallId: string): void;
  decide(toolCallId: string, decision: "apply" | "reject"): Promise<LocalToolResultPayload>;
  rehydrate?(toolCallId: string): Promise<{
    proposal?: VaultChangeBatchProposal;
    result?: LocalToolResultPayload;
  }>;
  undo(batchId: string): Promise<VaultUndoResultPayload>;
}

export class SidebarController {
  readonly #environment: SidebarEnvironment;
  readonly #runtime: RuntimeClient;
  readonly #vaultChanges?: VaultChangeDecisionClient;
  readonly #subscribers = new Set<Subscriber>();
  #viewModel: Omit<SidebarViewModel, "presentation"> = {
    title: "OfferAgent",
    conversation: {
      agentRuns: [],
      conversations: [],
      messages: [],
      models: [],
      runState: "idle",
      toolCalls: [],
      vaultChanges: [],
    },
    runtime: { state: "idle" },
  };
  #activeRun?: { agentRunId: string; conversationId: string };
  #providerStatus: "connected" | "unavailable" = "unavailable";
  readonly #recoveredToolResults = new Map<string, {
    eventId: string;
    result: LocalToolResultPayload;
    toolCallId: string;
  }>();

  constructor(
    runtime: RuntimeClient,
    vaultChanges?: VaultChangeDecisionClient,
    environment: SidebarEnvironment = DEFAULT_ENVIRONMENT,
  ) {
    this.#environment = environment;
    this.#runtime = runtime;
    this.#vaultChanges = vaultChanges;
    this.#runtime.onUnavailable((message) => {
      this.#providerStatus = "unavailable";
      this.#update({ state: "unavailable", message });
    });
  }

  getViewModel(): SidebarViewModel {
    return { ...this.#viewModel, presentation: this.#presentation() };
  }

  subscribe(subscriber: Subscriber): () => void {
    this.#subscribers.add(subscriber);
    subscriber(this.getViewModel());
    return () => this.#subscribers.delete(subscriber);
  }

  refreshPresentation(): void {
    for (const subscriber of this.#subscribers) subscriber(this.getViewModel());
  }

  async start(): Promise<void> {
    this.#update({ state: "starting" });
    try {
      await this.#runtime.start();
      this.#update({ state: "connected" });
      try {
        const models = await this.#runtime.listModels();
        this.#providerStatus = "connected";
        let conversations = await this.#runtime.listConversations();
        if (conversations.length === 0 && models[0]) {
          conversations = [
            await this.#runtime.createConversation({
              id: randomUUID(),
              title: "New Conversation",
              modelId: models[0].id,
            }),
          ];
        }
        const active = conversations[0];
        const snapshot = active ? await this.#runtime.openConversation(active.id) : undefined;
        this.#recoveredToolResults.clear();
        if (snapshot) await this.#rehydratePendingVaultChanges(snapshot.toolCalls ?? []);
        const toolCalls = this.#toolCallsWithRecoveredFailures(snapshot?.toolCalls ?? []);
        this.#updateConversation({
          ...this.#viewModel.conversation,
          models,
          conversations,
          activeConversationId: snapshot?.conversation.id,
          selectedModelId: snapshot?.conversation.modelId ?? models[0]?.id,
          messages:
            snapshot?.messages.map(({ agentRunId, role, text, citations }) => ({
              agentRunId,
              role,
              text,
              ...(citations ? { citations } : {}),
            })) ?? [],
          agentRuns: snapshot?.agentRuns ?? [],
          toolCalls,
          vaultChanges: this.#changesFromToolCalls(toolCalls),
          error: undefined,
        });
      } catch (error) {
        if (error instanceof RuntimeRequestError) {
          this.#providerStatus = providerHealthFailure(error.code) ? "unavailable" : "connected";
        }
        const message = error instanceof Error ? error.message : String(error);
        this.#updateConversation({
          ...this.#viewModel.conversation,
          error: {
            code: error instanceof RuntimeRequestError ? error.code : "provider_error",
            message,
          },
        });
      }
    } catch (error) {
      this.#providerStatus = "unavailable";
      const message = error instanceof Error ? error.message : String(error);
      this.#update({ state: "unavailable", message });
      throw error;
    }
  }

  async createConversation(title = "New Conversation"): Promise<void> {
    const modelId = this.#viewModel.conversation.selectedModelId;
    if (!modelId) throw new Error("Choose an available model before creating a Conversation.");
    const conversation = await this.#runtime.createConversation({
      id: randomUUID(),
      title,
      modelId,
    });
    this.#recoveredToolResults.clear();
    this.#updateConversation({
      ...this.#viewModel.conversation,
      activeConversationId: conversation.id,
      agentRuns: [],
      conversations: [conversation, ...this.#viewModel.conversation.conversations],
      messages: [],
      runState: "idle",
      selectedModelId: conversation.modelId,
      toolCalls: [],
      vaultChanges: [],
      error: undefined,
    });
  }

  async openConversation(conversationId: string): Promise<void> {
    if (this.#viewModel.conversation.runState === "streaming") {
      throw new Error("Stop the current Agent Run before switching Conversations.");
    }
    const snapshot = await this.#runtime.openConversation(conversationId);
    this.#recoveredToolResults.clear();
    await this.#rehydratePendingVaultChanges(snapshot.toolCalls ?? []);
    const toolCalls = this.#toolCallsWithRecoveredFailures(snapshot.toolCalls ?? []);
    this.#updateConversation({
      ...this.#viewModel.conversation,
      activeConversationId: snapshot.conversation.id,
      agentRuns: snapshot.agentRuns,
      messages: snapshot.messages.map(({ agentRunId, role, text, citations }) => ({
        agentRunId,
        role,
        text,
        ...(citations ? { citations } : {}),
      })),
      runState: "idle",
      selectedModelId: snapshot.conversation.modelId,
      toolCalls,
      vaultChanges: this.#changesFromToolCalls(toolCalls),
      error: undefined,
    });
  }

  async deleteCurrentConversation(): Promise<void> {
    const conversationId = this.#viewModel.conversation.activeConversationId;
    if (!conversationId) return;
    if (this.#viewModel.conversation.runState === "streaming") {
      throw new Error("Stop the current Agent Run before deleting its Conversation.");
    }
    const proposalCalls = this.#viewModel.conversation.toolCalls.filter(
      (call) => call.name === "vault_propose_changes",
    );
    for (const call of proposalCalls) {
      if (call.name !== "vault_propose_changes") continue;
      if (call.status === "requested") this.#vaultChanges?.cancel(call.id);
      await this.#vaultChanges?.acknowledge?.(call.id);
    }
    await this.#runtime.deleteConversation(conversationId);
    this.#recoveredToolResults.clear();
    const conversations = this.#viewModel.conversation.conversations.filter(
      (conversation) => conversation.id !== conversationId,
    );
    this.#updateConversation({
      ...this.#viewModel.conversation,
      activeConversationId: undefined,
      agentRuns: [],
      conversations,
      messages: [],
      runState: "idle",
      toolCalls: [],
      vaultChanges: [],
      error: undefined,
    });
    if (conversations[0]) await this.openConversation(conversations[0].id);
    else await this.createConversation();
  }

  async selectModel(modelId: string): Promise<void> {
    if (!this.#viewModel.conversation.models.some((model) => model.id === modelId)) {
      throw new Error(`The selected model '${modelId}' is unavailable.`);
    }
    this.#updateConversation({
      ...this.#viewModel.conversation,
      selectedModelId: modelId,
      error: undefined,
    });
    const conversationId = this.#viewModel.conversation.activeConversationId;
    if (!conversationId) return;
    try {
      const updated = await this.#runtime.updateConversationModel(conversationId, modelId);
      this.#updateConversation({
        ...this.#viewModel.conversation,
        conversations: this.#viewModel.conversation.conversations.map((conversation) =>
          conversation.id === updated.id ? updated : conversation,
        ),
      });
    } catch (error) {
      this.#updateConversation({
        ...this.#viewModel.conversation,
        error: {
          code: "provider_error",
          message: error instanceof Error ? error.message : String(error),
        },
      });
    }
  }

  async sendMessage(input: string): Promise<void> {
    const text = input.trim();
    const selectedModelId = this.#viewModel.conversation.selectedModelId;
    const conversationId = this.#viewModel.conversation.activeConversationId;
    if (!text) return;
    if (!selectedModelId) throw new Error("Choose an available model before sending a message.");
    if (!conversationId) throw new Error("Create a Conversation before sending a message.");
    if (this.#viewModel.conversation.runState === "streaming") {
      throw new Error("Wait for the current Agent Run to finish.");
    }
    const agentRunId = randomUUID();
    const messages = [
      ...this.#viewModel.conversation.messages,
      { agentRunId, role: "user" as const, text },
      { agentRunId, role: "assistant" as const, text: "" },
    ];
    const agentRuns = [
      ...this.#viewModel.conversation.agentRuns,
      { id: agentRunId, modelId: selectedModelId, status: "running" as const },
    ];
    this.#activeRun = { agentRunId, conversationId };
    this.#updateConversation({
      ...this.#viewModel.conversation,
      agentRuns,
      messages,
      runState: "streaming",
      error: undefined,
    });

    try {
      for await (const event of this.#runtime.runAgent({
        conversationId,
        agentRunId,
        model: selectedModelId,
        ...(this.#viewModel.conversation.models.find(({ id }) => id === selectedModelId)
          ?.supportsFastMode && this.#environment.getFastModeEnabled()
          ? { fastMode: true }
          : {}),
        input: text,
      })) {
        if (event.type === "agent_run.started") {
          this.#providerStatus = "connected";
          this.refreshPresentation();
        } else if (event.type === "agent_run.delta") {
          messages[messages.length - 1] = {
            agentRunId,
            role: "assistant",
            text: messages[messages.length - 1].text + event.delta,
          };
          this.#updateConversation({ ...this.#viewModel.conversation, messages: [...messages] });
        } else if (event.type === "agent_run.completed") {
          messages[messages.length - 1] = { agentRunId, ...event.output };
          this.#setRunStatus(agentRunId, "completed");
        } else if (event.type === "tool_call.requested") {
          const requestedChange = this.#requestedChange(event.toolCallId, event.tool.name, event.tool.arguments);
          this.#updateConversation({
            ...this.#viewModel.conversation,
            toolCalls: [
              ...this.#viewModel.conversation.toolCalls,
              {
                id: event.toolCallId,
                agentRunId,
                name: event.tool.name,
                arguments: event.tool.arguments,
                status: "requested",
              },
            ],
            vaultChanges: requestedChange
              ? [...this.#viewModel.conversation.vaultChanges, requestedChange]
              : this.#viewModel.conversation.vaultChanges,
          });
        } else if (event.type === "tool_call.completed") {
          if (event.tool.name === "vault_propose_changes") {
            await this.#vaultChanges?.acknowledge?.(event.toolCallId);
          }
          const vaultChanges =
            event.status === "failed"
              ? this.#viewModel.conversation.vaultChanges.map((change) =>
                  change.toolCallId === event.toolCallId
                    ? { ...change, status: "failed" as const, message: event.error?.message }
                    : change,
                )
              : this.#viewModel.conversation.vaultChanges.map((change) =>
                  change.toolCallId === event.toolCallId && change.status === "pending"
                    ? { ...change, status: "applied" as const }
                    : change,
                );
          this.#updateConversation({
            ...this.#viewModel.conversation,
            toolCalls: this.#viewModel.conversation.toolCalls.map((call) =>
              call.id === event.toolCallId
                ? {
                    ...call,
                    status: event.status,
                    ...(event.error ? { error: event.error } : {}),
                  }
                : call,
            ),
            vaultChanges,
            error:
              event.status === "failed" && event.error
                ? { code: event.error.code, message: event.error.message }
                : this.#viewModel.conversation.error,
          });
        } else if (event.type === "agent_run.failed") {
          if (providerHealthFailure(event.error.code)) this.#providerStatus = "unavailable";
          const vaultChanges = this.#cancelPendingVaultChanges(agentRunId);
          messages.pop();
          this.#updateConversation({
            ...this.#viewModel.conversation,
            messages: [...messages],
            runState: "idle",
            agentRuns: this.#runsWithStatus(agentRunId, "failed", event.error),
            toolCalls: this.#terminalizedToolCalls(agentRunId),
            vaultChanges,
            error: event.error,
          });
        } else if (event.type === "agent_run.cancelled" || event.type === "agent_run.interrupted") {
          const cancelled = event.type === "agent_run.cancelled";
          const vaultChanges = cancelled
            ? this.#cancelPendingVaultChanges(agentRunId)
            : this.#viewModel.conversation.vaultChanges;
          messages.pop();
          this.#updateConversation({
            ...this.#viewModel.conversation,
            messages: [...messages],
            runState: "idle",
            toolCalls: cancelled
              ? this.#terminalizedToolCalls(agentRunId)
              : this.#interruptedToolCalls(agentRunId),
            vaultChanges,
            agentRuns: this.#runsWithStatus(
              agentRunId,
              cancelled ? "cancelled" : "interrupted",
            ),
          });
        }
      }
      this.#updateConversation({
        ...this.#viewModel.conversation,
        messages: [...messages],
        runState: "idle",
      });
    } catch (error) {
      this.#providerStatus = "unavailable";
      messages.pop();
      const message = error instanceof Error ? error.message : String(error);
      this.#updateConversation({
        ...this.#viewModel.conversation,
        messages: [...messages],
        runState: "idle",
        toolCalls: this.#interruptedToolCalls(agentRunId),
        vaultChanges: this.#viewModel.conversation.vaultChanges,
        agentRuns: this.#runsWithStatus(agentRunId, "interrupted"),
        error: { code: "transport_error", message },
      });
    } finally {
      if (this.#activeRun?.agentRunId === agentRunId) this.#activeRun = undefined;
    }
  }

  async resumeAgentRun(agentRunId: string): Promise<void> {
    await this.#resumeAgentRun(agentRunId, this.#recoveredToolResults.get(agentRunId));
  }

  async #resumeAgentRun(
    agentRunId: string,
    recoveredToolResult?: {
      eventId: string;
      result: LocalToolResultPayload;
      toolCallId: string;
    },
  ): Promise<void> {
    const conversationId = this.#viewModel.conversation.activeConversationId;
    if (!conversationId) throw new Error("Create a Conversation before resuming an Agent Run.");
    if (this.#viewModel.conversation.runState === "streaming") {
      throw new Error("Wait for the current Agent Run to finish.");
    }
    const run = this.#viewModel.conversation.agentRuns.find((candidate) => candidate.id === agentRunId);
    if (!run || run.status !== "interrupted") {
      throw new Error(`Agent Run '${agentRunId}' is not Interrupted.`);
    }
    const messages = [
      ...this.#viewModel.conversation.messages,
      { agentRunId, role: "assistant" as const, text: "" },
    ];
    this.#activeRun = { agentRunId, conversationId };
    this.#updateConversation({
      ...this.#viewModel.conversation,
      agentRuns: this.#runsWithStatus(agentRunId, "running"),
      messages,
      runState: "streaming",
      error: undefined,
    });
    try {
      for await (const event of this.#runtime.resumeAgentRun({
        conversationId,
        agentRunId,
        ...(recoveredToolResult ? { recoveredToolResult } : {}),
      })) {
        if (event.type === "agent_run.resumed") {
          this.#providerStatus = "connected";
          this.refreshPresentation();
        } else if (event.type === "agent_run.delta") {
          messages[messages.length - 1] = {
            agentRunId,
            role: "assistant",
            text: messages[messages.length - 1].text + event.delta,
          };
          this.#updateConversation({ ...this.#viewModel.conversation, messages: [...messages] });
        } else if (event.type === "agent_run.completed") {
          messages[messages.length - 1] = { agentRunId, ...event.output };
          this.#recoveredToolResults.delete(agentRunId);
          this.#setRunStatus(agentRunId, "completed");
        } else if (event.type === "tool_call.requested") {
          const requestedChange = this.#requestedChange(
            event.toolCallId,
            event.tool.name,
            event.tool.arguments,
          );
          this.#updateConversation({
            ...this.#viewModel.conversation,
            toolCalls: [
              ...this.#viewModel.conversation.toolCalls,
              {
                id: event.toolCallId,
                agentRunId,
                name: event.tool.name,
                arguments: event.tool.arguments,
                status: "requested",
              },
            ],
            vaultChanges: requestedChange
              ? [...this.#viewModel.conversation.vaultChanges, requestedChange]
              : this.#viewModel.conversation.vaultChanges,
          });
        } else if (event.type === "tool_call.completed") {
          if (event.tool.name === "vault_propose_changes") {
            await this.#vaultChanges?.acknowledge?.(event.toolCallId);
          }
          if (this.#recoveredToolResults.get(agentRunId)?.toolCallId === event.toolCallId) {
            this.#recoveredToolResults.delete(agentRunId);
          }
          this.#updateConversation({
            ...this.#viewModel.conversation,
            toolCalls: this.#viewModel.conversation.toolCalls.map((call) =>
              call.id === event.toolCallId
                ? {
                    ...call,
                    status: event.status,
                    ...(event.error ? { error: event.error } : {}),
                  }
                : call
            ),
            error:
              event.status === "failed" && event.error
                ? { code: event.error.code, message: event.error.message }
                : this.#viewModel.conversation.error,
          });
        } else if (event.type === "agent_run.failed") {
          if (providerHealthFailure(event.error.code)) this.#providerStatus = "unavailable";
          this.#recoveredToolResults.delete(agentRunId);
          messages.pop();
          this.#updateConversation({
            ...this.#viewModel.conversation,
            messages: [...messages],
            agentRuns: this.#runsWithStatus(agentRunId, "failed", event.error),
            runState: "idle",
            error: event.error,
          });
        } else if (event.type === "agent_run.cancelled" || event.type === "agent_run.interrupted") {
          const cancelled = event.type === "agent_run.cancelled";
          if (cancelled) this.#recoveredToolResults.delete(agentRunId);
          messages.pop();
          this.#updateConversation({
            ...this.#viewModel.conversation,
            messages: [...messages],
            agentRuns: this.#runsWithStatus(agentRunId, cancelled ? "cancelled" : "interrupted"),
            runState: "idle",
            toolCalls: cancelled
              ? this.#terminalizedToolCalls(agentRunId)
              : this.#interruptedToolCalls(agentRunId, recoveredToolResult?.toolCallId),
            vaultChanges: cancelled
              ? this.#cancelPendingVaultChanges(agentRunId)
              : this.#viewModel.conversation.vaultChanges,
          });
        }
      }
      this.#updateConversation({
        ...this.#viewModel.conversation,
        messages: [...messages],
        runState: "idle",
      });
    } catch (error) {
      this.#providerStatus = "unavailable";
      messages.pop();
      this.#updateConversation({
        ...this.#viewModel.conversation,
        messages: [...messages],
        runState: "idle",
        agentRuns: this.#runsWithStatus(agentRunId, "interrupted"),
        toolCalls: this.#interruptedToolCalls(agentRunId, recoveredToolResult?.toolCallId),
        error: {
          code: "transport_error",
          message: error instanceof Error ? error.message : String(error),
        },
      });
    } finally {
      if (this.#activeRun?.agentRunId === agentRunId) this.#activeRun = undefined;
    }
  }

  stopAgentRun(): void {
    if (!this.#activeRun) return;
    this.#runtime.cancelAgentRun(this.#activeRun);
  }

  async decideVaultChange(toolCallId: string, decision: "apply" | "reject"): Promise<void> {
    if (!this.#vaultChanges) throw new Error("Vault Change decisions are unavailable.");
    const call = this.#viewModel.conversation.toolCalls.find((candidate) => candidate.id === toolCallId);
    const run = call
      ? this.#viewModel.conversation.agentRuns.find((candidate) => candidate.id === call.agentRunId)
      : undefined;
    this.#setVaultChangeStatus(toolCallId, decision === "apply" ? "applying" : "rejecting");
    let result: LocalToolResultPayload;
    try {
      result = await this.#vaultChanges.decide(toolCallId, decision);
    } catch (error) {
      const message = error instanceof Error ? error.message : String(error);
      this.#setVaultChangeStatus(toolCallId, "pending");
      this.#updateConversation({
        ...this.#viewModel.conversation,
        error: { code: "tool_error", message },
      });
      return;
    }
    if (!result.ok) {
      this.#setVaultChangeStatus(toolCallId, "failed");
      this.#updateConversation({
        ...this.#viewModel.conversation,
        error: { code: result.error.code, message: result.error.message },
      });
    } else if (result.value.type === "vault_propose_changes") {
      this.#setVaultChangeStatus(toolCallId, result.value.decision);
    } else {
      throw new Error("The Vault Change decision returned the wrong Tool Result type.");
    }
    if (call && run?.status === "interrupted") {
      const recoveredToolResult = {
        eventId: randomUUID(),
        toolCallId,
        result,
      };
      this.#recoveredToolResults.set(run.id, recoveredToolResult);
      await this.#resumeAgentRun(run.id, recoveredToolResult);
    }
  }

  async undoVaultChange(batchId: string): Promise<void> {
    if (!this.#vaultChanges) throw new Error("Vault Change undo is unavailable.");
    const result = await this.#vaultChanges.undo(batchId);
    const change = this.#viewModel.conversation.vaultChanges.find(
      (candidate) => candidate.batchId === batchId,
    );
    if (!change) return;
    if (result.ok) this.#setVaultChangeStatus(change.toolCallId, "undone");
    else {
      if (result.error.code === "undo_conflict" && result.error.conflicts) {
        this.#updateConversation({
          ...this.#viewModel.conversation,
          vaultChanges: this.#viewModel.conversation.vaultChanges.map((candidate) =>
            candidate.toolCallId === change.toolCallId
              ? { ...candidate, status: "conflicted", conflicts: result.error.conflicts }
              : candidate,
          ),
        });
      }
      this.#updateConversation({
        ...this.#viewModel.conversation,
        error: { code: "provider_error", message: result.error.message },
      });
    }
  }

  async stop(): Promise<void> {
    await this.#runtime.stop();
    this.#update({ state: "idle" });
  }

  #update(runtime: SidebarViewModel["runtime"]): void {
    this.#viewModel = { ...this.#viewModel, runtime };
    for (const subscriber of this.#subscribers) subscriber(this.getViewModel());
  }

  async #rehydratePendingVaultChanges(calls: ToolCallRecord[]): Promise<void> {
    if (!this.#vaultChanges?.rehydrate) return;
    for (const call of calls) {
      if (
        call.name === "vault_propose_changes" &&
        call.status === "requested" &&
        call.vaultChangeState === "pending"
      ) {
        const restored = await this.#vaultChanges.rehydrate(call.id);
        if (restored.result) {
          this.#recoveredToolResults.set(call.agentRunId, {
            eventId: randomUUID(),
            toolCallId: call.id,
            result: restored.result,
          });
        }
      } else if (call.name === "vault_propose_changes") {
        await this.#vaultChanges.acknowledge?.(call.id);
      }
    }
  }

  #toolCallsWithRecoveredFailures(calls: ToolCallRecord[]): ToolCallRecord[] {
    const recoveredIds = new Set(
      [...this.#recoveredToolResults.values()].map(({ toolCallId }) => toolCallId),
    );
    return calls.map((call) =>
      recoveredIds.has(call.id) && call.status === "requested"
        ? { ...call, status: "failed" }
        : call,
    );
  }

  #updateConversation(conversation: SidebarViewModel["conversation"]): void {
    this.#viewModel = { ...this.#viewModel, conversation };
    for (const subscriber of this.#subscribers) subscriber(this.getViewModel());
  }

  #presentation(): SidebarViewModel["presentation"] {
    const activities = this.#viewModel.conversation.toolCalls.flatMap((call) => {
      if (call.name === "vault_propose_changes") return [];
      const action = TOOL_ACTIONS[call.name];
      const target = toolTarget(call);
      return [{
        action,
        details: JSON.stringify(
          {
            arguments: call.arguments,
            status: call.status,
            ...(call.error ? { error: call.error } : {}),
          },
          null,
          2,
        ),
        id: call.id,
        label: `${action}${target ? ` ${target}` : ""} · ${call.status}`,
        status: call.status,
        ...(target ? { target } : {}),
      }];
    });
    const resumable = [...this.#viewModel.conversation.agentRuns]
      .reverse()
      .find(
        (run) =>
          run.status === "interrupted" &&
          !this.#viewModel.conversation.toolCalls.some(
            (call) =>
              call.agentRunId === run.id &&
              call.name === "vault_propose_changes" &&
              call.status === "requested",
          ),
      );
    const primaryAction = this.#viewModel.conversation.runState === "streaming"
      ? {
          ...(this.#activeRun ? { agentRunId: this.#activeRun.agentRunId } : {}),
          kind: "stop" as const,
          label: "Stop" as const,
        }
      : resumable
        ? { agentRunId: resumable.id, kind: "resume" as const, label: "Resume" as const }
        : { kind: "send" as const, label: "Send" as const };
    const selectedModel = this.#viewModel.conversation.models.find(
      ({ id }) => id === this.#viewModel.conversation.selectedModelId,
    );
    const permissionMode = this.#environment.getVaultPermissionMode();
    const activityById = new Map(activities.map((activity) => [activity.id, activity]));
    const changeByToolCallId = new Map(
      this.#viewModel.conversation.vaultChanges.map((change) => [change.toolCallId, change]),
    );
    const transcript: SidebarViewModel["presentation"]["transcript"] = [];
    const includedMessages = new Set<SidebarViewModel["conversation"]["messages"][number]>();
    const includedCalls = new Set<string>();
    const latestRun = this.#viewModel.conversation.agentRuns.at(-1);
    for (const run of this.#viewModel.conversation.agentRuns) {
      const runMessages = this.#viewModel.conversation.messages.filter(
        (message) => message.agentRunId === run.id,
      );
      for (const message of runMessages.filter(({ role }) => role === "user")) {
        includedMessages.add(message);
        transcript.push({ kind: "message", message });
      }
      for (const call of this.#viewModel.conversation.toolCalls.filter(
        (candidate) => candidate.agentRunId === run.id,
      )) {
        includedCalls.add(call.id);
        const change = changeByToolCallId.get(call.id);
        const activity = activityById.get(call.id);
        if (change) transcript.push({ kind: "vault_change", change });
        else if (activity) transcript.push({ kind: "activity", activity });
      }
      for (const message of runMessages.filter(({ role }) => role === "assistant")) {
        includedMessages.add(message);
        transcript.push({ kind: "message", message });
      }
      if (run.status !== "completed" && run.status !== "running") {
        const labels = {
          cancelled: "Run stopped.",
          failed: "Run failed.",
          interrupted: "Run interrupted. Resume when ready.",
        } as const;
        transcript.push({
          kind: "run_status",
          agentRunId: run.id,
          label: labels[run.status],
          status: run.status,
          ...(run.error?.message
            ? { message: run.error.message }
            : latestRun?.id === run.id && this.#viewModel.conversation.error
              ? { message: this.#viewModel.conversation.error.message }
            : {}),
        });
      }
    }
    for (const message of this.#viewModel.conversation.messages) {
      if (!includedMessages.has(message)) transcript.push({ kind: "message", message });
    }
    for (const call of this.#viewModel.conversation.toolCalls) {
      if (includedCalls.has(call.id)) continue;
      const change = changeByToolCallId.get(call.id);
      const activity = activityById.get(call.id);
      if (change) transcript.push({ kind: "vault_change", change });
      else if (activity) transcript.push({ kind: "activity", activity });
    }
    return {
      activities,
      composer: {
        contextChips: [{ kind: "scope", label: "Vault context" }],
        permissionMode,
        primaryAction,
      },
      settings: {
        advanced: {
          diagnostics:
            this.#viewModel.runtime.message ?? `Runtime is ${this.#viewModel.runtime.state}.`,
          gitRetention: "Git Checkpoints: 30 days or the most recent 100 batches.",
          hostedWebSearch: "Hosted Web Search capability is probed per backend and model.",
        },
        ...(selectedModel?.supportsFastMode
          ? { fastMode: { enabled: this.#environment.getFastModeEnabled() } }
          : {}),
        ...(selectedModel ? { model: { id: selectedModel.id, label: selectedModel.label } } : {}),
        permissionMode,
        providerStatus: this.#providerStatus,
        runtimeStatus: this.#viewModel.runtime.state,
      },
      transcript,
    };
  }

  #runsWithStatus(
    agentRunId: string,
    status: AgentRunRecord["status"],
    error?: AgentRunRecord["error"],
  ): AgentRunRecord[] {
    return this.#viewModel.conversation.agentRuns.map((run) => {
      if (run.id !== agentRunId) return run;
      const { error: _previousError, ...withoutError } = run;
      return { ...withoutError, status, ...(error ? { error } : {}) };
    });
  }

  #terminalizedToolCalls(agentRunId: string): ToolCallRecord[] {
    return this.#viewModel.conversation.toolCalls.map((call) =>
      call.agentRunId === agentRunId && call.status === "requested"
        ? { ...call, status: "failed" }
        : call,
    );
  }

  #interruptedToolCalls(agentRunId: string, recoveredToolCallId?: string): ToolCallRecord[] {
    return this.#viewModel.conversation.toolCalls.map((call) =>
      call.agentRunId === agentRunId &&
      call.status === "requested" &&
      (call.name !== "vault_propose_changes" || call.id === recoveredToolCallId)
        ? { ...call, status: "failed" }
        : call,
    );
  }

  #setRunStatus(agentRunId: string, status: AgentRunRecord["status"]): void {
    this.#updateConversation({
      ...this.#viewModel.conversation,
      agentRuns: this.#runsWithStatus(agentRunId, status),
    });
  }

  #setVaultChangeStatus(
    toolCallId: string,
    status: SidebarViewModel["conversation"]["vaultChanges"][number]["status"],
  ): void {
    this.#updateConversation({
      ...this.#viewModel.conversation,
      vaultChanges: this.#vaultChangesWithStatus(toolCallId, status),
    });
  }

  #vaultChangesWithStatus(
    toolCallId: string,
    status: SidebarViewModel["conversation"]["vaultChanges"][number]["status"],
  ): SidebarViewModel["conversation"]["vaultChanges"] {
    return this.#viewModel.conversation.vaultChanges.map((change) =>
      change.toolCallId === toolCallId ? { ...change, status } : change,
    );
  }

  #cancelPendingVaultChanges(
    agentRunId: string,
  ): SidebarViewModel["conversation"]["vaultChanges"] {
    const pendingIds = new Set(
      this.#viewModel.conversation.toolCalls
        .filter(
          (call) =>
            call.agentRunId === agentRunId &&
            call.name === "vault_propose_changes" &&
            call.status === "requested",
        )
        .map((call) => call.id),
    );
    if (pendingIds.size === 0) return this.#viewModel.conversation.vaultChanges;
    for (const toolCallId of pendingIds) this.#vaultChanges?.cancel(toolCallId);
    return this.#viewModel.conversation.vaultChanges.map((change) =>
      pendingIds.has(change.toolCallId) && change.status === "pending"
        ? { ...change, status: "failed" }
        : change,
    );
  }

  #requestedChange(
    toolCallId: string,
    name: ToolCallRecord["name"],
    arguments_: unknown,
  ): SidebarViewModel["conversation"]["vaultChanges"][number] | undefined {
    if (name !== "vault_propose_changes" || !arguments_ || typeof arguments_ !== "object") {
      return undefined;
    }
    const proposal = arguments_ as Partial<VaultChangeBatchProposal>;
    if (
      typeof proposal.batchId !== "string" ||
      typeof proposal.task !== "string" ||
      !Array.isArray(proposal.actions)
    ) {
      return undefined;
    }
    return {
      toolCallId,
      batchId: proposal.batchId,
      task: proposal.task,
      status: "pending",
      actions: proposal.actions
        .filter(
          (action) =>
            action && typeof action.path === "string" && typeof action.operation === "string",
        )
        .map((action) => ({ path: action.path, operation: action.operation })),
    };
  }

  #changesFromToolCalls(
    calls: ToolCallRecord[],
  ): SidebarViewModel["conversation"]["vaultChanges"] {
    return calls.flatMap((call) => {
      const change = this.#requestedChange(call.id, call.name, call.arguments);
      if (!change) return [];
      return [
        {
          ...change,
          status: this.#vaultChangeStatus(call),
          ...(call.error ? { message: call.error.message } : {}),
        },
      ];
    });
  }

  #vaultChangeStatus(
    call: ToolCallRecord,
  ): SidebarViewModel["conversation"]["vaultChanges"][number]["status"] {
    if (
      call.vaultChangeState === "applied" ||
      call.vaultChangeState === "expired" ||
      call.vaultChangeState === "rejected" ||
      call.vaultChangeState === "undone"
    ) {
      return call.vaultChangeState;
    }
    if (
      call.vaultChangeState === "recovery_failed" ||
      call.vaultChangeState === "rolled_back" ||
      call.status === "failed"
    ) {
      return "failed";
    }
    if (call.status === "requested") return "pending";
    return call.decision ?? "applied";
  }
}
