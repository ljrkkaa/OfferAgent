import type { ProviderCapabilityStatus } from "@offeragent/protocol";
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
    capability: "hosted_web_search" | "vision",
  ): Promise<ProviderCapabilityStatus>;
  resetProviderCapability(
    backendId: string,
    modelId: string,
    capability: "hosted_web_search" | "vision",
  ): Promise<void>;
  setProviderCapability(
    backendId: string,
    modelId: string,
    capability: "hosted_web_search" | "vision",
    status: ProviderCapabilityStatus,
  ): Promise<void>;
}

const HOSTED_WEB_SEARCH = { kind: "hosted", name: "web_search" } as const;
const MAX_CAPABILITY_PRELUDE_EVENTS = 8;
const VISION_PROBE_ATTACHMENT_ID = "offeragent-vision-probe";
const VISION_PROBE_DATA_URL =
  "data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII=";

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

function isUnsupported(
  error: unknown,
  capability?: "hosted_web_search" | "vision",
): boolean {
  return error instanceof ModelProviderError &&
    error.code === "unsupported_capability" &&
    (!capability || !error.capability || error.capability === capability);
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

  getHostedWebSearchCapability(model: string): Promise<ProviderCapabilityStatus> {
    return this.#store.getProviderCapability(this.backendId, model, "hosted_web_search");
  }

  getVisionCapability(model: string): Promise<ProviderCapabilityStatus> {
    return this.#store.getProviderCapability(this.backendId, model, "vision");
  }

  async reprobeHostedWebSearch(model: string, signal: AbortSignal): Promise<ProviderCapabilityStatus> {
    await this.#store.resetProviderCapability(this.backendId, model, "hosted_web_search");
    return this.#probe(model, signal);
  }

  async *stream(request: ModelRequest): AsyncIterable<ModelStreamEvent> {
    if (request.imageInputs?.length) {
      let capability = await this.getVisionCapability(request.model);
      if (capability === "unknown") capability = await this.#probeVision(request.model, request.signal);
      if (capability !== "available") {
        throw new ModelProviderError(
          "unsupported_capability",
          "The selected model cannot process images. Choose a vision-capable model or provide text.",
        );
      }
      try {
        yield* this.#streamWithHostedWebSearch(request);
        return;
      } catch (error) {
        if (!isUnsupported(error, "vision")) throw error;
        await this.#store.setProviderCapability(
          this.backendId,
          request.model,
          "vision",
          "unavailable",
        );
        throw new ModelProviderError(
          "unsupported_capability",
          "The selected model cannot process images. Choose a vision-capable model or provide text.",
          { cause: error },
        );
      }
    }
    yield* this.#streamWithHostedWebSearch(request);
  }

  async *#streamWithHostedWebSearch(request: ModelRequest): AsyncIterable<ModelStreamEvent> {
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
        if (isUnsupported(outcome.error, "hosted_web_search")) {
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
              if (isUnsupported(outcome.error, "hosted_web_search")) {
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
          if (isUnsupported(decision.error, "hosted_web_search")) {
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
          if (isUnsupported(outcome.error, "hosted_web_search")) {
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

  async #probe(model: string, signal: AbortSignal): Promise<ProviderCapabilityStatus> {
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

  async #probeVision(model: string, signal: AbortSignal): Promise<ProviderCapabilityStatus> {
    try {
      for await (const _event of this.#provider.stream({
        model,
        signal,
        instructions: "This is a minimal Vision Capability probe. Reply only OK.",
        input: [{
          type: "user_message",
          text: "Reply OK.",
          attachments: [{
            attachmentId: VISION_PROBE_ATTACHMENT_ID,
            contentHash: "sha256:offeragent-vision-probe",
            fileName: "probe.png",
            mediaType: "image/png",
            order: 0,
            size: 68,
          }],
        }],
        imageInputs: [{
          attachmentId: VISION_PROBE_ATTACHMENT_ID,
          dataUrl: VISION_PROBE_DATA_URL,
          mediaType: "image/png",
          order: 0,
        }],
        tools: [],
      })) {
        // A completed response proves that this backend/model accepted image input.
      }
      await this.#store.setProviderCapability(this.backendId, model, "vision", "available");
      return "available";
    } catch (error) {
      if (!isUnsupported(error, "vision")) throw error;
      await this.#store.setProviderCapability(this.backendId, model, "vision", "unavailable");
      return "unavailable";
    }
  }
}

export { ModelProviderError };
