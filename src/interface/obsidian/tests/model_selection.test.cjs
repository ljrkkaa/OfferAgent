const assert = require("node:assert/strict");
const path = require("node:path");
const test = require("node:test");
const { buildSync } = require("esbuild");

function loadPluginModule() {
    const output = buildSync({
        entryPoints: [path.join(__dirname, "../src/main.ts")],
        bundle: true,
        external: ["obsidian"],
        format: "cjs",
        platform: "node",
        target: "node16",
        write: false,
    }).outputFiles[0].text;
    const compiled = { exports: {} };
    class Component {}
    const obsidian = {
        FileSystemAdapter: class {},
        ItemView: Component,
        Menu: class {},
        Modal: Component,
        Notice: class {},
        Plugin: Component,
        PluginSettingTab: Component,
        Setting: class {},
        setIcon: () => undefined,
    };
    const localRequire = (specifier) => specifier === "obsidian" ? obsidian : require(specifier);
    new Function("require", "module", "exports", output)(localRequire, compiled, compiled.exports);
    return compiled.exports.default;
}

const ACCOUNT_A = `sha256:${"a".repeat(64)}`;
const ACCOUNT_B = `sha256:${"b".repeat(64)}`;

function catalogModel(model = "gpt-catalog-model") {
    return {
        provider: "codex-subscription-experimental",
        model,
        displayName: "GPT Catalog Model",
        supportsStreaming: true,
        supportsStructuredOutput: true,
        inputModalities: ["text", "image"],
        supportsImageDetailOriginal: true,
        supportsHostedSearch: true,
        supportsFastMode: false,
        contextWindow: 128_000,
        available: true,
    };
}

function modelChoice(model, accountBinding) {
    return {
        ...catalogModel(model),
        accountBinding,
        catalogFreshness: "fresh",
    };
}

function pluginWithCatalog(response) {
    const OfferAgentPlugin = loadPluginModule();
    const plugin = Object.create(OfferAgentPlugin.prototype);
    plugin.ensureReady = async () => undefined;
    plugin.runtime = {
        harness: {
            request: async (method) => {
                assert.equal(method, "models/list");
                return response;
            },
        },
    };
    return plugin;
}

test("fresh model choices carry the exact account binding from their catalog snapshot", async () => {
    const plugin = pluginWithCatalog({
        models: [catalogModel()],
        configRevision: 3,
        catalogFreshness: "fresh",
        accountBinding: ACCOUNT_A,
        error: null,
    });

    assert.deepEqual(await plugin.listModels(), [modelChoice("gpt-catalog-model", ACCOUNT_A)]);
});

test("a fresh catalog without one valid account binding is rejected before it reaches selection", async () => {
    for (const accountBinding of [null, undefined, "", `sha256:${"A".repeat(64)}`, `sha256:${"a".repeat(63)}`]) {
        const plugin = pluginWithCatalog({
            models: [catalogModel()],
            configRevision: 3,
            catalogFreshness: "fresh",
            ...(accountBinding === undefined ? {} : { accountBinding }),
            error: null,
        });
        await assert.rejects(plugin.listModels(), /账户绑定|account binding/i);
    }
});

test("selecting a fresh model persists its model and account binding together", async () => {
    const OfferAgentPlugin = loadPluginModule();
    const plugin = Object.create(OfferAgentPlugin.prototype);
    plugin.settings = { model: "old-model", modelAccountBinding: ACCOUNT_A };
    plugin.listModels = async () => [modelChoice("new-model", ACCOUNT_B)];
    const saves = [];
    plugin.saveLocalSettings = async () => {
        saves.push({
            model: plugin.settings.model,
            modelAccountBinding: plugin.settings.modelAccountBinding,
        });
    };
    plugin.applyRuntimeSettings = async () => undefined;

    await plugin.selectModel("new-model");

    assert.deepEqual(saves, [{ model: "new-model", modelAccountBinding: ACCOUNT_B }]);
    assert.deepEqual(
        { model: plugin.settings.model, modelAccountBinding: plugin.settings.modelAccountBinding },
        { model: "new-model", modelAccountBinding: ACCOUNT_B },
    );
});

test("a failed Runtime apply rolls the model and account binding back as one persisted pair", async () => {
    const OfferAgentPlugin = loadPluginModule();
    const plugin = Object.create(OfferAgentPlugin.prototype);
    plugin.settings = { model: "old-model", modelAccountBinding: ACCOUNT_A };
    plugin.listModels = async () => [modelChoice("new-model", ACCOUNT_B)];
    const saves = [];
    plugin.saveLocalSettings = async () => {
        saves.push({
            model: plugin.settings.model,
            modelAccountBinding: plugin.settings.modelAccountBinding,
        });
    };
    plugin.applyRuntimeSettings = async () => {
        throw new Error("Runtime rejected model selection");
    };

    await assert.rejects(plugin.selectModel("new-model"), /Runtime rejected/);

    assert.deepEqual(saves, [
        { model: "new-model", modelAccountBinding: ACCOUNT_B },
        { model: "old-model", modelAccountBinding: ACCOUNT_A },
    ]);
    assert.deepEqual(
        { model: plugin.settings.model, modelAccountBinding: plugin.settings.modelAccountBinding },
        { model: "old-model", modelAccountBinding: ACCOUNT_A },
    );
});

test("catalog health does not present the same model id from another account as the current selection", async () => {
    const OfferAgentPlugin = loadPluginModule();
    const plugin = Object.create(OfferAgentPlugin.prototype);
    plugin.settings = { model: "shared-model", modelAccountBinding: ACCOUNT_A };
    plugin.listModels = async () => [modelChoice("shared-model", ACCOUNT_B)];

    assert.match(await plugin.checkModelCatalog(), /请.*选择/);
});
