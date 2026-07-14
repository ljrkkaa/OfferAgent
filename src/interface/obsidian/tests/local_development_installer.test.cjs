const assert = require("node:assert/strict");
const {
    mkdirSync,
    mkdtempSync,
    readFileSync,
    rmSync,
    writeFileSync,
} = require("node:fs");
const { tmpdir } = require("node:os");
const path = require("node:path");
const test = require("node:test");
const { createHash } = require("node:crypto");
const { buildSync } = require("esbuild");

function loadModule(expectedManifestSha256) {
    const output = buildSync({
        entryPoints: [path.join(__dirname, "../src/runtime/local_development_installer.ts")],
        bundle: true,
        format: "cjs",
        platform: "node",
        target: "node16",
        write: false,
        define: {
            __OFFERAGENT_DEVELOPMENT_MANIFEST_SHA256__: JSON.stringify(expectedManifestSha256),
        },
    }).outputFiles[0].text;
    const compiled = { exports: {} };
    new Function("require", "module", "exports", output)(require, compiled, compiled.exports);
    return compiled.exports;
}

function canonicalText(value) {
    const normalize = (item) => Array.isArray(item) ? item.map(normalize) : item && typeof item === "object"
        ? Object.fromEntries(Object.keys(item).sort().map((key) => [key, normalize(item[key])])) : item;
    return JSON.stringify(normalize(value));
}

function digest(value) {
    return `sha256:${createHash("sha256").update(value).digest("hex")}`;
}

function peX64(label) {
    const result = Buffer.alloc(512);
    result.writeUInt16LE(0x5a4d, 0);
    result.writeUInt32LE(0x80, 0x3c);
    result.write("PE\0\0", 0x80, "binary");
    result.writeUInt16LE(0x8664, 0x84);
    result.write(label, 0x100, "utf8");
    return result;
}

function fixture() {
    const pluginDirectory = mkdtempSync(path.join(tmpdir(), "offeragent-local-installer-"));
    const runtimeRoot = path.join(pluginDirectory, "runtime", "windows-x64", "local-development");
    const payloads = new Map([
        ["offeragent-host.exe", peX64("host")],
        ["offeragent-process-host.exe", peX64("process")],
        ["offeragent-self-test.exe", peX64("self-test")],
        ["offeragent-worker.exe", peX64("worker")],
        ["process-catalog.v1.json", Buffer.from("{}\n")],
        ["skills/local/SKILL.md", Buffer.from("# Local\n")],
        ["web/index.html", Buffer.from("<!doctype html>\n")],
    ]);
    const files = [...payloads].sort(([left], [right]) => left.localeCompare(right)).map(([relative, payload]) => {
        const target = path.join(runtimeRoot, ...relative.split("/"));
        mkdirSync(path.dirname(target), { recursive: true });
        writeFileSync(target, payload);
        return {
            byteLength: payload.length,
            kind: relative.endsWith(".exe") ? "executable" : relative.startsWith("skills/") ? "skill" : "asset",
            path: relative,
            sha256: digest(payload),
        };
    });
    const manifest = {
        build: {
            commit: "a".repeat(40),
            sourceTreeSha256: digest(Buffer.from("source")),
        },
        coreVersion: "0.1.0-local.0123456789abcdef",
        developmentOnly: true,
        files,
        platform: { architecture: "x64", minimumWindowsBuild: 10240, os: "windows" },
        pluginVersion: "2.0.0-beta.28",
        protocol: {
            maximum: "1.0",
            minimum: "1.0",
            schemaHash: `sha256:${"b".repeat(64)}`,
        },
        runtimeContentSha256: digest(Buffer.from(canonicalText({ files }), "utf8")),
        runtimeVersion: "0.1.0-local.0123456789abcdef",
        schemaVersion: 1,
        stateSchemaVersion: 1,
        toolAbiVersion: "1",
    };
    writeFileSync(
        path.join(runtimeRoot, "development-runtime-manifest.json"),
        Buffer.from(`${canonicalText(manifest)}\n`, "utf8"),
    );
    return { pluginDirectory, runtimeRoot, manifest };
}

const windowsTest = process.platform === "win32" ? test : test.skip;

windowsTest("local development installer defers exact-tree validation to Host launch without a self-test", async () => {
    const value = fixture();
    const manifestSha256 = digest(readFileSync(path.join(
        value.runtimeRoot,
        "development-runtime-manifest.json",
    )));
    const { LocalDevelopmentRuntimeInstaller } = loadModule(manifestSha256);
    try {
        const phases = [];
        const installer = new LocalDevelopmentRuntimeInstaller({
            pluginDirectory: value.pluginDirectory,
            pluginVersion: value.manifest.pluginVersion,
            protocolVersion: value.manifest.protocol.minimum,
            schemaHash: value.manifest.protocol.schemaHash,
        });
        const runtime = await installer.ensureReady(new AbortController().signal, (phase) => phases.push(phase));
        assert.equal(runtime.version, value.manifest.runtimeVersion);
        assert.equal(runtime.hostExecutable, path.join(value.runtimeRoot, "offeragent-host.exe"));
        assert.equal(runtime.hostDiscoveryTimeoutMs, 180_000);
        assert.deepEqual(phases, [
            "not_installed",
            "locating_embedded_bundle",
            "verifying_manifest_and_signature",
            "ready",
        ]);
        await runtime.beforeHostLaunch();
    } finally {
        rmSync(value.pluginDirectory, { force: true, recursive: true });
    }
});

