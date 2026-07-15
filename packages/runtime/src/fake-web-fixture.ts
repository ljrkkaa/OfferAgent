import { WebReader } from "./web-read";

export const FAKE_WEB_FIXTURES = [
  "answer-research-conflicting",
  "answer-research-current",
  "answer-research-missing",
  "interview-url-failure",
  "interview-url-insufficient",
  "interview-url-success",
] as const;
export type FakeWebFixture = (typeof FAKE_WEB_FIXTURES)[number];

export function isFakeWebFixture(value: string): value is FakeWebFixture {
  return (FAKE_WEB_FIXTURES as readonly string[]).includes(value);
}

export function createFakeWebReader(fixture: FakeWebFixture): WebReader {
  const fakeFetch = async (input: URL | RequestInfo): Promise<Response> => {
    const url = String(input);
    if (fixture === "answer-research-missing") {
      return new Response("temporarily unavailable", { status: 503 });
    }
    if (fixture === "answer-research-conflicting") {
      return new Response(
        "<!doctype html><title>Conflicting cache guidance</title><main>" +
          "Current sources contain conflicting recommendations: one requires versioned writes and post-commit invalidation, " +
          "while another rejects invalidation and recommends unordered direct cache publication. The conflict is unresolved." +
          "</main>",
        { headers: { "content-type": "text/html; charset=utf-8" } },
      );
    }
    if (fixture === "answer-research-current") {
      return new Response(
        "<!doctype html><title>Current distributed cache consistency documentation</title><main>" +
          "Current official documentation confirms versioned writes and compare-and-set at the authoritative store. " +
          "Invalidate stale cache entries only after the authoritative commit succeeds so publication remains ordered." +
          "</main>",
        { headers: { "content-type": "text/html; charset=utf-8" } },
      );
    }
    if (fixture === "interview-url-failure") {
      return new Response("temporarily unavailable", { status: 503 });
    }
    if (fixture === "interview-url-insufficient") {
      return new Response(
        "<!doctype html><title>Sign in</title><main>Sign in to view this page.</main>",
        { headers: { "content-type": "text/html; charset=utf-8" } },
      );
    }
    if (url === "https://example.com/shared/backend-42") {
      return new Response(null, {
        status: 302,
        headers: { location: "https://example.com/interviews/backend-42" },
      });
    }
    return new Response(
      "<!doctype html><title>Example Backend Interview</title><main>" +
        "Candidate B interviewed for an Example Corp backend final round on 2026-07-01. " +
        "The interview asked how distributed cache consistency should be maintained. " +
        "FULL-PAGE-MARKER-URL-42 Additional page prose must not be copied into the Vault." +
        "</main>",
      { headers: { "content-type": "text/html; charset=utf-8" } },
    );
  };
  return new WebReader({
    fetch: fakeFetch as typeof fetch,
    resolve: async () => [{ address: "93.184.216.34" }],
  });
}
