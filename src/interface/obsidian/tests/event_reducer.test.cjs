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

function event(sequence, type, payload, overrides = {}) {
    return {
        protocolVersion: "1.0",
        schemaVersion: "1",
        eventId: `evt_${String(sequence).padStart(26, "0")}`,
        sequence,
        timestamp: "2026-07-13T02:00:00+00:00",
        traceId: "trc_01J00000000000000000000000",
        workspaceId: WORKSPACE,
        sessionId: "ses_01J00000000000000000000000",
        turnId: "trn_01J00000000000000000000000",
        runId: "run_01J00000000000000000000000",
        rootRunId: "run_01J00000000000000000000000",
        parentRunId: null,
        type,
        payload,
        ...overrides,
    };
}

test("reducer buffers gaps, applies in order, and ignores exact event duplicates", () => {
    const { EventReducer } = loadModule();
    const gaps = [];
    const reducer = new EventReducer(WORKSPACE, { onGap: (key, after) => gaps.push([key, after]) });
    const second = event(2, "assistant.delta", { blockIndex: 0, offset: 3, delta: "lo" });
    const first = event(1, "assistant.delta", { blockIndex: 0, offset: 0, delta: "hel" });

    assert.equal(reducer.accept(second), false);
    assert.equal(reducer.pendingCount("run:run_01J00000000000000000000000"), 1);
    assert.deepEqual(gaps, [["run:run_01J00000000000000000000000", 0]]);
    assert.equal(reducer.accept(first), true);
    assert.equal(reducer.pendingCount(), 0);
    assert.equal(reducer.state.runs.get(first.runId).assistantBlocks[0], "hello");
    assert.equal(reducer.accept(first), false);
});

test("Session replay cursors expose only the reducer-applied head of each Session Run", () => {
    const { EventReducer } = loadModule();
    const reducer = new EventReducer(WORKSPACE);
    reducer.accept(event(1, "turn.started", { input: [{ type: "text", text: "hello" }] }));
    reducer.accept(event(2, "assistant.completed", { content: [{ type: "text", text: "world" }] }));

    assert.equal(reducer.runLastSequence("run_01J00000000000000000000000"), 2);
    assert.deepEqual(reducer.sessionRunCursors("ses_01J00000000000000000000000"), {
        run_01J00000000000000000000000: 2,
    });
    assert.deepEqual(reducer.sessionRunCursors("ses_unrelated"), {});
});

test("event types absent from the generated protocol fail closed", () => {
    const { EventReducer } = loadModule();
    const reducer = new EventReducer(WORKSPACE);
    assert.throws(
        () => reducer.accept(event(1, "adapter.invented_terminal", {})),
        /absent from the generated protocol/,
    );
});

test("phase changes never invent a terminal status", () => {
    const { EventReducer } = loadModule();
    const reducer = new EventReducer(WORKSPACE);
    reducer.accept(event(1, "phase.changed", { previousPhase: "planning", phase: "completing", reason: null }));
    const run = reducer.state.runs.get("run_01J00000000000000000000000");
    assert.equal(run.status, "running");
    assert.equal(run.phase, "completing");
    reducer.accept(event(2, "turn.completed", { reason: "completed", completedAt: "2026-07-13T02:00:01+00:00" }));
    assert.equal(run.status, "completed");
});

test("tool and approval cards derive only from typed semantic events", () => {
    const { EventReducer } = loadModule();
    const reducer = new EventReducer(WORKSPACE);
    const call = {
        toolCallId: "tool_01J00000000000000000000000",
        name: "vault.patch",
        arguments: { path: "notes/a.md" },
    };
    reducer.accept(event(1, "tool.queued", { call, ordinal: 0 }));
    reducer.accept(event(2, "tool.started", { call, attempt: 1 }));
    reducer.accept(event(3, "tool.progress", {
        toolCallId: call.toolCallId,
        message: "applying",
        completedUnits: 1,
        totalUnits: 2,
        artifact: null,
    }));
    reducer.accept(event(4, "approval.required", {
        approval: { approvalId: "apr_01J00000000000000000000000", argsHash: `sha256:${"a".repeat(64)}` },
        explanation: "Review diff",
        diffArtifactIds: ["art_01J00000000000000000000000"],
    }));
    reducer.accept(event(5, "approval.resolved", {
        approvalId: "apr_01J00000000000000000000000",
        decision: "approve",
        scope: "once",
        resolvedAt: "2026-07-13T02:00:00+00:00",
        resolvedBy: "user",
        status: "approved",
        resolverId: "obsidian",
        includeDescendants: false,
        reason: null,
    }));
    reducer.accept(event(6, "tool.completed", {
        result: { toolCallId: call.toolCallId, status: "succeeded", output: { changed: true } },
        artifactIds: ["art_01J00000000000000000000000"],
        sourceReferenceIds: [],
        sideEffectFacts: [{ kind: "vault_write", state: "committed", resourceId: "notes/a.md" }],
    }));

    const run = reducer.state.runs.get("run_01J00000000000000000000000");
    const tool = run.tools.get(call.toolCallId);
    assert.equal(tool.status, "succeeded");
    assert.equal(tool.progressMessage, "applying");
    assert.deepEqual(tool.artifactIds, ["art_01J00000000000000000000000"]);
    const approval = run.approvals.get("apr_01J00000000000000000000000");
    assert.equal(approval.status, "approved");
    assert.equal(approval.scope, "once");
});

