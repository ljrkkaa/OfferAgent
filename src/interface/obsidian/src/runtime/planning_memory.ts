import { Buffer } from "node:buffer";
import { createHash } from "node:crypto";

import type { TFile } from "obsidian";

import type { ExecutableToolCallDescriptor, SourceRef, ToolResultDescriptor } from "./generated_protocol";

const TOPIC_PATH = /^memory\/(user|feedback|project|study)\/[^/.][^/]*\.md$/u;
const CONTENT_HASH = /^sha256:[0-9a-f]{64}$/u;
const MAX_TOPIC_BYTES = 32_768;
const MAX_READ_BYTES = 65_536;
const MAX_LIST_TOPICS = 100;
const MAX_SCANNED_TOPICS = 1_000;

export interface PlanningMemoryVaultPort {
    getFiles(): TFile[];
    getFileByPath(path: string): TFile | null;
    cachedRead(file: TFile): Promise<string>;
}

interface TopicMetadata {
    readonly path: string;
    readonly type: "user" | "feedback" | "project" | "study";
    readonly name: string;
    readonly description: string;
    readonly modifiedVersion: string;
    readonly contentHash: string;
}

/** Bounded metadata discovery and exact reads for user-visible Planning Memory. */
export class PlanningMemoryAdapter {
    constructor(
        private readonly vault: PlanningMemoryVaultPort,
        private readonly workspaceId: string,
    ) {}

    async execute(call: ExecutableToolCallDescriptor): Promise<ToolResultDescriptor> {
        if (call.name === "planning_memory.list") return this.list(call);
        if (call.name === "planning_memory.read") return this.read(call);
        throw new Error(`unsupported Planning Memory Tool: ${call.name}@${call.version}`);
    }

    private async list(call: ExecutableToolCallDescriptor): Promise<ToolResultDescriptor> {
        if (Object.keys(call.arguments).length !== 0) {
            return failed(call, "protocol.invalid_params", "Planning Memory list accepts no arguments.");
        }
        try {
            const candidates = this.vault.getFiles()
                .filter((file) => file.extension.toLocaleLowerCase() === "md" && TOPIC_PATH.test(file.path))
                .sort((left, right) => left.path.localeCompare(right.path));
            const topics: TopicMetadata[] = [];
            let scanned = 0;
            let scanTruncated = false;
            for (const file of candidates) {
                if (scanned >= MAX_SCANNED_TOPICS || topics.length > MAX_LIST_TOPICS) {
                    scanTruncated = true;
                    break;
                }
                scanned += 1;
                const snapshot = await stableRead(this.vault, file);
                const metadata = topicMetadata(file.path, snapshot);
                if (metadata !== null) topics.push(metadata);
            }
            const truncated = topics.length > MAX_LIST_TOPICS || scanTruncated;
            return succeeded(call, "Listed Planning Memory topic metadata without topic bodies.", {
                topics: topics.slice(0, MAX_LIST_TOPICS),
                truncated,
            });
        } catch (error) {
            return readFailure(call, error, "Planning Memory metadata");
        }
    }

    private async read(call: ExecutableToolCallDescriptor): Promise<ToolResultDescriptor> {
        const selections = call.arguments.topics;
        if (!Array.isArray(selections) || selections.length < 1 || selections.length > 5 ||
            selections.some((selection) => !isTopicSelection(selection)) ||
            new Set(selections.flatMap((selection) => isTopicSelection(selection) ? [selection.path] : [])).size !==
                selections.length || hasExtraKeys(call.arguments, ["topics"])) {
            return failed(
                call,
                "protocol.invalid_params",
                "Planning Memory read requires one to five unique topics bound to listed versions and hashes.",
            );
        }
        try {
            const topics: Array<Record<string, string>> = [];
            const sourceRefs: SourceRef[] = [];
            let totalBytes = 0;
            for (const selection of selections as TopicSelection[]) {
                const { path } = selection;
                const file = this.vault.getFileByPath(path);
                if (file === null || file.extension.toLocaleLowerCase() !== "md") {
                    return failed(call, "resource.not_found", "A selected Planning Memory topic is missing.");
                }
                const snapshot = await stableRead(this.vault, file);
                const metadata = topicMetadata(path, snapshot);
                if (metadata === null || snapshot.modifiedVersion !== selection.expectedModifiedVersion ||
                    snapshot.contentHash !== selection.expectedContentHash) {
                    return failed(
                        call,
                        "resource.conflict",
                        "A selected Planning Memory topic changed after metadata discovery.",
                        true,
                    );
                }
                totalBytes += Buffer.byteLength(snapshot.content, "utf8");
                if (totalBytes > MAX_READ_BYTES) throw new RangeError("Planning Memory selection is oversized");
                topics.push({ path, ...snapshot });
                sourceRefs.push(vaultSource(this.workspaceId, path, snapshot.contentHash, "Planning Memory topic"));
            }
            return succeeded(call, "Read exact selected Planning Memory topics.", { topics }, sourceRefs);
        } catch (error) {
            return readFailure(call, error, "Planning Memory selection");
        }
    }
}

