import { Buffer } from "node:buffer";
import { createHash } from "node:crypto";
import { promises as fs } from "node:fs";
import { arch, platform, release } from "node:os";
import { isAbsolute, parse, relative, resolve, sep } from "node:path";

import { InstalledRuntime, InstallerPhase, RuntimeInstaller } from "./bootstrap";
import { parseStrictJson } from "./strict_json";

const DEVELOPMENT_MARKER = "OFFERAGENT_LOCAL_DEVELOPMENT_RUNTIME_V1";
const MANIFEST_NAME = "development-runtime-manifest.json";
const MAXIMUM_MANIFEST_BYTES = 8 * 1024 * 1024;
const SHA256 = /^sha256:[0-9a-f]{64}$/;
const VERSION = /^[0-9A-Za-z][0-9A-Za-z.+_-]{0,63}$/;
const PROTOCOL = /^(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)$/;
const COMMIT = /^[0-9a-f]{40}$/;
const CANONICAL_PATH = /^[^\\\0]+$/;
const REQUIRED_EXECUTABLES = new Set([
    "offeragent-process-host.exe",
    "offeragent-worker.exe",
    "tools/rg.exe",
]);

declare const __OFFERAGENT_DEVELOPMENT_MANIFEST_SHA256__: string;

export interface LocalDevelopmentRuntimeInstallerOptions {
    readonly pluginDirectory: string;
    readonly pluginVersion: string;
    readonly protocolVersion: string;
    readonly schemaHash: string;
}

interface DevelopmentFileRecord {
    readonly byteLength: number;
    readonly kind: string;
    readonly path: string;
    readonly sha256: string;
}

interface DevelopmentRuntimeManifest {
    readonly build: {
        readonly commit: string;
        readonly sourceTreeSha256: string;
    };
    readonly coreVersion: string;
    readonly developmentOnly: true;
    readonly files: ReadonlyArray<DevelopmentFileRecord>;
    readonly platform: {
        readonly architecture: "x64";
        readonly minimumWindowsBuild: number;
        readonly os: "windows";
    };
    readonly pluginVersion: string;
    readonly protocol: {
        readonly maximum: string;
        readonly minimum: string;
        readonly schemaHash: string;
    };
    readonly runtimeContentSha256: string;
    readonly runtimeVersion: string;
    readonly schemaVersion: 1;
    readonly stateSchemaVersion: number;
    readonly toolAbiVersion: string;
}

export class LocalDevelopmentRuntimeVerificationError extends Error {
    readonly code: string;

    constructor(code: string, message: string) {
        super(message);
        this.name = "LocalDevelopmentRuntimeVerificationError";
        this.code = code;
    }
}

export class LocalDevelopmentRuntimeInstaller implements RuntimeInstaller {
    private readonly pluginDirectory: string;
    private readonly pluginVersion: string;
    private readonly protocolVersion: string;
    private readonly schemaHash: string;

    constructor(options: LocalDevelopmentRuntimeInstallerOptions) {
        if (!isAbsolute(options.pluginDirectory) || options.pluginDirectory.includes("\0")) {
            throw new TypeError("pluginDirectory must be an absolute local path");
        }
        if (!VERSION.test(options.pluginVersion) || !PROTOCOL.test(options.protocolVersion) ||
            !SHA256.test(options.schemaHash)) {
            throw new TypeError("plugin/protocol/schema identity is invalid");
        }
        this.pluginDirectory = resolve(options.pluginDirectory);
        this.pluginVersion = options.pluginVersion;
        this.protocolVersion = options.protocolVersion;
        this.schemaHash = options.schemaHash;
    }

