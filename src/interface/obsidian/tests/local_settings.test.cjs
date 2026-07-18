const assert = require("node:assert/strict");
const path = require("node:path");
const test = require("node:test");
const { buildSync } = require("esbuild");

function loadModule() {
    const output = buildSync({
        entryPoints: [path.join(__dirname, "../src/local/settings.ts")],
        bundle: true,
        format: "cjs",
        platform: "node",
        target: "node16",
        external: ["obsidian"],
        write: false,
    }).outputFiles[0].text;
    const compiled = { exports: {} };
    const fakeRequire = (id) => id === "obsidian"
        ? { PluginSettingTab: class {}, Setting: class {}, App: class {}, Plugin: class {} }
        : require(id);
    new Function("require", "module", "exports", output)(fakeRequire, compiled, compiled.exports);
    return compiled.exports;
}

test("obsolete settings schemas fail closed to the Codex-only schema", () => {
    const { DEFAULT_LOCAL_SETTINGS, parseLocalSettings } = loadModule();
    const settings = parseLocalSettings({
        schemaVersion: 1,
        khojUrl: "https://server.example",
        khojApiKey: "must-not-survive",
        provider: "local",
        model: "qwen:latest",
        telemetryEnabled: true,
    });
    assert.deepEqual(settings, DEFAULT_LOCAL_SETTINGS);
    assert.equal("khojUrl" in settings, false);
    assert.equal("khojApiKey" in settings, false);
});

test("unversioned settings cannot smuggle a free-text model or permission state", () => {
    const { DEFAULT_LOCAL_SETTINGS, parseLocalSettings } = loadModule();
    assert.deepEqual(parseLocalSettings({
        model: "attacker-selected-model",
        proxyUrl: "http://127.0.0.1:7896",
        workspaceTrusted: true,
        permissionMode: "bypass",
    }), DEFAULT_LOCAL_SETTINGS);
});

test("schema v2 migration requires fresh catalog reselection and keeps safe non-model settings", () => {
    const { parseLocalSettings } = loadModule();
    const migrated = parseLocalSettings({
        schemaVersion: 2,
        provider: "codex-subscription-experimental",
        wireApi: "ollama-chat",
        baseUrl: "https://models.example/v1?api_key=must-not-survive",
        approvedRemoteHttpsEndpoint: "https://models.example/v1?api_key=must-not-survive",
        apiKey: "must-not-survive",
        credential: "must-not-survive",
        model: "gpt-catalog-candidate",
        proxyUrl: "http://127.0.0.1:7896",
        reasoningEffort: "high",
        permissionMode: "normal",
        workspaceTrusted: true,
        autoApproveVaultWrites: true,
        shellEnabled: true,
        subagentsEnabled: true,
        hooksEnabled: true,
    });
    assert.deepEqual(migrated, {
        schemaVersion: 3,
        model: "",
        modelAccountBinding: null,
        proxyUrl: "http://127.0.0.1:7896",
        reasoningEffort: "high",
        permissionMode: "normal",
        workspaceTrusted: true,
        autoApproveVaultWrites: true,
        shellEnabled: true,
        subagentsEnabled: true,
        hooksEnabled: true,
        telemetryEnabled: false,
    });
    assert.equal(JSON.stringify(migrated).includes("must-not-survive"), false);
    for (const retired of ["provider", "wireApi", "baseUrl", "approvedRemoteHttpsEndpoint", "apiKey", "credential"]) {
        assert.equal(retired in migrated, false, retired);
    }
    assert.deepEqual(parseLocalSettings(migrated), migrated);
});

test("retired provider selections cannot migrate a free-text model into production settings", () => {
    const { DEFAULT_LOCAL_SETTINGS, parseLocalSettings } = loadModule();
    assert.equal(DEFAULT_LOCAL_SETTINGS.schemaVersion, 3);
    assert.equal(DEFAULT_LOCAL_SETTINGS.model, "");
    assert.equal(DEFAULT_LOCAL_SETTINGS.modelAccountBinding, null);
    for (const provider of ["deepseek", "codex", "openai", "openai-compatible", "local", "remote-agent"]) {
        const migrated = parseLocalSettings({
            schemaVersion: 2,
            provider,
            model: "attacker-selected-model",
            proxyUrl: "http://127.0.0.1:7896",
        });
        assert.equal(migrated.model, "", provider);
        assert.equal(migrated.proxyUrl, "", provider);
        assert.equal("provider" in migrated, false, provider);
    }
});