interface TopicSelection {
    readonly path: string;
    readonly expectedModifiedVersion: string;
    readonly expectedContentHash: string;
}

function isTopicSelection(value: unknown): value is TopicSelection {
    if (typeof value !== "object" || value === null || Array.isArray(value)) return false;
    const candidate = value as Record<string, unknown>;
    return typeof candidate.path === "string" && TOPIC_PATH.test(candidate.path) &&
        typeof candidate.expectedModifiedVersion === "string" &&
        candidate.expectedModifiedVersion.length >= 1 && candidate.expectedModifiedVersion.length <= 128 &&
        typeof candidate.expectedContentHash === "string" && CONTENT_HASH.test(candidate.expectedContentHash) &&
        !hasExtraKeys(candidate, ["path", "expectedModifiedVersion", "expectedContentHash"]);
}

interface TopicSnapshot {
    readonly content: string;
    readonly contentHash: string;
    readonly modifiedVersion: string;
}

async function stableRead(vault: PlanningMemoryVaultPort, file: TFile): Promise<TopicSnapshot> {
    if (file.stat.size < 1 || file.stat.size > MAX_TOPIC_BYTES) throw new RangeError("Planning Memory topic is oversized");
    const before = modifiedVersion(file);
    const content = await vault.cachedRead(file);
    const after = vault.getFileByPath(file.path);
    if (after === null || modifiedVersion(after) !== before) throw new Error("Planning Memory topic changed during read");
    const byteLength = Buffer.byteLength(content, "utf8");
    if (!content.trim() || content.includes("\0") || byteLength < 1 || byteLength > MAX_TOPIC_BYTES) {
        throw new RangeError("Planning Memory topic content is invalid");
    }
    return { content, contentHash: digest(content), modifiedVersion: before };
}

function topicMetadata(path: string, snapshot: TopicSnapshot): TopicMetadata | null {
    const pathMatch = TOPIC_PATH.exec(path);
    const lines = snapshot.content.replace(/\r\n/gu, "\n").split("\n");
    if (pathMatch === null || lines[0] !== "---") return null;
    const closing = lines.slice(1, 34).findIndex((line) => line === "---");
    if (closing < 0) return null;
    const values = new Map<string, string>();
    for (const line of lines.slice(1, closing + 1)) {
        if (!line.trim()) continue;
        const match = /^(name|description|type):\s*(.+)$/u.exec(line);
        if (match === null || values.has(match[1])) return null;
        const value = scalar(match[2]);
        if (value === null) return null;
        values.set(match[1], value);
    }
    const name = values.get("name")?.trim() ?? "";
    const description = values.get("description")?.trim() ?? "";
    const type = values.get("type");
    if (type !== pathMatch[1] || !name || !description || name.includes("\n") || description.includes("\n") ||
        Buffer.byteLength(name, "utf8") > 128 || Buffer.byteLength(description, "utf8") > 512) return null;
    return {
        path,
        type: type as TopicMetadata["type"],
        name,
        description,
        modifiedVersion: snapshot.modifiedVersion,
        contentHash: snapshot.contentHash,
    };
}

function scalar(raw: string): string | null {
    const value = raw.trim();
    if (value.startsWith('"')) {
        try {
            const parsed = JSON.parse(value);
            return typeof parsed === "string" ? parsed : null;
        } catch {
            return null;
        }
    }
    if (value.startsWith("'")) {
        return value.endsWith("'") && value.length >= 2 ? value.slice(1, -1).replace(/''/gu, "'") : null;
    }
    return value && !/[\[\]{}\n\r]/u.test(value) ? value : null;
}

function modifiedVersion(file: TFile): string {
    return `mtime:${file.stat.mtime}:size:${file.stat.size}`;
}

function digest(content: string): string {
    return `sha256:${createHash("sha256").update(content, "utf8").digest("hex")}`;
}

function vaultSource(workspaceId: string, path: string, contentHash: string, label: string): SourceRef {
    return { type: "vault", file: { workspaceId, path, contentHash }, freshness: "fresh", label };
}

function succeeded(
    call: ExecutableToolCallDescriptor,
    summary: string,
    data: Record<string, unknown>,
    sourceRefs: ToolResultDescriptor["sourceRefs"] = [],
): ToolResultDescriptor {
    return { toolCallId: call.toolCallId, status: "succeeded", summary, data, sourceRefs, retryable: false };
}

function readFailure(call: ExecutableToolCallDescriptor, error: unknown, label: string): ToolResultDescriptor {
    const oversized = error instanceof RangeError;
    return failed(
        call,
        oversized ? "protocol.message_too_large" : "resource.conflict",
        oversized ? `${label} exceeds its bounded read contract.` : `${label} could not be read consistently.`,
        !oversized,
    );
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

function hasExtraKeys(value: Readonly<Record<string, unknown>>, allowed: readonly string[]): boolean {
    return Object.keys(value).some((key) => !allowed.includes(key));
}
