const assert = require("node:assert/strict");
const { createHash } = require("node:crypto");
const { mkdtemp, rm, writeFile } = require("node:fs/promises");
const { tmpdir } = require("node:os");
const path = require("node:path");
const test = require("node:test");
const { buildSync } = require("esbuild");

class TFile {
    constructor(filePath, content = "") { this.path = filePath; this.content = content; }
}
class TFolder {
    constructor(folderPath) { this.path = folderPath; this.children = []; }
}
class MarkdownView {}

function loadModule(entry) {
    const output = buildSync({
        entryPoints: [path.join(__dirname, `../src/local/${entry}`)],
        bundle: true,
        format: "cjs",
        platform: "node",
        target: "node16",
        external: ["obsidian"],
        write: false,
    }).outputFiles[0].text;
    const compiled = { exports: {} };
    const fakeRequire = (id) => id === "obsidian" ? { MarkdownView, TFile, TFolder } : require(id);
    new Function("require", "module", "exports", output)(fakeRequire, compiled, compiled.exports);
    return compiled.exports;
}

class FakeVault {
    constructor(files = {}) {
        this.entries = new Map(Object.entries(files).map(([filePath, content]) => [filePath, new TFile(filePath, content)]));
        this.failCreate = null;
    }
    getAbstractFileByPath(filePath) { return this.entries.get(filePath) ?? null; }
    async read(file) { return file.content; }
    async cachedRead(file) { return file.content; }
    async process(file, transform) { file.content = transform(file.content); return file.content; }
    async create(filePath, content) {
        if (filePath === this.failCreate) throw new Error("injected create failure");
        if (this.entries.has(filePath)) throw new Error("exists");
        const file = new TFile(filePath, content); this.entries.set(filePath, file); return file;
    }
    async createFolder(folderPath) {
        const folder = new TFolder(folderPath); this.entries.set(folderPath, folder); return folder;
    }
    async rename(file, destination) { this.entries.delete(file.path); file.path = destination; this.entries.set(destination, file); }
    async delete(entry) { this.entries.delete(entry.path); }
}

function createApp(files = {}, openEditor = null) {
    const vault = new FakeVault(files);
    const leaves = [];
    if (openEditor) {
        const file = vault.getAbstractFileByPath(openEditor.path);
        let live = openEditor.content;
        const editor = {
            getValue: () => live,
            setValue: (value) => { live = value; },
            getSelection: () => "",
            getCursor: () => ({ line: 0, ch: 0 }),
            posToOffset: () => 0,
        };
        leaves.push({
            view: Object.assign(new MarkdownView(), { file, editor }),
            getViewState: () => ({ type: "markdown", state: { file: openEditor.path } }),
        });
    }
    const workspace = {
        getLeavesOfType: () => leaves,
        getActiveViewOfType: () => leaves[0]?.view ?? null,
        getActiveFile: () => leaves[0]?.view.file ?? null,
    };
    const app = {
        vault,
        workspace,
        fileManager: { trashFile: async (file) => vault.entries.delete(file.path) },
    };
    return { app, vault, leaves };
}

async function fixture(t, files = {}, openEditor = null) {
    const directory = await mkdtemp(path.join(tmpdir(), "offeragent-client-tool-"));
    t.after(() => rm(directory, { recursive: true, force: true }));
    const { ClientInvocationJournal } = loadModule("client_invocation_journal.ts");
    const { ObsidianClientToolBridge } = loadModule("client_tool_bridge.ts");
    const { ObsidianContextBridge } = loadModule("obsidian_context.ts");
    const appFixture = createApp(files, openEditor);
    const context = new ObsidianContextBridge(appFixture.app);
    const journalPath = path.join(directory, "journal.json");
    const journal = new ClientInvocationJournal(journalPath, "wsi_01");
    const bridge = new ObsidianClientToolBridge(appFixture.app, context, journal, { present: async () => true });
    return { ...appFixture, bridge, journal, journalPath };
}

function digest(value) { return `sha256:${createHash("sha256").update(value, "utf8").digest("hex")}`; }

