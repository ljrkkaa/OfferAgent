const assert = require("node:assert/strict");
const { readdir, readFile } = require("node:fs/promises");
const path = require("node:path");
const test = require("node:test");

async function sourceFiles(directory) {
    const output = [];
    for (const entry of await readdir(directory, { withFileTypes: true })) {
        const target = path.join(directory, entry.name);
        if (entry.isDirectory()) output.push(...await sourceFiles(target));
        else if (entry.name.endsWith(".ts")) output.push(target);
    }
    return output;
}

test("production plugin source contains no prohibited HTTP Agent or content-sync path", async () => {
    const root = path.join(__dirname, "../src");
    const files = await sourceFiles(root);
    const combined = (await Promise.all(files.map((file) => readFile(file, "utf8")))).join("\n");

    for (const forbidden of [
        "OfferAgentServer", "khojUrl", "khojApiKey", "api/chat", "12805",
        "updateContentIndex", "syncFolders", "connectedToBackend",
        "auth.json", "chatgpt.com", "backend-api", "access_token", "refresh_token", "id_token",
        "api.deepseek.com",
        "fetch(", "XMLHttpRequest", "requestUrl(",
    ]) assert.equal(combined.includes(forbidden), false, `production boundary token remains: ${forbidden}`);
    assert.equal(files.some((file) => /(?:api|chat_runtime|interact_with_files|similar_view)\.ts$/.test(file)), false);
});

test("UI adapters do not own an Agent loop, Planner, model gateway, or direct model request", async () => {
    const uiRoot = path.join(__dirname, "../src/local");
    const files = await sourceFiles(uiRoot);
    const combined = (await Promise.all(files.map((file) => readFile(file, "utf8")))).join("\n");
    for (const forbidden of [
        "class AgentLoop", "class Planner", "ModelGateway", "api.openai.com", "codex app-server",
        "auth.json", "chatgpt.com", "backend-api", "access_token", "refresh_token", "id_token",
        "api.deepseek.com",
        "fetch(", "XMLHttpRequest", "requestUrl(",
    ]) {
        assert.equal(combined.includes(forbidden), false, `UI boundary violation: ${forbidden}`);
    }
});

test("DeepSeek UI is fixed configuration and delegates credentials to the authenticated Worker", async () => {
    const main = await readFile(path.join(__dirname, "../src/main.ts"), "utf8");
    const settings = await readFile(path.join(__dirname, "../src/local/settings.ts"), "utf8");

    assert.match(settings, /DeepSeek API（API Key）/);
    assert.match(settings, /provider:\s*"deepseek"/);
    assert.match(settings, /wireApi:\s*"chat-completions"/);
    assert.match(settings, /model:\s*"deepseek-v4-flash"/);
    assert.match(settings, /schemaVersion:\s*2/);
    assert.match(main, /interface LocalPluginData[\s\S]*schemaVersion:\s*2/);
    assert.match(settings, /if \(provider === DEEPSEEK_PROVIDER\) return "chat-completions"/);
    assert.match(settings, /if \(settings\.provider !== "openai-compatible"\) return settings\.provider/);
    assert.match(main, /request\("secrets\/put", params\)/);
    assert.doesNotMatch(main, /migrateUnconfiguredLegacyCodexSubscription/);
    assert.doesNotMatch(main, /process\.env\.(?:HTTPS_PROXY|HTTP_PROXY)/);
});

