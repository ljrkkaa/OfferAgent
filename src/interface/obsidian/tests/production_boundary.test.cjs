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
    const ensureStore = main.slice(main.indexOf("async ensureChatStore"), main.indexOf("async readArtifactText"));

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

test("bypass mode is rendered explicitly and cannot be mistaken for planning", async () => {
    const view = await readFile(path.join(__dirname, "../src/local/chat_view.ts"), "utf8");
    assert.match(view, /mode === "bypass"\) return "免审批执行"/);
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

test("Obsidian contains no plugin-owned Vault execution authority", async () => {
    const harness = await readFile(path.join(__dirname, "../src/runtime/harness_client.ts"), "utf8");
    const main = await readFile(path.join(__dirname, "../src/main.ts"), "utf8");
    const settings = await readFile(path.join(__dirname, "../src/local/settings.ts"), "utf8");
    for (const source of [harness, main, settings]) {
        assert.doesNotMatch(source, /headlessVaultWrite|vault\/headless|client\/tool|ClientReverseHandlers/);
    }
});

test("configuration restart drains client ACK windows and unload synchronously initiates teardown", async () => {
    const main = await readFile(path.join(__dirname, "../src/main.ts"), "utf8");
    const restart = main.slice(
        main.indexOf("private async performRuntimeRestartIfIdle"),
        main.indexOf("private scheduleRuntimeRestartCheck"),
    );
    const unload = main.slice(main.indexOf("onunload(): void"), main.indexOf("runtimeSnapshot()"));
    const startup = main.slice(main.indexOf("private async startRuntime"), main.indexOf("private restartRuntimeIfIdle"));
    const stopCurrent = main.slice(main.indexOf("private async stopLocalRuntime"), main.indexOf("private createRuntime"));

    assert.ok(restart.indexOf("this.chatStore?.snapshot.busy") >= 0);
    assert.ok(restart.indexOf("this.chatStore?.snapshot.busy") < restart.indexOf("this.chatStore?.dispose"));
    assert.ok(restart.indexOf("this.chatStore?.dispose") < restart.indexOf("this.runtime.stop"));
    assert.notEqual(unload.length, 0);
    assert.doesNotMatch(unload, /async onunload|\bawait\b/);
    assert.match(unload, /this\.runtime\?\.beginUnload\(\)/);
    assert.match(unload, /void Promise\.all/);
    assert.match(startup, /enqueueRuntimeSettingsApply/);
    assert.doesNotMatch(startup, /await this\.applyRuntimeSettings\(\)/);
    assert.ok(stopCurrent.indexOf("runtimeExplicitlyStopped = true") < stopCurrent.indexOf("await runtime.stop"));
    assert.ok(stopCurrent.indexOf("await runtime.stop") < stopCurrent.indexOf("await Promise.all"));
});

test("personal plugin lifecycle exposes only its direct child Worker", async () => {
    const main = await readFile(path.join(__dirname, "../src/main.ts"), "utf8");
    const bootstrap = await readFile(path.join(__dirname, "../src/runtime/bootstrap.ts"), "utf8");
    const settings = await readFile(path.join(__dirname, "../src/local/settings.ts"), "utf8");
    const pluginReadme = await readFile(path.join(__dirname, "../README.md"), "utf8");

    for (const source of [main, bootstrap, settings]) {
        assert.doesNotMatch(source, /starting_host|attaching_worker|keepWorkerInBackground|stop-all-local-runtime/);
    }
    assert.match(main, /new StdioWorkerTransport/);
    assert.match(main, /停止当前 Vault 的 OfferAgent Runtime/);
    assert.match(
        pluginReadme,
        /没有常驻协调 Host、插件 IPC 中间 Host、discovery、Named Pipe、listener、后台 Worker 或常驻运行模式/,
    );
    assert.match(
        pluginReadme,
        /插件热重载、禁用、Obsidian 退出或 `onunload` 会在回调返回前同步关闭 stdio、发起当前子 Worker/,
    );
    assert.match(pluginReadme, /实际进程 join 在后台继续/);
});

test("plugin build exposes only the pinned local-development Runtime installer", async () => {
    const main = await readFile(path.join(__dirname, "../src/main.ts"), "utf8");
    const build = await readFile(path.join(__dirname, "../esbuild.config.mjs"), "utf8");
    const bootstrap = await readFile(path.join(__dirname, "../src/runtime/bootstrap.ts"), "utf8");
    const packageJson = JSON.parse(await readFile(path.join(__dirname, "../package.json"), "utf8"));
    const runtimeFiles = await readdir(path.join(__dirname, "../src/runtime"));

    assert.match(main, /new LocalDevelopmentRuntimeInstaller/);
    assert.doesNotMatch(main, /RELEASE_PUBLIC_KEYS|releasePublicKeys|generated_release_keyring|installer_mode/);
    assert.match(build, /process\.argv\[2\] !== 'local-development'/);
    assert.match(build, /__OFFERAGENT_DEVELOPMENT_MANIFEST_SHA256__/);
    assert.doesNotMatch(build, /production|installerSelection|onResolve/);
    assert.equal(packageJson.scripts.build, undefined);
    assert.equal(packageJson.scripts.dev, undefined);
    assert.match(packageJson.scripts["build:local"], /local-development/);
    assert.doesNotMatch(bootstrap, /installing|extracting_to_staging|verifying_each_file|atomic_activate|runtime_self_test/);
    assert.equal(runtimeFiles.includes("generated_release_keyring.ts"), false);
    assert.equal(runtimeFiles.some((name) => name.startsWith("installer_mode")), false);
});
