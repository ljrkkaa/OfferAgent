const assert = require("node:assert/strict");
const { mkdir, mkdtemp, readFile, rm, symlink, writeFile } = require("node:fs/promises");
const { tmpdir } = require("node:os");
const path = require("node:path");
const test = require("node:test");
const { buildSync } = require("esbuild");

function loadModule() {
    const output = buildSync({
        entryPoints: [path.join(__dirname, "../src/runtime/workspace_identity.ts")],
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

async function fixture(t) {
    const root = await mkdtemp(path.join(tmpdir(), "offeragent-workspace-"));
    t.after(() => rm(root, { recursive: true, force: true }));
    return root;
}

test("workspace identity is atomically created and reused without Runtime state in the Vault", async (t) => {
    const { loadOrCreatePortableWorkspaceIdentity, readPortableWorkspaceIdentity } = loadModule();
    const root = await fixture(t);
    const uuid = "12345678-1234-4234-9234-123456789abc";

    const first = await loadOrCreatePortableWorkspaceIdentity(root, () => uuid);
    const second = await loadOrCreatePortableWorkspaceIdentity(root, () => "ffffffff-ffff-4fff-8fff-ffffffffffff");

    assert.deepEqual(first, { schemaVersion: 1, portableWorkspaceId: `ws_${uuid}` });
    assert.deepEqual(second, first);
    assert.deepEqual(await readPortableWorkspaceIdentity(root), first);
    assert.equal(
        await readFile(path.join(root, ".offeragent", "workspace.json"), "utf8"),
        `{"portableWorkspaceId":"ws_${uuid}","schemaVersion":1}\n`,
    );
    assert.deepEqual((await require("node:fs/promises").readdir(path.join(root, ".offeragent"))), ["workspace.json"]);
});

test("concurrent creators converge on exactly one valid identity", async (t) => {
    const { loadOrCreatePortableWorkspaceIdentity } = loadModule();
    const root = await fixture(t);
    const values = await Promise.all([
        loadOrCreatePortableWorkspaceIdentity(root, () => "11111111-1111-4111-8111-111111111111"),
        loadOrCreatePortableWorkspaceIdentity(root, () => "22222222-2222-4222-8222-222222222222"),
    ]);
    assert.deepEqual(values[0], values[1]);
});

test("corrupt identity is never overwritten or silently regenerated", async (t) => {
    const { loadOrCreatePortableWorkspaceIdentity } = loadModule();
    const root = await fixture(t);
    await mkdir(path.join(root, ".offeragent"));
    const file = path.join(root, ".offeragent", "workspace.json");
    await writeFile(file, '{"portableWorkspaceId":"ws_bad","schemaVersion":1}\n');

    await assert.rejects(() => loadOrCreatePortableWorkspaceIdentity(root), /schema is invalid/);
    assert.equal(await readFile(file, "utf8"), '{"portableWorkspaceId":"ws_bad","schemaVersion":1}\n');
});

test(
    "symlinked configuration directory is rejected",
    { skip: process.platform === "win32" ? "Windows symlink creation requires Developer Mode" : false },
    async (t) => {
        const { loadOrCreatePortableWorkspaceIdentity } = loadModule();
        const root = await fixture(t);
        const outside = await fixture(t);
        await symlink(outside, path.join(root, ".offeragent"), "dir");
        await assert.rejects(() => loadOrCreatePortableWorkspaceIdentity(root), /real directory/);
    },
);
