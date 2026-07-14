import type { LocalToolName } from "@offeragent/protocol";
import type { ModelConversationItem } from "./model-provider";

export function toolResultFor(
  input: ModelConversationItem[],
  name: LocalToolName,
  predicate: (call: Extract<ModelConversationItem, { type: "local_tool_call" }>) => boolean = () => true,
): Extract<ModelConversationItem, { type: "local_tool_result" }> | undefined {
  for (let index = input.length - 1; index >= 0; index -= 1) {
    const result = input[index];
    if (result.type !== "local_tool_result") continue;
    const call = input.find(
      (candidate): candidate is Extract<ModelConversationItem, { type: "local_tool_call" }> =>
        candidate.type === "local_tool_call" &&
        candidate.callId === result.callId &&
        candidate.name === name,
    );
    if (call && predicate(call)) return result;
  }
  return undefined;
}

export function toolResultForAfter(
  input: ModelConversationItem[],
  name: LocalToolName,
  afterIndex: number,
  predicate: (call: Extract<ModelConversationItem, { type: "local_tool_call" }>) => boolean,
): Extract<ModelConversationItem, { type: "local_tool_result" }> | undefined {
  for (let index = input.length - 1; index > afterIndex; index -= 1) {
    const result = input[index];
    if (result.type !== "local_tool_result") continue;
    const call = input.find(
      (candidate, callIndex): candidate is Extract<ModelConversationItem, { type: "local_tool_call" }> =>
        callIndex > afterIndex &&
        candidate.type === "local_tool_call" &&
        candidate.callId === result.callId &&
        candidate.name === name,
    );
    if (call && predicate(call)) return result;
  }
  return undefined;
}
