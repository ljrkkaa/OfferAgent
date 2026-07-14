import { createHash, randomUUID } from "node:crypto";
import { mkdir, readFile, rename, writeFile } from "node:fs/promises";
import path from "node:path";
import {
  PROTOCOL_VERSION,
  type AgentRunEvent,
  type AgentRunRecord,
  type AgentRunStatus,
  type ConversationMessage,
  type ConversationSummary,
  type HostedWebSearchCapability,
  type LocalToolResultPayload,
  type ProviderErrorCode,
  type ToolCallRecord,
  type VaultChangeJournalRecord,
  type VaultChangeTargetResult,
  type VaultChangeTransactionState,
  type VaultToolErrorCode,
  type WebCitation,
} from "@offeragent/protocol";
import initSqlJs, { type Database, type SqlJsStatic } from "sql.js/dist/sql-asm.js";
import type { ModelConversationItem } from "./model-provider";

const CURRENT_SCHEMA_VERSION = 13;
const MAX_CONVERSATION_CONTEXT_BYTES = 64 * 1_024;

export interface RunCheckpoint {
  canonicalReadPaths: Array<[string, string]>;
  completedSteps: number;
  fastMode?: boolean;
  hostedWebSearchProbeAttempted: boolean;
  input: ModelConversationItem[];
  localSkills: string[];
  changedMemoryPaths?: string[];
  dailyPlanApplied?: boolean;
  memoryHandledByMain?: boolean;
  resolvedDailyNotePaths?: string[];
  postResponseCitations?: WebCitation[];
  postResponseOutput?: string;
  pendingToolStep?: {
    completedSteps: number;
    name: ToolCallRecord["name"];
    providerCallId: string;
    toolCallId: string;
  };
  requiredRereads: string[];
  version: 1;
}

export interface ResumableRun {
  checkpoint: RunCheckpoint;
  model: string;
  nextSequence: number;
}

function persistedToolArguments(name: ToolCallRecord["name"], arguments_: unknown): unknown {
  if (name !== "vault_propose_changes" || !arguments_ || typeof arguments_ !== "object") {
    return arguments_;
  }
  const proposal = arguments_ as {
    actions?: unknown[];
    batchId?: unknown;
    idempotencyKey?: unknown;
    task?: unknown;
  };
  return {
    batchId: proposal.batchId,
    idempotencyKey: proposal.idempotencyKey,
    task: proposal.task,
    actions: Array.isArray(proposal.actions)
      ? proposal.actions.map((candidate) => {
          if (!candidate || typeof candidate !== "object") return {};
          const action = candidate as Record<string, unknown>;
          return {
            actionId: action.actionId,
            idempotencyKey: action.idempotencyKey,
            operation: action.operation,
            path: action.path,
            expectedVersion: action.expectedVersion,
          };
        })
      : [],
  };
}

function persistedRunCheckpoint(checkpoint: RunCheckpoint): RunCheckpoint {
  const skillCallIds = new Set(
    checkpoint.input
      .filter(
        (item): item is Extract<ModelConversationItem, { type: "local_tool_call" }> =>
          item.type === "local_tool_call" && item.name === "skill_read",
      )
      .map((item) => item.callId),
  );
  const input = checkpoint.input.flatMap((item) => {
    if (
      (item.type === "local_tool_call" || item.type === "local_tool_result") &&
      skillCallIds.has(item.callId)
    ) {
      return [];
    }
    return item.type === "local_tool_call" && item.name === "vault_propose_changes"
      ? [{ ...item, arguments: persistedToolArguments(item.name, item.arguments) }]
      : [item];
  });
  return {
    version: 1,
    input,
    localSkills: checkpoint.localSkills.filter(
      (skill): skill is string => typeof skill === "string" && /^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$/.test(skill),
    ),
    canonicalReadPaths: checkpoint.canonicalReadPaths,
    requiredRereads: checkpoint.requiredRereads,
    hostedWebSearchProbeAttempted: checkpoint.hostedWebSearchProbeAttempted,
    completedSteps: checkpoint.completedSteps,
    ...(checkpoint.dailyPlanApplied ? { dailyPlanApplied: true } : {}),
    ...(checkpoint.memoryHandledByMain ? { memoryHandledByMain: true } : {}),
    ...(checkpoint.resolvedDailyNotePaths?.length
      ? { resolvedDailyNotePaths: [...new Set(checkpoint.resolvedDailyNotePaths)] }
      : {}),
    ...(checkpoint.changedMemoryPaths?.length
      ? { changedMemoryPaths: [...new Set(checkpoint.changedMemoryPaths)] }
      : {}),
    ...(checkpoint.postResponseOutput !== undefined
      ? { postResponseOutput: checkpoint.postResponseOutput }
      : {}),
    ...(checkpoint.postResponseCitations?.length
      ? { postResponseCitations: checkpoint.postResponseCitations }
      : {}),
    ...(checkpoint.fastMode ? { fastMode: true } : {}),
    ...(checkpoint.pendingToolStep ? { pendingToolStep: checkpoint.pendingToolStep } : {}),
  };
}

function persistedToolResult(result: LocalToolResultPayload): unknown {
  if (
    result.ok &&
    (result.value.type === "agent_contract_read" || result.value.type === "skill_read")
  ) {
    const { content: _content, ...metadata } = result.value;
    return { ok: true, value: metadata };
  }
  if (result.ok && result.value.type === "planning_memory_read") {
    return {
      ok: true,
      value: {
        type: "planning_memory_read",
        topics: result.value.topics.map(({ content: _content, ...metadata }) => metadata),
      },
    };
  }
  return result;
}

function canonicalJson(value: unknown): string {
  if (Array.isArray(value)) return `[${value.map((item) => canonicalJson(item)).join(",")}]`;
  if (value && typeof value === "object") {
    const entries = Object.entries(value as Record<string, unknown>)
      .filter(([, item]) => item !== undefined)
      .sort(([left], [right]) => left < right ? -1 : left > right ? 1 : 0);
    return `{${entries.map(([key, item]) => `${JSON.stringify(key)}:${canonicalJson(item)}`).join(",")}}`;
  }
  return JSON.stringify(value) ?? "null";
}

function protocolRequestHash(value: unknown): string {
  return createHash("sha256").update(canonicalJson(value)).digest("hex");
}

