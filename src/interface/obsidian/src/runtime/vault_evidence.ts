import { createHash } from "node:crypto";
import { Buffer } from "node:buffer";

import type { CachedMetadata, TFile } from "obsidian";

import type { ExecutableToolCallDescriptor, ToolResultDescriptor } from "./generated_protocol";

const MAX_LIST_RESULTS = 100;
const MAX_SEARCH_RESULTS = 20;
const MAX_SEARCH_FILES = 1_000;
const MAX_SEARCH_BYTES = 16 * 1_048_576;
const MAX_SOURCE_BYTES = 1_048_576;
const MAX_READ_LINES = 200;
const MAX_READ_BYTES = 32_768;
const MAX_QUERY_BYTES = 512;
const MAX_SNIPPETS = 3;
const MAX_SNIPPET_BYTES = 512;
const EXCLUDED_SEGMENTS = new Set([".git", ".obsidian", ".codex", "node_modules"]);

export interface VaultEvidencePort {
    getFiles(): TFile[];
    getFileByPath(path: string): TFile | null;
    cachedRead(file: TFile): Promise<string>;
}

export interface MetadataReadPort {
    getFileCache(file: TFile): CachedMetadata | null;
}

export class VaultEvidenceAdapter {
    constructor(
        private readonly vault: VaultEvidencePort,
        private readonly workspaceId: string,
        private readonly metadata?: MetadataReadPort,
    ) {}

    async execute(call: ExecutableToolCallDescriptor): Promise<ToolResultDescriptor> {
        if (call.name === "vault.list") return this.list(call);
        if (call.name === "vault.search") return this.search(call);
        if (call.name === "vault.read") return this.read(call);
        throw new Error(`unsupported Vault evidence Tool: ${call.name}@${call.version}`);
    }

    private async list(call: ExecutableToolCallDescriptor): Promise<ToolResultDescriptor> {
        const input = call.arguments;
        const directory = optionalDirectory(input.directory);
        const limit = boundedInteger(input.limit, 50, MAX_LIST_RESULTS);
        if (directory === undefined || limit === undefined || hasExtraKeys(input, ["directory", "limit"])) {
            return failed(call, "protocol.invalid_params", "Vault list bounds are invalid.");
        }
        const candidates = this.files(directory);
        const entries: Array<Record<string, string | number>> = [];
        for (const file of candidates.slice(0, limit)) {
            const snapshot = await this.snapshot(file);
            if (snapshot !== null) entries.push(snapshot);
        }
        return succeeded(call, "Listed current Vault files.", {
            entries,
            truncated: candidates.length > limit,
        });
    }

    private async search(call: ExecutableToolCallDescriptor): Promise<ToolResultDescriptor> {
        const input = call.arguments;
        const query = typeof input.query === "string" ? input.query.trim().toLocaleLowerCase() : "";
        const limit = boundedInteger(input.limit, 10, MAX_SEARCH_RESULTS);
        const snippetsPerFile = boundedInteger(input.snippetsPerFile, 2, MAX_SNIPPETS);
        const snippetMaxBytes = boundedInteger(input.snippetMaxBytes, 240, MAX_SNIPPET_BYTES, 16);
        if (!query || Buffer.byteLength(query, "utf8") > MAX_QUERY_BYTES || limit === undefined ||
            snippetsPerFile === undefined || snippetMaxBytes === undefined ||
            hasExtraKeys(input, ["query", "limit", "snippetsPerFile", "snippetMaxBytes"])) {
            return failed(call, "protocol.invalid_params", "Vault search bounds are invalid.");
        }
        const terms = query.split(/\s+/u).filter(Boolean);
        const matches: Array<{ rank: number; score: number; entry: Record<string, unknown> }> = [];
        let scannedBytes = 0;
        let scannedFiles = 0;
        let truncated = false;
        for (const file of this.files("")) {
            if (++scannedFiles > MAX_SEARCH_FILES || scannedBytes + file.stat.size > MAX_SEARCH_BYTES) {
                truncated = true;
                break;
            }
            const snapshot = await this.readStable(file);
            if (snapshot === null) continue;
            scannedBytes += Buffer.byteLength(snapshot.content, "utf8");
            const normalizedPath = file.path.toLocaleLowerCase();
            const metadata = metadataText(this.metadata?.getFileCache(file)).toLocaleLowerCase();
            const body = snapshot.content.toLocaleLowerCase();
            const containsAll = (value: string) => terms.every((term) => value.includes(term));
            const rank = containsAll(normalizedPath) ? 0 : containsAll(metadata) ? 1 : containsAll(body) ? 2 : -1;
            if (rank < 0) continue;
            const lines = snapshot.content.replace(/\r\n/g, "\n").split("\n");
            const snippets = lines
                .map((line, index) => ({ line, normalized: line.toLocaleLowerCase(), lineNumber: index + 1 }))
                .filter(({ normalized }) => containsAll(normalized))
                .slice(0, snippetsPerFile)
                .map(({ line, lineNumber }) => ({
                    content: truncateUtf8(line, snippetMaxBytes),
                    lineStart: lineNumber,
                    lineEnd: lineNumber,
                }));
            const rankedText = rank === 0 ? normalizedPath : rank === 1 ? metadata : body;
            matches.push({
                rank,
                score: terms.reduce((total, term) => total + occurrences(rankedText, term), 0),
                entry: {
                    path: file.path,
                    modifiedVersion: snapshot.modifiedVersion,
                    contentHash: snapshot.contentHash,
                    matchTier: rank === 0 ? "path" : rank === 1 ? "metadata" : "body",
                    snippets,
                },
            });
        }
        matches.sort((left, right) => left.rank - right.rank || right.score - left.score ||
            String(left.entry.path).localeCompare(String(right.entry.path)));
        return succeeded(call, "Searched current Vault files; use vault.read before answering.", {
            entries: matches.slice(0, limit).map(({ entry }) => entry),
            truncated: truncated || matches.length > limit,
        });
    }

