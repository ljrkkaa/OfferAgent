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

test("production settings expose only the Codex Subscription catalog selection", async () => {
    const main = await readFile(path.join(__dirname, "../src/main.ts"), "utf8");
    const settings = await readFile(path.join(__dirname, "../src/local/settings.ts"), "utf8");

    assert.match(settings, /schemaVersion:\s*3/);
    assert.match(settings, /model:\s*""/);
    assert.match(settings, /实时 Codex 模型目录/);
    const runtimePatch = settings.slice(
        settings.indexOf("export function modelRuntimePatch"),
        settings.indexOf("export function snapshotLocalSettings"),
    );
    assert.match(runtimePatch, /model:\s*settings\.model/);
    assert.match(runtimePatch, /reasoning_effort:\s*settings\.reasoningEffort/);
    assert.match(runtimePatch, /proxy_url:\s*settings\.proxyUrl/);
    assert.doesNotMatch(runtimePatch, /provider|wire_api|base_url|credential_handle|allow_remote_https/);
    for (const retired of ["DeepSeek", "Ollama", "OpenAI-compatible", "模型 Provider", "模型协议", "模型端点"]) {
        assert.equal(settings.includes(retired), false, retired);
    }
    assert.doesNotMatch(main, /process\.env\.(?:HTTPS_PROXY|HTTP_PROXY)/);
});

test("Codex subscription login and transport have no production model SecretStore path", async () => {
    const main = await readFile(path.join(__dirname, "../src/main.ts"), "utf8");
    const settings = await readFile(path.join(__dirname, "../src/local/settings.ts"), "utf8");
    const apply = main.slice(
        main.indexOf("private async applyRuntimeSettingsToReadyRuntime"),
        main.indexOf("async checkModelCatalog"),
    );

    assert.match(settings, /Codex CLI 的 ChatGPT 登录/);
    assert.doesNotMatch(settings, /Provider 凭据|SecretStore|API Key/);
    assert.match(apply, /modelRuntimePatch\(settings\)/);
    assert.doesNotMatch(apply, /providerCredential|credentialHandle|usesProviderSecretStore/);
    assert.doesNotMatch(main, /saveProviderCredential|deleteProviderCredential|modelCredentialProviderId/);
    assert.doesNotMatch(main, /request\("secrets\/(?:put|delete)"/);
    assert.doesNotMatch(main, /checkModelHealth/);
});

test("model selection and catalog health accept only a fresh visible Codex model", async () => {
    const main = await readFile(path.join(__dirname, "../src/main.ts"), "utf8");
    const select = main.slice(main.indexOf("async selectModel"), main.indexOf("openSettings():"));
    const health = main.slice(main.indexOf("async checkModelCatalog"), main.indexOf("async extensionRequest"));

    assert.match(select, /item\.model === candidate && item\.available && item\.catalogFreshness === "fresh"/);
    assert.match(main, /async checkModelCatalog/);
    assert.match(health, /await this\.listModels\(\)/);
    assert.match(health, /catalogFreshness !== "fresh"|!model\.available/);
    assert.doesNotMatch(main, /request\("models\/health"/);
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
    assert.match(view, /editor\.setSelection/);
    assert.match(view, /target\.lineStart/);
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

test("Codex model settings apply is serialized and generation-checked without credentials", async () => {
    const main = await readFile(path.join(__dirname, "../src/main.ts"), "utf8");
    const apply = main.slice(
        main.indexOf("private enqueueRuntimeSettingsApply"),
        main.indexOf("async checkModelCatalog"),
    );

    assert.match(main, /runtimeSettingsWrites\s*=\s*new SerializedOperationQueue/);
    assert.match(apply, /runtimeSettingsWrites\.run/);
    assert.match(apply, /generation !== this\.settingsGeneration/);
    assert.match(apply, /modelRuntimePatch\(settings\)/);
    assert.doesNotMatch(apply, /credential|SecretStore|secrets\//i);
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

test("Obsidian owns only the generated Vault Tool Adapter boundary", async () => {
    const harness = await readFile(path.join(__dirname, "../src/runtime/harness_client.ts"), "utf8");
    const main = await readFile(path.join(__dirname, "../src/main.ts"), "utf8");
    const settings = await readFile(path.join(__dirname, "../src/local/settings.ts"), "utf8");
    for (const source of [harness, main, settings]) {
        assert.doesNotMatch(source, /headlessVaultWrite|vault\/headless|client\/tool|ClientReverseHandlers/);
    }
    assert.match(main, /new VaultToolAdapter\(this\.app\.vault, client, this\.workspaceId,/);
    assert.match(main, /observePluginToolEvents\(client\.reducer/);
    assert.doesNotMatch(main, /class AgentLoop|class Planner|ModelGateway/);
});

test("Vault Change recovery state survives plugin replacement and lifecycle teardown drains tools", async () => {
    const main = await readFile(path.join(__dirname, "../src/main.ts"), "utf8");
    const createRuntime = main.slice(main.indexOf("private createRuntime"), main.indexOf("private attachVaultToolAdapter"));
    const attach = main.slice(main.indexOf("private attachVaultToolAdapter"), main.indexOf("private authorizeVaultChange"));
    const stopCurrent = main.slice(main.indexOf("private async stopLocalRuntime"), main.indexOf("private createRuntime"));
    const restart = main.slice(main.indexOf("private async performRuntimeRestartIfIdle"), main.indexOf("private scheduleRuntimeRestartCheck"));
    const unload = main.slice(main.indexOf("onunload(): void"), main.indexOf("runtimeSnapshot()"));

    assert.match(createRuntime, /resolve\(this\.vaultRoot, this\.app\.vault\.configDir, "offeragent", "vault-change-journal"\)/);
    assert.doesNotMatch(attach, /new FileVaultChangeJournal\(resolve\(pluginInstallDirectory\([^)]*\), "vault-change-journal"\)\)/);
    assert.match(attach, /journal\.migrateLegacyDirectory\(resolve\(pluginInstallDirectory\(this, this\.vaultRoot\), "vault-change-journal"\)\)/);
    assert.match(attach, /new SerializedPluginToolExecutionFence\(/);
    assert.match(createRuntime, /randomBytes\(32\)\.toString\("hex"\)/);
    assert.match(createRuntime, /new StdioWorkerTransport\([\s\S]*journalDirectory,[\s\S]*recoveryToken/);
    assert.match(createRuntime, /beforeConnect:[\s\S]*fence\.ready\(\)/);
    assert.ok(attach.indexOf("changes.beginRecovery()") < attach.indexOf("journal.markRecoveryReady(recoveryToken)"));
    assert.ok(stopCurrent.indexOf("await this.disposeVaultToolAdapter()") < stopCurrent.indexOf("await runtime.stop()"));
    assert.ok(restart.indexOf("await this.disposeVaultToolAdapter()") < restart.indexOf("await this.runtime.stop()"));
    assert.match(unload, /const vaultToolRetirement = this\.disposeVaultToolAdapter\(\)/);
    assert.match(unload, /vaultToolRetirement/);
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
