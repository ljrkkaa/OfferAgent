const assert = require("node:assert/strict");
const { execFileSync } = require("node:child_process");
const { mkdtemp, rm } = require("node:fs/promises");
const os = require("node:os");
const path = require("node:path");
const test = require("node:test");
const { build } = require("esbuild");

test("sealed qualification driver may live under the guarded checkout without loading source", async (t) => {
    const repository = path.resolve(__dirname, "../../..");
    const outputRoot = await mkdtemp(path.join(repository, ".qualification-driver-fixture-"));
    t.after(async () => rm(outputRoot, { recursive: true, force: true }));
    const bundle = path.join(outputRoot, "offeragent-qualification-driver.cjs");
    await build({
        entryPoints: [path.join(__dirname, "../src/qualification_driver.ts")],
        bundle: true,
        external: ["node:*"],
        format: "cjs",
        platform: "node",
        target: "node16",
        outfile: bundle,
    });

    const stdout = execFileSync(process.execPath, [bundle, "probe"], {
        cwd: outputRoot,
        encoding: "utf8",
        env: {
            ...process.env,
            NODE_PATH: "",
            OFFERAGENT_QUALIFICATION_FORBID_SOURCE_ROOT: repository,
        },
        windowsHide: true,
    });

    const report = JSON.parse(stdout);
    assert.equal(report.sourceFreeRuntime, true);
    assert.equal(report.driverProtocolVersion, 1);
});