test("terminal tool sourceRefs are the replayable citation authority", () => {
    const { EventReducer } = loadModule();
    const reducer = new EventReducer(WORKSPACE);
    const call = {
        toolCallId: "tool_01J00000000000000000000001",
        name: "grep",
        arguments: { pattern: "Agent" },
    };
    reducer.accept(event(1, "tool.queued", { call, ordinal: 0 }));
    reducer.accept(event(2, "tool.completed", {
        result: {
            toolCallId: call.toolCallId,
            status: "succeeded",
            sourceRefs: [{
                type: "vault",
                file: {
                    workspaceId: WORKSPACE,
                    path: "notes/Agent.md",
                    lineStart: 3,
                    lineEnd: 5,
                },
                freshness: "stale",
                label: "旧索引",
            }],
        },
        artifactIds: [],
        sourceReferenceIds: ["vault:opaque-provenance"],
        sideEffectFacts: [],
    }));

    const run = reducer.state.runs.get("run_01J00000000000000000000000");
    assert.equal(run.references.length, 1);
    assert.equal(run.references[0].file.path, "notes/Agent.md");
    assert.equal(run.references[0].file.lineStart, 3);

    reducer.accept(event(3, "references.updated", {
        references: [{
            type: "vault",
            file: {
                workspaceId: WORKSPACE,
                path: "notes/Agent.md",
                lineStart: 3,
                lineEnd: 5,
            },
            freshness: "fresh",
            label: "新索引",
        }],
        replace: false,
    }));
    assert.equal(run.references.length, 1);
    assert.equal(run.references[0].label, "新索引");
    assert.equal(run.references[0].freshness, "fresh");
});

test("flat or model-invented Vault reference fields fail closed", () => {
    const { EventReducer } = loadModule();
    const reducer = new EventReducer(WORKSPACE);
    const call = {
        toolCallId: "tool_01J00000000000000000000002",
        name: "vault.read",
        arguments: { path: "notes/Agent.md" },
    };
    reducer.accept(event(1, "tool.queued", { call, ordinal: 0 }));
    assert.throws(() => reducer.accept(event(2, "tool.completed", {
        result: {
            toolCallId: call.toolCallId,
            status: "succeeded",
            sourceRefs: [{ type: "vault", path: "notes/Agent.md", sourceId: "invented" }],
        },
        artifactIds: [],
        sourceReferenceIds: [],
        sideEffectFacts: [],
    })), /Vault file reference must be an object/);
});

test("same sequence with a different event id is corruption", () => {
    const { EventReducer, EventProjectionError } = loadModule();
    const reducer = new EventReducer(WORKSPACE);
    reducer.accept(event(1, "reasoning.summary", { summary: "a", partial: false }));
    assert.throws(
        () => reducer.accept(event(1, "reasoning.summary", { summary: "b", partial: false }, {
            eventId: "evt_99999999999999999999999999",
        })),
        EventProjectionError,
    );
});

test("non-contiguous text offsets fail closed", () => {
    const { EventReducer, EventProjectionError } = loadModule();
    const reducer = new EventReducer(WORKSPACE);
    reducer.accept(event(1, "assistant.delta", { blockIndex: 0, offset: 0, delta: "hello" }));
    assert.throws(
        () => reducer.accept(event(2, "assistant.delta", { blockIndex: 0, offset: 3, delta: "bad" })),
        EventProjectionError,
    );
});
