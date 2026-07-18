import { Buffer } from "node:buffer";
import { createHash } from "node:crypto";
import { lstat, readdir, readFile, realpath, stat } from "node:fs/promises";
import { extname, isAbsolute, join, relative, resolve, sep } from "node:path";

import type { TFile } from "obsidian";

import type { ExecutableToolCallDescriptor, ProjectSourceRef, ToolResultDescriptor } from "./generated_protocol";
import { failed, hasExtraKeys, succeeded } from "./plugin_tool_results";

const REGISTRY_PATH = "projects/index.md";
const MAX_CONTROL_BYTES = 32_768;
const MAX_PATH_LENGTH = 512;
const MAX_FILE_BYTES = 1_048_576;
const MAX_SCANNED_FILES = 2_000;
const MAX_SCAN_BYTES = 32 * 1_048_576;
const MAX_LIST_LIMIT = 100;
const MAX_SEARCH_LIMIT = 20;
const MAX_QUERY_BYTES = 512;
const MAX_READ_LINES = 200;
const MAX_READ_BYTES = 32_768;
const MAX_SNIPPET_BYTES = 512;

const ALLOWED_EXTENSIONS = new Set([
    ".c", ".cc", ".cjs", ".cpp", ".cs", ".css", ".go", ".graphql", ".h", ".hpp",
    ".html", ".ini", ".java", ".js", ".json", ".jsx", ".kt", ".kts", ".md", ".mjs",
    ".php", ".properties", ".proto", ".py", ".rb", ".rs", ".scala", ".scss", ".sh",
    ".sql", ".swift", ".toml", ".ts", ".tsx", ".txt", ".xml", ".yaml", ".yml",
]);
const EXCLUDED_SEGMENTS = new Set([
    ".git", ".hg", ".svn", ".cache", ".idea", ".vscode", ".venv", "__pycache__",
    "bin", "build", "coverage", "dist", "env", "gen", "generated", "node_modules", "obj",
    "out", "target", "vendor", "venv",
]);
const SECRET_SEGMENTS = new Set([".aws", ".azure", ".gnupg", ".ssh", "secrets"]);
const SECRET_FILE = /^(?:\.env(?:\..+)?|credentials?(?:\..+)?|id_(?:rsa|dsa|ecdsa|ed25519)(?:\.pub)?|secrets?(?:\..+)?|.*\.(?:key|pem|p12|pfx))$/iu;
const GENERATED_FILE = /(?:^(?:package-lock\.json|pnpm-lock\.yaml)$|\.generated\.|_generated\.|\.map$|\.min\.(?:css|js)$)/iu;

export interface ProjectRegistryVault {
    getFiles(): TFile[];
    cachedRead(file: TFile): Promise<string>;
}

interface RegisteredProject {
    readonly id: string;
    readonly root: string;
}

interface EligibleFile {
    readonly absolutePath: string;
    readonly path: string;
    readonly contentHash: string;
    readonly modifiedVersion: string;
}

interface ScanResult {
    readonly files: EligibleFile[];
    readonly truncated: boolean;
}

/** Bounded read-only access to roots explicitly authorized by the Vault Project Registry. */
export class ProjectEvidenceAdapter {
    constructor(private readonly vault: ProjectRegistryVault) {}

    async execute(call: ExecutableToolCallDescriptor): Promise<ToolResultDescriptor> {
        if (!["project.list", "project.search", "project.read"].includes(call.name) || call.version !== "1") {
            throw new Error(`unsupported Project evidence Tool: ${call.name}@${call.version}`);
        }
        const id = projectId(call.arguments.projectId);
        if (!id) return failed(call, "protocol.invalid_params", "Project ID is invalid.");
        const registration = await this.registeredProject(id, call);
        if ("result" in registration) return registration.result;
        if (call.name === "project.list") return this.list(call, registration.project);
        if (call.name === "project.search") return this.search(call, registration.project);
        return this.read(call, registration.project);
    }