    async ensureReady(signal: AbortSignal, onPhase: (phase: InstallerPhase) => void): Promise<InstalledRuntime> {
        signal.throwIfAborted();
        onPhase("locating_embedded_bundle");
        if (platform() !== "win32" || arch() !== "x64") fail("development_platform_unsupported");
        const runtimeRoot = confinedPath(this.pluginDirectory, "runtime/windows-x64/local-development");
        await requireSafeDirectoryChain(this.pluginDirectory);
        await requireSafeDirectoryChain(runtimeRoot);
        const manifestPath = confinedPath(runtimeRoot, MANIFEST_NAME);
        onPhase("verifying_manifest");
        const manifestBytes = await readBounded(manifestPath, MAXIMUM_MANIFEST_BYTES);
        const manifestSha256 = sha256Bytes(manifestBytes);
        if (!SHA256.test(__OFFERAGENT_DEVELOPMENT_MANIFEST_SHA256__) ||
            manifestSha256 !== __OFFERAGENT_DEVELOPMENT_MANIFEST_SHA256__) {
            fail("development_manifest_anchor_mismatch");
        }
        const manifest = parseDevelopmentManifest(
            manifestBytes,
            this.pluginVersion,
            this.protocolVersion,
            this.schemaHash,
        );
        requireCurrentWindowsBuild(manifest.platform.minimumWindowsBuild);
        const verifyPinnedTree = async (launchSignal?: AbortSignal): Promise<void> => {
            launchSignal?.throwIfAborted();
            await requireSafeDirectoryChain(this.pluginDirectory);
            await requireSafeDirectoryChain(runtimeRoot);
            await verifyExactRuntimeTree(runtimeRoot, manifest, manifestSha256);
            launchSignal?.throwIfAborted();
        };
        signal.throwIfAborted();
        const workerExecutable = confinedPath(runtimeRoot, "offeragent-worker.exe");
        onPhase("ready");
        return {
            version: manifest.runtimeVersion,
            workerExecutable,
            protocolMinimum: manifest.protocol.minimum,
            protocolMaximum: manifest.protocol.maximum,
            schemaHash: manifest.protocol.schemaHash,
            beforeWorkerLaunch: async (launchSignal?: AbortSignal): Promise<void> => {
                // Verify the pinned bundle once at the sole executable launch
                // boundary. The plugin publishes no discoverable local endpoint.
                await verifyPinnedTree(launchSignal);
                if (resolve(await fs.realpath(workerExecutable)).toLocaleLowerCase("en-US") !==
                    resolve(workerExecutable).toLocaleLowerCase("en-US")) {
                    fail("development_worker_path_changed");
                }
            },
        };
    }
}

