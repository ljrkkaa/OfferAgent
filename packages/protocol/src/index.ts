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
  supportsFastMode?: boolean;
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
  citations?: WebCitation[];
  id: string;
  role: "assistant" | "user";
  sequence: number;
  text: string;
}

export type HostedWebSearchCapability = "available" | "unavailable" | "unknown";

export interface WebCitation {
  endIndex: number;
  startIndex: number;
  title: string;
  url: string;
}

export interface WebSearchSource {
  title?: string;
  url: string;
}

export interface AgentRunRecord {
  error?: { code: ProviderErrorCode; message: string };
  id: string;
  modelId: string;
  status: AgentRunStatus;
}

export type LocalToolName =
  | "agent_contract_read"
  | "daily_note_context"
  | "hosted_web_search_probe"
  | "skill_read"
  | "vault_list"
  | "vault_propose_changes"
  | "vault_read"
  | "vault_search"
  | "web_read";

export type VaultToolErrorCode =
  | "invalid_path"
  | "invalid_change"
  | "malformed_control_file"
  | "not_found"
  | "permission_denied"
  | "plugin_disconnected"
  | "request_too_large"
  | "response_too_large"
  | "redirect_error"
  | "stale_evidence"
  | "tool_error"
  | "unreadable_content"
  | "unsafe_url"
  | "undo_conflict";

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

export interface WebReadResult {
  content: string;
  contentType: string;
  finalUrl: string;
  sourceTitle?: string;
  truncated: boolean;
  type: "web_read";
  url: string;
}

export interface HostedWebSearchProbeResult {
  status: HostedWebSearchCapability;
  type: "hosted_web_search_probe";
}

export interface AgentContractResult {
  content: string;
  contentHash: string;
  modifiedVersion: string;
  path: "agent.md";
  type: "agent_contract_read";
}

export interface DailyNoteContextResult {
  dateFormat: string;
  resolvedDate: string;
  targetExists: boolean;
  targetPath: string;
  targetVersion: string;
  templateContent: string | null;
  templatePath: string | null;
  templateVersion: string | null;
  type: "daily_note_context";
}

export interface SkillReadResult {
  content: string;
  contentHash: string;
  modifiedVersion: string;
  path: string;
  resource: string;
  skill: string;
  type: "skill_read";
}

interface VaultActionBase {
  actionId: string;
  expectedVersion: string;
  idempotencyKey: string;
  path: string;
}

export type VaultAction =
  | (VaultActionBase & {
      content: string;
      operation: "append" | "create";
    })
  | (VaultActionBase & {
      expectedContent: string;
      operation: "exact_replace";
      replacement: string;
    });

export interface VaultChangeBatchProposal {
  actions: VaultAction[];
  batchId: string;
  idempotencyKey: string;
  task: string;
}

export interface VaultChangeTargetResult {
  afterHash: string;
  beforeHash: string;
  path: string;
}

export type VaultChangeTransactionState =
  | "applied"
  | "applying"
  | "expired"
  | "failed"
  | "pending"
  | "recovery_failed"
  | "rejected"
  | "rolled_back"
  | "undone";

export interface VaultChangeJournalRecord {
  batchId: string;
  checkpointRef: string;
  state: VaultChangeTransactionState;
  targets: VaultChangeTargetResult[];
}

export interface VaultChangeApplyingRequest {
  batchId: string;
  checkpointRef: string;
  targets: VaultChangeTargetResult[];
}

export interface VaultChangeStateRequest {
  batchId: string;
  state: Extract<
    VaultChangeTransactionState,
    "applied" | "expired" | "recovery_failed" | "rolled_back" | "undone"
  >;
}

interface ProtocolCommandBase {
  agentRunId: string;
  conversationId: string;
  eventId: string;
  protocolVersion: typeof PROTOCOL_VERSION;
  sequence: number;
}

export type VaultChangeCommand =
  | (ProtocolCommandBase & {
      type: "vault_changes.list";
      states: VaultChangeTransactionState[];
    })
  | (ProtocolCommandBase & VaultChangeApplyingRequest & { type: "vault_changes.applying" })
  | (ProtocolCommandBase & VaultChangeStateRequest & { type: "vault_changes.state" });