test("Run config requires an account-bound Codex catalog selection without exposing provider choice", () => {
    const { parseLocalSettings, runConfig, snapshotLocalSettings } = loadModule();
    assert.throws(() => runConfig(parseLocalSettings({ schemaVersion: 3 })), /目录选择/);
    assert.throws(() => runConfig({
        ...parseLocalSettings({ schemaVersion: 3 }),
        model: "gpt-catalog-model",
    }), /目录选择/);
    assert.throws(() => runConfig({
        ...parseLocalSettings({ schemaVersion: 3 }),
        modelAccountBinding: `sha256:${"a".repeat(64)}`,
    }), /目录选择/);
    const settings = parseLocalSettings({
        schemaVersion: 3,
        model: "gpt-catalog-model",
        modelAccountBinding: `sha256:${"a".repeat(64)}`,
    });
    const config = runConfig(settings);
    assert.deepEqual(config, {
        model: "gpt-catalog-model",
        reasoningEffort: "medium",
        permissionMode: "normal",
    });
    assert.equal("enabledSkills" in config, false);
    const snapshot = snapshotLocalSettings(settings);
    assert.equal(Object.isFrozen(snapshot), true);
    assert.equal("enabledSkills" in snapshot, false);
});

test("removed background Worker setting is discarded from persisted snapshots", () => {
    const { parseLocalSettings, snapshotLocalSettings } = loadModule();
    const parsed = parseLocalSettings({ schemaVersion: 2, keepWorkerInBackground: true });
    const snapshot = snapshotLocalSettings(parsed);

    assert.equal("keepWorkerInBackground" in parsed, false);
    assert.equal("keepWorkerInBackground" in snapshot, false);
});

test("non-canonical local enum spellings fail closed", () => {
    const { parseLocalSettings } = loadModule();
    const settings = parseLocalSettings({ schemaVersion: 2, reasoningEffort: "none", permissionMode: "auto_edit" });
    assert.equal(settings.reasoningEffort, "medium");
    assert.equal(settings.permissionMode, "normal");
    assert.equal(settings.workspaceTrusted, false);
});

test("Workspace trust is independent, explicit, and fail-closed for effective permissions", () => {
    const { effectivePermissionMode, parseLocalSettings, vaultWriteAvailable } = loadModule();
    const initial = parseLocalSettings({ schemaVersion: 2, permissionMode: "normal" });
    assert.equal(initial.workspaceTrusted, false);
    assert.equal(effectivePermissionMode(initial), "read-only");
    assert.equal(vaultWriteAvailable(initial), false);

    const trustedNormal = parseLocalSettings({ schemaVersion: 2, permissionMode: "normal", workspaceTrusted: true });
    assert.equal(effectivePermissionMode(trustedNormal), "normal");
    assert.equal(vaultWriteAvailable(trustedNormal), true);

    const revoked = parseLocalSettings({ schemaVersion: 2, permissionMode: "trusted-workspace", workspaceTrusted: false });
    assert.equal(revoked.workspaceTrusted, false);
    assert.equal(revoked.permissionMode, "normal");
    assert.equal(effectivePermissionMode(revoked), "read-only");

    const missingExplicitTrust = parseLocalSettings({ schemaVersion: 2, permissionMode: "trusted-workspace" });
    assert.equal(missingExplicitTrust.workspaceTrusted, false);
    assert.equal(missingExplicitTrust.permissionMode, "normal");
    assert.equal(effectivePermissionMode(missingExplicitTrust), "read-only");

    const bypass = parseLocalSettings({ schemaVersion: 2, permissionMode: "bypass", workspaceTrusted: true });
    assert.equal(bypass.permissionMode, "bypass");
    assert.equal(effectivePermissionMode(bypass), "bypass");
    assert.equal(vaultWriteAvailable(bypass), true);

    const bypassWithoutTrust = parseLocalSettings({ schemaVersion: 2, permissionMode: "bypass" });
    assert.equal(bypassWithoutTrust.permissionMode, "normal");
    assert.equal(effectivePermissionMode(bypassWithoutTrust), "read-only");

    const plan = parseLocalSettings({ schemaVersion: 2, permissionMode: "plan", workspaceTrusted: false });
    assert.equal(effectivePermissionMode(plan), "plan");
    assert.equal(vaultWriteAvailable(plan), false);
});

