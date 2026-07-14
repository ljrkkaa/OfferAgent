import { Buffer } from "node:buffer";
import { ChildProcess, spawn } from "node:child_process";
import { createHash, createPublicKey, randomBytes, verify as verifySignature } from "node:crypto";
import { createReadStream, promises as fs } from "node:fs";
import { arch, platform, release } from "node:os";
import { isAbsolute, relative, resolve, sep } from "node:path";
import { Readable, Writable } from "node:stream";

import { InstalledRuntime, InstallerPhase, RuntimeInstaller } from "./bootstrap";
import { parseStrictJson } from "./strict_json";

const MAX_MANIFEST_BYTES = 8 * 1024 * 1024;
const MAX_SIGNATURE_BYTES = 1024;
const MAX_HELPER_RESULT_BYTES = 256 * 1024;
const ED25519_SPKI_PREFIX = Buffer.from("302a300506032b6570032100", "hex");
const SHA256 = /^sha256:[0-9a-f]{64}$/;
const VERSION = /^[0-9][0-9A-Za-z.+_-]{0,63}$/;
const KEY_ID = /^[a-z][a-z0-9_.-]{0,63}$/;
const PROTOCOL = /^(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)$/;
const ARCHIVE_PATH = /^[^\\\0]+$/;
const BASE64URL_SIGNATURE = /^[A-Za-z0-9_-]{86}\n$/;
const RECEIPT_CONFIRMATION = "我确认授予 OfferAgent Runtime 上述本地权限";
const IDENTIFIER = /^[a-z][a-z0-9_.-]{0,63}$/;
const ENVIRONMENT_NAME = /^[A-Za-z_][A-Za-z0-9_]{0,127}$/;
const NETWORK_CATEGORIES = new Set(["model", "signed_update"]);
const PE_MACHINE: Readonly<Record<RuntimeArchitecture, number>> = Object.freeze({
    arm64: 0xaa64,
    x64: 0x8664,
});
const BOOTSTRAP_PHASES = new Set<InstallerPhase>([
    "extracting_to_staging",
    "verifying_each_file",
    "atomic_activate",
    "runtime_self_test",
    "ready",
]);

export class RuntimeInstallVerificationError extends Error {
    readonly code: string;

    constructor(code: string, message: string) {
        super(message);
        this.name = "RuntimeInstallVerificationError";
        this.code = code;
    }
}

export type RuntimeArchitecture = "x64" | "arm64";

export function runtimeArchitectureForNodeArch(nodeArchitecture: string): RuntimeArchitecture {
    if (nodeArchitecture === "x64" || nodeArchitecture === "arm64") return nodeArchitecture;
    throw new RuntimeInstallVerificationError(
        "platform_unsupported",
        "OfferAgent Runtime requires native Windows x64 or arm64",
    );
}

export function embeddedPlatformDirectoryForNodeArch(nodeArchitecture: string): string {
    return `windows-${runtimeArchitectureForNodeArch(nodeArchitecture)}`;
}

export interface EmbeddedRuntimeInstallerOptions {
    readonly pluginDirectory: string;
    /** Canonical candidate Vault root, sent only over bootstrap fd3. */
    readonly vaultRoot: string;
    readonly pluginVersion: string;
    readonly protocolVersion: string;
    readonly schemaHash: string;
    /** Raw Ed25519 public keys encoded as canonical unpadded base64url. */
    readonly releasePublicKeys: Readonly<Record<string, string>>;
    readonly ownerId?: string;
    /** Exact legacy owner from this live Obsidian process; used once for safe migration. */
    readonly legacyOwnerId?: string | null;
    readonly timeoutMs?: number;
    readonly spawnProcess?: typeof spawn;
    /** Called only for a first install, a privilege expansion, or an unprovable change. */
    readonly approvePrivilegeExpansion?: (
        request: RuntimePrivilegeApprovalRequest,
        signal: AbortSignal,
    ) => Promise<boolean>;
}

export interface RuntimePrivilegeApprovalRequest {
    readonly currentRuntimeVersion: string | null;
    readonly candidateRuntimeVersion: string;
    readonly oldPrivilegeFingerprint: string | null;
    readonly newPrivilegeFingerprint: string;
    readonly diffHash: string;
    /** Bounded, value-free Chinese descriptions suitable for direct display. */
    readonly summary: ReadonlyArray<string>;
}

interface PrivilegeRootCapability {
    readonly rootId: string;
    readonly relativePath: string;
    readonly access: "cwd" | "read_only" | "read_write";
}

interface EnvironmentPrivilegeProfile {
    readonly profileId: string;
    readonly allowedNames: ReadonlyArray<string>;
    readonly allowedSecretNames: ReadonlyArray<string>;
}

interface ExecutablePrivilegeProfile {
    readonly executableId: string;
    readonly relativePath: string;
    readonly trust: "signed_release";
    readonly fixedArguments: ReadonlyArray<string>;
    readonly minimumVariableArguments: number;
    readonly maximumVariableArguments: number;
    readonly variableArgumentPattern: string;
    readonly allowShellMetacharacters: false;
    readonly allowedCwdRootIds: ReadonlyArray<string>;
    readonly environmentProfileIds: ReadonlyArray<string>;
    readonly allowedStdinModes: ReadonlyArray<string>;
    readonly appContainerFilesystem: ReadonlyArray<PrivilegeRootCapability>;
    readonly privilegeFingerprint: string;
}

interface RuntimeProfilePrivilege {
    readonly kind: "hook" | "shell";
    readonly profileId: string;
    readonly executableId: string;
    readonly risk: string;
    readonly sideEffectClass: string;
    readonly fixedArguments: ReadonlyArray<string>;
    readonly minimumVariableArguments: number;
    readonly maximumVariableArguments: number;
    readonly variableArgumentPattern: string;
    readonly allowedCwdRootIds: ReadonlyArray<string>;
    readonly environmentProfileIds: ReadonlyArray<string>;
    readonly allowedPlainEnvironmentNames: ReadonlyArray<string>;
    readonly allowedSecretEnvironmentNames: ReadonlyArray<string>;
    readonly allowNetwork: false;
    readonly privilegeFingerprint: string;
}

interface RuntimePrivilegeEnvelope {
    readonly schemaVersion: 1;
    readonly processCatalogSha256: string;
    readonly allowedNetworkCategories: ReadonlyArray<string>;
    readonly localProcessNetwork: false;
    readonly rootCapabilities: ReadonlyArray<PrivilegeRootCapability>;
    readonly environmentProfiles: ReadonlyArray<EnvironmentPrivilegeProfile>;
    readonly executableProfiles: ReadonlyArray<ExecutablePrivilegeProfile>;
    readonly profilePrivileges: ReadonlyArray<RuntimeProfilePrivilege>;
    readonly fingerprint: string;
}

interface RuntimePrivilegeApprovalReceipt {
    readonly schemaVersion: 1;
    readonly receiptId: string;
    readonly issuedAt: string;
    readonly expiresAt: string;
    readonly confirmation: typeof RECEIPT_CONFIRMATION;
    readonly oldManifestHash: string | null;
    readonly newManifestHash: string;
    readonly oldPrivilegeFingerprint: string | null;
    readonly newPrivilegeFingerprint: string;
    readonly diffHash: string;
}

interface RuntimeManifest {
    readonly schemaVersion: number;
    readonly runtimeVersion: string;
    readonly coreVersion: string;
    readonly pluginMinimumVersion: string;
    readonly pluginMaximumVersion: string;
    readonly signingKeyId: string;
    readonly buildCommit: string;
    readonly createdAt: string;
    readonly platform: {
        readonly os: "windows";
        readonly architecture: RuntimeArchitecture;
        readonly minimumWindowsBuild: number;
    };
    readonly protocol: {
        readonly minimum: string;
        readonly maximum: string;
        readonly schemaHash: string;
    };
    readonly stateSchemaVersion: number;
    readonly toolAbiVersion: string;
    readonly archive: {
        readonly fileName: string;
        readonly contentDigest: string;
        readonly maximumExpandedBytes: number;
    };
    readonly bootstrap: {
        readonly path: string;
        readonly byteLength: number;
        readonly sha256: string;
        readonly authenticode: true;
        readonly dependencies: ReadonlyArray<{
            readonly path: string;
            readonly byteLength: number;
            readonly sha256: string;
            readonly kind: string;
            readonly authenticode: boolean;
        }>;
    };
    readonly files: ReadonlyArray<{
        readonly path: string;
        readonly byteLength: number;
        readonly sha256: string;
        readonly kind: string;
        readonly authenticode: boolean;
    }>;
    readonly capabilities: ReadonlyArray<string>;
    readonly privilegeEnvelope: RuntimePrivilegeEnvelope | null;
}

interface BootstrapProgress {
    readonly type: "progress";
    readonly phase: InstallerPhase;
}

interface BootstrapResult {
    readonly type: "result";
    readonly status: "ready";
    readonly runtimeVersion: string;
    readonly hostExecutable: string;
    readonly protocolMinimum: string;
    readonly protocolMaximum: string;
    readonly schemaHash: string;
    readonly manifestHash: string;
    readonly bootstrapAuthenticode: true;
    readonly privilegeFingerprint: string;
}