test("Codex subscription UI delegates login and transport to Worker without SecretStore calls", async () => {
    const main = await readFile(path.join(__dirname, "../src/main.ts"), "utf8");
    const settings = await readFile(path.join(__dirname, "../src/local/settings.ts"), "utf8");
    const apply = main.slice(
        main.indexOf("private async applyRuntimeSettingsToReadyRuntime"),
        main.indexOf("async saveProviderCredential"),
    );
    const saveCredential = main.slice(
        main.indexOf("async saveProviderCredential"),
        main.indexOf("async deleteProviderCredential"),
    );
    const deleteCredential = main.slice(
        main.indexOf("async deleteProviderCredential"),
        main.indexOf("async checkModelHealth"),
    );

    assert.match(settings, /Codex 订阅（实验，本机登录）/);
    assert.match(settings, /Codex\/OpenAI API（API Key）/);
    assert.match(settings, /if \(usesProviderSecretStore\(this\.host\.settings\)\)/);
    assert.match(apply, /!usesProviderSecretStore\(settings\)[\s\S]*\? null/);
    assert.match(saveCredential, /if \(!usesProviderSecretStore\(settings\)\)[\s\S]*throw new Error/);
    assert.match(deleteCredential, /if \(!usesProviderSecretStore\(settings\)\)[\s\S]*throw new Error/);
    assert.match(settings, /credential_handle: subscription \? null : credentialHandle/);
});

