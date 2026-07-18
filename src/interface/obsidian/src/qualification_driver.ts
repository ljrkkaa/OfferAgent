import * as Module from "node:module";
import * as path from "node:path";
import { randomBytes } from "node:crypto";
import { readFileSync } from "node:fs";

import { StdioWorkerTransport } from "./runtime/stdio_worker";
import { VaultToolAdapter } from "./runtime/vault_tool_adapter";
import {
    FileVaultChangeJournal,
    GitCheckpointStore,
    VaultChangeCoordinator,
} from "./runtime/vault_changes";

const PRODUCTION_EXPORTS = Object.freeze([
    "StdioWorkerTransport",
    "VaultToolAdapter",
    "VaultChangeCoordinator",
    "FileVaultChangeJournal",
    "GitCheckpointStore",
]);

function installSourceRootGuard(): void {
    const raw = process.env.OFFERAGENT_QUALIFICATION_FORBID_SOURCE_ROOT;
    if (raw === undefined || raw.length === 0) return;
    const forbidden = path.resolve(raw);
    const withinForbidden = (candidate: string): boolean => {
        const resolved = path.resolve(candidate);
        return resolved === forbidden || resolved.startsWith(`${forbidden}${path.sep}`);
    };
    const sealedDriver = path.resolve(__filename);
    for (const loaded of Object.keys(require.cache)) {
        if (path.resolve(loaded) !== sealedDriver && withinForbidden(loaded)) {
            throw new Error("qualification driver loaded repository source");
        }
    }
    const loader = Module as unknown as {
        _resolveFilename: (
            request: string,
            parent: NodeModule | null,
            isMain: boolean,
            options?: unknown,
        ) => string;
    };
    const resolveFilename = loader._resolveFilename;
    loader._resolveFilename = function guardedResolve(
        request: string,
        parent: NodeModule | null,
        isMain: boolean,
        options?: unknown,
    ): string {
        const resolved = resolveFilename.call(loader, request, parent, isMain, options);
        if (path.isAbsolute(resolved) && withinForbidden(resolved)) {
            throw new Error("qualification driver attempted a repository source import");
        }
        return resolved;
    };
}

function probe(): void {
    installSourceRootGuard();
    const constructors = [
        StdioWorkerTransport,
        VaultToolAdapter,
        VaultChangeCoordinator,
        FileVaultChangeJournal,
        GitCheckpointStore,
    ];
    if (constructors.some((value) => typeof value !== "function")) {
        throw new Error("qualification driver production adapter closure is incomplete");
    }
    process.stdout.write(`${JSON.stringify({
        driverProtocolVersion: 1,
        productionExports: PRODUCTION_EXPORTS,
        sourceFreeRuntime: true,
    })}\n`);
}

type SmokeInput = {
    readonly localAppData: string;
    readonly pluginVersion: string;
    readonly protocolVersion: string;
    readonly runtimeVersion: string;
    readonly schemaHash: string;
    readonly vaultRoot: string;
    readonly workerExecutable: string;
    readonly workspaceId: string;
};

function smokeInput(value: unknown): SmokeInput {
    if (value === null || typeof value !== "object" || Array.isArray(value)) {
        throw new TypeError("qualification smoke input must be an object");
    }
    const candidate = value as Record<string, unknown>;
    const expected = [
        "localAppData", "pluginVersion", "protocolVersion", "runtimeVersion",
        "schemaHash", "vaultRoot", "workerExecutable", "workspaceId",
    ];
    if (Object.keys(candidate).sort().join("\n") !== [...expected].sort().join("\n") ||
        expected.some((key) => typeof candidate[key] !== "string" || candidate[key] === "")) {
        throw new TypeError("qualification smoke input shape is invalid");
    }
    for (const key of ["localAppData", "vaultRoot", "workerExecutable"]) {
        if (!path.isAbsolute(candidate[key] as string)) throw new TypeError(`${key} must be absolute`);
    }
    if (!/^sha256:[0-9a-f]{64}$/u.test(candidate.schemaHash as string)) {
        throw new TypeError("qualification schema hash is invalid");
    }
    return candidate as unknown as SmokeInput;
}

async function smoke(): Promise<void> {
    installSourceRootGuard();
    const input = smokeInput(JSON.parse(readFileSync(0, "utf8")));
    process.env.LOCALAPPDATA = input.localAppData;
    const journalDirectory = path.join(
        input.vaultRoot,
        ".obsidian",
        "offeragent",
        "vault-change-journal",
    );
    const recoveryToken = randomBytes(32).toString("hex");
    const journal = new FileVaultChangeJournal(journalDirectory);
    await journal.markRecoveryReady(recoveryToken);
    const transport = new StdioWorkerTransport(
        input.workerExecutable,
        input.vaultRoot,
        input.runtimeVersion,
        journalDirectory,
        recoveryToken,
    );
    const peer = await transport.connect();
    let workerPid: number | undefined;
    try {
        const result = await peer.request("initialize", {
            protocolVersion: input.protocolVersion,
            clientVersion: input.pluginVersion,
            workspaceId: input.workspaceId,
            capabilities: {
                eventReplay: true,
                multiSession: true,
                approvals: true,
                skills: true,
                shell: true,
                hooks: true,
                subagents: true,
                artifacts: true,
                contentBlocks: true,
                cancellation: true,
                diagnostics: true,
            },
            supportedProtocolRange: {
                minimum: input.protocolVersion,
                maximum: input.protocolVersion,
            },
            requiredCapabilities: [
                "eventReplay", "multiSession", "approvals", "skills", "shell", "hooks",
                "subagents", "artifacts", "contentBlocks", "cancellation", "diagnostics",
            ],
            schemaHash: input.schemaHash,
        });
        if (result === null || typeof result !== "object" || Array.isArray(result)) {
            throw new Error("Worker initialize response is invalid");
        }
        const initialized = result as Record<string, unknown>;
        if (!Number.isSafeInteger(initialized.workerPid) || initialized.transport !== "stdio") {
            throw new Error("Worker initialize identity is invalid");
        }
        workerPid = initialized.workerPid as number;
        await peer.request("shutdown", { reason: "user", gracePeriodMs: 30_000 });
    } finally {
        await peer.close();
    }
    process.stdout.write(`${JSON.stringify({
        driverProtocolVersion: 1,
        sourceFreeRuntime: true,
        transport: "stdio",
        workerPid,
    })}\n`);
}

const command = process.argv[2];
if (command === "probe") {
    probe();
} else if (command === "smoke") {
    void smoke().catch((error: unknown) => {
        process.stderr.write(`${error instanceof Error ? error.stack ?? error.message : String(error)}\n`);
        process.exitCode = 1;
    });
} else {
    throw new Error("qualification driver command is unsupported");
}
