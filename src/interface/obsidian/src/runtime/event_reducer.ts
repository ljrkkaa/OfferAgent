import { PROTOCOL_EVENT_TYPES } from "./generated_protocol";
import type { EventEnvelope as GeneratedEventEnvelope, SourceRef } from "./generated_protocol";
import { JsonObject, JsonValue, requireJsonObject, requireJsonValue } from "./json_rpc";
import { mergeSourceReferences, sourceReferenceArray } from "./source_references";

type NullableLineageField = "sessionId" | "turnId" | "runId" | "rootRunId" | "parentRunId";
export type EventEnvelope = Omit<GeneratedEventEnvelope, "payload" | NullableLineageField> & {
    readonly payload: JsonObject;
    readonly sessionId: string | null;
    readonly turnId: string | null;
    readonly runId: string | null;
    readonly rootRunId: string | null;
    readonly parentRunId: string | null;
};

const EVENT_TYPES: ReadonlySet<string> = new Set(PROTOCOL_EVENT_TYPES);

interface TimelineItemBase {
    readonly itemId: string;
    readonly sequence: number;
}

export interface UserMessageTimelineItem extends TimelineItemBase {
    readonly kind: "user_message";
    readonly source: "turn" | "steer";
    blocks: string[];
}

export interface ReasoningTimelineItem extends TimelineItemBase {
    readonly kind: "reasoning";
    summary: string;
    partial: boolean;
}

export interface AssistantMessageTimelineItem extends TimelineItemBase {
    readonly kind: "assistant_message";
    blocks: string[];
    completed: boolean;
}

export interface ToolCallTimelineItem extends TimelineItemBase {
    readonly kind: "tool_call";
    readonly toolCallId: string;
    name: string;
    version: string;
    status: string;
    arguments: JsonObject;
    artifactIds: string[];
    sourceReferenceIds: string[];
    sideEffects: JsonObject[];
    result: JsonObject | null;
    error: JsonObject | null;
}

export interface ApprovalTimelineItem extends TimelineItemBase {
    readonly kind: "approval";
    readonly approvalId: string;
    status: string;
    explanation: string;
    expectedArgsHash: string | null;
    diffArtifactIds: string[];
    scope: string | null;
}

export interface SubagentTimelineItem extends TimelineItemBase {
    readonly kind: "subagent";
    readonly childRunId: string;
    agentName: string;
    task: string;
    depth: number;
    status: string;
    message: string | null;
    summary: string | null;
}

export type TimelineItem =
    | UserMessageTimelineItem
    | ReasoningTimelineItem
    | AssistantMessageTimelineItem
    | ToolCallTimelineItem
    | ApprovalTimelineItem
    | SubagentTimelineItem;

export interface RunViewState {
    readonly runId: string;
    rootRunId: string;
    parentRunId: string | null;
    sessionId: string;
    turnId: string;
    status: string;
    phase: string | null;
    timeline: TimelineItem[];
    references: SourceRef[];
    artifacts: Map<string, JsonObject>;
    usage: JsonObject | null;
    childRunIds: Set<string>;
    warnings: JsonObject[];
    termination: JsonObject | null;
}

export interface SessionViewState {
    readonly sessionId: string;
    summary: JsonObject | null;
    runIds: Set<string>;
}

export interface ProjectionState {
    readonly workspaceId: string;
    readonly sessions: Map<string, SessionViewState>;
    readonly runs: Map<string, RunViewState>;
    readonly workspaceWarnings: JsonObject[];
}

interface EventStream {
    lastSequence: number;
    pending: Map<number, EventEnvelope>;
    appliedBySequence: Map<number, string>;
}

export interface EventReducerOptions {
    maxPendingPerStream?: number;
    maxSeenEventIds?: number;
    onGap?: (streamKey: string, afterSequence: number) => void;
}

export class EventProjectionError extends Error {
    constructor(message: string) {
        super(message);
        this.name = "EventProjectionError";
    }
}

export class EventReducer {
    readonly state: ProjectionState;
    private readonly streams = new Map<string, EventStream>();
    private readonly seenIds = new Map<string, true>();
    private readonly maxPendingPerStream: number;
    private readonly maxSeenEventIds: number;
    private readonly onGap: ((streamKey: string, afterSequence: number) => void) | undefined;
    private readonly listeners = new Set<(event: EventEnvelope, state: ProjectionState) => void>();

