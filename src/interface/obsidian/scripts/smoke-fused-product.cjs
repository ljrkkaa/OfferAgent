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
const http = require("node:http");
const os = require("node:os");
const path = require("node:path");
const { buildSync } = require("esbuild");

const TERMINAL_EVENTS = new Set(["turn.completed", "turn.cancelled", "turn.failed", "turn.interrupted"]);
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

function planningStep(calls, finalResponse = null, requiresWriteOutcome = false) {
    return JSON.stringify({ requiresWriteOutcome, calls, finalResponse });
}

function call(name, arguments_, reason) {
    return { name, version: "1", arguments: arguments_, reason };
}

function sseResponse(text, id) {
    const events = [
        { type: "response.created", sequence_number: 0, response: { id } },
        { type: "response.output_text.delta", sequence_number: 1, output_index: 0, content_index: 0, delta: text },
        { type: "response.output_text.done", sequence_number: 2, output_index: 0, content_index: 0, text },
        {
            type: "response.completed",
            sequence_number: 3,
            response: {
                status: "completed",
                output: [{ type: "message", content: [{ type: "output_text", text }] }],
                usage: {
                    input_tokens: 9,
                    output_tokens: 4,
                    input_tokens_details: { cached_tokens: 0 },
                    output_tokens_details: { reasoning_tokens: 0 },
                },
            },
        },
    ];
    return events.map((event) => `event: ${event.type}\ndata: ${JSON.stringify(event)}\n\n`).join("");
}

async function startModelServer(outputs) {
    const requests = [];
    let index = 0;
    const server = http.createServer((request, response) => {
        const chunks = [];
        request.on("data", (chunk) => chunks.push(chunk));
        request.on("end", () => {
            try {
                assert.equal(request.method, "POST");
                assert.equal(request.url, "/v1/responses");
                const body = JSON.parse(Buffer.concat(chunks).toString("utf8"));
                assert.equal(body.model, "smoke-model");
                assert.equal(body.store, false);
                assert.ok(index < outputs.length, `unexpected model request ${index + 1}`);
                requests.push(body);
                const payload = sseResponse(outputs[index], `resp_smoke_${index + 1}`);
                index += 1;
                response.writeHead(200, { "content-type": "text/event-stream" });
                response.end(payload);
            } catch (error) {
                response.writeHead(500, { "content-type": "text/plain" });
                response.end(error instanceof Error ? error.stack : String(error));
            }
        });
    });
    await new Promise((resolvePromise, rejectPromise) => {
        server.once("error", rejectPromise);
        server.listen(0, "127.0.0.1", resolvePromise);
    });
    const address = server.address();
    assert.ok(address && typeof address === "object");
    return {
        baseUrl: `http://127.0.0.1:${address.port}/v1`,
        requests,
        assertExhausted() { assert.equal(index, outputs.length, "model response script was not exhausted"); },
        close: () => new Promise((resolvePromise, rejectPromise) => server.close((error) => error ? rejectPromise(error) : resolvePromise())),
    };
}

function safeVaultTarget(root, relative) {
    assert.equal(path.posix.normalize(relative), relative);
    assert.ok(!relative.startsWith("../") && !path.isAbsolute(relative));
    const target = path.resolve(root, ...relative.split("/"));
    assert.ok(target.startsWith(`${path.resolve(root)}${path.sep}`));
    return target;
}

function fileVault(root) {
    return {
        async read(relative) {
            try { return await readFile(safeVaultTarget(root, relative), "utf8"); }
            catch (error) { if (error && error.code === "ENOENT") return undefined; throw error; }
        },
        async write(relative, content) {
            const target = safeVaultTarget(root, relative);
            await mkdir(path.dirname(target), { recursive: true });
            await writeFile(target, content, "utf8");
        },
        async remove(relative) { await rm(safeVaultTarget(root, relative), { force: true }); },
    };
}

function processIds(imageName) {
    const script = [
        `$items = @(Get-CimInstance Win32_Process -Filter \"Name='${imageName}'\" -ErrorAction SilentlyContinue)`,
        "@($items | ForEach-Object { [int]$_.ProcessId }) | ConvertTo-Json -Compress",
    ].join("; ");
    const raw = execFileSync("powershell.exe", ["-NoProfile", "-NonInteractive", "-Command", script], {
        encoding: "utf8", windowsHide: true,
    }).trim();
    if (!raw) return [];
    const value = JSON.parse(raw);
    return Array.isArray(value) ? value : [value];
}

