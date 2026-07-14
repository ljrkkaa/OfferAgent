const assert = require("node:assert/strict");
const { readFileSync } = require("node:fs");
const path = require("node:path");
const test = require("node:test");
const { buildSync } = require("esbuild");

function loadGeneratedProtocol() {
    const output = buildSync({
        entryPoints: [path.join(__dirname, "../src/runtime/generated_protocol.ts")],
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

test("generated TypeScript protocol has the exact schema identity and method catalogs", () => {
    const generated = loadGeneratedProtocol();
    const schemaRoot = path.join(__dirname, "../../../../packages/offeragent-harness/schema");
    const manifest = JSON.parse(readFileSync(path.join(schemaRoot, "protocol-manifest.json"), "utf8"));
    const schema = JSON.parse(readFileSync(path.join(schemaRoot, manifest.schemaBundle), "utf8"));

    assert.equal(generated.PROTOCOL_TYPES_SCHEMA_HASH, manifest.schemaHash);
    assert.deepEqual(generated.PROTOCOL_COMMAND_METHODS, Object.keys(schema.commands).sort());
    assert.deepEqual(generated.PROTOCOL_REVERSE_REQUEST_METHODS, Object.keys(schema.reverseRequests).sort());
    assert.deepEqual(generated.PROTOCOL_EVENT_TYPES, Object.keys(schema.events).sort());
    assert.deepEqual(generated.PROTOCOL_ERROR_CODES, schema.$defs.ErrorCode.enum);
    assert.deepEqual(generated.PROTOCOL_JSON_RPC_ERROR_CODES, schema.$defs.JsonRpcErrorCode.enum);
    assert.equal(generated.PROTOCOL_RPC_CANCEL_METHOD, schema.$defs.RpcCancelNotification.properties.method.const);
    assert.equal(generated.PROTOCOL_EVENT_NOTIFICATION_METHOD, schema.$defs.EventNotification.properties.method.const);
    assert.equal(new Set(generated.PROTOCOL_COMMAND_METHODS).size, generated.PROTOCOL_COMMAND_METHODS.length);
    assert.equal(new Set(generated.PROTOCOL_EVENT_TYPES).size, generated.PROTOCOL_EVENT_TYPES.length);
});

test("public Harness requests have no string/JsonObject escape overload", () => {
    const client = readFileSync(path.join(__dirname, "../src/runtime/harness_client.ts"), "utf8");
    const typeContract = readFileSync(path.join(__dirname, "protocol_type_contract.ts"), "utf8");

    assert.doesNotMatch(client, /request\(\s*method:\s*string/);
    assert.match(client, /Method extends ProtocolCommandMethod/);
    assert.match(typeContract, /@ts-expect-error unknown methods/);
    assert.match(typeContract, /@ts-expect-error required params/);
});
