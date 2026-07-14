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
