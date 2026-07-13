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
