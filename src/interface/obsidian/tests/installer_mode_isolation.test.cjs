const assert = require("node:assert/strict");
const { readFileSync } = require("node:fs");
const path = require("node:path");
const test = require("node:test");
const { buildSync } = require("esbuild");

function bundle(relative) {
    return buildSync({
        entryPoints: [path.join(__dirname, "..", relative)],
        bundle: true,
        format: "cjs",
        platform: "node",
        target: "node16",
        treeShaking: true,
        write: false,
    }).outputFiles[0].text;
}

test("production and local development installers are compile-time isolated", () => {
    const production = bundle("src/runtime/installer_mode.ts");
    const development = bundle("src/runtime/installer_mode.local_development.ts");
    const buildConfig = readFileSync(path.join(__dirname, "../esbuild.config.mjs"), "utf8");

    assert.equal(production.includes("OFFERAGENT_LOCAL_DEVELOPMENT_RUNTIME_V1"), false);
    assert.equal(production.includes("__OFFERAGENT_DEVELOPMENT_MANIFEST_SHA256__"), false);
    assert.equal(production.includes("release keyring is empty"), true);
    assert.equal(development.includes("OFFERAGENT_LOCAL_DEVELOPMENT_RUNTIME_V1"), true);
    assert.equal(development.includes("__OFFERAGENT_DEVELOPMENT_MANIFEST_SHA256__"), true);
    assert.equal(development.includes("release keyring is empty"), false);
    assert.match(buildConfig, /local-development/);
    assert.match(buildConfig, /installer_mode\.local_development\.ts/);
    assert.match(buildConfig, /onResolve/);
});
