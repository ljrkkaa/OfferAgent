import assert from "node:assert/strict";
import path from "node:path";
import test from "node:test";
import { createRequire } from "node:module";
import { fileURLToPath } from "node:url";

const require = createRequire(import.meta.url);
const repositoryRoot = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..");
const { WebReader } = require(path.join(repositoryRoot, "packages", "runtime", "dist", "web-read.js"));

test("web_read extracts bounded page content and source metadata", async () => {
  const requests = [];
  const reader = new WebReader({
    fetch: async (url, init) => {
      requests.push({ url: String(url), redirect: init.redirect });
      return new Response(
        "<!doctype html><html><head><title>Example &amp; Docs</title><style>hidden</style></head>" +
          "<body><main><h1>Hello</h1><script>ignored()</script><p>Useful&nbsp;text.</p></main></body></html>",
        { headers: { "content-type": "text/html; charset=utf-8" } },
      );
    },
  });
  const result = await reader.execute({ url: "https://example.com/docs", maxBytes: 24 });

  assert.equal(result.ok, true);
  assert.equal(result.value.type, "web_read");
  assert.equal(result.value.url, "https://example.com/docs");
  assert.equal(result.value.finalUrl, "https://example.com/docs");
  assert.equal(result.value.sourceTitle, "Example & Docs");
  assert.match(result.value.sourceFingerprint, /^sha256:[a-f0-9]{64}$/);
  assert.equal(result.value.contentType, "text/html");
  assert.equal(Buffer.byteLength(result.value.content, "utf8") <= 24, true);
  assert.equal(result.value.truncated, true);
  assert.doesNotMatch(result.value.content, /hidden|ignored/);
  assert.deepEqual(requests, [{ url: "https://example.com/docs", redirect: "manual" }]);

  const complete = await reader.execute({ url: "https://example.com/docs", maxBytes: 1_024 });
  assert.equal(complete.ok, true);
  assert.equal(complete.value.truncated, false);
  assert.equal(complete.value.sourceFingerprint, result.value.sourceFingerprint);
});

