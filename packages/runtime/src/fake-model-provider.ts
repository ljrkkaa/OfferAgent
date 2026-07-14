import type { LocalToolName, ModelDescriptor } from "@offeragent/protocol";
import {
  ModelProviderError,
  type ModelConversationItem,
  type ModelProvider,
  type ModelRequest,
  type ModelStreamEvent,
} from "./model-provider";
import { MEMORY_SELECTOR_INSTRUCTIONS, type MemorySelectionInput } from "./planning-memory";

function findLatest<T extends ModelConversationItem>(
  input: ModelConversationItem[],
  predicate: (item: ModelConversationItem) => item is T,
): T | undefined {
  for (let index = input.length - 1; index >= 0; index -= 1) {
    const item = input[index];
    if (predicate(item)) return item;
  }
  return undefined;
}

const MODEL: ModelDescriptor = {
  id: "fake-interview-model",
  label: "Fake Interview Model",
  supportsFastMode: true,
};

export class FakeModelProvider implements ModelProvider {
  readonly backendId = "fake";
  #toolCallSequence = 0;

  async listModels(): Promise<ModelDescriptor[]> {
    return [MODEL];
  }

  async *stream(request: ModelRequest): AsyncIterable<ModelStreamEvent> {
    if (request.signal.aborted) {
      throw new ModelProviderError("transport_error", "The Agent Run was interrupted.");
    }
    if (request.model !== MODEL.id) {
      throw new ModelProviderError(
        "model_unavailable",
        `The selected model '${request.model}' is not available.`,
      );
    }
    const userInput = findLatest(
      request.input,
      (item): item is Extract<ModelConversationItem, { type: "user_message" }> =>
        item.type === "user_message",
    )?.text ?? "";
    if (request.instructions === MEMORY_SELECTOR_INSTRUCTIONS) {
      let selected: string[] = [];
      try {
        const selection = JSON.parse(userInput) as MemorySelectionInput;
        const context = [
          selection.request,
          ...selection.conversationContext.map(({ text }) => text),
        ].join(" ");
        const contextFeatures = textFeatures(context);
        const scored = selection.topics
          .map((topic, index) => ({
            index,
            path: topic.path,
            score: overlapScore(contextFeatures, textFeatures(`${topic.name} ${topic.description}`)),
          }))
        const maximumScore = Math.max(0, ...scored.map(({ score }) => score));
        selected = scored
          .filter(({ score }) => score >= Math.max(8, maximumScore * 0.2))
          .sort((left, right) => right.score - left.score || left.index - right.index)
          .slice(0, 5)
          .map(({ path }) => path);
      } catch {
        selected = [];
      }
      yield { type: "output_text.delta", delta: JSON.stringify(selected) };
      return;
    }
    if (userInput.trim() === "planning_memory_acceptance") {
      const feedback = /Relevant Feedback Memory:\n([\s\S]*?)(?:\n\nOther Relevant Planning Memory:|\n\nModel defaults)/.exec(
        request.instructions,
      )?.[1];
      const planning = /Other Relevant Planning Memory:\n([\s\S]*?)(?:\n\nModel defaults)/.exec(
        request.instructions,
      )?.[1];
      yield {
        type: "output_text.delta",
        delta: feedback
          ? "OfferAgent applied relevant Feedback Memory: planned study remains future work, not completed evidence."
          : planning
            ? "OfferAgent applied relevant Planning Memory to this response."
          : "OfferAgent found no relevant Planning Memory.",
      };
      return;
    }
    const precedence = /^planning_memory_precedence\s+(\w+)$/i.exec(userInput.trim());
    if (precedence) {
      const contract = /Agent Contract \(highest instruction priority\):\n([\s\S]*?)(?:\n\nRequested Local Skills|\n\nInstruction precedence)/.exec(
        request.instructions,
      )?.[1];
      const contractChoice = /PRECEDENCE_CHOICE:\s*(\w+)/.exec(contract ?? "")?.[1];
      const feedback = /Relevant Feedback Memory:\n([\s\S]*?)(?:\n\nOther Relevant Planning Memory:|\n\nModel defaults)/.exec(
        request.instructions,
      )?.[1];
      const memoryChoice = /PRECEDENCE_CHOICE:\s*(\w+)/.exec(feedback ?? "")?.[1];
      yield {
        type: "output_text.delta",
        delta: `PRECEDENCE_CHOICE: ${contractChoice ?? precedence[1] ?? memoryChoice ?? "default"}`,
      };
      return;
    }
    if (userInput.trim() === "conversation_context") {
      yield {
        type: "output_text.delta",
        delta: JSON.stringify(
          request.input.map((item) =>
            item.type === "user_message" || item.type === "assistant_message"
              ? {
                  role: item.type === "user_message" ? "user" : "assistant",
                  marker: item.text.slice(0, 1),
                  length: item.text.length,
                }
              : { role: item.type },
          ),
        ),
      };
      return;
    }
    if (userInput.trim() === "context_tool_demo") {
      const result = findLatest(
        request.input,
        (item): item is Extract<ModelConversationItem, { type: "local_tool_result" }> =>
          item.type === "local_tool_result" && item.callId.startsWith("fake-context-read-"),
      );
      if (!result) {
        this.#toolCallSequence += 1;
        yield {
          type: "local_tool_call",
          callId: `fake-context-read-${this.#toolCallSequence}`,
          name: "vault_read",
          arguments: { path: "notes/context.md", lineStart: 1, lineEnd: 1 },
        };
        return;
      }
      yield { type: "output_text.delta", delta: "Tool completed." };
      return;
    }
    if (userInput.trim() === "可以的") {
      const priorResponse = findLatest(
        request.input,
        (item): item is Extract<ModelConversationItem, { type: "assistant_message" }> =>
          item.type === "assistant_message",
      );
      yield {
        type: "output_text.delta",
        delta: priorResponse
          ? `OfferAgent confirmed: ${priorResponse.text}`
          : "OfferAgent cannot confirm without prior context.",
      };
      return;
    }
    if (userInput.trim() === "hosted_search_demo") {
      yield {
        type: "hosted_web_search_call",
        callId: "fake-hosted-search",
        sources: [{ url: "https://example.com/source", title: "Example source" }],
      };
      yield { type: "output_text.delta", delta: "A cited answer [1]" };
      yield {
        type: "url_citation",
        citation: {
          url: "https://example.com/source",
          title: "Example source",
          startIndex: 15,
          endIndex: 18,
        },
      };
      return;
    }
    if (userInput.trim() === "citation_then_tool") {
      const toolResult = findLatest(
        request.input,
        (item): item is Extract<ModelConversationItem, { type: "local_tool_result" }> =>
          item.type === "local_tool_result" && item.callId.startsWith("fake-citation-read-"),
      );
      if (!toolResult) {
        this.#toolCallSequence += 1;
        yield { type: "output_text.delta", delta: "Old cited [1]" };
        yield {
          type: "url_citation",
          citation: {
            url: "https://example.com/old",
            title: "Old source",
            startIndex: 10,
            endIndex: 13,
          },
        };
        yield {
          type: "local_tool_call",
          callId: `fake-citation-read-${this.#toolCallSequence}`,
          name: "vault_read",
          arguments: { path: "notes/citation.md", lineStart: 1, lineEnd: 1 },
        };
        return;
      }
      yield { type: "output_text.delta", delta: "Final answer without a citation" };
      return;
    }
    const staleFlow = /^stale_evidence_flow\s+([^\s]+)$/i.exec(userInput.trim());
    if (staleFlow) {
      const calls = request.input.filter((item) => item.type === "local_tool_call");
      const results = request.input.filter((item) => item.type === "local_tool_result");
      const successfulRead = results.find(
        (item) => item.result.ok && item.result.value.type === "vault_read",
      );
      const staleResult = results.find(
        (item) => !item.result.ok && item.result.error.code === "stale_evidence",
      );
      const hasSearchCall = calls.some((item) => item.name === "vault_search");
      if (!successfulRead && !staleResult) {
        this.#toolCallSequence += 1;
        yield {
          type: "local_tool_call",
          callId: `fake-stale-read-${this.#toolCallSequence}`,
          name: "vault_read",
          arguments: { path: `${staleFlow[1]} `, lineStart: 1, lineEnd: 1 },
        };
        return;
      }
      if (successfulRead && !hasSearchCall) {
        this.#toolCallSequence += 1;
        yield {
          type: "local_tool_call",
          callId: `fake-stale-search-${this.#toolCallSequence}`,
          name: "vault_search",
          arguments: { query: "content", exactPhrase: true },
        };
        return;
      }
      if (staleResult && !successfulRead) {
        this.#toolCallSequence += 1;
        yield {
          type: "local_tool_call",
          callId: `fake-stale-reread-${this.#toolCallSequence}`,
          name: "vault_read",
          arguments: { path: staleFlow[1], lineStart: 1, lineEnd: 1 },
        };
        return;
      }
      if (staleResult && successfulRead) {
        const readContents = results
          .filter(
            (item) => item.result.ok && item.result.value.type === "vault_read",
          )
          .map((item) =>
            item.result.ok && item.result.value.type === "vault_read"
              ? item.result.value.content
              : "",
          );
        yield { type: "output_text.delta", delta: "OfferAgent received: " };
        yield { type: "output_text.delta", delta: JSON.stringify(readContents) };
        return;
      }
    }
    const toolResult = findLatest(
      request.input,
      (item): item is Extract<ModelConversationItem, { type: "local_tool_result" }> =>
        item.type === "local_tool_result",
    );
    if (!toolResult) {
      const toolRequest = fakeToolRequest(userInput);
      if (toolRequest) {
        this.#toolCallSequence += 1;
        yield {
          type: "local_tool_call",
          callId: `fake-${toolRequest.name}-call-${this.#toolCallSequence}`,
          name: toolRequest.name,
          arguments: toolRequest.arguments,
        };
        return;
      }
    }
    yield { type: "output_text.delta", delta: "OfferAgent received: " };
    if (request.signal.aborted) {
      throw new ModelProviderError("transport_error", "The Agent Run was interrupted.");
    }
    yield {
      type: "output_text.delta",
      delta: toolResult ? JSON.stringify(toolResult.result) : userInput,
    };
  }
}

