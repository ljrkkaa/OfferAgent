import type { ResearchBrowserResult } from "@offeragent/protocol";
import type { ModelConversationItem, ModelRequest, ModelStreamEvent } from "./model-provider";
import { frontmatterField } from "./fake-interview-evidence";
import { interviewUrlIngestionEvent } from "./fake-interview-url-scenario";
import { toolResultFor } from "./fake-provider-conversation";
import { candidateReadStep, interviewOutput as output } from "./fake-interview-workflow";

const SEARCH_URL = "https://dynamic.example/search/example-backend";

function researchResult(
  request: ModelRequest,
  action: ResearchBrowserResult["action"],
) {
  return toolResultFor(
    request.input,
    "research_browser",
    (call) => (call.arguments as { action?: unknown }).action === action,
  );
}

function syntheticCanonicalInput(input: ModelConversationItem[]): ModelConversationItem[] {
  const readCallIndex = input.findLastIndex(
    (item) => item.type === "local_tool_call" && item.name === "research_browser" &&
      (item.arguments as { action?: unknown }).action === "read",
  );
  if (readCallIndex < 0) return [];
  return input.slice(readCallIndex).map((item): ModelConversationItem => {
    if (item.type === "local_tool_call" && item.name === "research_browser") {
      return {
        ...item,
        name: "web_read",
        arguments: { url: SEARCH_URL, maxBytes: 32_768 },
      };
    }
    if (item.type !== "local_tool_result") return item;
    const source = item.result.ok && item.result.value.type === "research_browser"
      ? item.result.value
      : undefined;
    if (!source || source.action !== "read" || source.status !== "ready" ||
        source.content === undefined || source.sourceFingerprint === undefined) return item;
    return {
      ...item,
      result: {
        ok: true,
        value: {
          type: "web_read",
          url: source.url,
          finalUrl: source.url,
          sourceTitle: source.title,
          sourceFingerprint: source.sourceFingerprint,
          contentType: "text/plain; charset=utf-8",
          content: source.content,
          truncated: source.truncated ?? false,
        },
      },
    };
  });
}

export function dynamicInterviewResearchEvent(
  request: ModelRequest,
  nextCallId: (prefix: string) => string,
): ModelStreamEvent {
  const researchCatalog = toolResultFor(
    request.input,
    "interview_catalog",
    (call) => !(call.arguments as { canonicalUrl?: unknown }).canonicalUrl,
  );
  if (!researchCatalog) {
    return {
      type: "local_tool_call",
      callId: nextCallId("fake-dynamic-research-catalog"),
      name: "interview_catalog",
      arguments: {
        query: "Example Corp Backend Engineer distributed systems dynamic interviews 2026-01-15 through 2026-07-15",
        limit: 10,
      },
    };
  }
  if (!researchCatalog.result.ok || researchCatalog.result.value.type !== "interview_catalog") {
    return output("The Interview Catalog returned an invalid dynamic-research result.");
  }
  const existingPaths = researchCatalog.result.value.experienceCandidates.map(({ path }) => path);
  const candidateStep = candidateReadStep(request.input, existingPaths, nextCallId);
  if (candidateStep) return candidateStep;
  const existingUrls = new Set(existingPaths.flatMap((path) => {
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
  }));

  const opened = researchResult(request, "open");
  if (!opened) {
    return {
      type: "local_tool_call",
      callId: nextCallId("fake-dynamic-open"),
      name: "research_browser",
      arguments: { action: "open", url: SEARCH_URL },
    };
  }
  if (!opened.result.ok || opened.result.value.type !== "research_browser") {
    return output("The isolated Research Browser could not open the requested dynamic source.");
  }
  if (opened.result.value.status === "login_required") {
    return output("Dynamic research paused: complete login or the security check manually in the visible isolated Research Browser, then retry. No credentials or Vault changes were requested.");
  }

  const enumerated = researchResult(request, "enumerate");
  if (!enumerated) {
    return {
      type: "local_tool_call",
      callId: nextCallId("fake-dynamic-enumerate"),
      name: "research_browser",
      arguments: { action: "enumerate", limit: 10 },
    };
  }
  if (!enumerated.result.ok || enumerated.result.value.type !== "research_browser" ||
      enumerated.result.value.status !== "ready" || enumerated.result.value.entries === undefined) {
    return output("The isolated Research Browser returned an invalid bounded result list.");
  }
  const newCandidates = enumerated.result.value.entries.filter(({ url }) => !existingUrls.has(url));
  const selected = newCandidates.find(({ title }) =>
    /Example Corp/iu.test(title) && /Backend Engineer/iu.test(title) &&
    /distributed systems|distributed cache/iu.test(title) && /2026/iu.test(title) &&
    /specific questions|consistency question/iu.test(title),
  ) ?? newCandidates[0];
  if (!selected) {
    return output("Insufficient matching dynamic interview results in the requested scope; the scope was not widened and no Vault changes were proposed.");
  }

  const followed = researchResult(request, "follow");
  if (!followed) {
    return {
      type: "local_tool_call",
      callId: nextCallId("fake-dynamic-follow"),
      name: "research_browser",
      arguments: { action: "follow", targetId: selected.id },
    };
  }
  const rendered = researchResult(request, "read");
  if (!rendered) {
    return {
      type: "local_tool_call",
      callId: nextCallId("fake-dynamic-read"),
      name: "research_browser",
      arguments: { action: "read", maxBytes: 32_768 },
    };
  }
  if (!rendered.result.ok || rendered.result.value.type !== "research_browser" ||
      rendered.result.value.status !== "ready" || !rendered.result.value.content ||
      rendered.result.value.content.length < 120) {
    return output("The dynamic source did not contain enough interview evidence; no Vault changes were proposed.");
  }

  const canonicalEvent = interviewUrlIngestionEvent(
    { ...request, input: syntheticCanonicalInput(request.input) },
    nextCallId,
  );
  if (canonicalEvent.type !== "output_text.delta") return canonicalEvent;
  return /stored and recurring Question synchronized atomically/u.test(canonicalEvent.delta)
    ? output("Ignored hostile page instructions, preserved the requested scope, and ingested the selected dynamic result through the canonical atomic URL path.")
    : canonicalEvent;
}
