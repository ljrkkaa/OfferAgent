const assert = require("node:assert/strict");
const path = require("node:path");
const test = require("node:test");
const { buildSync } = require("esbuild");

function loadModule(entry) {
    const output = buildSync({
        entryPoints: [path.join(__dirname, `../src/runtime/${entry}`)],
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

test("strict JSON rejects literal and escaped duplicate object members", () => {
    const { parseStrictJson } = loadModule("strict_json.ts");
    assert.throws(() => parseStrictJson('{"a":1,"a":2}'), /duplicate/);
    assert.throws(() => parseStrictJson('{"a":1,"\\u0061":2}'), /duplicate/);
    assert.throws(() => parseStrictJson('{"outer":{"x":1,"x":2}}'), /duplicate/);
});

test("strict JSON accepts the complete bounded JSON value grammar", () => {
    const { parseStrictJson } = loadModule("strict_json.ts");
    assert.deepEqual(parseStrictJson('{"a":[true,false,null,-1.25e2,"你\\n好"],"b":{}}'), {
        a: [true, false, null, -125, "你\n好"],
        b: {},
    });
    const protectedKey = parseStrictJson('{"__proto__":{"polluted":true}}');
    assert.equal(Object.prototype.polluted, undefined);
    assert.deepEqual(protectedKey.__proto__, { polluted: true });
});

test("strict JSON rejects malformed, unbounded and non-finite documents", () => {
    const { parseStrictJson } = loadModule("strict_json.ts");
    for (const value of ["", "01", "1.", "1e", "1e400", "true false", '[1,]', '{"a":1,}']) {
        assert.throws(() => parseStrictJson(value));
    }
    assert.throws(() => parseStrictJson("[[[]]]", { maximumDepth: 1 }), /depth/);
    assert.throws(() => parseStrictJson("[1,2]", { maximumNodes: 2 }), /node/);
});

test("Pipe frame decoder rejects duplicate members before JSON-RPC dispatch", () => {
    const { FrameDecoder } = loadModule("framing.ts");
    const payload = Buffer.from('{"jsonrpc":"2.0","id":1,"id":2,"result":null}', "utf8");
    const frame = Buffer.alloc(payload.length + 4);
    frame.writeUInt32BE(payload.length, 0);
    payload.copy(frame, 4);
    assert.throws(() => new FrameDecoder().feed(frame), /strict UTF-8 JSON/);
});