    constructor(workspaceId: string, options: EventReducerOptions = {}) {
        requireIdentifier(workspaceId, "workspaceId");
        this.maxPendingPerStream = positiveInteger(options.maxPendingPerStream ?? 2_048, "maxPendingPerStream");
        this.maxSeenEventIds = positiveInteger(options.maxSeenEventIds ?? 100_000, "maxSeenEventIds");
        this.onGap = options.onGap;
        this.state = {
            workspaceId,
            sessions: new Map(),
            runs: new Map(),
            workspaceWarnings: [],
        };
    }

    accept(raw: unknown): boolean {
        const event = parseEventEnvelope(raw);
        if (event.workspaceId !== this.state.workspaceId) {
            throw new EventProjectionError("event belongs to a different Workspace");
        }
        if (this.seenIds.has(event.eventId)) return false;
        const key = streamKey(event);
        const stream = this.streams.get(key) ?? {
            lastSequence: 0,
            pending: new Map<number, EventEnvelope>(),
            appliedBySequence: new Map<number, string>(),
        };
        this.streams.set(key, stream);
        const appliedId = stream.appliedBySequence.get(event.sequence);
        if (appliedId !== undefined) {
            if (appliedId !== event.eventId) throw new EventProjectionError("event sequence was reused with another id");
            this.rememberId(event.eventId);
            return false;
        }
        const pending = stream.pending.get(event.sequence);
        if (pending !== undefined) {
            if (pending.eventId !== event.eventId) throw new EventProjectionError("pending event sequence collision");
            this.rememberId(event.eventId);
            return false;
        }
        if (event.sequence <= stream.lastSequence) throw new EventProjectionError("event sequence regressed");
        if (stream.pending.size >= this.maxPendingPerStream) {
            throw new EventProjectionError("out-of-order event buffer exceeded its hard limit");
        }
        stream.pending.set(event.sequence, event);
        this.rememberId(event.eventId);
        const applied = this.drain(key, stream);
        if (stream.pending.size > 0 && !stream.pending.has(stream.lastSequence + 1)) {
            this.onGap?.(key, stream.lastSequence);
        }
        return applied;
    }

    lastSequence(streamKeyValue: string): number {
        return this.streams.get(streamKeyValue)?.lastSequence ?? 0;
    }

    runLastSequence(runId: string): number {
        requireIdentifier(runId, "runId");
        return this.lastSequence(`run:${runId}`);
    }

    sessionRunCursors(sessionId: string): Readonly<Record<string, number>> {
        requireIdentifier(sessionId, "sessionId");
        return Object.fromEntries(
            [...this.state.runs.values()]
                .filter((run) => run.sessionId === sessionId)
                .map((run) => [run.runId, this.runLastSequence(run.runId)] as const)
                .sort(([left], [right]) => left < right ? -1 : left > right ? 1 : 0),
        );
    }

    pendingCount(streamKeyValue?: string): number {
        if (streamKeyValue !== undefined) return this.streams.get(streamKeyValue)?.pending.size ?? 0;
        let total = 0;
        for (const stream of this.streams.values()) total += stream.pending.size;
        return total;
    }

    subscribe(listener: (event: EventEnvelope, state: ProjectionState) => void): () => void {
        this.listeners.add(listener);
        return () => this.listeners.delete(listener);
    }

    private drain(key: string, stream: EventStream): boolean {
        let applied = false;
        while (true) {
            const sequence = stream.lastSequence + 1;
            const event = stream.pending.get(sequence);
            if (!event) break;
            stream.pending.delete(sequence);
            this.apply(event);
            stream.lastSequence = sequence;
            stream.appliedBySequence.set(sequence, event.eventId);
            for (const listener of this.listeners) listener(event, this.state);
            applied = true;
        }
        if (stream.appliedBySequence.size > this.maxSeenEventIds) {
            const cutoff = stream.lastSequence - this.maxSeenEventIds;
            for (const sequence of stream.appliedBySequence.keys()) {
                if (sequence <= cutoff) stream.appliedBySequence.delete(sequence);
            }
        }
        return applied;
    }

    private apply(event: EventEnvelope): void {
        const payload = event.payload;
        if (event.type === "session.updated") {
            const summary = objectField(payload, "session");
            const sessionId = textField(summary, "sessionId");
            const session = this.state.sessions.get(sessionId) ?? { sessionId, summary: null, runIds: new Set<string>() };
            session.summary = summary;
            this.state.sessions.set(sessionId, session);
            return;
        }
        if (event.type === "runtime.warning" && event.runId === null) {
            this.state.workspaceWarnings.push(payload);
            return;
        }
        if (event.runId === null || event.sessionId === null || event.turnId === null || event.rootRunId === null) {
            throw new EventProjectionError(`run event ${event.type} lacks lineage`);
        }
        const run = this.ensureRun(event);
        this.applyRun(run, event);
    }