    private async read(call: ExecutableToolCallDescriptor): Promise<ToolResultDescriptor> {
        const input = call.arguments;
        const path = safeFilePath(input.path);
        const lineStart = boundedInteger(input.lineStart, 1, Number.MAX_SAFE_INTEGER);
        const requestedEnd = boundedInteger(input.lineEnd, Number.MAX_SAFE_INTEGER, Number.MAX_SAFE_INTEGER);
        const expectedHash = optionalDigest(input.expectedContentHash);
        const expectedVersion = optionalText(input.expectedModifiedVersion, 128);
        if (!path || lineStart === undefined || requestedEnd === undefined || requestedEnd < lineStart ||
            expectedHash === null || expectedVersion === null ||
            hasExtraKeys(input, ["path", "lineStart", "lineEnd", "expectedContentHash", "expectedModifiedVersion"])) {
            return failed(call, "protocol.invalid_params", "Vault read path, version, or line range is invalid.");
        }
        const file = this.vault.getFileByPath(path);
        if (file === null || !readable(file) || !eligible(file.path)) {
            return failed(call, "resource.not_found", "Vault evidence is missing or excluded.");
        }
        if (file.stat.size > MAX_SOURCE_BYTES) {
            return failed(call, "protocol.message_too_large", "Vault evidence exceeds the bounded source limit.");
        }
        const snapshot = await this.readStable(file);
        if (snapshot === null) return failed(call, "resource.conflict", "Vault evidence changed during read.", true);
        if ((expectedHash !== undefined && expectedHash !== snapshot.contentHash) ||
            (expectedVersion !== undefined && expectedVersion !== snapshot.modifiedVersion)) {
            return failed(call, "resource.conflict", "Vault evidence no longer matches the selected version.", true);
        }
        const lines = snapshot.content.replace(/\r\n/g, "\n").split("\n");
        if (lineStart > lines.length) return failed(call, "resource.not_found", "Vault evidence line is missing.");
        const lineEnd = Math.min(requestedEnd, lineStart + MAX_READ_LINES - 1, lines.length);
        const content = lines.slice(lineStart - 1, lineEnd).join("\n");
        if (Buffer.byteLength(content, "utf8") > MAX_READ_BYTES) {
            return failed(call, "protocol.message_too_large", "Vault evidence selection exceeds the read limit.");
        }
        return succeeded(call, "Read precise current Vault evidence.", {
            path,
            lineStart,
            lineEnd,
            modifiedVersion: snapshot.modifiedVersion,
            contentHash: snapshot.contentHash,
            content,
            truncated: lineEnd < lines.length,
        }, [{
            type: "vault",
            file: {
                workspaceId: this.workspaceId,
                path,
                contentHash: snapshot.contentHash,
                lineStart,
                lineEnd,
            },
            freshness: "fresh",
        }]);
    }

