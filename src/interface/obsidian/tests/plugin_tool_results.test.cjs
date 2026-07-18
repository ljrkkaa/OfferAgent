const assert = require("node:assert/strict");
const path = require("node:path");
const test = require("node:test");
const { buildSync } = require("esbuild");

function loadModule() {
    const output = buildSync({
        entryPoints: [path.join(__dirname, "../src/runtime/plugin_tool_results.ts")],
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

test("shared plugin Tool result builders preserve one typed success/failure contract", () => {
    const { failed, hasExtraKeys, succeeded } = loadModule();
    const call = { toolCallId: "call_shared" };
    const sourceRefs = [{ type: "web", url: "https://example.com", title: "Example", freshness: "fresh" }];

    assert.deepEqual(succeeded(call, "Read evidence.", { count: 1 }, sourceRefs), {
        toolCallId: "call_shared",
        status: "succeeded",
        summary: "Read evidence.",
        data: { count: 1 },
        sourceRefs,
        retryable: false,
    });
    assert.deepEqual(failed(call, "policy.denied", "Denied.", false, "denied"), {
        toolCallId: "call_shared",
        status: "denied",
        summary: "Denied.",
        data: {},
        retryable: false,
        error: {
            code: "policy.denied",
            retryable: false,
            cancelled: false,
            userVisibleMessage: "Denied.",
            details: {},
        },
    });
    assert.equal(hasExtraKeys({ action: "read" }, ["action"]), false);
    assert.equal(hasExtraKeys({ action: "read", write: true }, ["action"]), true);
});