windowsTest("local development installer rejects file drift and extra files at Host launch", async () => {
    for (const mutate of [
        (value) => writeFileSync(path.join(value.runtimeRoot, "offeragent-worker.exe"), peX64("drift")),
        (value) => writeFileSync(path.join(value.runtimeRoot, "unexpected.dll"), Buffer.from("extra")),
    ]) {
        const value = fixture();
        const manifestSha256 = digest(readFileSync(path.join(
            value.runtimeRoot,
            "development-runtime-manifest.json",
        )));
        const { LocalDevelopmentRuntimeInstaller } = loadModule(manifestSha256);
        try {
            mutate(value);
            const installer = new LocalDevelopmentRuntimeInstaller({
                pluginDirectory: value.pluginDirectory,
                pluginVersion: value.manifest.pluginVersion,
                protocolVersion: value.manifest.protocol.minimum,
                schemaHash: value.manifest.protocol.schemaHash,
            });
            const runtime = await installer.ensureReady(new AbortController().signal, () => {});
            await assert.rejects(
                () => runtime.beforeHostLaunch(),
                /verification failed/,
            );
        } finally {
            rmSync(value.pluginDirectory, { force: true, recursive: true });
        }
    }
});

windowsTest("local development installer rejects a missing marker and noncanonical bytes", async () => {
    for (const mutate of [
        (payload) => payload.replace('"developmentOnly":true', '"developmentOnly":false'),
        (payload) => payload.replace('"coreVersion"', ' "coreVersion"'),
    ]) {
        const value = fixture();
        const originalManifestSha256 = digest(readFileSync(path.join(
            value.runtimeRoot,
            "development-runtime-manifest.json",
        )));
        const { LocalDevelopmentRuntimeInstaller } = loadModule(originalManifestSha256);
        try {
            const target = path.join(value.runtimeRoot, "development-runtime-manifest.json");
            writeFileSync(target, mutate(readFileSync(target, "utf8")));
            const installer = new LocalDevelopmentRuntimeInstaller({
                pluginDirectory: value.pluginDirectory,
                pluginVersion: value.manifest.pluginVersion,
                protocolVersion: value.manifest.protocol.minimum,
                schemaHash: value.manifest.protocol.schemaHash,
            });
            await assert.rejects(
                () => installer.ensureReady(new AbortController().signal, () => {}),
                /verification failed/,
            );
        } finally {
            rmSync(value.pluginDirectory, { force: true, recursive: true });
        }
    }
});

windowsTest("local development installer rejects a valid manifest that differs from the embedded anchor", async () => {
    const value = fixture();
    const { LocalDevelopmentRuntimeInstaller } = loadModule(digest(Buffer.from("different-build")));
    try {
        const installer = new LocalDevelopmentRuntimeInstaller({
            pluginDirectory: value.pluginDirectory,
            pluginVersion: value.manifest.pluginVersion,
            protocolVersion: value.manifest.protocol.minimum,
            schemaHash: value.manifest.protocol.schemaHash,
        });
        await assert.rejects(
            () => installer.ensureReady(new AbortController().signal, () => {}),
            /verification failed/,
        );
    } finally {
        rmSync(value.pluginDirectory, { force: true, recursive: true });
    }
});

windowsTest("local development runtime revalidates the pinned tree at the actual Host launch boundary", async () => {
    const value = fixture();
    const manifestSha256 = digest(readFileSync(path.join(
        value.runtimeRoot,
        "development-runtime-manifest.json",
    )));
    const { LocalDevelopmentRuntimeInstaller } = loadModule(manifestSha256);
    try {
        const installer = new LocalDevelopmentRuntimeInstaller({
            pluginDirectory: value.pluginDirectory,
            pluginVersion: value.manifest.pluginVersion,
            protocolVersion: value.manifest.protocol.minimum,
            schemaHash: value.manifest.protocol.schemaHash,
        });
        const runtime = await installer.ensureReady(new AbortController().signal, () => {});
        writeFileSync(path.join(value.runtimeRoot, "offeragent-worker.exe"), peX64("launch-drift"));
        await assert.rejects(() => runtime.beforeHostLaunch(), /verification failed/);
    } finally {
        rmSync(value.pluginDirectory, { force: true, recursive: true });
    }
});
