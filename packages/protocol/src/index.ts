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

export type LocalToolName = "vault_list" | "vault_read" | "vault_search";

export type VaultToolErrorCode =
  | "invalid_path"
  | "not_found"
  | "plugin_disconnected"
  | "request_too_large"
  | "stale_evidence"
  | "tool_error";

export interface VaultListResult {
  entries: Array<{
    contentHash: string;
    modifiedVersion: string;
    path: string;
  }>;
  truncated: boolean;
  type: "vault_list";
}

export interface VaultReadResult {
  content: string;
  contentHash: string;
  lineEnd: number;
  lineStart: number;
  modifiedVersion: string;
  path: string;
  truncated: boolean;
  type: "vault_read";
}

export interface VaultSearchResult {
  entries: Array<{
    contentHash: string;
    matchTier: "body" | "metadata" | "path";
    modifiedVersion: string;
    path: string;
    snippets: Array<{
      content: string;
      lineEnd: number;
      lineStart: number;
      truncated: boolean;
    }>;
  }>;
  truncated: boolean;
  type: "vault_search";
}

export type LocalToolResultPayload =
  | { ok: true; value: VaultListResult | VaultReadResult | VaultSearchResult }
  | { ok: false; error: { code: VaultToolErrorCode; message: string } };

export interface ToolCallRecord {
  agentRunId: string;
  arguments: unknown;
  id: string;
  name: LocalToolName;
  status: "completed" | "failed" | "requested";
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
      toolCalls: ToolCallRecord[];
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

export interface ToolResultCommand {
  agentRunId: string;
  conversationId: string;
  eventId: string;
  protocolVersion: typeof PROTOCOL_VERSION;
  result: LocalToolResultPayload;
  sequence: number;
  toolCallId: string;
  type: "tool_result";
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
      type: "tool_call.requested";
      toolCallId: string;
      tool: { arguments: unknown; kind: "local"; name: LocalToolName };
    })
  | (AgentRunEventBase & {
      type: "tool_call.completed";
      toolCallId: string;
      tool: { kind: "local"; name: LocalToolName };
      status: "completed" | "failed";
      error?: { code: VaultToolErrorCode; message: string };
    })
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
