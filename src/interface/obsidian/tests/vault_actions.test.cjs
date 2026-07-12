const assert = require("node:assert/strict");
const path = require("node:path");
const test = require("node:test");
const { buildSync } = require("esbuild");

class TFile {
    constructor(path, content = "") {
        this.path = path;
        this.name = path.split("/").at(-1);
        this.basename = this.name.replace(/\.[^.]+$/, "");
        this.extension = this.name.split(".").at(-1);
        this.content = content;
    }
}

class TFolder {
    constructor(path) {
        this.path = path;
        this.children = [];
    }
}

function loadModule() {
    const output = buildSync({
        entryPoints: [path.join(__dirname, "../src/interact_with_files.ts")],
        bundle: true,
        format: "cjs",
        platform: "node",
        write: false,
        external: ["obsidian"],
    }).outputFiles[0].text;
    const compiled = { exports: {} };
    const fakeRequire = (id) => id === "obsidian" ? { MarkdownView: class {}, TFile, TFolder } : require(id);
    new Function("require", "module", "exports", output)(fakeRequire, compiled, compiled.exports);
    return compiled.exports;
}

class FakeVault {
    constructor(files = {}) {
        this.entries = new Map(Object.entries(files).map(([filePath, content]) => [filePath, new TFile(filePath, content)]));
        this.failCreatePath = null;
        this.beforeProcess = null;
        this.onCreateFailure = null;
    }

    getAbstractFileByPath(filePath) {
        return this.entries.get(filePath) ?? null;
    }

    async read(file) {
        return file.content;
    }

    async modify(file, content) {
        file.content = content;
    }

    async process(file, transform) {
        if (this.beforeProcess) {
            const hook = this.beforeProcess;
            this.beforeProcess = null;
            hook(file);
        }
        file.content = transform(file.content);
        return file.content;
    }

    async create(filePath, content) {
        if (filePath === this.failCreatePath) {
            if (this.onCreateFailure) this.onCreateFailure();
            throw new Error("injected create failure");
        }
        const file = new TFile(filePath, content);
        this.entries.set(filePath, file);
        return file;
    }

    async createFolder(folderPath) {
        const folder = new TFolder(folderPath);
        this.entries.set(folderPath, folder);
        const parentPath = folderPath.split("/").slice(0, -1).join("/");
        const parent = this.entries.get(parentPath);
        if (parent instanceof TFolder) parent.children.push(folder);
        return folder;
    }

    async delete(entry) {
        this.entries.delete(entry.path);
    }
}

function createInteractions(files = {}) {
    const { FileInteractions } = loadModule();
    const vault = new FakeVault(files);
    const app = { vault, workspace: { getLeavesOfType: () => [] } };
    return { interactions: new FileInteractions(app), vault };
}

test("vault actions require the complete structured schema", () => {
    const { parseVaultActions } = loadModule();

    assert.equal(parseVaultActions({ actions: [{ op: "append_file", path: "notes.md" }] }), null);
    assert.equal(parseVaultActions({ actions: [{ op: "replace_text", path: "notes.md", find: "", replace: "x", mode: "replace" }] }), null);
    assert.equal(parseVaultActions({ actions: [{ op: "create_file", path: "new.md", content: "x", mode: "append" }] }), null);
    assert.equal(parseVaultActions({ actions: [{ op: "create_file", path: "new.md", content: "x", mode: "create_only", extra: true }] }), null);
    assert.equal(parseVaultActions({ actions: [], extra: true }), null);
    assert.deepEqual(parseVaultActions({
        actions: [{ op: "create_file", path: "daily/new.md", content: "# New\n", mode: "create_only" }],
    }), [{ op: "create_file", path: "daily/new.md", content: "# New\n", mode: "create_only" }]);
});

test("vault action review exposes the complete write payload", () => {
    const { vaultActionReview } = loadModule();

    assert.deepEqual(
        vaultActionReview({ op: "create_file", path: "new.md", content: "# New\n", mode: "create_only" }),
        { summary: "create_file: new.md", details: "Content:\n# New\n" },
    );
    assert.deepEqual(
        vaultActionReview({
            op: "append_file",
            path: "notes.md",
            content: "- Redis",
            heading: "Interview",
            mode: "append",
        }),
        { summary: "append_file: notes.md", details: "Heading: Interview\n\nContent:\n- Redis" },
    );
    assert.deepEqual(
        vaultActionReview({
            op: "replace_text",
            path: "notes.md",
            find: "draft",
            replace: "final",
            reason: "approve answer",
            mode: "replace",
        }),
        {
            summary: "replace_text: notes.md",
            details: "Reason: approve answer\n\nFind:\ndraft\n\nReplace with:\nfinal",
        },
    );
});

