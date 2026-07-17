const assert = require("node:assert/strict");
const path = require("node:path");
const test = require("node:test");
const { buildSync } = require("esbuild");

function loadModule() {
    const output = buildSync({
        entryPoints: [path.join(__dirname, "../src/runtime/event_reducer.ts")],
        bundle: true,
        format: "cjs",
        platform: "node",
        target: "node16",
        write: false,
    }).outputFiles[0].text;
    const compiled = { exports: {} };
    new Function("require", "module", "exports", output)(require, compiled, compiled.exports);
    return compiled.exports;
}

const WORKSPACE = "wsi_01J00000000000000000000000";
const SESSION = "ses_01J00000000000000000000000";
const TURN = "trn_01J00000000000000000000000";
const RUN = "run_01J00000000000000000000000";
const SHA = `sha256:${"a".repeat(64)}`;

function event(sequence, type, payload, overrides = {}) {
    return {
        protocolVersion: "1.0",
        schemaVersion: "1",
        eventId: `evt_${String(sequence).padStart(26, "0")}`,
        sequence,
        timestamp: "2026-07-13T02:00:00+00:00",
        traceId: "trc_01J00000000000000000000000",
        workspaceId: WORKSPACE,
        sessionId: SESSION,
        turnId: TURN,
        runId: RUN,
        rootRunId: RUN,
        parentRunId: null,
        type,
        payload,
        ...overrides,
    };
}

function persistedCall(overrides = {}) {
    return {
        toolCallId: "tool_01J00000000000000000000000",
        name: "vault.patch",
        version: "1",
        arguments: { path: "notes/a.md" },
        argsHash: SHA,
        definitionFingerprint: SHA,
        idempotencyKey: "idem_01J00000000000000000000000",
        deadline: null,
        lineage: {
            rootRunId: RUN,
            runId: RUN,
            parentRunId: null,
            ancestorRunIds: [],
            depth: 0,
            agentName: "primary",
        },
        ...overrides,
    };
}

function callDescriptor(call = persistedCall()) {
    return {
        toolCallId: call.toolCallId,
        name: call.name,
        version: call.version,
        arguments: call.arguments,
        argsHash: call.argsHash,
        idempotencyKey: call.idempotencyKey,
        risk: "write",
        agentLineage: [RUN],
        reason: null,
    };
}

function item(run, kind) {
    return run.timeline.find((candidate) => candidate.kind === kind);
}

test("reducer buffers gaps, applies in order, and ignores exact event duplicates", () => {
    const { EventReducer } = loadModule();
    const gaps = [];
    const reducer = new EventReducer(WORKSPACE, { onGap: (key, after) => gaps.push([key, after]) });
    const second = event(2, "assistant.delta", { blockIndex: 0, offset: 3, delta: "lo" });
    const first = event(1, "assistant.delta", { blockIndex: 0, offset: 0, delta: "hel" });

    assert.equal(reducer.accept(second), false);
    assert.equal(reducer.pendingCount(`run:${RUN}`), 1);
    assert.deepEqual(gaps, [[`run:${RUN}`, 0]]);
    assert.equal(reducer.accept(first), true);
    assert.equal(reducer.pendingCount(), 0);
    assert.equal(item(reducer.state.runs.get(RUN), "assistant_message").blocks[0], "hello");
    assert.equal(reducer.accept(first), false);
});

test("timeline preserves semantic item order and accepts only real tool lifecycle facts", () => {
    const { EventReducer } = loadModule();
    const reducer = new EventReducer(WORKSPACE);
    const call = persistedCall();
    reducer.accept(event(1, "turn.started", { input: [{ type: "text", text: "plan today" }] }));
    reducer.accept(event(2, "reasoning.summary", { summary: "检查", partial: true }));
    reducer.accept(event(3, "reasoning.summary", { summary: "本地文件", partial: true }));
    reducer.accept(event(4, "reasoning.summary", { summary: "检查本地文件", partial: false }));
    reducer.accept(event(5, "tool.calls.accepted", { calls: [call] }));
    reducer.accept(event(6, "tool.started", { call: callDescriptor(call), attempt: 1 }));
    reducer.accept(event(7, "approval.required", {
        approval: {
            approvalId: "apr_01J00000000000000000000000",
            toolCall: callDescriptor(call),
        },
        explanation: "Review diff",
        diffArtifactIds: ["art_01J00000000000000000000000"],
    }));
    reducer.accept(event(8, "approval.resolved", {
        approvalId: "apr_01J00000000000000000000000",
        decision: "allow_once",
        scope: "once",
        resolvedAt: "2026-07-13T02:00:00+00:00",
        resolvedBy: "user",
        status: "approved",
        resolverId: "obsidian",
        includeDescendants: false,
        reason: null,
    }));
    reducer.accept(event(9, "tool.completed", {
        result: { toolCallId: call.toolCallId, status: "succeeded", summary: "patched" },
        artifactIds: ["art_01J00000000000000000000000"],
        sourceReferenceIds: [],
        sideEffectFacts: [{ kind: "vault_write", state: "committed", resourceId: "notes/a.md" }],
    }));
    reducer.accept(event(10, "assistant.completed", { content: [{ type: "text", text: "完成" }] }));

    const run = reducer.state.runs.get(RUN);
    assert.deepEqual(run.timeline.map((entry) => entry.kind), [
        "user_message", "reasoning", "tool_call", "approval", "assistant_message",
    ]);
    const reasoning = item(run, "reasoning");
    assert.equal(reasoning.summary, "检查本地文件");
    assert.equal(reasoning.partial, false);
    const tool = item(run, "tool_call");
    assert.equal(tool.status, "succeeded");
    assert.deepEqual(tool.artifactIds, ["art_01J00000000000000000000000"]);
    const approval = item(run, "approval");
    assert.equal(approval.status, "approved");
    assert.equal(approval.scope, "once");
});

