import { createHash } from "node:crypto";

import { App, CachedMetadata, MarkdownView, TFile } from "obsidian";

import { JsonObject, JsonValue } from "../runtime/json_rpc";

const MAX_SELECTION_CHARS = 262_144;
const MAX_FRONTMATTER_KEYS = 256;
const MAX_METADATA_ITEMS = 2048;
const MAX_METADATA_TEXT = 4096;
const MAX_METADATA_DEPTH = 8;
const CONTEXT_FIELDS = new Set(["activeFile", "selection", "cursor", "metadata", "backlinks", "unsavedState"]);

export class ObsidianContextBridge {
    private readonly app: App;
    private readonly fileRevisions = new Map<string, number>();
    private selectionRevision = 0;
    private metadataRevision = 0;

    constructor(app: App) {
        this.app = app;
    }

    noteFileChanged(path: string): void {
        if (!safeRelativePath(path)) return;
        this.fileRevisions.set(path, (this.fileRevisions.get(path) ?? 0) + 1);
    }

    noteEditorChanged(): void {
        this.selectionRevision += 1;
        const file = this.app.workspace.getActiveFile();
        if (file) this.noteFileChanged(file.path);
    }

    noteMetadataChanged(file?: TFile | null): void {
        this.metadataRevision += 1;
        if (file) this.noteFileChanged(file.path);
    }

    async capture(requestedFields?: readonly string[]): Promise<JsonObject> {
        const fields = requestedFields ? validateFields(requestedFields) : CONTEXT_FIELDS;
        const view = this.app.workspace.getActiveViewOfType(MarkdownView);
        const file = view?.file ?? this.app.workspace.getActiveFile();
        if (!file || !safeRelativePath(file.path)) return emptyContext();
        const saved = await this.app.vault.cachedRead(file);
        const editor = view?.editor;
        const live = editor?.getValue() ?? saved;
        const context: JsonObject = emptyContext();
        if (fields.has("activeFile")) {
            context.activeFile = file.path;
            context.activeFileHash = digest(live);
            context.activeFileRevision = this.fileRevisions.get(file.path) ?? 0;
        }
        if (fields.has("unsavedState")) context.hasUnsavedChanges = live !== saved;
        if (editor && fields.has("selection")) {
            const text = editor.getSelection();
            const from = editor.getCursor("from");
            const to = editor.getCursor("to");
            if (text.length <= MAX_SELECTION_CHARS) {
                context.selection = {
                    text,
                    anchorOffset: editor.posToOffset(from),
                    headOffset: editor.posToOffset(to),
                };
                context.selectionRevision = this.selectionRevision;
            }
        }
        if (editor && fields.has("cursor")) context.cursorOffset = editor.posToOffset(editor.getCursor());
        if (fields.has("metadata") || fields.has("backlinks")) {
            context.metadataCacheRevision = this.metadataRevision;
            const cache = this.app.metadataCache.getFileCache(file);
            if (fields.has("metadata")) context.metadata = this.captureMetadata(file, cache);
            if (fields.has("backlinks")) context.backlinks = this.captureBacklinks(file);
        }
        return context;
    }

    async handleContextGet(params: JsonObject, signal: AbortSignal): Promise<JsonObject> {
        signal.throwIfAborted();
        exactKeys(params, ["requestId", "runId", "fields", "deadline"]);
        requireOpaque(params.requestId, "requestId");
        requireOpaque(params.runId, "runId");
        if (typeof params.deadline !== "string" || !Number.isFinite(Date.parse(params.deadline))) {
            throw new Error("client context deadline is invalid");
        }
        if (Date.now() >= Date.parse(params.deadline)) throw timeoutError();
        if (!Array.isArray(params.fields)) throw new Error("client context fields must be an array");
        const fields = params.fields.map((field) => {
            if (typeof field !== "string") throw new Error("client context field must be text");
            return field;
        });
        const context = await this.capture(fields);
        signal.throwIfAborted();
        return { context, capturedAt: new Date().toISOString() };
    }

    private captureMetadata(file: TFile, cache: CachedMetadata | null): JsonObject {
        const resolvedLinks = new Set<string>();
        const unresolvedLinks = new Set<string>();
        for (const link of cache?.links ?? []) {
            const destination = this.app.metadataCache.getFirstLinkpathDest(link.link, file.path);
            if (destination && safeRelativePath(destination.path)) resolvedLinks.add(destination.path);
            else if (safeMetadataText(link.link)) unresolvedLinks.add(link.link);
        }
        for (const link of Object.keys(this.app.metadataCache.unresolvedLinks[file.path] ?? {})) {
            if (safeMetadataText(link)) unresolvedLinks.add(link);
        }
        const tags = new Set<string>();
        for (const item of cache?.tags ?? []) if (safeTag(item.tag)) tags.add(item.tag);
        for (const item of frontmatterTags(cache?.frontmatter)) if (safeTag(item)) tags.add(item);
        return {
            frontmatter: sanitizeFrontmatter(cache?.frontmatter),
            tags: boundedSorted(tags, 1024),
            links: boundedSorted(resolvedLinks, MAX_METADATA_ITEMS),
            unresolvedLinks: boundedSorted(unresolvedLinks, MAX_METADATA_ITEMS),
        };
    }

