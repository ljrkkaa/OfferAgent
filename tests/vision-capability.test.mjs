import assert from "node:assert/strict";
import path from "node:path";
import test from "node:test";
import { fileURLToPath, pathToFileURL } from "node:url";

const repositoryRoot = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..");
const modulePath = path.join(
  repositoryRoot,
  "packages",
  "runtime",
  "dist",
  "capability-gated-provider.js",
);
const IMAGE = {
  attachmentId: "attachment-a",
  dataUrl: "data:image/png;base64,iVBORw0KGgo=",
  mediaType: "image/png",
  order: 0,
};
const IMAGE_MESSAGE = {
  type: "user_message",
  text: "Describe this interview screenshot.",
  attachments: [{ attachmentId: IMAGE.attachmentId, mediaType: IMAGE.mediaType, order: 0 }],
};

class Store {
  values = new Map();
  key(backend, model, capability) { return `${backend}\0${model}\0${capability}`; }
  async getProviderCapability(backend, model, capability) {
    return this.values.get(this.key(backend, model, capability)) ?? "unknown";
  }
  async resetProviderCapability(backend, model, capability) {
    this.values.delete(this.key(backend, model, capability));
  }
  async setProviderCapability(backend, model, capability, status) {
    this.values.set(this.key(backend, model, capability), status);
  }
}

function request(input, imageInputs = undefined) {
  return {
    model: "vision-model",
    signal: new AbortController().signal,
    instructions: "Answer the user.",
    input,
    ...(imageInputs ? { imageInputs } : {}),
    tools: [],
  };
}

test("unknown Vision Capability is probed once and the active image request stays ordered", async () => {
  const { CapabilityGatedModelProvider } = await import(pathToFileURL(modulePath));
  const store = new Store();
  const calls = [];
  const provider = {
    backendId: "fake",
    async listModels() { return []; },
    async *stream(input) {
      calls.push(input);
      yield { type: "output_text.delta", delta: calls.length === 1 ? "OK" : "image understood" };
    },
  };
  const gated = new CapabilityGatedModelProvider(provider, store);
  const events = [];
  for await (const event of gated.stream(request([IMAGE_MESSAGE], [IMAGE]))) events.push(event);
  assert.equal(calls.length, 2);
  assert.equal(calls[0].imageInputs.length, 1);
  assert.deepEqual(calls[1].input[0].attachments, IMAGE_MESSAGE.attachments);
  assert.deepEqual(calls[1].imageInputs, [IMAGE]);
  assert.equal(await store.getProviderCapability("fake", "vision-model", "vision"), "available");
  assert.equal(events.at(-1).delta, "image understood");
});

test("unavailable vision is actionable and cannot break a later text Run", async () => {
  const { CapabilityGatedModelProvider, ModelProviderError } = await import(pathToFileURL(modulePath));
  const store = new Store();
  const provider = {
    backendId: "fake",
    async listModels() { return []; },
    async *stream(input) {
      if (input.imageInputs?.length) {
        throw new ModelProviderError("unsupported_capability", "input_image is unsupported");
      }
      yield { type: "output_text.delta", delta: "text still works" };
    },
  };
  const gated = new CapabilityGatedModelProvider(provider, store);
  await assert.rejects(
    async () => {
      for await (const _event of gated.stream(request([IMAGE_MESSAGE], [IMAGE]))) { /* consume */ }
    },
    (error) =>
      error instanceof ModelProviderError &&
      error.code === "unsupported_capability" &&
      /vision|image|text/i.test(error.message),
  );
  assert.equal(await store.getProviderCapability("fake", "vision-model", "vision"), "unavailable");
  const text = [];
  for await (const event of gated.stream(request([{ type: "user_message", text: "Hello" }]))) {
    text.push(event.delta);
  }
  assert.deepEqual(text, ["text still works"]);
});

test("vision rejection cannot poison an available hosted-search capability", async () => {
  const { CapabilityGatedModelProvider, ModelProviderError } = await import(pathToFileURL(modulePath));
  const store = new Store();
  await store.setProviderCapability("fake", "vision-model", "vision", "available");
  await store.setProviderCapability("fake", "vision-model", "hosted_web_search", "available");
  const calls = [];
  const provider = {
    backendId: "fake",
    async listModels() { return []; },
    async *stream(input) {
      calls.push(input);
      if (input.imageInputs?.length) {
        throw new ModelProviderError(
          "unsupported_capability",
          "input_image is unsupported",
          { capability: "vision" },
        );
      }
      yield { type: "output_text.delta", delta: "text search remains available" };
    },
  };
  const gated = new CapabilityGatedModelProvider(provider, store);
  await assert.rejects(async () => {
    for await (const _event of gated.stream({
      ...request([IMAGE_MESSAGE], [IMAGE]),
      tools: [{ kind: "hosted", name: "web_search" }],
    })) { /* consume */ }
  }, (error) => error?.code === "unsupported_capability");
  assert.equal(calls.length, 1);
  assert.equal(
    await store.getProviderCapability("fake", "vision-model", "hosted_web_search"),
    "available",
  );
  assert.equal(await store.getProviderCapability("fake", "vision-model", "vision"), "unavailable");
  const text = [];
  for await (const event of gated.stream({
    ...request([{ type: "user_message", text: "Search with text." }]),
    tools: [{ kind: "hosted", name: "web_search" }],
  })) text.push(event.delta);
  assert.deepEqual(text, ["text search remains available"]);
});

test("invalid image content does not change an available Vision Capability", async () => {
  const { CapabilityGatedModelProvider, ModelProviderError } = await import(pathToFileURL(modulePath));
  const store = new Store();
  await store.setProviderCapability("fake", "vision-model", "vision", "available");
  const provider = {
    backendId: "fake",
    async listModels() { return []; },
    async *stream() {
      throw new ModelProviderError("provider_error", "Invalid image input: invalid PNG data");
    },
  };
  const gated = new CapabilityGatedModelProvider(provider, store);

  await assert.rejects(async () => {
    for await (const _event of gated.stream(request([IMAGE_MESSAGE], [IMAGE]))) { /* consume */ }
  }, (error) => error?.code === "provider_error");

  assert.equal(await store.getProviderCapability("fake", "vision-model", "vision"), "available");
});