test("vault action batches are atomic when validation fails", async () => {
    const { interactions, vault } = createInteractions({ "notes.md": "# Notes\n" });

    const results = await interactions.applyVaultActions([
        { op: "append_file", path: "notes.md", content: "first", mode: "append" },
        { op: "replace_text", path: "missing.md", find: "x", replace: "y", mode: "replace" },
    ]);

    assert.deepEqual(results.map((result) => result.success), [false, false]);
    assert.deepEqual(results.map((result) => result.status), ["not_applied", "not_applied"]);
    assert.equal(vault.getAbstractFileByPath("notes.md").content, "# Notes\n");
});

test("an unsafe path rejects the whole batch", async () => {
    const { interactions, vault } = createInteractions({ "notes.md": "# Notes\n" });

    const results = await interactions.applyVaultActions([
        { op: "create_file", path: "../escape.md", content: "escape", mode: "create_only" },
        { op: "append_file", path: "notes.md", content: "must not be written", mode: "append" },
    ]);

    assert.deepEqual(results.map((result) => result.success), [false, false]);
    assert.equal(vault.getAbstractFileByPath("notes.md").content, "# Notes\n");
});

test("failed nested creates preserve new folders for manual review", async () => {
    const { interactions, vault } = createInteractions();
    vault.failCreatePath = "daily/new.md";

    const results = await interactions.applyVaultActions([
        { op: "create_file", path: "daily/new.md", content: "# New\n", mode: "create_only" },
    ]);

    assert.equal(results[0].success, false);
    assert.equal(results[0].status, "manual_review_required");
    assert.ok(vault.getAbstractFileByPath("daily") instanceof TFolder);
    assert.equal(vault.getAbstractFileByPath("daily/new.md"), null);
});

test("valid actions are applied in order", async () => {
    const { interactions, vault } = createInteractions({ "notes.md": "# Notes\n" });

    const results = await interactions.applyVaultActions([
        { op: "append_file", path: "notes.md", content: "draft", mode: "append" },
        { op: "replace_text", path: "notes.md", find: "draft", replace: "final", mode: "replace" },
        { op: "create_file", path: "daily/new.md", content: "# New\n", mode: "create_only" },
    ]);

    assert.deepEqual(results.map((result) => result.success), [true, true, true]);
    assert.deepEqual(results.map((result) => result.status), ["applied", "applied", "applied"]);
    assert.equal(vault.getAbstractFileByPath("notes.md").content, "# Notes\nfinal\n");
    assert.equal(vault.getAbstractFileByPath("daily/new.md").content, "# New\n");
});

test("concurrent edits are detected atomically and preserved", async () => {
    const { interactions, vault } = createInteractions({ "notes.md": "# Notes\n" });
    vault.beforeProcess = (file) => { file.content = "# User edit\n"; };

    const results = await interactions.applyVaultActions([
        { op: "append_file", path: "notes.md", content: "agent edit", mode: "append" },
    ]);

    assert.equal(results[0].success, false);
    assert.equal(results[0].status, "not_applied");
    assert.match(results[0].error, /changed before apply/);
    assert.equal(vault.getAbstractFileByPath("notes.md").content, "# User edit\n");
});

test("rollback preserves edits made after the batch write", async () => {
    const { interactions, vault } = createInteractions({ "notes.md": "# Notes\n" });
    vault.failCreatePath = "daily/new.md";
    vault.onCreateFailure = () => {
        vault.getAbstractFileByPath("notes.md").content = "# Concurrent edit\n";
    };

    const results = await interactions.applyVaultActions([
        { op: "append_file", path: "notes.md", content: "agent edit", mode: "append" },
        { op: "create_file", path: "daily/new.md", content: "# New\n", mode: "create_only" },
    ]);

    assert.deepEqual(results.map((result) => result.success), [false, false]);
    assert.deepEqual(results.map((result) => result.status), ["manual_review_required", "manual_review_required"]);
    assert.match(results[0].error, /rollback conflict/);
    assert.equal(vault.getAbstractFileByPath("notes.md").content, "# Concurrent edit\n");
});

test("rollback never deletes files created by a partially applied batch", async () => {
    const { interactions, vault } = createInteractions();
    vault.failCreatePath = "second.md";

    const results = await interactions.applyVaultActions([
        { op: "create_file", path: "first.md", content: "created by agent\n", mode: "create_only" },
        { op: "create_file", path: "second.md", content: "fails\n", mode: "create_only" },
    ]);

    assert.deepEqual(results.map((result) => result.status), ["manual_review_required", "manual_review_required"]);
    assert.equal(vault.getAbstractFileByPath("first.md").content, "created by agent\n");
    assert.match(results[0].error, /manual review required/);
});
