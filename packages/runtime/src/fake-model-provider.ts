import type { LocalToolName, ModelDescriptor } from "@offeragent/protocol";
import {
  ModelProviderError,
  type ModelProvider,
  type ModelRequest,
  type ModelStreamEvent,
} from "./model-provider";

const MODEL: ModelDescriptor = {
  id: "fake-interview-model",
  label: "Fake Interview Model",
};

export class FakeModelProvider implements ModelProvider {
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
    const userInput = request.input.find((item) => item.type === "user_message")?.text ?? "";
    const toolResult = [...request.input].reverse().find((item) => item.type === "local_tool_result");
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

function fakeToolRequest(input: string): { name: LocalToolName; arguments: unknown } | undefined {
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
  return undefined;
}