    private ensureRun(event: EventEnvelope): RunViewState {
        const runId = event.runId as string;
        let run = this.state.runs.get(runId);
        if (run) {
            if (run.rootRunId !== event.rootRunId || run.parentRunId !== event.parentRunId ||
                run.sessionId !== event.sessionId || run.turnId !== event.turnId) {
                throw new EventProjectionError("Run lineage changed across events");
            }
            return run;
        }
        run = {
            runId,
            rootRunId: event.rootRunId as string,
            parentRunId: event.parentRunId,
            sessionId: event.sessionId as string,
            turnId: event.turnId as string,
            status: "running",
            phase: null,
            timeline: [],
            references: [],
            artifacts: new Map(),
            usage: null,
            childRunIds: new Set(),
            warnings: [],
            termination: null,
        };
        this.state.runs.set(runId, run);
        const session = this.state.sessions.get(run.sessionId) ?? {
            sessionId: run.sessionId,
            summary: null,
            runIds: new Set<string>(),
        };
        session.runIds.add(runId);
        this.state.sessions.set(run.sessionId, session);
        if (run.parentRunId) this.state.runs.get(run.parentRunId)?.childRunIds.add(runId);
        return run;
    }

    private applyRun(run: RunViewState, event: EventEnvelope): void {
        const { type, payload } = event;
        switch (type) {
            case "turn.started":
                appendTimelineItem(run, {
                    kind: "user_message",
                    itemId: `turn:${run.turnId}`,
                    sequence: event.sequence,
                    source: "turn",
                    blocks: contentText(arrayField(payload, "input")),
                });
                return;
            case "turn.steered":
                appendTimelineItem(run, {
                    kind: "user_message",
                    itemId: `steer:${textField(payload, "messageId")}`,
                    sequence: event.sequence,
                    source: "steer",
                    blocks: contentText(arrayField(payload, "input")),
                });
                return;
            case "phase.changed":
                run.phase = textField(payload, "phase");
                return;
            case "reasoning.summary": {
                const summary = textField(payload, "summary");
                const partial = payload.partial === true;
                const existing = run.timeline.find(
                    (item): item is ReasoningTimelineItem => item.kind === "reasoning",
                );
                if (!existing) {
                    appendTimelineItem(run, {
                        kind: "reasoning",
                        itemId: `reasoning:${event.sequence}`,
                        sequence: event.sequence,
                        summary,
                        partial,
                    });
                } else if (partial) {
                    if (!existing.partial) throw new EventProjectionError("reasoning summary resumed after completion");
                    existing.summary += summary;
                } else {
                    if (!existing.partial || existing.summary !== summary) {
                        throw new EventProjectionError("reasoning summary final snapshot does not match deltas");
                    }
                    existing.partial = false;
                }
                return;
            }
            case "assistant.delta": {
                const index = integerField(payload, "blockIndex", 0);
                const offset = integerField(payload, "offset", 0);
                const assistant = run.timeline.find(
                    (item): item is AssistantMessageTimelineItem => item.kind === "assistant_message",
                ) ?? appendTimelineItem(run, {
                    kind: "assistant_message",
                    itemId: `assistant:${event.sequence}`,
                    sequence: event.sequence,
                    blocks: [],
                    completed: false,
                });
                if (assistant.completed) throw new EventProjectionError("assistant delta arrived after completion");
                while (assistant.blocks.length <= index) assistant.blocks.push("");
                if (assistant.blocks[index].length !== offset) {
                    throw new EventProjectionError("assistant delta offset is non-contiguous");
                }
                assistant.blocks[index] += textField(payload, "delta");
                return;
            }
            case "assistant.completed": {
                const assistant = run.timeline.find(
                    (item): item is AssistantMessageTimelineItem => item.kind === "assistant_message",
                ) ?? appendTimelineItem(run, {
                    kind: "assistant_message",
                    itemId: `assistant:${event.sequence}`,
                    sequence: event.sequence,
                    blocks: [],
                    completed: false,
                });
                if (assistant.completed) throw new EventProjectionError("assistant completed more than once");
                assistant.blocks = contentText(arrayField(payload, "content"));
                assistant.completed = true;
                return;
            }
            case "tool.calls.accepted": {
                for (const call of arrayField(payload, "calls")) {
                    const descriptor = objectValue(call);
                    const toolCallId = textField(descriptor, "toolCallId");
                    appendTimelineItem(run, {
                        kind: "tool_call",
                        itemId: `tool:${toolCallId}`,
                        sequence: event.sequence,
                        toolCallId,
                        name: textField(descriptor, "name"),
                        version: textField(descriptor, "version"),
                        status: "accepted",
                        arguments: objectField(descriptor, "arguments"),
                        artifactIds: [],
                        sourceReferenceIds: [],
                        sideEffects: [],
                        result: null,
                        error: null,
                    });
                }
                return;
            }
            case "tool.started": {
                const call = objectField(payload, "call");
                const tool = requireTool(run, textField(call, "toolCallId"));
                const name = textField(call, "name");
                const version = textField(call, "version");
                if (tool.name !== name || tool.version !== version) {
                    throw new EventProjectionError("tool identity changed after acceptance");
                }
                tool.arguments = objectField(call, "arguments");
                tool.status = "running";
                return;
            }
            case "tool.completed":
            case "tool.failed": {
                const result = objectField(payload, "result");
                const tool = requireTool(run, textField(result, "toolCallId"));
                tool.status = textField(result, "status");
                tool.result = result;
                tool.error = result.error && typeof result.error === "object" && !Array.isArray(result.error)
                    ? objectField(result, "error")
                    : null;
                tool.artifactIds = textArray(payload, "artifactIds");
                tool.sourceReferenceIds = textArray(payload, "sourceReferenceIds");
                tool.sideEffects = objectArray(payload, "sideEffectFacts");
                run.references = mergeSourceReferences(
                    run.references,
                    sourceReferenceArray(result.sourceRefs),
                );
                return;
            }
            case "approval.required": {
                const approval = objectField(payload, "approval");
                const approvalId = textField(approval, "approvalId");
                appendTimelineItem(run, {
                    kind: "approval",
                    itemId: `approval:${approvalId}`,
                    sequence: event.sequence,
                    approvalId,
                    status: "pending",
                    explanation: textField(payload, "explanation"),
                    expectedArgsHash: textField(objectField(approval, "toolCall"), "argsHash"),
                    diffArtifactIds: textArray(payload, "diffArtifactIds"),
                    scope: null,
                });
                return;
            }
            case "approval.resolved":
            case "approval.expired": {
                const approval = requireApproval(run, textField(payload, "approvalId"));
                approval.status = optionalText(payload, "status") ?? "expired";
                approval.scope = optionalText(payload, "scope");
                return;
            }
            case "subagent.queued": {
                const childRunId = textField(payload, "childRunId");
                appendTimelineItem(run, {
                    kind: "subagent",
                    itemId: `subagent:${childRunId}`,
                    sequence: event.sequence,
                    childRunId,
                    agentName: textField(payload, "agentName"),
                    task: textField(payload, "task"),
                    depth: integerField(payload, "depth", 1),
                    status: "queued",
                    message: null,
                    summary: null,
                });
                return;
            }
            case "subagent.started": {
                const subagent = requireSubagent(run, textField(payload, "childRunId"));
                subagent.agentName = textField(payload, "agentName");
                subagent.status = "started";
                return;
            }
            case "subagent.progress": {
                const subagent = requireSubagent(run, textField(payload, "childRunId"));
                subagent.status = "running";
                subagent.message = textField(payload, "message");
                return;
            }
            case "subagent.waiting": {
                const subagent = requireSubagent(run, textField(payload, "childRunId"));
                subagent.status = "waiting";
                subagent.message = textField(payload, "reason");
                return;
            }
            case "subagent.result_available": {
                const subagent = requireSubagent(run, textField(payload, "childRunId"));
                subagent.status = "result_available";
                subagent.summary = textField(payload, "summary");
                return;
            }
            case "subagent.completed": {
                const result = objectField(payload, "result");
                const subagent = requireSubagent(run, textField(result, "runId"));
                subagent.status = "completed";
                subagent.summary = optionalText(result, "summary");
                return;
            }
            case "subagent.failed": {
                const subagent = requireSubagent(run, textField(payload, "childRunId"));
                subagent.status = "failed";
                return;
            }
            case "subagent.cancelled": {
                const subagent = requireSubagent(run, textField(payload, "childRunId"));
                subagent.status = "cancelled";
                return;
            }
            case "subagent.interrupted":
            case "subagent.orphaned": {
                const subagent = requireSubagent(run, textField(payload, "childRunId"));
                subagent.status = type.endsWith("orphaned") ? "orphaned" : "interrupted";
                return;
            }
            case "subagent.recovered": {
                const subagent = requireSubagent(run, textField(payload, "childRunId"));
                subagent.status = "recovered";
                return;
            }
            case "references.updated": {
                const incoming = sourceReferenceArray(payload.references);
                run.references = payload.replace === true
                    ? incoming
                    : mergeSourceReferences(run.references, incoming);
                return;
            }
            case "artifact.created": {
                const artifact = objectField(payload, "artifact");
                run.artifacts.set(textField(artifact, "artifactId"), artifact);
                return;
            }
            case "usage.updated":
                run.usage = objectField(payload, "usage");
                return;
            case "runtime.warning":
                run.warnings.push(payload);
                return;
            case "turn.completed":
                terminal(run, "completed", payload);
                return;
            case "turn.cancelled":
                terminal(run, "cancelled", payload);
                return;
            case "turn.failed":
                terminal(run, "failed", payload);
                return;
            case "turn.interrupted":
                terminal(run, "interrupted", payload);
                return;
            default:
                // Audit-only events remain durable but do not create visual items.
                return;
        }
    }

