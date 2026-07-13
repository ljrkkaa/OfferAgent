import type { ModelDescriptor, ProviderErrorCode } from "@offeragent/protocol";

export interface ModelRequest {
  input: string;
  model: string;
  signal: AbortSignal;
}

export interface ModelProvider {
  listModels(): Promise<ModelDescriptor[]>;
  stream(request: ModelRequest): AsyncIterable<string>;
}

export class ModelProviderError extends Error {
  readonly code: ProviderErrorCode;

  constructor(code: ProviderErrorCode, message: string, options?: ErrorOptions) {
    super(message, options);
    this.name = "ModelProviderError";
    this.code = code;
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