export class EmbeddedRuntimeInstaller implements RuntimeInstaller {
    private readonly pluginDirectory: string;
    private readonly vaultRoot: string;
    private readonly pluginVersion: string;
    private readonly protocolVersion: string;
    private readonly schemaHash: string;
    private readonly keys: Readonly<Record<string, string>>;
    private readonly ownerId: string;
    private readonly legacyOwnerId: string | null;
    private readonly timeoutMs: number;
    private readonly spawnProcess: typeof spawn;
    private readonly approvePrivilegeExpansion: ((
        request: RuntimePrivilegeApprovalRequest,
        signal: AbortSignal,
    ) => Promise<boolean>) | null;

    constructor(options: EmbeddedRuntimeInstallerOptions) {
        if (!isAbsolute(options.pluginDirectory) || options.pluginDirectory.includes("\0")) {
            throw new TypeError("pluginDirectory must be an absolute local path");
        }
        if (!isAbsolute(options.vaultRoot) || options.vaultRoot.includes("\0") ||
            options.vaultRoot.includes("\r") || options.vaultRoot.includes("\n") ||
            options.vaultRoot.startsWith("\\\\") || options.vaultRoot.startsWith("//")) {
            throw new TypeError("vaultRoot must be an absolute local path");
        }
        if (!VERSION.test(options.pluginVersion) || !PROTOCOL.test(options.protocolVersion) ||
            !SHA256.test(options.schemaHash)) {
            throw new TypeError("plugin/protocol/schema identity is invalid");
        }
        if (Object.keys(options.releasePublicKeys).length === 0) throw new TypeError("release keyring is empty");
        for (const [keyId, key] of Object.entries(options.releasePublicKeys)) {
            const decoded = decodeBase64url(key);
            if (!KEY_ID.test(keyId) || !/^[A-Za-z0-9_-]{43}$/.test(key) || decoded.length !== 32 ||
                encodeBase64url(decoded) !== key) {
                throw new TypeError("release keyring contains an invalid Ed25519 key");
            }
        }
        this.pluginDirectory = resolve(options.pluginDirectory);
        this.vaultRoot = resolve(options.vaultRoot);
        this.pluginVersion = options.pluginVersion;
        this.protocolVersion = options.protocolVersion;
        this.schemaHash = options.schemaHash;
        this.keys = Object.freeze({ ...options.releasePublicKeys });
        this.ownerId = options.ownerId ?? "obsidian-plugin";
        if (!/^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$/.test(this.ownerId)) throw new TypeError("ownerId is invalid");
        this.legacyOwnerId = options.legacyOwnerId ?? null;
        if (this.legacyOwnerId !== null && !/^obsidian-[1-9][0-9]{0,19}$/.test(this.legacyOwnerId)) {
            throw new TypeError("legacyOwnerId is invalid");
        }
        this.timeoutMs = positiveInteger(options.timeoutMs ?? 120_000, "timeoutMs");
        this.spawnProcess = options.spawnProcess ?? spawn;
        this.approvePrivilegeExpansion = options.approvePrivilegeExpansion ?? null;
    }

    async ensureReady(signal: AbortSignal, onPhase: (phase: InstallerPhase) => void): Promise<InstalledRuntime> {
        signal.throwIfAborted();
        onPhase("not_installed");
        onPhase("locating_embedded_bundle");
        if (platform() !== "win32") fail("platform_unsupported");
        const runtimeArchitecture = runtimeArchitectureForNodeArch(arch());
        const embeddedRoot = confinedPath(this.pluginDirectory, "runtime");
        const platformRoot = confinedPath(
            embeddedRoot,
            embeddedPlatformDirectoryForNodeArch(runtimeArchitecture),
        );
        const manifestPath = confinedPath(platformRoot, "runtime-manifest.json");
        const signaturePath = confinedPath(platformRoot, "runtime-manifest.sig");
        onPhase("verifying_manifest_and_signature");
        const manifestBytes = await readBounded(manifestPath, MAX_MANIFEST_BYTES);
        const signatureBytes = await readBounded(signaturePath, MAX_SIGNATURE_BYTES);
        const manifest = parseAndVerifyManifest(
            manifestBytes,
            signatureBytes,
            this.keys,
            this.pluginVersion,
            this.protocolVersion,
            this.schemaHash,
        );
        requireWindowsArchitecture(manifest, runtimeArchitecture);
        const archivePath = confinedPath(platformRoot, manifest.archive.fileName);
        await requireRegularFile(archivePath);
        const bootstrapPath = confinedPath(platformRoot, manifest.bootstrap.path);
        await verifyBootstrapIdentity(bootstrapPath, manifest.bootstrap, runtimeArchitecture);
        for (const dependency of manifest.bootstrap.dependencies) {
            const dependencyPath = confinedPath(platformRoot, dependency.path);
            const stats = await requireRegularFile(dependencyPath);
            if (stats.size !== dependency.byteLength || await sha256File(dependencyPath) !== dependency.sha256) {
                fail("bootstrap_dependency_identity");
            }
            if (dependency.authenticode) await verifyPeArchitecture(dependencyPath, runtimeArchitecture);
        }
        const actualOuterFiles = await listRegularTree(platformRoot);
        const expectedOuterFiles = [
            "runtime-manifest.json",
            "runtime-manifest.sig",
            manifest.archive.fileName,
            manifest.bootstrap.path,
            ...manifest.bootstrap.dependencies.map((item) => item.path),
        ].sort();
        if (actualOuterFiles.length !== expectedOuterFiles.length ||
            actualOuterFiles.some((item, index) => item !== expectedOuterFiles[index])) {
            fail("payload_file_set_mismatch");
        }
        signal.throwIfAborted();
        if (manifest.privilegeEnvelope === null) fail("privilege_envelope_missing");
        const current = await loadCurrentPrivilegeState(this.keys, runtimeArchitecture);
        const assessment = assessPrivilegeChange(current?.manifest.privilegeEnvelope ?? null, manifest.privilegeEnvelope);
        let privilegeApproval: RuntimePrivilegeApprovalReceipt | null = null;
        if (!assessment.automatic) {
            if (this.approvePrivilegeExpansion === null) {
                throw new RuntimeInstallVerificationError(
                    "runtime_privilege_approval_required",
                    "Runtime 权限变化需要用户明确确认",
                );
            }
            const approved = await this.approvePrivilegeExpansion(
                {
                    currentRuntimeVersion: current?.manifest.runtimeVersion ?? null,
                    candidateRuntimeVersion: manifest.runtimeVersion,
                    oldPrivilegeFingerprint: assessment.oldFingerprint,
                    newPrivilegeFingerprint: assessment.newFingerprint,
                    diffHash: assessment.diffHash,
                    summary: summarizePrivilegeChange(current?.manifest.privilegeEnvelope ?? null, manifest.privilegeEnvelope),
                },
                signal,
            );
            signal.throwIfAborted();
            if (!approved) {
                throw new RuntimeInstallVerificationError(
                    "runtime_privilege_approval_declined",
                    "用户未授予 Runtime 新权限",
                );
            }
            privilegeApproval = createPrivilegeApprovalReceipt(
                current?.manifestHash ?? null,
                sha256(manifestBytes),
                assessment,
            );
        }
        signal.throwIfAborted();
        return invokeSignedBootstrap({
            executable: bootstrapPath,
            platformRoot,
            manifest,
            manifestHash: sha256(manifestBytes),
            pluginVersion: this.pluginVersion,
            protocolVersion: this.protocolVersion,
            schemaHash: this.schemaHash,
            vaultRoot: this.vaultRoot,
            ownerId: this.ownerId,
            legacyOwnerId: this.legacyOwnerId,
            privilegeApproval,
            timeoutMs: this.timeoutMs,
            signal,
            onPhase,
            spawnProcess: this.spawnProcess,
        });
    }
}