    private rememberId(eventId: string): void {
        this.seenIds.delete(eventId);
        this.seenIds.set(eventId, true);
        while (this.seenIds.size > this.maxSeenEventIds) {
            const oldest = this.seenIds.keys().next().value as string | undefined;
            if (oldest === undefined) break;
            this.seenIds.delete(oldest);
        }
    }
}

export function parseEventEnvelope(raw: unknown): EventEnvelope {
    const value = requireJsonObject(raw);
    const keys = [
        "protocolVersion", "schemaVersion", "eventId", "sequence", "timestamp", "traceId", "workspaceId",
        "sessionId", "turnId", "runId", "rootRunId", "parentRunId", "type", "payload",
    ];
    if (Object.keys(value).length !== keys.length || keys.some((key) => !(key in value))) {
        throw new EventProjectionError("event envelope fields do not match the protocol");
    }
    const event: EventEnvelope = {
        protocolVersion: textField(value, "protocolVersion"),
        schemaVersion: textField(value, "schemaVersion"),
        eventId: textField(value, "eventId"),
        sequence: integerField(value, "sequence", 1),
        timestamp: timestampField(value, "timestamp"),
        traceId: textField(value, "traceId"),
        workspaceId: textField(value, "workspaceId"),
        sessionId: nullableText(value, "sessionId"),
        turnId: nullableText(value, "turnId"),
        runId: nullableText(value, "runId"),
        rootRunId: nullableText(value, "rootRunId"),
        parentRunId: nullableText(value, "parentRunId"),
        type: eventTypeField(value),
        payload: objectField(value, "payload"),
    };
    if (event.runId !== null && (event.sessionId === null || event.turnId === null || event.rootRunId === null)) {
        throw new EventProjectionError("run event lineage is incomplete");
    }
    if (event.parentRunId !== null && event.runId === null) throw new EventProjectionError("parentRunId requires runId");
    return event;
}