function invocation(argumentsValue, overrides = {}) {
    const { canonicalJson } = loadModule("client_invocation_journal.ts");
    return {
        invocationId: "inv_01",
        toolCallId: "call_01",
        runId: "run_01",
        name: "obsidian.vault.transaction",
        arguments: argumentsValue,
        argsHash: digest(canonicalJson(argumentsValue)),
        idempotencyKey: "idem_01",
        deadline: new Date(Date.now() + 60_000).toISOString(),
        traceId: "trace_01",
        ...overrides,
    };
}

function previewRequest(argumentsValue, overrides = {}) {
    const { idempotencyKey, ...params } = invocation(argumentsValue, overrides);
    return params;
}

function commitObserveRequest(paths, overrides = {}) {
    return {
        invocationId: "inv_01",
        toolCallId: "call_01",
        runId: "run_01",
        paths,
        deadline: new Date(Date.now() + 60_000).toISOString(),
        traceId: "trace_01",
        ...overrides,
    };
}

function invocationRequestHash(params) {
    const { canonicalJson } = loadModule("client_invocation_journal.ts");
    return digest(canonicalJson({
        invocationId: params.invocationId,
        toolCallId: params.toolCallId,
        runId: params.runId,
        name: params.name,
        arguments: params.arguments,
        argsHash: params.argsHash,
        idempotencyKey: params.idempotencyKey,
    }));
}

test("preview returns exact live-editor path proof without writing the Vault", async (t) => {
    const { bridge, leaves, vault } = await fixture(t, { "notes/a.md": "saved" }, { path: "notes/a.md", content: "draft" });
    const args = {
        transactionId: "tx_preview",
        operations: [{ op: "append", path: "notes/a.md", content: "+new", expectedHash: digest("draft") }],
    };

    const preview = await bridge.preview(previewRequest(args), new AbortController().signal);

    assert.equal(preview.hasUnsavedEditors, true);
    assert.equal(preview.hasOpenEditors, true);
    assert.deepEqual(preview.paths, ["notes/a.md"]);
    assert.deepEqual(preview.pathStates, [{
        path: "notes/a.md",
        beforeHash: digest("draft"),
        afterHash: digest("draft+new"),
        unsavedEditor: true,
        openEditor: true,
    }]);
    assert.equal(preview.diffSha256, digest(preview.diff));
    assert.match(preview.diff, /--- a\/notes\/a\.md/);
    assert.match(preview.diff, /-draft/);
    assert.match(preview.diff, /\+draft\+new/);
    assert.equal(leaves[0].view.editor.getValue(), "draft");
    assert.equal(vault.getAbstractFileByPath("notes/a.md").content, "saved");

    leaves[0].view.editor.setValue("changed after approval");
    await assert.rejects(() => bridge.preview(previewRequest(args), new AbortController().signal), /expectedHash/);
});

test("an open but saved editor is distinguished from an unsaved editor", async (t) => {
    const { bridge, vault } = await fixture(t, { "notes/a.md": "saved" }, { path: "notes/a.md", content: "saved" });
    const args = {
        transactionId: "tx_saved_editor",
        operations: [{ op: "append", path: "notes/a.md", content: "+new", expectedHash: digest("saved") }],
    };
    const preview = await bridge.preview(previewRequest(args), new AbortController().signal);
    assert.equal(preview.hasOpenEditors, true);
    assert.equal(preview.hasUnsavedEditors, false);
    assert.equal(preview.pathStates[0].openEditor, true);
    assert.equal(preview.pathStates[0].unsavedEditor, false);
    assert.equal(vault.getAbstractFileByPath("notes/a.md").content, "saved");
});