function parseAndVerifyManifest(
    payload: Buffer,
    signaturePayload: Buffer,
    keys: Readonly<Record<string, string>>,
    pluginVersion: string,
    protocolVersion: string,
    schemaHash: string,
    allowLegacy = false,
): RuntimeManifest {
    let raw: unknown;
    try {
        raw = parseStrictJson(new TextDecoder("utf-8", { fatal: true }).decode(payload), {
            maximumCharacters: MAX_MANIFEST_BYTES,
        });
    } catch {
        throw new RuntimeInstallVerificationError("manifest_malformed", "Runtime manifest is malformed");
    }
    const value = objectValue(raw, "manifest");
    const schemaVersion = integer(value.schemaVersion, "schemaVersion", 1, 2);
    const manifestKeys = [
        "archive", "bootstrap", "buildCommit", "capabilities", "coreVersion", "createdAt", "files", "platform",
        "pluginMaximumVersion", "pluginMinimumVersion", "protocol", "runtimeVersion", "schemaVersion", "signingKeyId",
        "stateSchemaVersion", "toolAbiVersion",
    ];
    if (schemaVersion === 2) manifestKeys.push("privilegeEnvelope");
    exactKeys(value, manifestKeys, "manifest");
    const canonical = Buffer.from(canonicalJson(value), "utf8");
    if (!canonical.equals(payload)) {
        throw new RuntimeInstallVerificationError("manifest_noncanonical", "Runtime manifest is not canonical JSON");
    }
    const signingKeyId = text(value.signingKeyId, "signingKeyId");
    const encodedKey = keys[signingKeyId];
    if (!encodedKey) throw new RuntimeInstallVerificationError("release_key_unknown", "Runtime signing key is unknown");
    if (!BASE64URL_SIGNATURE.test(signaturePayload.toString("ascii"))) {
        throw new RuntimeInstallVerificationError("signature_encoding_invalid", "Runtime signature encoding is invalid");
    }
    const signature = decodeBase64url(signaturePayload.subarray(0, -1).toString("ascii"));
    if (signature.length !== 64 || encodeBase64url(signature) !== signaturePayload.subarray(0, -1).toString("ascii")) {
        throw new RuntimeInstallVerificationError("signature_encoding_invalid", "signature length is invalid");
    }
    const publicKey = createPublicKey({
        key: Buffer.concat([ED25519_SPKI_PREFIX, decodeBase64url(encodedKey)]),
        format: "der",
        type: "spki",
    });
    if (!verifySignature(null, payload, publicKey, signature)) {
        throw new RuntimeInstallVerificationError("release_signature_invalid", "Runtime signature is invalid");
    }
    const manifest = validateManifest(value, allowLegacy);
    if (!allowLegacy) {
        if (compareVersion(pluginVersion, manifest.pluginMinimumVersion) < 0 ||
            compareVersion(pluginVersion, manifest.pluginMaximumVersion) > 0) {
            throw new RuntimeInstallVerificationError("plugin_runtime_incompatible", "Plugin is outside Runtime range");
        }
        const protocol = protocolTuple(protocolVersion);
        if (compareTuple(protocol, protocolTuple(manifest.protocol.minimum)) < 0 ||
            compareTuple(protocol, protocolTuple(manifest.protocol.maximum)) > 0 || manifest.protocol.schemaHash !== schemaHash) {
            throw new RuntimeInstallVerificationError("protocol_runtime_incompatible", "Runtime protocol/schema is incompatible");
        }
    }
    return manifest;
}

function validateManifest(value: Record<string, unknown>, allowLegacy: boolean): RuntimeManifest {
    const platformValue = objectValue(value.platform, "platform");
    exactKeys(platformValue, ["architecture", "minimumWindowsBuild", "os"], "platform");
    const protocolValue = objectValue(value.protocol, "protocol");
    exactKeys(protocolValue, ["maximum", "minimum", "schemaHash"], "protocol");
    const archiveValue = objectValue(value.archive, "archive");
    exactKeys(archiveValue, ["contentDigest", "fileName", "maximumExpandedBytes"], "archive");
    const bootstrapValue = objectValue(value.bootstrap, "bootstrap");
    exactKeys(bootstrapValue, ["authenticode", "byteLength", "dependencies", "path", "sha256"], "bootstrap");
    const filesValue = arrayValue(value.files, "files");
    const files = filesValue.map((item) => {
        const file = objectValue(item, "file");
        exactKeys(file, ["authenticode", "byteLength", "kind", "path", "sha256"], "file");
        const record = {
            path: safeArchiveName(text(file.path, "file.path"), false),
            byteLength: integer(file.byteLength, "file.byteLength", 0, 2 * 1024 * 1024 * 1024),
            sha256: hash(file.sha256, "file.sha256"),
            kind: text(file.kind, "file.kind"),
            authenticode: booleanValue(file.authenticode, "file.authenticode"),
        };
        if (record.authenticode && !record.path.toLowerCase().endsWith(".exe")) fail("authenticode_target_invalid");
        return record;
    });
    if (files.length === 0 || files.some((record, index) => index > 0 && files[index - 1].path >= record.path)) {
        fail("manifest_files_unsorted");
    }
    const folded = files.map((file) => file.path.toLowerCase());
    if (new Set(folded).size !== folded.length) fail("manifest_path_collision");
    for (const name of [
        "offeragent-host.exe",
        "offeragent-process-host.exe",
        "offeragent-worker.exe",
        "offeragent-self-test.exe",
    ]) {
        const executable = files.find((file) => file.path === name);
        if (!executable || executable.kind !== "executable" || !executable.authenticode) fail("runtime_executable_missing");
    }
    if (!files.some((file) => file.path === "web/index.html") ||
        !["license", "provenance", "sbom", "web"].every((kind) => files.some((file) => file.kind === kind))) {
        fail("release_metadata_missing");
    }
    const capabilities = arrayValue(value.capabilities, "capabilities").map((item) => text(item, "capability"));
    if (capabilities.some((item, index) => !KEY_ID.test(item) || (index > 0 && capabilities[index - 1] >= item))) {
        fail("capabilities_invalid");
    }
    const bootstrapDependencies = arrayValue(bootstrapValue.dependencies, "bootstrap.dependencies").map((item) => {
        const dependency = objectValue(item, "bootstrap.dependency");
        exactKeys(dependency, ["authenticode", "byteLength", "kind", "path", "sha256"], "bootstrap.dependency");
        return {
            path: safeArchiveName(text(dependency.path, "bootstrap.dependency.path"), false),
            byteLength: integer(dependency.byteLength, "bootstrap.dependency.byteLength", 0, 2 * 1024 * 1024 * 1024),
            sha256: hash(dependency.sha256, "bootstrap.dependency.sha256"),
            kind: text(dependency.kind, "bootstrap.dependency.kind"),
            authenticode: booleanValue(dependency.authenticode, "bootstrap.dependency.authenticode"),
        };
    });
    if (bootstrapDependencies.some((record, index) => index > 0 && bootstrapDependencies[index - 1].path >= record.path) ||
        new Set(bootstrapDependencies.map((record) => record.path.toLowerCase())).size !== bootstrapDependencies.length) {
        fail("bootstrap_dependencies_invalid");
    }
    const schemaVersion = integer(value.schemaVersion, "schemaVersion", 1, 2);
    if (schemaVersion !== 2 && !allowLegacy) fail("manifest_privilege_envelope_required");
    const privilegeEnvelope = schemaVersion === 2 ? validatePrivilegeEnvelope(value.privilegeEnvelope) : null;
    const manifest: RuntimeManifest = {
        schemaVersion,
        runtimeVersion: version(value.runtimeVersion, "runtimeVersion"),
        coreVersion: version(value.coreVersion, "coreVersion"),
        pluginMinimumVersion: version(value.pluginMinimumVersion, "pluginMinimumVersion"),
        pluginMaximumVersion: version(value.pluginMaximumVersion, "pluginMaximumVersion"),
        signingKeyId: keyId(value.signingKeyId, "signingKeyId"),
        buildCommit: matchText(value.buildCommit, /^[0-9a-f]{40}$/, "buildCommit"),
        createdAt: timestamp(value.createdAt),
        platform: {
            os: matchText(platformValue.os, /^windows$/, "platform.os") as "windows",
            architecture: matchText(
                platformValue.architecture,
                /^(?:x64|arm64)$/,
                "platform.architecture",
            ) as RuntimeArchitecture,
            minimumWindowsBuild: integer(platformValue.minimumWindowsBuild, "minimumWindowsBuild", 10_240, 99_999),
        },
        protocol: {
            minimum: protocolText(protocolValue.minimum, "protocol.minimum"),
            maximum: protocolText(protocolValue.maximum, "protocol.maximum"),
            schemaHash: hash(protocolValue.schemaHash, "protocol.schemaHash"),
        },
        stateSchemaVersion: integer(value.stateSchemaVersion, "stateSchemaVersion", 1, Number.MAX_SAFE_INTEGER),
        toolAbiVersion: version(value.toolAbiVersion, "toolAbiVersion"),
        archive: {
            fileName: safeArchiveName(text(archiveValue.fileName, "archive.fileName"), true),
            contentDigest: hash(archiveValue.contentDigest, "archive.contentDigest"),
            maximumExpandedBytes: integer(
                archiveValue.maximumExpandedBytes,
                "archive.maximumExpandedBytes",
                1,
                4 * 1024 * 1024 * 1024,
            ),
        },
        bootstrap: {
            path: safeArchiveName(text(bootstrapValue.path, "bootstrap.path"), true),
            byteLength: integer(bootstrapValue.byteLength, "bootstrap.byteLength", 1, 1024 * 1024 * 1024),
            sha256: hash(bootstrapValue.sha256, "bootstrap.sha256"),
            authenticode: requireTrue(bootstrapValue.authenticode, "bootstrap.authenticode"),
            dependencies: bootstrapDependencies,
        },
        files,
        capabilities,
        privilegeEnvelope,
    };
    if (compareVersion(manifest.pluginMinimumVersion, manifest.pluginMaximumVersion) > 0 ||
        compareTuple(protocolTuple(manifest.protocol.minimum), protocolTuple(manifest.protocol.maximum)) > 0) {
        fail("compatibility_range_invalid");
    }
    const digestPayload = files.map((file) => ({ byteLength: file.byteLength, path: file.path, sha256: file.sha256 }));
    if (sha256(Buffer.from(JSON.stringify(digestPayload), "utf8")) !== manifest.archive.contentDigest) {
        fail("archive_digest_mismatch");
    }
    if (files.reduce((total, file) => total + file.byteLength, 0) > manifest.archive.maximumExpandedBytes) {
        fail("archive_expanded_limit");
    }
    if (privilegeEnvelope !== null) {
        const catalog = files.find((file) => file.path === "process-catalog.v1.json");
        if (!catalog || catalog.kind !== "asset" || catalog.sha256 !== privilegeEnvelope.processCatalogSha256) {
            fail("privilege_catalog_identity");
        }
    }
    return manifest;
}