    private files(directory: string): TFile[] {
        const prefix = directory ? `${directory}/` : "";
        return this.vault.getFiles()
            .filter((file) => readable(file) && eligible(file.path) && (!prefix || file.path.startsWith(prefix)))
            .sort((left, right) => left.path.localeCompare(right.path));
    }

    private async snapshot(file: TFile): Promise<Record<string, string | number> | null> {
        const value = await this.readStable(file);
        if (value === null) return null;
        return { path: file.path, modifiedVersion: value.modifiedVersion, contentHash: value.contentHash, sizeBytes: file.stat.size };
    }

    private async readStable(file: TFile): Promise<{ content: string; contentHash: string; modifiedVersion: string } | null> {
        if (file.stat.size > MAX_SOURCE_BYTES) return null;
        const before = version(file);
        try {
            const content = await this.vault.cachedRead(file);
            const after = this.vault.getFileByPath(file.path);
            if (after === null || version(after) !== before || Buffer.byteLength(content, "utf8") > MAX_SOURCE_BYTES) return null;
            return { content, contentHash: hash(content), modifiedVersion: before };
        } catch {
            return null;
        }
    }
}

function succeeded(
    call: ExecutableToolCallDescriptor,
    summary: string,
    data: Record<string, unknown>,
    sourceRefs: ToolResultDescriptor["sourceRefs"] = [],
): ToolResultDescriptor {
    return { toolCallId: call.toolCallId, status: "succeeded", summary, data, sourceRefs, retryable: false };
}

function failed(
    call: ExecutableToolCallDescriptor,
    code: "protocol.invalid_params" | "protocol.message_too_large" | "resource.not_found" | "resource.conflict",
    message: string,
    retryable = false,
): ToolResultDescriptor {
    return {
        toolCallId: call.toolCallId,
        status: "failed",
        summary: message,
        data: {},
        retryable,
        error: { code, retryable, cancelled: false, userVisibleMessage: message, details: {} },
    };
}

function safeFilePath(value: unknown): string | undefined {
    if (typeof value !== "string") return undefined;
    const path = value.trim();
    if (!path || path.length > 512 || path.includes("\\") || path.startsWith("/") || /^[A-Za-z]:/u.test(path)) return undefined;
    return eligible(path) ? path : undefined;
}

function optionalDirectory(value: unknown): string | undefined {
    if (value === undefined || value === "") return "";
    const path = safeFilePath(`${value}/placeholder.md`);
    return path ? path.slice(0, -"/placeholder.md".length) : undefined;
}

function eligible(path: string): boolean {
    if (path.toLocaleLowerCase() === "agent.md") return false;
    const segments = path.split("/");
    return segments.every((segment) => segment && segment !== "." && segment !== ".." &&
        !segment.startsWith(".") && !EXCLUDED_SEGMENTS.has(segment.toLocaleLowerCase()));
}

function readable(file: TFile): boolean {
    return file.extension.toLocaleLowerCase() === "md" || file.extension.toLocaleLowerCase() === "txt";
}

function version(file: TFile): string {
    return `mtime:${file.stat.mtime}:size:${file.stat.size}`;
}

function hash(content: string): string {
    return `sha256:${createHash("sha256").update(content, "utf8").digest("hex")}`;
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

function hasExtraKeys(value: Readonly<Record<string, unknown>>, allowed: readonly string[]): boolean {
    return Object.keys(value).some((key) => !allowed.includes(key));
}

function metadataText(cache: CachedMetadata | null | undefined): string {
    const values = (value: unknown): string[] => {
        if (["string", "number", "boolean"].includes(typeof value)) return [String(value)];
        if (Array.isArray(value)) return value.flatMap(values);
        if (value && typeof value === "object") return Object.values(value).flatMap(values);
        return [];
    };
    return [
        ...values(cache?.frontmatter),
        ...(cache?.headings?.map((heading) => heading.heading) ?? []),
        ...(cache?.tags?.map((tag) => tag.tag) ?? []),
    ].join("\n");
}

function occurrences(value: string, needle: string): number {
    let count = 0;
    let offset = 0;
    while ((offset = value.indexOf(needle, offset)) >= 0) {
        count += 1;
        offset += Math.max(1, needle.length);
    }
    return count;
}

function truncateUtf8(value: string, maximum: number): string {
    if (Buffer.byteLength(value, "utf8") <= maximum) return value;
    let end = Math.min(value.length, maximum);
    while (end > 0 && Buffer.byteLength(value.slice(0, end), "utf8") > maximum) end -= 1;
    return value.slice(0, end);
}