test("post-commit observation reports exact disk hashes and reopened editor state", async (t) => {
    const { bridge, vault } = await fixture(t, { "notes/a.md": "after" });
    const closed = await bridge.observeCommit(
        commitObserveRequest(["notes/a.md", "notes/missing.md"]),
        new AbortController().signal,
    );
    assert.deepEqual(closed.pathStates, [
        { path: "notes/a.md", observedHash: digest("after"), unsavedEditor: false, openEditor: false },
        { path: "notes/missing.md", observedHash: "absent", unsavedEditor: false, openEditor: false },
    ]);
    assert.equal(closed.hasOpenEditors, false);
    assert.equal(closed.hasUnsavedEditors, false);
    assert.equal(vault.getAbstractFileByPath("notes/a.md").content, "after");

    const reopened = await fixture(t, { "notes/a.md": "after" }, { path: "notes/a.md", content: "stale draft" });
    const unsafe = await reopened.bridge.observeCommit(
        commitObserveRequest(["notes/a.md"]),
        new AbortController().signal,
    );
    assert.equal(unsafe.hasOpenEditors, true);
    assert.equal(unsafe.hasUnsavedEditors, true);
    assert.equal(unsafe.pathStates[0].observedHash, digest("after"));
});

test("post-commit observation rejects editor open and TFile replacement during vault.read", async (t) => {
    const editorRace = await fixture(t, { "notes/a.md": "after" });
    const originalRead = editorRace.vault.read.bind(editorRace.vault);
    editorRace.vault.read = async (file) => {
        const content = await originalRead(file);
        const editor = { getValue: () => content };
        editorRace.leaves.push({
            view: Object.assign(new MarkdownView(), { file, editor }),
            getViewState: () => ({ type: "markdown", state: { file: file.path } }),
        });
        return content;
    };
    await assert.rejects(
        () => editorRace.bridge.observeCommit(
            commitObserveRequest(["notes/a.md"]),
            new AbortController().signal,
        ),
        /文件或编辑器在读取期间发生变化/,
    );

    const identityRace = await fixture(t, { "notes/a.md": "after" });
    identityRace.vault.read = async (file) => {
        identityRace.vault.entries.set(file.path, new TFile(file.path, "replacement"));
        return file.content;
    };
    await assert.rejects(
        () => identityRace.bridge.observeCommit(
            commitObserveRequest(["notes/a.md"]),
            new AbortController().signal,
        ),
        /文件或编辑器在读取期间发生变化/,
    );
});

test("create fails closed when an externally deleted path still has a dirty editor", async (t) => {
    const { bridge, vault, leaves } = await fixture(
        t,
        { "notes/new.md": "saved before external delete" },
        { path: "notes/new.md", content: "unsaved editor draft" },
    );
    vault.entries.delete("notes/new.md");
    leaves[0].view.file = null;
    const args = {
        transactionId: "tx_create_orphan_editor",
        operations: [{ op: "create", path: "notes/new.md", content: "worker content", expectedHash: "absent" }],
    };

    await assert.rejects(
        () => bridge.preview(previewRequest(args), new AbortController().signal),
        /路径不存在，但仍由 Obsidian 编辑器占用/,
    );
    assert.equal(leaves[0].view.editor.getValue(), "unsaved editor draft");
    assert.equal(vault.getAbstractFileByPath("notes/new.md"), null);
});

test("preview fails closed when a watcher replaces the file behind an open editor", async (t) => {
    const { bridge, vault, leaves } = await fixture(
        t,
        { "notes/a.md": "before watcher replacement" },
        { path: "notes/a.md", content: "before watcher replacement" },
    );
    vault.entries.set("notes/a.md", new TFile("notes/a.md", "replacement on disk"));
    const args = {
        transactionId: "tx_watcher_identity_change",
        operations: [{
            op: "append",
            path: "notes/a.md",
            content: "+new",
            expectedHash: digest("replacement on disk"),
        }],
    };

    await assert.rejects(
        () => bridge.preview(previewRequest(args), new AbortController().signal),
        /编辑器与当前 Vault 文件身份不一致/,
    );
    assert.equal(leaves[0].view.editor.getValue(), "before watcher replacement");
    assert.equal(vault.getAbstractFileByPath("notes/a.md").content, "replacement on disk");
});