async function assertProcessExited(pid) {
    for (let attempt = 0; attempt < 20; attempt += 1) {
        try { process.kill(pid, 0); }
        catch (error) { if (error && error.code === "ESRCH") return; throw error; }
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

function completionParams(callDescriptor, result) {
    return {
        workspaceId: callDescriptor.workspaceId,
        runId: callDescriptor.runId,
        definitionFingerprint: callDescriptor.definitionFingerprint,
        argsHash: callDescriptor.argsHash,
        idempotencyKey: callDescriptor.idempotencyKey,
        result,
    };
}

function succeeded(callDescriptor, summary, data, sourceRefs = []) {
    return {
        toolCallId: callDescriptor.toolCallId,
        status: "succeeded",
        summary,
        data,
        sourceRefs,
        retryable: false,
    };
}

function waitForTerminal(events, runId, failures) {
    const existing = events.find((event) => event.runId === runId && TERMINAL_EVENTS.has(event.type));
    if (existing) return Promise.resolve(existing);
    return new Promise((resolvePromise, rejectPromise) => {
        const timer = setTimeout(() => rejectPromise(new Error(
            `Run ${runId} did not reach a terminal event; observed: ${events.filter((event) => event.runId === runId).map((event) => event.type).join(", ")}`,
        )), 60_000);
        failures.push({ runId, resolvePromise, rejectPromise, timer });
    });
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
    let modelServer;
    let firstPeer;
    let secondPeer;
    let firstWorkerPid;
    let secondWorkerPid;
    try {
        await mkdir(path.join(vaultRoot, ".offeragent"), { recursive: true });
        await mkdir(localAppData, { recursive: true });
        await writeFile(
            path.join(vaultRoot, ".offeragent", "workspace.json"),
            `${JSON.stringify({ portableWorkspaceId: workspaceId, schemaVersion: 1 })}\n`,
            "utf8",
        );
        await writeFile(path.join(vaultRoot, "agent.md"), "# OfferAgent smoke contract\n\nRead evidence before answering.\n", "utf8");
        await mkdir(path.join(vaultRoot, "notes"), { recursive: true });
        await writeFile(path.join(vaultRoot, "notes", "source.md"), "Precise current evidence\n", "utf8");
        execFileSync("git", ["init", "--quiet", vaultRoot], { windowsHide: true });

        const contractContent = await readFile(path.join(vaultRoot, "agent.md"), "utf8");
        const evidenceContent = await readFile(path.join(vaultRoot, "notes", "source.md"), "utf8");
        const contractHash = sha256(contractContent);
        const evidenceHash = sha256(evidenceContent);
        const evidenceVersion = `mtime:17:size:${Buffer.byteLength(evidenceContent)}`;
        const outputs = [
            planningStep([call("agent_contract.read", {}, "Load the current Vault contract before acting.")]),
            planningStep([call("vault.search", { query: "Precise current evidence", limit: 5 }, "Locate current Vault evidence.")]),
            planningStep([call("vault.read", {
                path: "notes/source.md", lineStart: 1, lineEnd: 1,
                expectedContentHash: evidenceHash, expectedModifiedVersion: evidenceVersion,
            }, "Read the exact version-bound source line.")]),
            planningStep([], "依据 notes/source.md 第 1 行的当前证据：Precise current evidence。"),
            planningStep([call("agent_contract.read", {}, "Reload the current Vault contract before writing.")], null, true),
            planningStep([call("vault.changes.apply", {
                batchId: "batch_fused_smoke",
                task: "Create a verified smoke-test summary",
                operations: [{
                    op: "create", path: "notes/result.md", content: "verified\n", expectedContentHash: "absent",
                }],
            }, "Apply one confirmed, hash-bound Vault Change Batch.")], null, true),
            planningStep([], "已通过确认的 Vault Change Batch 创建 notes/result.md。", true),
        ];
        modelServer = await startModelServer(outputs);
        process.env.LOCALAPPDATA = localAppData;

        const { StdioWorkerTransport, VaultChangeCoordinator, FileVaultChangeJournal, GitCheckpointStore } = loadRuntimeModules();
        const vault = fileVault(await realpath(vaultRoot));
        const journal = new FileVaultChangeJournal(path.join(localAppData, "OfferAgent", "smoke-vault-change-journal"));
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
                assert.deepEqual(proposal.paths, ["notes/result.md"]);
                assert.match(proposal.diff, /verified/);
                return true;
            },
        });
        await coordinator.beginRecovery();

        const transport = new StdioWorkerTransport(worker, vaultRoot, manifest.runtimeVersion);
        const events = [];
        const waiters = [];
        const tasks = new Set();
        let asyncFailure;
        const track = (operation) => {
            const task = Promise.resolve(operation).catch((error) => { asyncFailure = error; });
            tasks.add(task);
            void task.finally(() => tasks.delete(task));
        };
        const handleEvent = (event) => {
            events.push(event);
            for (const waiter of [...waiters]) {
                if (event.runId === waiter.runId && TERMINAL_EVENTS.has(event.type)) {
                    clearTimeout(waiter.timer);
                    waiters.splice(waiters.indexOf(waiter), 1);
                    waiter.resolvePromise(event);
                }
            }
            if (event.type === "approval.required") {
                const approval = event.payload.approval;
                track(firstPeer.request("approval/resolve", {
                    approvalId: approval.approvalId,
                    decision: "allow_once",
                    scope: "once",
                    expectedArgsHash: approval.toolCall.argsHash,
                    includeDescendants: false,
                    comment: "fused product smoke test",
                }));
            }
            if (event.type !== "tool.started") return;
            track((async () => {
                const descriptor = event.payload.call;
                let result;
                if (descriptor.name === "agent_contract.read") {
                    result = succeeded(descriptor, "Read the current Agent Contract.", {
                        path: "agent.md", content: contractContent, contentHash: contractHash,
                    });
                } else if (descriptor.name === "vault.search") {
                    result = succeeded(descriptor, "Located a candidate; precise read required.", {
                        entries: [{
                            path: "notes/source.md",
                            modifiedVersion: evidenceVersion,
                            contentHash: evidenceHash,
                            matchTier: "body",
                            snippets: [{ content: "Precise current evidence", lineStart: 1, lineEnd: 1 }],
                        }],
                        truncated: false,
                    });
                } else if (descriptor.name === "vault.read") {
                    result = succeeded(descriptor, "Read precise current evidence.", {
                        path: "notes/source.md",
                        lineStart: 1,
                        lineEnd: 1,
                        modifiedVersion: evidenceVersion,
                        contentHash: evidenceHash,
                        content: "Precise current evidence",
                        truncated: false,
                    }, [{
                        type: "vault",
                        file: {
                            workspaceId: descriptor.workspaceId,
                            path: "notes/source.md",
                            contentHash: evidenceHash,
                            lineStart: 1,
                            lineEnd: 1,
                        },
                        freshness: "fresh",
                    }]);
                } else if (descriptor.name === "vault.changes.apply") {
                    result = await coordinator.execute(descriptor);
                    assert.equal(result.status, "succeeded");
                } else {
                    throw new Error(`unexpected plugin tool: ${descriptor.name}`);
                }
                const acknowledged = await firstPeer.request("plugin-tools/complete", completionParams(descriptor, result));
                assert.equal(acknowledged.accepted, true);
            })());
        };

        firstPeer = await transport.connect();
        firstPeer.onNotification("event", handleEvent);

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
        await new Promise((resolvePromise) => setTimeout(resolvePromise, 250));

        const beforeConfig = await firstPeer.request("config/get", { scope: "workspace" });
        const configured = await firstPeer.request("config/update", {
            scope: "workspace",
            expectedRevision: beforeConfig.revision,
            patch: {
                model: {
                    provider: "local",
                    wire_api: "responses",
                    model: "smoke-model",
                    base_url: modelServer.baseUrl,
                    allow_remote_https: false,
                },
                policy: { read_only: false, workspace_trusted: true, approve_vault_writes: true },
            },
        });
        assert.ok(["applied", "restart_required"].includes(configured.status));
        if (configured.status === "restart_required") {
            await firstPeer.request("shutdown", { reason: "upgrade", gracePeriodMs: 30_000 });
            await firstPeer.close();
            await assertProcessExited(firstWorkerPid);
            firstPeer = await transport.connect();
            firstPeer.onNotification("event", handleEvent);
            const restarted = await firstPeer.request(
                "initialize",
                initializeParams(workspaceId, manifest, build.pluginVersion),
            );
            firstWorkerPid = restarted.workerPid;
            assert.equal(restarted.workspaceId, workspaceId);
            assert.equal(restarted.runtimeVersion, manifest.runtimeVersion);
            await new Promise((resolvePromise) => setTimeout(resolvePromise, 250));
        }
        const afterConfig = await firstPeer.request("config/get", { scope: "workspace" });
        assert.equal(afterConfig.values.model.provider, "local");
        assert.equal(afterConfig.values.model.model, "smoke-model");
        assert.equal(afterConfig.values.model.base_url, modelServer.baseUrl);
        assert.equal(afterConfig.restartPending, false);
        const models = await firstPeer.request("models/list", { provider: "local", includeUnavailable: true });
        assert.equal(models.models.length, 1);
        assert.equal(models.models[0].model, "smoke-model");
        assert.equal(models.models[0].local, true);

        const created = await firstPeer.request("session/create", {
            title: "Fused product smoke",
            clientRequestId: "req_fused_smoke_session",
        });
        const runConfig = {
            provider: "local", model: "smoke-model", reasoningEffort: "medium",
            permissionMode: "normal", budgets: null,
        };
        const evidenceTurn = await firstPeer.request("turn/start", {
            sessionId: created.session.sessionId,
            turnId: "turn_fused_evidence",
            idempotencyKey: "fused-evidence-v1",
            input: [{ type: "text", text: "Read current Vault evidence and answer with the exact source line." }],
            pinnedContext: [],
            runConfig,
        });
        let evidenceTerminal;
        try {
            evidenceTerminal = await waitForTerminal(events, evidenceTurn.runId, waiters);
        } catch (error) {
            let status;
            let replay;
            try { status = await firstPeer.request("agent/status", { runId: evidenceTurn.runId }); }
            catch (statusError) { status = { error: statusError.message, envelope: statusError.envelope }; }
            try {
                replay = await firstPeer.request("events/replay", {
                    runId: evidenceTurn.runId, afterSequence: 0, runCursors: {}, limit: 1000, types: [],
                });
            } catch (replayError) { replay = { error: replayError.message, envelope: replayError.envelope }; }
            console.error(JSON.stringify({
                status,
                replay: replay.events ? {
                    eventTypes: replay.events.map((event) => event.type),
                    lastSequence: replay.lastSequence,
                    hasMore: replay.hasMore,
                } : replay,
                asyncFailure: asyncFailure && { message: asyncFailure.message, envelope: asyncFailure.envelope },
            }, null, 2));
            throw error;
        }
        assert.equal(evidenceTerminal.type, "turn.completed");
        await Promise.all([...tasks]);
        if (asyncFailure) throw asyncFailure;

        const changeTurn = await firstPeer.request("turn/start", {
            sessionId: created.session.sessionId,
            turnId: "turn_fused_change",
            idempotencyKey: "fused-change-v1",
            input: [{ type: "text", text: "Create notes/result.md with the verified summary." }],
            pinnedContext: [],
            runConfig,
        });
        const changeTerminal = await waitForTerminal(events, changeTurn.runId, waiters);
        await Promise.all([...tasks]);
        if (asyncFailure) throw asyncFailure;
        if (changeTerminal.type !== "turn.completed") {
            console.error(JSON.stringify({
                eventTypes: events.filter((event) => event.runId === changeTurn.runId).map((event) => event.type),
                terminal: changeTerminal,
            }, null, 2));
        }
        assert.equal(changeTerminal.type, "turn.completed");
        assert.equal(pluginConfirmations, 1);
        assert.equal(await readFile(path.join(vaultRoot, "notes", "result.md"), "utf8"), "verified\n");

        const evidenceEvents = events.filter((event) => event.runId === evidenceTurn.runId);
        assert.deepEqual(
            evidenceEvents.filter((event) => event.type === "tool.started").map((event) => event.payload.call.name),
            ["agent_contract.read", "vault.search", "vault.read"],
        );
        assert.ok(evidenceEvents.some((event) =>
            event.type === "tool.completed" && event.payload.sourceReferenceIds.length === 1));
        const evidenceAnswer = evidenceEvents.find((event) => event.type === "assistant.completed");
        assert.match(evidenceAnswer.payload.content[0].text, /notes\/source\.md/);
        const changeEvents = events.filter((event) => event.runId === changeTurn.runId);
        assert.ok(changeEvents.some((event) => event.type === "approval.required"));
        assert.ok(changeEvents.some((event) => event.type === "approval.resolved"));
        assert.ok(changeEvents.some((event) => event.type === "tool.completed" &&
            event.payload.sideEffectFacts?.some((effect) =>
                effect.state === "committed" && effect.resourceId === "notes/result.md")));

        const recoveredCoordinator = new VaultChangeCoordinator({
            vault,
            journal,
            checkpoints,
            permissionMode: () => "ask_every_time",
            authorize: async () => { throw new Error("recovery must not request a second authorization"); },
        });
        await recoveredCoordinator.beginRecovery();
        await writeFile(path.join(vaultRoot, "notes", "result.md"), "tampered\n", "utf8");
        const conflict = await recoveredCoordinator.undo("batch_fused_smoke");
        assert.equal(conflict.status, "conflict");
        assert.deepEqual(conflict.paths, ["notes/result.md"]);
        await writeFile(path.join(vaultRoot, "notes", "result.md"), "verified\n", "utf8");
        const undone = await recoveredCoordinator.undo("batch_fused_smoke");
        assert.equal(undone.status, "undone");
        await assert.rejects(access(path.join(vaultRoot, "notes", "result.md")), /ENOENT/);

        await firstPeer.request("shutdown", { reason: "upgrade", gracePeriodMs: 30_000 });
        await firstPeer.close();
        firstPeer = undefined;
        await assertProcessExited(firstWorkerPid);

        secondPeer = await transport.connect();
        const secondInitialized = await secondPeer.request(
            "initialize",
            initializeParams(workspaceId, manifest, build.pluginVersion),
        );
        secondWorkerPid = secondInitialized.workerPid;
        assert.notEqual(secondWorkerPid, firstWorkerPid);
        const evidenceReplay = await secondPeer.request("events/replay", {
            runId: evidenceTurn.runId, afterSequence: 0, runCursors: {}, limit: 1000, types: [],
        });
        const changeReplay = await secondPeer.request("events/replay", {
            runId: changeTurn.runId, afterSequence: 0, runCursors: {}, limit: 1000, types: [],
        });
        assert.equal(evidenceReplay.hasMore, false);
        assert.equal(changeReplay.hasMore, false);
        assert.equal(evidenceReplay.events.at(-1).type, "turn.completed");
        assert.equal(changeReplay.events.at(-1).type, "turn.completed");
        assert.deepEqual(
            evidenceReplay.events.map((event) => event.eventId),
            [...new Set(evidenceReplay.events.map((event) => event.eventId))],
        );
        assert.deepEqual(
            evidenceReplay.events.map((event) => event.sequence),
            evidenceReplay.events.map((_event, index) => index + 1),
        );
        assert.ok(evidenceReplay.events.some((event) =>
            event.type === "tool.completed" && event.payload.sourceReferenceIds.length === 1));
        assert.ok(changeReplay.events.some((event) => event.type === "approval.resolved"));
        await secondPeer.request("shutdown", { reason: "user", gracePeriodMs: 30_000 });
        await secondPeer.close();
        secondPeer = undefined;
        await assertProcessExited(secondWorkerPid);

        modelServer.assertExhausted();
        assert.equal(modelServer.requests.length, 7);
        const finalWorkers = processIds("offeragent-worker.exe").filter((pid) => !processBaseline.workers.has(pid));
        const finalHosts = processIds("offeragent-process-host.exe").filter((pid) => !processBaseline.hosts.has(pid));
        assert.deepEqual(finalWorkers, [], "smoke test leaked an OfferAgent Worker process");
        assert.deepEqual(finalHosts, [], "smoke test leaked an OfferAgent Process Host process");
        console.log(JSON.stringify({
            artifactRoot,
            runtimeVersion: manifest.runtimeVersion,
            workspaceId,
            modelRequests: modelServer.requests.length,
            evidenceEvents: evidenceReplay.events.length,
            changeEvents: changeReplay.events.length,
            pluginConfirmations,
            guardedUndo: [conflict.status, undone.status],
            replayAfterRestart: true,
            leakedProcesses: 0,
        }, null, 2));
    } finally {
        if (firstPeer) await firstPeer.close().catch(() => undefined);
        if (secondPeer) await secondPeer.close().catch(() => undefined);
        if (firstWorkerPid) await assertProcessExited(firstWorkerPid).catch(() => undefined);
        if (secondWorkerPid) await assertProcessExited(secondWorkerPid).catch(() => undefined);
        if (modelServer) await modelServer.close().catch(() => undefined);
        await rm(root, { recursive: true, force: true });
    }
}

main().catch((error) => {
    console.error(error instanceof Error ? error.stack : error);
    if (error && error.envelope) console.error(JSON.stringify(error.envelope, null, 2));
    process.exitCode = 1;
});
