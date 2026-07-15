import type { ModelConversationItem, ModelRequest, ModelStreamEvent } from "./model-provider";
import { frontmatterField } from "./fake-interview-evidence";
import { interviewUrlIngestionEvent } from "./fake-interview-url-scenario";
import { toolResultFor } from "./fake-provider-conversation";
import {
  candidateReadStep,
  interviewOutput as output,
} from "./fake-interview-workflow";

const SELECTED_URL = "https://example.com/shared/backend-42";

function parseLocalDate(instructions: string): Date {
  const value = /OfferAgent current local date:\s*(\d{4}-\d{2}-\d{2})/u.exec(instructions)?.[1];
  const parsed = value ? new Date(`${value}T12:00:00`) : new Date();
  return Number.isNaN(parsed.getTime()) ? new Date() : parsed;
}

function formatLocalDate(date: Date): string {
  const year = date.getFullYear();
  const month = String(date.getMonth() + 1).padStart(2, "0");
  const day = String(date.getDate()).padStart(2, "0");
  return `${year}-${month}-${day}`;
}

function subtractCalendarMonths(date: Date, months: number): Date {
  const target = new Date(date.getFullYear(), date.getMonth() - months, 1, 12);
  const lastDay = new Date(target.getFullYear(), target.getMonth() + 1, 0, 12).getDate();
  target.setDate(Math.min(date.getDate(), lastDay));
  return target;
}

function userInput(request: ModelRequest): string {
  for (let index = request.input.length - 1; index >= 0; index -= 1) {
    const item = request.input[index];
    if (item.type === "user_message") return item.text;
  }
  return "";
}

function researchScope(request: ModelRequest) {
  const input = userInput(request);
  const explicitRange = /(\d{4}-\d{2}-\d{2})[\s\S]*?(\d{4}-\d{2}-\d{2})/u.exec(input);
  const today = parseLocalDate(request.instructions);
  const rangeStart = explicitRange?.[1] ?? formatLocalDate(subtractCalendarMonths(today, 6));
  const rangeEnd = explicitRange?.[2] ?? formatLocalDate(today);
  const ios = /iOS Engineer|SwiftUI/iu.test(input);
  const company = "Example Corp";
  const position = ios ? "iOS Engineer" : "Backend Engineer";
  const technicalDirection = ios ? "SwiftUI" : "distributed systems";
  return {
    company,
    explicit: Boolean(explicitRange),
    input,
    position,
    query: `${company} ${position} ${technicalDirection} interviews from ${rangeStart} through ${rangeEnd}`,
    rangeEnd,
    rangeStart,
    technicalDirection,
  };
}

function latestSelectedWebReadCallIndex(input: ModelConversationItem[]): number {
  for (let index = input.length - 1; index >= 0; index -= 1) {
    const item = input[index];
    if (
      item.type === "local_tool_call" &&
      item.name === "web_read" &&
      (item.arguments as { url?: unknown }).url === SELECTED_URL
    ) {
      return index;
    }
  }
  return -1;
}

export function publicInterviewResearchEvents(
  request: ModelRequest,
  nextCallId: (prefix: string) => string,
): ModelStreamEvent[] {
  const scope = researchScope(request);
  const researchCatalog = toolResultFor(
    request.input,
    "interview_catalog",
    (call) => !(call.arguments as { canonicalUrl?: unknown }).canonicalUrl,
  );
  if (!researchCatalog) {
    return [{
      type: "local_tool_call",
      callId: nextCallId("fake-public-research-catalog"),
      name: "interview_catalog",
      arguments: { query: scope.query, limit: 10 },
    }];
  }
  if (!researchCatalog.result.ok || researchCatalog.result.value.type !== "interview_catalog") {
    return [researchCatalog.result.ok
      ? output("The Interview Catalog returned an invalid public-research result.")
      : output(`Interview Catalog failed: ${researchCatalog.result.error.message}`)];
  }

  const existingPaths = researchCatalog.result.value.experienceCandidates.map(({ path }) => path);
  const candidateStep = candidateReadStep(request.input, existingPaths, nextCallId);
  if (candidateStep) return [candidateStep];
  const existingUrls = new Set(
    existingPaths.flatMap((path) => {
      const read = toolResultFor(
        request.input,
        "vault_read",
        (call) => (call.arguments as { path?: unknown }).path === path,
      );
      return read?.result.ok && read.result.value.type === "vault_read"
        ? [frontmatterField(read.result.value.content, "source-url")].filter(
            (value): value is string => Boolean(value),
          )
        : [];
    }),
  );

  const probe = toolResultFor(request.input, "hosted_web_search_probe");
  if (!probe) {
    return [{
      type: "local_tool_call",
      callId: nextCallId("fake-public-research-probe"),
      name: "hosted_web_search_probe",
      arguments: { query: scope.query },
    }];
  }
  if (
    !probe.result.ok ||
    probe.result.value.type !== "hosted_web_search_probe" ||
    probe.result.value.status !== "available"
  ) {
    return [output("Public interview research could not search the requested scope because Hosted Web Search is unavailable.")];
  }

  if (/Do not broaden any part of the scope/iu.test(scope.input)) {
    return [
      { type: "hosted_web_search_call", callId: nextCallId("fake-public-search"), sources: [] },
      output(
        `Insufficient matching public results for ${scope.company} ${scope.position} ${scope.technicalDirection} from ${scope.rangeStart} through ${scope.rangeEnd}; the requested scope was not widened.`,
      ),
    ];
  }

  if (/do not ingest/iu.test(scope.input)) {
    return [
      {
        type: "hosted_web_search_call",
        callId: nextCallId("fake-public-search"),
        sources: [
          { url: "https://example.com/interviews/backend-2025-03", title: "Example backend systems interview - March 2025" },
          { url: "https://example.com/interviews/backend-2025-02", title: "Example backend platform interview - February 2025" },
        ],
      },
      output(`Found 2 scoped public matches from ${scope.rangeStart} through ${scope.rangeEnd}; no ingestion was requested.`),
    ];
  }

  const rankedSources = [
    { url: SELECTED_URL, title: "Example backend distributed-systems interview with specific questions" },
    { url: "https://example.com/interviews/backend-platform-older", title: "Example backend platform interview" },
    { url: "https://example.com/interviews/already-stored", title: "Already stored Example backend interview" },
  ].filter(({ url }) => !existingUrls.has(url));
  const selectedWebRead = toolResultFor(
    request.input,
    "web_read",
    (call) => (call.arguments as { url?: unknown }).url === SELECTED_URL,
  );
  if (!selectedWebRead) {
    return [
      {
        type: "hosted_web_search_call",
        callId: nextCallId("fake-public-search"),
        sources: rankedSources.slice(0, 5),
      },
      {
        type: "local_tool_call",
        callId: nextCallId("fake-public-selected-read"),
        name: "web_read",
        arguments: { url: SELECTED_URL, maxBytes: 32_768 },
      },
    ];
  }

  const selectedCallIndex = latestSelectedWebReadCallIndex(request.input);
  const canonicalEvent = interviewUrlIngestionEvent(
    { ...request, input: request.input.slice(selectedCallIndex) },
    nextCallId,
  );
  if (canonicalEvent.type !== "output_text.delta") return [canonicalEvent];
  if (/stored and recurring Question synchronized atomically/u.test(canonicalEvent.delta)) {
    return [output(
      `Researched ${rankedSources.length} scoped public results, excluded ${existingUrls.size} existing Experience, and ingested the top new match through the canonical atomic URL path.`,
    )];
  }
  return [canonicalEvent];
}