const MIGRATIONS = [
  {
    version: 1,
    sql: `
      CREATE TABLE conversations (
        id TEXT PRIMARY KEY,
        title TEXT NOT NULL,
        model_id TEXT NOT NULL,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL
      );
      CREATE TABLE agent_runs (
        id TEXT PRIMARY KEY,
        conversation_id TEXT NOT NULL REFERENCES conversations(id) ON DELETE CASCADE,
        model_id TEXT NOT NULL,
        status TEXT NOT NULL CHECK (status IN ('running', 'completed', 'failed', 'cancelled', 'interrupted')),
        user_message_id TEXT,
        assistant_message_id TEXT,
        error_code TEXT,
        error_message TEXT,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL
      );
      CREATE TABLE messages (
        id TEXT PRIMARY KEY,
        conversation_id TEXT NOT NULL REFERENCES conversations(id) ON DELETE CASCADE,
        agent_run_id TEXT NOT NULL REFERENCES agent_runs(id) ON DELETE CASCADE,
        role TEXT NOT NULL CHECK (role IN ('user', 'assistant')),
        text TEXT NOT NULL,
        sequence INTEGER NOT NULL,
        created_at TEXT NOT NULL,
        UNIQUE (conversation_id, sequence)
      );
      CREATE TABLE durable_events (
        id TEXT PRIMARY KEY,
        conversation_id TEXT NOT NULL REFERENCES conversations(id) ON DELETE CASCADE,
        agent_run_id TEXT REFERENCES agent_runs(id) ON DELETE CASCADE,
        event_type TEXT NOT NULL,
        sequence INTEGER NOT NULL,
        payload_json TEXT NOT NULL,
        created_at TEXT NOT NULL,
        acknowledged_at TEXT,
        UNIQUE (agent_run_id, sequence)
      );
      CREATE TABLE run_checkpoints (
        id TEXT PRIMARY KEY,
        conversation_id TEXT NOT NULL REFERENCES conversations(id) ON DELETE CASCADE,
        agent_run_id TEXT NOT NULL REFERENCES agent_runs(id) ON DELETE CASCADE,
        checkpoint_json TEXT NOT NULL,
        created_at TEXT NOT NULL
      );
      CREATE TABLE settings_metadata (
        key TEXT PRIMARY KEY,
        value TEXT NOT NULL,
        updated_at TEXT NOT NULL
      );
      CREATE TABLE schema_migrations (
        version INTEGER PRIMARY KEY,
        applied_at TEXT NOT NULL
      );
      CREATE INDEX messages_by_conversation ON messages(conversation_id, sequence);
      CREATE INDEX runs_by_conversation ON agent_runs(conversation_id, created_at);
      CREATE INDEX events_by_run ON durable_events(agent_run_id, sequence);
    `,
  },
  {
    version: 2,
    sql: `
      ALTER TABLE agent_runs
      ADD COLUMN last_sequence INTEGER NOT NULL DEFAULT 0;
      UPDATE agent_runs
      SET last_sequence = COALESCE(
        (SELECT MAX(sequence) FROM durable_events WHERE agent_run_id = agent_runs.id),
        0
      );
      DELETE FROM durable_events WHERE event_type = 'agent_run.delta';
    `,
  },
  {
    version: 3,
    sql: `
      CREATE TABLE tool_calls (
        id TEXT PRIMARY KEY,
        conversation_id TEXT NOT NULL REFERENCES conversations(id) ON DELETE CASCADE,
        agent_run_id TEXT NOT NULL REFERENCES agent_runs(id) ON DELETE CASCADE,
        name TEXT NOT NULL CHECK (name IN ('vault_list', 'vault_read')),
        arguments_json TEXT NOT NULL,
        status TEXT NOT NULL CHECK (status IN ('requested', 'completed', 'failed')),
        error_code TEXT,
        error_message TEXT,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL
      );
      CREATE TABLE evidence_snapshots (
        id TEXT PRIMARY KEY,
        conversation_id TEXT NOT NULL REFERENCES conversations(id) ON DELETE CASCADE,
        agent_run_id TEXT NOT NULL REFERENCES agent_runs(id) ON DELETE CASCADE,
        tool_call_id TEXT NOT NULL UNIQUE REFERENCES tool_calls(id) ON DELETE CASCADE,
        path TEXT NOT NULL,
        line_start INTEGER NOT NULL,
        line_end INTEGER NOT NULL,
        modified_version TEXT NOT NULL,
        content_hash TEXT NOT NULL,
        content TEXT NOT NULL,
        created_at TEXT NOT NULL
      );
      CREATE INDEX tool_calls_by_run ON tool_calls(agent_run_id, created_at);
      CREATE INDEX evidence_by_run ON evidence_snapshots(agent_run_id, created_at);
    `,
  },
  {
    version: 4,
    sql: `
      CREATE TABLE tool_calls_v4 (
        id TEXT PRIMARY KEY,
        conversation_id TEXT NOT NULL REFERENCES conversations(id) ON DELETE CASCADE,
        agent_run_id TEXT NOT NULL REFERENCES agent_runs(id) ON DELETE CASCADE,
        name TEXT NOT NULL CHECK (name IN ('vault_list', 'vault_read', 'vault_search')),
        arguments_json TEXT NOT NULL,
        status TEXT NOT NULL CHECK (status IN ('requested', 'completed', 'failed')),
        error_code TEXT,
        error_message TEXT,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL
      );
      INSERT INTO tool_calls_v4
        (id, conversation_id, agent_run_id, name, arguments_json, status,
         error_code, error_message, created_at, updated_at)
      SELECT id, conversation_id, agent_run_id, name, arguments_json, status,
             error_code, error_message, created_at, updated_at
      FROM tool_calls;
      CREATE TABLE evidence_snapshots_v4 (
        id TEXT PRIMARY KEY,
        conversation_id TEXT NOT NULL REFERENCES conversations(id) ON DELETE CASCADE,
        agent_run_id TEXT NOT NULL REFERENCES agent_runs(id) ON DELETE CASCADE,
        tool_call_id TEXT NOT NULL UNIQUE REFERENCES tool_calls_v4(id) ON DELETE CASCADE,
        path TEXT NOT NULL,
        line_start INTEGER NOT NULL,
        line_end INTEGER NOT NULL,
        modified_version TEXT NOT NULL,
        content_hash TEXT NOT NULL,
        content TEXT NOT NULL,
        is_stale INTEGER NOT NULL DEFAULT 0 CHECK (is_stale IN (0, 1)),
        stale_detected_at TEXT,
        created_at TEXT NOT NULL
      );
      INSERT INTO evidence_snapshots_v4
        (id, conversation_id, agent_run_id, tool_call_id, path, line_start, line_end,
         modified_version, content_hash, content, is_stale, stale_detected_at, created_at)
      SELECT id, conversation_id, agent_run_id, tool_call_id, path, line_start, line_end,
             modified_version, content_hash, content, 0, NULL, created_at
      FROM evidence_snapshots;
      DROP TABLE evidence_snapshots;
      DROP TABLE tool_calls;
      ALTER TABLE tool_calls_v4 RENAME TO tool_calls;
      ALTER TABLE evidence_snapshots_v4 RENAME TO evidence_snapshots;
      CREATE INDEX tool_calls_by_run ON tool_calls(agent_run_id, created_at);
      CREATE INDEX evidence_by_run ON evidence_snapshots(agent_run_id, created_at);
      CREATE INDEX evidence_by_source ON evidence_snapshots(path, content_hash, is_stale);
    `,
  },
  {
    version: 5,
    sql: `
      CREATE TABLE tool_calls_v5 (
        id TEXT PRIMARY KEY,
        conversation_id TEXT NOT NULL REFERENCES conversations(id) ON DELETE CASCADE,
        agent_run_id TEXT NOT NULL REFERENCES agent_runs(id) ON DELETE CASCADE,
        name TEXT NOT NULL CHECK (name IN (
          'agent_contract_read', 'skill_read', 'vault_list', 'vault_read', 'vault_search'
        )),
        arguments_json TEXT NOT NULL,
        status TEXT NOT NULL CHECK (status IN ('requested', 'completed', 'failed')),
        error_code TEXT,
        error_message TEXT,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL
      );
      INSERT INTO tool_calls_v5
        (id, conversation_id, agent_run_id, name, arguments_json, status,
         error_code, error_message, created_at, updated_at)
      SELECT id, conversation_id, agent_run_id, name, arguments_json, status,
             error_code, error_message, created_at, updated_at
      FROM tool_calls;
      CREATE TABLE evidence_snapshots_v5 (
        id TEXT PRIMARY KEY,
        conversation_id TEXT NOT NULL REFERENCES conversations(id) ON DELETE CASCADE,
        agent_run_id TEXT NOT NULL REFERENCES agent_runs(id) ON DELETE CASCADE,
        tool_call_id TEXT NOT NULL UNIQUE REFERENCES tool_calls_v5(id) ON DELETE CASCADE,
        path TEXT NOT NULL,
        line_start INTEGER NOT NULL,
        line_end INTEGER NOT NULL,
        modified_version TEXT NOT NULL,
        content_hash TEXT NOT NULL,
        content TEXT NOT NULL,
        is_stale INTEGER NOT NULL DEFAULT 0 CHECK (is_stale IN (0, 1)),
        stale_detected_at TEXT,
        created_at TEXT NOT NULL
      );
      INSERT INTO evidence_snapshots_v5
        (id, conversation_id, agent_run_id, tool_call_id, path, line_start, line_end,
         modified_version, content_hash, content, is_stale, stale_detected_at, created_at)
      SELECT id, conversation_id, agent_run_id, tool_call_id, path, line_start, line_end,
             modified_version, content_hash, content, is_stale, stale_detected_at, created_at
      FROM evidence_snapshots;
      DROP TABLE evidence_snapshots;
      DROP TABLE tool_calls;
      ALTER TABLE tool_calls_v5 RENAME TO tool_calls;
      ALTER TABLE evidence_snapshots_v5 RENAME TO evidence_snapshots;
      CREATE INDEX tool_calls_by_run ON tool_calls(agent_run_id, created_at);
      CREATE INDEX evidence_by_run ON evidence_snapshots(agent_run_id, created_at);
      CREATE INDEX evidence_by_source ON evidence_snapshots(path, content_hash, is_stale);
    `,
  },
  {
    version: 6,
    sql: `
      CREATE TABLE tool_calls_v6 (
        id TEXT PRIMARY KEY,
        conversation_id TEXT NOT NULL REFERENCES conversations(id) ON DELETE CASCADE,
        agent_run_id TEXT NOT NULL REFERENCES agent_runs(id) ON DELETE CASCADE,
        name TEXT NOT NULL CHECK (name IN (
          'agent_contract_read', 'skill_read', 'vault_list', 'vault_propose_changes',
          'vault_read', 'vault_search'
        )),
        arguments_json TEXT NOT NULL,
        status TEXT NOT NULL CHECK (status IN ('requested', 'completed', 'failed')),
        error_code TEXT,
        error_message TEXT,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL
      );
      INSERT INTO tool_calls_v6
        (id, conversation_id, agent_run_id, name, arguments_json, status,
         error_code, error_message, created_at, updated_at)
      SELECT id, conversation_id, agent_run_id, name, arguments_json, status,
             error_code, error_message, created_at, updated_at
      FROM tool_calls;
      CREATE TABLE evidence_snapshots_v6 (
        id TEXT PRIMARY KEY,
        conversation_id TEXT NOT NULL REFERENCES conversations(id) ON DELETE CASCADE,
        agent_run_id TEXT NOT NULL REFERENCES agent_runs(id) ON DELETE CASCADE,
        tool_call_id TEXT NOT NULL UNIQUE REFERENCES tool_calls_v6(id) ON DELETE CASCADE,
        path TEXT NOT NULL,
        line_start INTEGER NOT NULL,
        line_end INTEGER NOT NULL,
        modified_version TEXT NOT NULL,
        content_hash TEXT NOT NULL,
        content TEXT NOT NULL,
        is_stale INTEGER NOT NULL DEFAULT 0 CHECK (is_stale IN (0, 1)),
        stale_detected_at TEXT,
        created_at TEXT NOT NULL
      );
      INSERT INTO evidence_snapshots_v6
        (id, conversation_id, agent_run_id, tool_call_id, path, line_start, line_end,
         modified_version, content_hash, content, is_stale, stale_detected_at, created_at)
      SELECT id, conversation_id, agent_run_id, tool_call_id, path, line_start, line_end,
             modified_version, content_hash, content, is_stale, stale_detected_at, created_at
      FROM evidence_snapshots;
      DROP TABLE evidence_snapshots;
      DROP TABLE tool_calls;
      ALTER TABLE tool_calls_v6 RENAME TO tool_calls;
      ALTER TABLE evidence_snapshots_v6 RENAME TO evidence_snapshots;
      CREATE INDEX tool_calls_by_run ON tool_calls(agent_run_id, created_at);
      CREATE INDEX evidence_by_run ON evidence_snapshots(agent_run_id, created_at);
      CREATE INDEX evidence_by_source ON evidence_snapshots(path, content_hash, is_stale);
      CREATE TABLE vault_change_batches (
        id TEXT PRIMARY KEY,
        conversation_id TEXT NOT NULL REFERENCES conversations(id) ON DELETE CASCADE,
        agent_run_id TEXT NOT NULL REFERENCES agent_runs(id) ON DELETE CASCADE,
        tool_call_id TEXT NOT NULL UNIQUE REFERENCES tool_calls(id) ON DELETE CASCADE,
        idempotency_key TEXT NOT NULL UNIQUE,
        task TEXT NOT NULL,
        proposal_json TEXT NOT NULL,
        target_paths_json TEXT NOT NULL,
        state TEXT NOT NULL CHECK (state IN ('pending', 'applied', 'rejected', 'failed')),
        checkpoint_ref TEXT,
        after_hashes_json TEXT,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL
      );
      CREATE INDEX vault_change_batches_by_run
        ON vault_change_batches(agent_run_id, created_at);
    `,
  },
  {
    version: 7,
    sql: `
      CREATE TABLE vault_change_batches_v7 (
        id TEXT PRIMARY KEY,
        conversation_id TEXT NOT NULL REFERENCES conversations(id) ON DELETE CASCADE,
        agent_run_id TEXT NOT NULL REFERENCES agent_runs(id) ON DELETE CASCADE,
        tool_call_id TEXT NOT NULL UNIQUE REFERENCES tool_calls(id) ON DELETE CASCADE,
        idempotency_key TEXT NOT NULL UNIQUE,
        task TEXT NOT NULL,
        target_paths_json TEXT NOT NULL,
        state TEXT NOT NULL CHECK (state IN (
          'pending', 'applying', 'applied', 'rejected', 'failed', 'rolled_back',
          'recovery_failed', 'undone', 'expired'
        )),
        checkpoint_ref TEXT,
        before_hashes_json TEXT,
        after_hashes_json TEXT,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL
      );
      INSERT INTO vault_change_batches_v7
        (id, conversation_id, agent_run_id, tool_call_id, idempotency_key, task,
         target_paths_json, state, checkpoint_ref, before_hashes_json,
         after_hashes_json, created_at, updated_at)
      SELECT id, conversation_id, agent_run_id, tool_call_id, idempotency_key, task,
             target_paths_json, state, checkpoint_ref, NULL,
             after_hashes_json, created_at, updated_at
      FROM vault_change_batches;
      DROP TABLE vault_change_batches;
      ALTER TABLE vault_change_batches_v7 RENAME TO vault_change_batches;
      CREATE INDEX vault_change_batches_by_run
        ON vault_change_batches(agent_run_id, created_at);
      CREATE INDEX vault_change_batches_by_state
        ON vault_change_batches(state, updated_at);
    `,
  },
  {
    version: 8,
    sql: `
      CREATE TABLE tool_calls_v8 (
        id TEXT PRIMARY KEY,
        conversation_id TEXT NOT NULL REFERENCES conversations(id) ON DELETE CASCADE,
        agent_run_id TEXT NOT NULL REFERENCES agent_runs(id) ON DELETE CASCADE,
        name TEXT NOT NULL CHECK (name IN (
          'agent_contract_read', 'hosted_web_search_probe', 'skill_read', 'vault_list', 'vault_propose_changes',
          'vault_read', 'vault_search', 'web_read'
        )),
        arguments_json TEXT NOT NULL,
        status TEXT NOT NULL CHECK (status IN ('requested', 'completed', 'failed')),
        error_code TEXT,
        error_message TEXT,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL
      );
      INSERT INTO tool_calls_v8
        (id, conversation_id, agent_run_id, name, arguments_json, status,
         error_code, error_message, created_at, updated_at)
      SELECT id, conversation_id, agent_run_id, name, arguments_json, status,
             error_code, error_message, created_at, updated_at
      FROM tool_calls;
      CREATE TABLE evidence_snapshots_v8 (
        id TEXT PRIMARY KEY,
        conversation_id TEXT NOT NULL REFERENCES conversations(id) ON DELETE CASCADE,
        agent_run_id TEXT NOT NULL REFERENCES agent_runs(id) ON DELETE CASCADE,
        tool_call_id TEXT NOT NULL UNIQUE REFERENCES tool_calls_v8(id) ON DELETE CASCADE,
        path TEXT NOT NULL,
        line_start INTEGER NOT NULL,
        line_end INTEGER NOT NULL,
        modified_version TEXT NOT NULL,
        content_hash TEXT NOT NULL,
        content TEXT NOT NULL,
        is_stale INTEGER NOT NULL DEFAULT 0 CHECK (is_stale IN (0, 1)),
        stale_detected_at TEXT,
        created_at TEXT NOT NULL
      );
      INSERT INTO evidence_snapshots_v8
        (id, conversation_id, agent_run_id, tool_call_id, path, line_start, line_end,
         modified_version, content_hash, content, is_stale, stale_detected_at, created_at)
      SELECT id, conversation_id, agent_run_id, tool_call_id, path, line_start, line_end,
             modified_version, content_hash, content, is_stale, stale_detected_at, created_at
      FROM evidence_snapshots;
      CREATE TABLE vault_change_batches_v8 (
        id TEXT PRIMARY KEY,
        conversation_id TEXT NOT NULL REFERENCES conversations(id) ON DELETE CASCADE,
        agent_run_id TEXT NOT NULL REFERENCES agent_runs(id) ON DELETE CASCADE,
        tool_call_id TEXT NOT NULL UNIQUE REFERENCES tool_calls_v8(id) ON DELETE CASCADE,
        idempotency_key TEXT NOT NULL UNIQUE,
        task TEXT NOT NULL,
        target_paths_json TEXT NOT NULL,
        state TEXT NOT NULL CHECK (state IN (
          'pending', 'applying', 'applied', 'rejected', 'failed', 'rolled_back',
          'recovery_failed', 'undone', 'expired'
        )),
        checkpoint_ref TEXT,
        before_hashes_json TEXT,
        after_hashes_json TEXT,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL
      );
      INSERT INTO vault_change_batches_v8
        (id, conversation_id, agent_run_id, tool_call_id, idempotency_key, task,
         target_paths_json, state, checkpoint_ref, before_hashes_json,
         after_hashes_json, created_at, updated_at)
      SELECT id, conversation_id, agent_run_id, tool_call_id, idempotency_key, task,
             target_paths_json, state, checkpoint_ref, before_hashes_json,
             after_hashes_json, created_at, updated_at
      FROM vault_change_batches;
      DROP TABLE evidence_snapshots;
      DROP TABLE vault_change_batches;
      DROP TABLE tool_calls;
      ALTER TABLE tool_calls_v8 RENAME TO tool_calls;
      ALTER TABLE evidence_snapshots_v8 RENAME TO evidence_snapshots;
      ALTER TABLE vault_change_batches_v8 RENAME TO vault_change_batches;
      CREATE INDEX tool_calls_by_run ON tool_calls(agent_run_id, created_at);
      CREATE INDEX evidence_by_run ON evidence_snapshots(agent_run_id, created_at);
      CREATE INDEX evidence_by_source ON evidence_snapshots(path, content_hash, is_stale);
      CREATE INDEX vault_change_batches_by_run ON vault_change_batches(agent_run_id, created_at);
      CREATE INDEX vault_change_batches_by_state ON vault_change_batches(state, updated_at);
      CREATE TABLE IF NOT EXISTS provider_capabilities (
        backend_id TEXT NOT NULL,
        model_id TEXT NOT NULL,
        capability TEXT NOT NULL CHECK (capability = 'hosted_web_search'),
        status TEXT NOT NULL CHECK (status IN ('unknown', 'available', 'unavailable')),
        updated_at TEXT NOT NULL,
        PRIMARY KEY (backend_id, model_id, capability)
      );
    `,
  },
  {
    version: 9,
    sql: `
      CREATE TABLE IF NOT EXISTS protocol_responses (
        request_event_id TEXT PRIMARY KEY,
        request_type TEXT NOT NULL,
        conversation_id TEXT NOT NULL,
        agent_run_id TEXT NOT NULL,
        owner_conversation_id TEXT,
        request_hash TEXT NOT NULL,
        response_json TEXT,
        created_at TEXT NOT NULL
      );
      CREATE INDEX IF NOT EXISTS protocol_responses_by_conversation
        ON protocol_responses(conversation_id);
      CREATE INDEX IF NOT EXISTS protocol_responses_by_owner
        ON protocol_responses(owner_conversation_id);
    `,
  },
  {
    version: 10,
    sql: `SELECT 1;`,
  },
  {
    version: 11,
    sql: `
      CREATE TABLE tool_calls_v11 (
        id TEXT PRIMARY KEY,
        conversation_id TEXT NOT NULL REFERENCES conversations(id) ON DELETE CASCADE,
        agent_run_id TEXT NOT NULL REFERENCES agent_runs(id) ON DELETE CASCADE,
        name TEXT NOT NULL CHECK (name IN (
          'agent_contract_read', 'daily_note_context', 'hosted_web_search_probe',
          'skill_read', 'vault_list', 'vault_propose_changes', 'vault_read',
          'vault_search', 'web_read'
        )),
        arguments_json TEXT NOT NULL,
        status TEXT NOT NULL CHECK (status IN ('requested', 'completed', 'failed')),
        error_code TEXT,
        error_message TEXT,
        result_json TEXT,
        result_event_id TEXT,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL
      );
      INSERT INTO tool_calls_v11
        (id, conversation_id, agent_run_id, name, arguments_json, status,
         error_code, error_message, result_json, result_event_id, created_at, updated_at)
      SELECT id, conversation_id, agent_run_id, name, arguments_json, status,
             error_code, error_message, result_json, result_event_id, created_at, updated_at
      FROM tool_calls;
      CREATE TABLE evidence_snapshots_v11 (
        id TEXT PRIMARY KEY,
        conversation_id TEXT NOT NULL REFERENCES conversations(id) ON DELETE CASCADE,
        agent_run_id TEXT NOT NULL REFERENCES agent_runs(id) ON DELETE CASCADE,
        tool_call_id TEXT NOT NULL UNIQUE REFERENCES tool_calls_v11(id) ON DELETE CASCADE,
        path TEXT NOT NULL,
        line_start INTEGER NOT NULL,
        line_end INTEGER NOT NULL,
        modified_version TEXT NOT NULL,
        content_hash TEXT NOT NULL,
        content TEXT NOT NULL,
        is_stale INTEGER NOT NULL DEFAULT 0 CHECK (is_stale IN (0, 1)),
        stale_detected_at TEXT,
        created_at TEXT NOT NULL
      );
      INSERT INTO evidence_snapshots_v11
        (id, conversation_id, agent_run_id, tool_call_id, path, line_start, line_end,
         modified_version, content_hash, content, is_stale, stale_detected_at, created_at)
      SELECT id, conversation_id, agent_run_id, tool_call_id, path, line_start, line_end,
             modified_version, content_hash, content, is_stale, stale_detected_at, created_at
      FROM evidence_snapshots;
      CREATE TABLE vault_change_batches_v11 (
        id TEXT PRIMARY KEY,
        conversation_id TEXT NOT NULL REFERENCES conversations(id) ON DELETE CASCADE,
        agent_run_id TEXT NOT NULL REFERENCES agent_runs(id) ON DELETE CASCADE,
        tool_call_id TEXT NOT NULL UNIQUE REFERENCES tool_calls_v11(id) ON DELETE CASCADE,
        idempotency_key TEXT NOT NULL UNIQUE,
        task TEXT NOT NULL,
        target_paths_json TEXT NOT NULL,
        state TEXT NOT NULL CHECK (state IN (
          'pending', 'applying', 'applied', 'rejected', 'failed', 'rolled_back',
          'recovery_failed', 'undone', 'expired'
        )),
        checkpoint_ref TEXT,
        before_hashes_json TEXT,
        after_hashes_json TEXT,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL
      );
      INSERT INTO vault_change_batches_v11
        (id, conversation_id, agent_run_id, tool_call_id, idempotency_key, task,
         target_paths_json, state, checkpoint_ref, before_hashes_json,
         after_hashes_json, created_at, updated_at)
      SELECT id, conversation_id, agent_run_id, tool_call_id, idempotency_key, task,
             target_paths_json, state, checkpoint_ref, before_hashes_json,
             after_hashes_json, created_at, updated_at
      FROM vault_change_batches;
      DROP TABLE evidence_snapshots;
      DROP TABLE vault_change_batches;
      DROP TABLE tool_calls;
      ALTER TABLE tool_calls_v11 RENAME TO tool_calls;
      ALTER TABLE evidence_snapshots_v11 RENAME TO evidence_snapshots;
      ALTER TABLE vault_change_batches_v11 RENAME TO vault_change_batches;
      CREATE INDEX tool_calls_by_run ON tool_calls(agent_run_id, created_at);
      CREATE INDEX evidence_by_run ON evidence_snapshots(agent_run_id, created_at);
      CREATE INDEX evidence_by_source ON evidence_snapshots(path, content_hash, is_stale);
      CREATE INDEX vault_change_batches_by_run ON vault_change_batches(agent_run_id, created_at);
      CREATE INDEX vault_change_batches_by_state ON vault_change_batches(state, updated_at);
    `,
  },
  {
    version: 12,
    sql: `
      CREATE TABLE tool_calls_v12 (
        id TEXT PRIMARY KEY,
        conversation_id TEXT NOT NULL REFERENCES conversations(id) ON DELETE CASCADE,
        agent_run_id TEXT NOT NULL REFERENCES agent_runs(id) ON DELETE CASCADE,
        name TEXT NOT NULL CHECK (name IN (
          'agent_contract_read', 'daily_note_context', 'hosted_web_search_probe',
          'planning_memory_list', 'planning_memory_read', 'skill_read', 'vault_list',
          'vault_propose_changes', 'vault_read', 'vault_search', 'web_read'
        )),
        arguments_json TEXT NOT NULL,
        status TEXT NOT NULL CHECK (status IN ('requested', 'completed', 'failed')),
        error_code TEXT,
        error_message TEXT,
        result_json TEXT,
        result_event_id TEXT,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL
      );
      INSERT INTO tool_calls_v12
        (id, conversation_id, agent_run_id, name, arguments_json, status,
         error_code, error_message, result_json, result_event_id, created_at, updated_at)
      SELECT id, conversation_id, agent_run_id, name, arguments_json, status,
             error_code, error_message, result_json, result_event_id, created_at, updated_at
      FROM tool_calls;
      CREATE TABLE evidence_snapshots_v12 (
        id TEXT PRIMARY KEY,
        conversation_id TEXT NOT NULL REFERENCES conversations(id) ON DELETE CASCADE,
        agent_run_id TEXT NOT NULL REFERENCES agent_runs(id) ON DELETE CASCADE,
        tool_call_id TEXT NOT NULL UNIQUE REFERENCES tool_calls_v12(id) ON DELETE CASCADE,
        path TEXT NOT NULL,
        line_start INTEGER NOT NULL,
        line_end INTEGER NOT NULL,
        modified_version TEXT NOT NULL,
        content_hash TEXT NOT NULL,
        content TEXT NOT NULL,
        is_stale INTEGER NOT NULL DEFAULT 0 CHECK (is_stale IN (0, 1)),
        stale_detected_at TEXT,
        created_at TEXT NOT NULL
      );
      INSERT INTO evidence_snapshots_v12
        (id, conversation_id, agent_run_id, tool_call_id, path, line_start, line_end,
         modified_version, content_hash, content, is_stale, stale_detected_at, created_at)
      SELECT id, conversation_id, agent_run_id, tool_call_id, path, line_start, line_end,
             modified_version, content_hash, content, is_stale, stale_detected_at, created_at
      FROM evidence_snapshots;
      CREATE TABLE vault_change_batches_v12 (
        id TEXT PRIMARY KEY,
        conversation_id TEXT NOT NULL REFERENCES conversations(id) ON DELETE CASCADE,
        agent_run_id TEXT NOT NULL REFERENCES agent_runs(id) ON DELETE CASCADE,
        tool_call_id TEXT NOT NULL UNIQUE REFERENCES tool_calls_v12(id) ON DELETE CASCADE,
        idempotency_key TEXT NOT NULL UNIQUE,
        task TEXT NOT NULL,
        target_paths_json TEXT NOT NULL,
        state TEXT NOT NULL CHECK (state IN (
          'pending', 'applying', 'applied', 'rejected', 'failed', 'rolled_back',
          'recovery_failed', 'undone', 'expired'
        )),
        checkpoint_ref TEXT,
        before_hashes_json TEXT,
        after_hashes_json TEXT,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL
      );
      INSERT INTO vault_change_batches_v12
        (id, conversation_id, agent_run_id, tool_call_id, idempotency_key, task,
         target_paths_json, state, checkpoint_ref, before_hashes_json,
         after_hashes_json, created_at, updated_at)
      SELECT id, conversation_id, agent_run_id, tool_call_id, idempotency_key, task,
             target_paths_json, state, checkpoint_ref, before_hashes_json,
             after_hashes_json, created_at, updated_at
      FROM vault_change_batches;
      DROP TABLE evidence_snapshots;
      DROP TABLE vault_change_batches;
      DROP TABLE tool_calls;
      ALTER TABLE tool_calls_v12 RENAME TO tool_calls;
      ALTER TABLE evidence_snapshots_v12 RENAME TO evidence_snapshots;
      ALTER TABLE vault_change_batches_v12 RENAME TO vault_change_batches;
      CREATE INDEX tool_calls_by_run ON tool_calls(agent_run_id, created_at);
      CREATE INDEX evidence_by_run ON evidence_snapshots(agent_run_id, created_at);
      CREATE INDEX evidence_by_source ON evidence_snapshots(path, content_hash, is_stale);
      CREATE INDEX vault_change_batches_by_run ON vault_change_batches(agent_run_id, created_at);
      CREATE INDEX vault_change_batches_by_state ON vault_change_batches(state, updated_at);
    `,
  },
  {
    version: 13,
    sql: `
      CREATE TABLE tool_calls_v13 (
        id TEXT PRIMARY KEY,
        conversation_id TEXT NOT NULL REFERENCES conversations(id) ON DELETE CASCADE,
        agent_run_id TEXT NOT NULL REFERENCES agent_runs(id) ON DELETE CASCADE,
        name TEXT NOT NULL CHECK (name IN (
          'agent_contract_read', 'daily_note_context', 'hosted_web_search_probe',
          'interview_catalog', 'planning_memory_list', 'planning_memory_read',
          'skill_read', 'vault_list', 'vault_propose_changes', 'vault_read',
          'vault_search', 'web_read'
        )),
        arguments_json TEXT NOT NULL,
        status TEXT NOT NULL CHECK (status IN ('requested', 'completed', 'failed')),
        error_code TEXT,
        error_message TEXT,
        result_json TEXT,
        result_event_id TEXT,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL
      );
      INSERT INTO tool_calls_v13
        (id, conversation_id, agent_run_id, name, arguments_json, status,
         error_code, error_message, result_json, result_event_id, created_at, updated_at)
      SELECT id, conversation_id, agent_run_id, name, arguments_json, status,
             error_code, error_message, result_json, result_event_id, created_at, updated_at
      FROM tool_calls;
      CREATE TABLE evidence_snapshots_v13 (
        id TEXT PRIMARY KEY,
        conversation_id TEXT NOT NULL REFERENCES conversations(id) ON DELETE CASCADE,
        agent_run_id TEXT NOT NULL REFERENCES agent_runs(id) ON DELETE CASCADE,
        tool_call_id TEXT NOT NULL UNIQUE REFERENCES tool_calls_v13(id) ON DELETE CASCADE,
        path TEXT NOT NULL,
        line_start INTEGER NOT NULL,
        line_end INTEGER NOT NULL,
        modified_version TEXT NOT NULL,
        content_hash TEXT NOT NULL,
        content TEXT NOT NULL,
        is_stale INTEGER NOT NULL DEFAULT 0 CHECK (is_stale IN (0, 1)),
        stale_detected_at TEXT,
        created_at TEXT NOT NULL
      );
      INSERT INTO evidence_snapshots_v13
        (id, conversation_id, agent_run_id, tool_call_id, path, line_start, line_end,
         modified_version, content_hash, content, is_stale, stale_detected_at, created_at)
      SELECT id, conversation_id, agent_run_id, tool_call_id, path, line_start, line_end,
             modified_version, content_hash, content, is_stale, stale_detected_at, created_at
      FROM evidence_snapshots;
      CREATE TABLE vault_change_batches_v13 (
        id TEXT PRIMARY KEY,
        conversation_id TEXT NOT NULL REFERENCES conversations(id) ON DELETE CASCADE,
        agent_run_id TEXT NOT NULL REFERENCES agent_runs(id) ON DELETE CASCADE,
        tool_call_id TEXT NOT NULL UNIQUE REFERENCES tool_calls_v13(id) ON DELETE CASCADE,
        idempotency_key TEXT NOT NULL UNIQUE,
        task TEXT NOT NULL,
        target_paths_json TEXT NOT NULL,
        state TEXT NOT NULL CHECK (state IN (
          'pending', 'applying', 'applied', 'rejected', 'failed', 'rolled_back',
          'recovery_failed', 'undone', 'expired'
        )),
        checkpoint_ref TEXT,
        before_hashes_json TEXT,
        after_hashes_json TEXT,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL
      );
      INSERT INTO vault_change_batches_v13
        (id, conversation_id, agent_run_id, tool_call_id, idempotency_key, task,
         target_paths_json, state, checkpoint_ref, before_hashes_json,
         after_hashes_json, created_at, updated_at)
      SELECT id, conversation_id, agent_run_id, tool_call_id, idempotency_key, task,
             target_paths_json, state, checkpoint_ref, before_hashes_json,
             after_hashes_json, created_at, updated_at
      FROM vault_change_batches;
      DROP TABLE evidence_snapshots;
      DROP TABLE vault_change_batches;
      DROP TABLE tool_calls;
      ALTER TABLE tool_calls_v13 RENAME TO tool_calls;
      ALTER TABLE evidence_snapshots_v13 RENAME TO evidence_snapshots;
      ALTER TABLE vault_change_batches_v13 RENAME TO vault_change_batches;
      CREATE INDEX tool_calls_by_run ON tool_calls(agent_run_id, created_at);
      CREATE INDEX evidence_by_run ON evidence_snapshots(agent_run_id, created_at);
      CREATE INDEX evidence_by_source ON evidence_snapshots(path, content_hash, is_stale);
      CREATE INDEX vault_change_batches_by_run ON vault_change_batches(agent_run_id, created_at);
      CREATE INDEX vault_change_batches_by_state ON vault_change_batches(state, updated_at);
    `,
  },
] as const;