test("web_read rejects unsafe URLs, redirect abuse, oversized, and unreadable responses", async (t) => {
  await t.test("unsafe scheme and local targets", async () => {
    const reader = new WebReader({ fetch: async () => { throw new Error("must not fetch"); } });
    for (const url of [
      "file:///etc/passwd",
      "http://localhost/private",
      "http://127.0.0.1/",
      "http://[::1]/",
      "http://[fc00::1]/",
      "http://[::ffff:127.0.0.1]/",
      "http://198.18.0.1/",
      "http://192.0.2.1/",
      "http://[fec0::1]/",
      "http://[ff00::1]/",
      "http://[2001:db8::1]/",
      "http://[2002:7f00:1::1]/",
    ]) {
      const result = await reader.execute({ url });
      assert.equal(result.ok, false);
      assert.equal(result.error.code, "unsafe_url");
    }
  });

  await t.test("redirect loop", async () => {
    const reader = new WebReader({
      maxRedirects: 2,
      fetch: async (url) => new Response(null, {
        status: 302,
        headers: { location: String(url).endsWith("/a") ? "/b" : "/a" },
      }),
    });
    const result = await reader.execute({ url: "https://example.com/a" });
    assert.equal(result.ok, false);
    assert.equal(result.error.code, "redirect_error");

    const malformed = new WebReader({
      fetch: async () => new Response(null, { status: 302, headers: { location: "http://[" } }),
    });
    const malformedResult = await malformed.execute({ url: "https://example.com/start" });
    assert.equal(malformedResult.ok, false);
    assert.equal(malformedResult.error.code, "redirect_error");

    let requests = 0;
    const mappedLoopback = new WebReader({
      fetch: async () => {
        requests += 1;
        return new Response(null, {
          status: 302,
          headers: { location: "http://[::ffff:127.0.0.1]/private" },
        });
      },
    });
    const mappedResult = await mappedLoopback.execute({ url: "https://example.com/start" });
    assert.equal(mappedResult.ok, false);
    assert.equal(mappedResult.error.code, "unsafe_url");
    assert.equal(requests, 1);

    const run = new AbortController();
    let cancelledRequests = 0;
    const cancelledRedirect = new WebReader({
      fetch: async () => {
        cancelledRequests += 1;
        run.abort();
        return new Response(null, { status: 302, headers: { location: "/next" } });
      },
    });
    const cancelledResult = await cancelledRedirect.execute(
      { url: "https://example.com/start" },
      run.signal,
    );
    assert.equal(cancelledResult.ok, false);
    assert.equal(cancelledResult.error.code, "tool_error");
    assert.equal(cancelledRequests, 1);
  });

  await t.test("oversized declared and streamed bodies", async () => {
    const declared = new WebReader({
      maxResponseBytes: 16,
      fetch: async () => new Response("tiny", {
        headers: { "content-type": "text/plain", "content-length": "17" },
      }),
    });
    assert.equal((await declared.execute({ url: "https://example.com" })).error.code, "response_too_large");

    const streamed = new WebReader({
      maxResponseBytes: 16,
      fetch: async () => new Response("x".repeat(17), {
        headers: { "content-type": "text/plain" },
      }),
    });
    assert.equal((await streamed.execute({ url: "https://example.com" })).error.code, "response_too_large");
  });

  await t.test("unreadable media", async () => {
    const reader = new WebReader({
      fetch: async () => new Response(new Uint8Array([0, 1, 2]), {
        headers: { "content-type": "image/png" },
      }),
    });
    const result = await reader.execute({ url: "https://example.com/image.png" });
    assert.equal(result.ok, false);
    assert.equal(result.error.code, "unreadable_content");

    const invalidText = new WebReader({
      fetch: async () => new Response(new Uint8Array([0xff, 0xfe, 0xfd]), {
        headers: { "content-type": "text/plain; charset=utf-8" },
      }),
    });
    const invalidResult = await invalidText.execute({ url: "https://example.com/broken.txt" });
    assert.equal(invalidResult.ok, false);
    assert.equal(invalidResult.error.code, "unreadable_content");
  });

  await t.test("rejected responses cancel their bodies", async () => {
    let cancellations = 0;
    const body = () => new ReadableStream({
      cancel() {
        cancellations += 1;
      },
    });
    const responses = [
      new Response(body(), { status: 404, headers: { "content-type": "text/plain" } }),
      new Response(body(), { headers: { "content-type": "image/png" } }),
      new Response(body(), {
        headers: { "content-type": "text/plain", "content-length": "17" },
      }),
    ];
    const reader = new WebReader({
      maxResponseBytes: 16,
      fetch: async () => responses.shift(),
    });

    for (let index = 0; index < 3; index += 1) {
      const result = await reader.execute({ url: `https://example.com/${index}` });
      assert.equal(result.ok, false);
    }
    assert.equal(cancellations, 3);
  });

  await t.test("stalled response body", async () => {
    const reader = new WebReader({
      timeoutMs: 10,
      fetch: async (_url, init) => new Response(new ReadableStream({
        start(controller) {
          init.signal.addEventListener("abort", () => controller.error(new Error("aborted")));
        },
      }), { headers: { "content-type": "text/plain" } }),
    });
    const result = await reader.execute({ url: "https://example.com/slow" });
    assert.equal(result.ok, false);
    assert.equal(result.error.code, "unreadable_content");
    assert.match(result.error.message, /timed out/i);
  });

  await t.test("Agent Run cancellation aborts a stalled response body", async () => {
    const run = new AbortController();
    const reader = new WebReader({
      timeoutMs: 60_000,
      fetch: async (_url, init) => new Response(new ReadableStream({
        start(controller) {
          init.signal.addEventListener("abort", () => controller.error(new Error("aborted")));
        },
      }), { headers: { "content-type": "text/plain" } }),
    });
    const pending = reader.execute({ url: "https://example.com/slow" }, run.signal);
    run.abort();
    const result = await pending;
    assert.equal(result.ok, false);
    assert.equal(result.error.code, "tool_error");
    assert.match(result.error.message, /cancelled/i);
  });
});
