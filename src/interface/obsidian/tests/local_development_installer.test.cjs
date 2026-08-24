const assert = require("node:assert/strict");
const { createHash } = require("node:crypto");
const { promises: fs } = require("node:fs");
const path = require("node:path");
const test = require("node:test");
const { build } = require("esbuild");

const PLUGIN_VERSION = "2.0.0-beta.28";
const PROTOCOL_VERSION = "1.0";
const SCHEMA_HASH = `sha256:${"a".repeat(64)}`;

async function loadInstaller(manifestSha256) {
    const result = await build({
        entryPoints: [path.join(__dirname, "../src/runtime/local_development_installer.ts")],
        bundle: true,
        define: {
            __OFFERAGENT_DEVELOPMENT_MANIFEST_SHA256__: JSON.stringify(manifestSha256),
        },
        format: "cjs",
        platform: "node",
        plugins: [{
            name: "fixture-windows-platform",
            setup(build) {
                build.onResolve({ filter: /^node:os$/ }, () => ({
                    path: "windows-os-fixture",
                    namespace: "fixture",
                }));
                build.onLoad({ filter: /.*/, namespace: "fixture" }, () => ({
                    contents: [
                        'export const arch = () => "x64";',
                        'export const platform = () => "win32";',
                        'export const release = () => "10.0.26100";',
                    ].join("\n"),
                    loader: "js",
                }));
            },
        }],
        target: "node16",
        write: false,
    });
    const output = result.outputFiles[0].text;
    const compiled = { exports: {} };
    new Function("require", "module", "exports", output)(require, compiled, compiled.exports);
    return compiled.exports;
}

function canonicalJson(value) {
    const normalize = (item) => {
        if (Array.isArray(item)) return item.map(normalize);
        if (item !== null && typeof item === "object") {
            return Object.fromEntries(
                Object.keys(item).sort().map((key) => [key, normalize(item[key])]),
            );
        }
        return item;
    };
    return JSON.stringify(normalize(value));
}

function sha256Bytes(value) {
    return `sha256:${createHash("sha256").update(value).digest("hex")}`;
}

function peX64Fixture() {
    const payload = Buffer.alloc(512);
    payload.writeUInt16LE(0x5a4d, 0);
    payload.writeUInt32LE(0x80, 0x3c);
    payload.write("PE\0\0", 0x80, "ascii");
    payload.writeUInt16LE(0x8664, 0x84);
    return payload;
}

async function createFixture(t, options = {}) {
    const pluginDirectory = await fs.mkdtemp(path.join(process.cwd(), ".installer-fixture-"));
    t.after(async () => fs.rm(pluginDirectory, { force: true, recursive: true }));
    const runtimeRoot = path.join(pluginDirectory, "runtime/windows-x64/local-development");
    const executable = peX64Fixture();
    const files = [
        ["offeragent-process-host.exe", "executable", executable],
        ["offeragent-worker.exe", "executable", executable],
        ["process-catalog.v1.json", "asset", Buffer.from("{}\n", "utf8")],
        ["skills/core/SKILL.md", "asset", Buffer.from("# Core\n", "utf8")],
        ...(options.omitRipgrep ? [] : [["tools/rg.exe", "executable", executable]]),
    ];
    const records = [];
    for (const [relativePath, kind, payload] of files) {
        const target = path.join(runtimeRoot, ...relativePath.split("/"));
        await fs.mkdir(path.dirname(target), { recursive: true });
        await fs.writeFile(target, payload);
        records.push({
            byteLength: payload.length,
            kind,
            path: relativePath,
            sha256: sha256Bytes(payload),
        });
    }
    records.sort((left, right) => left.path < right.path ? -1 : left.path > right.path ? 1 : 0);
    const manifest = {
        build: {
            commit: "c".repeat(40),
            sourceTreeSha256: `sha256:${"b".repeat(64)}`,
        },
        coreVersion: "0.1.0-local.test",
        developmentOnly: true,
        files: records,
        platform: {
            architecture: "x64",
            minimumWindowsBuild: 10_240,
            os: "windows",
        },
        pluginVersion: PLUGIN_VERSION,
        protocol: {
            maximum: PROTOCOL_VERSION,
            minimum: PROTOCOL_VERSION,
            schemaHash: SCHEMA_HASH,
        },
        runtimeContentSha256: sha256Bytes(Buffer.from(canonicalJson({ files: records }), "utf8")),
        runtimeVersion: "0.1.0-local.test",
        schemaVersion: 1,
        stateSchemaVersion: 1,
        toolAbiVersion: "1.0",
    };
    const manifestBytes = Buffer.from(`${canonicalJson(manifest)}\n`, "utf8");
    await fs.writeFile(path.join(runtimeRoot, "development-runtime-manifest.json"), manifestBytes);
    return {
        manifestSha256: sha256Bytes(manifestBytes),
        pluginDirectory,
        runtimeRoot,
    };
}

function installerOptions(fixture) {
    return {
        pluginDirectory: fixture.pluginDirectory,
        pluginVersion: PLUGIN_VERSION,
        protocolVersion: PROTOCOL_VERSION,
        schemaHash: SCHEMA_HASH,
    };
}

test("canonical local Runtime fixture with every executable record verifies successfully", async (t) => {
    const fixture = await createFixture(t);
    const { LocalDevelopmentRuntimeInstaller } = await loadInstaller(fixture.manifestSha256);
    const installer = new LocalDevelopmentRuntimeInstaller(installerOptions(fixture));
    const phases = [];

    const installed = await installer.ensureReady(new AbortController().signal, (phase) => phases.push(phase));
    await installed.beforeWorkerLaunch();

    assert.deepEqual(phases, ["locating_embedded_bundle", "verifying_manifest", "ready"]);
    assert.equal(installed.workerExecutable, path.join(fixture.runtimeRoot, "offeragent-worker.exe"));
});

test("canonical manifest missing the ripgrep record fails at executable validation", async (t) => {
    const fixture = await createFixture(t, { omitRipgrep: true });
    const { LocalDevelopmentRuntimeInstaller } = await loadInstaller(fixture.manifestSha256);
    const installer = new LocalDevelopmentRuntimeInstaller(installerOptions(fixture));
    const phases = [];

    await assert.rejects(
        installer.ensureReady(new AbortController().signal, (phase) => phases.push(phase)),
        (error) => {
            assert.equal(error.name, "LocalDevelopmentRuntimeVerificationError");
            assert.equal(error.code, "development_executable_record_invalid");
            assert.match(error.message, /development_executable_record_invalid/);
            return true;
        },
    );
    // Reaching manifest verification with an injected matching anchor and a
    // deterministic Windows x64 platform distinguishes this from earlier
    // anchor/platform failures.
    assert.deepEqual(phases, ["locating_embedded_bundle", "verifying_manifest"]);
});
