const assert = require("node:assert/strict");
const path = require("node:path");
const test = require("node:test");
const { buildSync } = require("esbuild");

const HASH_A = `sha256:${"a".repeat(64)}`;
const HASH_B = `sha256:${"b".repeat(64)}`;

function loadModule() {
    const output = buildSync({
        entryPoints: [path.join(__dirname, "../src/local/extension_settings.ts")],
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

function responses() {
    return {
        "skills/list": {
            workspaceId: "ws_test", revision: 2, snapshotHash: HASH_A, diagnostics: [],
            skills: [{
                rootId: "user", packagePath: "review-helper", layer: "user", name: "review-helper",
                description: "Review", metadataHash: HASH_A, trustState: "confirmed", enabled: true,
                allowedTools: [],
            }],
        },
        "skills/status": {
            status: { revision: 2, snapshotHash: HASH_A, discoveredCount: 1, enabledCount: 1, partial: false, diagnostics: [] },
        },
        "shell/list": {
            workspaceId: "ws_test", revision: 3, snapshotHash: HASH_A, profiles: [], environments: [
                { profileId: "minimal", allowedNames: [], allowedSecretNames: [] },
            ], executables: [{
                executableId: "registered", fingerprint: HASH_B, fixedArguments: ["safe-prefix"],
                minimumVariableArguments: 0, maximumVariableArguments: 1,
                variableArgumentPattern: "^[a-z-]+$", allowedCwdRootIds: ["vault"],
                environmentProfileIds: ["minimal"], allowedStdinModes: ["closed", "fixed_payload"], allowNetwork: false,
            }],
        },
        "hooks/list": {
            workspaceId: "ws_test", profileId: "profile_local", revision: 1,
            snapshotHash: HASH_A, layers: [], builtinHandlerIds: ["builtin.allow"],
        },
        "process/registrations/list": {
            workspaceId: "ws_test", catalogRevision: 0, activeCatalogRevision: 0,
            snapshotHash: HASH_A, restartRequired: false, executables: [], environments: [],
        },
    };
}

test("extension manager reads every authoritative extension and process catalog", async () => {
    const { ExtensionRuntimeManager } = loadModule();
    const values = responses();
    const calls = [];
    const manager = new ExtensionRuntimeManager({ request: async (method, params) => {
        calls.push([method, params]);
        return values[method];
    } });
    const snapshot = await manager.snapshot();
    assert.deepEqual(calls.map(([method]) => method), [
        "skills/list", "skills/status", "shell/list", "hooks/list", "process/registrations/list",
    ]);
    assert.equal(snapshot.skills[0].name, "review-helper");
    assert.equal(snapshot.shell.executables[0].executableId, "registered");
    assert.equal(snapshot.hooks.profileId, "profile_local");
});

test("Shell and Hook installs remain structured and never emit raw command or secret fields", async () => {
    const { ExtensionRuntimeManager } = loadModule();
    const calls = [];
    const manager = new ExtensionRuntimeManager({ request: async (method, params) => {
        calls.push([method, params]);
        return {};
    } });
    await manager.installShell({
        profileId: "safe_tool", description: "Safe tool", executableId: "registered",
        executableProfileFingerprint: HASH_B, executableFixedArguments: ["safe-prefix"],
        executableVariableArgumentPattern: "^[a-z-]+$", fixedArguments: ["status"],
        cwdRootId: "vault", environmentProfileId: "minimal", risk: "execute",
        sideEffectClass: "execute", allowNetwork: false, expectedRevision: 0,
    });
    await manager.installHook({
        scope: "workspace", ownerId: "ws_test", hookId: "safe-hook", event: "TurnStart",
        implementation: "command", handlerId: null, executableId: "registered",
        executableProfileFingerprint: HASH_B, executableFixedArguments: ["safe-prefix"],
        arguments: ["status"], cwdRootId: "vault", environmentProfileId: "minimal",
        expectedRevision: 0, layerRevision: 1,
    });
    const shell = calls[0][1].profile;
    const hook = calls[1][1].layer.hooks[0].command;
    assert.deepEqual(shell.fixedArguments, ["safe-prefix", "status"]);
    assert.deepEqual(hook.arguments, ["safe-prefix", "status"]);
    const serialized = JSON.stringify(calls);
    assert.equal(serialized.includes("rawCommand"), false);
    assert.equal(serialized.includes("secret"), false);
    assert.match(calls[0][1].clientRequestId, /^req_extension_[0-9a-f]{32}$/);
    assert.match(calls[1][1].clientRequestId, /^req_extension_[0-9a-f]{32}$/);
});

test("credential-like structured argv is rejected before Pipe dispatch", async () => {
    const { ExtensionRuntimeManager } = loadModule();
    const manager = new ExtensionRuntimeManager({ request: async () => {
        throw new Error("must not dispatch");
    } });
    await assert.rejects(() => manager.installShell({
        profileId: "safe_tool", description: "Safe tool", executableId: "registered",
        executableProfileFingerprint: HASH_B, executableFixedArguments: [],
        executableVariableArgumentPattern: ".*", fixedArguments: ["--token=forbidden"],
        cwdRootId: "vault", environmentProfileId: "minimal", risk: "execute",
        sideEffectClass: "execute", allowNetwork: false, expectedRevision: 0,
    }), /credential|逐项填写/);
});

test("Process executable registration is two-phase and the absolute path is absent from confirmation", async () => {
    const { ExtensionRuntimeManager } = loadModule();
    const calls = [];
    const manager = new ExtensionRuntimeManager({ request: async (method, params) => {
        calls.push([method, params]);
        if (method === "process/registrations/probe") return {
            challengeId: `process-probe_${"a".repeat(24)}`,
            kind: "executable",
            registrationId: "local_tool",
            contentHash: HASH_A,
            expiresAt: "2026-07-13T10:05:00+00:00",
            executable: {
                executableId: "local_tool",
                canonicalPath: "C:\\Vendor\\tool.exe",
                fixedRoot: "C:\\Vendor",
                trust: "fixed_hash",
                authenticodeVerified: false,
                fileSha256: HASH_B,
                fileDevice: "9",
                fileIndex: "42",
                fileSize: 4096,
                profileFingerprint: HASH_A,
                fixedArguments: ["--stdio"],
                minimumVariableArguments: 0,
                maximumVariableArguments: 1,
                variableArgumentPattern: "^[a-z]+$",
                environmentProfileIds: ["minimal"],
                allowedStdinModes: ["duplex"],
                allowedCwdRootIds: ["process-scratch"],
                appcontainerFilesystem: [
                    { rootId: "process-scratch", relativePath: "working", access: "read_write" },
                ],
                allowNetwork: false,
            },
            environment: null,
        };
        if (method === "process/registrations/confirm") return {
            clientRequestId: params.clientRequestId, kind: "executable", registrationId: "local_tool",
            catalogRevision: 1, snapshotHash: HASH_A, recordRevision: 1,
            recordContentHash: HASH_A, deleted: false, restartRequired: true,
        };
        throw new Error(`unexpected method ${method}`);
    } });
    const catalog = responses()["process/registrations/list"];
    const probe = await manager.probeProcessExecutable({
        executableId: "local_tool",
        executablePath: "C:\\Vendor\\tool.exe",
        fixedArguments: ["--stdio"],
        minimumVariableArguments: 0,
        maximumVariableArguments: 1,
        variableArgumentPattern: "^[a-z]+$",
        environmentProfileIds: ["minimal"],
        allowedStdinModes: ["duplex"],
        cwdRootId: "process-scratch",
        appContainerRelativePath: "working",
    }, catalog);
    await manager.confirmProcess(probe, catalog);

    assert.equal(probe.canonicalPath, "C:\\Vendor\\tool.exe");
    assert.deepEqual(probe.fixedArguments, ["--stdio"]);
    assert.deepEqual(probe.allowedStdinModes, ["duplex"]);
    assert.deepEqual(probe.allowedCwdRootIds, ["process-scratch"]);
    assert.deepEqual(probe.appcontainerFilesystem, [
        { rootId: "process-scratch", relativePath: "working", access: "read_write" },
    ]);
    assert.equal(calls[0][0], "process/registrations/probe");
    assert.equal(calls[0][1].executable.allowNetwork, false);
    assert.equal(calls[0][1].executable.executablePath, "C:\\Vendor\\tool.exe");
    assert.equal(calls[1][0], "process/registrations/confirm");
    assert.equal(JSON.stringify(calls[1][1]).includes("Vendor"), false);
    assert.equal(calls[1][1].expectedCatalogRevision, 0);
});
