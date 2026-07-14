const assert = require("node:assert/strict");
const path = require("node:path");
const test = require("node:test");
const { buildSync } = require("esbuild");

function loadModule() {
    const output = buildSync({
        entryPoints: [path.join(__dirname, "../src/runtime/source_references.ts")],
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

test("Vault reference helpers use protocol file.path and preserve line targets", () => {
    const {
        mergeSourceReferences,
        sourceReferenceArray,
        sourceReferenceLabel,
        vaultReferenceTarget,
    } = loadModule();
    const raw = [{
        type: "vault",
        file: {
            workspaceId: "wsi_01J00000000000000000000000",
            path: "notes/Agent.md",
            lineStart: 4,
            lineEnd: 8,
            heading: "Architecture",
        },
        freshness: "stale_partial",
    }];
    const references = sourceReferenceArray(raw);
    assert.equal(sourceReferenceLabel(references[0]), "Architecture:4-8");
    assert.deepEqual(vaultReferenceTarget(references[0]), {
        path: "notes/Agent.md",
        lineStart: 4,
        lineEnd: 8,
        heading: "Architecture",
    });

    const refreshed = sourceReferenceArray([{ ...raw[0], freshness: "fresh", label: "已刷新" }]);
    const merged = mergeSourceReferences(references, refreshed);
    assert.equal(merged.length, 1);
    assert.equal(merged[0].freshness, "fresh");
    assert.equal(sourceReferenceLabel(merged[0]), "已刷新:4-8");
});
