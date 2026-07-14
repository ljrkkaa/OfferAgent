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

function headlessStatus(overrides = {}) {
    return {
        state: "pipe_client_tool",
        pipeConnectionCount: 1,
        baselineReliable: true,
        workspaceRevision: 9,
        approvalId: null,
        operationId: null,
        argsHash: null,
        revision: 0,
        expiresAt: null,
        reasonCode: "pipe_client_tool_authoritative",
        userMessage: "Obsidian 已连接；Vault 写入只会经过唯一 Named Pipe Client Tool。",
        canRequest: false,
        canActivate: false,
        canRevoke: false,
        ...overrides,
    };
}

test("obsolete settings schemas fail closed instead of being migrated", () => {
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

test("invalid enum/list values fail closed to safe defaults", () => {
    const { parseLocalSettings } = loadModule();
    const settings = parseLocalSettings({
        schemaVersion: 2,
        provider: "remote-agent",
        permissionMode: "skip-approval",
        enabledSkills: ["a", "a"],
    });
    assert.equal(settings.schemaVersion, 2);
    assert.equal(settings.provider, "deepseek");
    assert.equal(settings.wireApi, "chat-completions");
    assert.equal(settings.model, "deepseek-v4-flash");
    assert.equal(settings.reasoningEffort, "medium");
    assert.equal(settings.permissionMode, "normal");
    assert.equal(settings.workspaceTrusted, false);
    assert.deepEqual(settings.enabledSkills, []);
});

test("current provider settings retain their explicit credential identity", () => {
    const {
        DEFAULT_LOCAL_SETTINGS,
        modelCredentialProviderId,
        parseLocalSettings,
        usesProviderSecretStore,
    } = loadModule();
    assert.equal(DEFAULT_LOCAL_SETTINGS.schemaVersion, 2);
    assert.equal(DEFAULT_LOCAL_SETTINGS.provider, "deepseek");
    assert.equal(DEFAULT_LOCAL_SETTINGS.wireApi, "chat-completions");
    assert.equal(DEFAULT_LOCAL_SETTINGS.model, "deepseek-v4-flash");
    assert.equal(parseLocalSettings(undefined).provider, "deepseek");

    const configured = parseLocalSettings({ schemaVersion: 2, provider: "codex", wireApi: "responses" });
    assert.equal(configured.provider, "codex");
    assert.equal(configured.wireApi, "responses");
    assert.equal(usesProviderSecretStore(configured), true);
    assert.equal(modelCredentialProviderId(configured), "codex");
});

test("Run config is copied and cannot mutate saved settings through aliases", () => {
    const { parseLocalSettings, runConfig, snapshotLocalSettings } = loadModule();
    const settings = parseLocalSettings({ schemaVersion: 2, enabledSkills: ["review"] });
    const config = runConfig(settings);
    config.enabledSkills.push("mutated");
    assert.deepEqual(settings.enabledSkills, ["review"]);
    const snapshot = snapshotLocalSettings(settings);
    assert.equal(Object.isFrozen(snapshot), true);
    assert.equal(Object.isFrozen(snapshot.enabledSkills), true);
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

    const plan = parseLocalSettings({ schemaVersion: 2, permissionMode: "plan", workspaceTrusted: false });
    assert.equal(effectivePermissionMode(plan), "plan");
    assert.equal(vaultWriteAvailable(plan), false);
});

test("model endpoints are restricted to HTTPS remote or literal loopback local URLs", () => {
    const { parseLocalSettings } = loadModule();
    const local = parseLocalSettings({ schemaVersion: 2, provider: "local", baseUrl: "http://127.0.0.1:11434/api/" });
    assert.equal(local.wireApi, "ollama-chat");
    assert.equal(local.baseUrl, "http://127.0.0.1:11434/api");
    assert.equal(parseLocalSettings({ provider: "local" }).baseUrl, "http://127.0.0.1:11434/api");
    assert.equal(parseLocalSettings({ schemaVersion: 2, provider: "local", baseUrl: "http://127.0.0.1:11434" }).baseUrl, "");
    assert.equal(parseLocalSettings({ provider: "local", baseUrl: "http://192.168.1.2:11434" }).baseUrl, "");
    assert.equal(parseLocalSettings({ provider: "openai-compatible", baseUrl: "http://remote.example/v1" }).baseUrl, "");
    assert.equal(parseLocalSettings({ provider: "openai-compatible", baseUrl: "https://models.example/v1" }).baseUrl,
        "https://models.example/v1");
    assert.equal(parseLocalSettings({
        provider: "openai-compatible",
        baseUrl: "https://MODELS.example:443//v1//",
    }).baseUrl, "https://models.example/v1");
    assert.equal(parseLocalSettings({
        provider: "openai-compatible",
        baseUrl: "https://models.example/%76%31",
    }).baseUrl, "");
    assert.equal(parseLocalSettings({ provider: "openai", baseUrl: "https://evil.example" }).baseUrl, "");
    assert.equal(parseLocalSettings({
        schemaVersion: 2,
        provider: "codex-subscription-experimental",
        wireApi: "ollama-chat",
        baseUrl: "https://evil.example",
    }).baseUrl, "");
    assert.equal(parseLocalSettings({
        schemaVersion: 2,
        provider: "codex-subscription-experimental",
        wireApi: "ollama-chat",
    }).wireApi, "responses");
});

test("DeepSeek is a fixed Chat Completions provider bound to its own SecretStore identity", () => {
    const {
        modelCredentialProviderId,
        modelHealthMessage,
        modelRuntimePatch,
        parseLocalSettings,
        usesProviderSecretStore,
    } = loadModule();
    const settings = parseLocalSettings({
        schemaVersion: 2,
        provider: "deepseek",
        wireApi: "responses",
        baseUrl: "https://attacker.example/v1",
        proxyUrl: "http://127.0.0.1:7896",
        model: "deepseek-v4-flash",
    });
    assert.equal(settings.wireApi, "chat-completions");
    assert.equal(settings.baseUrl, "");
    assert.equal(settings.proxyUrl, "");
    assert.equal(usesProviderSecretStore(settings), true);
    assert.equal(modelCredentialProviderId(settings), "deepseek");
    assert.deepEqual(modelRuntimePatch(settings, "secret:v1:0123456789abcdef0123456789abcdef"), {
        provider: "deepseek",
        wire_api: "chat-completions",
        model: "deepseek-v4-flash",
        reasoning_effort: "medium",
        base_url: "",
        proxy_url: null,
        credential_handle: "secret:v1:0123456789abcdef0123456789abcdef",
        allow_remote_https: false,
    });
    assert.match(modelHealthMessage(settings, "healthy"), /DeepSeek/);
    assert.match(modelHealthMessage(settings, "auth_required", "credential_unavailable"), /尚未安全保存/);
    assert.match(modelHealthMessage(settings, "auth_required", "auth_required"), /拒绝当前 API Key/);
    assert.match(modelHealthMessage(settings, "auth_required"), /认证失败/);
    assert.match(modelHealthMessage(settings, "unreachable"), /本机网络/);
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

test("subscription Runtime patch is credential-free and pins the Responses dialect", () => {
    const {
        modelCredentialProviderId,
        modelRuntimePatch,
        parseLocalSettings,
        usesProviderSecretStore,
    } = loadModule();
    const settings = parseLocalSettings({
        schemaVersion: 2,
        provider: "codex-subscription-experimental",
        wireApi: "ollama-chat",
        baseUrl: "https://untrusted.example/v1",
        proxyUrl: "http://127.0.0.1:7896",
    });
    assert.deepEqual(
        modelRuntimePatch(settings, "secret:v1:0123456789abcdef0123456789abcdef"),
        {
            provider: "codex-subscription-experimental",
            wire_api: "responses",
            model: "gpt-5.6-luna",
            reasoning_effort: "medium",
            base_url: "",
            proxy_url: "http://127.0.0.1:7896",
            credential_handle: null,
            allow_remote_https: false,
        },
    );
    assert.equal(usesProviderSecretStore(settings), false);
    assert.throws(() => modelCredentialProviderId(settings), /不使用 Provider SecretStore/);
});

test("subscription health messages distinguish login and loopback proxy recovery", () => {
    const { modelHealthMessage, parseLocalSettings } = loadModule();
    const direct = parseLocalSettings({ schemaVersion: 2, provider: "codex-subscription-experimental" });
    assert.match(modelHealthMessage(direct, "healthy"), /本机 Codex 登录/);
    assert.match(modelHealthMessage(direct, "auth_required"), /codex login/);
    assert.match(modelHealthMessage(direct, "unreachable"), /配置本机回环 HTTP 代理/);

    const proxied = parseLocalSettings({
        schemaVersion: 2,
        provider: "codex-subscription-experimental",
        proxyUrl: "http://127.0.0.1:7896",
    });
    assert.match(modelHealthMessage(proxied, "unreachable"), /确认本机 HTTP 代理正在运行/);
    assert.match(modelHealthMessage(proxied, "unsupported"), /更新本地 Runtime/);
});

test("remote HTTPS authorization is default-deny and bound to the exact canonical endpoint", () => {
    const { isRemoteHttpsEndpointApproved, modelRuntimePatch, parseLocalSettings, runConfig } = loadModule();
    const endpoint = "https://models.example/v1";
    const unapproved = parseLocalSettings({ provider: "openai-compatible", baseUrl: endpoint });
    assert.equal(unapproved.approvedRemoteHttpsEndpoint, null);
    assert.equal(isRemoteHttpsEndpointApproved(unapproved), false);
    assert.deepEqual(modelRuntimePatch(unapproved, "secret:v1:0123456789abcdef0123456789abcdef"), {
        provider: "codex",
        wire_api: "responses",
        model: "gpt-5.6-luna",
        reasoning_effort: "medium",
        base_url: "",
        proxy_url: null,
        credential_handle: null,
        allow_remote_https: false,
    });
    assert.throws(() => runConfig(unapproved), /尚未确认/);

    const approved = parseLocalSettings({
        provider: "openai-compatible",
        baseUrl: `${endpoint}/`,
        approvedRemoteHttpsEndpoint: endpoint,
    });
    assert.equal(approved.baseUrl, endpoint);
    assert.equal(approved.approvedRemoteHttpsEndpoint, endpoint);
    assert.equal(isRemoteHttpsEndpointApproved(approved), true);
    assert.equal(modelRuntimePatch(approved, null).allow_remote_https, true);
    assert.equal(runConfig(approved).provider, "openai-compatible");

    const changed = parseLocalSettings({
        provider: "openai-compatible",
        baseUrl: "https://other.example/v1",
        approvedRemoteHttpsEndpoint: endpoint,
    });
    assert.equal(changed.approvedRemoteHttpsEndpoint, null);
    assert.equal(modelRuntimePatch(changed, null).provider, "codex");
    assert.equal(modelRuntimePatch(changed, null).base_url, "");
});

test("compatible credential identity is endpoint-scoped with a fixed cross-language vector", () => {
    const { modelCredentialProviderId, parseLocalSettings } = loadModule();
    const endpoint = parseLocalSettings({
        provider: "openai-compatible",
        baseUrl: "https://MODELS.example:443//v1//",
    });
    assert.equal(
        modelCredentialProviderId(endpoint),
        "openai-compatible.00a98afb5b4eaaf4f9a877f0f3683900",
    );
    assert.match(modelCredentialProviderId(endpoint), /^[a-z][a-z0-9_.-]{0,63}$/);
    assert.notEqual(
        modelCredentialProviderId(endpoint),
        modelCredentialProviderId(parseLocalSettings({
            provider: "openai-compatible",
            baseUrl: "https://other.example/v1",
        })),
    );
    assert.equal(modelCredentialProviderId(parseLocalSettings({ provider: "openai" })), "openai");
    assert.throws(
        () => modelCredentialProviderId(parseLocalSettings({ provider: "openai-compatible" })),
        /先配置有效/,
    );
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

test("only a remote compatible endpoint can carry remote HTTPS authorization", () => {
    const { modelRuntimePatch, parseLocalSettings } = loadModule();
    const local = parseLocalSettings({
        provider: "local",
        approvedRemoteHttpsEndpoint: "https://models.example/v1",
    });
    assert.equal(local.approvedRemoteHttpsEndpoint, null);
    assert.equal(modelRuntimePatch(local, null).allow_remote_https, false);

    const loopback = parseLocalSettings({
        provider: "openai-compatible",
        baseUrl: "https://127.0.0.1:8443/v1",
        approvedRemoteHttpsEndpoint: "https://127.0.0.1:8443/v1",
    });
    assert.equal(loopback.approvedRemoteHttpsEndpoint, null);
    assert.equal(modelRuntimePatch(loopback, null).allow_remote_https, false);
});

test("remote compatible endpoint approval is gated by an explicit UI confirmation", () => {
    const source = require("node:fs").readFileSync(path.join(__dirname, "../src/local/settings.ts"), "utf8");
    assert.match(source, /允许远程兼容模型端点/);
    assert.match(source, /window\.confirm\(/);
    assert.match(source, /approvedRemoteHttpsEndpoint = endpoint/);
    assert.match(source, /const wasApproved = isRemoteHttpsEndpointApproved/);
    assert.match(source, /if \(wasApproved && changed\)[\s\S]*await this\.persistAndApply/);
    assert.match(source, /usesUnconfiguredCompatibleFallback\(this\.host\.settings\)/);
});

test("trusted Workspace can explicitly disable only Vault write prompts", () => {
    const { parseLocalSettings } = loadModule();
    const enabled = parseLocalSettings({ workspaceTrusted: true, autoApproveVaultWrites: true });
    const revoked = parseLocalSettings({ workspaceTrusted: false, autoApproveVaultWrites: true });
    const settingsSource = require("node:fs").readFileSync(path.join(__dirname, "../src/local/settings.ts"), "utf8");
    const mainSource = require("node:fs").readFileSync(path.join(__dirname, "../src/main.ts"), "utf8");

    assert.equal(enabled.autoApproveVaultWrites, true);
    assert.equal(revoked.autoApproveVaultWrites, false);
    assert.match(settingsSource, /Vault 写入无需逐次审批/);
    assert.match(settingsSource, /Shell 与网络权限不会因此开放/);
    assert.match(mainSource, /approve_vault_writes: !settings\.autoApproveVaultWrites/);
});

test("headless Vault status parser preserves the closed wire proof and renders Pipe authority", () => {
    const { headlessVaultWriteDescription, parseHeadlessVaultWriteStatus } = loadModule();
    const status = parseHeadlessVaultWriteStatus(headlessStatus());

    assert.equal(status.state, "pipe_client_tool");
    assert.equal(status.pipeConnectionCount, 1);
    assert.match(headlessVaultWriteDescription(status), /Pipe\/Client Tool 权威/);
    assert.match(headlessVaultWriteDescription(status), /Web 授权已动态撤销/);
    assert.match(headlessVaultWriteDescription(status), /Workspace revision 9/);
});

test("headless Vault status parser accepts a complete revocable authorization identity", () => {
    const { parseHeadlessVaultWriteStatus } = loadModule();
    const status = parseHeadlessVaultWriteStatus(headlessStatus({
        state: "active",
        pipeConnectionCount: 0,
        approvalId: "apr_headless_1",
        operationId: `op_headless_${"a".repeat(32)}`,
        argsHash: `sha256:${"b".repeat(64)}`,
        revision: 3,
        expiresAt: "2026-07-13T12:00:00+08:00",
        reasonCode: "authorization_active",
        userMessage: "授权已激活。",
        canRevoke: true,
    }));

    assert.equal(status.canRevoke, true);
    assert.equal(status.revision, 3);
    assert.equal(status.approvalId, "apr_headless_1");
});

test("headless Vault status parser rejects partial or incoherent authorization proofs", () => {
    const { parseHeadlessVaultWriteStatus } = loadModule();
    assert.throws(() => parseHeadlessVaultWriteStatus(headlessStatus({
        approvalId: "apr_headless_1",
    })), /身份字段不完整/);
    assert.throws(() => parseHeadlessVaultWriteStatus(headlessStatus({
        canActivate: true,
    })), /只有已批准/);
    assert.throws(() => parseHeadlessVaultWriteStatus(headlessStatus({
        canRevoke: true,
    })), /缺少审批身份/);
    assert.throws(() => parseHeadlessVaultWriteStatus(headlessStatus({
        pipeConnectionCount: 17,
    })), /Pipe 连接数.*无效/);
});
