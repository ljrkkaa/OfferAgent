const assert = require("node:assert/strict");
const { copyFileSync, mkdtempSync, rmSync, writeFileSync } = require("node:fs");
const { tmpdir } = require("node:os");
const path = require("node:path");
const { spawnSync } = require("node:child_process");
const test = require("node:test");

const pluginRoot = path.join(__dirname, "..");

function check(script, output) {
    return spawnSync(process.execPath, [path.join(pluginRoot, "scripts", script), "--check", "--output", output], {
        cwd: pluginRoot,
        encoding: "utf8",
    });
}

for (const [name, script, generated] of [
    ["identity", "generate-protocol-identity.mjs", "generated_protocol_identity.ts"],
    ["types", "generate-protocol-types.mjs", "generated_protocol.ts"],
]) {
    test(`protocol ${name} generator check mode detects missing and stale output without rewriting it`, () => {
        const directory = mkdtempSync(path.join(tmpdir(), `offeragent-protocol-${name}-`));
        const candidate = path.join(directory, generated);
        try {
            const missing = check(script, candidate);
            assert.notEqual(missing.status, 0);
            assert.match(missing.stderr, /missing/);

            copyFileSync(path.join(pluginRoot, "src", "runtime", generated), candidate);
            const current = check(script, candidate);
            assert.equal(current.status, 0, current.stderr);

            writeFileSync(candidate, "stale generated output\n", "utf8");
            const stale = check(script, candidate);
            assert.notEqual(stale.status, 0);
            assert.match(stale.stderr, /stale/);
            assert.equal(require("node:fs").readFileSync(candidate, "utf8"), "stale generated output\n");
        } finally {
            rmSync(directory, { recursive: true, force: true });
        }
    });
}

test("typecheck, the local build, and CI use the non-mutating protocol freshness gate", () => {
    const packageJson = require(path.join(pluginRoot, "package.json"));
    assert.match(packageJson.scripts["protocol:check"], /--check/);
    assert.match(packageJson.scripts.typecheck, /protocol:check/);
    assert.match(packageJson.scripts["build:local"], /typecheck/);
    assert.equal(packageJson.scripts.build, undefined);
    assert.equal(packageJson.scripts.dev, undefined);

    const workflow = require("node:fs").readFileSync(
        path.join(pluginRoot, "..", "..", "..", ".github", "workflows", "windows-local-runtime.yml"),
        "utf8",
    );
    assert.match(workflow, /^  harness:\s*$/m);
    assert.match(workflow, /^  obsidian:\s*$/m);
    assert.match(workflow, /corepack yarn protocol:check/);
    assert.match(workflow, /corepack yarn typecheck/);
    assert.match(workflow, /^  workflow_dispatch:\s*$/m);
    assert.doesNotMatch(workflow, /^    inputs:\s*$/m);
    for (const retiredReleaseWiring of [
        /native-arm64-signed-release/,
        /native-x64-signed-release/,
        /run_native_(?:arm64|x64)_release/,
        /\$\{\{ inputs\./,
        /scripts\/build_windows_release\.py/,
        /scripts\/audit_windows_release\.py/,
        /OFFERAGENT_AUTHENTICODE_CERTIFICATE_SHA1/,
    ]) {
        assert.doesNotMatch(workflow, retiredReleaseWiring);
    }
});
