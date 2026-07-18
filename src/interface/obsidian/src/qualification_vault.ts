import { createHash } from "node:crypto";
import {
    existsSync,
    readdirSync,
    statSync,
} from "node:fs";
import type { Stats } from "node:fs";
import {
    mkdir,
    readFile,
    writeFile,
} from "node:fs/promises";
import * as path from "node:path";

import type { TFile } from "obsidian";

import type {
    ConditionalMutationOutcome,
    ConditionalVaultMutation,
    VaultChangePort,
    VaultChangeSnapshot,
    VaultIdentity,
} from "./runtime/vault_changes";
import type { VaultReadPort } from "./runtime/vault_tool_adapter";

/**
 * Filesystem-backed qualification implementation of the two Obsidian Vault ports.
 * It deliberately contains no Run policy; it only makes the sealed plugin adapters
 * executable against a temporary, qualification-owned Vault.
 */
export class QualificationVaultPort implements VaultReadPort, VaultChangePort {
    private readonly root: string;

    constructor(root: string) {
        if (!path.isAbsolute(root) || root.includes("\0")) {
            throw new TypeError("qualification Vault root must be absolute");
        }
        this.root = path.resolve(root);
    }

    getFiles(): TFile[] {
        return this.walk(this.root, "").sort((left, right) => left.path.localeCompare(right.path));
    }

    getFileByPath(relativePath: string): TFile | null {
        const target = this.target(relativePath);
        if (!existsSync(target)) return null;
        const stats = statSync(target);
        return stats.isFile() ? qualificationFile(relativePath, stats) : null;
    }

    async cachedRead(file: TFile): Promise<string> {
        const current = this.getFileByPath(file.path);
        if (current === null) throw new Error(`qualification Vault file is missing: ${file.path}`);
        return readFile(this.target(file.path), "utf8");
    }

    async read(relativePath: string): Promise<string | undefined> {
        const file = this.getFileByPath(relativePath);
        return file === null ? undefined : this.cachedRead(file);
    }

    async snapshot(relativePath: string): Promise<VaultChangeSnapshot> {
        const file = this.getFileByPath(relativePath);
        if (file === null) return { content: undefined, modifiedVersion: "missing" };
        const before = modifiedVersion(file);
        const content = await this.cachedRead(file);
        const after = this.getFileByPath(relativePath);
        if (after === null || modifiedVersion(after) !== before) {
            throw new Error(`qualification Vault file changed while being read: ${relativePath}`);
        }
        return { content, modifiedVersion: before };
    }

    async applyConditional(mutation: ConditionalVaultMutation): Promise<ConditionalMutationOutcome> {
        const before = await this.snapshot(mutation.path);
        const observed = identity(before);
        if (!sameIdentity(observed, mutation.expected)) return { status: "conflict", observed };
        if (mutation.kind === "delete") return { status: "unsupported", operation: "delete" };
        const target = this.target(mutation.path);
        await mkdir(path.dirname(target), { recursive: true });
        try {
            await writeFile(target, mutation.afterContent, {
                encoding: "utf8",
                ...(mutation.kind === "create" ? { flag: "wx" } : {}),
            });
        } catch (error) {
            const current = await this.observedIdentity(mutation.path);
            if (current !== null && !sameIdentity(current, mutation.expected)) {
                return { status: "conflict", observed: current };
            }
            throw error;
        }
        const applied = await this.observedIdentity(mutation.path);
        return applied !== null && applied.contentHash === contentIdentity(mutation.afterContent)
            ? { status: "applied", applied }
            : { status: "unknown", observed: applied };
    }

    private async observedIdentity(relativePath: string): Promise<VaultIdentity | null> {
        try {
            return identity(await this.snapshot(relativePath));
        } catch {
            return null;
        }
    }

    private target(relativePath: string): string {
        if (typeof relativePath !== "string" || relativePath.length === 0 ||
            relativePath.includes("\0") || relativePath.includes("\\") ||
            path.posix.normalize(relativePath) !== relativePath || relativePath.startsWith("/") ||
            relativePath === ".." || relativePath.startsWith("../") || /^[A-Za-z]:/u.test(relativePath)) {
            throw new TypeError("qualification path must be a safe relative Vault path");
        }
        const target = path.resolve(this.root, ...relativePath.split("/"));
        if (!target.startsWith(`${this.root}${path.sep}`)) {
            throw new TypeError("qualification path must be a safe relative Vault path");
        }
        return target;
    }

    private walk(directory: string, relativeDirectory: string): TFile[] {
        const files: TFile[] = [];
        for (const entry of readdirSync(directory, { withFileTypes: true })) {
            if (entry.isSymbolicLink()) continue;
            const relative = relativeDirectory ? `${relativeDirectory}/${entry.name}` : entry.name;
            const absolute = path.join(directory, entry.name);
            if (entry.isDirectory()) files.push(...this.walk(absolute, relative));
            else if (entry.isFile()) files.push(qualificationFile(relative, statSync(absolute)));
        }
        return files;
    }
}

function qualificationFile(relativePath: string, stats: Stats): TFile {
    const name = path.posix.basename(relativePath);
    const dot = name.lastIndexOf(".");
    return {
        path: relativePath,
        name,
        basename: dot > 0 ? name.slice(0, dot) : name,
        extension: dot > 0 ? name.slice(dot + 1) : "",
        stat: {
            ctime: Math.trunc(stats.ctimeMs),
            mtime: Math.trunc(stats.mtimeMs),
            size: stats.size,
        },
        parent: null,
    } as unknown as TFile;
}

function modifiedVersion(file: TFile): string {
    return `mtime:${file.stat.mtime}:size:${file.stat.size}`;
}

function contentIdentity(content: string | undefined): string {
    return content === undefined
        ? "absent"
        : `sha256:${createHash("sha256").update(content, "utf8").digest("hex")}`;
}

function identity(snapshot: VaultChangeSnapshot): VaultIdentity {
    return {
        contentHash: contentIdentity(snapshot.content),
        modifiedVersion: snapshot.modifiedVersion,
    };
}

function sameIdentity(left: VaultIdentity, right: VaultIdentity): boolean {
    return left.contentHash === right.contentHash && left.modifiedVersion === right.modifiedVersion;
}
