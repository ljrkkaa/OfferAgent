import type { ProjectSourceRef, SourceRef, VaultSourceRef, WebSourceRef } from "./generated_protocol";
import type { JsonObject, JsonValue } from "./json_rpc";

const FRESHNESS = new Set(["fresh", "stale", "partial", "stale_partial", "unknown"]);

export interface VaultReferenceTarget {
    readonly path: string;
    readonly lineStart: number | null;
    readonly lineEnd: number | null;
    readonly heading: string | null;
}

export interface ProjectReferenceTarget {
    readonly projectId: string;
    readonly path: string;
    readonly lineStart: number | null;
    readonly lineEnd: number | null;
}

export function sourceReferenceArray(value: JsonValue | undefined): SourceRef[] {
    if (value === undefined) return [];
    if (!Array.isArray(value)) throw new TypeError("sourceRefs must be an array");
    return value.map(sourceReference);
}

export function mergeSourceReferences(current: SourceRef[], incoming: SourceRef[]): SourceRef[] {
    const merged = new Map<string, SourceRef>();
    for (const reference of [...current, ...incoming]) merged.set(sourceReferenceKey(reference), reference);
    return [...merged.values()];
}

export function sourceReferenceKey(reference: SourceRef): string {
    switch (reference.type) {
        case "vault":
            return [
                "vault",
                reference.file.workspaceId,
                reference.file.path,
                reference.file.lineStart ?? "",
                reference.file.lineEnd ?? "",
                reference.file.heading ?? "",
                reference.file.blockId ?? "",
            ].join(":");
        case "artifact":
            return `artifact:${reference.artifact.artifactId}`;
        case "project":
            return [
                "project",
                reference.projectId,
                reference.path,
                reference.lineStart ?? "",
                reference.lineEnd ?? "",
            ].join(":");
        case "web":
            return `web:${reference.url}:${reference.contentHash}`;
    }
}

export function sourceReferenceLabel(reference: SourceRef): string {
    switch (reference.type) {
        case "vault": {
            const line = reference.file.lineStart === undefined || reference.file.lineStart === null
                ? ""
                : reference.file.lineEnd && reference.file.lineEnd !== reference.file.lineStart
                    ? `:${reference.file.lineStart}-${reference.file.lineEnd}`
                    : `:${reference.file.lineStart}`;
            return `${reference.label ?? reference.file.heading ?? reference.file.path}${line}`;
        }
        case "artifact":
            return reference.label ?? reference.artifact.title ?? reference.artifact.artifactId;
        case "project": {
            const line = reference.lineStart === undefined || reference.lineStart === null
                ? ""
                : reference.lineEnd && reference.lineEnd !== reference.lineStart
                    ? `:${reference.lineStart}-${reference.lineEnd}`
                    : `:${reference.lineStart}`;
            return `${reference.label ?? `${reference.projectId}/${reference.path}`}${line}`;
        }
        case "web":
            return reference.label ?? reference.title;
    }
}

export function projectReferenceTarget(reference: SourceRef): ProjectReferenceTarget | null {
    if (reference.type !== "project") return null;
    return {
        projectId: reference.projectId,
        path: reference.path,
        lineStart: reference.lineStart ?? null,
        lineEnd: reference.lineEnd ?? null,
    };
}

export function vaultReferenceTarget(reference: SourceRef): VaultReferenceTarget | null {
    if (reference.type !== "vault") return null;
    return {
        path: reference.file.path,
        lineStart: reference.file.lineStart ?? null,
        lineEnd: reference.file.lineEnd ?? null,
        heading: reference.file.heading ?? null,
    };
}

export function webReferenceTarget(reference: SourceRef): string | null {
    if (reference.type !== "web") return null;
    return safeWebUrl(reference.url);
}

