import { Buffer } from "node:buffer";
import { createHash } from "node:crypto";

import type { TFile } from "obsidian";

import type { ExecutableToolCallDescriptor, SourceRef, ToolResultDescriptor } from "./generated_protocol";
import { failed, hasExtraKeys, succeeded } from "./plugin_tool_results";

const MAX_CONTROL_BYTES = 32_768;
const SKILL_NAME = /^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$/u;

export interface VaultControlPort {
    getFileByPath(path: string): TFile | null;
    cachedRead(file: TFile): Promise<string>;
}

export interface DailyNotesConfiguration {
    readonly folder?: string;
    readonly format?: string;
    readonly template?: string;
}

export interface DailyNotesApi {
    resolveToday(): string;
    readConfiguration(): Promise<DailyNotesConfiguration | undefined>;
    formatDate(date: string, format: string): string;
}

export interface VaultControlOptions {
    readonly dailyNotes?: DailyNotesApi;
}

/** Explicit control-plane reads which are intentionally excluded from evidence discovery. */
export class VaultControlAdapter {
    constructor(
        private readonly vault: VaultControlPort,
        private readonly workspaceId: string,
        private readonly options: VaultControlOptions = {},
    ) {}

    async execute(call: ExecutableToolCallDescriptor): Promise<ToolResultDescriptor> {
        if (call.name === "agent_contract.read") return this.readAgentContract(call);
        if (call.name === "skill.read") return this.readSkill(call);
        if (call.name === "daily_note.context") return this.dailyNoteContext(call);
        throw new Error(`unsupported Vault control Tool: ${call.name}@${call.version}`);
    }

    private async readAgentContract(call: ExecutableToolCallDescriptor): Promise<ToolResultDescriptor> {
        if (Object.keys(call.arguments).length !== 0) {
            return failed(call, "protocol.invalid_params", "Agent Contract read accepts no arguments.");
        }
        const path = "agent.md";
        const file = this.vault.getFileByPath(path);
        if (file === null || file.extension.toLocaleLowerCase() !== "md") {
            return failed(call, "resource.not_found", "当前 Vault 中没有 agent.md Agent Contract。");
        }
        try {
            const snapshot = await boundedRead(this.vault, file);
            return succeeded(call, "Read the Vault Agent Contract.", {
                path,
                content: snapshot.content,
                contentHash: snapshot.contentHash,
            }, [vaultSource(this.workspaceId, path, snapshot.contentHash, "Vault Agent Contract")]);
        } catch (error) {
            return controlReadFailure(call, error, "Vault Agent Contract");
        }
    }

    private async readSkill(call: ExecutableToolCallDescriptor): Promise<ToolResultDescriptor> {
        const input = call.arguments;
        const skill = typeof input.skill === "string" && SKILL_NAME.test(input.skill) ? input.skill : undefined;
        const resource = input.resource === undefined ? "SKILL.md" : safeRelativePath(input.resource, true);
        if (!skill || !resource || hasExtraKeys(input, ["skill", "resource"])) {
            return failed(call, "protocol.invalid_params", "Local Skill name or resource is invalid.");
        }
        const root = `.codex/skills/${skill}`;
        const manifestPath = `${root}/SKILL.md`;
        const manifest = this.vault.getFileByPath(manifestPath);
        if (manifest === null || manifest.extension.toLocaleLowerCase() !== "md") {
            return failed(call, "resource.not_found", "The selected Local Skill is not installed in this Vault.");
        }
        const resourcePath = `${root}/${resource}`;
        const target = this.vault.getFileByPath(resourcePath);
        if (target === null || !readable(target)) {
            return failed(call, "resource.not_found", "The selected Local Skill resource is missing.");
        }
        try {
            const manifestContent = await boundedRead(this.vault, manifest);
            if (resource !== "SKILL.md" && !directlyReferences(manifestContent.content, resource)) {
                return failed(call, "resource.not_found", "The resource is not directly referenced by SKILL.md.");
            }
            const snapshot = resource === "SKILL.md" ? manifestContent : await boundedRead(this.vault, target);
            return succeeded(call, "Read an explicit Local Skill resource.", {
                skill,
                resource,
                path: resourcePath,
                modifiedVersion: snapshot.modifiedVersion,
                contentHash: snapshot.contentHash,
                content: snapshot.content,
            }, [vaultSource(this.workspaceId, resourcePath, snapshot.contentHash, "Local Skill resource")]);
        } catch (error) {
            return controlReadFailure(call, error, "Local Skill resource");
        }
    }