    private async registeredProject(
        id: string,
        call: ExecutableToolCallDescriptor,
    ): Promise<{ project: RegisteredProject } | { result: ToolResultDescriptor }> {
        const files = this.vault.getFiles();
        const registry = files.find((candidate) => candidate.path === REGISTRY_PATH);
        if (registry === undefined) {
            return { result: failed(call, "policy.denied", `Project '${id}' is not registered.`) };
        }
        const index = await this.boundedVaultRead(registry);
        if (index === undefined) {
            return { result: failed(call, "tool.failed", "Project Registry is unreadable.") };
        }
        const linkedDescriptors = new Set(
            [...index.matchAll(/\[\[([^\]|#]+)(?:#[^\]|]+)?(?:\|[^\]]+)?\]\]/gu)]
                .map((match) => match[1]?.trim())
                .filter((value): value is string => Boolean(value))
                .map((value) => value.endsWith(".md") ? value : `${value}.md`)
                .filter((value) => /^projects\/[^/.][^/]*\.md$/u.test(value) && value !== REGISTRY_PATH),
        );
        let registered: RegisteredProject | undefined;
        for (const descriptorPath of linkedDescriptors) {
            const descriptor = files.find((candidate) => candidate.path === descriptorPath);
            if (descriptor === undefined) continue;
            const content = await this.boundedVaultRead(descriptor);
            if (content === undefined || frontmatterValue(content, "project-id") !== id) continue;
            const configuredRoot = frontmatterValue(content, "project-root");
            if (!configuredRoot || !isAbsolute(configuredRoot)) {
                return { result: failed(call, "tool.failed", "Registered project root is invalid.") };
            }
            try {
                const root = await realpath(configuredRoot);
                if (!(await stat(root)).isDirectory()) throw new Error("not a directory");
                if (registered !== undefined) {
                    return { result: failed(call, "tool.failed", `Project '${id}' has conflicting Registry entries.`) };
                }
                registered = { id, root };
            } catch {
                return { result: failed(call, "resource.not_found", `Registered project '${id}' is unavailable.`) };
            }
        }
        return registered === undefined
            ? { result: failed(call, "policy.denied", `Project '${id}' is not registered.`) }
            : { project: registered };
    }

    private async boundedVaultRead(file: TFile): Promise<string | undefined> {
        if (file.stat.size > MAX_CONTROL_BYTES) return undefined;
        try {
            const content = await this.vault.cachedRead(file);
            return Buffer.byteLength(content, "utf8") <= MAX_CONTROL_BYTES && !content.includes("\0")
                ? content
                : undefined;
        } catch {
            return undefined;
        }
    }

    private async list(call: ExecutableToolCallDescriptor, project: RegisteredProject): Promise<ToolResultDescriptor> {
        const input = call.arguments;
        const directory = safeRelativePath(input.directory, true);
        const limit = boundedInteger(input.limit, 50, MAX_LIST_LIMIT);
        if (directory === undefined || limit === undefined || hasExtraKeys(input, ["projectId", "directory", "limit"])) {
            return failed(call, "protocol.invalid_params", "Project list bounds are invalid.");
        }
        if (!eligibleDirectory(directory)) return failed(call, "policy.denied", "Project directory is excluded.");
        try {
            const scanned = await this.scan(project, directory);
            return succeeded(call, "Listed bounded registered Project evidence.", {
                projectId: project.id,
                entries: scanned.files.slice(0, limit).map(({ absolutePath: _absolutePath, ...entry }) => entry),
                truncated: scanned.truncated || scanned.files.length > limit,
            });
        } catch {
            return failed(call, "tool.failed", "Registered Project sources could not be listed.", true);
        }
    }

    private async search(call: ExecutableToolCallDescriptor, project: RegisteredProject): Promise<ToolResultDescriptor> {
        const input = call.arguments;
        const query = typeof input.query === "string" ? input.query.trim().toLocaleLowerCase() : "";
        const limit = boundedInteger(input.limit, 10, MAX_SEARCH_LIMIT);
        const snippetMaxBytes = boundedInteger(input.snippetMaxBytes, 240, MAX_SNIPPET_BYTES, 16);
        if (!query || Buffer.byteLength(query, "utf8") > MAX_QUERY_BYTES || limit === undefined ||
            snippetMaxBytes === undefined || hasExtraKeys(input, ["projectId", "query", "limit", "snippetMaxBytes"])) {
            return failed(call, "protocol.invalid_params", "Project search bounds are invalid.");
        }
        try {
            const scanned = await this.scan(project, "");
            const terms = query.split(/\s+/u).filter(Boolean);
            const entries: Array<Record<string, unknown>> = [];
            for (const file of scanned.files) {
                if (entries.length >= limit) break;
                const snapshot = await stableExternalRead(file.absolutePath, MAX_FILE_BYTES);
                if (snapshot === undefined || snapshot.modifiedVersion !== file.modifiedVersion ||
                    snapshot.contentHash !== file.contentHash) continue;
                const lines = snapshot.content.replace(/\r\n/g, "\n").split("\n");
                const lineIndex = lines.findIndex((line) => {
                    const normalized = line.toLocaleLowerCase();
                    return terms.every((term) => normalized.includes(term));
                });
                if (lineIndex < 0) continue;
                const snippet = truncateUtf8(lines[lineIndex] ?? "", snippetMaxBytes);
                entries.push({
                    path: file.path,
                    modifiedVersion: file.modifiedVersion,
                    contentHash: file.contentHash,
                    snippets: [{ content: snippet.content, lineStart: lineIndex + 1, lineEnd: lineIndex + 1, truncated: snippet.truncated }],
                });
            }
            return succeeded(call, "Located registered Project candidates; use project.read before answering.", {
                projectId: project.id,
                entries,
                truncated: scanned.truncated || entries.length >= limit,
            });
        } catch {
            return failed(call, "tool.failed", "Registered Project sources could not be searched.", true);
        }
    }

    private async read(call: ExecutableToolCallDescriptor, project: RegisteredProject): Promise<ToolResultDescriptor> {
        const input = call.arguments;
        const requestedPath = safeRelativePath(input.path);
        const lineStart = boundedInteger(input.lineStart, 1, Number.MAX_SAFE_INTEGER);
        const requestedEnd = boundedInteger(input.lineEnd, Number.MAX_SAFE_INTEGER, Number.MAX_SAFE_INTEGER);
        const expectedHash = optionalDigest(input.expectedContentHash);
        const expectedVersion = optionalText(input.expectedModifiedVersion, 128);
        if (!requestedPath || lineStart === undefined || requestedEnd === undefined || requestedEnd < lineStart ||
            expectedHash === null || expectedVersion === null || hasExtraKeys(input, [
                "projectId", "path", "lineStart", "lineEnd", "expectedContentHash", "expectedModifiedVersion",
            ])) {
            return failed(call, "protocol.invalid_params", "Project read path, range, or version is invalid.");
        }
        if (!eligiblePath(requestedPath)) return failed(call, "policy.denied", "Project source is excluded.");
        const absolutePath = resolve(project.root, ...requestedPath.split("/"));
        if (!contained(project.root, absolutePath)) return failed(call, "protocol.invalid_params", "Project path escapes its root.");
        let canonical: string;
        try {
            canonical = await realpath(absolutePath);
            const canonicalRelative = relative(project.root, canonical).split(sep).join("/");
            if (!contained(project.root, canonical) || !sameIdentity(requestedPath, canonicalRelative) || !eligiblePath(canonicalRelative)) {
                return failed(call, "policy.denied", "Project source aliases or escapes are not readable.");
            }
        } catch {
            return failed(call, "resource.not_found", "Project source was not found.");
        }
        const snapshot = await stableExternalRead(canonical, MAX_FILE_BYTES);
        if (snapshot === undefined) return failed(call, "resource.conflict", "Project source changed or is unreadable.", true);
        if ((expectedHash !== undefined && expectedHash !== snapshot.contentHash) ||
            (expectedVersion !== undefined && expectedVersion !== snapshot.modifiedVersion)) {
            return failed(call, "resource.conflict", "Project evidence no longer matches the selected version.", true);
        }
        const lines = snapshot.content.replace(/\r\n/g, "\n").split("\n");
        if (lineStart > lines.length) return failed(call, "resource.not_found", "Project evidence line is missing.");
        const boundedEnd = Math.min(requestedEnd, lineStart + MAX_READ_LINES - 1, lines.length);
        const selected = truncateUtf8(lines.slice(lineStart - 1, boundedEnd).join("\n"), MAX_READ_BYTES);
        const returnedLines = selected.content ? selected.content.split("\n").length : 1;
        const lineEnd = lineStart + returnedLines - 1;
        const source: ProjectSourceRef = {
            type: "project",
            projectId: project.id,
            path: requestedPath,
            contentHash: snapshot.contentHash,
            modifiedVersion: snapshot.modifiedVersion,
            lineStart,
            lineEnd,
            freshness: "fresh",
        };
        return succeeded(call, "Read precise current registered Project evidence.", {
            projectId: project.id,
            path: requestedPath,
            lineStart,
            lineEnd,
            modifiedVersion: snapshot.modifiedVersion,
            contentHash: snapshot.contentHash,
            content: selected.content,
            truncated: selected.truncated || boundedEnd < lines.length,
        }, [source]);
    }

    private async scan(project: RegisteredProject, directory: string): Promise<ScanResult> {
        const start = resolve(project.root, ...directory.split("/").filter(Boolean));
        if (!contained(project.root, start)) throw new Error("directory escape");
        const canonical = await realpath(start);
        const canonicalDirectory = relative(project.root, canonical).split(sep).join("/");
        if (!contained(project.root, canonical) || !sameIdentity(directory, canonicalDirectory) ||
            !eligibleDirectory(canonicalDirectory) || !(await stat(canonical)).isDirectory()) {
            throw new Error("directory alias or exclusion");
        }
        const files: EligibleFile[] = [];
        const pending = [start];
        let visited = 0;
        let scannedBytes = 0;
        while (pending.length > 0 && visited < MAX_SCANNED_FILES && scannedBytes < MAX_SCAN_BYTES) {
            const current = pending.shift();
            if (current === undefined) break;
            const entries = (await readdir(current, { withFileTypes: true }))
                .sort((left, right) => left.name.localeCompare(right.name));
            for (const entry of entries) {
                if (++visited > MAX_SCANNED_FILES) break;
                const absolutePath = join(current, entry.name);
                const relativePath = relative(project.root, absolutePath).split(sep).join("/");
                if (entry.isSymbolicLink()) continue;
                if (entry.isDirectory()) {
                    if (eligibleDirectory(relativePath)) pending.push(absolutePath);
                    continue;
                }
                if (!entry.isFile() || !eligiblePath(relativePath)) continue;
                const metadata = await lstat(absolutePath);
                if (metadata.size > MAX_FILE_BYTES) continue;
                if (scannedBytes + metadata.size > MAX_SCAN_BYTES) {
                    scannedBytes = MAX_SCAN_BYTES;
                    break;
                }
                const snapshot = await stableExternalRead(absolutePath, MAX_FILE_BYTES);
                if (snapshot === undefined) continue;
                scannedBytes += Buffer.byteLength(snapshot.content, "utf8");
                files.push({ absolutePath, path: relativePath, contentHash: snapshot.contentHash, modifiedVersion: snapshot.modifiedVersion });
            }
        }
        files.sort((left, right) => left.path.localeCompare(right.path));
        return {
            files,
            truncated: pending.length > 0 || visited >= MAX_SCANNED_FILES || scannedBytes >= MAX_SCAN_BYTES,
        };
    }
}

async function stableExternalRead(path: string, maximumBytes: number): Promise<{
    content: string;
    contentHash: string;
    modifiedVersion: string;
} | undefined> {
    try {
        const before = await lstat(path);
        if (!before.isFile() || before.nlink !== 1 || before.size > maximumBytes) return undefined;
        const buffer = await readFile(path);
        const after = await lstat(path);
        if (!after.isFile() || after.nlink !== 1 || before.dev !== after.dev || before.ino !== after.ino ||
            before.size !== after.size || before.mtimeMs !== after.mtimeMs || buffer.byteLength > maximumBytes) return undefined;
        let content: string;
        try { content = new TextDecoder("utf-8", { fatal: true }).decode(buffer); } catch { return undefined; }
        if (content.includes("\0")) return undefined;
        return { content, contentHash: digest(buffer), modifiedVersion: version(after) };
    } catch {
        return undefined;
    }
}

function projectId(value: unknown): string | undefined {
    return typeof value === "string" && /^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$/u.test(value) ? value : undefined;
}

function safeRelativePath(value: unknown, allowEmpty = false): string | undefined {
    if (value === undefined && allowEmpty) return "";
    if (typeof value !== "string") return undefined;
    const candidate = value.trim();
    if (allowEmpty && !candidate) return "";
    if (!candidate || candidate.length > MAX_PATH_LENGTH || candidate.includes("\\") ||
        candidate.startsWith("/") || isAbsolute(candidate) || /^[A-Za-z]:/u.test(candidate)) return undefined;
    const segments = candidate.split("/");
    return segments.some((segment) => !segment || segment === "." || segment === "..") ? undefined : segments.join("/");
}

function eligibleDirectory(path: string): boolean {
    if (!path) return true;
    return path.split("/").map((segment) => segment.toLocaleLowerCase())
        .every((segment) => !segment.startsWith(".") && !EXCLUDED_SEGMENTS.has(segment) && !SECRET_SEGMENTS.has(segment));
}

function eligiblePath(path: string): boolean {
    const segments = path.split("/");
    const name = segments[segments.length - 1]?.toLocaleLowerCase() ?? "";
    return eligibleDirectory(segments.slice(0, -1).join("/")) && !name.startsWith(".") &&
        !SECRET_FILE.test(name) && !GENERATED_FILE.test(name) && ALLOWED_EXTENSIONS.has(extname(name).toLocaleLowerCase());
}

function contained(root: string, target: string): boolean {
    const candidate = relative(root, target);
    return candidate === "" || (!isAbsolute(candidate) && candidate !== ".." && !candidate.startsWith(`..${sep}`));
}

function sameIdentity(requested: string, canonical: string): boolean {
    return process.platform === "win32" ? requested.toLocaleLowerCase() === canonical.toLocaleLowerCase() : requested === canonical;
}

function frontmatterValue(content: string, key: string): string | undefined {
    const normalized = content.replace(/\r\n/g, "\n");
    if (!normalized.startsWith("---\n")) return undefined;
    const end = normalized.indexOf("\n---", 4);
    if (end < 0) return undefined;
    for (const line of normalized.slice(4, end).split("\n")) {
        const separator = line.indexOf(":");
        if (separator < 0 || line.slice(0, separator).trim() !== key) continue;
        const raw = line.slice(separator + 1).trim();
        const value = raw.replace(/^(?:"(.*)"|'(.*)')$/u, "$1$2");
        return value || undefined;
    }
    return undefined;
}

function boundedInteger(value: unknown, fallback: number, maximum: number, minimum = 1): number | undefined {
    if (value === undefined) return fallback;
    return Number.isSafeInteger(value) && Number(value) >= minimum && Number(value) <= maximum ? Number(value) : undefined;
}

function optionalDigest(value: unknown): string | undefined | null {
    if (value === undefined) return undefined;
    return typeof value === "string" && /^sha256:[0-9a-f]{64}$/u.test(value) ? value : null;
}

function optionalText(value: unknown, maximum: number): string | undefined | null {
    if (value === undefined) return undefined;
    return typeof value === "string" && value.length > 0 && value.length <= maximum ? value : null;
}

function digest(content: Buffer | string): string {
    return `sha256:${createHash("sha256").update(content).digest("hex")}`;
}

function version(metadata: { readonly mtimeMs: number; readonly size: number }): string {
    return `mtime:${Math.trunc(metadata.mtimeMs)}:size:${metadata.size}`;
}

function truncateUtf8(value: string, maximumBytes: number): { content: string; truncated: boolean } {
    if (Buffer.byteLength(value, "utf8") <= maximumBytes) return { content: value, truncated: false };
    let end = Math.min(value.length, maximumBytes);
    while (end > 0 && Buffer.byteLength(value.slice(0, end), "utf8") > maximumBytes) end -= 1;
    if (end > 0 && /[\uD800-\uDBFF]/u.test(value[end - 1] ?? "")) end -= 1;
    return { content: value.slice(0, end), truncated: true };
}
