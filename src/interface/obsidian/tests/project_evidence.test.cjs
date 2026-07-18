const assert = require("node:assert/strict");
const { link, mkdir, mkdtemp, rm, writeFile } = require("node:fs/promises");
const os = require("node:os");
const path = require("node:path");
const test = require("node:test");
const { buildSync } = require("esbuild");

function loadModule() {
    const output = buildSync({
        entryPoints: [path.join(__dirname, "../src/runtime/project_evidence.ts")],
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

function call(name, arguments_, id = `call_${name}`) {
    return {
        toolCallId: id,
        workspaceId: "ws_vault",
        runId: "run_project",
        name,
        version: "1",
        arguments: arguments_,
        argsHash: `sha256:${"a".repeat(64)}`,
        idempotencyKey: id,
        risk: "read",
        reason: null,
        agentLineage: ["run_project"],
        executorLocation: "plugin",
        definitionFingerprint: `sha256:${"b".repeat(64)}`,
        resultSensitivity: "workspace",
        deadline: null,
    };
}

function registeredVault(root, registered = true) {
    const indexContent = registered ? "# Registry\n\n- [[projects/offeragent|OfferAgent]]\n" : "# Registry\n";
    const descriptorContent = `---\nproject-id: offeragent\nproject-root: ${root}\n---\n`;
    const files = [
        { path: "projects/index.md", extension: "md", stat: { mtime: 1, size: Buffer.byteLength(indexContent) }, content: indexContent },
        { path: "projects/offeragent.md", extension: "md", stat: { mtime: 2, size: Buffer.byteLength(descriptorContent) }, content: descriptorContent },
    ];
    return {
        getFiles: () => files,
        getFileByPath: (target) => files.find((file) => file.path === target) ?? null,
        cachedRead: async (file) => file.content,
    };
}

test("Project Registry exclusively authorizes bounded list, search, and precise read", async (t) => {
    const { ProjectEvidenceAdapter } = loadModule();
    const root = await mkdtemp(path.join(os.tmpdir(), "offeragent-project-"));
    t.after(() => rm(root, { recursive: true, force: true }));
    await mkdir(path.join(root, "src"), { recursive: true });
    await writeFile(path.join(root, "src", "agent.py"), "def answer():\n    return 'current evidence'\n");
    await writeFile(path.join(root, ".env"), "TOKEN=secret\n");
    const adapter = new ProjectEvidenceAdapter(registeredVault(root));

    const listed = await adapter.execute(call("project.list", { projectId: "offeragent", limit: 10 }));
    assert.equal(listed.status, "succeeded");
    assert.deepEqual(listed.data.entries.map((entry) => entry.path), ["src/agent.py"]);
    assert.equal(JSON.stringify(listed).includes(root), false);

    const searched = await adapter.execute(call("project.search", {
        projectId: "offeragent", query: "current evidence", limit: 10,
    }));
    assert.equal(searched.status, "succeeded");
    assert.equal(searched.data.entries[0].snippets[0].lineStart, 2);

    const candidate = searched.data.entries[0];
    const read = await adapter.execute(call("project.read", {
        projectId: "offeragent",
        path: candidate.path,
        lineStart: 2,
        lineEnd: 2,
        expectedContentHash: candidate.contentHash,
        expectedModifiedVersion: candidate.modifiedVersion,
    }));
    assert.equal(read.status, "succeeded");
    assert.equal(read.data.content, "    return 'current evidence'");
    assert.deepEqual(read.sourceRefs[0], {
        type: "project",
        projectId: "offeragent",
        path: "src/agent.py",
        contentHash: candidate.contentHash,
        modifiedVersion: candidate.modifiedVersion,
        lineStart: 2,
        lineEnd: 2,
        freshness: "fresh",
    });

    const denied = await new ProjectEvidenceAdapter(registeredVault(root, false)).execute(
        call("project.read", { projectId: "offeragent", path: "src/agent.py" }),
    );
    assert.equal(denied.status, "failed");
    assert.equal(denied.error.code, "policy.denied");
});

test("Project Evidence rejects stale selections and excluded or escaping sources", async (t) => {
    const { ProjectEvidenceAdapter } = loadModule();
    const root = await mkdtemp(path.join(os.tmpdir(), "offeragent-project-policy-"));
    t.after(() => rm(root, { recursive: true, force: true }));
    await mkdir(path.join(root, "src"), { recursive: true });
    const target = path.join(root, "src", "agent.py");
    await writeFile(target, "old evidence\n");
    const adapter = new ProjectEvidenceAdapter(registeredVault(root));
    const search = await adapter.execute(call("project.search", { projectId: "offeragent", query: "old" }));
    await writeFile(target, "new evidence with a different size\n");
    const stale = await adapter.execute(call("project.read", {
        projectId: "offeragent",
        path: "src/agent.py",
        expectedContentHash: search.data.entries[0].contentHash,
        expectedModifiedVersion: search.data.entries[0].modifiedVersion,
    }));
    assert.equal(stale.status, "failed");
    assert.equal(stale.error.code, "resource.conflict");

    for (const candidate of ["../secret.py", ".env", ".git/config", "dist/bundle.js"]) {
        const result = await adapter.execute(call("project.read", { projectId: "offeragent", path: candidate }));
        assert.equal(result.status, "failed", candidate);
    }
});

test("Project Evidence rejects an allowed-name hard link to an external secret", async (t) => {
    const { ProjectEvidenceAdapter } = loadModule();
    const root = await mkdtemp(path.join(os.tmpdir(), "offeragent-project-hardlink-"));
    const external = await mkdtemp(path.join(os.tmpdir(), "offeragent-project-secret-"));
    t.after(() => rm(root, { recursive: true, force: true }));
    t.after(() => rm(external, { recursive: true, force: true }));
    await mkdir(path.join(root, "src"), { recursive: true });
    const secret = path.join(external, "credentials.txt");
    await writeFile(secret, "API_TOKEN=must-not-leak\n");
    await link(secret, path.join(root, "src", "notes.txt"));
    const adapter = new ProjectEvidenceAdapter(registeredVault(root));

    const listed = await adapter.execute(call("project.list", { projectId: "offeragent", limit: 10 }));
    const searched = await adapter.execute(call("project.search", {
        projectId: "offeragent", query: "must-not-leak", limit: 10,
    }));
    const read = await adapter.execute(call("project.read", {
        projectId: "offeragent", path: "src/notes.txt", lineStart: 1, lineEnd: 1,
    }));

    assert.deepEqual(listed.data.entries, []);
    assert.deepEqual(searched.data.entries, []);
    assert.equal(read.status, "failed");
    assert.equal(read.error.code, "resource.conflict");
    assert.equal(JSON.stringify([listed, searched, read]).includes("must-not-leak"), false);
});
