const assert = require("node:assert/strict");
const { execFileSync, spawn } = require("node:child_process");
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

test("sealed qualification driver exposes a durable line protocol before product startup", async (t) => {
    const repository = path.resolve(__dirname, "../../..");
    const outputRoot = await mkdtemp(path.join(repository, ".qualification-driver-serve-fixture-"));
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

    const child = spawn(process.execPath, [bundle, "serve"], {
        cwd: outputRoot,
        env: {
            ...process.env,
            NODE_PATH: "",
            OFFERAGENT_QUALIFICATION_FORBID_SOURCE_ROOT: repository,
        },
        stdio: ["pipe", "pipe", "pipe"],
        windowsHide: true,
    });
    t.after(() => child.kill());

    const lines = [];
    child.stdout.setEncoding("utf8");
    child.stdout.on("data", (chunk) => lines.push(...chunk.trim().split(/\r?\n/u).filter(Boolean)));
    child.stdin.write(`${JSON.stringify({ id: "req_1", command: "hello", params: {} })}\n`);

    const response = await new Promise((resolve, reject) => {
        const deadline = setTimeout(() => reject(new Error("qualification driver did not answer hello")), 5_000);
        const inspect = () => {
            const match = lines.map((line) => JSON.parse(line)).find((line) => line.id === "req_1");
            if (match === undefined) return;
            clearTimeout(deadline);
            resolve(match);
        };
        child.stdout.on("data", inspect);
        inspect();
    });

    assert.deepEqual(response, {
        id: "req_1",
        ok: true,
        result: {
            driverProtocolVersion: 2,
            reviewResolution: "explicit",
            sourceFreeRuntime: true,
        },
    });
    child.stdin.write(`${JSON.stringify({ id: "req_2", command: "stop", params: {} })}\n`);
    await new Promise((resolve, reject) => {
        child.once("exit", (code) => code === 0 ? resolve() : reject(new Error(`driver exited ${code}`)));
    });
});