    private captureBacklinks(file: TFile): JsonObject[] {
        const rows: JsonObject[] = [];
        for (const [source, destinations] of Object.entries(this.app.metadataCache.resolvedLinks)) {
            const count = destinations[file.path];
            if (safeRelativePath(source) && Number.isSafeInteger(count) && count > 0) rows.push({ path: source, count });
        }
        rows.sort((left, right) => String(left.path).localeCompare(String(right.path)));
        if (rows.length > MAX_METADATA_ITEMS) throw new Error("active file has too many backlinks for Client Context");
        return rows;
    }
}

function emptyContext(): JsonObject {
    return {
        activeFile: null,
        activeFileHash: null,
        activeFileRevision: null,
        selection: null,
        selectionRevision: null,
        cursorOffset: null,
        hasUnsavedChanges: false,
        metadataCacheRevision: null,
        metadata: null,
        backlinks: null,
    };
}

function sanitizeFrontmatter(value: unknown): JsonObject {
    if (!value || typeof value !== "object" || Array.isArray(value)) return {};
    const entries = Object.entries(value as Record<string, unknown>)
        .filter(([key]) => key !== "position")
        .sort(([left], [right]) => left.localeCompare(right));
    if (entries.length > MAX_FRONTMATTER_KEYS) throw new Error("frontmatter contains too many keys for Client Context");
    const output: JsonObject = {};
    for (const [key, item] of entries) {
        if (!safeMetadataText(key)) throw new Error("frontmatter key is invalid for Client Context");
        output[key] = sanitizeJson(item, 0);
    }
    return output;
}

function sanitizeJson(value: unknown, depth: number): JsonValue {
    if (depth > MAX_METADATA_DEPTH) throw new Error("frontmatter nesting exceeds Client Context limit");
    if (value === null || typeof value === "boolean") return value;
    if (typeof value === "number") {
        if (!Number.isFinite(value)) throw new Error("frontmatter contains a non-finite number");
        return value;
    }
    if (typeof value === "string") {
        if (!safeMetadataText(value)) throw new Error("frontmatter text is invalid for Client Context");
        return value;
    }
    if (Array.isArray(value)) {
        if (value.length > MAX_FRONTMATTER_KEYS) throw new Error("frontmatter array exceeds Client Context limit");
        return value.map((item) => sanitizeJson(item, depth + 1));
    }
    if (value && typeof value === "object") {
        const entries = Object.entries(value as Record<string, unknown>).sort(([left], [right]) => left.localeCompare(right));
        if (entries.length > MAX_FRONTMATTER_KEYS) throw new Error("frontmatter object exceeds Client Context limit");
        const output: JsonObject = {};
        for (const [key, item] of entries) {
            if (!safeMetadataText(key)) throw new Error("frontmatter object key is invalid for Client Context");
            output[key] = sanitizeJson(item, depth + 1);
        }
        return output;
    }
    throw new Error("frontmatter contains an unsupported value");
}

function frontmatterTags(frontmatter: Record<string, unknown> | undefined): string[] {
    if (!frontmatter) return [];
    const raw = frontmatter.tags ?? frontmatter.tag;
    if (typeof raw === "string") return raw.split(/[ ,]+/).filter(Boolean).map(normalizeTag);
    if (!Array.isArray(raw)) return [];
    return raw.filter((item): item is string => typeof item === "string").map(normalizeTag);
}

function normalizeTag(value: string): string {
    return value.startsWith("#") ? value : `#${value}`;
}

function safeTag(value: string): boolean {
    return /^#[^\s#\0]{1,255}$/.test(value);
}

function safeMetadataText(value: string): boolean {
    return value.length <= MAX_METADATA_TEXT && !value.includes("\0");
}

function boundedSorted(values: Set<string>, maximum: number): string[] {
    if (values.size > maximum) throw new Error("metadata collection exceeds Client Context limit");
    return [...values].sort();
}

function validateFields(values: readonly string[]): Set<string> {
    if (values.length < 1 || values.length > CONTEXT_FIELDS.size || new Set(values).size !== values.length) {
        throw new Error("client context fields must be a unique non-empty subset");
    }
    for (const field of values) if (!CONTEXT_FIELDS.has(field)) throw new Error(`unsupported client context field: ${field}`);
    return new Set(values);
}

function exactKeys(value: JsonObject, expected: string[]): void {
    const keys = Object.keys(value);
    if (keys.length !== expected.length || expected.some((key) => !keys.includes(key))) {
        throw new Error("client context request contains unexpected fields");
    }
}

function requireOpaque(value: JsonValue | undefined, name: string): string {
    if (typeof value !== "string" || !/^[A-Za-z0-9][A-Za-z0-9_-]{0,127}$/.test(value)) {
        throw new Error(`${name} is invalid`);
    }
    return value;
}

function safeRelativePath(path: string): boolean {
    return path.length > 0 && path.length <= 1024 && !path.startsWith("/") && !path.includes("\\") &&
        !path.includes("\0") && path.split("/").every((part) => part !== "" && part !== "." && part !== "..");
}

function digest(value: string): string {
    return `sha256:${createHash("sha256").update(value, "utf8").digest("hex")}`;
}

function timeoutError(): Error {
    const error = new Error("client context request deadline expired");
    error.name = "TimeoutError";
    return error;
}
