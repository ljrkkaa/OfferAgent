export { RuntimeSupervisor } from "./runtime-supervisor";

import { RuntimeSupervisor } from "./runtime-supervisor";

export type RuntimeViewState = "connected" | "idle" | "starting" | "unavailable";

export interface SidebarViewModel {
  runtime: {
    message?: string;
    state: RuntimeViewState;
  };
  title: "OfferAgent";
}

type Subscriber = (viewModel: SidebarViewModel) => void;

export class SidebarController {
  readonly #runtime: RuntimeSupervisor;
  readonly #subscribers = new Set<Subscriber>();
  #viewModel: SidebarViewModel = {
    title: "OfferAgent",
    runtime: { state: "idle" },
  };

  constructor(runtime: RuntimeSupervisor) {
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
    } catch (error) {
      const message = error instanceof Error ? error.message : String(error);
      this.#update({ state: "unavailable", message });
      throw error;
    }
  }

  async stop(): Promise<void> {
    await this.#runtime.stop();
    this.#update({ state: "idle" });
  }

  #update(runtime: SidebarViewModel["runtime"]): void {
    this.#viewModel = { title: "OfferAgent", runtime };
    for (const subscriber of this.#subscribers) subscriber(this.#viewModel);
  }
}
