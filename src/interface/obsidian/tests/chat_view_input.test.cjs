const assert = require("node:assert/strict");
const { readFileSync } = require("node:fs");
const path = require("node:path");
const test = require("node:test");
const { buildSync } = require("esbuild");

function loadModule() {
    const output = buildSync({
        entryPoints: [path.join(__dirname, "../src/local/chat_view.ts")],
        bundle: true,
        format: "cjs",
        platform: "node",
        target: "node16",
        external: ["obsidian"],
        write: false,
    }).outputFiles[0].text;
    const compiled = { exports: {} };
    const obsidian = new Proxy({
        ItemView: class {},
        Menu: class {},
        Notice: class {},
        WorkspaceLeaf: class {},
        setIcon() {},
    }, {
        get(target, key) { return key in target ? target[key] : class {}; },
    });
    const fakeRequire = (id) => id === "obsidian" ? obsidian : require(id);
    new Function("require", "module", "exports", output)(fakeRequire, compiled, compiled.exports);
    return compiled.exports;
}

function key(overrides = {}) {
    return { key: "Enter", shiftKey: false, isComposing: false, keyCode: 13, ...overrides };
}

test("composer Enter submits only outside IME composition and blocked states", () => {
    const { shouldSendComposerInput } = loadModule();

    assert.equal(shouldSendComposerInput(key(), false, false), true);
    assert.equal(shouldSendComposerInput(key({ shiftKey: true }), false, false), false);
    assert.equal(shouldSendComposerInput(key({ isComposing: true }), false, false), false);
    assert.equal(shouldSendComposerInput(key({ keyCode: 229 }), false, false), false);
    assert.equal(shouldSendComposerInput(key(), true, false), false);
    assert.equal(shouldSendComposerInput(key(), false, true), false);
    assert.equal(shouldSendComposerInput(key({ key: "Process", keyCode: 229 }), false, false), false);
});

test("composer wires both composition lifecycle events before its input and key handlers", () => {
    const source = readFileSync(path.join(__dirname, "../src/local/chat_view.ts"), "utf8");
    const start = source.indexOf('addEventListener("compositionstart"');
    const end = source.indexOf('addEventListener("compositionend"');
    const input = source.indexOf("input.oninput");
    const keydown = source.indexOf("input.onkeydown");

    assert.ok(start >= 0 && end > start);
    assert.ok(input > end && keydown > input);
    assert.match(source, /event\.keyCode !== 229/);
});

test("model capability labels come from the live catalog instead of a vision probe", () => {
    const { modelCapabilityLabel } = loadModule();

    assert.equal(modelCapabilityLabel({
        inputModalities: ["text", "image"],
        supportsImageDetailOriginal: true,
        supportsHostedSearch: true,
        supportsFastMode: true,
    }), "图文（原图） · 托管搜索 · Fast Mode");
    assert.equal(modelCapabilityLabel({
        inputModalities: ["text"],
        supportsImageDetailOriginal: false,
        supportsHostedSearch: false,
        supportsFastMode: false,
    }), "文本 · 无托管搜索 · 标准速度");
});

test("the composer treats a model id from another Codex account as an unselected choice", () => {
    const { modelChoiceMatchesSelection } = loadModule();
    const choice = {
        model: "shared-model",
        accountBinding: `sha256:${"a".repeat(64)}`,
    };

    assert.equal(modelChoiceMatchesSelection(choice, {
        model: "shared-model",
        modelAccountBinding: choice.accountBinding,
    }), true);
    assert.equal(modelChoiceMatchesSelection(choice, {
        model: "shared-model",
        modelAccountBinding: `sha256:${"b".repeat(64)}`,
    }), false);
    assert.equal(modelChoiceMatchesSelection(choice, {
        model: "different-model",
        modelAccountBinding: choice.accountBinding,
    }), false);
});

test("a late Store bind cannot attach an obsolete Worker generation after restart", async () => {
    const { LocalChatView } = loadModule();
    const originalWindow = global.window;
    global.window = { requestAnimationFrame: () => 1 };
    let runtime = { state: "ready", generation: 1, attempt: 1, error: null };
    let runtimeListener = null;
    let releaseFirst;
    const first = new Promise((resolve) => { releaseFirst = resolve; });
    const subscriptions = [];
    const store = (name) => ({
        subscribe() {
            subscriptions.push(name);
            return () => undefined;
        },
    });
    let ensureCalls = 0;
    const host = {
        runtimeSnapshot: () => runtime,
        subscribeRuntime(listener) {
            runtimeListener = listener;
            return () => undefined;
        },
        async ensureChatStore() {
            ensureCalls += 1;
            return ensureCalls === 1 ? await first : store("current");
        },
        async listModels() { return []; },
    };
    try {
        const view = new LocalChatView({}, host);
        const opening = view.onOpen();
        runtime = { state: "ready", generation: 2, attempt: 1, error: null };
        runtimeListener(runtime);
        releaseFirst(store("obsolete"));
        await opening;
        await new Promise((resolve) => setImmediate(resolve));

        assert.equal(ensureCalls, 2);
        assert.deepEqual(subscriptions, ["current"]);
        await view.onClose();
    } finally {
        if (originalWindow === undefined) delete global.window;
        else global.window = originalWindow;
    }
});

