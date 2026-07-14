import { WebReader } from "./web-read";

export const FAKE_WEB_FIXTURES = [
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