interface ConversationSnapshot {
  agentRuns: AgentRunRecord[];
  conversation: ConversationSummary;
  messages: ConversationMessage[];
  toolCalls: ToolCallRecord[];
}

type PersistStateFile = (
  statePath: string,
  temporaryPath: string,
  bytes: Uint8Array,
) => Promise<void>;

async function persistStateFile(
  statePath: string,
  temporaryPath: string,
  bytes: Uint8Array,
): Promise<void> {
  await mkdir(path.dirname(statePath), { recursive: true });
  await writeFile(temporaryPath, bytes);
  await rename(temporaryPath, statePath);
}

function now(): string {
  return new Date().toISOString();
}

function firstRow(database: Database, sql: string, parameters: unknown[] = []): unknown[] | undefined {
  return database.exec(sql, parameters as never)[0]?.values[0];
}

function valueAt(database: Database, sql: string, parameters: unknown[] = []): unknown {
  return firstRow(database, sql, parameters)?.[0];
}

function contextByteLength(items: ModelConversationItem[]): number {
  return items.reduce(
    (total, item) => total + Buffer.byteLength(JSON.stringify(item), "utf8"),
    0,
  );
}

export class RuntimeStateStore {
  #database: Database;
  readonly #SQL: SqlJsStatic;
  readonly #persistStateFile: PersistStateFile;
  readonly #statePath?: string;
  #closed = false;
  #writeTail: Promise<void> = Promise.resolve();