test("chat history delegates Session hydration and replay to the single ChatStore path", async () => {
    const main = await readFile(path.join(__dirname, "../src/main.ts"), "utf8");
    const view = await readFile(path.join(__dirname, "../src/local/chat_view.ts"), "utf8");
    const store = await readFile(path.join(__dirname, "../src/runtime/chat_store.ts"), "utf8");
    const ensureStore = main.slice(main.indexOf("async ensureChatStore"), main.indexOf("async captureClientContext"));

    assert.match(view, /openSession\(session\.sessionId\)/);
    assert.doesNotMatch(view, /createTab\(session\.sessionId/);
    assert.doesNotMatch(ensureStore, /session\/get|replaySession/);
    assert.match(store, /request\("session\/get", \{ sessionId, includeTurns: true \}\)/);
    assert.match(store, /replaySession\(sessionId, replayCursors\)/);
});

test("chat citations render and navigate through typed SourceRef helpers", async () => {
    const view = await readFile(path.join(__dirname, "../src/local/chat_view.ts"), "utf8");
    assert.match(view, /sourceReferenceLabel\(reference\)/);
    assert.match(view, /vaultReferenceTarget\(reference\)/);
    assert.match(view, /openLinkText\(linkText/);
    assert.doesNotMatch(view, /reference\.(?:path|sourceId)/);
});

test("Subagents require an explicit local setting and send a bounded execution configuration", async () => {
    const main = await readFile(path.join(__dirname, "../src/main.ts"), "utf8");
    const settings = await readFile(path.join(__dirname, "../src/local/settings.ts"), "utf8");
    assert.match(main, /subagents_enabled:\s*settings\.subagentsEnabled/);
    assert.match(main, /max_subagents_per_vault:\s*settings\.subagentsEnabled\s*\?\s*[1-9][0-9]*\s*:\s*0/);
    assert.doesNotMatch(main, /subagent.*depth/i);
    assert.match(settings, /subagentsEnabled:\s*false/);
});

test("workspace trust is an independent explicit confirmation and never follows the default mode", async () => {
    const main = await readFile(path.join(__dirname, "../src/main.ts"), "utf8");
    const settings = await readFile(path.join(__dirname, "../src/local/settings.ts"), "utf8");
    assert.match(main, /workspace_trusted:\s*settings\.workspaceTrusted/);
    assert.doesNotMatch(main, /workspace_trusted:\s*settings\.permissionMode/);
    assert.match(settings, /workspaceTrusted:\s*false/);
    assert.match(settings, /信任当前 Workspace/);
    assert.match(settings, /window\.confirm/);
    assert.match(settings, /尚未信任：当前有效权限为只读/);
});

test("model settings apply is serialized, generation-checked, and credential metadata is endpoint-bound", async () => {
    const main = await readFile(path.join(__dirname, "../src/main.ts"), "utf8");
    const apply = main.slice(
        main.indexOf("private enqueueRuntimeSettingsApply"),
        main.indexOf("async saveProviderCredential"),
    );
    const credentials = main.slice(
        main.indexOf("async saveProviderCredential"),
        main.indexOf("private persistLocalData"),
    );

    assert.match(main, /runtimeSettingsWrites\s*=\s*new SerializedOperationQueue/);
    assert.match(apply, /runtimeSettingsWrites\.run/);
    assert.match(apply, /generation !== this\.settingsGeneration/);
    assert.match(apply, /modelRuntimePatch\(settings,/);
    assert.doesNotMatch(apply, /modelRuntimePatch\(this\.settings,/);
    assert.match(credentials, /modelCredentialProviderId\(settings\)/);
    assert.match(credentials, /metadata\.kind !== "model-provider"/);
    assert.match(credentials, /metadata\.providerId !== providerId/);
    assert.match(credentials, /details\.reason/);
    assert.match(credentials, /modelHealthMessage\(settings, status, reason\)/);
});

test("Subagent protocol support is structural while execution remains configuration-controlled", async () => {
    const harness = await readFile(path.join(__dirname, "../src/runtime/harness_client.ts"), "utf8");
    const main = await readFile(path.join(__dirname, "../src/main.ts"), "utf8");
    const requiredBlock = harness.match(/REQUIRED_RUNTIME_CAPABILITIES[\s\S]*?Object\.freeze\(\[([\s\S]*?)\]\)/);

    assert.ok(requiredBlock, "explicit required Runtime capability list is missing");
    assert.equal(requiredBlock[1].includes('"subagents"'), true);
    assert.match(main, /requiredCapabilities:\s*REQUIRED_RUNTIME_CAPABILITIES/);
    assert.equal(main.includes("Object.keys(CLIENT_CAPABILITIES)"), false);
});

test("Obsidian exposes only Pipe-authoritative headless status and explicit revoke", async () => {
    const harness = await readFile(path.join(__dirname, "../src/runtime/harness_client.ts"), "utf8");
    const main = await readFile(path.join(__dirname, "../src/main.ts"), "utf8");
    const settings = await readFile(path.join(__dirname, "../src/local/settings.ts"), "utf8");
    const headlessMethods = [...main.matchAll(/"(vault\/headless\/[a-z-]+)"/g)].map((match) => match[1]).sort();

    assert.match(harness, /headlessVaultWrite:\s*true/);
    assert.deepEqual(headlessMethods, ["vault/headless/revoke", "vault/headless/status"]);
    assert.equal(main.includes("vault/headless/request"), false);
    assert.equal(main.includes("vault/headless/activate"), false);
    assert.match(settings, /Pipe\/Client Tool 权威/);
    assert.match(settings, /Web 授权已动态撤销/);
    assert.match(settings, /status\?\.canRevoke/);
});

test("configuration restart drains client ACK windows and unload aborts before awaiting lifecycle work", async () => {
    const main = await readFile(path.join(__dirname, "../src/main.ts"), "utf8");
    const restart = main.slice(
        main.indexOf("private async performRuntimeRestartIfIdle"),
        main.indexOf("private scheduleRuntimeRestartCheck"),
    );
    const unload = main.slice(main.indexOf("async onunload"), main.indexOf("runtimeSnapshot()"));
    const startup = main.slice(main.indexOf("private async startRuntime"), main.indexOf("private restartRuntimeIfIdle"));
    const stopAll = main.slice(main.indexOf("private async stopAllLocalRuntime"), main.indexOf("private createRuntime"));

    assert.ok(restart.indexOf("this.chatStore?.snapshot.busy") >= 0);
    assert.ok(restart.indexOf("this.chatStore?.snapshot.busy") < restart.indexOf("this.chatStore?.dispose"));
    assert.ok(restart.indexOf("this.chatStore?.dispose") < restart.indexOf("this.runtime.stop"));
    assert.ok(unload.indexOf("this.runtime?.stop") < unload.indexOf("await Promise.all"));
    assert.match(startup, /enqueueRuntimeSettingsApply/);
    assert.doesNotMatch(startup, /await this\.applyRuntimeSettings\(\)/);
    assert.ok(stopAll.indexOf("runtimeExplicitlyStopped = true") < stopAll.indexOf("await runtime.stop"));
    assert.ok(stopAll.indexOf("await runtime.stop") < stopAll.indexOf("await Promise.all"));
});