function validatePrivilegeEnvelope(raw: unknown): RuntimePrivilegeEnvelope {
    const value = objectValue(raw, "privilegeEnvelope");
    exactKeys(value, [
        "allowedNetworkCategories", "environmentProfiles", "executableProfiles", "fingerprint",
        "localProcessNetwork", "processCatalogSha256", "profilePrivileges", "rootCapabilities", "schemaVersion",
    ], "privilegeEnvelope");
    if (integer(value.schemaVersion, "privilegeEnvelope.schemaVersion", 1, 1) !== 1) fail("privilege_schema");
    const allowedNetworkCategories = sortedUniqueText(
        value.allowedNetworkCategories, "allowedNetworkCategories", KEY_ID, NETWORK_CATEGORIES,
    );
    if (booleanValue(value.localProcessNetwork, "localProcessNetwork")) fail("privilege_local_network");
    const environmentProfiles = arrayValue(value.environmentProfiles, "environmentProfiles").map((rawProfile) => {
        const profile = objectValue(rawProfile, "environment privilege profile");
        exactKeys(profile, ["allowedNames", "allowedSecretNames", "profileId"], "environment privilege profile");
        const allowedNames = sortedUniqueText(profile.allowedNames, "allowedNames", ENVIRONMENT_NAME);
        const allowedSecretNames = sortedUniqueText(
            profile.allowedSecretNames, "allowedSecretNames", ENVIRONMENT_NAME,
        );
        if (allowedNames.some((name) => allowedSecretNames.includes(name))) fail("privilege_environment_overlap");
        return {
            profileId: matchText(profile.profileId, IDENTIFIER, "environment profileId"),
            allowedNames,
            allowedSecretNames,
        };
    });
    requireCanonicalRecords(environmentProfiles, (item) => item.profileId, "environmentProfiles");
    const rootCapabilities = arrayValue(value.rootCapabilities, "rootCapabilities").map((item) =>
        validateRootCapability(item, true));
    requireCanonicalRecords(
        rootCapabilities, (item) => `${item.rootId}\0${item.relativePath}\0${item.access}`, "rootCapabilities",
    );
    const executableProfiles = arrayValue(value.executableProfiles, "executableProfiles").map((rawProfile) => {
        const profile = objectValue(rawProfile, "executable privilege profile");
        exactKeys(profile, [
            "allowShellMetacharacters", "allowedCwdRootIds", "allowedStdinModes", "appContainerFilesystem",
            "environmentProfileIds", "executableId", "fixedArguments", "maximumVariableArguments",
            "minimumVariableArguments", "privilegeFingerprint", "relativePath", "trust", "variableArgumentPattern",
        ], "executable privilege profile");
        if (booleanValue(profile.allowShellMetacharacters, "allowShellMetacharacters")) {
            fail("privilege_shell_metacharacters");
        }
        const minimum = integer(profile.minimumVariableArguments, "minimumVariableArguments", 0, 64);
        const maximum = integer(profile.maximumVariableArguments, "maximumVariableArguments", minimum, 64);
        const appContainerFilesystem = arrayValue(
            profile.appContainerFilesystem, "appContainerFilesystem",
        ).map((item) => validateRootCapability(item, false));
        requireCanonicalRecords(
            appContainerFilesystem,
            (item) => `${item.rootId}\0${item.relativePath}\0${item.access}`,
            "appContainerFilesystem",
        );
        const result: ExecutablePrivilegeProfile = {
            allowShellMetacharacters: false,
            allowedCwdRootIds: sortedUniqueText(profile.allowedCwdRootIds, "allowedCwdRootIds", IDENTIFIER),
            allowedStdinModes: sortedUniqueText(profile.allowedStdinModes, "allowedStdinModes", IDENTIFIER),
            appContainerFilesystem,
            environmentProfileIds: sortedUniqueText(
                profile.environmentProfileIds, "environmentProfileIds", IDENTIFIER,
            ),
            executableId: matchText(profile.executableId, IDENTIFIER, "executableId"),
            fixedArguments: privilegeArguments(profile.fixedArguments),
            maximumVariableArguments: maximum,
            minimumVariableArguments: minimum,
            privilegeFingerprint: hash(profile.privilegeFingerprint, "privilegeFingerprint"),
            relativePath: privilegeRelativePath(profile.relativePath, false),
            trust: matchText(profile.trust, /^signed_release$/, "trust") as "signed_release",
            variableArgumentPattern: boundedPrivilegeText(profile.variableArgumentPattern, "variableArgumentPattern", 4096),
        };
        if (privilegeFingerprint(withoutPrivilegeFingerprint(result)) !== result.privilegeFingerprint) {
            fail("privilege_item_fingerprint");
        }
        return result;
    });
    requireCanonicalRecords(executableProfiles, (item) => item.executableId, "executableProfiles");
    const risks = new Set(["read", "network", "write", "execute", "destructive", "external_path", "secret_access"]);
    const sideEffects = new Set(["none", "read", "network", "write", "execute", "destructive", "unknown"]);
    const profilePrivileges = arrayValue(value.profilePrivileges, "profilePrivileges").map((rawProfile) => {
        const profile = objectValue(rawProfile, "Runtime profile privilege");
        exactKeys(profile, [
            "allowNetwork", "allowedCwdRootIds", "allowedPlainEnvironmentNames", "allowedSecretEnvironmentNames",
            "environmentProfileIds", "executableId", "fixedArguments", "kind", "maximumVariableArguments",
            "minimumVariableArguments", "privilegeFingerprint", "profileId", "risk", "sideEffectClass",
            "variableArgumentPattern",
        ], "Runtime profile privilege");
        if (booleanValue(profile.allowNetwork, "allowNetwork")) fail("privilege_local_network");
        const minimum = integer(profile.minimumVariableArguments, "minimumVariableArguments", 0, 64);
        const maximum = integer(profile.maximumVariableArguments, "maximumVariableArguments", minimum, 64);
        const allowedPlainEnvironmentNames = sortedUniqueText(
            profile.allowedPlainEnvironmentNames, "allowedPlainEnvironmentNames", ENVIRONMENT_NAME,
        );
        const allowedSecretEnvironmentNames = sortedUniqueText(
            profile.allowedSecretEnvironmentNames, "allowedSecretEnvironmentNames", ENVIRONMENT_NAME,
        );
        if (allowedPlainEnvironmentNames.some((name) => allowedSecretEnvironmentNames.includes(name))) {
            fail("privilege_environment_overlap");
        }
        const result: RuntimeProfilePrivilege = {
            allowNetwork: false,
            allowedCwdRootIds: sortedUniqueText(profile.allowedCwdRootIds, "allowedCwdRootIds", IDENTIFIER),
            allowedPlainEnvironmentNames,
            allowedSecretEnvironmentNames,
            environmentProfileIds: sortedUniqueText(
                profile.environmentProfileIds, "environmentProfileIds", IDENTIFIER,
            ),
            executableId: matchText(profile.executableId, IDENTIFIER, "executableId"),
            fixedArguments: privilegeArguments(profile.fixedArguments),
            kind: matchText(profile.kind, /^(?:hook|shell)$/, "kind") as "hook" | "shell",
            maximumVariableArguments: maximum,
            minimumVariableArguments: minimum,
            privilegeFingerprint: hash(profile.privilegeFingerprint, "privilegeFingerprint"),
            profileId: matchText(profile.profileId, IDENTIFIER, "profileId"),
            risk: memberText(profile.risk, risks, "risk"),
            sideEffectClass: memberText(profile.sideEffectClass, sideEffects, "sideEffectClass"),
            variableArgumentPattern: boundedPrivilegeText(profile.variableArgumentPattern, "variableArgumentPattern", 4096),
        };
        if (privilegeFingerprint(withoutPrivilegeFingerprint(result)) !== result.privilegeFingerprint) {
            fail("privilege_item_fingerprint");
        }
        return result;
    });
    requireCanonicalRecords(
        profilePrivileges, (item) => `${item.kind}\0${item.profileId}`, "profilePrivileges",
    );
    const envelope: RuntimePrivilegeEnvelope = {
        allowedNetworkCategories,
        environmentProfiles,
        executableProfiles,
        fingerprint: hash(value.fingerprint, "fingerprint"),
        localProcessNetwork: false,
        processCatalogSha256: hash(value.processCatalogSha256, "processCatalogSha256"),
        profilePrivileges,
        rootCapabilities,
        schemaVersion: 1,
    };
    if (Math.max(
        rootCapabilities.length,
        environmentProfiles.length,
        executableProfiles.length,
        profilePrivileges.length,
    ) > 128) fail("privilege_envelope_limit");
    validatePrivilegeReferences(envelope);
    if (privilegeFingerprint(withoutEnvelopeFingerprint(envelope)) !== envelope.fingerprint) {
        fail("privilege_envelope_fingerprint");
    }
    if (Buffer.byteLength(JSON.stringify(sortJson(envelope)), "utf8") > 32 * 1024) fail("privilege_envelope_limit");
    return envelope;
}