test("current settings fail closed when retired Provider decisions conflict with the Codex-only schema", () => {
    const { DEFAULT_LOCAL_SETTINGS, modelRuntimePatch, parseLocalSettings } = loadModule();
    const binding = `sha256:${"a".repeat(64)}`;
    const settings = parseLocalSettings({
        schemaVersion: 3,
        model: "gpt-catalog-model",
        modelAccountBinding: binding,
        proxyUrl: "http://127.0.0.1:7896",
        provider: "deepseek",
        wireApi: "chat-completions",
        baseUrl: "https://attacker.example/v1",
        credential: "must-not-survive",
    });
    assert.deepEqual(settings, DEFAULT_LOCAL_SETTINGS);
    assert.deepEqual(modelRuntimePatch(settings), {
        model: "",
        account_binding: null,
        reasoning_effort: "medium",
        proxy_url: null,
    });
    assert.equal(modelRuntimePatch.length, 1);
});

test("schema v3 preserves only a bounded model and valid SHA-256 account binding pair", () => {
    const { parseLocalSettings } = loadModule();
    const binding = `sha256:${"a".repeat(64)}`;
    assert.deepEqual(
        (({ model, modelAccountBinding }) => ({ model, modelAccountBinding }))(parseLocalSettings({
            schemaVersion: 3,
            model: "  gpt-catalog-model  ",
            modelAccountBinding: binding,
        })),
        { model: "gpt-catalog-model", modelAccountBinding: binding },
    );
    for (const raw of [
        { model: "gpt-catalog-model" },
        { modelAccountBinding: binding },
        { model: "gpt-catalog-model", modelAccountBinding: `sha256:${"A".repeat(64)}` },
        { model: "gpt-catalog-model", modelAccountBinding: `sha256:${"a".repeat(63)}` },
        { model: `gpt${"x".repeat(254)}`, modelAccountBinding: binding },
        { model: "gpt\0injected", modelAccountBinding: binding },
        { model: 42, modelAccountBinding: binding },
    ]) {
        const parsed = parseLocalSettings({ schemaVersion: 3, ...raw });
        assert.equal(parsed.model, "");
        assert.equal(parsed.modelAccountBinding, null);
    }
});

test("subscription proxy accepts only explicit literal-loopback HTTP ports", () => {
    const { parseLocalSettings, safeProxyUrl } = loadModule();
    assert.equal(safeProxyUrl(""), "");
    assert.equal(safeProxyUrl(" http://127.0.0.1:7896/ "), "http://127.0.0.1:7896");
    assert.equal(safeProxyUrl("http://[::1]:8080"), "http://[::1]:8080");
    assert.equal(safeProxyUrl("http://127.0.0.1:00080"), "http://127.0.0.1:80");
    for (const unsafe of [
        "http://127.0.0.1",
        "https://127.0.0.1:7896",
        "http://localhost:7896",
        "http://127.0.0.2:7896",
        "http://user:pass@127.0.0.1:7896",
        "http://127.0.0.1:7896/path",
        "http://127.0.0.1:7896?query=1",
        "http://127.0.0.1:0",
        "http://127.0.0.1:65536",
    ]) assert.equal(safeProxyUrl(unsafe), "", unsafe);
    assert.equal(parseLocalSettings({
        schemaVersion: 2,
        provider: "codex-subscription-experimental",
        proxyUrl: "http://192.168.1.2:7896",
    }).proxyUrl, "");
});

test("an empty catalog selection remains empty without a model or Provider fallback", () => {
    const module = loadModule();
    const { modelRuntimePatch, parseLocalSettings } = module;
    const settings = parseLocalSettings({
        schemaVersion: 3,
        proxyUrl: "http://127.0.0.1:7896",
    });
    assert.deepEqual(
        modelRuntimePatch(settings),
        {
            model: "",
            account_binding: null,
            reasoning_effort: "medium",
            proxy_url: "http://127.0.0.1:7896",
        },
    );
    for (const retiredExport of [
        "modelCredentialProviderId",
        "usesProviderSecretStore",
        "usesUnconfiguredCompatibleFallback",
        "isRemoteHttpsEndpointApproved",
        "requiresRemoteHttpsApproval",
        "modelHealthMessage",
    ]) assert.equal(retiredExport in module, false, retiredExport);
});