    private async dailyNoteContext(call: ExecutableToolCallDescriptor): Promise<ToolResultDescriptor> {
        const input = call.arguments;
        const requestedDate = input.date === undefined ? undefined : isoDate(input.date);
        if (requestedDate === null || hasExtraKeys(input, ["date"])) {
            return failed(call, "protocol.invalid_params", "Daily Note date is invalid.");
        }
        const dailyNotes = this.options.dailyNotes;
        if (dailyNotes === undefined) {
            return failed(call, "resource.not_found", "Daily Notes integration is unavailable.");
        }
        try {
            const config = await dailyNotes.readConfiguration() ?? {};
            const resolvedDate = requestedDate ?? isoDate(dailyNotes.resolveToday());
            const folder = config.folder === undefined || config.folder === "" ? "" : safeRelativePath(config.folder, false);
            const format = typeof config.format === "string" && config.format.trim() ? config.format.trim() : "YYYY-MM-DD";
            if (resolvedDate === null || folder === undefined || format.length > 128) {
                return failed(call, "protocol.invalid_params", "Daily Notes configuration is unsafe.");
            }
            const formatted = dailyNotes.formatDate(resolvedDate, format);
            const targetPath = safeRelativePath(`${folder ? `${folder}/` : ""}${formatted}.md`, false);
            if (!targetPath) return failed(call, "protocol.invalid_params", "Daily Note target path is unsafe.");

            const sourceRefs: SourceRef[] = [];
            const target = this.vault.getFileByPath(targetPath);
            let targetSnapshot: Snapshot | undefined;
            if (target !== null) {
                if (target.extension.toLocaleLowerCase() !== "md") {
                    return failed(call, "resource.not_found", "Daily Note target is not Markdown.");
                }
                targetSnapshot = await boundedRead(this.vault, target);
                sourceRefs.push(vaultSource(this.workspaceId, targetPath, targetSnapshot.contentHash, "Daily Note"));
            }

            const templateSetting = typeof config.template === "string" ? config.template.trim() : "";
            let templatePath: string | null = null;
            let templateSnapshot: Snapshot | undefined;
            if (templateSetting) {
                const candidate = safeRelativePath(templateSetting, false);
                if (!candidate) return failed(call, "protocol.invalid_params", "Daily Note template path is unsafe.");
                const paths = candidate.toLocaleLowerCase().endsWith(".md") ? [candidate] : [candidate, `${candidate}.md`];
                const match = paths.map((path) => ({ path, file: this.vault.getFileByPath(path) }))
                    .find(({ file }) => file !== null);
                if (match === undefined || match.file === null || match.file.extension.toLocaleLowerCase() !== "md") {
                    return failed(call, "resource.not_found", "Configured Daily Note template is missing.");
                }
                templatePath = match.path;
                templateSnapshot = await boundedRead(this.vault, match.file);
                sourceRefs.push(vaultSource(this.workspaceId, templatePath, templateSnapshot.contentHash, "Daily Note template"));
            }

            return succeeded(call, "Resolved Daily Note context without creating or modifying a note.", {
                resolvedDate,
                dateFormat: format,
                targetPath,
                targetExists: targetSnapshot !== undefined,
                targetContent: targetSnapshot?.content ?? null,
                targetModifiedVersion: targetSnapshot?.modifiedVersion ?? null,
                targetContentHash: targetSnapshot?.contentHash ?? null,
                templatePath,
                templateContent: templateSnapshot?.content ?? null,
                templateModifiedVersion: templateSnapshot?.modifiedVersion ?? null,
                templateContentHash: templateSnapshot?.contentHash ?? null,
            }, sourceRefs);
        } catch (error) {
            return controlReadFailure(call, error, "Daily Note context");
        }
    }
}

interface Snapshot {
    readonly content: string;
    readonly contentHash: string;
    readonly modifiedVersion: string;
}

async function boundedRead(vault: VaultControlPort, file: TFile): Promise<Snapshot> {
    if (file.stat.size > MAX_CONTROL_BYTES) throw new RangeError("resource exceeds the control read limit");
    const before = modifiedVersion(file);
    const content = await vault.cachedRead(file);
    const after = vault.getFileByPath(file.path);
    if (after === null || modifiedVersion(after) !== before) throw new Error("resource changed during read");
    if (!content || content.includes("\0") || Buffer.byteLength(content, "utf8") > MAX_CONTROL_BYTES) {
        throw new RangeError("resource content is empty, binary, or oversized");
    }
    return { content, contentHash: digest(content), modifiedVersion: before };
}

function directlyReferences(manifest: string, resource: string): boolean {
    const links = manifest.matchAll(/\]\(<?([^)>\s]+)>?(?:\s+["'][^"']*["'])?\)/gu);
    for (const match of links) {
        const raw = match[1].split("#", 1)[0].split("?", 1)[0];
        let decoded: string;
        try { decoded = decodeURIComponent(raw); } catch { continue; }
        if (safeRelativePath(decoded, true) === resource) return true;
    }
    return false;
}

function safeRelativePath(value: unknown, allowHidden: boolean): string | undefined {
    if (typeof value !== "string") return undefined;
    const path = value.trim().replace(/^<|>$/gu, "");
    if (!path || path.length > 512 || path.includes("\\") || path.startsWith("/") || path.includes(":") || path.includes("\0")) {
        return undefined;
    }
    const segments = path.split("/");
    if (segments.some((segment) => !segment || segment === "." || segment === ".." || (!allowHidden && segment.startsWith(".")))) {
        return undefined;
    }
    return path;
}

function isoDate(value: unknown): string | null {
    if (typeof value !== "string" || !/^\d{4}-\d{2}-\d{2}$/u.test(value)) return null;
    const [year, month, day] = value.split("-").map(Number);
    const date = new Date(Date.UTC(year, month - 1, day));
    return date.getUTCFullYear() === year && date.getUTCMonth() + 1 === month && date.getUTCDate() === day ? value : null;
}

function readable(file: TFile): boolean {
    return file.extension.toLocaleLowerCase() === "md" || file.extension.toLocaleLowerCase() === "txt";
}

function modifiedVersion(file: TFile): string {
    return `mtime:${file.stat.mtime}:size:${file.stat.size}`;
}

function digest(content: string): string {
    return `sha256:${createHash("sha256").update(content, "utf8").digest("hex")}`;
}

function vaultSource(
    workspaceId: string,
    path: string,
    contentHash: string,
    label: string,
): SourceRef {
    return { type: "vault", file: { workspaceId, path, contentHash }, freshness: "fresh", label };
}

function controlReadFailure(call: ExecutableToolCallDescriptor, error: unknown, label: string): ToolResultDescriptor {
    const oversized = error instanceof RangeError;
    const message = oversized ? `${label} exceeds its bounded read contract.` : `${label} could not be read consistently.`;
    return failed(call, oversized ? "protocol.message_too_large" : "resource.conflict", message, !oversized);
}