function textFeatures(value: string): Set<string> {
  const normalized = value.toLocaleLowerCase().replace(/\s+/g, " ").trim();
  const features = new Set(normalized.match(/[\p{L}\p{N}]{2,}/gu) ?? []);
  const compact = normalized.replace(/[^\p{L}\p{N}]/gu, "");
  for (let size = 2; size <= 4; size += 1) {
    for (let index = 0; index + size <= compact.length; index += 1) {
      features.add(compact.slice(index, index + size));
    }
  }
  return features;
}

function overlapScore(left: Set<string>, right: Set<string>): number {
  let score = 0;
  for (const feature of right) if (left.has(feature)) score += feature.length;
  return score;
}

function fakeToolRequest(input: string): { name: LocalToolName; arguments: unknown } | undefined {
  const dailyContext = /^daily_note_context(?:\s+([^\s]+))?$/i.exec(input.trim());
  if (dailyContext) {
    return {
      name: "daily_note_context",
      arguments: dailyContext[1] ? { date: dailyContext[1] } : {},
    };
  }
  const webRead = /^web_read\s+(.+)$/i.exec(input.trim());
  if (webRead) {
    return { name: "web_read", arguments: { url: webRead[1].trim() } };
  }
  const change = /^vault_propose_changes\s+([\s\S]+)$/i.exec(input.trim());
  if (change) {
    try {
      return { name: "vault_propose_changes", arguments: JSON.parse(change[1]) as unknown };
    } catch {
      return { name: "vault_propose_changes", arguments: change[1] };
    }
  }
  const skill = /^skill_read\s+([^\s]+)(?:\s+(.+))?$/i.exec(input.trim());
  if (skill) {
    return {
      name: "skill_read",
      arguments: {
        skill: skill[1],
        ...(skill[2] ? { resource: skill[2].trim() } : {}),
      },
    };
  }
  const read = /^vault_read\s+([^\s]+)(?:\s+(\d+)-(\d+))?$/i.exec(input.trim());
  if (read) {
    return {
      name: "vault_read",
      arguments: {
        path: read[1],
        ...(read[2] ? { lineStart: Number(read[2]), lineEnd: Number(read[3]) } : {}),
      },
    };
  }
  if (/^vault_list(?:\s|$)/i.test(input.trim())) {
    const directory = input.trim().slice("vault_list".length).trim();
    return {
      name: "vault_list",
      arguments: directory ? { directory } : {},
    };
  }
  const search = /^vault_search\s+(.+)$/i.exec(input.trim());
  if (search) {
    return {
      name: "vault_search",
      arguments: { query: search[1] },
    };
  }
  return undefined;
}