test("rename fails closed when its absent destination still has an orphan editor", async (t) => {
    const { bridge, vault, leaves } = await fixture(
        t,
        { "notes/source.md": "source", "archive/target.md": "deleted destination" },
        { path: "archive/target.md", content: "orphan destination draft" },
    );
    vault.entries.delete("archive/target.md");
    const args = {
        transactionId: "tx_rename_orphan_destination",
        operations: [{
            op: "rename",
            path: "notes/source.md",
            destination: "archive/target.md",
            expectedHash: digest("source"),
            expectedDestinationHash: "absent",
        }],
    };

    await assert.rejects(
        () => bridge.preview(previewRequest(args), new AbortController().signal),
        /路径不存在，但仍由 Obsidian 编辑器占用/,
    );
    assert.equal(leaves[0].view.editor.getValue(), "orphan destination draft");
    assert.equal(vault.getAbstractFileByPath("notes/source.md").content, "source");
    assert.equal(vault.getAbstractFileByPath("archive/target.md"), null);
});

test("trash preview proves the exact Worker-owned source and deterministic archive target", async (t) => {
    const { bridge, vault } = await fixture(t, { "notes/b.md": "trash me" });
    const args = {
        transactionId: "tx_trash_preview",
        operations: [{ op: "trash", path: "notes/b.md", expectedHash: digest("trash me") }],
    };
    const request = previewRequest(args);
    const { canonicalJson } = loadModule("client_invocation_journal.ts");
    const publicArgsHash = digest(canonicalJson({ operations: args.operations }));
    assert.equal(publicArgsHash, "sha256:5e1d37faa6d92f356ff08b8b26c3725162fe36d5d8f82bd261ecf6a489e4de32");
    const suffix = createHash("sha256")
        .update(`${request.runId}:${request.toolCallId}:${publicArgsHash}:0:notes/b.md`, "utf8")
        .digest("hex")
        .slice(0, 20);
    const trashPath = `.trash/offeragent/${suffix}-b.md`;
    assert.equal(trashPath, ".trash/offeragent/2ef6182d2c964d281088-b.md");

    const preview = await bridge.preview(request, new AbortController().signal);

    assert.deepEqual(preview.paths, [trashPath, "notes/b.md"]);
    assert.deepEqual(preview.pathStates, [
        {
            path: trashPath,
            beforeHash: "absent",
            afterHash: digest("trash me"),
            unsavedEditor: false,
            openEditor: false,
        },
        {
            path: "notes/b.md",
            beforeHash: digest("trash me"),
            afterHash: "absent",
            unsavedEditor: false,
            openEditor: false,
        },
    ]);
    assert.match(preview.diff, new RegExp(trashPath.replace(/[.*+?^${}()|[\]\\]/g, "\\$&")));
    assert.equal(vault.getAbstractFileByPath("notes/b.md").content, "trash me");
    assert.equal(vault.getAbstractFileByPath(trashPath), null);

    vault.entries.set(trashPath, new TFile(trashPath, "collision"));
    await assert.rejects(
        () => bridge.preview(request, new AbortController().signal),
        new RegExp(`expectedHash.*${trashPath.replace(/[.*+?^${}()|[\]\\]/g, "\\$&")}`),
    );
});

