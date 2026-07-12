import { ChatRequest, OfferAgentServer } from "./api";

export const STREAM_EVENT_DELIMITER = "␃🔚␗";

export type StreamEventType =
    | "start_llm_response"
    | "end_llm_response"
    | "end_response"
    | "status"
    | "thought"
    | "references"
    | "vault_actions"
    | "metadata"
    | "usage"
    | "message";

export interface StreamEvent {
    type: StreamEventType;
    data: unknown;
}

const STRUCTURED_EVENT_TYPES = new Set<StreamEventType>([
    "start_llm_response",
    "end_llm_response",
    "end_response",
    "status",
    "thought",
    "references",
    "vault_actions",
    "metadata",
    "usage",
    "message",
]);

export function parseStreamFrame(frame: string): StreamEvent | null {
    if (!frame) return null;
    let value: unknown;
    try {
        value = JSON.parse(frame);
    } catch {
        throw new Error("Invalid OfferAgent stream event");
    }
    if (!isRecord(value)
        || Object.keys(value).length !== 2
        || !("type" in value)
        || !("data" in value)
        || typeof value.type !== "string"
        || !STRUCTURED_EVENT_TYPES.has(value.type as StreamEventType)) {
        throw new Error("Invalid OfferAgent stream event");
    }
    validateEventData(value.type as StreamEventType, value.data);
    return { type: value.type as StreamEventType, data: value.data };
}

export async function readStreamEvents(
    response: Response,
    onEvent: (event: StreamEvent) => void | Promise<void>,
): Promise<void> {
    if (!response.body) throw new Error("OfferAgent chat response has no body");
    const reader = response.body.getReader();
    const decoder = new TextDecoder();
    let buffer = "";

    while (true) {
        const { value, done } = await reader.read();
        buffer += decoder.decode(value, { stream: !done });
        let delimiterIndex: number;
        while ((delimiterIndex = buffer.indexOf(STREAM_EVENT_DELIMITER)) !== -1) {
            const event = parseStreamFrame(buffer.slice(0, delimiterIndex));
            buffer = buffer.slice(delimiterIndex + STREAM_EVENT_DELIMITER.length);
            if (event) await onEvent(event);
        }
        if (done) break;
    }
    const trailingEvent = parseStreamFrame(buffer);
    if (trailingEvent) await onEvent(trailingEvent);
}

export class ChatRuntime {
    private abortController: AbortController | null = null;
    private conversationId: string | null = null;
    private selectionVersion = 0;

    constructor(private readonly server: OfferAgentServer) {}

    get currentConversationId(): string | null {
        return this.conversationId;
    }

    selectConversation(conversationId: string | null): void {
        if (conversationId !== this.conversationId) {
            this.selectionVersion += 1;
            this.cancel();
        }
        this.conversationId = conversationId;
    }

    async createConversation(): Promise<string> {
        this.cancel();
        const selectionVersion = ++this.selectionVersion;
        const conversationId = await this.server.createConversation();
        if (selectionVersion !== this.selectionVersion) {
            throw new Error("Conversation selection changed while a new conversation was being created");
        }
        this.conversationId = conversationId;
        return this.conversationId;
    }

    async loadHistory(conversationId?: string | null) {
        const requestedConversationId = conversationId ?? this.conversationId;
        this.selectConversation(requestedConversationId);
        const selectionVersion = this.selectionVersion;
        const history = await this.server.getChatHistory(requestedConversationId);
        if (this.conversationId !== requestedConversationId || this.selectionVersion !== selectionVersion) {
            throw new Error("Conversation changed while history was loading");
        }
        this.conversationId = history.conversation_id;
        return history;
    }

    async send(
        request: Omit<ChatRequest, "conversation_id">,
        onEvent: (event: StreamEvent) => void | Promise<void>,
    ): Promise<void> {
        if (!this.conversationId) throw new Error("A conversation must be selected before sending a message");
        this.cancel();
        const controller = new AbortController();
        this.abortController = controller;
        const conversationId = this.conversationId;
        const selectionVersion = this.selectionVersion;
        try {
            const response = await this.server.streamChat(
                { ...request, conversation_id: conversationId },
                controller.signal,
            );
            await readStreamEvents(response, async event => {
                if (
                    this.abortController === controller
                    && this.conversationId === conversationId
                    && this.selectionVersion === selectionVersion
                ) {
                    await onEvent(event);
                }
            });
        } finally {
            if (this.abortController === controller) this.abortController = null;
        }
    }

    cancel(): void {
        this.abortController?.abort();
        this.abortController = null;
    }
}

function isRecord(value: unknown): value is Record<string, unknown> {
    return typeof value === "object" && value !== null;
}

function validateEventData(type: StreamEventType, data: unknown): void {
    if (["start_llm_response", "end_llm_response", "end_response", "status", "thought"].includes(type)) {
        if (typeof data !== "string") throw new Error(`Invalid ${type} event`);
        return;
    }
    if (type === "message") {
        if (typeof data !== "string") throw new Error("Invalid message event");
        return;
    }
    if (!isRecord(data)) throw new Error(`Invalid ${type} event`);
}
