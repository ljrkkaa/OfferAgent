const assert = require("node:assert/strict");
const { mkdtemp, mkdir, readFile, rm, writeFile } = require("node:fs/promises");
const os = require("node:os");
const path = require("node:path");
const test = require("node:test");
const { buildSync } = require("esbuild");

function loadModule() {
    const output = buildSync({
        entryPoints: [path.join(__dirname, "../src/qualification_vault.ts")],
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

test("qualification Vault presents stable Obsidian reads and conditional writes", async (t) => {
    const root = await mkdtemp(path.join(os.tmpdir(), "offeragent-qualification-vault-"));
    t.after(async () => rm(root, { recursive: true, force: true }));
    await mkdir(path.join(root, "notes"));
    await writeFile(path.join(root, "notes", "source.md"), "source\n", "utf8");
    const { QualificationVaultPort } = loadModule();
    const vault = new QualificationVaultPort(root);

    const files = vault.getFiles();
    assert.deepEqual(files.map((file) => file.path), ["notes/source.md"]);
    assert.equal(files[0].extension, "md");
    assert.equal(await vault.cachedRead(files[0]), "source\n");
    const missing = await vault.snapshot("notes/result.md");
    assert.deepEqual(missing, { content: undefined, modifiedVersion: "missing" });

    const outcome = await vault.applyConditional({
        kind: "create",
        path: "notes/result.md",
        afterContent: "result\n",
        expected: { contentHash: "absent", modifiedVersion: "missing" },
    });
    assert.equal(outcome.status, "applied");
    assert.equal(await readFile(path.join(root, "notes", "result.md"), "utf8"), "result\n");

    const conflict = await vault.applyConditional({
        kind: "create",
        path: "notes/result.md",
        afterContent: "other\n",
        expected: { contentHash: "absent", modifiedVersion: "missing" },
    });
    assert.equal(conflict.status, "conflict");
});

test("qualification Vault rejects paths outside its owned root", async (t) => {
    const root = await mkdtemp(path.join(os.tmpdir(), "offeragent-qualification-vault-path-"));
    t.after(async () => rm(root, { recursive: true, force: true }));
    const { QualificationVaultPort } = loadModule();
    const vault = new QualificationVaultPort(root);

    assert.throws(() => vault.getFileByPath("../outside.md"), /safe relative Vault path/u);
    await assert.rejects(() => vault.snapshot("C:/outside.md"), /safe relative Vault path/u);
});
