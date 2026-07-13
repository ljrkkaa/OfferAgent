import { createHash } from "node:crypto";
import { realpath } from "node:fs/promises";
import path from "node:path";
import type { MetadataCache, TFile, Vault } from "obsidian";
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
const DEFAULT_SEARCH_RESULTS = 10;
const MAX_SEARCH_RESULTS = 20;
const DEFAULT_SEARCH_SNIPPETS = 2;
const MAX_SEARCH_SNIPPETS = 3;
const DEFAULT_SEARCH_SNIPPET_BYTES = 240;
const MAX_SEARCH_SNIPPET_BYTES = 512;
const MAX_SEARCH_QUERY_BYTES = 512;
const MAX_CONTROL_FILE_BYTES = 32_768;
const MAX_SKILL_NAME_LENGTH = 64;
const EXCLUDED_SEGMENTS = new Set([".git", ".obsidian", ".codex", "node_modules"]);

type VaultToolCall = Extract<AgentRunEvent, { type: "tool_call.requested" }>;
type VaultApi = Pick<Vault, "cachedRead" | "getFiles">;
type MetadataApi = Pick<MetadataCache, "getFileCache">;
type CanonicalizeVaultPath = (vaultPath: string) => Promise<string>;

function failure(
  code: VaultToolErrorCode,
  message: string,
): Extract<LocalToolResultPayload, { ok: false }> {
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
        EXCLUDED_SEGMENTS.has(segment.toLowerCase()),
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
  const extension = file.extension.toLowerCase();
  return extension === "md" || extension === "txt";
}

function defaultCanonicalizer(vault: VaultApi): CanonicalizeVaultPath | undefined {
  const adapter = (vault as VaultApi & { adapter?: { getBasePath?: () => string } }).adapter;
  if (!adapter || typeof adapter.getBasePath !== "function") return undefined;
  const basePath = adapter.getBasePath();
  return (vaultPath) => realpath(path.resolve(basePath, vaultPath));
}

function isContained(root: string, target: string): boolean {
  const relative = path.relative(root, target);
  return (
    relative === "" ||
    (!path.isAbsolute(relative) && relative !== ".." && !relative.startsWith(`..${path.sep}`))
  );
}

function normalizeSkillResource(value: unknown): string | undefined {
  if (typeof value !== "string") return undefined;
  let candidate = value.trim();
  if (candidate.startsWith("<") && candidate.endsWith(">")) {
    candidate = candidate.slice(1, -1);
  }
  candidate = candidate.split("#", 1)[0];
  try {
    candidate = decodeURIComponent(candidate);
  } catch {
    return undefined;
  }
  if (
    !candidate ||
    candidate.length > MAX_PATH_LENGTH ||
    candidate.includes("\\") ||
    candidate.startsWith("/") ||
    /^[A-Za-z]:/.test(candidate) ||
    /^[a-z][a-z0-9+.-]*:/i.test(candidate)
  ) {
    return undefined;
  }
  const segments = candidate.split("/");
  if (
    segments.some(
      (segment) => !segment || segment === "." || segment === ".." || segment.startsWith("."),
    )
  ) {
    return undefined;
  }
  return segments.join("/");
}

function referencedResources(content: string): Set<string> {
  const resources = new Set<string>();
  const links = /\[[^\]]*\]\(([^)\s]+)(?:\s+[^)]*)?\)/g;
  for (const match of content.matchAll(links)) {
    const normalized = normalizeSkillResource(match[1]);
    if (normalized) resources.add(normalized);
  }
  return resources;
}

function occurrences(value: string, needles: string[]): number {
  return needles.reduce((total, needle) => {
    let count = 0;
    let offset = 0;
    while ((offset = value.indexOf(needle, offset)) !== -1) {
      count += 1;
      offset += Math.max(needle.length, 1);
    }
    return total + count;
  }, 0);
}

function metadataValues(value: unknown): string[] {
  if (typeof value === "string" || typeof value === "number" || typeof value === "boolean") {
    return [String(value)];
  }
  if (Array.isArray(value)) return value.flatMap(metadataValues);
  if (value && typeof value === "object") {
    return Object.values(value).flatMap(metadataValues);
  }
  return [];
}

