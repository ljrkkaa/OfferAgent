import type {
  LocalToolName,
  LocalToolResultPayload,
  ModelDescriptor,
  ProviderErrorCode,
  RunAttachmentMetadata,
  WebCitation,
  WebSearchSource,
} from "@offeragent/protocol";

export const MAX_LOCAL_TOOL_ARGUMENT_BYTES = 8_192;
export const MAX_PROVIDER_REASONING_BYTES = 256 * 1_024;

export interface ProviderReasoningItem {
  content: unknown[];
  encrypted_content: string;
  id: string;
  summary: unknown[];
  type: "reasoning";
}

export type ModelConversationItem =
  | { attachments?: RunAttachmentMetadata[]; type: "user_message"; text: string }
  | { type: "assistant_message"; text: string }
  | { type: "provider_reasoning"; item: ProviderReasoningItem }
  | {
      type: "local_tool_call";
      callId: string;
      name: LocalToolName;
      arguments: unknown;
      providerItemId?: string;
      providerStatus?: "completed" | "in_progress";
    }
  | { type: "local_tool_result"; callId: string; result: LocalToolResultPayload };

export type ModelStreamEvent =
  | { type: "output_text.delta"; delta: string }
  | { type: "provider_reasoning"; item: ProviderReasoningItem }
  | {
      type: "local_tool_call";
      callId: string;
      name: LocalToolName;
      arguments: unknown;
      providerItemId?: string;
      providerStatus?: "completed" | "in_progress";
    }
  | { type: "hosted_web_search_call"; callId: string; sources: WebSearchSource[] }
  | { type: "url_citation"; citation: WebCitation };

export interface LocalToolDefinition {
  kind: "local";
  description: string;
  name: LocalToolName;
  parameters: Record<string, unknown>;
}

export interface HostedToolDefinition {
  kind: "hosted";
  name: "web_search";
}

export type ModelToolDefinition = HostedToolDefinition | LocalToolDefinition;

export interface ModelRequest {
  fastMode?: boolean;
  imageInputs?: ModelImageInput[];
  input: ModelConversationItem[];
  instructions: string;
  model: string;
  signal: AbortSignal;
  tools: ModelToolDefinition[];
}

export interface ModelImageInput {
  attachmentId: string;
  dataUrl: string;
  mediaType: RunAttachmentMetadata["mediaType"];
  order: number;
}

export interface ModelProvider {
  readonly backendId: string;
  listModels(): Promise<ModelDescriptor[]>;
  stream(request: ModelRequest): AsyncIterable<ModelStreamEvent>;
}

export class ModelProviderError extends Error {
  readonly capability?: "hosted_web_search" | "vision";
  readonly code: ProviderErrorCode;

  constructor(
    code: ProviderErrorCode,
    message: string,
    options?: ErrorOptions & { capability?: "hosted_web_search" | "vision" },
  ) {
    super(message, options);
    this.name = "ModelProviderError";
    this.code = code;
    this.capability = options?.capability;
  }
}

export function asModelProviderError(error: unknown): ModelProviderError {
  if (error instanceof ModelProviderError) return error;
  return new ModelProviderError(
    "provider_error",
    "The model provider could not complete this Agent Run.",
    { cause: error },
  );
}