function validateRootCapability(raw: unknown, allowCwd: boolean): PrivilegeRootCapability {
    const value = objectValue(raw, "root capability");
    exactKeys(value, ["access", "relativePath", "rootId"], "root capability");
    const access = matchText(value.access, /^(?:cwd|read_only|read_write)$/, "access") as PrivilegeRootCapability["access"];
    if (!allowCwd && access === "cwd") fail("privilege_appcontainer_access");
    const relativePath = privilegeRelativePath(value.relativePath, access === "cwd");
    if (access === "cwd" && relativePath !== ".") fail("privilege_root_cwd");
    return { access, relativePath, rootId: matchText(value.rootId, IDENTIFIER, "rootId") };
}

function validatePrivilegeReferences(envelope: RuntimePrivilegeEnvelope): void {
    const environmentIds = new Set(envelope.environmentProfiles.map((item) => item.profileId));
    const executableIds = new Set(envelope.executableProfiles.map((item) => item.executableId));
    const rootIds = new Set(envelope.rootCapabilities.map((item) => item.rootId));
    for (const executable of envelope.executableProfiles) {
        if (executable.environmentProfileIds.some((item) => !environmentIds.has(item)) ||
            executable.allowedCwdRootIds.some((item) => !rootIds.has(item))) fail("privilege_reference");
    }
    for (const profile of envelope.profilePrivileges) {
        if (!executableIds.has(profile.executableId) ||
            profile.environmentProfileIds.some((item) => !environmentIds.has(item)) ||
            profile.allowedCwdRootIds.some((item) => !rootIds.has(item))) fail("privilege_reference");
    }
}

function withoutPrivilegeFingerprint<T extends { readonly privilegeFingerprint: string }>(
    value: T,
): Omit<T, "privilegeFingerprint"> {
    const copy: Record<string, unknown> = { ...value };
    delete copy.privilegeFingerprint;
    return copy as Omit<T, "privilegeFingerprint">;
}

function withoutEnvelopeFingerprint(envelope: RuntimePrivilegeEnvelope): Omit<RuntimePrivilegeEnvelope, "fingerprint"> {
    const copy: Record<string, unknown> = { ...envelope };
    delete copy.fingerprint;
    return copy as Omit<RuntimePrivilegeEnvelope, "fingerprint">;
}

function privilegeFingerprint(value: unknown): string {
    return `sha256:${createHash("sha256").update(JSON.stringify(sortJson(value)), "utf8").digest("hex")}`;
}

function sortedUniqueText(
    raw: unknown,
    label: string,
    pattern: RegExp,
    allowed?: ReadonlySet<string>,
): string[] {
    const values = arrayValue(raw, label).map((item) => matchText(item, pattern, label));
    if (values.some((item, index) => index > 0 && values[index - 1] >= item) ||
        new Set(values).size !== values.length || values.some((item) => allowed !== undefined && !allowed.has(item))) {
        fail("privilege_order_or_value");
    }
    return values;
}

function privilegeArguments(raw: unknown): string[] {
    const values = arrayValue(raw, "fixedArguments");
    if (values.length > 64) fail("privilege_arguments");
    return values.map((item) => {
        const value = text(item, "fixed argument");
        if (value.length > 4096 || /[\0\r\n]/.test(value)) fail("privilege_arguments");
        return value;
    });
}

function boundedPrivilegeText(raw: unknown, label: string, maximum: number): string {
    const value = text(raw, label);
    if (value.length < 1 || value.length > maximum || /[\0\r\n]/.test(value)) fail("privilege_text");
    return value;
}

function privilegeRelativePath(raw: unknown, allowDot: boolean): string {
    const value = text(raw, "manifest-relative path");
    if (allowDot && value === ".") return value;
    boundedPrivilegeText(value, "manifest-relative path", 512);
    if (value.includes("\\") || value.startsWith("/") || value.split("/").some((part) =>
        part.length === 0 || part === "." || part === ".." || part.includes(":"))) fail("privilege_relative_path");
    return value;
}

function memberText(raw: unknown, allowed: ReadonlySet<string>, label: string): string {
    const value = text(raw, label);
    if (!allowed.has(value)) fail("privilege_value");
    return value;
}

function requireCanonicalRecords<T>(values: ReadonlyArray<T>, key: (value: T) => string, label: string): void {
    const keys = values.map(key);
    if (keys.some((item, index) => index > 0 && keys[index - 1] >= item) || new Set(keys).size !== keys.length) {
        fail(`${label}_order`);
    }
}

interface CurrentPrivilegeState {
    readonly manifest: RuntimeManifest;
    readonly manifestHash: string;
}

interface PrivilegeAssessment {
    readonly automatic: boolean;
    readonly oldFingerprint: string | null;
    readonly newFingerprint: string;
    readonly diffHash: string;
}

async function loadCurrentPrivilegeState(
    keys: Readonly<Record<string, string>>,
    runtimeArchitecture: RuntimeArchitecture,
): Promise<CurrentPrivilegeState | null> {
    const localAppData = process.env.LOCALAPPDATA;
    if (!localAppData) fail("local_app_data_missing");
    const runtimeRoot = resolve(localAppData, "OfferAgent", "runtime");
    const pointerPath = resolve(runtimeRoot, "current.json");
    let pointerBytes: Buffer;
    try {
        pointerBytes = await readBounded(pointerPath, 128 * 1024);
    } catch (error) {
        if ((error as NodeJS.ErrnoException).code === "ENOENT" ||
            error instanceof RuntimeInstallVerificationError && error.code === "release_file_unavailable") {
            try {
                await fs.access(pointerPath);
            } catch (accessError) {
                if ((accessError as NodeJS.ErrnoException).code === "ENOENT") return null;
            }
        }
        throw error;
    }
    let raw: unknown;
    try {
        raw = parseStrictJson(new TextDecoder("utf-8", { fatal: true }).decode(pointerBytes), {
            maximumCharacters: 128 * 1024,
        });
    } catch {
        fail("current_pointer_malformed");
    }
    const pointer = objectValue(raw, "current pointer");
    const legacyKeys = [
        "activatedAt", "currentVersion", "generation", "manifestHash", "previousVersion", "rollbackSnapshot",
        "stateSchemaVersion",
    ];
    const currentKeys = [...legacyKeys, "privilegeApprovalJournal"];
    const actualKeys = Object.keys(pointer).sort();
    if (!sameStrings(actualKeys, legacyKeys.slice().sort()) && !sameStrings(actualKeys, currentKeys.slice().sort())) {
        fail("current_pointer_fields");
    }
    const currentVersion = version(pointer.currentVersion, "currentVersion");
    const manifestHash = hash(pointer.manifestHash, "manifestHash");
    timestamp(pointer.activatedAt);
    integer(pointer.generation, "generation", 1, Number.MAX_SAFE_INTEGER);
    integer(pointer.stateSchemaVersion, "stateSchemaVersion", 1, Number.MAX_SAFE_INTEGER);
    if (pointer.previousVersion !== null) version(pointer.previousVersion, "previousVersion");
    if (pointer.rollbackSnapshot !== null && typeof pointer.rollbackSnapshot !== "string") fail("current_pointer_type");
    if (actualKeys.includes("privilegeApprovalJournal")) validatePrivilegeJournal(pointer.privilegeApprovalJournal);
    if (!Buffer.from(canonicalJson(pointer), "utf8").equals(pointerBytes)) fail("current_pointer_noncanonical");
    const currentRoot = confinedPath(runtimeRoot, currentVersion);
    let rootStats;
    try {
        rootStats = await fs.lstat(currentRoot);
    } catch {
        fail("current_runtime_missing");
    }
    if (!rootStats.isDirectory() || rootStats.isSymbolicLink()) fail("current_runtime_type");
    const manifestPath = confinedPath(currentRoot, "runtime-manifest.json");
    const signaturePath = confinedPath(currentRoot, "runtime-manifest.sig");
    const manifestBytes = await readBounded(manifestPath, MAX_MANIFEST_BYTES);
    const signatureBytes = await readBounded(signaturePath, MAX_SIGNATURE_BYTES);
    if (sha256(manifestBytes) !== manifestHash) fail("installed_manifest_pointer_mismatch");
    const manifest = parseAndVerifyManifest(
        manifestBytes,
        signatureBytes,
        keys,
        "0",
        "0.0",
        `sha256:${"0".repeat(64)}`,
        true,
    );
    requireWindowsArchitecture(manifest, runtimeArchitecture);
    if (manifest.runtimeVersion !== currentVersion) fail("installed_manifest_version_mismatch");
    const catalog = manifest.files.find((item) => item.path === "process-catalog.v1.json");
    if (!catalog || catalog.kind !== "asset") fail("installed_privilege_catalog_missing");
    const catalogPath = confinedPath(currentRoot, catalog.path);
    const catalogStats = await requireRegularFile(catalogPath);
    if (catalogStats.size !== catalog.byteLength || await sha256File(catalogPath) !== catalog.sha256) {
        fail("installed_privilege_catalog_identity");
    }
    return { manifest, manifestHash };
}

function validatePrivilegeJournal(raw: unknown): void {
    const values = arrayValue(raw, "privilegeApprovalJournal");
    if (values.length > 128) fail("current_pointer_journal");
    const ids: string[] = [];
    for (const rawValue of values) {
        const value = objectValue(rawValue, "privilege approval journal entry");
        exactKeys(value, ["expiresAt", "receiptId"], "privilege approval journal entry");
        const receiptId = matchText(value.receiptId, /^[0-9a-f]{64}$/, "receiptId");
        const expiresAt = text(value.expiresAt, "expiresAt");
        if (!/^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{3}Z$/.test(expiresAt) || !Number.isFinite(Date.parse(expiresAt))) {
            fail("current_pointer_journal");
        }
        ids.push(receiptId);
    }
    if (ids.some((item, index) => index > 0 && ids[index - 1] >= item) || new Set(ids).size !== ids.length) {
        fail("current_pointer_journal");
    }
}