function boundedUtf8(value: string, maximumBytes: number): { content: string; truncated: boolean } {
  if (Buffer.byteLength(value, "utf8") <= maximumBytes) {
    return { content: value, truncated: false };
  }
  let end = Math.min(value.length, maximumBytes);
  while (end > 0 && Buffer.byteLength(value.slice(0, end), "utf8") > maximumBytes) end -= 1;
  if (end > 0) {
    const trailingCodeUnit = value.charCodeAt(end - 1);
    if (trailingCodeUnit >= 0xd800 && trailingCodeUnit <= 0xdbff) end -= 1;
  }
  return { content: value.slice(0, end), truncated: true };
}

export class ObsidianVaultToolAdapter {
  readonly #canonicalize?: CanonicalizeVaultPath;
  readonly #metadata?: MetadataApi;
  readonly #vault: VaultApi;

  constructor(
    vault: VaultApi,
    metadata?: MetadataApi,
    canonicalize: CanonicalizeVaultPath | undefined = defaultCanonicalizer(vault),
  ) {
    this.#vault = vault;
    this.#metadata = metadata;
    this.#canonicalize = canonicalize;
  }

  async execute(call: VaultToolCall): Promise<LocalToolResultPayload> {
    try {
      if (call.tool.name === "vault_list") return await this.#list(call.tool.arguments);
      if (call.tool.name === "vault_search") return await this.#search(call.tool.arguments);
      if (call.tool.name === "agent_contract_read") {
        return await this.#readAgentContract(call.tool.arguments);
      }
      if (call.tool.name === "skill_read") return await this.#readSkill(call.tool.arguments);
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

  async #search(arguments_: unknown): Promise<LocalToolResultPayload> {
    if (!arguments_ || typeof arguments_ !== "object" || Array.isArray(arguments_)) {
      return failure("request_too_large", "vault_search arguments must be an object.");
    }
    const input = arguments_ as {
      exactPhrase?: unknown;
      limit?: unknown;
      query?: unknown;
      snippetMaxBytes?: unknown;
      snippetsPerFile?: unknown;
    };
    const query = typeof input.query === "string" ? input.query.trim().toLowerCase() : "";
    if (!query || Buffer.byteLength(query, "utf8") > MAX_SEARCH_QUERY_BYTES) {
      return failure(
        "request_too_large",
        `vault_search query must be between 1 and ${MAX_SEARCH_QUERY_BYTES} UTF-8 bytes.`,
      );
    }
    if (input.exactPhrase !== undefined && typeof input.exactPhrase !== "boolean") {
      return failure("request_too_large", "vault_search exactPhrase must be a boolean.");
    }
    const limit = input.limit === undefined ? DEFAULT_SEARCH_RESULTS : input.limit;
    const snippetsPerFile =
      input.snippetsPerFile === undefined ? DEFAULT_SEARCH_SNIPPETS : input.snippetsPerFile;
    const snippetMaxBytes =
      input.snippetMaxBytes === undefined
        ? DEFAULT_SEARCH_SNIPPET_BYTES
        : input.snippetMaxBytes;
    if (!Number.isInteger(limit) || (limit as number) < 1 || (limit as number) > MAX_SEARCH_RESULTS) {
      return failure("request_too_large", `vault_search limit must be between 1 and ${MAX_SEARCH_RESULTS}.`);
    }
    if (
      !Number.isInteger(snippetsPerFile) ||
      (snippetsPerFile as number) < 1 ||
      (snippetsPerFile as number) > MAX_SEARCH_SNIPPETS
    ) {
      return failure(
        "request_too_large",
        `vault_search snippetsPerFile must be between 1 and ${MAX_SEARCH_SNIPPETS}.`,
      );
    }
    if (
      !Number.isInteger(snippetMaxBytes) ||
      (snippetMaxBytes as number) < 16 ||
      (snippetMaxBytes as number) > MAX_SEARCH_SNIPPET_BYTES
    ) {
      return failure(
        "request_too_large",
        `vault_search snippetMaxBytes must be between 16 and ${MAX_SEARCH_SNIPPET_BYTES}.`,
      );
    }

    const needles = input.exactPhrase === true ? [query] : query.split(/\s+/).filter(Boolean);
    const matches = (value: string): boolean => needles.some((needle) => value.includes(needle));
    const candidates = [];
    for (const file of this.#vault.getFiles()) {
      if (!isReadableFile(file) || !safePath(file.path)) continue;
      const content = await this.#vault.cachedRead(file);
      const normalizedContent = content.toLowerCase();
      const cache = this.#metadata?.getFileCache(file);
      const metadataText = [
        ...metadataValues(cache?.frontmatter),
        ...(cache?.headings
          ?.filter((heading) => heading.level === 1)
          .map((heading) => heading.heading) ?? []),
        ...(cache?.tags?.map((tag) => tag.tag) ?? []),
      ]
        .join("\n")
        .toLowerCase();
      const normalizedPath = file.path.toLowerCase();
      const matchTier: "body" | "metadata" | "path" | undefined = matches(normalizedPath)
        ? "path"
        : matches(metadataText)
          ? "metadata"
          : matches(normalizedContent)
            ? "body"
            : undefined;
      if (!matchTier) continue;
      const rankText =
        matchTier === "path" ? normalizedPath : matchTier === "metadata" ? metadataText : normalizedContent;
      const snippets = content
        .split(/\r?\n/)
        .map((line, index) => ({ line, normalized: line.toLowerCase(), lineNumber: index + 1 }))
        .filter(({ normalized }) => matches(normalized))
        .slice(0, snippetsPerFile as number)
        .map(({ line, lineNumber }) => {
          const bounded = boundedUtf8(line, snippetMaxBytes as number);
          return {
            lineStart: lineNumber,
            lineEnd: lineNumber,
            content: bounded.content,
            truncated: bounded.truncated,
          };
        });
      candidates.push({
        rank: matchTier === "path" ? 0 : matchTier === "metadata" ? 1 : 2,
        score: occurrences(rankText, needles),
        entry: {
          path: file.path,
          modifiedVersion: fileVersion(file),
          contentHash: contentHash(content),
          matchTier,
          snippets,
        },
      });
    }
    candidates.sort(
      (left, right) =>
        left.rank - right.rank ||
        right.score - left.score ||
        left.entry.path.localeCompare(right.entry.path),
    );
    const entries = candidates.slice(0, limit as number).map(({ entry }) => entry);
    return {
      ok: true,
      value: {
        type: "vault_search",
        entries,
        truncated: candidates.length > entries.length,
      },
    };
  }

  async #readAgentContract(arguments_: unknown): Promise<LocalToolResultPayload> {
    if (
      !arguments_ ||
      typeof arguments_ !== "object" ||
      Array.isArray(arguments_) ||
      Object.keys(arguments_).length > 0
    ) {
      return failure("request_too_large", "agent_contract_read accepts an empty object.");
    }
    const file = this.#vault.getFiles().find((candidate) => candidate.path === "agent.md");
    if (!file || !isReadableFile(file)) {
      return failure("not_found", "The root Agent Contract 'agent.md' does not exist.");
    }
    if (!this.#canonicalize) {
      return failure(
        "tool_error",
        "The Agent Contract path containment could not be verified safely.",
      );
    }
    try {
      const [vaultRoot, canonicalContract] = await Promise.all([
        this.#canonicalize(""),
        this.#canonicalize("agent.md"),
      ]);
      if (!isContained(vaultRoot, canonicalContract)) {
        return failure("invalid_path", "The Agent Contract resolves outside the Vault root.");
      }
    } catch {
      return failure("invalid_path", "The Agent Contract path could not be resolved safely.");
    }
    const content = await this.#vault.cachedRead(file);
    const invalid = this.#validateControlContent(content, "Agent Contract");
    if (invalid) return invalid;
    return {
      ok: true,
      value: {
        type: "agent_contract_read",
        path: "agent.md",
        modifiedVersion: fileVersion(file),
        contentHash: contentHash(content),
        content,
      },
    };
  }