test("current settings discard retired model configuration and secret-shaped values", () => {
    const { parseLocalSettings, snapshotLocalSettings } = loadModule();
    const parsed = parseLocalSettings({
        schemaVersion: 3,
        model: "gpt-catalog-model",
        modelAccountBinding: `sha256:${"a".repeat(64)}`,
        provider: "openai-compatible",
        wireApi: "chat-completions",
        baseUrl: "https://user:secret@models.example/v1",
        approvedRemoteHttpsEndpoint: "https://models.example/v1",
        apiKey: "must-not-survive",
        credentialHandle: "secret:v1:must-not-survive",
    });
    assert.deepEqual(snapshotLocalSettings(parsed), parsed);
    assert.deepEqual(Object.keys(parsed).sort(), [
        "autoApproveVaultWrites",
        "hooksEnabled",
        "model",
        "modelAccountBinding",
        "permissionMode",
        "proxyUrl",
        "reasoningEffort",
        "schemaVersion",
        "shellEnabled",
        "subagentsEnabled",
        "telemetryEnabled",
        "workspaceTrusted",
    ]);
    assert.equal(JSON.stringify(parsed).includes("must-not-survive"), false);
});

test("serialized settings operations cannot let a later apply overtake an earlier apply", async () => {
    const { SerializedOperationQueue } = loadModule();
    const queue = new SerializedOperationQueue();
    const order = [];
    let releaseFirst;
    const firstGate = new Promise((resolve) => { releaseFirst = resolve; });
    const first = queue.run(async () => {
        order.push("first:start");
        await firstGate;
        order.push("first:end");
    });
    const second = queue.run(async () => {
        order.push("second:start");
        order.push("second:end");
    });
    await new Promise((resolve) => setImmediate(resolve));
    assert.deepEqual(order, ["first:start"]);
    releaseFirst();
    await Promise.all([first, second]);
    assert.deepEqual(order, ["first:start", "first:end", "second:start", "second:end"]);
});

test("serialized settings operations continue after a rejected operation", async () => {
    const { SerializedOperationQueue } = loadModule();
    const queue = new SerializedOperationQueue();
    await assert.rejects(queue.run(async () => { throw new Error("expected"); }), /expected/);
    assert.equal(await queue.run(async () => "recovered"), "recovered");
});

test("settings UI has no Provider, protocol, endpoint, credential, or free-text model controls", () => {
    const source = require("node:fs").readFileSync(path.join(__dirname, "../src/local/settings.ts"), "utf8");
    for (const retiredSurface of [
        "模型 Provider",
        "模型协议",
        "模型端点",
        "Provider 凭据",
        "OpenAI-compatible",
        "DeepSeek",
        "Ollama",
        "saveProviderCredential",
        "deleteProviderCredential",
        "approvedRemoteHttpsEndpoint",
    ]) assert.equal(source.includes(retiredSurface), false, retiredSurface);
    const modelStart = source.indexOf('.setName("模型")');
    const modelEnd = source.indexOf('.setName("Codex 登录")', modelStart);
    assert.notEqual(modelStart, -1);
    assert.notEqual(modelEnd, -1);
    assert.doesNotMatch(source.slice(modelStart, modelEnd), /\.addText|\.addDropdown/);
    assert.match(source.slice(modelStart, modelEnd), /实时 Codex 模型目录/);
    assert.match(source, /本机 HTTP 代理（可选）/);
});

test("trusted Workspace can explicitly disable only Vault write prompts", () => {
    const { parseLocalSettings } = loadModule();
    const enabled = parseLocalSettings({ schemaVersion: 3, workspaceTrusted: true, autoApproveVaultWrites: true });
    const revoked = parseLocalSettings({ schemaVersion: 3, workspaceTrusted: false, autoApproveVaultWrites: true });
    const settingsSource = require("node:fs").readFileSync(path.join(__dirname, "../src/local/settings.ts"), "utf8");
    const mainSource = require("node:fs").readFileSync(path.join(__dirname, "../src/main.ts"), "utf8");

    assert.equal(enabled.autoApproveVaultWrites, true);
    assert.equal(revoked.autoApproveVaultWrites, false);
    assert.match(settingsSource, /Vault 写入无需逐次审批/);
    assert.match(settingsSource, /Shell 与网络权限不会因此开放/);
    assert.match(mainSource, /approve_vault_writes: !settings\.autoApproveVaultWrites/);
});