  private constructor(
    SQL: SqlJsStatic,
    database: Database,
    statePath: string | undefined,
    persistFile: PersistStateFile,
  ) {
    this.#SQL = SQL;
    this.#database = database;
    this.#statePath = statePath;
    this.#persistStateFile = persistFile;
  }

  static async open(
    statePath?: string,
    persistFile: PersistStateFile = persistStateFile,
  ): Promise<RuntimeStateStore> {
    const SQL = await initSqlJs();
    let bytes: Uint8Array | undefined;
    if (statePath) {
      try {
        bytes = await readFile(statePath);
      } catch (error) {
        if ((error as NodeJS.ErrnoException).code !== "ENOENT") throw error;
      }
    }
    const store = new RuntimeStateStore(SQL, new SQL.Database(bytes), statePath, persistFile);
    store.#database.run("PRAGMA foreign_keys = ON");
    await store.#migrate();
    await store.interruptActiveRuns();
    return store;
  }

  async createConversation(
    conversation: ConversationSummary,
  ): Promise<ConversationSummary> {
    return this.#write(() => {
      const existing = firstRow(
        this.#database,
        "SELECT title, model_id FROM conversations WHERE id = ?",
        [conversation.id],
      );
      if (existing) {
        if (existing[0] === conversation.title && existing[1] === conversation.modelId) {
          return conversation;
        }
      }
      const timestamp = now();
      this.#database.run(
        `INSERT INTO conversations (id, title, model_id, created_at, updated_at)
         VALUES (?, ?, ?, ?, ?)`,
        [conversation.id, conversation.title, conversation.modelId, timestamp, timestamp],
      );
      return conversation;
    });
  }

  async ensureConversation(conversationId: string, modelId: string): Promise<void> {
    await this.#write(() => {
      const timestamp = now();
      this.#database.run(
        `INSERT OR IGNORE INTO conversations (id, title, model_id, created_at, updated_at)
         VALUES (?, 'New Conversation', ?, ?, ?)`,
        [conversationId, modelId, timestamp, timestamp],
      );
    });
  }

  async hasConversation(conversationId: string): Promise<boolean> {
    await this.#writeTail;
    return valueAt(this.#database, "SELECT 1 FROM conversations WHERE id = ?", [conversationId]) === 1;
  }

  async getProviderCapability(
    backendId: string,
    modelId: string,
    capability: "hosted_web_search",
  ): Promise<HostedWebSearchCapability> {
    await this.#writeTail;
    const status = valueAt(
      this.#database,
      `SELECT status FROM provider_capabilities
       WHERE backend_id = ? AND model_id = ? AND capability = ?`,
      [backendId, modelId, capability],
    );
    return status === "available" || status === "unavailable" ? status : "unknown";
  }

  async setProviderCapability(
    backendId: string,
    modelId: string,
    capability: "hosted_web_search",
    status: HostedWebSearchCapability,
  ): Promise<void> {
    await this.#write(() => {
      this.#database.run(
        `INSERT INTO provider_capabilities
          (backend_id, model_id, capability, status, updated_at)
         VALUES (?, ?, ?, ?, ?)
         ON CONFLICT(backend_id, model_id, capability)
         DO UPDATE SET status = excluded.status, updated_at = excluded.updated_at`,
        [backendId, modelId, capability, status, now()],
      );
    });
  }

  async resetProviderCapability(
    backendId: string,
    modelId: string,
    capability: "hosted_web_search",
  ): Promise<void> {
    await this.#write(() => {
      this.#database.run(
        `DELETE FROM provider_capabilities
         WHERE backend_id = ? AND model_id = ? AND capability = ?`,
        [backendId, modelId, capability],
      );
    });
  }

  async getProtocolResponse(
    requestEventId: string,
    requestType: string,
    conversationId: string,
    agentRunId: string,
    request: unknown,
  ): Promise<unknown | undefined> {
    await this.#writeTail;
    const row = firstRow(
      this.#database,
      `SELECT request_type, conversation_id, agent_run_id, request_hash, response_json
       FROM protocol_responses WHERE request_event_id = ?`,
      [requestEventId],
    );
    if (!row) return undefined;
    if (
      row[0] !== requestType ||
      row[1] !== conversationId ||
      row[2] !== agentRunId ||
      row[3] !== protocolRequestHash(request)
    ) {
      throw new Error(`Protocol event '${requestEventId}' was reused with conflicting identity.`);
    }
    if (row[4] === null) {
      throw new Error(`Protocol event '${requestEventId}' was invalidated after resource deletion.`);
    }
    return JSON.parse(row[4] as string) as unknown;
  }

  async storeProtocolResponse(
    requestEventId: string,
    requestType: string,
    conversationId: string,
    agentRunId: string,
    request: unknown,
    response: unknown,
    ownerConversationId?: string,
  ): Promise<unknown> {
    return this.#write(() => {
      const existing = firstRow(
        this.#database,
        `SELECT request_type, conversation_id, agent_run_id, request_hash, response_json
         FROM protocol_responses WHERE request_event_id = ?`,
        [requestEventId],
      );
      if (existing) {
        if (
          existing[0] !== requestType ||
          existing[1] !== conversationId ||
          existing[2] !== agentRunId ||
          existing[3] !== protocolRequestHash(request)
        ) {
          throw new Error(`Protocol event '${requestEventId}' was reused with conflicting identity.`);
        }
        if (existing[4] === null) {
          throw new Error(`Protocol event '${requestEventId}' was invalidated after resource deletion.`);
        }
        return JSON.parse(existing[4] as string) as unknown;
      }
      this.#database.run(
        `INSERT INTO protocol_responses
          (request_event_id, request_type, conversation_id, agent_run_id,
           owner_conversation_id, request_hash, response_json, created_at)
         VALUES (?, ?, ?, ?, ?, ?, ?, ?)`,
        [
          requestEventId,
          requestType,
          conversationId,
          agentRunId,
          ownerConversationId ?? null,
          protocolRequestHash(request),
          JSON.stringify(response),
          now(),
        ],
      );
      return response;
    });
  }

  async beginAgentRun(
    conversationId: string,
    agentRunId: string,
    modelId: string,
    input: string,
    startedEvent?: Extract<AgentRunEvent, { type: "agent_run.started" }>,
    startEventId?: string,
  ): Promise<boolean> {
    return this.#write(() => {
      const timestamp = now();
      const existing = firstRow(
        this.#database,
        `SELECT agent_runs.conversation_id, agent_runs.model_id, agent_runs.start_event_id,
                messages.text
         FROM agent_runs
         LEFT JOIN messages ON messages.id = agent_runs.user_message_id
         WHERE agent_runs.id = ?`,
        [agentRunId],
      );
      if (existing) {
        if (
          startEventId &&
          existing[0] === conversationId &&
          existing[1] === modelId &&
          existing[2] === startEventId &&
          existing[3] === input
        ) {
          return false;
        }
        throw new Error(`Agent Run '${agentRunId}' already exists with different identity.`);
      }
      if (
        startEventId &&
        firstRow(
          this.#database,
          "SELECT id FROM agent_runs WHERE start_event_id = ?",
          [startEventId],
        )
      ) {
        throw new Error(`Protocol event '${startEventId}' was reused with conflicting identity.`);
      }
      const messageId = randomUUID();
      this.#database.run(
        `INSERT OR IGNORE INTO conversations (id, title, model_id, created_at, updated_at)
         VALUES (?, 'New Conversation', ?, ?, ?)`,
        [conversationId, modelId, timestamp, timestamp],
      );
      const sequence = this.#nextMessageSequence(conversationId);
      this.#database.run(
        `INSERT INTO agent_runs
          (id, conversation_id, model_id, status, start_event_id, created_at, updated_at)
         VALUES (?, ?, ?, 'running', ?, ?, ?)`,
        [agentRunId, conversationId, modelId, startEventId ?? null, timestamp, timestamp],
      );
      this.#database.run(
        `INSERT INTO messages
          (id, conversation_id, agent_run_id, role, text, sequence, created_at)
         VALUES (?, ?, ?, 'user', ?, ?, ?)`,
        [messageId, conversationId, agentRunId, input, sequence, timestamp],
      );
      this.#database.run("UPDATE agent_runs SET user_message_id = ? WHERE id = ?", [
        messageId,
        agentRunId,
      ]);
      this.#recordEvent(
        startedEvent ?? {
          type: "agent_run.started",
          protocolVersion: PROTOCOL_VERSION,
          eventId: randomUUID(),
          conversationId,
          agentRunId,
          sequence: this.#nextAgentRunSequence(agentRunId),
          model: modelId,
        },
      );
      this.#touchConversation(conversationId, timestamp);
      return true;
    });
  }

  async completeAgentRun(
    agentRunId: string,
    output: string,
    completedEvent?: Extract<AgentRunEvent, { type: "agent_run.completed" }>,
  ): Promise<void> {
    await this.#finishRun(agentRunId, "completed", output, completedEvent);
  }

  async failAgentRun(
    agentRunId: string,
    code: string,
    message: string,
    failedEvent?: Extract<AgentRunEvent, { type: "agent_run.failed" }>,
  ): Promise<void> {
    await this.#write(() => {
      const run = this.#requiredRun(agentRunId);
      const timestamp = now();
      this.#database.run(
        `UPDATE agent_runs
         SET status = 'failed', error_code = ?, error_message = ?, updated_at = ?
         WHERE id = ?`,
        [code, message, timestamp, agentRunId],
      );
      this.#recordEvent(
        failedEvent ?? {
          type: "agent_run.failed",
          protocolVersion: PROTOCOL_VERSION,
          eventId: randomUUID(),
          conversationId: run.conversationId,
          agentRunId,
          sequence: this.#nextAgentRunSequence(agentRunId),
          error: { code: code as ProviderErrorCode, message },
        },
      );
      this.#touchConversation(run.conversationId, timestamp);
    });
  }

  async cancelAgentRun(
    agentRunId: string,
    event?: Extract<AgentRunEvent, { type: "agent_run.cancelled" }>,
  ): Promise<boolean> {
    return this.#setRunStatus(agentRunId, "cancelled", event);
  }

  async interruptAgentRun(
    agentRunId: string,
    event?: Extract<AgentRunEvent, { type: "agent_run.interrupted" }>,
  ): Promise<boolean> {
    return this.#setRunStatus(agentRunId, "interrupted", event);
  }

  async interruptActiveRuns(): Promise<AgentRunEvent[]> {
    return this.#write(() => {
      const timestamp = now();
      const running = this.#database.exec(
        "SELECT id, conversation_id FROM agent_runs WHERE status = 'running' ORDER BY created_at, id",
      )[0]?.values ?? [];
      const events: AgentRunEvent[] = [];
      for (const [agentRunId, conversationId] of running) {
        this.#database.run(
          "UPDATE agent_runs SET status = 'interrupted', updated_at = ? WHERE id = ?",
          [timestamp, agentRunId as string],
        );
        this.#database.run(
          `UPDATE tool_calls
           SET status = 'failed', error_code = 'plugin_disconnected',
               error_message = 'The Obsidian plugin disconnected during the Vault tool call.',
               updated_at = ?
           WHERE agent_run_id = ? AND status = 'requested'
             AND name != 'vault_propose_changes'`,
          [timestamp, agentRunId as string],
        );
        const event: AgentRunEvent = {
          type: "agent_run.interrupted",
          protocolVersion: PROTOCOL_VERSION,
          eventId: randomUUID(),
          conversationId: conversationId as string,
          agentRunId: agentRunId as string,
          sequence: this.#nextAgentRunSequence(agentRunId as string),
        };
        this.#recordEvent(event);
        events.push(event);
      }
      return events;
    });
  }

  async saveRunCheckpoint(agentRunId: string, checkpoint: RunCheckpoint): Promise<void> {
    await this.#write(() => {
      const run = firstRow(
        this.#database,
        "SELECT conversation_id, status FROM agent_runs WHERE id = ?",
        [agentRunId],
      );
      if (!run) throw new Error(`Agent Run '${agentRunId}' does not exist.`);
      if (run[1] !== "running") {
        throw new Error(`Agent Run '${agentRunId}' cannot checkpoint from '${run[1]}'.`);
      }
      this.#database.run("DELETE FROM run_checkpoints WHERE agent_run_id = ?", [agentRunId]);
      this.#database.run(
        `INSERT INTO run_checkpoints
          (id, conversation_id, agent_run_id, checkpoint_json, created_at)
         VALUES (?, ?, ?, ?, ?)`,
        [randomUUID(), run[0] as string, agentRunId, JSON.stringify(persistedRunCheckpoint(checkpoint)), now()],
      );
    });
  }

  async resumeAgentRun(
    conversationId: string,
    agentRunId: string,
    recoveredToolResult?: { result: LocalToolResultPayload; toolCallId: string },
  ): Promise<ResumableRun> {
    return this.#write(() => {
      const run = firstRow(
        this.#database,
        `SELECT conversation_id, model_id, status, last_sequence
         FROM agent_runs WHERE id = ?`,
        [agentRunId],
      );
      if (!run || run[0] !== conversationId) {
        throw new Error(`Agent Run '${agentRunId}' does not belong to Conversation '${conversationId}'.`);
      }
      if (run[2] !== "interrupted") {
        throw new Error(`Agent Run '${agentRunId}' cannot be resumed from '${run[2]}'.`);
      }
      const checkpointJson = valueAt(
        this.#database,
        `SELECT checkpoint_json FROM run_checkpoints
         WHERE agent_run_id = ? ORDER BY created_at DESC, rowid DESC LIMIT 1`,
        [agentRunId],
      );
      if (typeof checkpointJson !== "string") {
        throw new Error(`Agent Run '${agentRunId}' has no durable Run Checkpoint.`);
      }
      const checkpoint = JSON.parse(checkpointJson) as RunCheckpoint;
      if (checkpoint.version !== 1) {
        throw new Error(`Agent Run '${agentRunId}' has an unsupported Run Checkpoint.`);
      }
      if (checkpoint.pendingToolStep?.name === "vault_propose_changes") {
        const pendingStatus = valueAt(
          this.#database,
          "SELECT status FROM tool_calls WHERE id = ? AND agent_run_id = ?",
          [checkpoint.pendingToolStep.toolCallId, agentRunId],
        );
        if (
          pendingStatus === "requested" &&
          recoveredToolResult?.toolCallId !== checkpoint.pendingToolStep.toolCallId
        ) {
          throw new Error(
            `Agent Run '${agentRunId}' is waiting for its pending Vault Change decision.`,
          );
        }
        if (pendingStatus !== "requested" && pendingStatus !== "completed" && pendingStatus !== "failed") {
          throw new Error(`Agent Run '${agentRunId}' has an invalid pending confirmation.`);
        }
        if (recoveredToolResult) {
          this.#assertToolResultMatches(
            checkpoint.pendingToolStep.toolCallId,
            agentRunId,
            recoveredToolResult.result,
          );
        }
      } else if (recoveredToolResult) {
        throw new Error(`Agent Run '${agentRunId}' has no pending confirmation to recover.`);
      }
      this.#database.run(
        `UPDATE agent_runs SET status = 'running', error_code = NULL, error_message = NULL,
           updated_at = ? WHERE id = ? AND status = 'interrupted'`,
        [now(), agentRunId],
      );
      if (this.#database.getRowsModified() !== 1) {
        throw new Error(`Agent Run '${agentRunId}' could not transition back to running.`);
      }
      return {
        checkpoint,
        model: run[1] as string,
        nextSequence: Number(run[3]) + 1,
      };
    });
  }

  async getToolCallResult(toolCallId: string, agentRunId: string): Promise<LocalToolResultPayload | undefined> {
    await this.#writeTail;
    const row = firstRow(
      this.#database,
      `SELECT status, result_json FROM tool_calls WHERE id = ? AND agent_run_id = ?`,
      [toolCallId, agentRunId],
    );
    if (!row || (row[0] !== "completed" && row[0] !== "failed") || typeof row[1] !== "string") {
      return undefined;
    }
    return JSON.parse(row[1]) as LocalToolResultPayload;
  }

  async acknowledgeDurableEvent(
    eventId: string,
    conversationId: string,
    agentRunId: string,
    sequence?: number,
  ): Promise<void> {
    await this.#write(() => {
      const acknowledgedSequence = sequence ?? valueAt(
        this.#database,
        `SELECT sequence FROM durable_events
         WHERE id = ? AND conversation_id = ? AND agent_run_id = ?`,
        [eventId, conversationId, agentRunId],
      );
      const numericSequence = Number(acknowledgedSequence);
      if (
        !Number.isInteger(numericSequence) ||
        valueAt(
          this.#database,
          `SELECT 1 FROM durable_events
           WHERE id = ? AND conversation_id = ? AND agent_run_id = ? AND sequence = ?`,
          [eventId, conversationId, agentRunId, numericSequence],
        ) !== 1
      ) {
        throw new Error("Durable event acknowledgement does not match a persisted event.");
      }
      this.#database.run(
        `UPDATE durable_events SET acknowledged_at = ?
         WHERE conversation_id = ? AND agent_run_id = ? AND sequence <= ?
           AND acknowledged_at IS NULL`,
        [now(), conversationId, agentRunId, numericSequence],
      );
    });
  }

  async advanceAgentRunSequence(agentRunId: string, sequence: number): Promise<void> {
    await this.#write(() => {
      this.#database.run(
        `UPDATE agent_runs SET last_sequence = ?
         WHERE id = ? AND status = 'running' AND last_sequence < ?`,
        [sequence, agentRunId, sequence],
      );
      if (this.#database.getRowsModified() !== 1) {
        throw new Error(`Agent Run '${agentRunId}' cannot advance to sequence ${sequence}.`);
      }
    });
  }

  async recordAgentRunEvent(event: AgentRunEvent): Promise<void> {
    await this.#write(() => {
      this.#requiredRun(event.agentRunId);
      this.#recordEvent(event);
    });
  }

  async listUnacknowledgedEvents(agentRunId?: string): Promise<AgentRunEvent[]> {
    await this.#writeTail;
    const rows =
      this.#database.exec(
        `SELECT id, conversation_id, agent_run_id, event_type, sequence, payload_json
         FROM durable_events
         WHERE acknowledged_at IS NULL${agentRunId ? " AND agent_run_id = ?" : ""}
         ORDER BY rowid`,
        agentRunId ? [agentRunId] : [],
      )[0]?.values ?? [];
    return rows.map(([eventId, conversationId, storedAgentRunId, eventType, sequence, payload]) => ({
      ...(JSON.parse(payload as string) as Record<string, unknown>),
      type: eventType,
      protocolVersion: PROTOCOL_VERSION,
      eventId,
      conversationId,
      agentRunId: storedAgentRunId,
      sequence,
    }) as AgentRunEvent);
  }

  async requestToolCall(
    agentRunId: string,
    event: Extract<AgentRunEvent, { type: "tool_call.requested" }>,
    checkpoint?: RunCheckpoint,
  ): Promise<void> {
    await this.#write(() => {
      const run = this.#requiredRun(agentRunId);
      const timestamp = now();
      this.#database.run(
        `INSERT INTO tool_calls
          (id, conversation_id, agent_run_id, name, arguments_json, status, created_at, updated_at)
         VALUES (?, ?, ?, ?, ?, 'requested', ?, ?)`,
        [
          event.toolCallId,
          run.conversationId,
          agentRunId,
          event.tool.name,
          JSON.stringify(persistedToolArguments(event.tool.name, event.tool.arguments)),
          timestamp,
          timestamp,
        ],
      );
      if (
        event.tool.name === "vault_propose_changes" &&
        event.tool.arguments &&
        typeof event.tool.arguments === "object" &&
        !Array.isArray(event.tool.arguments)
      ) {
        const proposal = event.tool.arguments as {
          actions?: Array<{ path?: unknown }>;
          batchId?: unknown;
          idempotencyKey?: unknown;
          task?: unknown;
        };
        if (
          typeof proposal.batchId === "string" &&
          typeof proposal.idempotencyKey === "string" &&
          typeof proposal.task === "string" &&
          Array.isArray(proposal.actions) &&
          proposal.actions.every((action) => typeof action?.path === "string")
        ) {
          this.#database.run(
            `INSERT INTO vault_change_batches
              (id, conversation_id, agent_run_id, tool_call_id, idempotency_key, task,
               target_paths_json, state, created_at, updated_at)
             VALUES (?, ?, ?, ?, ?, ?, ?, 'pending', ?, ?)`,
            [
              proposal.batchId,
              run.conversationId,
              agentRunId,
              event.toolCallId,
              proposal.idempotencyKey,
              proposal.task,
              JSON.stringify(proposal.actions.map((action) => action.path)),
              timestamp,
              timestamp,
            ],
          );
        }
      }
      if (checkpoint) {
        this.#database.run("DELETE FROM run_checkpoints WHERE agent_run_id = ?", [agentRunId]);
        this.#database.run(
          `INSERT INTO run_checkpoints
            (id, conversation_id, agent_run_id, checkpoint_json, created_at)
           VALUES (?, ?, ?, ?, ?)`,
          [
            randomUUID(),
            run.conversationId,
            agentRunId,
            JSON.stringify(persistedRunCheckpoint(checkpoint)),
            timestamp,
          ],
        );
      }
      this.#recordEvent(
        event.tool.name === "vault_propose_changes"
          ? {
              ...event,
              tool: {
                ...event.tool,
                arguments: persistedToolArguments(event.tool.name, event.tool.arguments),
              },
            }
          : event,
      );
      this.#touchConversation(run.conversationId, timestamp);
    });
  }

  async markVaultChangeApplying(
    batchId: string,
    checkpointRef: string,
    targets: VaultChangeTargetResult[],
  ): Promise<void> {
    await this.#write(() => {
      const row = firstRow(
        this.#database,
        `SELECT state, target_paths_json, checkpoint_ref, before_hashes_json, after_hashes_json
         FROM vault_change_batches WHERE id = ?`,
        [batchId],
      );
      if (!row) throw new Error(`Vault Change Batch '${batchId}' does not exist.`);
      const targetPaths = JSON.stringify(targets.map((target) => target.path));
      if (row[1] !== targetPaths) {
        throw new Error(`Vault Change Batch '${batchId}' target paths do not match its proposal.`);
      }
      const beforeHashes = JSON.stringify(
        Object.fromEntries(targets.map((target) => [target.path, target.beforeHash])),
      );
      const afterHashes = JSON.stringify(
        Object.fromEntries(targets.map((target) => [target.path, target.afterHash])),
      );
      if (row[0] === "applying") {
        if (row[2] === checkpointRef && row[3] === beforeHashes && row[4] === afterHashes) return;
        throw new Error(`Vault Change Batch '${batchId}' has conflicting applying metadata.`);
      }
      if (row[0] !== "pending") {
        throw new Error(`Vault Change Batch '${batchId}' cannot enter applying from '${row[0]}'.`);
      }
      this.#database.run(
        `UPDATE vault_change_batches
         SET state = 'applying', checkpoint_ref = ?, before_hashes_json = ?,
             after_hashes_json = ?, updated_at = ?
         WHERE id = ? AND state = 'pending'`,
        [checkpointRef, beforeHashes, afterHashes, now(), batchId],
      );
      if (this.#database.getRowsModified() !== 1) {
        throw new Error(`Vault Change Batch '${batchId}' could not enter applying.`);
      }
    });
  }

  async markVaultChangeState(
    batchId: string,
    state: Extract<
      VaultChangeTransactionState,
      "applied" | "expired" | "recovery_failed" | "rolled_back" | "undone"
    >,
  ): Promise<void> {
    await this.#write(() => {
      const current = valueAt(
        this.#database,
        "SELECT state FROM vault_change_batches WHERE id = ?",
        [batchId],
      );
      if (current === state) return;
      const allowed =
        (current === "applying" &&
          (state === "applied" || state === "recovery_failed" || state === "rolled_back")) ||
        (current === "applied" &&
          (state === "expired" || state === "recovery_failed" || state === "undone"));
      if (!allowed) {
        throw new Error(`Vault Change Batch '${batchId}' cannot transition from '${current}' to '${state}'.`);
      }
      this.#database.run(
        "UPDATE vault_change_batches SET state = ?, updated_at = ? WHERE id = ?",
        [state, now(), batchId],
      );
    });
  }

  async listVaultChangeBatches(
    states: VaultChangeTransactionState[],
  ): Promise<VaultChangeJournalRecord[]> {
    await this.#writeTail;
    if (states.length === 0) return [];
    const rows =
      this.#database.exec(
        `SELECT vault_change_batches.id, checkpoint_ref, vault_change_batches.state,
                target_paths_json, before_hashes_json, after_hashes_json,
                tool_calls.arguments_json
         FROM vault_change_batches
         JOIN tool_calls ON tool_calls.id = vault_change_batches.tool_call_id
         WHERE vault_change_batches.state IN (${states.map(() => "?").join(", ")})
         ORDER BY vault_change_batches.created_at, vault_change_batches.id`,
        states as never,
      )[0]?.values ?? [];
    return rows.flatMap(
      ([
        batchId,
        checkpointRef,
        state,
        targetPathsJson,
        beforeHashesJson,
        afterHashesJson,
        argumentsJson,
      ]) => {
        if (!checkpointRef || !afterHashesJson) return [];
        const paths = JSON.parse(targetPathsJson as string) as string[];
        const beforeHashes = beforeHashesJson
          ? JSON.parse(beforeHashesJson as string) as Record<string, string>
          : {};
        const afterHashes = JSON.parse(afterHashesJson as string) as Record<string, string>;
        let operations = new Map<string, unknown>();
        try {
          const metadata = JSON.parse(argumentsJson as string) as {
            actions?: Array<{ operation?: unknown; path?: unknown }>;
          };
          operations = new Map(
            (metadata.actions ?? []).flatMap((action) =>
              typeof action.path === "string" ? [[action.path, action.operation] as const] : [],
            ),
          );
        } catch {
          // Missing legacy metadata is handled conservatively during checkpoint reconciliation.
        }
        return [{
          batchId: batchId as string,
          checkpointRef: checkpointRef as string,
          state: state as VaultChangeTransactionState,
          targets: paths.map((path) => ({
            path,
            beforeHash: beforeHashes[path] ?? (operations.get(path) === "create" ? "missing" : ""),
            afterHash: afterHashes[path],
          })),
        }];
      },
    );
  }

  async getVaultChangeConversationId(batchId: string): Promise<string> {
    await this.#writeTail;
    const conversationId = valueAt(
      this.#database,
      "SELECT conversation_id FROM vault_change_batches WHERE id = ?",
      [batchId],
    );
    if (typeof conversationId !== "string") {
      throw new Error(`Vault Change Batch '${batchId}' does not exist.`);
    }
    return conversationId;
  }

  async completeToolCall(
    agentRunId: string,
    result: LocalToolResultPayload,
    event: Extract<AgentRunEvent, { type: "tool_call.completed" }>,
    resultEventId?: string,
  ): Promise<string[]> {
    return this.#write(() => {
      const run = this.#requiredRun(agentRunId);
      this.#assertToolResultMatches(event.toolCallId, agentRunId, result);
      const timestamp = now();
      const stalePaths: string[] = [];
      this.#database.run(
        `UPDATE tool_calls
         SET status = ?, error_code = ?, error_message = ?, result_event_id = ?,
             result_json = ?, updated_at = ?
         WHERE id = ? AND agent_run_id = ? AND status = 'requested'`,
        [
          result.ok ? "completed" : "failed",
          result.ok ? null : result.error.code,
          result.ok ? null : result.error.message,
          resultEventId ?? null,
          JSON.stringify(persistedToolResult(result)),
          timestamp,
          event.toolCallId,
          agentRunId,
        ],
      );
      if (this.#database.getRowsModified() !== 1) {
        throw new Error(`Tool Call '${event.toolCallId}' is not pending.`);
      }
      if (result.ok && result.value.type === "vault_propose_changes") {
        this.#database.run(
          `UPDATE vault_change_batches
           SET state = ?, checkpoint_ref = ?, after_hashes_json = ?, updated_at = ?
           WHERE tool_call_id = ?
             AND state IN ('pending', 'applying', 'applied', 'rejected')`,
          [
            result.value.decision,
            result.value.checkpointRef ?? null,
            JSON.stringify(
              Object.fromEntries(
                result.value.targets.map((target) => [target.path, target.afterHash]),
              ),
            ),
            timestamp,
            event.toolCallId,
          ],
        );
      } else if (!result.ok) {
        this.#database.run(
          `UPDATE vault_change_batches SET state = 'failed', updated_at = ?
           WHERE tool_call_id = ? AND state = 'pending'`,
          [timestamp, event.toolCallId],
        );
      }
      if (result.ok) {
        const sources =
          result.value.type === "vault_read"
            ? [{ path: result.value.path, contentHash: result.value.contentHash }]
            : result.value.type === "vault_propose_changes" &&
                result.value.decision === "applied"
              ? result.value.targets.map(({ path, afterHash }) => ({
                  path,
                  contentHash: afterHash,
                }))
            : result.value.type === "vault_list" || result.value.type === "vault_search"
              ? result.value.entries.map(({ path, contentHash }) => ({ path, contentHash }))
              : [];
        for (const source of sources) {
          this.#database.run(
            `UPDATE evidence_snapshots
             SET is_stale = 1, stale_detected_at = ?
             WHERE path = ? AND content_hash <> ? AND is_stale = 0`,
            [timestamp, source.path, source.contentHash],
          );
          if (this.#database.getRowsModified() > 0) stalePaths.push(source.path);
        }
      }
      if (result.ok && result.value.type === "vault_read") {
        const evidence = result.value;
        this.#database.run(
          `INSERT INTO evidence_snapshots
            (id, conversation_id, agent_run_id, tool_call_id, path, line_start, line_end,
             modified_version, content_hash, content, created_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)`,
          [
            randomUUID(),
            run.conversationId,
            agentRunId,
            event.toolCallId,
            evidence.path,
            evidence.lineStart,
            evidence.lineEnd,
            evidence.modifiedVersion,
            evidence.contentHash,
            evidence.content,
            timestamp,
          ],
        );
      }
      this.#recordEvent(event);
      this.#touchConversation(run.conversationId, timestamp);
      return stalePaths;
    });
  }

  #assertToolResultMatches(
    toolCallId: string,
    agentRunId: string,
    result: LocalToolResultPayload,
  ): void {
    const row = firstRow(
      this.#database,
      "SELECT name FROM tool_calls WHERE id = ? AND agent_run_id = ?",
      [toolCallId, agentRunId],
    );
    if (!row) throw new Error(`Tool Call '${toolCallId}' does not belong to Agent Run '${agentRunId}'.`);
    if (!result.ok) return;
    const name = row[0] as ToolCallRecord["name"];
    if (result.value.type !== name) {
      throw new Error(`Tool Call '${toolCallId}' returned '${result.value.type}' for '${name}'.`);
    }
    if (name !== "vault_propose_changes" || result.value.type !== "vault_propose_changes") return;
    const batch = firstRow(
      this.#database,
      "SELECT id, target_paths_json FROM vault_change_batches WHERE tool_call_id = ? AND agent_run_id = ?",
      [toolCallId, agentRunId],
    );
    if (!batch) throw new Error(`Vault Change Tool Call '${toolCallId}' has no pending batch.`);
    const expectedPaths = JSON.parse(batch[1] as string) as string[];
    const resultPaths = result.value.targets.map((target) => target.path);
    if (result.value.batchId !== batch[0] || JSON.stringify(resultPaths) !== JSON.stringify(expectedPaths)) {
      throw new Error(`Vault Change Tool Call '${toolCallId}' returned a mismatched batch result.`);
    }
  }

  async isDuplicateToolResult(
    toolCallId: string,
    eventId: string,
    result: LocalToolResultPayload,
  ): Promise<boolean> {
    await this.#writeTail;
    const row = firstRow(
      this.#database,
      `SELECT result_event_id, result_json, status FROM tool_calls WHERE id = ?`,
      [toolCallId],
    );
    return Boolean(
      row &&
      row[0] === eventId &&
      row[1] === JSON.stringify(persistedToolResult(result)) &&
      (row[2] === "completed" || row[2] === "failed"),
    );
  }

  async getConversation(conversationId: string): Promise<ConversationSnapshot> {
    await this.#writeTail;
    const conversationRow = firstRow(
      this.#database,
      "SELECT id, title, model_id FROM conversations WHERE id = ?",
      [conversationId],
    );
    if (!conversationRow) throw new Error(`Conversation '${conversationId}' does not exist.`);
    const messageRows =
      this.#database.exec(
        `SELECT id, agent_run_id, role, text, sequence, citations_json
         FROM messages WHERE conversation_id = ? ORDER BY sequence`,
        [conversationId],
      )[0]?.values ?? [];
    const runRows =
      this.#database.exec(
        `SELECT id, model_id, status, error_code, error_message
         FROM agent_runs WHERE conversation_id = ? ORDER BY created_at, id`,
        [conversationId],
      )[0]?.values ?? [];
    const toolCallRows =
      this.#database.exec(
        `SELECT tool_calls.id, tool_calls.agent_run_id, tool_calls.name,
                tool_calls.arguments_json, tool_calls.status, vault_change_batches.state,
                tool_calls.error_code, tool_calls.error_message
         FROM tool_calls
         LEFT JOIN vault_change_batches ON vault_change_batches.tool_call_id = tool_calls.id
         WHERE tool_calls.conversation_id = ?
         ORDER BY tool_calls.created_at, tool_calls.id`,
        [conversationId],
      )[0]?.values ?? [];
    return {
      conversation: {
        id: conversationRow[0] as string,
        title: conversationRow[1] as string,
        modelId: conversationRow[2] as string,
      },
      messages: messageRows.map(([id, agentRunId, role, text, sequence, citationsJson]) => ({
        id: id as string,
        agentRunId: agentRunId as string,
        role: role as "assistant" | "user",
        text: text as string,
        sequence: sequence as number,
        ...((JSON.parse(citationsJson as string) as unknown[]).length > 0
          ? { citations: JSON.parse(citationsJson as string) }
          : {}),
      })),
      agentRuns: runRows.map(([id, modelId, status, errorCode, errorMessage]) => ({
        id: id as string,
        modelId: modelId as string,
        status: status as AgentRunStatus,
        ...(errorCode && errorMessage
          ? { error: { code: errorCode as ProviderErrorCode, message: errorMessage as string } }
          : {}),
      })),
      toolCalls: toolCallRows.map(([
        id,
        agentRunId,
        name,
        argumentsJson,
        status,
        batchState,
        errorCode,
        errorMessage,
      ]) => ({
        id: id as string,
        agentRunId: agentRunId as string,
        name: name as ToolCallRecord["name"],
        arguments: JSON.parse(argumentsJson as string) as unknown,
        status: status as ToolCallRecord["status"],
        ...(errorCode && errorMessage
          ? { error: { code: errorCode as VaultToolErrorCode, message: errorMessage as string } }
          : {}),
        ...(batchState === "applied" || batchState === "rejected"
          ? { decision: batchState }
          : {}),
        ...(batchState ? { vaultChangeState: batchState as VaultChangeTransactionState } : {}),
      })),
    };
  }

  async getConversationContext(
    conversationId: string,
    agentRunId: string,
  ): Promise<ModelConversationItem[]> {
    await this.#writeTail;
    const rows =
      this.#database.exec(
        `SELECT messages.agent_run_id, messages.role, messages.text
         FROM messages
         JOIN agent_runs ON agent_runs.id = messages.agent_run_id
         WHERE messages.conversation_id = ?
           AND (agent_runs.status = 'completed' OR agent_runs.id = ?)
         ORDER BY messages.sequence`,
        [conversationId, agentRunId],
      )[0]?.values ?? [];
    const current: ModelConversationItem[] = [];
    const completedTurns: ModelConversationItem[][] = [];
    const turnsByRunId = new Map<string, ModelConversationItem[]>();
    for (const [messageRunIdValue, role, textValue] of rows) {
      const messageRunId = messageRunIdValue as string;
      const item: ModelConversationItem = {
        type: role === "assistant" ? "assistant_message" : "user_message",
        text: textValue as string,
      };
      if (messageRunId === agentRunId) {
        current.push(item);
        continue;
      }
      let turn = turnsByRunId.get(messageRunId);
      if (!turn) {
        turn = [];
        turnsByRunId.set(messageRunId, turn);
        completedTurns.push(turn);
      }
      turn.push(item);
    }

    let remainingBytes = Math.max(
      0,
      MAX_CONVERSATION_CONTEXT_BYTES - contextByteLength(current),
    );
    const retainedTurns: ModelConversationItem[][] = [];
    for (let index = completedTurns.length - 1; index >= 0; index -= 1) {
      const turn = completedTurns[index];
      const turnBytes = contextByteLength(turn);
      if (turnBytes > remainingBytes) break;
      retainedTurns.unshift(turn);
      remainingBytes -= turnBytes;
    }
    return [...retainedTurns.flat(), ...current];
  }

  async listConversations(): Promise<ConversationSummary[]> {
    await this.#writeTail;
    const rows =
      this.#database.exec(
        "SELECT id, title, model_id FROM conversations ORDER BY updated_at DESC, id",
      )[0]?.values ?? [];
    return rows.map(([id, title, modelId]) => ({
      id: id as string,
      title: title as string,
      modelId: modelId as string,
    }));
  }

  async updateConversationModel(
    conversationId: string,
    modelId: string,
  ): Promise<ConversationSummary> {
    await this.#write(() => {
      this.#database.run(
        "UPDATE conversations SET model_id = ?, updated_at = ? WHERE id = ?",
        [modelId, now(), conversationId],
      );
      if (this.#database.getRowsModified() !== 1) {
        throw new Error(`Conversation '${conversationId}' does not exist.`);
      }
    });
    return (await this.getConversation(conversationId)).conversation;
  }

  async deleteConversation(conversationId: string): Promise<void> {
    await this.#write(() => {
      this.#database.run(
        `UPDATE protocol_responses SET response_json = NULL
         WHERE conversation_id = ? OR owner_conversation_id = ?`,
        [conversationId, conversationId],
      );
      this.#database.run("DELETE FROM conversations WHERE id = ?", [conversationId]);
    });
  }

  async close(): Promise<void> {
    if (this.#closed) return;
    await this.#writeTail;
    await this.#persist();
    this.#database.close();
    this.#closed = true;
  }

  async #finishRun(
    agentRunId: string,
    status: "completed",
    output: string,
    completedEvent?: Extract<AgentRunEvent, { type: "agent_run.completed" }>,
  ): Promise<void> {
    await this.#write(() => {
      const run = this.#requiredRun(agentRunId);
      const timestamp = now();
      const messageId = randomUUID();
      const sequence = this.#nextMessageSequence(run.conversationId);
      this.#database.run(
        `INSERT INTO messages
          (id, conversation_id, agent_run_id, role, text, sequence, citations_json, created_at)
         VALUES (?, ?, ?, 'assistant', ?, ?, ?, ?)`,
        [
          messageId,
          run.conversationId,
          agentRunId,
          output,
          sequence,
          JSON.stringify(completedEvent?.output.citations ?? []),
          timestamp,
        ],
      );
      this.#database.run(
        `UPDATE agent_runs
         SET status = ?, assistant_message_id = ?, updated_at = ? WHERE id = ?`,
        [status, messageId, timestamp, agentRunId],
      );
      this.#recordEvent(
        completedEvent ?? {
          type: "agent_run.completed",
          protocolVersion: PROTOCOL_VERSION,
          eventId: randomUUID(),
          conversationId: run.conversationId,
          agentRunId,
          sequence: this.#nextAgentRunSequence(agentRunId),
          output: { role: "assistant", text: output },
        },
      );
      this.#touchConversation(run.conversationId, timestamp);
    });
  }

  async #setRunStatus(
    agentRunId: string,
    status: "cancelled" | "interrupted",
    event?: Extract<AgentRunEvent, { type: "agent_run.cancelled" | "agent_run.interrupted" }>,
  ): Promise<boolean> {
    return this.#write(() => {
      const run = this.#requiredRun(agentRunId);
      const timestamp = now();
      this.#database.run(
        "UPDATE agent_runs SET status = ?, updated_at = ? WHERE id = ? AND status = 'running'",
        [status, timestamp, agentRunId],
      );
      if (this.#database.getRowsModified() !== 1) return false;
      this.#database.run(
        `UPDATE tool_calls
          SET status = 'failed', error_code = ?, error_message = ?, updated_at = ?
          WHERE agent_run_id = ? AND status = 'requested'
            ${status === "cancelled" ? "" : "AND name != 'vault_propose_changes'"}`,
        [
          status === "cancelled" ? "tool_error" : "plugin_disconnected",
          status === "cancelled"
            ? "The Agent Run was cancelled during the Vault tool call."
            : "The Obsidian plugin disconnected during the Vault tool call.",
          timestamp,
          agentRunId,
        ],
      );
      if (status === "cancelled") {
        this.#database.run(
          `UPDATE vault_change_batches
            SET state = 'failed', updated_at = ?
            WHERE agent_run_id = ? AND state = 'pending'`,
          [timestamp, agentRunId],
        );
      }
      this.#recordEvent(
        event ?? {
          type: status === "cancelled" ? "agent_run.cancelled" : "agent_run.interrupted",
          protocolVersion: PROTOCOL_VERSION,
          eventId: randomUUID(),
          conversationId: run.conversationId,
          agentRunId,
          sequence: this.#nextAgentRunSequence(agentRunId),
        },
      );
      this.#touchConversation(run.conversationId, timestamp);
      return true;
    });
  }

  #requiredRun(agentRunId: string): { conversationId: string } {
    const row = firstRow(
      this.#database,
      "SELECT conversation_id FROM agent_runs WHERE id = ?",
      [agentRunId],
    );
    if (!row) throw new Error(`Agent Run '${agentRunId}' does not exist.`);
    return { conversationId: row[0] as string };
  }

  #nextMessageSequence(conversationId: string): number {
    const current = valueAt(
      this.#database,
      "SELECT COALESCE(MAX(sequence), 0) FROM messages WHERE conversation_id = ?",
      [conversationId],
    );
    return Number(current) + 1;
  }

  #recordEvent(event: AgentRunEvent): void {
    this.#database.run(
      `INSERT INTO durable_events
        (id, conversation_id, agent_run_id, event_type, sequence, payload_json, created_at)
       VALUES (?, ?, ?, ?, ?, ?, ?)`,
      [
        event.eventId,
        event.conversationId,
        event.agentRunId,
        event.type,
        event.sequence,
        JSON.stringify(event),
        now(),
      ],
    );
    this.#database.run(
      "UPDATE agent_runs SET last_sequence = MAX(last_sequence, ?) WHERE id = ?",
      [event.sequence, event.agentRunId],
    );
  }

  #nextAgentRunSequence(agentRunId: string): number {
    const current = valueAt(
      this.#database,
      "SELECT last_sequence FROM agent_runs WHERE id = ?",
      [agentRunId],
    );
    return Number(current) + 1;
  }

  #touchConversation(conversationId: string, timestamp: string): void {
    this.#database.run("UPDATE conversations SET updated_at = ? WHERE id = ?", [
      timestamp,
      conversationId,
    ]);
  }

  async #migrate(): Promise<void> {
    const version = Number(valueAt(this.#database, "PRAGMA user_version") ?? 0);
    if (version > CURRENT_SCHEMA_VERSION) {
      throw new Error(
        `Runtime State schema ${version} is newer than supported schema ${CURRENT_SCHEMA_VERSION}.`,
      );
    }
    for (const migration of MIGRATIONS) {
      if (migration.version <= version) continue;
      this.#database.run("BEGIN IMMEDIATE");
      try {
        if (migration.version === 9) this.#ensureProtocolColumns();
        this.#database.run(migration.sql);
        if (migration.version === 7) this.#scrubPersistedVaultChangeArguments();
        if (migration.version === 8) this.#ensureCitationsColumn();
        if (migration.version === 10) this.#scrubResumableStateBodies();
        const timestamp = now();
        this.#database.run(
          "INSERT INTO schema_migrations (version, applied_at) VALUES (?, ?)",
          [migration.version, timestamp],
        );
        this.#database.run(
          `INSERT INTO settings_metadata (key, value, updated_at)
           VALUES ('schema_version', ?, ?)
           ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at`,
          [`${migration.version}`, timestamp],
        );
        this.#database.run(`PRAGMA user_version = ${migration.version}`);
        this.#database.run("COMMIT");
      } catch (error) {
        this.#database.run("ROLLBACK");
        throw error;
      }
    }
    // Early v9/v10 builds could persist the current version before these additive
    // protocol columns existed. Keep the repair idempotent for already-versioned State.
    this.#ensureProtocolColumns();
    if (version < 10) this.#database.run("VACUUM");
    await this.#persist();
  }

  #scrubPersistedVaultChangeArguments(): void {
    const rows =
      this.#database.exec(
        `SELECT id, arguments_json FROM tool_calls
         WHERE name = 'vault_propose_changes'`,
      )[0]?.values ?? [];
    for (const [id, argumentsJson] of rows) {
      let arguments_: unknown = {};
      try {
        arguments_ = JSON.parse(argumentsJson as string) as unknown;
      } catch {
        // Invalid legacy arguments stay represented as empty metadata, never raw source text.
      }
      this.#database.run(
        "UPDATE tool_calls SET arguments_json = ? WHERE id = ?",
        [JSON.stringify(persistedToolArguments("vault_propose_changes", arguments_)), id],
      );
    }
    const events =
      this.#database.exec(
        `SELECT id, payload_json FROM durable_events
         WHERE event_type = 'tool_call.requested'`,
      )[0]?.values ?? [];
    for (const [id, payloadJson] of events) {
      try {
        const event = JSON.parse(payloadJson as string) as Extract<
          AgentRunEvent,
          { type: "tool_call.requested" }
        >;
        if (event.tool?.name !== "vault_propose_changes") continue;
        this.#database.run(
          "UPDATE durable_events SET payload_json = ? WHERE id = ?",
          [
            JSON.stringify({
              ...event,
              tool: {
                ...event.tool,
                arguments: persistedToolArguments(event.tool.name, event.tool.arguments),
              },
            }),
            id,
          ],
        );
      } catch {
        this.#database.run(
          "UPDATE durable_events SET payload_json = '{}' WHERE id = ?",
          [id],
        );
      }
    }
  }

  #scrubResumableStateBodies(): void {
    const checkpoints = this.#database.exec(
      "SELECT id, checkpoint_json FROM run_checkpoints",
    )[0]?.values ?? [];
    for (const [id, checkpointJson] of checkpoints) {
      try {
        const legacy = JSON.parse(checkpointJson as string) as Partial<RunCheckpoint> & {
          localSkills?: Array<string | [string, string]>;
        };
        const checkpoint: RunCheckpoint = {
          version: 1,
          input: Array.isArray(legacy.input) ? legacy.input : [],
          localSkills: Array.isArray(legacy.localSkills)
            ? legacy.localSkills.flatMap((skill) =>
                typeof skill === "string"
                  ? [skill]
                  : Array.isArray(skill) && typeof skill[0] === "string"
                    ? [skill[0]]
                    : [],
              )
            : [],
          canonicalReadPaths: Array.isArray(legacy.canonicalReadPaths)
            ? legacy.canonicalReadPaths
            : [],
          requiredRereads: Array.isArray(legacy.requiredRereads) ? legacy.requiredRereads : [],
          hostedWebSearchProbeAttempted: legacy.hostedWebSearchProbeAttempted === true,
          completedSteps: Number.isInteger(legacy.completedSteps) ? legacy.completedSteps as number : 0,
          ...(legacy.pendingToolStep
            ? { pendingToolStep: legacy.pendingToolStep }
            : (legacy as { pendingConfirmation?: Omit<NonNullable<RunCheckpoint["pendingToolStep"]>, "name"> }).pendingConfirmation
              ? {
                  pendingToolStep: {
                    ...(legacy as { pendingConfirmation: Omit<NonNullable<RunCheckpoint["pendingToolStep"]>, "name"> }).pendingConfirmation,
                    name: "vault_propose_changes",
                  },
                }
              : {}),
        };
        this.#database.run(
          "UPDATE run_checkpoints SET checkpoint_json = ? WHERE id = ?",
          [JSON.stringify(persistedRunCheckpoint(checkpoint)), id],
        );
      } catch {
        this.#database.run("DELETE FROM run_checkpoints WHERE id = ?", [id]);
      }
    }
    const results = this.#database.exec(
      `SELECT id, result_json FROM tool_calls
       WHERE name IN ('agent_contract_read', 'planning_memory_read', 'skill_read')
         AND result_json IS NOT NULL`,
    )[0]?.values ?? [];
    for (const [id, resultJson] of results) {
      try {
        const result = JSON.parse(resultJson as string) as LocalToolResultPayload;
        this.#database.run(
          "UPDATE tool_calls SET result_json = ? WHERE id = ?",
          [JSON.stringify(persistedToolResult(result)), id],
        );
      } catch {
        this.#database.run("UPDATE tool_calls SET result_json = NULL WHERE id = ?", [id]);
      }
    }
  }

  #ensureProtocolColumns(): void {
    const runColumns = new Set(
      (this.#database.exec("PRAGMA table_info(agent_runs)")[0]?.values ?? [])
        .map((column) => column[1]),
    );
    if (!runColumns.has("start_event_id")) {
      this.#database.run("ALTER TABLE agent_runs ADD COLUMN start_event_id TEXT");
    }
    const toolColumns = new Set(
      (this.#database.exec("PRAGMA table_info(tool_calls)")[0]?.values ?? [])
        .map((column) => column[1]),
    );
    if (!toolColumns.has("result_event_id")) {
      this.#database.run("ALTER TABLE tool_calls ADD COLUMN result_event_id TEXT");
    }
    if (!toolColumns.has("result_json")) {
      this.#database.run("ALTER TABLE tool_calls ADD COLUMN result_json TEXT");
    }
    const responseColumns = new Set(
      (this.#database.exec("PRAGMA table_info(protocol_responses)")[0]?.values ?? [])
        .map((column) => column[1]),
    );
    if (responseColumns.size > 0 && !responseColumns.has("owner_conversation_id")) {
      this.#database.run("ALTER TABLE protocol_responses ADD COLUMN owner_conversation_id TEXT");
    }
    if (responseColumns.size > 0 && !responseColumns.has("request_hash")) {
      this.#database.run("ALTER TABLE protocol_responses ADD COLUMN request_hash TEXT");
    }
    if (responseColumns.size > 0) {
      this.#database.run(
        "CREATE INDEX IF NOT EXISTS protocol_responses_by_conversation ON protocol_responses(conversation_id)",
      );
      this.#database.run(
        "CREATE INDEX IF NOT EXISTS protocol_responses_by_owner ON protocol_responses(owner_conversation_id)",
      );
    }
    this.#database.run(
      `CREATE UNIQUE INDEX IF NOT EXISTS agent_runs_by_start_event
       ON agent_runs(start_event_id) WHERE start_event_id IS NOT NULL`,
    );
    this.#database.run(
      `CREATE UNIQUE INDEX IF NOT EXISTS tool_calls_by_result_event
       ON tool_calls(result_event_id) WHERE result_event_id IS NOT NULL`,
    );
  }

  #ensureCitationsColumn(): void {
    const columns = this.#database.exec("PRAGMA table_info(messages)")[0]?.values ?? [];
    if (!columns.some((column) => column[1] === "citations_json")) {
      this.#database.run("ALTER TABLE messages ADD COLUMN citations_json TEXT NOT NULL DEFAULT '[]'");
    }
  }

  #write<T>(operation: () => T): Promise<T> {
    if (this.#closed) return Promise.reject(new Error("Runtime State is closed."));
    const result = this.#writeTail.then(async () => {
      const snapshot = this.#database.export();
      this.#database.run("PRAGMA foreign_keys = ON");
      let committed = false;
      this.#database.run("BEGIN IMMEDIATE");
      try {
        const value = operation();
        this.#database.run("COMMIT");
        committed = true;
        await this.#persist();
        return value;
      } catch (error) {
        if (committed) this.#restore(snapshot);
        else this.#database.run("ROLLBACK");
        throw error;
      }
    });
    this.#writeTail = result.then(
      () => undefined,
      () => undefined,
    );
    return result;
  }

  async #persist(): Promise<void> {
    if (!this.#statePath) return;
    const temporaryPath = `${this.#statePath}.${process.pid}.tmp`;
    const bytes = this.#database.export();
    // sql.js reopens the in-memory database during export, resetting connection PRAGMAs.
    this.#database.run("PRAGMA foreign_keys = ON");
    await this.#persistStateFile(this.#statePath, temporaryPath, bytes);
  }

  #restore(snapshot: Uint8Array): void {
    this.#database.close();
    this.#database = new this.#SQL.Database(snapshot);
    this.#database.run("PRAGMA foreign_keys = ON");
  }
}

export { CURRENT_SCHEMA_VERSION };
