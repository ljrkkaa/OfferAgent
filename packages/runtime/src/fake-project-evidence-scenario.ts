import type { ModelRequest, ModelStreamEvent } from "./model-provider";
import { toolResultFor } from "./fake-provider-conversation";

export function projectEvidenceQuestionEvent(
  request: ModelRequest,
  nextCallId: (prefix: string) => string,
): ModelStreamEvent {
  const list = toolResultFor(request.input, "project_list");
  if (!list) {
    return {
      type: "local_tool_call",
      callId: nextCallId("fake-project-list"),
      name: "project_list",
      arguments: { projectId: "offeragent", directory: "src", limit: 50 },
    };
  }
  if (!list.result.ok || list.result.value.type !== "project_list") {
    return { type: "output_text.delta", delta: "I cannot answer from first-person Project Evidence because the registered project is unavailable." };
  }
  const search = toolResultFor(request.input, "project_search");
  if (!search) {
    return {
      type: "local_tool_call",
      callId: nextCallId("fake-project-search"),
      name: "project_search",
      arguments: { projectId: "offeragent", query: "cache invalidation version", limit: 10 },
    };
  }
  if (!search.result.ok || search.result.value.type !== "project_search") {
    return { type: "output_text.delta", delta: "The registered project search failed, so I cannot support an implementation claim." };
  }
  const source = search.result.value.entries[0];
  if (!source) {
    return { type: "output_text.delta", delta: "The registered project contains no exact evidence for that question. Gap: the implementation is not documented in the readable project scope." };
  }
  const read = toolResultFor(request.input, "project_read", (call) =>
    (call.arguments as { path?: unknown }).path === source.path);
  if (!read) {
    return {
      type: "local_tool_call",
      callId: nextCallId("fake-project-read"),
      name: "project_read",
      arguments: { projectId: "offeragent", path: source.path, lineStart: 1, lineEnd: 80 },
    };
  }
  if (!read.result.ok || read.result.value.type !== "project_read") {
    return { type: "output_text.delta", delta: "The candidate source could not be read exactly, so I cannot support an implementation claim." };
  }
  const evidence = read.result.value;
  const supportsVersioning = /version/iu.test(evidence.content) && /invalidat/iu.test(evidence.content);
  if (!supportsVersioning) {
    return { type: "output_text.delta", delta: `The exact source does not support the proposed cache-safety claim. Gap: no versioned invalidation behavior is visible in [Project Evidence: ${evidence.evidencePath}:${evidence.lineStart}-${evidence.lineEnd}].` };
  }
  return {
    type: "output_text.delta",
    delta: `I made cache invalidation safe by rejecting stale versions before deleting an entry, so an older request cannot invalidate a newer cached value. [Project Evidence: ${evidence.evidencePath}:${evidence.lineStart}-${evidence.lineEnd}] Gap: the readable source does not provide production latency, hit-rate, or incident metrics, so I would not claim those outcomes.`,
  };
}
