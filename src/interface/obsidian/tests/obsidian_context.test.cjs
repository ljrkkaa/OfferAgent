const assert = require("node:assert/strict");
const { createHash } = require("node:crypto");
const path = require("node:path");
const test = require("node:test");
const { buildSync } = require("esbuild");

class TFile {
    constructor(path) { this.path = path; }
}
class MarkdownView {}

function loadModule() {
    const output = buildSync({
        entryPoints: [path.join(__dirname, "../src/local/obsidian_context.ts")],
        bundle: true,
        format: "cjs",
        platform: "node",
        target: "node16",
        external: ["obsidian"],
        write: false,
    }).outputFiles[0].text;
    const compiled = { exports: {} };
    const fakeRequire = (id) => id === "obsidian" ? { MarkdownView, TFile } : require(id);
    new Function("require", "module", "exports", output)(fakeRequire, compiled, compiled.exports);
    return compiled.exports;
}

function fixture({ saved = "saved", live = "unsaved", selection = "select" } = {}) {
    const file = new TFile("notes/a.md");
    const editor = {
        getValue: () => live,
        getSelection: () => selection,
        getCursor: (which) => which === "from" ? { line: 0, ch: 1 } : which === "to" ? { line: 0, ch: 3 } : { line: 0, ch: 2 },
        posToOffset: (position) => position.ch,
    };
    const view = Object.assign(new MarkdownView(), { file, editor });
    const app = {
        workspace: {
            getActiveViewOfType: (type) => type === MarkdownView ? view : null,
            getActiveFile: () => file,
        },
        vault: { cachedRead: async () => saved },
        metadataCache: {
            getFileCache: () => ({
                frontmatter: { company: "Acme", tags: ["interview"], position: { start: 0 } },
                tags: [{ tag: "#offer" }],
                links: [{ link: "Company" }, { link: "Missing" }],
            }),
            getFirstLinkpathDest: (link) => link === "Company" ? new TFile("notes/company.md") : null,
            resolvedLinks: {
                "notes/source.md": { "notes/a.md": 2 },
                "notes/other.md": { "notes/company.md": 1 },
            },
            unresolvedLinks: { "notes/a.md": { Missing: 1 } },
        },
    };
    return { app, file };
}

test("context captures the bounded unsaved editor snapshot without an absolute path", async () => {
    const { ObsidianContextBridge } = loadModule();
    const { app, file } = fixture();
    const bridge = new ObsidianContextBridge(app);
    bridge.noteFileChanged(file.path);
    bridge.noteEditorChanged();
    bridge.noteMetadataChanged(file);
    const context = await bridge.capture();

    assert.equal(context.activeFile, "notes/a.md");
    assert.equal(context.activeFileHash, `sha256:${createHash("sha256").update("unsaved").digest("hex")}`);
    assert.equal(context.hasUnsavedChanges, true);
    assert.deepEqual(context.selection, { text: "select", anchorOffset: 1, headOffset: 3 });
    assert.equal(context.cursorOffset, 2);
    assert.equal(context.metadataCacheRevision, 1);
    assert.equal(context.activeFileRevision, 3);
    assert.deepEqual(context.metadata, {
        frontmatter: { company: "Acme", tags: ["interview"] },
        tags: ["#interview", "#offer"],
        links: ["notes/company.md"],
        unresolvedLinks: ["Missing"],
    });
    assert.deepEqual(context.backlinks, [{ path: "notes/source.md", count: 2 }]);
});

test("context reverse request validates exact fields and deadline", async () => {
    const { ObsidianContextBridge } = loadModule();
    const bridge = new ObsidianContextBridge(fixture({ saved: "same", live: "same" }).app);
    const params = {
        requestId: "req_01",
        runId: "run_01",
        fields: ["activeFile", "unsavedState"],
        deadline: new Date(Date.now() + 10_000).toISOString(),
    };
    const result = await bridge.handleContextGet(params, new AbortController().signal);
    assert.equal(result.context.activeFile, "notes/a.md");
    assert.equal(result.context.hasUnsavedChanges, false);

    await assert.rejects(
        () => bridge.handleContextGet({ ...params, extra: true }, new AbortController().signal),
        /unexpected fields/,
    );
    await assert.rejects(
        () => bridge.handleContextGet({ ...params, deadline: "2020-01-01T00:00:00Z" }, new AbortController().signal),
        (error) => error.name === "TimeoutError",
    );
});

test("oversized selection is omitted rather than crossing the protocol limit", async () => {
    const { ObsidianContextBridge } = loadModule();
    const bridge = new ObsidianContextBridge(fixture({ selection: "x".repeat(262_145) }).app);
    const context = await bridge.capture(["selection"]);
    assert.equal(context.selection, null);
    assert.equal(context.selectionRevision, null);
});

test("context returns only requested metadata fields and rejects unsafe cached values", async () => {
    const { ObsidianContextBridge } = loadModule();
    const { app } = fixture();
    const bridge = new ObsidianContextBridge(app);
    const activeOnly = await bridge.capture(["activeFile"]);
    assert.equal(activeOnly.metadata, null);
    assert.equal(activeOnly.backlinks, null);
    assert.equal(activeOnly.metadataCacheRevision, null);

    app.metadataCache.getFileCache = () => ({ frontmatter: { invalid: Number.NaN } });
    await assert.rejects(() => bridge.capture(["metadata"]), /non-finite/);
});

test("unsafe Vault-relative paths fail closed to an empty context", async () => {
    const { ObsidianContextBridge } = loadModule();
    const fixtureValue = fixture();
    fixtureValue.file.path = "../outside.md";
    const bridge = new ObsidianContextBridge(fixtureValue.app);
    const context = await bridge.capture();
    assert.equal(context.activeFile, null);
    assert.equal(context.hasUnsavedChanges, false);
});
