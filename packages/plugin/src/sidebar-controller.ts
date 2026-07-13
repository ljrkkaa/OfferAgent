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

export type RuntimeViewState = "connected" | "idle" | "starting" | "unavailable";

export interface SidebarViewModel {
  conversation: {
    activeConversationId?: string;
    agentRuns: AgentRunRecord[];
    conversations: ConversationSummary[];
    error?: { code: ProviderErrorCode | VaultToolErrorCode; message: string };
    messages: Array<{ citations?: WebCitation[]; role: "assistant" | "user"; text: string }>;
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
  title: "OfferAgent";
}

type Subscriber = (viewModel: SidebarViewModel) => void;

export interface VaultChangeDecisionClient {
  cancel(toolCallId: string): void;
  decide(toolCallId: string, decision: "apply" | "reject"): Promise<LocalToolResultPayload>;
  undo(batchId: string): Promise<VaultUndoResultPayload>;
}

export class SidebarController {
  readonly #runtime: RuntimeClient;
  readonly #vaultChanges?: VaultChangeDecisionClient;
  readonly #subscribers = new Set<Subscriber>();
  #viewModel: SidebarViewModel = {
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

  constructor(runtime: RuntimeClient, vaultChanges?: VaultChangeDecisionClient) {
    this.#runtime = runtime;
    this.#vaultChanges = vaultChanges;
    this.#runtime.onUnavailable((message) => {
      this.#update({ state: "unavailable", message });
    });
  }

  getViewModel(): SidebarViewModel {
    return this.#viewModel;
  }

  subscribe(subscriber: Subscriber): () => void {
    this.#subscribers.add(subscriber);
    subscriber(this.#viewModel);
    return () => this.#subscribers.delete(subscriber);
  }

  async start(): Promise<void> {
    this.#update({ state: "starting" });
    try {
      await this.#runtime.start();
      this.#update({ state: "connected" });
      try {
        const models = await this.#runtime.listModels();
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
        this.#updateConversation({
          ...this.#viewModel.conversation,
          models,
          conversations,
          activeConversationId: snapshot?.conversation.id,
          selectedModelId: snapshot?.conversation.modelId ?? models[0]?.id,
          messages:
            snapshot?.messages.map(({ role, text }) => ({ role, text })) ?? [],
          agentRuns: snapshot?.agentRuns ?? [],
          toolCalls: snapshot?.toolCalls ?? [],
          vaultChanges: this.#changesFromToolCalls(snapshot?.toolCalls ?? []),
          error: undefined,
        });
      } catch (error) {
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
    this.#updateConversation({
      ...this.#viewModel.conversation,
      activeConversationId: snapshot.conversation.id,
      agentRuns: snapshot.agentRuns,
      messages: snapshot.messages.map(({ role, text, citations }) => ({
        role,
        text,
        ...(citations ? { citations } : {}),
      })),
      runState: "idle",
      selectedModelId: snapshot.conversation.modelId,
      toolCalls: snapshot.toolCalls ?? [],
      vaultChanges: this.#changesFromToolCalls(snapshot.toolCalls ?? []),
      error: undefined,
    });
  }

  async deleteCurrentConversation(): Promise<void> {
    const conversationId = this.#viewModel.conversation.activeConversationId;
    if (!conversationId) return;
    if (this.#viewModel.conversation.runState === "streaming") {
      throw new Error("Stop the current Agent Run before deleting its Conversation.");
    }
    await this.#runtime.deleteConversation(conversationId);
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
    const messages = [
      ...this.#viewModel.conversation.messages,
      { role: "user" as const, text },
      { role: "assistant" as const, text: "" },
    ];
    const agentRunId = randomUUID();
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
        input: text,
      })) {
        if (event.type === "agent_run.delta") {
          messages[messages.length - 1] = {
            role: "assistant",
            text: messages[messages.length - 1].text + event.delta,
          };
          this.#updateConversation({ ...this.#viewModel.conversation, messages: [...messages] });
        } else if (event.type === "agent_run.completed") {
          messages[messages.length - 1] = event.output;
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
                ? { ...call, status: event.status }
                : call,
            ),
            vaultChanges,
            error:
              event.status === "failed" && event.error
                ? { code: event.error.code, message: event.error.message }
                : this.#viewModel.conversation.error,
          });
        } else if (event.type === "agent_run.failed") {
          const vaultChanges = this.#cancelPendingVaultChanges(agentRunId);
          messages.pop();
          this.#updateConversation({
            ...this.#viewModel.conversation,
            messages: [...messages],
            runState: "idle",
            agentRuns: this.#runsWithStatus(agentRunId, "failed"),
            toolCalls: this.#terminalizedToolCalls(agentRunId),
            vaultChanges,
            error: event.error,
          });
        } else if (event.type === "agent_run.cancelled" || event.type === "agent_run.interrupted") {
          const vaultChanges = this.#cancelPendingVaultChanges(agentRunId);
          messages.pop();
          this.#updateConversation({
            ...this.#viewModel.conversation,
            messages: [...messages],
            runState: "idle",
            toolCalls: this.#terminalizedToolCalls(agentRunId),
            vaultChanges,
            agentRuns: this.#runsWithStatus(
              agentRunId,
              event.type === "agent_run.cancelled" ? "cancelled" : "interrupted",
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
      const vaultChanges = this.#cancelPendingVaultChanges(agentRunId);
      messages.pop();
      const message = error instanceof Error ? error.message : String(error);
      this.#updateConversation({
        ...this.#viewModel.conversation,
        messages: [...messages],
        runState: "idle",
        toolCalls: this.#terminalizedToolCalls(agentRunId),
        vaultChanges,
        agentRuns: this.#runsWithStatus(agentRunId, "interrupted"),
        error: { code: "transport_error", message },
      });
    } finally {
      if (this.#activeRun?.agentRunId === agentRunId) this.#activeRun = undefined;
    }
  }

  stopAgentRun(): void {
    if (!this.#activeRun) return;
    const vaultChanges = this.#cancelPendingVaultChanges(this.#activeRun.agentRunId);
    if (vaultChanges !== this.#viewModel.conversation.vaultChanges) {
      this.#updateConversation({ ...this.#viewModel.conversation, vaultChanges });
    }
    this.#runtime.cancelAgentRun(this.#activeRun);
  }

  async decideVaultChange(toolCallId: string, decision: "apply" | "reject"): Promise<void> {
    if (!this.#vaultChanges) throw new Error("Vault Change decisions are unavailable.");
    this.#setVaultChangeStatus(toolCallId, decision === "apply" ? "applying" : "rejecting");
    const result = await this.#vaultChanges.decide(toolCallId, decision);
    if (!result.ok || result.value.type !== "vault_propose_changes") {
      this.#setVaultChangeStatus(toolCallId, "failed");
      if (!result.ok) {
        this.#updateConversation({
          ...this.#viewModel.conversation,
          error: { code: "provider_error", message: result.error.message },
        });
      }
      return;
    }
    this.#setVaultChangeStatus(toolCallId, result.value.decision);
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
    for (const subscriber of this.#subscribers) subscriber(this.#viewModel);
  }

  #updateConversation(conversation: SidebarViewModel["conversation"]): void {
    this.#viewModel = { ...this.#viewModel, conversation };
    for (const subscriber of this.#subscribers) subscriber(this.#viewModel);
  }

  #runsWithStatus(
    agentRunId: string,
    status: AgentRunRecord["status"],
  ): AgentRunRecord[] {
    return this.#viewModel.conversation.agentRuns.map((run) =>
      run.id === agentRunId ? { ...run, status } : run,
    );
  }

  #terminalizedToolCalls(agentRunId: string): ToolCallRecord[] {
    return this.#viewModel.conversation.toolCalls.map((call) =>
      call.agentRunId === agentRunId && call.status === "requested"
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