function parseDevelopmentManifest(
    payload: Buffer,
    pluginVersion: string,
    protocolVersion: string,
    schemaHash: string,
): DevelopmentRuntimeManifest {
    let raw: unknown;
    try {
        raw = parseStrictJson(payload.toString("utf8"), {
            maximumCharacters: MAXIMUM_MANIFEST_BYTES,
            maximumDepth: 16,
            maximumNodes: 100_000,
        });
    } catch {
        fail("development_manifest_malformed");
    }
    const value = objectValue(raw, "manifest");
    exactKeys(value, [
        "build", "coreVersion", "developmentOnly", "files", "platform", "pluginVersion", "protocol",
        "runtimeContentSha256", "runtimeVersion", "schemaVersion", "stateSchemaVersion", "toolAbiVersion",
    ], "manifest");
    if (value.developmentOnly !== true || value.schemaVersion !== 1) fail("development_marker_missing");
    const build = objectValue(value.build, "build");
    exactKeys(build, ["commit", "sourceTreeSha256"], "build");
    const commit = textValue(build.commit, "build.commit");
    const sourceTreeSha256 = hashValue(build.sourceTreeSha256, "build.sourceTreeSha256");
    if (!COMMIT.test(commit)) fail("development_build_invalid");
    const target = objectValue(value.platform, "platform");
    exactKeys(target, ["architecture", "minimumWindowsBuild", "os"], "platform");
    if (target.os !== "windows" || target.architecture !== "x64") fail("development_platform_invalid");
    const minimumWindowsBuild = integerValue(target.minimumWindowsBuild, 10_240, 999_999, "minimumWindowsBuild");
    const protocol = objectValue(value.protocol, "protocol");
    exactKeys(protocol, ["maximum", "minimum", "schemaHash"], "protocol");
    const minimum = protocolValue(protocol.minimum, "protocol.minimum");
    const maximum = protocolValue(protocol.maximum, "protocol.maximum");
    const actualSchemaHash = hashValue(protocol.schemaHash, "protocol.schemaHash");
    if (minimum !== protocolVersion || maximum !== protocolVersion || actualSchemaHash !== schemaHash) {
        fail("development_protocol_mismatch");
    }
    const actualPluginVersion = versionValue(value.pluginVersion, "pluginVersion");
    if (actualPluginVersion !== pluginVersion) fail("development_plugin_version_mismatch");
    const filesRaw = arrayValue(value.files, "files");
    if (filesRaw.length < REQUIRED_EXECUTABLES.size || filesRaw.length > 20_000) {
        fail("development_files_invalid");
    }
    const files: DevelopmentFileRecord[] = filesRaw.map((item, index) => {
        const record = objectValue(item, `files[${index}]`);
        exactKeys(record, ["byteLength", "kind", "path", "sha256"], `files[${index}]`);
        return Object.freeze({
            byteLength: integerValue(record.byteLength, 0, 2 * 1024 * 1024 * 1024, "byteLength"),
            kind: textValue(record.kind, "kind"),
            path: runtimeRelativePath(record.path),
            sha256: hashValue(record.sha256, "sha256"),
        });
    });
    const paths = files.map((item) => item.path);
    if (paths.some((item, index) => index > 0 && paths[index - 1] >= item) ||
        new Set(paths.map((item) => item.toLocaleLowerCase("en-US"))).size !== paths.length) {
        fail("development_files_noncanonical");
    }
    const byPath = new Map(files.map((item) => [item.path, item]));
    for (const executable of REQUIRED_EXECUTABLES) {
        if (byPath.get(executable)?.kind !== "executable") fail("development_executable_record_invalid");
    }
    if (byPath.get("process-catalog.v1.json")?.kind !== "asset" ||
        !files.some((item) => item.path.startsWith("skills/") && item.path.endsWith("/SKILL.md"))) {
        fail("development_assets_missing");
    }
    const runtimeContentSha256 = hashValue(value.runtimeContentSha256, "runtimeContentSha256");
    if (sha256(canonicalJson({ files })) !== runtimeContentSha256) fail("development_content_digest_mismatch");
    const manifest: DevelopmentRuntimeManifest = Object.freeze({
        build: Object.freeze({ commit, sourceTreeSha256 }),
        coreVersion: versionValue(value.coreVersion, "coreVersion"),
        developmentOnly: true,
        files: Object.freeze(files),
        platform: Object.freeze({ architecture: "x64", minimumWindowsBuild, os: "windows" }),
        pluginVersion: actualPluginVersion,
        protocol: Object.freeze({ maximum, minimum, schemaHash: actualSchemaHash }),
        runtimeContentSha256,
        runtimeVersion: versionValue(value.runtimeVersion, "runtimeVersion"),
        schemaVersion: 1,
        stateSchemaVersion: integerValue(value.stateSchemaVersion, 1, 2_147_483_647, "stateSchemaVersion"),
        toolAbiVersion: versionValue(value.toolAbiVersion, "toolAbiVersion"),
    });
    if (!payload.equals(Buffer.from(`${canonicalJson(manifest)}\n`, "utf8"))) {
        fail("development_manifest_noncanonical");
    }
    return manifest;
}

async function verifyExactRuntimeTree(
    root: string,
    manifest: DevelopmentRuntimeManifest,
    manifestSha256: string,
): Promise<void> {
    await verifyPinnedManifest(root, manifestSha256);
    const actual = await listRegularTree(root);
    const expected = [MANIFEST_NAME, ...manifest.files.map((item) => item.path)].sort();
    if (actual.length !== expected.length || actual.some((item, index) => item !== expected[index])) {
        fail("development_file_set_mismatch");
    }
    for (const record of manifest.files) {
        const target = confinedPath(root, record.path);
        const before = await requireRegularFile(target);
        if (before.nlink !== 1 || before.size !== record.byteLength || await sha256File(target) !== record.sha256) {
            fail("development_file_identity_mismatch");
        }
        const after = await requireRegularFile(target);
        if (after.nlink !== 1 || before.dev !== after.dev || before.ino !== after.ino ||
            before.size !== after.size || before.mtimeMs !== after.mtimeMs) {
            fail("development_file_changed");
        }
        if (record.kind === "executable") await verifyPeX64(target);
    }
    await verifyPinnedManifest(root, manifestSha256);
}

