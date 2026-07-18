#!/usr/bin/env node
"use strict";

const assert = require("node:assert/strict");
const { execFileSync } = require("node:child_process");
const { createHash, randomUUID } = require("node:crypto");
const {
    access,
    mkdir,
    mkdtemp,
    readFile,
    realpath,
    rm,
    writeFile,
} = require("node:fs/promises");
const os = require("node:os");
const path = require("node:path");
const { buildSync } = require("esbuild");

const REQUIRED_CAPABILITIES = [
    "eventReplay", "multiSession", "approvals", "skills", "shell", "hooks", "subagents",
    "artifacts", "loopbackWeb", "contentBlocks", "cancellation", "diagnostics",
];
const CLIENT_CAPABILITIES = Object.fromEntries(REQUIRED_CAPABILITIES.map((name) => [name, true]));

function loadRuntimeModules() {
    const source = [
        'export { StdioWorkerTransport } from "../src/runtime/stdio_worker";',
        'export { VaultChangeCoordinator, FileVaultChangeJournal, GitCheckpointStore } from "../src/runtime/vault_changes";',
    ].join("\n");
    const output = buildSync({
        stdin: { contents: source, loader: "ts", resolveDir: __dirname },
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

function sha256(content) {
    return `sha256:${createHash("sha256").update(content, "utf8").digest("hex")}`;
}

function safeVaultTarget(root, relative) {
    assert.equal(path.posix.normalize(relative), relative);
    assert.ok(!relative.startsWith("../") && !path.isAbsolute(relative));
    const target = path.resolve(root, ...relative.split("/"));
    assert.ok(target.startsWith(`${path.resolve(root)}${path.sep}`));
    return target;
}

function fileVault(root) {
    const versions = new Map();
    let writeSequence = 0;

    const read = async (relative) => {
        try {
            return await readFile(safeVaultTarget(root, relative), "utf8");
        } catch (error) {
            if (error && error.code === "ENOENT") return undefined;
            throw error;
        }
    };
    const snapshot = async (relative) => {
        const content = await read(relative);
        if (content === undefined) return { content: undefined, modifiedVersion: "missing" };
        let modifiedVersion = versions.get(relative);
        if (modifiedVersion === undefined) {
            modifiedVersion = `mtime:0:size:${Buffer.byteLength(content)}`;
            versions.set(relative, modifiedVersion);
        }
        return { content, modifiedVersion };
    };

    return {
        read,
        snapshot,
        async applyConditional(mutation) {
            const before = await snapshot(mutation.path);
            const observed = {
                contentHash: before.content === undefined ? "absent" : sha256(before.content),
                modifiedVersion: before.modifiedVersion,
            };
            if (observed.contentHash !== mutation.expected.contentHash ||
                observed.modifiedVersion !== mutation.expected.modifiedVersion) {
                return { status: "conflict", observed };
            }
            // Match the production Obsidian adapter: the public Vault API has no
            // compare-delete primitive, so reverse deletion remains manual review.
            if (mutation.kind === "delete") return { status: "unsupported", operation: "delete" };
            const target = safeVaultTarget(root, mutation.path);
            await mkdir(path.dirname(target), { recursive: true });
            await writeFile(target, mutation.afterContent, "utf8");
            writeSequence += 1;
            const applied = {
                contentHash: sha256(mutation.afterContent),
                modifiedVersion: `mtime:${writeSequence}:size:${Buffer.byteLength(mutation.afterContent)}`,
            };
            versions.set(mutation.path, applied.modifiedVersion);
            return { status: "applied", applied };
        },
    };
}

function processIds(imageName) {
    const script = [
        `$items = @(Get-CimInstance Win32_Process -Filter "Name='${imageName}'" -ErrorAction SilentlyContinue)`,
        "@($items | ForEach-Object { [int]$_.ProcessId }) | ConvertTo-Json -Compress",
    ].join("; ");
    const raw = execFileSync("powershell.exe", ["-NoProfile", "-NonInteractive", "-Command", script], {
        encoding: "utf8",
        windowsHide: true,
    }).trim();
    if (!raw) return [];
    const value = JSON.parse(raw);
    return Array.isArray(value) ? value : [value];
}

async function assertProcessExited(pid) {
    for (let attempt = 0; attempt < 20; attempt += 1) {
        try {
            process.kill(pid, 0);
        } catch (error) {
            if (error && error.code === "ESRCH") return;
            throw error;
        }
        await new Promise((resolvePromise) => setTimeout(resolvePromise, 50));
    }
    assert.fail(`Worker PID ${pid} survived authoritative transport close`);
}

function initializeParams(workspaceId, manifest, pluginVersion) {
    return {
        protocolVersion: manifest.protocol.minimum,
        clientVersion: pluginVersion,
        workspaceId,
        capabilities: CLIENT_CAPABILITIES,
        supportedProtocolRange: {
            minimum: manifest.protocol.minimum,
            maximum: manifest.protocol.maximum,
        },
        requiredCapabilities: REQUIRED_CAPABILITIES,
        schemaHash: manifest.protocol.schemaHash,
    };
}

function vaultChangeCall(workspaceId, sourceBinding) {
    const arguments_ = {
        batchId: "batch_fused_smoke",
        task: "Create a verified smoke-test summary",
        changeKind: "general",
        sourceBindings: [sourceBinding],
        interviewSubmission: null,
        operations: [{
            op: "create",
            path: "notes/result.md",
            content: "verified\n",
            expectedContentHash: "absent",
            expectedModifiedVersion: "missing",
        }],
    };
    return {
        toolCallId: "call_fused_smoke",
        workspaceId,
        runId: "run_fused_smoke",
        name: "vault.changes.apply",
        version: "1",
        arguments: arguments_,
        argsHash: sha256(JSON.stringify(arguments_)),
        idempotencyKey: "idem_fused_smoke",
        risk: "write",
        reason: null,
        agentLineage: ["run_fused_smoke"],
        executorLocation: "plugin",
        definitionFingerprint: `sha256:${"b".repeat(64)}`,
        resultSensitivity: "workspace",
        deadline: null,
    };
}

async function main() {
    const artifactRoot = path.resolve(process.argv[2] || "");
    if (!process.argv[2]) throw new Error("usage: node scripts/smoke-fused-product.cjs <artifact-directory>");
    const build = JSON.parse(await readFile(path.join(artifactRoot, "local-development-build.json"), "utf8"));
    const runtimeRoot = path.join(artifactRoot, "runtime", "windows-x64", "local-development");
    const manifest = JSON.parse(await readFile(path.join(runtimeRoot, "development-runtime-manifest.json"), "utf8"));
    const worker = path.join(runtimeRoot, "offeragent-worker.exe");
    await access(worker);

    const root = await mkdtemp(path.join(os.tmpdir(), "offeragent-fused-smoke-"));
    const vaultRoot = path.join(root, "Vault");
    const localAppData = path.join(root, "LocalAppData");
    const workspaceId = `ws_${randomUUID()}`;
    const processBaseline = {
        workers: new Set(processIds("offeragent-worker.exe")),
        hosts: new Set(processIds("offeragent-process-host.exe")),
    };
    let firstPeer;
    let secondPeer;
    let firstWorkerPid;
    let secondWorkerPid;
    try {
        await mkdir(path.join(vaultRoot, ".offeragent"), { recursive: true });
        await mkdir(path.join(vaultRoot, "notes"), { recursive: true });
        await mkdir(localAppData, { recursive: true });
        await writeFile(
            path.join(vaultRoot, ".offeragent", "workspace.json"),
            `${JSON.stringify({ portableWorkspaceId: workspaceId, schemaVersion: 1 })}\n`,
            "utf8",
        );
        await writeFile(path.join(vaultRoot, "agent.md"), "# OfferAgent smoke contract\n", "utf8");
        await writeFile(path.join(vaultRoot, "notes", "source.md"), "Precise current evidence\n", "utf8");
        execFileSync("git", ["init", "--quiet", vaultRoot], { windowsHide: true });
        process.env.LOCALAPPDATA = localAppData;

        const {
            StdioWorkerTransport,
            VaultChangeCoordinator,
            FileVaultChangeJournal,
            GitCheckpointStore,
        } = loadRuntimeModules();
        const transport = new StdioWorkerTransport(worker, vaultRoot, manifest.runtimeVersion);
        firstPeer = await transport.connect();
        const initialized = await firstPeer.request(
            "initialize",
            initializeParams(workspaceId, manifest, build.pluginVersion),
        );
        firstWorkerPid = initialized.workerPid;
        assert.equal(initialized.workspaceId, workspaceId);
        assert.equal(initialized.runtimeVersion, manifest.runtimeVersion);
        assert.equal(initialized.schemaHash, manifest.protocol.schemaHash);
        assert.equal(initialized.transport, "stdio");
        assert.equal(initialized.runtimeArch, "win-x64");

        const beforeConfig = await firstPeer.request("config/get", { scope: "workspace" });
        assert.equal(Object.hasOwn(beforeConfig.values.model, "provider"), false);
        assert.equal(Object.hasOwn(beforeConfig.values.model, "wire_api"), false);
        assert.equal(Object.hasOwn(beforeConfig.values.model, "base_url"), false);
        const configured = await firstPeer.request("config/update", {
            scope: "workspace",
            expectedRevision: beforeConfig.revision,
            patch: {
                policy: { read_only: false, workspace_trusted: true, approve_vault_writes: true },
            },
        });
        assert.equal(configured.status, "applied");
        const afterConfig = await firstPeer.request("config/get", { scope: "workspace" });
        assert.equal(afterConfig.revision, beforeConfig.revision + 1);
        assert.equal(afterConfig.values.policy.read_only, false);
        assert.equal(afterConfig.values.policy.workspace_trusted, true);
        assert.equal(afterConfig.values.policy.approve_vault_writes, true);
        assert.equal(afterConfig.restartPending, false);

        const created = await firstPeer.request("session/create", {
            title: "Fused artifact smoke",
            clientRequestId: "req_fused_smoke_session",
        });
        const sessionId = created.session.sessionId;

        const vault = fileVault(await realpath(vaultRoot));
        const source = await vault.snapshot("notes/source.md");
        assert.notEqual(source.content, undefined);
        const sourceBinding = {
            path: "notes/source.md",
            expectedModifiedVersion: source.modifiedVersion,
            expectedContentHash: sha256(source.content),
        };
        const journal = new FileVaultChangeJournal(
            path.join(localAppData, "OfferAgent", "smoke-vault-change-journal"),
        );
        const checkpoints = new GitCheckpointStore(await realpath(vaultRoot));
        let pluginConfirmations = 0;
        const coordinator = new VaultChangeCoordinator({
            vault,
            journal,
            checkpoints,
            permissionMode: () => "ask_every_time",
            authorize: async (proposal) => {
                pluginConfirmations += 1;
                assert.equal(proposal.batchId, "batch_fused_smoke");
                assert.equal(proposal.changeKind, "general");
                assert.deepEqual(proposal.paths, ["notes/result.md"]);
                assert.deepEqual(proposal.sourceBindings, [sourceBinding]);
                assert.equal(proposal.reviewTargets.length, 1);
                assert.equal(proposal.reviewTargets[0].beforeContent, null);
                assert.equal(proposal.reviewTargets[0].afterContent, "verified\n");
                assert.match(proposal.reviewHash, /^sha256:[0-9a-f]{64}$/u);
                return { decision: "accept", reviewHash: proposal.reviewHash };
            },
        });
        await coordinator.beginRecovery();
        const applied = await coordinator.execute(vaultChangeCall(workspaceId, sourceBinding));
        assert.equal(applied.status, "succeeded");
        assert.equal(pluginConfirmations, 1);
        assert.equal(await readFile(path.join(vaultRoot, "notes", "result.md"), "utf8"), "verified\n");

        const guardedUndo = await coordinator.undo("batch_fused_smoke");
        assert.equal(guardedUndo.status, "conflict");
        assert.deepEqual(guardedUndo.paths, ["notes/result.md"]);
        assert.equal(await readFile(path.join(vaultRoot, "notes", "result.md"), "utf8"), "verified\n");

        await firstPeer.request("shutdown", { reason: "upgrade", gracePeriodMs: 30_000 });
        await firstPeer.close();
        firstPeer = undefined;
        await assertProcessExited(firstWorkerPid);

        secondPeer = await transport.connect();
        const restarted = await secondPeer.request(
            "initialize",
            initializeParams(workspaceId, manifest, build.pluginVersion),
        );
        secondWorkerPid = restarted.workerPid;
        assert.notEqual(secondWorkerPid, firstWorkerPid);
        const persistedConfig = await secondPeer.request("config/get", { scope: "workspace" });
        assert.equal(persistedConfig.revision, afterConfig.revision);
        assert.equal(persistedConfig.values.policy.workspace_trusted, true);
        const persistedSession = await secondPeer.request("session/get", { sessionId });
        assert.equal(persistedSession.session.summary.sessionId, sessionId);
        assert.equal(persistedSession.session.summary.title, "Fused artifact smoke");
        await secondPeer.request("shutdown", { reason: "user", gracePeriodMs: 30_000 });
        await secondPeer.close();
        secondPeer = undefined;
        await assertProcessExited(secondWorkerPid);

        const finalWorkers = processIds("offeragent-worker.exe").filter((pid) => !processBaseline.workers.has(pid));
        const finalHosts = processIds("offeragent-process-host.exe").filter((pid) => !processBaseline.hosts.has(pid));
        assert.deepEqual(finalWorkers, [], "smoke test leaked an OfferAgent Worker process");
        assert.deepEqual(finalHosts, [], "smoke test leaked an OfferAgent Process Host process");
        console.log(JSON.stringify({
            artifactRoot,
            runtimeVersion: manifest.runtimeVersion,
            workspaceId,
            codexOnlyConfig: true,
            sessionPersistedAfterRestart: true,
            pluginConfirmations,
            guardedUndo: guardedUndo.status,
            manualReviewPathPreserved: true,
            leakedProcesses: 0,
        }, null, 2));
    } finally {
        if (firstPeer) await firstPeer.close().catch(() => undefined);
        if (secondPeer) await secondPeer.close().catch(() => undefined);
        if (firstWorkerPid) await assertProcessExited(firstWorkerPid).catch(() => undefined);
        if (secondWorkerPid) await assertProcessExited(secondWorkerPid).catch(() => undefined);
        await rm(root, { recursive: true, force: true });
    }
}

main().catch((error) => {
    console.error(error instanceof Error ? error.stack : error);
    if (error && error.envelope) console.error(JSON.stringify(error.envelope, null, 2));
    process.exitCode = 1;
});
