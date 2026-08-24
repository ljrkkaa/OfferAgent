import { randomBytes } from "node:crypto";
import { constants } from "node:fs";
import { access, lstat, mkdir, open, readFile, realpath } from "node:fs/promises";
import { isAbsolute, join, relative, resolve } from "node:path";

import { parseStrictJson } from "./strict_json";

const CONFIG_DIRECTORY = ".offeragent";
const CONFIG_FILE = "workspace.json";
// Keep this bound identical to the Worker parser so a portable identity is
// accepted or rejected consistently on both sides of the IPC boundary.
const MAX_CONFIG_BYTES = 4 * 1024;
const WORKSPACE_ID = /^ws_[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/;

export interface PortableWorkspaceIdentity {
    readonly schemaVersion: 1;
    readonly portableWorkspaceId: string;
}

export async function loadOrCreatePortableWorkspaceIdentity(
    vaultRoot: string,
    uuidSource: () => string = uuidV4,
): Promise<PortableWorkspaceIdentity> {
    const locations = await safeLocations(vaultRoot, true);
    try {
        return await readIdentity(locations.file);
    } catch (error) {
        if ((error as NodeJS.ErrnoException).code !== "ENOENT") throw error;
    }
    const portableWorkspaceId = `ws_${uuidSource()}`;
    if (!WORKSPACE_ID.test(portableWorkspaceId)) throw new Error("Workspace identity source returned an invalid canonical UUID");
    const identity: PortableWorkspaceIdentity = { schemaVersion: 1, portableWorkspaceId };
    const payload = encode(identity);
    let handle;
    try {
        handle = await open(locations.file, "wx", 0o644);
        await handle.writeFile(payload, { encoding: "utf8" });
        await handle.sync();
    } catch (error) {
        if ((error as NodeJS.ErrnoException).code === "EEXIST") return readIdentity(locations.file);
        throw error;
    } finally {
        await handle?.close();
    }
    return identity;
}

export async function readPortableWorkspaceIdentity(vaultRoot: string): Promise<PortableWorkspaceIdentity | null> {
    const locations = await safeLocations(vaultRoot, false);
    try {
        return await readIdentity(locations.file);
    } catch (error) {
        if ((error as NodeJS.ErrnoException).code === "ENOENT") return null;
        throw error;
    }
}

async function safeLocations(vaultRoot: string, createDirectory: boolean): Promise<{ root: string; directory: string; file: string }> {
    if (!isAbsolute(vaultRoot) || vaultRoot.includes("\0") || /[\r\n]/.test(vaultRoot)) {
        throw new TypeError("Vault root must be an absolute local path");
    }
    const root = await realpath(resolve(vaultRoot));
    const rootState = await lstat(root);
    if (!rootState.isDirectory() || rootState.isSymbolicLink()) throw new Error("Vault root is not a real directory");
    const directory = join(root, CONFIG_DIRECTORY);
    if (createDirectory) await mkdir(directory, { recursive: false }).catch(ignoreExistingDirectory);
    try {
        const directoryState = await lstat(directory);
        if (!directoryState.isDirectory() || directoryState.isSymbolicLink()) {
            throw new Error(".offeragent must be a real directory inside the Vault");
        }
        const canonicalDirectory = await realpath(directory);
        if (!isContained(root, canonicalDirectory)) throw new Error(".offeragent escapes the Vault root");
    } catch (error) {
        if (!createDirectory && (error as NodeJS.ErrnoException).code === "ENOENT") {
            return { root, directory, file: join(directory, CONFIG_FILE) };
        }
        throw error;
    }
    const file = join(directory, CONFIG_FILE);
    try {
        const fileState = await lstat(file);
        if (!fileState.isFile() || fileState.isSymbolicLink()) throw new Error("workspace identity must be a real file");
    } catch (error) {
        if ((error as NodeJS.ErrnoException).code !== "ENOENT") throw error;
    }
    return { root, directory, file };
}

function ignoreExistingDirectory(error: unknown): void {
    if ((error as NodeJS.ErrnoException).code !== "EEXIST") throw error;
}

async function readIdentity(file: string): Promise<PortableWorkspaceIdentity> {
    await access(file, constants.R_OK);
    const bytes = await readFile(file);
    if (bytes.length < 1 || bytes.length > MAX_CONFIG_BYTES) throw new Error("workspace identity file size is invalid");
    let raw: unknown;
    try {
        raw = parseStrictJson(new TextDecoder("utf-8", { fatal: true }).decode(bytes), {
            maximumCharacters: MAX_CONFIG_BYTES,
        });
    } catch (error) {
        throw new Error("workspace identity file is not canonical UTF-8 JSON");
    }
    if (!raw || typeof raw !== "object" || Array.isArray(raw)) throw new Error("workspace identity is not an object");
    const value = raw as Record<string, unknown>;
    if (Object.keys(value).length !== 2 || value.schemaVersion !== 1 ||
        typeof value.portableWorkspaceId !== "string" || !WORKSPACE_ID.test(value.portableWorkspaceId)) {
        throw new Error("workspace identity schema is invalid");
    }
    const identity: PortableWorkspaceIdentity = {
        schemaVersion: 1,
        portableWorkspaceId: value.portableWorkspaceId,
    };
    if (!Buffer.from(encode(identity), "utf8").equals(bytes)) throw new Error("workspace identity is not canonical JSON");
    return identity;
}

function encode(identity: PortableWorkspaceIdentity): string {
    return JSON.stringify({ portableWorkspaceId: identity.portableWorkspaceId, schemaVersion: 1 }) + "\n";
}

function isContained(root: string, candidate: string): boolean {
    const path = relative(root, candidate);
    return path === "" || (!path.startsWith("..") && !isAbsolute(path));
}

function uuidV4(): string {
    const bytes = randomBytes(16);
    bytes[6] = (bytes[6] & 0x0f) | 0x40;
    bytes[8] = (bytes[8] & 0x3f) | 0x80;
    const hex = bytes.toString("hex");
    return `${hex.slice(0, 8)}-${hex.slice(8, 12)}-${hex.slice(12, 16)}-${hex.slice(16, 20)}-${hex.slice(20)}`;
}