test("turn history keeps nested image metadata and pins distinct from text and used evidence", () => {
    const { EventReducer } = loadModule();
    const reducer = new EventReducer(WORKSPACE);
    reducer.accept(event(1, "turn.started", { input: [
        { type: "text", text: "compare these" },
        {
            type: "image",
            artifact: {
                artifactId: "art_image",
                contentHash: SHA,
                mediaType: "image/png",
                sizeBytes: 42,
                sensitivity: "private",
                state: "complete",
                title: "offer.png",
            },
            altText: "offer screenshot",
        },
        {
            type: "pinnedContext",
            references: [{ kind: "selection", path: "notes/offer.md", lineStart: 3, lineEnd: 8 }],
        },
    ] }));

    const message = item(reducer.state.runs.get(RUN), "user_message");
    assert.deepEqual(message.blocks, ["compare these"]);
    assert.equal(message.images[0].artifact.artifactId, "art_image");
    assert.equal(message.images[0].altText, "offer screenshot");
    assert.deepEqual(message.pinnedContext, [
        { kind: "selection", path: "notes/offer.md", lineStart: 3, lineEnd: 8 },
    ]);
});

test("tool events without the accepted durable call are rejected", () => {
    const { EventReducer, EventProjectionError } = loadModule();
    const reducer = new EventReducer(WORKSPACE);
    assert.throws(
        () => reducer.accept(event(1, "tool.started", { call: callDescriptor(), attempt: 1 })),
        EventProjectionError,
    );
});

test("failed tool items expose the typed nested result error", () => {
    const { EventReducer } = loadModule();
    const reducer = new EventReducer(WORKSPACE);
    const call = persistedCall();
    reducer.accept(event(1, "tool.calls.accepted", { calls: [call] }));
    reducer.accept(event(2, "tool.failed", {
        result: {
            toolCallId: call.toolCallId,
            status: "failed",
            summary: "write failed",
            error: { code: "write_failed", userVisibleMessage: "write failed" },
        },
        artifactIds: [],
        sourceReferenceIds: [],
        sideEffectFacts: [],
    }));

    const tool = item(reducer.state.runs.get(RUN), "tool_call");
    assert.equal(tool.status, "failed");
    assert.equal(tool.error.code, "write_failed");
});

test("subagent lifecycle is an item in the parent timeline and never terminates the parent Run", () => {
    const { EventReducer } = loadModule();
    const reducer = new EventReducer(WORKSPACE);
    const child = "run_01J00000000000000000000001";
    reducer.accept(event(1, "subagent.queued", {
        childRunId: child,
        agentName: "researcher",
        task: "inspect notes",
        depth: 1,
    }));
    reducer.accept(event(2, "subagent.started", { childRunId: child, agentName: "researcher" }));
    reducer.accept(event(3, "subagent.result_available", {
        childRunId: child,
        resultArtifactId: "art_01J00000000000000000000001",
        summary: "found two notes",
    }));
    reducer.accept(event(4, "subagent.completed", {
        result: { runId: child, status: "completed", summary: "found two notes" },
    }));

    const run = reducer.state.runs.get(RUN);
    const subagent = item(run, "subagent");
    assert.equal(subagent.status, "completed");
    assert.equal(subagent.summary, "found two notes");
    assert.equal(run.status, "running");
});

test("terminal tool sourceRefs are the replayable citation authority", () => {
    const { EventReducer } = loadModule();
    const reducer = new EventReducer(WORKSPACE);
    const call = persistedCall({ name: "grep", arguments: { pattern: "Agent" } });
    reducer.accept(event(1, "tool.calls.accepted", { calls: [call] }));
    reducer.accept(event(2, "tool.completed", {
        result: {
            toolCallId: call.toolCallId,
            status: "succeeded",
            summary: "found",
            sourceRefs: [{
                type: "vault",
                file: { workspaceId: WORKSPACE, path: "notes/Agent.md", lineStart: 3, lineEnd: 5 },
                freshness: "stale",
                label: "previous read",
            }],
        },
        artifactIds: [],
        sourceReferenceIds: ["vault:opaque-provenance"],
        sideEffectFacts: [],
    }));

    const run = reducer.state.runs.get(RUN);
    assert.equal(run.references.length, 1);
    assert.equal(run.references[0].file.path, "notes/Agent.md");
    reducer.accept(event(3, "references.updated", {
        references: [{
            type: "vault",
            file: { workspaceId: WORKSPACE, path: "notes/Agent.md", lineStart: 3, lineEnd: 5 },
            freshness: "fresh",
            label: "current read",
        }],
        replace: false,
    }));
    assert.equal(run.references[0].label, "current read");
    assert.equal(run.references[0].freshness, "fresh");
});

test("event types absent from the generated protocol fail closed", () => {
    const { EventReducer } = loadModule();
    const reducer = new EventReducer(WORKSPACE);
    assert.throws(() => reducer.accept(event(1, "adapter.invented_terminal", {})), /absent from the generated protocol/);
});

test("same sequence with a different event id and non-contiguous text fail closed", () => {
    const { EventReducer, EventProjectionError } = loadModule();
    const reducer = new EventReducer(WORKSPACE);
    reducer.accept(event(1, "assistant.delta", { blockIndex: 0, offset: 0, delta: "hello" }));
    assert.throws(
        () => reducer.accept(event(1, "reasoning.summary", { summary: "different", partial: false }, {
            eventId: "evt_99999999999999999999999999",
        })),
        EventProjectionError,
    );
    assert.throws(
        () => reducer.accept(event(2, "assistant.delta", { blockIndex: 0, offset: 3, delta: "bad" })),
        EventProjectionError,
    );
});