function assessPrivilegeChange(
    oldEnvelope: RuntimePrivilegeEnvelope | null,
    newEnvelope: RuntimePrivilegeEnvelope,
): PrivilegeAssessment {
    const diff = {
        new: withoutEnvelopeFingerprint(newEnvelope),
        old: oldEnvelope === null ? null : withoutEnvelopeFingerprint(oldEnvelope),
    };
    return {
        automatic: oldEnvelope !== null && isSameOrNarrower(oldEnvelope, newEnvelope),
        oldFingerprint: oldEnvelope?.fingerprint ?? null,
        newFingerprint: newEnvelope.fingerprint,
        diffHash: privilegeFingerprint(diff),
    };
}

function isSameOrNarrower(oldEnvelope: RuntimePrivilegeEnvelope, newEnvelope: RuntimePrivilegeEnvelope): boolean {
    if (!isSubset(newEnvelope.allowedNetworkCategories, oldEnvelope.allowedNetworkCategories) ||
        !rootCapabilitiesNarrower(oldEnvelope.rootCapabilities, newEnvelope.rootCapabilities)) return false;
    const oldEnvironments = new Map(oldEnvelope.environmentProfiles.map((item) => [item.profileId, item]));
    for (const candidate of newEnvelope.environmentProfiles) {
        const current = oldEnvironments.get(candidate.profileId);
        if (!current || !isSubset(candidate.allowedNames, current.allowedNames) ||
            !isSubset(candidate.allowedSecretNames, current.allowedSecretNames)) return false;
    }
    const oldExecutables = new Map(oldEnvelope.executableProfiles.map((item) => [item.executableId, item]));
    for (const candidate of newEnvelope.executableProfiles) {
        const current = oldExecutables.get(candidate.executableId);
        if (!current || candidate.relativePath !== current.relativePath || candidate.trust !== current.trust ||
            !sameStrings(candidate.fixedArguments, current.fixedArguments) ||
            candidate.variableArgumentPattern !== current.variableArgumentPattern ||
            candidate.minimumVariableArguments < current.minimumVariableArguments ||
            candidate.maximumVariableArguments > current.maximumVariableArguments ||
            !isSubset(candidate.allowedCwdRootIds, current.allowedCwdRootIds) ||
            !isSubset(candidate.environmentProfileIds, current.environmentProfileIds) ||
            !isSubset(candidate.allowedStdinModes, current.allowedStdinModes) ||
            !rootCapabilitiesNarrower(current.appContainerFilesystem, candidate.appContainerFilesystem)) return false;
    }
    const oldProfiles = new Map(oldEnvelope.profilePrivileges.map((item) => [`${item.kind}\0${item.profileId}`, item]));
    for (const candidate of newEnvelope.profilePrivileges) {
        const current = oldProfiles.get(`${candidate.kind}\0${candidate.profileId}`);
        if (!current || candidate.executableId !== current.executableId || candidate.risk !== current.risk ||
            candidate.sideEffectClass !== current.sideEffectClass ||
            !sameStrings(candidate.fixedArguments, current.fixedArguments) ||
            candidate.variableArgumentPattern !== current.variableArgumentPattern ||
            candidate.minimumVariableArguments < current.minimumVariableArguments ||
            candidate.maximumVariableArguments > current.maximumVariableArguments ||
            !isSubset(candidate.allowedCwdRootIds, current.allowedCwdRootIds) ||
            !isSubset(candidate.environmentProfileIds, current.environmentProfileIds) ||
            !isSubset(candidate.allowedPlainEnvironmentNames, current.allowedPlainEnvironmentNames) ||
            !isSubset(candidate.allowedSecretEnvironmentNames, current.allowedSecretEnvironmentNames)) return false;
    }
    return newEnvelope.environmentProfiles.length <= oldEnvelope.environmentProfiles.length &&
        newEnvelope.executableProfiles.length <= oldEnvelope.executableProfiles.length &&
        newEnvelope.profilePrivileges.length <= oldEnvelope.profilePrivileges.length;
}

function rootCapabilitiesNarrower(
    oldCapabilities: ReadonlyArray<PrivilegeRootCapability>,
    newCapabilities: ReadonlyArray<PrivilegeRootCapability>,
): boolean {
    const current = new Map(oldCapabilities.map((item) => [`${item.rootId}\0${item.relativePath}`, item.access]));
    const rank: Readonly<Record<string, number>> = { read_only: 0, read_write: 1 };
    for (const candidate of newCapabilities) {
        const previous = current.get(`${candidate.rootId}\0${candidate.relativePath}`);
        if (previous === undefined || candidate.access === "cwd" || previous === "cwd") {
            if (candidate.access !== previous) return false;
        } else if (rank[candidate.access] > rank[previous]) return false;
    }
    return true;
}

function summarizePrivilegeChange(
    oldEnvelope: RuntimePrivilegeEnvelope | null,
    newEnvelope: RuntimePrivilegeEnvelope,
): string[] {
    const lines: string[] = [];
    const add = (line: string) => {
        if (lines.length < 11) lines.push(line.slice(0, 180));
    };
    if (oldEnvelope === null) add("首次安装：将启用以下本地 Runtime 权限。");
    const oldNetworks = oldEnvelope?.allowedNetworkCategories ?? [];
    const networkAdded = newEnvelope.allowedNetworkCategories.filter((item) => !oldNetworks.includes(item));
    if (networkAdded.length > 0) add(`网络类别：${networkAdded.join("、")}`);
    const oldRoots = new Map((oldEnvelope?.rootCapabilities ?? []).map((item) => [
        `${item.rootId}\0${item.relativePath}`, item.access,
    ]));
    for (const root of newEnvelope.rootCapabilities) {
        const previous = oldRoots.get(`${root.rootId}\0${root.relativePath}`);
        if (previous !== root.access) add(`目录能力：${root.rootId}/${root.relativePath}（${previous ?? "新增"} → ${root.access}）`);
    }
    const oldEnvironments = new Map((oldEnvelope?.environmentProfiles ?? []).map((item) => [item.profileId, item]));
    for (const profile of newEnvelope.environmentProfiles) {
        const previous = oldEnvironments.get(profile.profileId);
        const plain = profile.allowedNames.filter((item) => !previous?.allowedNames.includes(item));
        const secret = profile.allowedSecretNames.filter((item) => !previous?.allowedSecretNames.includes(item));
        if (plain.length > 0) add(`环境变量名 ${profile.profileId}：${plain.join("、")}`);
        if (secret.length > 0) add(`秘密变量名 ${profile.profileId}：${secret.join("、")}`);
    }
    const oldExecutables = new Set((oldEnvelope?.executableProfiles ?? []).map((item) => item.executableId));
    const addedExecutables = newEnvelope.executableProfiles.filter((item) => !oldExecutables.has(item.executableId));
    if (addedExecutables.length > 0) add(`新增本地可执行配置：${addedExecutables.map((item) => item.executableId).join("、")}`);
    const oldExecutableProfiles = new Map((oldEnvelope?.executableProfiles ?? []).map((item) => [item.executableId, item]));
    const changedExecutables = newEnvelope.executableProfiles.filter((item) => {
        const previous = oldExecutableProfiles.get(item.executableId);
        return previous !== undefined && previous.privilegeFingerprint !== item.privilegeFingerprint;
    });
    if (changedExecutables.length > 0) {
        add(`本地可执行权限变更：${changedExecutables.map((item) => item.executableId).join("、")}`);
    }
    const oldProfiles = new Set((oldEnvelope?.profilePrivileges ?? []).map((item) => `${item.kind}:${item.profileId}`));
    const addedProfiles = newEnvelope.profilePrivileges.filter((item) => !oldProfiles.has(`${item.kind}:${item.profileId}`));
    if (addedProfiles.length > 0) add(`新增工具配置：${addedProfiles.map((item) => `${item.kind}:${item.profileId}`).join("、")}`);
    const oldRuntimeProfiles = new Map((oldEnvelope?.profilePrivileges ?? []).map((item) => [
        `${item.kind}:${item.profileId}`, item,
    ]));
    const changedProfiles = newEnvelope.profilePrivileges.filter((item) => {
        const previous = oldRuntimeProfiles.get(`${item.kind}:${item.profileId}`);
        return previous !== undefined && previous.privilegeFingerprint !== item.privilegeFingerprint;
    });
    if (changedProfiles.length > 0) {
        add(`工具权限变更：${changedProfiles.map((item) => `${item.kind}:${item.profileId}`).join("、")}`);
    }
    if (lines.length === 0) {
        add("权限定义发生无法自动证明为收窄的变化。");
    }
    if (lines.length >= 11) lines.push("其余变化已省略；确认收据仍绑定完整签名差异。");
    return lines.slice(0, 12);
}

