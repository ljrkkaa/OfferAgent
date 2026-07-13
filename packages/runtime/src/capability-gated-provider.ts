import type { HostedWebSearchCapability } from "@offeragent/protocol";
import {
  ModelProviderError,
  type ModelProvider,
  type ModelRequest,
  type ModelStreamEvent,
} from "./model-provider";

interface CapabilityStore {
  getProviderCapability(
    backendId: string,
    modelId: string,
    capability: "hosted_web_search",
  ): Promise<HostedWebSearchCapability>;
  resetProviderCapability(
    backendId: string,
    modelId: string,
    capability: "hosted_web_search",
  ): Promise<void>;
  setProviderCapability(
    backendId: string,
    modelId: string,
    capability: "hosted_web_search",
    status: HostedWebSearchCapability,
  ): Promise<void>;
}

const HOSTED_WEB_SEARCH = { kind: "hosted", name: "web_search" } as const;
const MAX_CAPABILITY_PRELUDE_EVENTS = 8;

type NextOutcome =
  | { kind: "event"; result: IteratorResult<ModelStreamEvent> }
  | { kind: "error"; error: unknown };

function nextOutcome(iterator: AsyncIterator<ModelStreamEvent>): Promise<NextOutcome> {
  return iterator.next().then(
    (result) => ({ kind: "event", result }),
    (error: unknown) => ({ kind: "error", error }),
  );
}

function waitOneTurn(outcome: Promise<NextOutcome>): Promise<NextOutcome | { kind: "commit" }> {
  return new Promise((resolve) => {
    const immediate = setImmediate(() => resolve({ kind: "commit" }));
    void outcome.then((result) => {
      clearImmediate(immediate);
      resolve(result);
    });
  });
}

function isUnsupported(error: unknown): boolean {
  return error instanceof ModelProviderError && error.code === "unsupported_capability";
}

function withoutHostedWebSearch(request: ModelRequest): ModelRequest {
  return { ...request, tools: request.tools.filter((tool) => tool.kind !== "hosted") };
}

function withHostedWebSearch(request: ModelRequest): ModelRequest {
  return request.tools.some((tool) => tool.kind === "hosted" && tool.name === "web_search")
    ? request
    : { ...request, tools: [...request.tools, HOSTED_WEB_SEARCH] };
}

export class CapabilityGatedModelProvider implements ModelProvider {
  readonly #provider: ModelProvider;
  readonly #store: CapabilityStore;

  constructor(provider: ModelProvider, store: CapabilityStore) {
    this.#provider = provider;
    this.#store = store;
  }

  get backendId(): string {
    return this.#provider.backendId;
  }

  listModels() {
    return this.#provider.listModels();
  }

  getHostedWebSearchCapability(model: string): Promise<HostedWebSearchCapability> {
    return this.#store.getProviderCapability(this.backendId, model, "hosted_web_search");
  }

  async reprobeHostedWebSearch(model: string, signal: AbortSignal): Promise<HostedWebSearchCapability> {
    await this.#store.resetProviderCapability(this.backendId, model, "hosted_web_search");
    return this.#probe(model, signal);
  }

  async *stream(request: ModelRequest): AsyncIterable<ModelStreamEvent> {
    const capability = await this.getHostedWebSearchCapability(request.model);
    if (capability !== "available") {
      yield* this.#provider.stream(withoutHostedWebSearch(request));
      return;
    }

    const hostedController = new AbortController();
    const abortHosted = () => hostedController.abort(request.signal.reason);
    if (request.signal.aborted) abortHosted();
    else request.signal.addEventListener("abort", abortHosted, { once: true });
    const iterator = this.#provider.stream(withHostedWebSearch({
      ...request,
      signal: hostedController.signal,
    }))[Symbol.asyncIterator]();
    const markUnavailable = () => this.#store.setProviderCapability(
      this.backendId,
      request.model,
      "hosted_web_search",
      "unavailable",
    );
    let iteratorDone = false;
    try {
      const prelude: ModelStreamEvent[] = [];
      let outcome = await nextOutcome(iterator);
      if (outcome.kind === "error") {
        iteratorDone = true;
        if (isUnsupported(outcome.error)) {
          await markUnavailable();
          yield* this.#provider.stream(withoutHostedWebSearch(request));
          return;
        }
        throw outcome.error;
      }
      if (outcome.result.done) {
        iteratorDone = true;
        return;
      }
      prelude.push(outcome.result.value);

      while (prelude.length < MAX_CAPABILITY_PRELUDE_EVENTS) {
        const pending = nextOutcome(iterator);
        const decision = await waitOneTurn(pending);
        if (decision.kind === "commit") {
          for (const event of prelude) yield event;
          outcome = await pending;
          while (true) {
            if (outcome.kind === "error") {
              iteratorDone = true;
              if (isUnsupported(outcome.error)) {
                await markUnavailable();
                yield* this.#provider.stream(withoutHostedWebSearch(request));
                return;
              }
              throw outcome.error;
            }
            if (outcome.result.done) {
              iteratorDone = true;
              return;
            }
            yield outcome.result.value;
            outcome = await nextOutcome(iterator);
          }
        }
        if (decision.kind === "error") {
          iteratorDone = true;
          if (isUnsupported(decision.error)) {
            await markUnavailable();
            yield* this.#provider.stream(withoutHostedWebSearch(request));
            return;
          }
          throw decision.error;
        }
        if (decision.result.done) {
          iteratorDone = true;
          for (const event of prelude) yield event;
          return;
        }
        prelude.push(decision.result.value);
      }

      for (const event of prelude) yield event;
      outcome = await nextOutcome(iterator);
      while (true) {
        if (outcome.kind === "error") {
          iteratorDone = true;
          if (isUnsupported(outcome.error)) {
            await markUnavailable();
            yield* this.#provider.stream(withoutHostedWebSearch(request));
            return;
          }
          throw outcome.error;
        }
        if (outcome.result.done) {
          iteratorDone = true;
          return;
        }
        yield outcome.result.value;
        outcome = await nextOutcome(iterator);
      }
    } finally {
      request.signal.removeEventListener("abort", abortHosted);
      hostedController.abort();
      if (!iteratorDone) {
        try {
          await iterator.return?.();
        } catch {
          // The child signal was already aborted; cleanup must not mask the caller's outcome.
        }
      }
    }
  }

  async #probe(model: string, signal: AbortSignal): Promise<HostedWebSearchCapability> {
    try {
      for await (const _event of this.#provider.stream({
        model,
        signal,
        instructions: "This is a minimal capability probe. Reply only OK and do not search.",
        input: [{ type: "user_message", text: "Reply OK." }],
        tools: [HOSTED_WEB_SEARCH],
      })) {
        // A completed response proves that this backend/model accepted the hosted tool declaration.
      }
      await this.#store.setProviderCapability(
        this.backendId,
        model,
        "hosted_web_search",
        "available",
      );
      return "available";
    } catch (error) {
      if (!(error instanceof ModelProviderError) || error.code !== "unsupported_capability") throw error;
      await this.#store.setProviderCapability(
        this.backendId,
        model,
        "hosted_web_search",
        "unavailable",
      );
      return "unavailable";
    }
  }
}

export { ModelProviderError };
