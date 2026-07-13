import { createHash } from "node:crypto";
import type { TFile, Vault } from "obsidian";
import type {
  AgentRunEvent,
  LocalToolResultPayload,
  VaultToolErrorCode,
} from "@offeragent/protocol";

const MAX_LIST_RESULTS = 100;
const DEFAULT_LIST_RESULTS = 50;
const MAX_READ_LINES = 200;
const MAX_READ_BYTES = 32_768;
const MAX_PATH_LENGTH = 512;
const EXCLUDED_SEGMENTS = new Set([".git", ".obsidian", ".codex", "node_modules"]);

type VaultToolCall = Extract<AgentRunEvent, { type: "tool_call.requested" }>;
type VaultApi = Pick<Vault, "cachedRead" | "getFiles">;

function failure(code: VaultToolErrorCode, message: string): LocalToolResultPayload {
  return { ok: false, error: { code, message } };
}

function safePath(value: unknown, allowEmpty = false): string | undefined {
  if (typeof value !== "string") return allowEmpty && value === undefined ? "" : undefined;
  const candidate = value.trim();
  if (allowEmpty && !candidate) return "";
  if (
    !candidate ||
    candidate.length > MAX_PATH_LENGTH ||
    candidate.includes("\\") ||
    candidate.startsWith("/") ||
    /^[A-Za-z]:/.test(candidate)
  ) {
    return undefined;
  }
  const segments = candidate.split("/");
  if (
    segments.some(
      (segment) =>
        !segment ||
        segment === "." ||
        segment === ".." ||
        segment.startsWith(".") ||
        EXCLUDED_SEGMENTS.has(segment),
    ) ||
    candidate.toLowerCase() === "agent.md"
  ) {
    return undefined;
  }
  return segments.join("/");
}

function fileVersion(file: TFile): string {
  return `mtime:${file.stat.mtime}:size:${file.stat.size}`;
}

function contentHash(content: string): string {
  return `sha256:${createHash("sha256").update(content, "utf8").digest("hex")}`;
}

function isReadableFile(file: TFile): boolean {
  return file.extension === "md" || file.extension === "txt";
}

export class ObsidianVaultToolAdapter {
  readonly #vault: VaultApi;

  constructor(vault: VaultApi) {
    this.#vault = vault;
  }

  async execute(call: VaultToolCall): Promise<LocalToolResultPayload> {
    try {
      if (call.tool.name === "vault_list") return await this.#list(call.tool.arguments);
      return await this.#read(call.tool.arguments);
    } catch (error) {
      return failure(
        "tool_error",
        error instanceof Error ? error.message : "The Vault tool could not complete the request.",
      );
    }
  }

  async #list(arguments_: unknown): Promise<LocalToolResultPayload> {
    if (!arguments_ || typeof arguments_ !== "object" || Array.isArray(arguments_)) {
      return failure("request_too_large", "vault_list arguments must be an object.");
    }
    const input = arguments_ as { directory?: unknown; limit?: unknown };
    const directory = safePath(input.directory, true);
    if (directory === undefined) {
      return failure("invalid_path", "The requested Vault directory is invalid or excluded.");
    }
    const limit = input.limit === undefined ? DEFAULT_LIST_RESULTS : input.limit;
    if (!Number.isInteger(limit) || (limit as number) < 1 || (limit as number) > MAX_LIST_RESULTS) {
      return failure("request_too_large", `vault_list limit must be between 1 and ${MAX_LIST_RESULTS}.`);
    }
    const prefix = directory ? `${directory}/` : "";
    const candidates = this.#vault
      .getFiles()
      .filter((file) => isReadableFile(file) && safePath(file.path) && file.path.startsWith(prefix))
      .sort((left, right) => left.path.localeCompare(right.path));
    const selected = candidates.slice(0, limit as number);
    const entries = await Promise.all(
      selected.map(async (file) => {
        const content = await this.#vault.cachedRead(file);
        return {
          path: file.path,
          modifiedVersion: fileVersion(file),
          contentHash: contentHash(content),
        };
      }),
    );
    return {
      ok: true,
      value: {
        type: "vault_list",
        entries,
        truncated: candidates.length > entries.length,
      },
    };
  }

  async #read(arguments_: unknown): Promise<LocalToolResultPayload> {
    if (!arguments_ || typeof arguments_ !== "object" || Array.isArray(arguments_)) {
      return failure("request_too_large", "vault_read arguments must be an object.");
    }
    const input = arguments_ as { path?: unknown; lineStart?: unknown; lineEnd?: unknown };
    const requestedPath = safePath(input.path);
    if (!requestedPath) {
      return failure("invalid_path", "The requested Vault path is invalid or excluded.");
    }
    const file = this.#vault.getFiles().find((candidate) => candidate.path === requestedPath);
    if (!file || !isReadableFile(file)) {
      return failure("not_found", `Vault file '${requestedPath}' does not exist.`);
    }
    const lineStart = input.lineStart === undefined ? 1 : input.lineStart;
    const requestedEnd = input.lineEnd;
    if (
      !Number.isInteger(lineStart) ||
      (lineStart as number) < 1 ||
      (requestedEnd !== undefined &&
        (!Number.isInteger(requestedEnd) || (requestedEnd as number) < (lineStart as number)))
    ) {
      return failure("request_too_large", "vault_read line range must use positive ordered integers.");
    }
    if (
      requestedEnd !== undefined &&
      (requestedEnd as number) - (lineStart as number) + 1 > MAX_READ_LINES
    ) {
      return failure("request_too_large", `vault_read accepts at most ${MAX_READ_LINES} lines.`);
    }
    const fullContent = await this.#vault.cachedRead(file);
    const lines = fullContent.split(/\r?\n/);
    if ((lineStart as number) > Math.max(lines.length, 1)) {
      return failure("not_found", `Vault file '${requestedPath}' has no line ${lineStart}.`);
    }
    const lineEnd = Math.min(
      requestedEnd === undefined ? (lineStart as number) + MAX_READ_LINES - 1 : (requestedEnd as number),
      lines.length,
    );
    const content = lines.slice((lineStart as number) - 1, lineEnd).join("\n");
    if (Buffer.byteLength(content, "utf8") > MAX_READ_BYTES) {
      return failure("request_too_large", `vault_read output exceeds ${MAX_READ_BYTES} UTF-8 bytes.`);
    }
    return {
      ok: true,
      value: {
        type: "vault_read",
        path: file.path,
        lineStart: lineStart as number,
        lineEnd,
        modifiedVersion: fileVersion(file),
        contentHash: contentHash(fullContent),
        content,
        truncated: lineEnd < lines.length,
      },
    };
  }
}

export { DEFAULT_LIST_RESULTS, MAX_LIST_RESULTS, MAX_READ_BYTES, MAX_READ_LINES };