function createPrivilegeApprovalReceipt(
    oldManifestHash: string | null,
    newManifestHash: string,
    assessment: PrivilegeAssessment,
): RuntimePrivilegeApprovalReceipt {
    const issued = new Date();
    const expires = new Date(issued.getTime() + 5 * 60 * 1000);
    return {
        confirmation: RECEIPT_CONFIRMATION,
        diffHash: assessment.diffHash,
        expiresAt: expires.toISOString(),
        issuedAt: issued.toISOString(),
        newManifestHash,
        newPrivilegeFingerprint: assessment.newFingerprint,
        oldManifestHash,
        oldPrivilegeFingerprint: assessment.oldFingerprint,
        receiptId: randomBytes(32).toString("hex"),
        schemaVersion: 1,
    };
}

function isSubset(candidate: ReadonlyArray<string>, current: ReadonlyArray<string>): boolean {
    const values = new Set(current);
    return candidate.every((item) => values.has(item));
}

function sameStrings(left: ReadonlyArray<string>, right: ReadonlyArray<string>): boolean {
    return left.length === right.length && left.every((item, index) => item === right[index]);
}

async function verifyBootstrapIdentity(
    path: string,
    expected: RuntimeManifest["bootstrap"],
    runtimeArchitecture: RuntimeArchitecture,
): Promise<void> {
    const stats = await requireRegularFile(path);
    if (stats.size !== expected.byteLength) fail("bootstrap_size_mismatch");
    if (await sha256File(path) !== expected.sha256) fail("bootstrap_hash_mismatch");
    await verifyPeArchitecture(path, runtimeArchitecture);
}

async function verifyPeArchitecture(path: string, runtimeArchitecture: RuntimeArchitecture): Promise<void> {
    const stats = await requireRegularFile(path);
    const handle = await fs.open(path, "r");
    try {
        const header = Buffer.alloc(64);
        const { bytesRead } = await handle.read(header, 0, header.length, 0);
        if (bytesRead !== header.length || header.subarray(0, 2).toString("ascii") !== "MZ") {
            fail("bootstrap_pe_invalid");
        }
        const peOffset = header.readUInt32LE(60);
        if (peOffset < 64 || peOffset > stats.size - 6) fail("bootstrap_pe_invalid");
        const coff = Buffer.alloc(6);
        const result = await handle.read(coff, 0, coff.length, peOffset);
        if (result.bytesRead !== 6 || !coff.subarray(0, 4).equals(Buffer.from("PE\0\0")) ||
            coff.readUInt16LE(4) !== PE_MACHINE[runtimeArchitecture]) {
            fail("bootstrap_pe_architecture");
        }
    } finally {
        await handle.close();
    }
}

async function invokeSignedBootstrap(options: {
    readonly executable: string;
    readonly platformRoot: string;
    readonly manifest: RuntimeManifest;
    readonly manifestHash: string;
    readonly pluginVersion: string;
    readonly protocolVersion: string;
    readonly schemaHash: string;
    readonly vaultRoot: string;
    readonly ownerId: string;
    readonly legacyOwnerId: string | null;
    readonly privilegeApproval: RuntimePrivilegeApprovalReceipt | null;
    readonly timeoutMs: number;
    readonly signal: AbortSignal;
    readonly onPhase: (phase: InstallerPhase) => void;
    readonly spawnProcess: typeof spawn;
}): Promise<InstalledRuntime> {
    options.signal.throwIfAborted();
    const request = Buffer.from(canonicalJson({
        bundleRoot: options.platformRoot,
        manifestHash: options.manifestHash,
        legacyOwnerId: options.legacyOwnerId,
        ownerId: options.ownerId,
        pluginVersion: options.pluginVersion,
        privilegeApproval: options.privilegeApproval,
        protocolVersion: options.protocolVersion,
        schemaHash: options.schemaHash,
        schemaVersion: 3,
        vaultRoot: options.vaultRoot,
    }), "utf8");
    let child: ChildProcess;
    try {
        child = options.spawnProcess(options.executable, [
            "install", "--request-fd", "3", "--result-fd", "4",
        ], {
            windowsHide: true,
            detached: false,
            stdio: ["ignore", "ignore", "pipe", "pipe", "pipe"],
            env: minimalEnvironment(),
        });
    } catch {
        throw new RuntimeInstallVerificationError("bootstrap_start_failed", "signed Runtime bootstrap could not start");
    }
    const requestStream = child.stdio[3];
    const resultStream = child.stdio[4];
    if (!(requestStream instanceof Writable) || !(resultStream instanceof Readable)) {
        child.kill();
        throw new RuntimeInstallVerificationError("bootstrap_fd_contract", "signed bootstrap did not open fd3/fd4");
    }
    requestStream.end(request);
    child.stdio[2]?.resume();
    return new Promise<InstalledRuntime>((resolvePromise, rejectPromise) => {
        let size = 0;
        let buffer = Buffer.alloc(0);
        let result: BootstrapResult | null = null;
        let exited = false;
        let resultEnded = false;
        let exitCode: number | null = null;
        let settled = false;
        const seenPhases = new Set<InstallerPhase>();
        const failRequest = (error: Error) => {
            if (settled) return;
            settled = true;
            cleanup();
            child.kill();
            rejectPromise(error);
        };
        const maybeFinish = () => {
            if (settled || !exited || !resultEnded) return;
            if (exitCode !== 0 || result === null) {
                failRequest(new RuntimeInstallVerificationError("bootstrap_failed", "signed Runtime bootstrap rejected install"));
                return;
            }
            try {
                const runtime = validateBootstrapResult(result, options);
                options.onPhase("ready");
                settled = true;
                cleanup();
                resolvePromise(runtime);
            } catch (error) {
                failRequest(error instanceof Error ? error : new Error("invalid bootstrap result"));
            }
        };
        const onAbort = () => failRequest(abortError());
        const timer = setTimeout(() => failRequest(
            new RuntimeInstallVerificationError("bootstrap_timeout", "signed Runtime bootstrap timed out"),
        ), options.timeoutMs);
        const cleanup = () => {
            clearTimeout(timer);
            options.signal.removeEventListener("abort", onAbort);
        };
        options.signal.addEventListener("abort", onAbort, { once: true });
        resultStream.on("data", (chunk: Buffer) => {
            size += chunk.length;
            if (size > MAX_HELPER_RESULT_BYTES) {
                failRequest(new RuntimeInstallVerificationError("bootstrap_output_limit", "bootstrap result exceeded limit"));
                return;
            }
            buffer = Buffer.concat([buffer, chunk]);
            while (true) {
                const newline = buffer.indexOf(0x0a);
                if (newline < 0) break;
                const line = buffer.subarray(0, newline + 1);
                buffer = buffer.subarray(newline + 1);
                try {
                    const message = parseCanonicalLine(line);
                    if (result !== null) fail("bootstrap_message_after_result");
                    if (message.type === "progress") {
                        const progress = validateProgress(message);
                        if (seenPhases.has(progress.phase) || progress.phase === "ready") {
                            fail("bootstrap_progress_duplicate");
                        }
                        seenPhases.add(progress.phase);
                        options.onPhase(progress.phase);
                    } else if (message.type === "result" && result === null) {
                        result = message as unknown as BootstrapResult;
                    } else {
                        fail("bootstrap_message_invalid");
                    }
                } catch (error) {
                    failRequest(error instanceof Error ? error : new Error("invalid bootstrap message"));
                }
            }
        });
        resultStream.once("end", () => {
            if (buffer.length !== 0) {
                failRequest(new RuntimeInstallVerificationError("bootstrap_output_invalid", "partial result"));
                return;
            }
            resultEnded = true;
            maybeFinish();
        });
        resultStream.once("error", () => failRequest(
            new RuntimeInstallVerificationError("bootstrap_output_failed", "bootstrap result fd failed"),
        ));
        child.once("error", () => failRequest(
            new RuntimeInstallVerificationError("bootstrap_start_failed", "signed Runtime bootstrap failed"),
        ));
        child.once("exit", (code) => {
            exited = true;
            exitCode = code;
            maybeFinish();
        });
    });
}

function validateProgress(value: Record<string, unknown>): BootstrapProgress {
    exactKeys(value, ["phase", "type"], "bootstrap progress");
    if (value.type !== "progress" || typeof value.phase !== "string" ||
        !BOOTSTRAP_PHASES.has(value.phase as InstallerPhase)) fail("bootstrap_progress_invalid");
    return value as unknown as BootstrapProgress;
}

function validateBootstrapResult(
    result: BootstrapResult,
    expected: { readonly manifest: RuntimeManifest; readonly manifestHash: string },
): InstalledRuntime {
    const value = result as unknown as Record<string, unknown>;
    exactKeys(value, [
        "bootstrapAuthenticode", "hostExecutable", "manifestHash", "protocolMaximum", "protocolMinimum",
        "privilegeFingerprint", "runtimeVersion", "schemaHash", "status", "type",
    ], "bootstrap result");
    if (result.type !== "result" || result.status !== "ready" || result.bootstrapAuthenticode !== true ||
        result.runtimeVersion !== expected.manifest.runtimeVersion || result.manifestHash !== expected.manifestHash ||
        result.protocolMinimum !== expected.manifest.protocol.minimum ||
        result.protocolMaximum !== expected.manifest.protocol.maximum || result.schemaHash !== expected.manifest.protocol.schemaHash ||
        expected.manifest.privilegeEnvelope === null ||
        result.privilegeFingerprint !== expected.manifest.privilegeEnvelope.fingerprint ||
        !isAbsolute(result.hostExecutable) || !result.hostExecutable.toLowerCase().endsWith("offeragent-host.exe")) {
        fail("bootstrap_result_mismatch");
    }
    const localAppData = process.env.LOCALAPPDATA;
    if (!localAppData || !isWithin(resolve(localAppData, "OfferAgent", "runtime"), resolve(result.hostExecutable))) {
        fail("bootstrap_host_scope");
    }
    return {
        version: result.runtimeVersion,
        hostExecutable: resolve(result.hostExecutable),
        hostDiscoveryTimeoutMs: 10_000,
        protocolMinimum: result.protocolMinimum,
        protocolMaximum: result.protocolMaximum,
        schemaHash: result.schemaHash,
    };
}