  async #readSkill(arguments_: unknown): Promise<LocalToolResultPayload> {
    if (!arguments_ || typeof arguments_ !== "object" || Array.isArray(arguments_)) {
      return failure("request_too_large", "skill_read arguments must be an object.");
    }
    const input = arguments_ as { resource?: unknown; skill?: unknown };
    if (
      typeof input.skill !== "string" ||
      input.skill.length > MAX_SKILL_NAME_LENGTH ||
      !/^[A-Za-z0-9][A-Za-z0-9_-]*$/.test(input.skill)
    ) {
      return failure("invalid_path", "The requested Local Skill name is invalid.");
    }
    const skillRoot = `.codex/skills/${input.skill}`;
    const skillFile = this.#vault
      .getFiles()
      .find((candidate) => candidate.path === `${skillRoot}/SKILL.md`);
    if (!skillFile) {
      return failure("not_found", `Local Skill '${input.skill}' is not registered.`);
    }
    const skillContent = await this.#vault.cachedRead(skillFile);
    const invalidSkill = this.#validateControlContent(skillContent, `Local Skill '${input.skill}'`);
    if (invalidSkill) return invalidSkill;
    const resource =
      input.resource === undefined ? "SKILL.md" : normalizeSkillResource(input.resource);
    if (!resource) {
      return failure("invalid_path", "The requested Local Skill resource path is invalid.");
    }
    if (resource !== "SKILL.md" && !referencedResources(skillContent).has(resource)) {
      return failure(
        "invalid_path",
        `Local Skill resource '${resource}' is not directly referenced by SKILL.md.`,
      );
    }
    const resourcePath = `${skillRoot}/${resource}`;
    const resourceFile = this.#vault
      .getFiles()
      .find((candidate) => candidate.path === resourcePath);
    if (!resourceFile || !isReadableFile(resourceFile)) {
      return failure("not_found", `Local Skill resource '${resource}' does not exist.`);
    }
    if (!this.#canonicalize) {
      return failure("tool_error", "Local Skill path containment could not be verified safely.");
    }
    try {
      const [vaultRoot, canonicalSkillRoot, canonicalResource] = await Promise.all([
        this.#canonicalize(""),
        this.#canonicalize(skillRoot),
        this.#canonicalize(resourcePath),
      ]);
      if (
        !isContained(vaultRoot, canonicalSkillRoot) ||
        !isContained(canonicalSkillRoot, canonicalResource)
      ) {
        return failure("invalid_path", "The Local Skill resource resolves outside its owning Skill.");
      }
    } catch {
      return failure("invalid_path", "The Local Skill resource path could not be resolved safely.");
    }
    const content =
      resource === "SKILL.md" ? skillContent : await this.#vault.cachedRead(resourceFile);
    const invalidResource = this.#validateControlContent(
      content,
      `Local Skill resource '${resource}'`,
    );
    if (invalidResource) return invalidResource;
    return {
      ok: true,
      value: {
        type: "skill_read",
        skill: input.skill,
        resource,
        path: resourcePath,
        modifiedVersion: fileVersion(resourceFile),
        contentHash: contentHash(content),
        content,
      },
    };
  }

  #validateControlContent(
    content: string,
    label: string,
  ): Extract<LocalToolResultPayload, { ok: false }> | undefined {
    if (Buffer.byteLength(content, "utf8") > MAX_CONTROL_FILE_BYTES) {
      return failure(
        "request_too_large",
        `${label} exceeds ${MAX_CONTROL_FILE_BYTES} UTF-8 bytes.`,
      );
    }
    if (!content.trim() || content.includes("\0")) {
      return failure("malformed_control_file", `${label} is empty or malformed.`);
    }
    return undefined;
  }
}

export {
  DEFAULT_LIST_RESULTS,
  DEFAULT_SEARCH_RESULTS,
  DEFAULT_SEARCH_SNIPPET_BYTES,
  DEFAULT_SEARCH_SNIPPETS,
  MAX_LIST_RESULTS,
  MAX_READ_BYTES,
  MAX_READ_LINES,
  MAX_SEARCH_QUERY_BYTES,
  MAX_SEARCH_RESULTS,
  MAX_SEARCH_SNIPPET_BYTES,
  MAX_SEARCH_SNIPPETS,
  MAX_CONTROL_FILE_BYTES,
};
