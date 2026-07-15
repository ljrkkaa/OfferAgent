import type { InterviewCatalogResult } from "@offeragent/protocol";
import type { ModelConversationItem, ModelStreamEvent } from "./model-provider";
import { toolResultFor } from "./fake-provider-conversation";
import { questionIdentity } from "./fake-interview-evidence";

export function interviewOutput(delta: string): ModelStreamEvent {
  return { type: "output_text.delta", delta };
}

export function interviewReadResultFor(input: ModelConversationItem[], path: string) {
  return toolResultFor(
    input,
    "vault_read",
    (call) => (call.arguments as { path?: unknown }).path === path,
  );
}

export function interviewReadRequest(
  path: string,
  nextCallId: (prefix: string) => string,
): ModelStreamEvent {
  return {
    type: "local_tool_call",
    callId: nextCallId("fake-interview-read"),
    name: "vault_read",
    arguments: { path },
  };
}

export function candidateReadStep(
  input: ModelConversationItem[],
  candidatePaths: string[],
  nextCallId: (prefix: string) => string,
): ModelStreamEvent | undefined {
  for (const candidatePath of candidatePaths) {
    const candidateRead = interviewReadResultFor(input, candidatePath);
    if (!candidateRead) return interviewReadRequest(candidatePath, nextCallId);
    if (!candidateRead.result.ok || candidateRead.result.value.type !== "vault_read") {
      return candidateRead.result.ok
        ? interviewOutput("Candidate evidence returned an invalid result.")
        : interviewOutput(`Candidate evidence failed: ${candidateRead.result.error.message}`);
    }
  }
  return undefined;
}

export function matchingQuestionCandidates(
  input: ModelConversationItem[],
  candidates: InterviewCatalogResult["questionCandidates"],
  identity: string,
) {
  return candidates.filter((candidate) => {
    const candidateRead = interviewReadResultFor(input, candidate.path);
    return candidateRead?.result.ok &&
      candidateRead.result.value.type === "vault_read" &&
      questionIdentity(candidateRead.result.value.content) === identity;
  });
}
