export { RuntimeSupervisor } from "./runtime-supervisor";

import { randomUUID } from "node:crypto";
import type { ModelDescriptor, ProviderErrorCode } from "@offeragent/protocol";
import { RuntimeRequestError, type RuntimeClient } from "./runtime-supervisor";

export type RuntimeViewState = "connected" | "idle" | "starting" | "unavailable";

export interface SidebarViewModel {
  conversation: {
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
    conversation: { messages: [], models: [], runState: "idle" },
    runtime: { state: "idle" },
  };
  readonly #conversationId = randomUUID();

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
        this.#updateConversation({
          ...this.#viewModel.conversation,
          models,
          selectedModelId: models[0]?.id,
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

  selectModel(modelId: string): void {
    if (!this.#viewModel.conversation.models.some((model) => model.id === modelId)) {
      throw new Error(`The selected model '${modelId}' is unavailable.`);
    }
    this.#updateConversation({
      ...this.#viewModel.conversation,
      selectedModelId: modelId,
      error: undefined,
    });
  }

  async sendMessage(input: string): Promise<void> {
    const text = input.trim();
    const selectedModelId = this.#viewModel.conversation.selectedModelId;
    if (!text) return;
    if (!selectedModelId) throw new Error("Choose an available model before sending a message.");
    if (this.#viewModel.conversation.runState === "streaming") {
      throw new Error("Wait for the current Agent Run to finish.");
    }
    const messages = [
      ...this.#viewModel.conversation.messages,
      { role: "user" as const, text },
      { role: "assistant" as const, text: "" },
    ];
    this.#updateConversation({
      ...this.#viewModel.conversation,
      messages,
      runState: "streaming",
      error: undefined,
    });

    try {
      for await (const event of this.#runtime.runAgent({
        conversationId: this.#conversationId,
        agentRunId: randomUUID(),
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
        } else if (event.type === "agent_run.failed") {
          this.#updateConversation({
            ...this.#viewModel.conversation,
            messages: [...messages],
            runState: "idle",
            error: event.error,
          });
          return;
        }
      }
      this.#updateConversation({
        ...this.#viewModel.conversation,
        messages: [...messages],
        runState: "idle",
      });
    } catch (error) {
      const message = error instanceof Error ? error.message : String(error);
      this.#updateConversation({
        ...this.#viewModel.conversation,
        messages: [...messages],
        runState: "idle",
        error: { code: "transport_error", message },
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
}