export type VaultChangeEvent =
  | (ProtocolCommandBase & {
      type: "vault_changes.listed";
      batches: VaultChangeJournalRecord[];
    })
  | (ProtocolCommandBase & { type: "vault_changes.applying_stored"; batchId: string })
  | (ProtocolCommandBase & {
      type: "vault_changes.state_stored";
      batchId: string;
      state: VaultChangeStateRequest["state"];
    })
  | (ProtocolCommandBase & {
      type: "vault_changes.error";
      error: { code: "storage_error"; message: string };
    });

export interface VaultUndoConflict {
  appliedHash: string;
  currentHash: string;
  diff: string;
  path: string;
}

export interface VaultChangeResult {
  batchId: string;
  checkpointRef?: string;
  decision: "applied" | "rejected";
  targets: VaultChangeTargetResult[];
  type: "vault_propose_changes";
}

export type VaultUndoResultPayload =
  | {
      ok: true;
      value: { batchId: string; status: "undone"; type: "vault_change_undo" };
    }
  | {
      ok: false;
      error: {
        code: VaultToolErrorCode;
        conflicts?: VaultUndoConflict[];
        message: string;
      };
    };

export type LocalToolResultPayload =
  | {
      ok: true;
      value:
        | AgentContractResult
        | DailyNoteContextResult
        | HostedWebSearchProbeResult
        | SkillReadResult
        | VaultListResult
        | VaultChangeResult
        | VaultReadResult
        | VaultSearchResult
        | WebReadResult;
    }
  | { ok: false; error: { code: VaultToolErrorCode; message: string } };

export interface ToolCallRecord {
  agentRunId: string;
  arguments: unknown;
  decision?: "applied" | "rejected";
  error?: { code: VaultToolErrorCode; message: string };
  id: string;
  name: LocalToolName;
  status: "completed" | "failed" | "requested";
  vaultChangeState?: VaultChangeTransactionState;
}

interface ConversationCommandBase extends ProtocolCommandBase {
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

export interface RuntimeHostedWebSearchCapability {
  modelId: string;
  status: HostedWebSearchCapability;
}

export type ProviderErrorCode =
  | "auth_required"
  | "instruction_error"
  | "model_unavailable"
  | "provider_error"
  | "transport_error"
  | "unsupported_capability";

export interface AgentRunStart {
  agentRunId: string;
  conversationId: string;
  eventId: string;
  fastMode?: boolean;
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

export interface AgentRunResume {
  agentRunId: string;
  conversationId: string;
  eventId: string;
  protocolVersion: typeof PROTOCOL_VERSION;
  sequence: 0;
  type: "agent_run.resume";
  recoveredToolResult?: {
    eventId: string;
    result: LocalToolResultPayload;
    toolCallId: string;
  };
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
  | (AgentRunEventBase & { type: "agent_run.resumed"; model: string })
  | (AgentRunEventBase & { type: "agent_run.delta"; delta: string })
  | (AgentRunEventBase & {
      type: "hosted_web_search.completed";
      searchCallId: string;
      sources: WebSearchSource[];
    })
  | (AgentRunEventBase & { type: "agent_run.cancelled" })
  | (AgentRunEventBase & { type: "agent_run.interrupted" })
  | (AgentRunEventBase & {
      type: "tool_call.requested";
      toolCallId: string;
      tool: { arguments: unknown; kind: "local" | "runtime"; name: LocalToolName };
    })
  | (AgentRunEventBase & {
      type: "tool_call.completed";
      toolCallId: string;
      tool: { kind: "local" | "runtime"; name: LocalToolName };
      status: "completed" | "failed";
      error?: { code: VaultToolErrorCode; message: string };
    })
  | (AgentRunEventBase & {
      type: "agent_run.completed";
      output: { citations?: WebCitation[]; role: "assistant"; text: string };
    })
  | (AgentRunEventBase & {
      type: "agent_run.failed";
      error: { code: ProviderErrorCode; message: string };
    });

export interface RuntimeError {
  code: "not_found" | "unauthorized" | ProviderErrorCode;
  message: string;
}
