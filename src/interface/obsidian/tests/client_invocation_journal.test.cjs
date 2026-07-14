const assert = require("node:assert/strict");
const { createHash } = require("node:crypto");
const { mkdtemp, readFile, rm, writeFile } = require("node:fs/promises");
const { tmpdir } = require("node:os");
const path = require("node:path");
const test = require("node:test");
const { buildSync } = require("esbuild");

function loadModule() {
    const output = buildSync({
        entryPoints: [path.join(__dirname, "../src/local/client_invocation_journal.ts")],
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

async function fixture(t) {
    const directory = await mkdtemp(path.join(tmpdir(), "offeragent-journal-"));
    t.after(() => rm(directory, { recursive: true, force: true }));
    return path.join(directory, "client-invocations.json");
}

function startRecord(id = "inv_01") {
    return {
        invocationId: id,
        toolCallId: "call_01",
        runId: "run_01",
        requestHash: `sha256:${"a".repeat(64)}`,
        recovery: [{ path: "notes/a.md", beforeHash: "absent", afterHash: `sha256:${"b".repeat(64)}` }],
    };
}

function result(id = "inv_01") {
    return {
        invocationId: id,
        toolCallId: "call_01",
        status: "succeeded",
        output: { paths: ["notes/a.md"] },
        userVisibleSummary: "done",
        beforeHash: null,
        afterHash: null,
        beforeState: null,
        afterState: null,
        workspaceRevision: 1,
        artifactIds: [],
        sourceReferenceIds: [],
        sideEffectFacts: [],
        actualOperations: [],
        error: null,
    };
}

test("journal makes start and completed result durable across instances", async (t) => {
    const { ClientInvocationJournal } = loadModule();
    const file = await fixture(t);
    const first = new ClientInvocationJournal(file, "wsi_01");
    const started = await first.begin(startRecord());
    assert.equal(started.state, "started");
    await first.complete("inv_01", startRecord().requestHash, result());

    const reopened = new ClientInvocationJournal(file, "wsi_01");
    const stored = await reopened.lookup("inv_01");
    assert.equal(stored.state, "completed");
    assert.deepEqual(stored.result, result());
    const raw = await readFile(file, "utf8");
    assert.equal(raw.endsWith("\n"), true);
});
test("invocation id cannot be rebound to another Run or args hash", async (t) => {
    const { ClientInvocationJournal, InvocationJournalConflict } = loadModule();
    const journal = new ClientInvocationJournal(await fixture(t), "wsi_01");
    await journal.begin(startRecord());
    await assert.rejects(
        () => journal.begin({ ...startRecord(), runId: "run_02" }),
        InvocationJournalConflict,
    );
    await assert.rejects(
        () => journal.begin({ ...startRecord(), requestHash: `sha256:${"c".repeat(64)}` }),
        InvocationJournalConflict,
    );
});

test("concurrent begins serialize without duplicate records", async (t) => {
    const { ClientInvocationJournal } = loadModule();
    const file = await fixture(t);
    const journal = new ClientInvocationJournal(file, "wsi_01");
    await Promise.all(Array.from({ length: 20 }, (_, index) => journal.begin(startRecord(`inv_${index + 1}`))));
    const raw = JSON.parse(await readFile(file, "utf8"));
    assert.equal(raw.records.length, 20);
    assert.equal(new Set(raw.records.map((record) => record.invocationId)).size, 20);
});

test("corruption and cross-Workspace journal reuse fail closed", async (t) => {
    const { ClientInvocationJournal, InvocationJournalConflict } = loadModule();
    const file = await fixture(t);
    const journal = new ClientInvocationJournal(file, "wsi_01");
    await journal.begin(startRecord());
    await assert.rejects(() => new ClientInvocationJournal(file, "wsi_02").lookup("inv_01"), InvocationJournalConflict);
    await writeFile(file, "{bad json", "utf8");
    await assert.rejects(() => journal.lookup("inv_01"), InvocationJournalConflict);
});

test("canonical JSON hash is byte-compatible with the Python Tool Kernel", () => {
    const { canonicalJson } = loadModule();
    const text = canonicalJson({ z: 1, a: { y: [true, null, "你"], x: -2.5 } });
    const hash = `sha256:${createHash("sha256").update(text, "utf8").digest("hex")}`;
    assert.equal(hash, "sha256:05c2a6d50e953903b824ad38453e306f52c363f4b4ca6a2e62d8394dc8eda0ad");
});