test("effectful Client Tool invocation is fail-closed and idempotent for every Vault operation", async (t) => {
    const { bridge, vault, leaves } = await fixture(
        t,
        { "notes/a.md": "saved", "notes/b.md": "b" },
        { path: "notes/a.md", content: "draft" },
    );
    const operations = [
        { op: "create", path: "notes/new.md", content: "new", expectedHash: "absent" },
        { op: "append", path: "notes/a.md", content: "x", expectedHash: digest("draft") },
        { op: "replace", path: "notes/a.md", find: "draft", replace: "after", expectedHash: digest("draft") },
        { op: "patch", path: "notes/a.md", edits: [{ startLine: 1, endLine: 1, replacement: "after" }], expectedHash: digest("draft") },
        { op: "rename", path: "notes/b.md", destination: "archive/b.md", expectedHash: digest("b"), expectedDestinationHash: "absent" },
        { op: "trash", path: "notes/b.md", expectedHash: digest("b") },
    ];
    for (let index = 0; index < operations.length; index += 1) {
        const args = { transactionId: `tx_${index + 1}`, operations: [operations[index]] };
        const params = invocation(args, { invocationId: `inv_${index + 1}`, toolCallId: `call_${index + 1}` });
        const first = await bridge.invoke(params, new AbortController().signal);
        const replay = await bridge.invoke(params, new AbortController().signal);
        assert.equal(first.status, "failed");
        assert.equal(first.error.code, "client_vault_mutation_worker_owned");
        assert.deepEqual(replay, first);
    }
    assert.equal(leaves[0].view.editor.getValue(), "draft");
    assert.equal(vault.getAbstractFileByPath("notes/a.md").content, "saved");
    assert.equal(vault.getAbstractFileByPath("notes/b.md").content, "b");
    assert.equal(vault.getAbstractFileByPath("notes/new.md"), null);
    assert.equal(vault.getAbstractFileByPath("archive/b.md"), null);
});

test("args hash mismatch is rejected before the Worker-owned mutation boundary", async (t) => {
    const { bridge, vault } = await fixture(t, { "notes/a.md": "original" });
    const args = {
        transactionId: "tx_bad_hash",
        operations: [{ op: "append", path: "notes/a.md", content: "x", expectedHash: digest("original") }],
    };
    const result = await bridge.invoke(
        invocation(args, { argsHash: `sha256:${"f".repeat(64)}` }),
        new AbortController().signal,
    );
    assert.equal(result.status, "failed");
    assert.equal(result.error.code, "args_hash_mismatch");
    assert.equal(vault.getAbstractFileByPath("notes/a.md").content, "original");
});

test("lookup reconciles an ACK-loss started journal from file after-hashes", async (t) => {
    const { bridge, journal } = await fixture(t, { "notes/a.md": "after" });
    const requestHash = `sha256:${"a".repeat(64)}`;
    await journal.begin({
        invocationId: "inv_01", toolCallId: "call_01", runId: "run_01", requestHash,
        recovery: [{ path: "notes/a.md", beforeHash: digest("before"), afterHash: digest("after") }],
    });
    const lookup = await bridge.lookup({ invocationId: "inv_01", runId: "run_01" }, new AbortController().signal);
    assert.equal(lookup.found, true);
    assert.equal(lookup.result.status, "succeeded");
    assert.equal(lookup.result.sideEffectFacts[0].state, "committed");
    const replay = await bridge.lookup({ invocationId: "inv_01", runId: "run_01" }, new AbortController().signal);
    assert.deepEqual(replay, lookup);
});

test("lookup never turns a crash-window empty recovery record into success", async (t) => {
    const { bridge, journal, vault } = await fixture(t, { "notes/a.md": "unchanged" });
    const requestHash = `sha256:${"b".repeat(64)}`;
    await journal.begin({
        invocationId: "inv_crash_lookup",
        toolCallId: "call_crash_lookup",
        runId: "run_01",
        requestHash,
        recovery: [],
    });

    const lookup = await bridge.lookup(
        { invocationId: "inv_crash_lookup", runId: "run_01" },
        new AbortController().signal,
    );

    assert.equal(lookup.found, true);
    assert.equal(lookup.result.invocationId, "inv_crash_lookup");
    assert.equal(lookup.result.toolCallId, "call_crash_lookup");
    assert.equal(lookup.result.status, "failed");
    assert.equal(lookup.result.error.code, "interrupted_before_apply");
    assert.deepEqual(lookup.result.sideEffectFacts, []);
    assert.equal(vault.getAbstractFileByPath("notes/a.md").content, "unchanged");
    const replay = await bridge.lookup(
        { invocationId: "inv_crash_lookup", runId: "run_01" },
        new AbortController().signal,
    );
    assert.deepEqual(replay, lookup);
});