function eventTypeField(value: JsonObject): EventEnvelope["type"] {
    const type = textField(value, "type");
    if (!EVENT_TYPES.has(type)) throw new EventProjectionError("event type is absent from the generated protocol");
    return type as EventEnvelope["type"];
}

export function eventStreamKey(event: EventEnvelope): string {
    return streamKey(event);
}

function streamKey(event: EventEnvelope): string {
    return event.runId ? `run:${event.runId}` : event.sessionId ? `session:${event.sessionId}` : `workspace:${event.workspaceId}`;
}

function terminal(run: RunViewState, status: string, payload: JsonObject): void {
    if (["completed", "cancelled", "failed", "interrupted", "orphaned"].includes(run.status)) {
        if (run.status !== status) throw new EventProjectionError("Run terminal status changed");
        return;
    }
    run.status = status;
    run.termination = payload;
}

function appendTimelineItem<T extends TimelineItem>(run: RunViewState, item: T): T {
    if (run.timeline.some((existing) => existing.itemId === item.itemId)) {
        throw new EventProjectionError(`duplicate timeline item ${item.itemId}`);
    }
    run.timeline.push(item);
    return item;
}

function requireTool(run: RunViewState, toolCallId: string): ToolCallTimelineItem {
    const tool = run.timeline.find(
        (item): item is ToolCallTimelineItem => item.kind === "tool_call" && item.toolCallId === toolCallId,
    );
    if (!tool) throw new EventProjectionError("tool event has no accepted-call projection");
    return tool;
}