async function listRegularTree(root: string): Promise<string[]> {
    const output: string[] = [];
    const visit = async (directory: string, prefix: string): Promise<void> => {
        const entries = await fs.readdir(directory, { withFileTypes: true });
        for (const entry of entries.sort((left, right) => left.name.localeCompare(right.name, "en-US"))) {
            const relativePath = prefix ? `${prefix}/${entry.name}` : entry.name;
            const target = confinedPath(root, relativePath);
            const stats = await fs.lstat(target);
            if (entry.isSymbolicLink() || stats.isSymbolicLink()) fail("development_reparse_rejected");
            if (entry.isDirectory()) {
                await visit(target, relativePath);
            } else if (entry.isFile() && stats.isFile() && stats.nlink === 1) {
                output.push(relativePath);
            } else {
                fail("development_special_file_rejected");
            }
        }
    };
    await visit(root, "");
    return output.sort();
}

async function requireRegularFile(path: string) {
    const stats = await fs.lstat(path);
    if (!stats.isFile() || stats.isSymbolicLink()) fail("development_file_type_invalid");
    return stats;
}

async function requireSafeDirectory(path: string): Promise<void> {
    const stats = await fs.lstat(path);
    if (!stats.isDirectory() || stats.isSymbolicLink()) fail("development_root_invalid");
    const canonical = await fs.realpath(path);
    if (resolve(canonical).toLocaleLowerCase("en-US") !== resolve(path).toLocaleLowerCase("en-US")) {
        fail("development_root_reparse");
    }
}

async function requireSafeDirectoryChain(path: string): Promise<void> {
    const absolute = resolve(path);
    const root = parse(absolute).root;
    let current = root;
    await requireSafeDirectory(current);
    const remainder = relative(root, absolute);
    if (remainder === "" || isAbsolute(remainder) || remainder === ".." || remainder.startsWith(`..${sep}`)) {
        if (remainder === "") return;
        fail("development_path_escape");
    }
    for (const part of remainder.split(sep)) {
        if (part === "" || part === "." || part === "..") fail("development_path_invalid");
        current = resolve(current, part);
        await requireSafeDirectory(current);
    }
}

async function sha256File(path: string): Promise<string> {
    const canonical = await fs.realpath(path);
    const handle = await fs.open(path, "r");
    try {
        const before = await handle.stat();
        if (!before.isFile() || before.nlink !== 1) fail("development_file_type_invalid");
        const digest = createHash("sha256");
        const buffer = Buffer.allocUnsafe(1024 * 1024);
        let position = 0;
        for (;;) {
            const { bytesRead } = await handle.read(buffer, 0, buffer.length, position);
            if (bytesRead === 0) break;
            digest.update(buffer.subarray(0, bytesRead));
            position += bytesRead;
        }
        const after = await handle.stat();
        if (!sameFileIdentity(before, after) || resolve(await fs.realpath(path)).toLocaleLowerCase("en-US") !==
            resolve(canonical).toLocaleLowerCase("en-US")) {
            fail("development_file_changed");
        }
        return `sha256:${digest.digest("hex")}`;
    } finally {
        await handle.close();
    }
}

async function verifyPeX64(path: string): Promise<void> {
    const handle = await fs.open(path, "r");
    try {
        const header = Buffer.alloc(4096);
        const { bytesRead } = await handle.read(header, 0, header.length, 0);
        if (bytesRead < 256 || header.readUInt16LE(0) !== 0x5a4d) fail("development_pe_invalid");
        const peOffset = header.readUInt32LE(0x3c);
        if (peOffset < 64 || peOffset + 6 > bytesRead || header.toString("ascii", peOffset, peOffset + 4) !== "PE\0\0" ||
            header.readUInt16LE(peOffset + 4) !== 0x8664) {
            fail("development_pe_architecture_mismatch");
        }
    } finally {
        await handle.close();
    }
}

async function readBounded(path: string, maximum: number): Promise<Buffer> {
    const canonical = await fs.realpath(path);
    const handle = await fs.open(path, "r");
    try {
        const before = await handle.stat();
        if (!before.isFile() || before.nlink !== 1 || before.size < 1 || before.size > maximum) {
            fail("development_manifest_size");
        }
        const payload = await handle.readFile();
        const after = await handle.stat();
        if (payload.length !== before.size || !sameFileIdentity(before, after) ||
            resolve(await fs.realpath(path)).toLocaleLowerCase("en-US") !==
            resolve(canonical).toLocaleLowerCase("en-US")) {
            fail("development_manifest_changed");
        }
        return payload;
    } finally {
        await handle.close();
    }
}

