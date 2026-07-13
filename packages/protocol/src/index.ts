export const PROTOCOL_VERSION = 1 as const;

export interface RuntimeHandshake {
  instanceId: string;
  pid: number;
  port: number;
  protocolVersion: typeof PROTOCOL_VERSION;
}

export interface RuntimeHealth {
  instanceId: string;
  protocolVersion: typeof PROTOCOL_VERSION;
  status: "healthy";
}

export interface RuntimeShutdown {
  status: "shutting_down";
}

export interface ModelDescriptor {
  id: string;
  label: string;
}

export type AgentRunStatus =
  | "cancelled"
  | "completed"
  | "failed"
  | "interrupted"
  | "running";

export interface ConversationSummary {
  id: string;
  modelId: string;
  title: string;
}

export interface ConversationMessage {
  agentRunId: string;
  id: string;
  role: "assistant" | "user";
  sequence: number;
  text: string;
}

export interface AgentRunRecord {
  id: string;
  modelId: string;
  status: AgentRunStatus;
}

interface ConversationCommandBase {
  agentRunId: string;
  conversationId: string;
  eventId: string;
  protocolVersion: typeof PROTOCOL_VERSION;
  sequence: number;
}

export type ConversationCommand =
  | (ConversationCommandBase & {
      type: "conversation.create";
      model: string;
      title: string;
    })
  | (ConversationCommandBase & { type: "conversation.delete" })
  | (ConversationCommandBase & { type: "conversation.list" })
  | (ConversationCommandBase & { type: "conversation.open" })
  | (ConversationCommandBase & { type: "conversation.update"; model: string });

export type ConversationEvent =
  | (ConversationCommandBase & {
      type: "conversation.created";
      conversation: ConversationSummary;
    })
  | (ConversationCommandBase & { type: "conversation.deleted" })
  | (ConversationCommandBase & {
      type: "conversation.error";
      error: { code: "storage_error"; message: string };
    })
  | (ConversationCommandBase & {
      type: "conversation.list";
      conversations: ConversationSummary[];
    })
  | (ConversationCommandBase & {
      type: "conversation.snapshot";
      conversation: ConversationSummary;
      messages: ConversationMessage[];
      agentRuns: AgentRunRecord[];
    })
  | (ConversationCommandBase & {
      type: "conversation.updated";
      conversation: ConversationSummary;
    });

export interface RuntimeModels {
  models: ModelDescriptor[];
}

export type ProviderErrorCode =
  | "auth_required"
  | "model_unavailable"
  | "provider_error"
  | "transport_error";

export interface AgentRunStart {
  agentRunId: string;
  conversationId: string;
  eventId: string;
  input: { role: "user"; text: string };
  model: string;
  protocolVersion: typeof PROTOCOL_VERSION;
  sequence: 0;
  type: "agent_run.start";
}

export interface AgentRunCancel {
  agentRunId: string;
  conversationId: string;
  eventId: string;
  protocolVersion: typeof PROTOCOL_VERSION;
  sequence: number;
  type: "agent_run.cancel";
}

export interface DurableEventAck {
  acknowledgedEventId: string;
  agentRunId: string;
  conversationId: string;
  eventId: string;
  protocolVersion: typeof PROTOCOL_VERSION;
  sequence: number;
  type: "event.ack";
}

interface AgentRunEventBase {
  agentRunId: string;
  conversationId: string;
  eventId: string;
  protocolVersion: typeof PROTOCOL_VERSION;
  sequence: number;
}

export type AgentRunEvent =
  | (AgentRunEventBase & { type: "agent_run.started"; model: string })
  | (AgentRunEventBase & { type: "agent_run.delta"; delta: string })
  | (AgentRunEventBase & { type: "agent_run.cancelled" })
  | (AgentRunEventBase & { type: "agent_run.interrupted" })
  | (AgentRunEventBase & {
      type: "agent_run.completed";
      output: { role: "assistant"; text: string };
    })
  | (AgentRunEventBase & {
      type: "agent_run.failed";
      error: { code: ProviderErrorCode; message: string };
    });

export interface RuntimeError {
  code: "not_found" | "unauthorized" | ProviderErrorCode;
  message: string;
}