function requireApproval(run: RunViewState, approvalId: string): ApprovalTimelineItem {
    const approval = run.timeline.find(
        (item): item is ApprovalTimelineItem => item.kind === "approval" && item.approvalId === approvalId,
    );
    if (!approval) throw new EventProjectionError("approval resolution has no required projection");
    return approval;
}

function requireSubagent(run: RunViewState, childRunId: string): SubagentTimelineItem {
    const subagent = run.timeline.find(
        (item): item is SubagentTimelineItem => item.kind === "subagent" && item.childRunId === childRunId,
    );
    if (!subagent) throw new EventProjectionError("subagent event has no queued projection");
    return subagent;
}

function contentText(content: JsonValue[]): string[] {
    return content.map((raw) => {
        const block = objectValue(raw);
        const type = textField(block, "type");
        if (type === "text") return textField(block, "text");
        if (type === "file") return `[File: ${textField(objectField(block, "file"), "path")}]`;
        if (type === "document") {
            const file = objectField(block, "file");
            return `[Document: ${textField(file, "path")} (${textField(block, "mediaType")})]`;
        }
        if (type === "artifact") {
            return `[Artifact: ${textField(objectField(block, "artifact"), "artifactId")}]`;
        }
        if (type === "image") return `[Image: ${optionalText(block, "altText") ?? "image"}]`;
        return `[${type}]`;
    });
}

function textField(value: JsonObject, key: string): string {
    const field = value[key];
    if (typeof field !== "string" || field.length === 0) throw new EventProjectionError(`${key} must be non-empty text`);
    return field;
}

function optionalText(value: JsonObject, key: string): string | null {
    const field = value[key];
    if (field === null || field === undefined) return null;
    return textField(value, key);
}

function nullableText(value: JsonObject, key: string): string | null {
    if (!(key in value) || value[key] === null) return null;
    return textField(value, key);
}

function integerField(value: JsonObject, key: string, minimum: number): number {
    const field = value[key];
    if (typeof field !== "number" || !Number.isSafeInteger(field) || field < minimum) {
        throw new EventProjectionError(`${key} must be an integer >= ${minimum}`);
    }
    return field;
}

function optionalInteger(value: JsonObject, key: string): number | null {
    const field = value[key];
    if (field === null || field === undefined) return null;
    return integerField(value, key, 0);
}

function objectField(value: JsonObject, key: string): JsonObject {
    const field = value[key];
    if (field === null || typeof field !== "object" || Array.isArray(field)) {
        throw new EventProjectionError(`${key} must be an object`);
    }
    return field;
}

function objectValue(value: JsonValue): JsonObject {
    if (value === null || typeof value !== "object" || Array.isArray(value)) {
        throw new EventProjectionError("expected an object");
    }
    return value;
}

function arrayField(value: JsonObject, key: string): JsonValue[] {
    const field = value[key];
    if (!Array.isArray(field)) throw new EventProjectionError(`${key} must be an array`);
    return field;
}

function textArray(value: JsonObject, key: string): string[] {
    return arrayField(value, key).map((item) => {
        if (typeof item !== "string" || item.length === 0) throw new EventProjectionError(`${key} has invalid text`);
        return item;
    });
}

function objectArray(value: JsonObject, key: string): JsonObject[] {
    return arrayField(value, key).map(objectValue);
}

function timestampField(value: JsonObject, key: string): string {
    const field = textField(value, key);
    if (!Number.isFinite(Date.parse(field)) || !/(?:Z|[+-]\d{2}:\d{2})$/.test(field)) {
        throw new EventProjectionError(`${key} must be an RFC3339 timestamp`);
    }
    return field;
}

function requireIdentifier(value: string, name: string): void {
    if (!value || value.length > 256 || /[\0\r\n]/.test(value)) throw new TypeError(`invalid ${name}`);
}

function positiveInteger(value: number, name: string): number {
    if (!Number.isSafeInteger(value) || value < 1) throw new RangeError(`${name} must be a positive integer`);
    return value;
}

void requireJsonValue;