test("invoke binds crash recovery to the exact durable request and empty recovery fails", async (t) => {
    const { bridge, journal, vault } = await fixture(t, { "notes/a.md": "unchanged" });
    const args = {
        transactionId: "tx_crash_invoke",
        operations: [{
            op: "append",
            path: "notes/a.md",
            content: "+new",
            expectedHash: digest("unchanged"),
        }],
    };
    const params = invocation(args, {
        invocationId: "inv_crash_invoke",
        toolCallId: "call_crash_invoke",
    });
    const requestHash = invocationRequestHash(params);
    await journal.begin({
        invocationId: params.invocationId,
        toolCallId: params.toolCallId,
        runId: params.runId,
        requestHash,
        recovery: [],
    });

    await assert.rejects(
        () => bridge.invoke(
            { ...params, toolCallId: "call_different_request" },
            new AbortController().signal,
        ),
        /identity\/request binding changed/,
    );
    assert.equal((await journal.lookup(params.invocationId)).state, "started");

    const result = await bridge.invoke(params, new AbortController().signal);
    assert.equal(result.invocationId, params.invocationId);
    assert.equal(result.toolCallId, params.toolCallId);
    assert.equal(result.status, "failed");
    assert.equal(result.error.code, "interrupted_before_apply");
    assert.deepEqual(result.sideEffectFacts, []);
    assert.equal(vault.getAbstractFileByPath("notes/a.md").content, "unchanged");
    assert.deepEqual(await bridge.invoke(params, new AbortController().signal), result);
});

test("durable non-empty recovery reconciles only for its exact request", async (t) => {
    const { bridge, journal } = await fixture(t, { "notes/a.md": "after" });
    const args = {
        transactionId: "tx_legacy_recovery",
        operations: [{
            op: "append",
            path: "notes/a.md",
            content: "after",
            expectedHash: digest("before"),
        }],
    };
    const params = invocation(args, {
        invocationId: "inv_legacy_recovery",
        toolCallId: "call_legacy_recovery",
    });
    const requestHash = invocationRequestHash(params);
    await journal.begin({
        invocationId: params.invocationId,
        toolCallId: params.toolCallId,
        runId: params.runId,
        requestHash,
        recovery: [{ path: "notes/a.md", beforeHash: digest("before"), afterHash: digest("after") }],
    });

    await assert.rejects(
        () => bridge.invoke(
            { ...params, idempotencyKey: "idem_different_request" },
            new AbortController().signal,
        ),
        /identity\/request binding changed/,
    );
    assert.equal((await journal.lookup(params.invocationId)).state, "started");

    const result = await bridge.invoke(params, new AbortController().signal);
    assert.equal(result.status, "succeeded");
    assert.equal(result.invocationId, params.invocationId);
    assert.equal(result.toolCallId, params.toolCallId);
});

test("journal rejects equal before/after recovery hashes on load", async (t) => {
    const { journal, journalPath } = await fixture(t, { "notes/a.md": "same" });
    const sameHash = digest("same");
    const { canonicalJson } = loadModule("client_invocation_journal.ts");
    await writeFile(journalPath, canonicalJson({
        records: [{
            schemaVersion: 1,
            invocationId: "inv_invalid_recovery",
            toolCallId: "call_invalid_recovery",
            runId: "run_01",
            requestHash: `sha256:${"d".repeat(64)}`,
            state: "started",
            startedAt: new Date().toISOString(),
            recovery: [{ path: "notes/a.md", beforeHash: sameHash, afterHash: sameHash }],
        }],
        schemaVersion: 1,
        workspaceInstanceId: "wsi_01",
    }) + "\n", "utf8");

    await assert.rejects(
        () => journal.lookup("inv_invalid_recovery"),
        /before\/after hashes must differ/,
    );
});

test("cancel never claims an effectful plugin invocation was active", async (t) => {
    const { bridge } = await fixture(t);
    const result = await bridge.cancel(
        { invocationId: "inv_missing", runId: "run_01", reason: "turn cancelled" },
        new AbortController().signal,
    );
    assert.deepEqual(result, { invocationId: "inv_missing", accepted: false, alreadyTerminal: false });
});
