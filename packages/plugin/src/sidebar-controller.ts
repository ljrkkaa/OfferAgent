export { RuntimeSupervisor } from "./runtime-supervisor";

import { randomUUID } from "node:crypto";
import type {
  AgentRunRecord,
  ConversationSummary,
  ModelDescriptor,
  ProviderErrorCode,
} from "@offeragent/protocol";
import { RuntimeRequestError, type RuntimeClient } from "./runtime-supervisor";

export type RuntimeViewState = "connected" | "idle" | "starting" | "unavailable";

export interface SidebarViewModel {
  conversation: {
    activeConversationId?: string;
    agentRuns: AgentRunRecord[];
    conversations: ConversationSummary[];
    error?: { code: ProviderErrorCode; message: string };
    messages: Array<{ role: "assistant" | "user"; text: string }>;
    models: ModelDescriptor[];
    runState: "idle" | "streaming";
    selectedModelId?: string;
  };
  runtime: {
    message?: string;
    state: RuntimeViewState;
  };
  title: "OfferAgent";
}

type Subscriber = (viewModel: SidebarViewModel) => void;

export class SidebarController {
  readonly #runtime: RuntimeClient;
  readonly #subscribers = new Set<Subscriber>();
  #viewModel: SidebarViewModel = {
    title: "OfferAgent",
    conversation: {
      agentRuns: [],
      conversations: [],
      messages: [],
      models: [],
      runState: "idle",
    },
    runtime: { state: "idle" },
  };
  #activeRun?: { agentRunId: string; conversationId: string };

  constructor(runtime: RuntimeClient) {
    this.#runtime = runtime;
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
      messages: snapshot.messages.map(({ role, text }) => ({ role, text })),
      runState: "idle",
      selectedModelId: snapshot.conversation.modelId,
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
        } else if (event.type === "agent_run.failed") {
          messages.pop();
          this.#updateConversation({
            ...this.#viewModel.conversation,
            messages: [...messages],
            runState: "idle",
            agentRuns: this.#runsWithStatus(agentRunId, "failed"),
            error: event.error,
          });
        } else if (event.type === "agent_run.cancelled" || event.type === "agent_run.interrupted") {
          messages.pop();
          this.#updateConversation({
            ...this.#viewModel.conversation,
            messages: [...messages],
            runState: "idle",
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
      messages.pop();
      const message = error instanceof Error ? error.message : String(error);
      this.#updateConversation({
        ...this.#viewModel.conversation,
        messages: [...messages],
        runState: "idle",
        agentRuns: this.#runsWithStatus(agentRunId, "interrupted"),
        error: { code: "transport_error", message },
      });
    } finally {
      if (this.#activeRun?.agentRunId === agentRunId) this.#activeRun = undefined;
    }
  }

  stopAgentRun(): void {
    if (this.#activeRun) this.#runtime.cancelAgentRun(this.#activeRun);
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

  #setRunStatus(agentRunId: string, status: AgentRunRecord["status"]): void {
    this.#updateConversation({
      ...this.#viewModel.conversation,
      agentRuns: this.#runsWithStatus(agentRunId, status),
    });
  }
}