function parseCanonicalLine(line: Buffer): Record<string, unknown> {
    let value: unknown;
    try {
        value = parseStrictJson(new TextDecoder("utf-8", { fatal: true }).decode(line), {
            maximumCharacters: MAX_HELPER_RESULT_BYTES,
        });
    } catch {
        fail("bootstrap_output_invalid");
    }
    const object = objectValue(value, "bootstrap output");
    if (Buffer.from(canonicalJson(object), "utf8").equals(line) === false) fail("bootstrap_output_noncanonical");
    return object;
}

function requireWindowsArchitecture(
    manifest: RuntimeManifest,
    runtimeArchitecture: RuntimeArchitecture,
): void {
    if (platform() !== "win32" || manifest.platform.os !== "windows" ||
        manifest.platform.architecture !== runtimeArchitecture) fail("platform_unsupported");
    const parts = release().split(".");
    const build = Number(parts[2]);
    if (!Number.isSafeInteger(build) || build < manifest.platform.minimumWindowsBuild) fail("windows_too_old");
}

async function readBounded(path: string, maximum: number): Promise<Buffer> {
    const stats = await requireRegularFile(path);
    if (stats.size < 1 || stats.size > maximum) fail("release_file_size");
    return fs.readFile(path);
}

async function requireRegularFile(path: string) {
    let stats;
    try {
        stats = await fs.lstat(path);
    } catch {
        fail("release_file_unavailable");
    }
    if (!stats.isFile() || stats.isSymbolicLink() || stats.nlink !== 1) fail("release_file_type");
    return stats;
}

async function listRegularTree(root: string): Promise<string[]> {
    const files: string[] = [];
    const visit = async (directory: string): Promise<void> => {
        for (const entry of await fs.readdir(directory, { withFileTypes: true })) {
            const target = confinedPath(root, resolve(directory, entry.name));
            const stats = await fs.lstat(target);
            if (stats.isSymbolicLink()) fail("payload_reparse_forbidden");
            if (stats.isDirectory()) {
                await visit(target);
            } else if (stats.isFile() && stats.nlink === 1) {
                files.push(relative(root, target).split(sep).join("/"));
            } else {
                fail("payload_file_type");
            }
        }
    };
    await visit(root);
    const folded = files.map((item) => item.toLowerCase());
    if (new Set(folded).size !== folded.length) fail("payload_path_collision");
    return files.sort();
}

async function sha256File(path: string): Promise<string> {
    const digest = createHash("sha256");
    await new Promise<void>((resolvePromise, rejectPromise) => {
        const stream = createReadStream(path);
        stream.on("data", (chunk) => digest.update(chunk));
        stream.once("end", resolvePromise);
        stream.once("error", rejectPromise);
    });
    return `sha256:${digest.digest("hex")}`;
}

function sha256(value: Buffer): string {
    return `sha256:${createHash("sha256").update(value).digest("hex")}`;
}

function safeArchiveName(value: string, topLevel: boolean): string {
    if (!ARCHIVE_PATH.test(value) || value.startsWith("/") || /^[A-Za-z]:/.test(value) ||
        value.split("/").some((part) => !part || part === "." || part === ".." || /[<>:"|?*\u0000-\u001f]/.test(part) ||
            /[. ]$/.test(part) || /^(?:CON|PRN|AUX|NUL|COM[1-9]|LPT[1-9])(?:\.|$)/i.test(part))) {
        fail("archive_path_invalid");
    }
    if (topLevel && value.includes("/")) fail("archive_path_invalid");
    return value;
}

function confinedPath(parent: string, child: string): string {
    const candidate = resolve(parent, child);
    if (!isWithin(parent, candidate)) fail("embedded_path_escape");
    return candidate;
}

function isWithin(parent: string, child: string): boolean {
    const relation = relative(resolve(parent), resolve(child));
    return relation !== "" && relation !== ".." && !relation.startsWith(`..${sep}`) && !isAbsolute(relation);
}

function canonicalJson(value: unknown): string {
    return `${JSON.stringify(sortJson(value))}\n`;
}

function sortJson(value: unknown): unknown {
    if (Array.isArray(value)) return value.map(sortJson);
    if (value !== null && typeof value === "object") {
        return Object.fromEntries(Object.keys(value as Record<string, unknown>).sort().map((key) => [
            key,
            sortJson((value as Record<string, unknown>)[key]),
        ]));
    }
    return value;
}

function objectValue(value: unknown, label: string): Record<string, unknown> {
    if (value === null || typeof value !== "object" || Array.isArray(value)) fail(`${label}_type`);
    return value as Record<string, unknown>;
}

function arrayValue(value: unknown, label: string): unknown[] {
    if (!Array.isArray(value)) fail(`${label}_type`);
    return value;
}

function exactKeys(value: Record<string, unknown>, keys: string[], label: string): void {
    const actual = Object.keys(value).sort();
    const expected = [...keys].sort();
    if (actual.length !== expected.length || actual.some((key, index) => key !== expected[index])) fail(`${label}_fields`);
}

function text(value: unknown, label: string): string {
    if (typeof value !== "string") fail(`${label}_type`);
    return value;
}

function matchText(value: unknown, pattern: RegExp, label: string): string {
    const result = text(value, label);
    if (!pattern.test(result)) fail(`${label}_invalid`);
    return result;
}

function version(value: unknown, label: string): string {
    return matchText(value, VERSION, label);
}

function keyId(value: unknown, label: string): string {
    return matchText(value, KEY_ID, label);
}

function protocolText(value: unknown, label: string): string {
    return matchText(value, PROTOCOL, label);
}

function hash(value: unknown, label: string): string {
    return matchText(value, SHA256, label);
}

function integer(value: unknown, label: string, minimum: number, maximum: number): number {
    if (!Number.isSafeInteger(value) || (value as number) < minimum || (value as number) > maximum) fail(`${label}_invalid`);
    return value as number;
}

function booleanValue(value: unknown, label: string): boolean {
    if (typeof value !== "boolean") fail(`${label}_type`);
    return value;
}

function requireTrue(value: unknown, label: string): true {
    if (value !== true) fail(`${label}_required`);
    return true;
}

function timestamp(value: unknown): string {
    const result = text(value, "createdAt");
    if (!/^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?Z$/.test(result) || !Number.isFinite(Date.parse(result))) {
        fail("createdAt_invalid");
    }
    return result;
}

function protocolTuple(value: string): [number, number] {
    const match = PROTOCOL.exec(value);
    if (!match) fail("protocol_invalid");
    return [Number(match[1]), Number(match[2])];
}

function compareTuple(left: [number, number], right: [number, number]): number {
    return left[0] - right[0] || left[1] - right[1];
}

function compareVersion(left: string, right: string): number {
    const a = left.split(/[.+_-]/);
    const b = right.split(/[.+_-]/);
    for (let index = 0; index < Math.max(a.length, b.length); index += 1) {
        if (index >= a.length) return -1;
        if (index >= b.length) return 1;
        const leftNumber = /^\d+$/.test(a[index]);
        const rightNumber = /^\d+$/.test(b[index]);
        if (leftNumber !== rightNumber) return leftNumber ? -1 : 1;
        const leftText = a[index].toLowerCase();
        const rightText = b[index].toLowerCase();
        const comparison = leftNumber ? Number(a[index]) - Number(b[index]) : leftText < rightText ? -1 : leftText > rightText ? 1 : 0;
        if (comparison !== 0) return comparison;
    }
    return 0;
}

function decodeBase64url(value: string): Buffer {
    const padding = "=".repeat((4 - value.length % 4) % 4);
    return Buffer.from(value.replace(/-/g, "+").replace(/_/g, "/") + padding, "base64");
}

function encodeBase64url(value: Buffer): string {
    return value.toString("base64").replace(/=/g, "").replace(/\+/g, "-").replace(/\//g, "_");
}

function minimalEnvironment(): NodeJS.ProcessEnv {
    return {
        LOCALAPPDATA: process.env.LOCALAPPDATA ?? "",
        SystemRoot: process.env.SystemRoot ?? "C:\\Windows",
        TEMP: process.env.TEMP ?? "",
        TMP: process.env.TMP ?? "",
    };
}

function positiveInteger(value: number, label: string): number {
    if (!Number.isSafeInteger(value) || value < 1) throw new RangeError(`${label} must be a positive integer`);
    return value;
}

function abortError(): Error {
    const error = new Error("operation aborted");
    error.name = "AbortError";
    return error;
}

function fail(code: string): never {
    throw new RuntimeInstallVerificationError(code.replace(/[^a-zA-Z0-9_.-]/g, "_"), "embedded Runtime verification failed");
}
