import type {
    ErrorCode,
    ExecutableToolCallDescriptor,
    ToolResultDescriptor,
} from "./generated_protocol";

export function succeeded(
    call: Pick<ExecutableToolCallDescriptor, "toolCallId">,
    summary: string,
    data: Record<string, unknown>,
    sourceRefs: ToolResultDescriptor["sourceRefs"] = [],
): ToolResultDescriptor {
    return { toolCallId: call.toolCallId, status: "succeeded", summary, data, sourceRefs, retryable: false };
}

export function failed(
    call: Pick<ExecutableToolCallDescriptor, "toolCallId">,
    code: ErrorCode,
    message: string,
    retryable = false,
    status: "failed" | "denied" | "unknown_outcome" = "failed",
): ToolResultDescriptor {
    return {
        toolCallId: call.toolCallId,
        status,
        summary: message,
        data: {},
        retryable,
        error: { code, retryable, cancelled: false, userVisibleMessage: message, details: {} },
    };
}

export function hasExtraKeys(value: Readonly<Record<string, unknown>>, allowed: readonly string[]): boolean {
    return Object.keys(value).some((key) => !allowed.includes(key));
}