test("timeline renders a submitted user message before its durable turn event arrives", () => {
    const source = readFileSync(path.join(__dirname, "../src/local/chat_view.ts"), "utf8");

    assert.match(source, /snapshot\.pendingSubmissions\.filter/);
    assert.match(source, /renderPendingSubmission/);
    assert.match(source, /正在提交给 OfferAgent/);
});

test("timeline preserves durable item order while collapsing only contiguous ordinary tools", () => {
    const source = readFileSync(path.join(__dirname, "../src/local/chat_view.ts"), "utf8");

    assert.match(source, /for \(let index = 0; index < run\.timeline\.length;\)/);
    assert.match(source, /const candidate = run\.timeline\[index\]/);
    assert.match(source, /case "reasoning"/);
    assert.match(source, /case "tool_call"/);
    assert.match(source, /case "subagent"/);
});

test("timeline follow intent preserves a reader who scrolled away from streaming output", () => {
    const { timelineScrollIntent } = loadModule();

    assert.deepEqual(timelineScrollIntent({ scrollTop: 552, scrollHeight: 1000, clientHeight: 400 }), {
        follow: true, scrollTop: 552, scrollHeight: 1000,
    });
    assert.deepEqual(timelineScrollIntent({ scrollTop: 100, scrollHeight: 1000, clientHeight: 400 }), {
        follow: false, scrollTop: 100, scrollHeight: 1000,
    });
});

test("terminal run actions distinguish explicit continuation and stop-and-revise text", () => {
    const { explicitContinuationStatus, originalTurnPrompt } = loadModule();
    const timeline = [
        { kind: "user_message", source: "turn", blocks: ["original", "prompt"] },
        { kind: "user_message", source: "steer", blocks: ["later steer"] },
    ];

    assert.equal(originalTurnPrompt(timeline), "original\n\nprompt");
    assert.equal(explicitContinuationStatus("interrupted"), true);
    assert.equal(explicitContinuationStatus("orphaned"), true);
    assert.equal(explicitContinuationStatus("cancelled"), false);
    assert.equal(explicitContinuationStatus("completed"), false);
});

test("ordinary tools collapse while Vault changes and failed outcomes remain direct", () => {
    const { ordinaryToolActivity } = loadModule();

    assert.equal(ordinaryToolActivity({ name: "vault.search", status: "succeeded" }), true);
    assert.equal(ordinaryToolActivity({ name: "vault.changes.apply", status: "succeeded" }), false);
    assert.equal(ordinaryToolActivity({ name: "project.read", status: "failed" }), false);
    assert.equal(ordinaryToolActivity({ name: "vault.read", status: "unknown_outcome" }), false);
});

test("applied Vault Change cards expose only their exact guarded undo batch", () => {
    const { vaultChangeUndoBatchId } = loadModule();
    const applied = {
        name: "vault.changes.apply",
        status: "succeeded",
        result: { data: { batchId: "guarded", state: "applied", undoAvailable: true } },
    };

    assert.equal(vaultChangeUndoBatchId(applied), "guarded");
    assert.equal(vaultChangeUndoBatchId({ ...applied, status: "failed" }), undefined);
    assert.equal(vaultChangeUndoBatchId({
        ...applied,
        result: { data: { ...applied.result.data, undoAvailable: false } },
    }), undefined);
    assert.equal(vaultChangeUndoBatchId({ ...applied, name: "vault.read" }), undefined);
});

test("Sidebar exposes confirmed Conversation deletion and guarded Vault undo through its host", () => {
    const source = readFileSync(path.join(__dirname, "../src/local/chat_view.ts"), "utf8");

    assert.match(source, /deleteConversation\(sessionId: string\): Promise<boolean>/);
    assert.match(source, /undoVaultChange\(batchId: string\): Promise<string>/);
    assert.match(source, /删除会话及其附件/);
    assert.match(source, /this\.host\.deleteConversation\(session\.sessionId\)/);
    assert.match(source, /撤销此批更改/);
    assert.match(source, /this\.host\.undoVaultChange\(batchId\)/);
});

test("sidebar uses Obsidian Markdown, Worker model choices, settings, and a frozen-scroll affordance", () => {
    const source = readFileSync(path.join(__dirname, "../src/local/chat_view.ts"), "utf8");

    assert.match(source, /MarkdownRenderer\.render/);
    assert.match(source, /new Component\(\)/);
    assert.match(source, /releaseMarkdownComponents/);
    assert.match(source, /Promise\.all\(markdownTasks\)/);
    assert.match(source, /scheduleStreamingAssistantPatch/);
    assert.match(source, /patchStreamingAssistant/);
    assert.match(source, /if \(this\.closed\) return/);
    assert.match(source, /host\.listModels\(\)/);
    assert.match(source, /host\.selectModel\(/);
    assert.match(source, /host\.openSettings\(\)/);
    assert.match(source, /新内容/);
    assert.match(source, /放入输入框/);
});

test("composer supports bounded image paste drop reuse and distinct pinned-context chips", () => {
    const source = readFileSync(path.join(__dirname, "../src/local/chat_view.ts"), "utf8");

    assert.match(source, /input\.onpaste/);
    assert.match(source, /input\.ondrop/);
    assert.match(source, /uploadConversationAttachment/);
    assert.match(source, /再次使用/);
    assert.match(source, /固定当前上下文/);
    assert.match(source, /偏好，不是已用证据/);
    assert.match(source, /本轮实际使用的证据/);
});