async function verifyPinnedManifest(root: string, expectedSha256: string): Promise<void> {
    const payload = await readBounded(confinedPath(root, MANIFEST_NAME), MAXIMUM_MANIFEST_BYTES);
    if (sha256Bytes(payload) !== expectedSha256) fail("development_manifest_changed");
}

function sameFileIdentity(left: Awaited<ReturnType<typeof fs.stat>>, right: Awaited<ReturnType<typeof fs.stat>>): boolean {
    return left.dev === right.dev && left.ino === right.ino && left.size === right.size && left.mtimeMs === right.mtimeMs &&
        left.nlink === 1 && right.nlink === 1 && left.isFile() && right.isFile();
}

function confinedPath(root: string, relativePath: string): string {
    const base = resolve(root);
    const target = resolve(base, relativePath);
    if (!isWithin(base, target)) fail("development_path_escape");
    return target;
}

function isWithin(root: string, target: string): boolean {
    const value = relative(resolve(root), resolve(target));
    return value === "" || (!value.startsWith(`..${sep}`) && value !== ".." && !isAbsolute(value));
}

function runtimeRelativePath(value: unknown): string {
    const path = textValue(value, "path");
    if (!CANONICAL_PATH.test(path) || path.startsWith("/") || /^[A-Za-z]:/.test(path) ||
        path.split("/").some((part) => part === "" || part === "." || part === "..") ||
        path === MANIFEST_NAME) {
        fail("development_path_invalid");
    }
    return path;
}

function requireCurrentWindowsBuild(minimum: number): void {
    const match = /^10\.0\.([0-9]+)(?:\.|$)/.exec(release());
    if (!match || Number(match[1]) < minimum) fail("development_windows_build_unsupported");
}

function canonicalJson(value: unknown): string {
    const normalize = (item: unknown): unknown => {
        if (Array.isArray(item)) return item.map(normalize);
        if (item !== null && typeof item === "object") {
            return Object.fromEntries(
                Object.keys(item as Record<string, unknown>).sort().map((key) => [
                    key,
                    normalize((item as Record<string, unknown>)[key]),
                ]),
            );
        }
        return item;
    };
    return JSON.stringify(normalize(value));
}

function sha256(value: string): string {
    return `sha256:${createHash("sha256").update(value, "utf8").digest("hex")}`;
}

function sha256Bytes(value: Buffer): string {
    return `sha256:${createHash("sha256").update(value).digest("hex")}`;
}

function exactKeys(value: Record<string, unknown>, expected: string[], label: string): void {
    const actual = Object.keys(value).sort();
    const canonical = [...expected].sort();
    if (actual.length !== canonical.length || actual.some((item, index) => item !== canonical[index])) {
        throw new TypeError(`${label} keys are invalid`);
    }
}

function objectValue(value: unknown, label: string): Record<string, unknown> {
    if (value === null || typeof value !== "object" || Array.isArray(value)) {
        throw new TypeError(`${label} must be an object`);
    }
    return value as Record<string, unknown>;
}

function arrayValue(value: unknown, label: string): unknown[] {
    if (!Array.isArray(value)) throw new TypeError(`${label} must be an array`);
    return value;
}

function textValue(value: unknown, label: string): string {
    if (typeof value !== "string" || value.length < 1 || value.length > 4096) {
        throw new TypeError(`${label} must be bounded text`);
    }
    return value;
}

function hashValue(value: unknown, label: string): string {
    const result = textValue(value, label);
    if (!SHA256.test(result)) throw new TypeError(`${label} must be a SHA-256 digest`);
    return result;
}

function versionValue(value: unknown, label: string): string {
    const result = textValue(value, label);
    if (!VERSION.test(result)) throw new TypeError(`${label} must be a version`);
    return result;
}

function protocolValue(value: unknown, label: string): string {
    const result = textValue(value, label);
    if (!PROTOCOL.test(result)) throw new TypeError(`${label} must be a protocol version`);
    return result;
}

function integerValue(value: unknown, minimum: number, maximum: number, label: string): number {
    if (!Number.isSafeInteger(value) || (value as number) < minimum || (value as number) > maximum) {
        throw new TypeError(`${label} must be a bounded integer`);
    }
    return value as number;
}

function fail(code: string): never {
    throw new LocalDevelopmentRuntimeVerificationError(
        code,
        `${DEVELOPMENT_MARKER}: local development Runtime verification failed (${code})`,
    );
}
