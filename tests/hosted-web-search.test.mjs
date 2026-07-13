import assert from "node:assert/strict";
import path from "node:path";
import test from "node:test";
import { createRequire } from "node:module";
import { fileURLToPath } from "node:url";

const require = createRequire(import.meta.url);
const repositoryRoot = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..");
const { CapabilityGatedModelProvider, ModelProviderError } = require(
  path.join(repositoryRoot, "packages", "runtime", "dist", "capability-gated-provider.js"),
);

class MemoryCapabilities {
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

const localTool = {
  kind: "local",
  name: "web_read",
  description: "Read a URL",
  parameters: { type: "object" },
};

function request() {
  return {
    model: "model-a",
    signal: new AbortController().signal,
    instructions: "Answer with sources.",
    input: [{ type: "user_message", text: "What changed today?" }],
    tools: [localTool],
  };
}

async function collect(stream) {
  const events = [];
  for await (const event of stream) events.push(event);
  return events;
}

test("unknown capability is probed and an available search preserves calls and citations", async () => {
  const calls = [];
  const provider = {
    backendId: "codex:test",
    async listModels() { return []; },
    async *stream(candidate) {
      calls.push(candidate);
      if (calls.length === 1) {
        assert.deepEqual(candidate.tools, [{ kind: "hosted", name: "web_search" }]);
        yield { type: "output_text.delta", delta: "OK" };
        return;
      }
      yield {
        type: "hosted_web_search_call",
        callId: "search-1",
        sources: [{ url: "https://example.com/source", title: "Source" }],
      };
      yield { type: "output_text.delta", delta: "Current answer [1]" };
      yield {
        type: "url_citation",
        citation: {
          url: "https://example.com/source",
          title: "Source",
          startIndex: 15,
          endIndex: 18,
        },
      };
    },
  };
  const store = new MemoryCapabilities();
  const gated = new CapabilityGatedModelProvider(provider, store);
  assert.equal(await gated.getHostedWebSearchCapability("model-a"), "unknown");
  assert.equal(
    await gated.reprobeHostedWebSearch("model-a", new AbortController().signal),
    "available",
  );
  const events = await collect(gated.stream(request()));

  assert.equal(await gated.getHostedWebSearchCapability("model-a"), "available");
  assert.equal(calls.length, 2);
  assert.equal(calls[1].tools.some((tool) => tool.kind === "hosted"), true);
  assert.deepEqual(events.map(({ type }) => type), [
    "hosted_web_search_call",
    "output_text.delta",
    "url_citation",
  ]);
});

test("unsupported probe falls back without hosted search and keeps web_read", async () => {
  const calls = [];
  const provider = {
    backendId: "codex:test",
    async listModels() { return []; },
    async *stream(candidate) {
      calls.push(candidate);
      if (calls.length === 1) {
        throw new ModelProviderError("unsupported_capability", "web_search is unsupported");
      }
      yield { type: "output_text.delta", delta: "Fallback answer" };
    },
  };
  const store = new MemoryCapabilities();
  const gated = new CapabilityGatedModelProvider(provider, store);
  assert.equal(
    await gated.reprobeHostedWebSearch("model-a", new AbortController().signal),
    "unavailable",
  );
  assert.deepEqual(await collect(gated.stream(request())), [
    { type: "output_text.delta", delta: "Fallback answer" },
  ]);
  assert.equal(await gated.getHostedWebSearchCapability("model-a"), "unavailable");
  assert.equal(calls.length, 2);
  assert.deepEqual(calls[1].tools, [localTool]);

  await collect(gated.stream(request()));
  assert.equal(calls.length, 3);
  assert.deepEqual(calls[2].tools, [localTool]);
});

test("an ordinary request leaves unknown capability unprobed", async () => {
  const calls = [];
  const provider = {
    backendId: "codex:test",
    async listModels() { return []; },
    async *stream(candidate) {
      calls.push(candidate);
      yield { type: "output_text.delta", delta: "No web needed" };
    },
  };
  const store = new MemoryCapabilities();
  const gated = new CapabilityGatedModelProvider(provider, store);
  await collect(gated.stream(request()));
  assert.equal(await gated.getHostedWebSearchCapability("model-a"), "unknown");
  assert.equal(calls.length, 1);
  assert.equal(calls[0].tools.some((tool) => tool.kind === "hosted"), false);
});

test("cached support retries once without search when the backend capability changes", async () => {
  const calls = [];
  const provider = {
    backendId: "codex:test",
    async listModels() { return []; },
    async *stream(candidate) {
      calls.push(candidate);
      if (candidate.tools.some((tool) => tool.kind === "hosted")) {
        yield { type: "output_text.delta", delta: "This partial attempt must be discarded." };
        throw new ModelProviderError("unsupported_capability", "parameter web_search is invalid");
      }
      yield { type: "output_text.delta", delta: "Recovered" };
    },
  };
  const store = new MemoryCapabilities();
  await store.setProviderCapability("codex:test", "model-a", "hosted_web_search", "available");
  const gated = new CapabilityGatedModelProvider(provider, store);
  assert.deepEqual(await collect(gated.stream(request())), [
    { type: "output_text.delta", delta: "Recovered" },
  ]);
  assert.equal(calls.length, 2);
  assert.equal(await gated.getHostedWebSearchCapability("model-a"), "unavailable");
});

test("a supported hosted response streams after a bounded capability prelude", async () => {
  let release;
  const gate = new Promise((resolve) => (release = resolve));
  const provider = {
    backendId: "codex:test",
    async listModels() { return []; },
    async *stream(candidate) {
      assert.equal(candidate.tools.some((tool) => tool.kind === "hosted"), true);
      yield { type: "output_text.delta", delta: "First" };
      await gate;
      yield { type: "output_text.delta", delta: " second" };
    },
  };
  const store = new MemoryCapabilities();
  await store.setProviderCapability("codex:test", "model-a", "hosted_web_search", "available");
  const gated = new CapabilityGatedModelProvider(provider, store);
  const iterator = gated.stream(request())[Symbol.asyncIterator]();
  const first = await Promise.race([
    iterator.next(),
    new Promise((_, reject) => setTimeout(() => reject(new Error("First delta did not stream")), 500)),
  ]);
  assert.deepEqual(first, {
    done: false,
    value: { type: "output_text.delta", delta: "First" },
  });
  release();
  assert.deepEqual(await iterator.next(), {
    done: false,
    value: { type: "output_text.delta", delta: " second" },
  });
  assert.equal((await iterator.next()).done, true);
});

test("stopping a hosted stream closes and aborts the underlying provider iterator", async () => {
  let closed = false;
  const provider = {
    backendId: "codex:test",
    async listModels() { return []; },
    async *stream(candidate) {
      try {
        yield { type: "output_text.delta", delta: "First" };
        await new Promise((_, reject) => {
          candidate.signal.addEventListener("abort", () => reject(new Error("aborted")), { once: true });
        });
      } finally {
        closed = true;
      }
    },
  };
  const store = new MemoryCapabilities();
  await store.setProviderCapability("codex:test", "model-a", "hosted_web_search", "available");
  const iterator = new CapabilityGatedModelProvider(provider, store)
    .stream(request())[Symbol.asyncIterator]();
  assert.equal((await iterator.next()).value.delta, "First");
  await iterator.return();
  assert.equal(closed, true);
});

test("a post-commit unsupported error retries once without replaying visible deltas", async () => {
  let release;
  const gate = new Promise((resolve) => (release = resolve));
  const calls = [];
  const provider = {
    backendId: "codex:test",
    async listModels() { return []; },
    async *stream(candidate) {
      calls.push(candidate);
      if (candidate.tools.some((tool) => tool.kind === "hosted")) {
        yield { type: "output_text.delta", delta: "Already visible" };
        await gate;
        throw new ModelProviderError("unsupported_capability", "late unsupported signal");
      }
      yield {
        type: "output_text.delta",
        delta: calls.length === 2 ? "Recovered without hosted search" : "Next run without hosted search",
      };
    },
  };
  const store = new MemoryCapabilities();
  await store.setProviderCapability("codex:test", "model-a", "hosted_web_search", "available");
  const gated = new CapabilityGatedModelProvider(provider, store);
  const iterator = gated.stream(request())[Symbol.asyncIterator]();
  assert.equal((await iterator.next()).value.delta, "Already visible");
  release();
  assert.deepEqual(await iterator.next(), {
    done: false,
    value: { type: "output_text.delta", delta: "Recovered without hosted search" },
  });
  assert.equal((await iterator.next()).done, true);
  assert.equal(calls.length, 2);
  assert.equal(await gated.getHostedWebSearchCapability("model-a"), "unavailable");
  assert.deepEqual(await collect(gated.stream(request())), [
    { type: "output_text.delta", delta: "Next run without hosted search" },
  ]);
  assert.equal(calls[1].tools.some((tool) => tool.kind === "hosted"), false);
  assert.equal(calls[2].tools.some((tool) => tool.kind === "hosted"), false);
});

test("the user can request a capability reprobe", async () => {
  let probes = 0;
  const provider = {
    backendId: "codex:test",
    async listModels() { return []; },
    async *stream(candidate) {
      probes += 1;
      assert.deepEqual(candidate.tools, [{ kind: "hosted", name: "web_search" }]);
      yield { type: "output_text.delta", delta: "OK" };
    },
  };
  const store = new MemoryCapabilities();
  await store.setProviderCapability("codex:test", "model-a", "hosted_web_search", "unavailable");
  const gated = new CapabilityGatedModelProvider(provider, store);
  assert.equal(
    await gated.reprobeHostedWebSearch("model-a", new AbortController().signal),
    "available",
  );
  assert.equal(probes, 1);
});
