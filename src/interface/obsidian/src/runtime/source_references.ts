import type { SourceRef, VaultSourceRef } from "./generated_protocol";
import type { JsonObject, JsonValue } from "./json_rpc";

const FRESHNESS = new Set(["fresh", "stale", "partial", "stale_partial", "unknown"]);

export interface VaultReferenceTarget {
    readonly path: string;
    readonly lineStart: number | null;
    readonly lineEnd: number | null;
    readonly heading: string | null;
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
    }
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

function sourceReference(value: JsonValue): SourceRef {
    const reference = jsonObject(value, "source reference");
    const type = requiredText(reference, "type");
    optionalText(reference, "label");
    if (type === "vault") return vaultSourceReference(reference);
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