function sourceReference(value: JsonValue): SourceRef {
    const reference = jsonObject(value, "source reference");
    const type = requiredText(reference, "type");
    optionalText(reference, "label");
    if (type === "vault") return vaultSourceReference(reference);
    if (type === "project") return projectSourceReference(reference);
    if (type === "web") return webSourceReference(reference);
    if (type === "artifact") {
        const artifact = jsonObject(reference.artifact, "artifact reference");
        requiredText(artifact, "artifactId");
        requiredText(artifact, "contentHash");
        requiredText(artifact, "mediaType");
        requiredText(artifact, "sensitivity");
        nonNegativeInteger(artifact, "sizeBytes");
        optionalText(artifact, "state");
        optionalText(artifact, "title");
        return reference as unknown as SourceRef;
    }
    throw new TypeError(`unsupported source reference type: ${type}`);
}

function webSourceReference(reference: JsonObject): WebSourceRef {
    const url = requiredText(reference, "url");
    if (safeWebUrl(url) === null) throw new TypeError("invalid Web source URL");
    requiredText(reference, "contentHash");
    requiredText(reference, "title");
    const freshness = optionalText(reference, "freshness");
    if (freshness !== null && !FRESHNESS.has(freshness)) throw new TypeError("invalid source freshness");
    return reference as unknown as WebSourceRef;
}

function safeWebUrl(value: string): string | null {
    try {
        const url = new URL(value);
        return ["http:", "https:"].includes(url.protocol) && !url.username && !url.password ? url.href : null;
    } catch {
        return null;
    }
}

function projectSourceReference(reference: JsonObject): ProjectSourceRef {
    requiredText(reference, "projectId");
    requiredText(reference, "path");
    requiredText(reference, "contentHash");
    requiredText(reference, "modifiedVersion");
    const lineStart = optionalPositiveInteger(reference, "lineStart");
    const lineEnd = optionalPositiveInteger(reference, "lineEnd");
    if (lineEnd !== null && (lineStart === null || lineEnd < lineStart)) {
        throw new TypeError("invalid Project source line range");
    }
    const freshness = optionalText(reference, "freshness");
    if (freshness !== null && !FRESHNESS.has(freshness)) throw new TypeError("invalid source freshness");
    return reference as unknown as ProjectSourceRef;
}

function vaultSourceReference(reference: JsonObject): VaultSourceRef {
    const file = jsonObject(reference.file, "Vault file reference");
    requiredText(file, "workspaceId");
    requiredText(file, "path");
    optionalText(file, "contentHash");
    optionalText(file, "heading");
    optionalText(file, "blockId");
    const lineStart = optionalPositiveInteger(file, "lineStart");
    const lineEnd = optionalPositiveInteger(file, "lineEnd");
    if (lineEnd !== null && (lineStart === null || lineEnd < lineStart)) {
        throw new TypeError("invalid Vault source line range");
    }
    const freshness = optionalText(reference, "freshness");
    if (freshness !== null && !FRESHNESS.has(freshness)) throw new TypeError("invalid source freshness");
    optionalNonNegativeInteger(reference, "workspaceRevision");
    return reference as unknown as VaultSourceRef;
}

function jsonObject(value: JsonValue | undefined, name: string): JsonObject {
    if (value === null || value === undefined || typeof value !== "object" || Array.isArray(value)) {
        throw new TypeError(`${name} must be an object`);
    }
    return value;
}

function requiredText(value: JsonObject, key: string): string {
    const field = value[key];
    if (typeof field !== "string" || field.length === 0) throw new TypeError(`${key} must be non-empty text`);
    return field;
}

function optionalText(value: JsonObject, key: string): string | null {
    const field = value[key];
    if (field === undefined || field === null) return null;
    return requiredText(value, key);
}

function optionalPositiveInteger(value: JsonObject, key: string): number | null {
    const field = value[key];
    if (field === undefined || field === null) return null;
    if (typeof field !== "number" || !Number.isSafeInteger(field) || field < 1) {
        throw new TypeError(`${key} must be a positive integer`);
    }
    return field;
}

function nonNegativeInteger(value: JsonObject, key: string): number {
    const field = value[key];
    if (typeof field !== "number" || !Number.isSafeInteger(field) || field < 0) {
        throw new TypeError(`${key} must be a non-negative integer`);
    }
    return field;
}

function optionalNonNegativeInteger(value: JsonObject, key: string): number | null {
    const field = value[key];
    if (field === undefined || field === null) return null;
    return nonNegativeInteger(value, key);
}
