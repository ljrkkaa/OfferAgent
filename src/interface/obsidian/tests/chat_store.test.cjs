const assert = require("node:assert/strict");
const path = require("node:path");
const test = require("node:test");
const { buildSync } = require("esbuild");

function loadModule(entry) {
    const output = buildSync({
        entryPoints: [path.join(__dirname, `../src/runtime/${entry}`)],
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

function createClient(handler, options = {}) {
    const { EventReducer } = loadModule("event_reducer.ts");
    const reducer = new EventReducer(WORKSPACE);
    return {
        reducer,
        async request(method, params) {
            if (method === "session/get") {
                options.onHydrationCall?.(method, params);
                return options.sessionGet
                    ? await options.sessionGet(params.sessionId)
                    : emptySessionResult(params.sessionId);
            }
            return await handler(method, params);
        },
        async replaySession(sessionId, runCursors = {}) {
            options.onHydrationCall?.("events/replay", { sessionId, runCursors: structuredClone(runCursors) });
            const page = options.replaySession
                ? await options.replaySession(sessionId, runCursors)
                : { events: [], runCursors };
            for (const event of page.events ?? []) reducer.accept(event);
            return structuredClone(page.runCursors ?? runCursors);
        },
    };
}

function emptySessionResult(sessionId, title = "Persisted session") {
    return { session: { summary: { sessionId, title, activeRunId: null }, turns: [] } };
}

function memoryPersistence(initial = null) {
    return {
        value: initial,
        saves: 0,
        async load() { return this.value; },
        async save(value) { this.value = structuredClone(value); this.saves += 1; },
    };
}

const runConfig = {
    provider: "local",
    model: "qwen-test",
    reasoningEffort: "medium",
    permissionMode: "normal",
    enabledSkills: [],
};

const SESSION = "ses_01J10000000000000000000000";
const TURN = "turn_01J10000000000000000000000";
const RUN = "run_01J10000000000000000000000";

function runEvent(sequence, type, payload) {
    return {
        protocolVersion: "1.0",
        schemaVersion: "1",
        eventId: `evt_${String(sequence).padStart(26, "0")}`,
        sequence,
        timestamp: `2026-07-13T02:00:0${sequence}+00:00`,
        traceId: "trc_01J10000000000000000000000",
        workspaceId: WORKSPACE,
        sessionId: SESSION,
        turnId: TURN,
        runId: RUN,
        rootRunId: RUN,
        parentRunId: null,
        type,
        payload,
    };
}

function durableSessionResult(events, { title = "Durable history", activeRunId = RUN } = {}) {
    const lastSequence = events.at(-1)?.sequence ?? 0;
    return {
        session: {
            summary: { sessionId: SESSION, title, activeRunId },
            turns: lastSequence === 0 ? [] : [{
                sessionId: SESSION,
                turnId: TURN,
                selectedRunId: RUN,
                runs: [{ runId: RUN, sessionId: SESSION, turnId: TURN, lastSequence }],
            }],
        },
    };
}

function durableReplay(events) {
    return async (_sessionId, cursors) => {
        const after = cursors[RUN] ?? 0;
        return {
            events: events.filter((event) => event.sequence > after),
            runCursors: { ...cursors, [RUN]: events.at(-1)?.sequence ?? after },
        };
    };
}

test("ChatStore persists multi-tabs without treating tab close as Run cancel", async () => {
    const { ChatStore } = loadModule("chat_store.ts");
    const calls = [];
    const client = createClient(async (method, params) => { calls.push([method, params]); return {}; });
    const persistence = memoryPersistence();
    const store = new ChatStore(client, persistence);
    await store.initialize();
    const first = store.activeTab.tabId;
    const second = await store.openSession("ses_01J00000000000000000000000");
    await store.updateDraft(store.activeTab.tabId, "continue later");
    await store.closeTab(second.tabId);

    assert.equal(store.snapshot.tabs.length, 1);
    assert.equal(store.snapshot.activeTabId, first);
    assert.deepEqual(calls, []);
    assert.equal(persistence.value.schemaVersion, 1);
    await store.dispose();
});

test("history reopen and tab selection hydrate durable events from the reducer cursor without duplicates", async () => {
    const { ChatStore } = loadModule("chat_store.ts");
    const events = [
        runEvent(1, "turn.started", { input: [{ type: "text", text: "persisted question" }] }),
        runEvent(2, "assistant.completed", { content: [{ type: "text", text: "persisted answer" }] }),
    ];
    let activeRunId = RUN;
    const hydrationCalls = [];
    const client = createClient(async () => ({}), {
        sessionGet: async () => durableSessionResult(events, { activeRunId }),
        replaySession: durableReplay(events),
        onHydrationCall: (method, params) => hydrationCalls.push([method, structuredClone(params)]),
    });
    const store = new ChatStore(client, memoryPersistence());
    await store.initialize();

    const opened = await store.openSession(SESSION);
    assert.equal(opened.title, "Durable history");
    assert.deepEqual(client.reducer.state.runs.get(RUN).userBlocks, ["persisted question"]);
    assert.deepEqual(client.reducer.state.runs.get(RUN).assistantBlocks, ["persisted answer"]);
    assert.deepEqual(hydrationCalls.filter(([method]) => method === "events/replay")[0][1].runCursors, { [RUN]: 0 });

    await store.createTab();
    events.push(runEvent(3, "turn.completed", { reason: "completed" }));
    activeRunId = null;
    await store.selectTab(opened.tabId);
    assert.equal(client.reducer.state.runs.get(RUN).status, "completed");
    assert.equal(store.activeTab.selectedRunId, null);
    assert.deepEqual(hydrationCalls.filter(([method]) => method === "events/replay")[1][1].runCursors, { [RUN]: 2 });

    await store.closeTab(opened.tabId);
    await store.openSession(SESSION);
    assert.equal(store.snapshot.tabs.filter((tab) => tab.sessionId === SESSION).length, 1);
    assert.deepEqual(hydrationCalls.filter(([method]) => method === "events/replay")[2][1].runCursors, { [RUN]: 3 });
    assert.deepEqual(client.reducer.state.runs.get(RUN).assistantBlocks, ["persisted answer"]);
});

test("Store and client reconstruction replay persisted tabs from their respective applied cursors", async () => {
    const { ChatStore } = loadModule("chat_store.ts");
    const events = [
        runEvent(1, "turn.started", { input: [{ type: "text", text: "rebuild question" }] }),
        runEvent(2, "assistant.completed", { content: [{ type: "text", text: "rebuild answer" }] }),
    ];
    const persistence = memoryPersistence({
        schemaVersion: 1,
        activeTabId: "tab_rebuild",
        tabs: [{ tabId: "tab_rebuild", sessionId: SESSION, title: "stale", draft: "", selectedRunId: null }],
    });
    const sameClientCursors = [];
    const sameClient = createClient(async () => ({}), {
        sessionGet: async () => durableSessionResult(events),
        replaySession: async (sessionId, cursors) => {
            sameClientCursors.push(structuredClone(cursors));
            return durableReplay(events)(sessionId, cursors);
        },
    });
    const first = new ChatStore(sameClient, persistence);
    await first.initialize();
    await first.dispose();
    const rebuiltStore = new ChatStore(sameClient, persistence);
    await rebuiltStore.initialize();
    assert.deepEqual(sameClientCursors, [{ [RUN]: 0 }, { [RUN]: 2 }]);
    assert.deepEqual(sameClient.reducer.state.runs.get(RUN).assistantBlocks, ["rebuild answer"]);
    await rebuiltStore.dispose();

    const freshClientCursors = [];
    const freshClient = createClient(async () => ({}), {
        sessionGet: async () => durableSessionResult(events),
        replaySession: async (sessionId, cursors) => {
            freshClientCursors.push(structuredClone(cursors));
            return durableReplay(events)(sessionId, cursors);
        },
    });
    const pluginRebuiltStore = new ChatStore(freshClient, persistence);
    await pluginRebuiltStore.initialize();
    assert.deepEqual(freshClientCursors, [{ [RUN]: 0 }]);
    assert.deepEqual(freshClient.reducer.state.runs.get(RUN).assistantBlocks, ["rebuild answer"]);
});

test("concurrent history opens single-flight an empty Session hydration and create one tab", async () => {
    const { ChatStore } = loadModule("chat_store.ts");
    let release;
    const gate = new Promise((resolve) => { release = resolve; });
    let gets = 0;
    let replays = 0;
    const client = createClient(async () => ({}), {
        sessionGet: async (sessionId) => {
            gets += 1;
            await gate;
            return emptySessionResult(sessionId, "Empty durable session");
        },
        replaySession: async (_sessionId, cursors) => {
            replays += 1;
            return { events: [], runCursors: cursors };
        },
    });
    const store = new ChatStore(client, memoryPersistence());
    await store.initialize();
    const first = store.openSession(SESSION);
    const second = store.openSession(SESSION);
    release();
    const [firstTab, secondTab] = await Promise.all([first, second]);

    assert.equal(gets, 2);
    assert.equal(replays, 1);
    assert.equal(firstTab.tabId, secondTab.tabId);
    assert.equal(store.activeTab.title, "Empty durable session");
    assert.equal(store.snapshot.tabs.filter((tab) => tab.sessionId === SESSION).length, 1);
    assert.equal(client.reducer.state.runs.size, 0);
});

test("live Run created during replay is selected only after the durable Session watermark stabilizes", async () => {
    const { ChatStore } = loadModule("chat_store.ts");
    const live = runEvent(1, "turn.started", { input: [{ type: "text", text: "live question" }] });
    let sessionGets = 0;
    let replays = 0;
    let client;
    client = createClient(async () => ({}), {
        sessionGet: async (sessionId) => {
            sessionGets += 1;
            return sessionGets === 1
                ? emptySessionResult(sessionId, "Before live Run")
                : durableSessionResult([live], { title: "After live Run", activeRunId: RUN });
        },
        replaySession: async (_sessionId, cursors) => {
            replays += 1;
            if (replays === 1) client.reducer.accept(live);
            return { events: [], runCursors: { ...cursors, [RUN]: 1 } };
        },
    });
    const store = new ChatStore(client, memoryPersistence());
    await store.initialize();

    const opened = await store.openSession(SESSION);
    assert.equal(opened.title, "After live Run");
    assert.equal(opened.selectedRunId, RUN);
    assert.equal(store.activeTab.selectedRunId, RUN);
    assert.equal(client.reducer.state.runs.get(RUN).status, "turn.started");
    assert.equal(sessionGets, 3);
    assert.equal(replays, 2);
});

test("one stale persisted Session cannot block ChatStore and remains retryable without losing its draft", async () => {
    const { ChatStore } = loadModule("chat_store.ts");
    let unavailable = true;
    const persistence = memoryPersistence({
        schemaVersion: 1,
        activeTabId: "tab_stale",
        tabs: [{
            tabId: "tab_stale",
            sessionId: SESSION,
            title: "Stale history",
            draft: "keep this draft",
            selectedRunId: null,
        }],
    });
    const client = createClient(async () => ({}), {
        sessionGet: async (sessionId) => {
            if (unavailable) throw new Error("Session 已删除或暂时不可用");
            return emptySessionResult(sessionId, "Recovered history");
        },
    });
    const store = new ChatStore(client, persistence);
    await store.initialize();

    const stale = store.snapshot.tabs.find((tab) => tab.tabId === "tab_stale");
    assert.equal(stale.sessionId, SESSION);
    assert.equal(stale.draft, "keep this draft");
    assert.equal(store.activeTab.sessionId, null);
    assert.match(store.snapshot.lastError, /部分历史会话恢复失败/);

    unavailable = false;
    await store.selectTab("tab_stale");
    assert.equal(store.activeTab.sessionId, SESSION);
    assert.equal(store.activeTab.title, "Recovered history");
    assert.equal(store.activeTab.draft, "keep this draft");
    assert.equal(store.snapshot.lastError, null);
});

test("continuously changing Session watermark fails after a bounded number of hydration passes", async () => {
    const { ChatStore } = loadModule("chat_store.ts");
    const events = [runEvent(1, "turn.started", { input: [{ type: "text", text: "changing" }] })];
    let gets = 0;
    const client = createClient(async () => ({}), {
        sessionGet: async () => {
            gets += 1;
            return durableSessionResult(events, { activeRunId: gets % 2 === 1 ? RUN : null });
        },
        replaySession: durableReplay(events),
    });
    const store = new ChatStore(client, memoryPersistence());
    await store.initialize();

    await assert.rejects(store.openSession(SESSION), /持续变化.*有界重试/);
    assert.equal(gets, 9);
    assert.equal(store.snapshot.tabs.some((tab) => tab.sessionId === SESSION), false);
    assert.match(store.snapshot.lastError, /持续变化/);
});

test("send creates a Session then submits one typed turn/start command", async () => {
    const { ChatStore } = loadModule("chat_store.ts");
    const calls = [];
    const client = createClient(async (method, params) => {
        calls.push([method, params]);
        if (method === "session/create") return {
            session: { sessionId: "ses_01J00000000000000000000000", title: "New" },
            created: true,
        };
        if (method === "turn/start") return {
            sessionId: params.sessionId,
            turnId: params.turnId,
            runId: "run_01J00000000000000000000000",
            accepted: true,
            duplicate: false,
        };
        throw new Error("unexpected method");
    });
    const store = new ChatStore(client, memoryPersistence());
    await store.initialize();
    await store.updateDraft(store.activeTab.tabId, "draft");
    const result = await store.send("  hello  ", { runConfig, context: null });

    assert.equal(result.runId, "run_01J00000000000000000000000");
    assert.deepEqual(calls.map(([method]) => method), ["session/create", "turn/start"]);
    assert.equal(calls[1][1].input[0].text, "hello");
    assert.equal(calls[1][1].runConfig.provider, "local");
    assert.deepEqual(calls[1][1].writeIntent, { kind: "none" });
    assert.match(calls[1][1].turnId, /^turn_[0-9a-f]{32}$/);
    assert.equal(store.activeTab.draft, "");
    assert.equal(store.activeTab.sessionId, "ses_01J00000000000000000000000");
});

test("draft persistence stays on its originating tab and does not emit a render snapshot", async () => {
    const { ChatStore } = loadModule("chat_store.ts");
    const persistence = memoryPersistence();
    const store = new ChatStore(createClient(async () => ({})), persistence);
    await store.initialize();
    const firstTabId = store.activeTab.tabId;
    const secondTab = await store.createTab();
    let emissions = 0;
    const unsubscribe = store.subscribe(() => { emissions += 1; });
    const beforeDraftSave = emissions;

    await store.updateDraft(firstTabId, "中文草稿");

    assert.equal(emissions, beforeDraftSave);
    assert.equal(store.activeTab.tabId, secondTab.tabId);
    assert.equal(store.activeTab.draft, "");
    assert.equal(store.snapshot.tabs.find((tab) => tab.tabId === firstTabId).draft, "中文草稿");
    assert.equal(persistence.value.tabs.find((tab) => tab.tabId === firstTabId).draft, "中文草稿");
    unsubscribe();
});

test("send is single-flight before the first turn/start await", async () => {
    const { ChatStore } = loadModule("chat_store.ts");
    let releaseStart;
    const startGate = new Promise((resolve) => { releaseStart = resolve; });
    let starts = 0;
    const client = createClient(async (method, params) => {
        assert.equal(method, "turn/start");
        starts += 1;
        await startGate;
        return {
            sessionId: params.sessionId,
            turnId: params.turnId,
            runId: "run_01J00000000000000000000011",
            accepted: true,
            duplicate: false,
        };
    });
    const persistence = memoryPersistence({
        schemaVersion: 1,
        activeTabId: "tab_single_flight",
        tabs: [{
            tabId: "tab_single_flight",
            sessionId: "ses_01J00000000000000000000000",
            title: "Single flight",
            draft: "",
            selectedRunId: null,
        }],
    });
    const store = new ChatStore(client, persistence);
    await store.initialize();

    const first = store.send("first", { runConfig });
    const duplicate = store.send("duplicate", { runConfig });
    await assert.rejects(duplicate, /正在发送/);
    assert.equal(starts, 1);
    releaseStart();
    await first;
});

test("new-tab send keeps its single-flight lock after Session creation changes the lock identity", async () => {
    const { ChatStore } = loadModule("chat_store.ts");
    let releaseStart;
    let signalStart;
    const startGate = new Promise((resolve) => { releaseStart = resolve; });
    const startSeen = new Promise((resolve) => { signalStart = resolve; });
    const calls = [];
    const client = createClient(async (method, params) => {
        calls.push(method);
        if (method === "session/create") return {
            session: { sessionId: "ses_01J00000000000000000000022", title: "New" },
            created: true,
        };
        assert.equal(method, "turn/start");
        signalStart();
        await startGate;
        return {
            sessionId: params.sessionId,
            turnId: params.turnId,
            runId: "run_01J00000000000000000000022",
            accepted: true,
            duplicate: false,
        };
    });
    const store = new ChatStore(client, memoryPersistence());
    await store.initialize();

    const first = store.send("first", { runConfig });
    await startSeen;
    await assert.rejects(store.send("duplicate", { runConfig }), /正在发送/);
    assert.deepEqual(calls, ["session/create", "turn/start"]);
    releaseStart();
    await first;
});

test("send refuses a second root Turn while the Session has a nonterminal Run", async () => {
    const { ChatStore } = loadModule("chat_store.ts");
    let requests = 0;
    const client = createClient(async () => { requests += 1; return {}; });
    const persistence = memoryPersistence({
        schemaVersion: 1,
        activeTabId: "tab_active_run",
        tabs: [{
            tabId: "tab_active_run",
            sessionId: SESSION,
            title: "Active",
            draft: "",
            selectedRunId: null,
        }],
    });
    const store = new ChatStore(client, persistence);
    await store.initialize();
    client.reducer.accept(runEvent(1, "turn.started", { input: [{ type: "text", text: "running" }] }));

    await assert.rejects(store.send("duplicate", { runConfig }), /仍在运行/);
    assert.equal(requests, 0);
});

test("send binds an explicit Vault write requirement to canonical target paths", async () => {
    const { ChatStore } = loadModule("chat_store.ts");
    let startParams;
    const client = createClient(async (method, params) => {
        assert.equal(method, "turn/start");
        startParams = params;
        return {
            sessionId: params.sessionId,
            turnId: params.turnId,
            runId: "run_01J00000000000000000000001",
            accepted: true,
            duplicate: false,
        };
    });
    const persistence = memoryPersistence({
        schemaVersion: 1,
        activeTabId: "tab_01",
        tabs: [{
            tabId: "tab_01",
            sessionId: "ses_01J00000000000000000000000",
            title: "Write",
            draft: "",
            selectedRunId: null,
        }],
    });
    const store = new ChatStore(client, persistence);
    await store.initialize();
    await store.send("write", {
        runConfig,
        writeIntent: {
            kind: "vault_write_required",
            targetPaths: ["notes/summary.md", "notes/offer.md", "notes/summary.md"],
        },
    });

    assert.deepEqual(startParams.writeIntent, {
        kind: "vault_write_required",
        targetPaths: ["notes/offer.md", "notes/summary.md"],
        intentHash: "sha256:fa5a23b66ecbf251c33f8a0f46e3c719d416d3b1109ecdb0baed08466f3b9490",
    });
    await assert.rejects(
        store.send("escape", {
            runConfig,
            writeIntent: { kind: "vault_write_required", targetPaths: ["../outside.md"] },
        }),
        /unsafe write intent Vault path/,
    );
});

test("write intent uses Unicode-scalar path order and the cross-language canonical hash vector", () => {
    const { buildWriteIntent } = loadModule("chat_store.ts");
    assert.deepEqual(buildWriteIntent({
        kind: "vault_write_required",
        targetPaths: ["notes/😀.md", "notes/\ue000.md"],
    }), {
        kind: "vault_write_required",
        targetPaths: ["notes/\ue000.md", "notes/😀.md"],
        intentHash: "sha256:30f42b95f8a9f84f5794d603e05de23bb6260532190ac6826bae7c2958f17bbb",
    });
    assert.throws(
        () => buildWriteIntent({ kind: "vault_write_required", targetPaths: ["notes/\ud800.md"] }),
        /unsafe write intent Vault path/,
    );
});

test("semantic events update the selected Run without UI terminal heuristics", async () => {
    const { ChatStore } = loadModule("chat_store.ts");
    const client = createClient(async () => ({}));
    const persistence = memoryPersistence({
        schemaVersion: 1,
        activeTabId: "tab_01",
        tabs: [{ tabId: "tab_01", sessionId: "ses_01", title: "A", draft: "", selectedRunId: null }],
    });
    const store = new ChatStore(client, persistence);
    await store.initialize();
    client.reducer.accept({
        protocolVersion: "1.0",
        schemaVersion: "1",
        eventId: "evt_01",
        sequence: 1,
        timestamp: "2026-07-13T02:00:00+00:00",
        traceId: "trace_01",
        workspaceId: WORKSPACE,
        sessionId: "ses_01",
        turnId: "turn_01",
        runId: "run_01",
        rootRunId: "run_01",
        parentRunId: null,
        type: "phase.changed",
        payload: { previousPhase: "planning", phase: "completing", reason: null },
    });

    assert.equal(store.activeTab.selectedRunId, "run_01");
    assert.equal(client.reducer.state.runs.get("run_01").status, "running");
});

test("approval resolution preserves the exact args hash binding", async () => {
    const { ChatStore } = loadModule("chat_store.ts");
    let request;
    const client = createClient(async (method, params) => { request = [method, params]; return { status: "approved" }; });
    const store = new ChatStore(client, memoryPersistence());
    await store.initialize();
    const hash = `sha256:${"b".repeat(64)}`;
    await store.resolveApproval("apr_01", "allow_once", "once", hash, false, "looks good");

    assert.equal(request[0], "approval/resolve");
    assert.equal(request[1].expectedArgsHash, hash);
    assert.equal(request[1].comment, "looks good");
});

test("corrupt persisted tabs fail closed to a new local tab", async () => {
    const { ChatStore, parsePersistedTabs } = loadModule("chat_store.ts");
    assert.equal(parsePersistedTabs({ schemaVersion: 1, activeTabId: "missing", tabs: [] }), null);
    assert.equal(parsePersistedTabs({
        schemaVersion: 1,
        activeTabId: "tab_01",
        tabs: [{ tabId: "tab_01", sessionId: null, title: "A", draft: "", selectedRunId: null, extra: true }],
    }), null);
    const persistence = memoryPersistence({ unsafe: true });
    const store = new ChatStore(createClient(async () => ({})), persistence);
    await store.initialize();
    assert.equal(store.snapshot.tabs.length, 1);
    assert.match(store.activeTab.tabId, /^tab_[0-9a-f]{32}$/);
});
