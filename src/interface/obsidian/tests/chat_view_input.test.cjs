const assert = require("node:assert/strict");
const { readFileSync } = require("node:fs");
const path = require("node:path");
const test = require("node:test");
const { buildSync } = require("esbuild");

const WORKSPACE = "ws_12345678-1234-1234-1234-123456789abc";

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

function arrayBuffer(bytes) {
    return Uint8Array.from(bytes).buffer;
}

function source(name, bytes) {
    const data = arrayBuffer(bytes);
    return {
        name,
        size: data.byteLength,
        async arrayBuffer() { return data.slice(0); },
    };
}

function memoryVault(initialFiles = new Map()) {
    const files = new Map([...initialFiles].map(([filePath, data]) => [filePath, data.slice(0)]));
    const folders = new Set();
    let creates = 0;
    return {
        files,
        get creates() { return creates; },
        getFolderByPath(folderPath) { return folders.has(folderPath) ? { path: folderPath } : null; },
        getFileByPath(filePath) { return files.has(filePath) ? { path: filePath } : null; },
        getAbstractFileByPath(itemPath) {
            if (files.has(itemPath)) return { path: itemPath };
            if (folders.has(itemPath)) return { path: itemPath };
            return null;
        },
        async createFolder(folderPath) {
            if (folders.has(folderPath) || files.has(folderPath)) throw new Error("already exists");
            folders.add(folderPath);
        },
        async createBinary(filePath, data) {
            if (files.has(filePath) || folders.has(filePath)) throw new Error("already exists");
            files.set(filePath, data.slice(0));
            creates += 1;
            return { path: filePath };
        },
        async readBinary(file) { return files.get(file.path).slice(0); },
    };
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

test("timeline renders a submitted user message before its durable turn event arrives", () => {
    const source = readFileSync(path.join(__dirname, "../src/local/chat_view.ts"), "utf8");

    assert.match(source, /snapshot\.pendingSubmissions\.filter/);
    assert.match(source, /renderPendingSubmission/);
    assert.match(source, /正在提交给 OfferAgent/);
});

test("timeline renders ordered durable items instead of legacy grouped cards", () => {
    const source = readFileSync(path.join(__dirname, "../src/local/chat_view.ts"), "utf8");

    assert.match(source, /for \(const item of run\.timeline\)/);
    assert.match(source, /case "reasoning"/);
    assert.match(source, /case "tool_call"/);
    assert.match(source, /case "subagent"/);
});

test("attachment media type is derived from verified bytes rather than the file name", () => {
    const { detectAttachmentMediaType } = loadModule();

    assert.equal(detectAttachmentMediaType(arrayBuffer([0x25, 0x50, 0x44, 0x46, 0x2d])), "application/pdf");
    assert.equal(detectAttachmentMediaType(arrayBuffer([
        0x89, 0x50, 0x4e, 0x47, 0x0d, 0x0a, 0x1a, 0x0a,
    ])), "image/png");
    assert.equal(detectAttachmentMediaType(arrayBuffer([0xff, 0xd8, 0xff, 0x00])), "image/jpeg");
    assert.equal(detectAttachmentMediaType(arrayBuffer([
        0x52, 0x49, 0x46, 0x46, 0, 0, 0, 0, 0x57, 0x45, 0x42, 0x50,
    ])), "image/webp");
    assert.throws(() => detectAttachmentMediaType(arrayBuffer([1, 2, 3])), /只支持内容有效/);
});

test("external attachment import creates one content-addressed Vault-relative file", async () => {
    const { importManagedAttachment } = loadModule();
    const vault = memoryVault();
    const pdf = source("misleading.png", [0x25, 0x50, 0x44, 0x46, 0x2d, 0x31, 0x2e, 0x37]);

    const first = await importManagedAttachment(vault, pdf, WORKSPACE);
    assert.equal(first.mediaType, "application/pdf");
    assert.equal(first.file.workspaceId, WORKSPACE);
    assert.match(first.file.contentHash, /^sha256:[0-9a-f]{64}$/);
    assert.equal(
        first.file.path,
        `OfferAgent/Attachments/${first.file.contentHash.slice("sha256:".length)}.pdf`,
    );
    assert.equal(first.file.path.includes("\\"), false);
    assert.equal(vault.creates, 1);

    const second = await importManagedAttachment(vault, source("same.pdf", [
        0x25, 0x50, 0x44, 0x46, 0x2d, 0x31, 0x2e, 0x37,
    ]), WORKSPACE);
    assert.equal(second.file.path, first.file.path);
    assert.equal(vault.creates, 1);
});

test("an existing content-addressed target is never overwritten when verification fails", async () => {
    const { importManagedAttachment, sha256Digest } = loadModule();
    const bytes = arrayBuffer([0x89, 0x50, 0x4e, 0x47, 0x0d, 0x0a, 0x1a, 0x0a, 1]);
    const hash = sha256Digest(bytes);
    const target = `OfferAgent/Attachments/${hash.slice("sha256:".length)}.png`;
    const corrupted = arrayBuffer([0x89, 0x50, 0x4e, 0x47, 0x0d, 0x0a, 0x1a, 0x0a, 2]);
    const vault = memoryVault(new Map([[target, corrupted]]));

    await assert.rejects(
        importManagedAttachment(vault, {
            name: "shot.png",
            size: bytes.byteLength,
            async arrayBuffer() { return bytes.slice(0); },
        }, WORKSPACE),
        /校验不一致/,
    );
    assert.deepEqual(new Uint8Array(vault.files.get(target)), new Uint8Array(corrupted));
    assert.equal(vault.creates, 0);
});

test("composer exposes picker, drag/drop, paste, cards, and removal wiring", () => {
    const sourceText = readFileSync(path.join(__dirname, "../src/local/chat_view.ts"), "utf8");

    assert.match(sourceText, /setIcon\(attach, "paperclip"\)/);
    assert.match(sourceText, /composer\.ondrop/);
    assert.match(sourceText, /input\.onpaste/);
    assert.match(sourceText, /application\/pdf,image\/png,image\/jpeg,image\/webp/);
    assert.match(sourceText, /offeragent-attachment-card/);
    assert.match(sourceText, /removeDraftAttachment/);
});
