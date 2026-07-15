import { createHash } from "node:crypto";
import { lstat, readdir, readFile, realpath, stat } from "node:fs/promises";
import path from "node:path";
import type { TFile, Vault } from "obsidian";
import type {
  AgentRunEvent,
  LocalToolResultPayload,
  ProjectEvidenceEntry,
  VaultToolErrorCode,
} from "@offeragent/protocol";

const REGISTRY_PATH = "projects/index.md";
const MAX_CONTROL_BYTES = 32_768;
const MAX_PATH_LENGTH = 512;
const MAX_FILE_BYTES = 1_048_576;
const MAX_SCANNED_FILES = 2_000;
const MAX_SCAN_BYTES = 32 * 1_048_576;
const DEFAULT_LIST_LIMIT = 50;
const MAX_LIST_LIMIT = 100;
const DEFAULT_SEARCH_LIMIT = 10;
const MAX_SEARCH_LIMIT = 20;
const MAX_QUERY_BYTES = 512;
const MAX_READ_LINES = 200;
const MAX_READ_BYTES = 32_768;
const MAX_SNIPPET_BYTES = 512;

const ALLOWED_EXTENSIONS = new Set([
  ".c", ".cc", ".cjs", ".cpp", ".cs", ".css", ".go", ".graphql", ".h", ".hpp",
  ".html", ".ini", ".java", ".js", ".json", ".jsx", ".kt", ".kts", ".md", ".mjs",
  ".php", ".properties", ".proto", ".py", ".rb", ".rs", ".scala", ".scss", ".sh",
  ".sql", ".swift", ".toml", ".ts", ".tsx", ".txt", ".xml", ".yaml", ".yml",
]);
const EXCLUDED_SEGMENTS = new Set([
  ".git", ".hg", ".svn", ".cache", ".idea", ".vscode", ".venv", "__pycache__",
  "bin", "build", "coverage", "dist", "env", "gen", "generated", "node_modules", "obj", "out", "target",
  "vendor", "venv",
]);
const SECRET_SEGMENTS = new Set([".aws", ".azure", ".gnupg", ".ssh", "secrets"]);
const SECRET_FILE = /^(?:\.env(?:\..+)?|credentials?(?:\..+)?|id_(?:rsa|dsa|ecdsa|ed25519)(?:\.pub)?|secrets?(?:\..+)?|.*\.(?:key|pem|p12|pfx))$/iu;
const GENERATED_FILE = /(?:^(?:package-lock\.json|pnpm-lock\.yaml)$|\.generated\.|_generated\.|\.map$|\.min\.(?:css|js)$)/iu;

type ProjectToolCall = Extract<AgentRunEvent, { type: "tool_call.requested" }>;
type RegistryVault = Pick<Vault, "cachedRead" | "getFiles">;

interface RegisteredProject {
  id: string;
  root: string;
}

interface EligibleFile {
  absolutePath: string;
  contentHash: string;
  modifiedVersion: string;
  path: string;
}

type ScanResult =
  | { ok: true; files: EligibleFile[]; truncated: boolean }
  | { ok: false; result: LocalToolResultPayload };

function failure(
  code: VaultToolErrorCode,
  message: string,
): Extract<LocalToolResultPayload, { ok: false }> {
  return { ok: false, error: { code, message } };
}

function projectId(value: unknown): string | undefined {
  return typeof value === "string" && /^[a-z0-9][a-z0-9_-]{0,63}$/u.test(value)
    ? value
    : undefined;
}

function boundedInteger(value: unknown, fallback: number, maximum: number): number | undefined {
  if (value === undefined) return fallback;
  return Number.isInteger(value) && Number(value) >= 1 && Number(value) <= maximum
    ? Number(value)
    : undefined;
}

function safeRelativePath(value: unknown, allowEmpty = false): string | undefined {
  if (value === undefined && allowEmpty) return "";
  if (typeof value !== "string") return undefined;
  const candidate = value.trim();
  if (allowEmpty && !candidate) return "";
  if (
    !candidate || candidate.length > MAX_PATH_LENGTH || candidate.includes("\\") ||
    candidate.startsWith("/") || path.isAbsolute(candidate) || /^[A-Za-z]:/u.test(candidate)
  ) return undefined;
  const segments = candidate.split("/");
  if (segments.some((segment) => !segment || segment === "." || segment === "..")) return undefined;
  return segments.join("/");
}

function isContained(root: string, target: string): boolean {
  const relative = path.relative(root, target);
  return relative === "" ||
    (!path.isAbsolute(relative) && relative !== ".." && !relative.startsWith(`..${path.sep}`));
}

function isSameRelativeIdentity(requested: string, canonical: string): boolean {
  return process.platform === "win32"
    ? requested.toLowerCase() === canonical.toLowerCase()
    : requested === canonical;
}

function isEligibleDirectory(relativePath: string): boolean {
  if (!relativePath) return true;
  const segments = relativePath.split("/");
  const lowered = segments.map((segment) => segment.toLowerCase());
  return !lowered.some((segment) =>
    segment.startsWith(".") || EXCLUDED_SEGMENTS.has(segment) || SECRET_SEGMENTS.has(segment));
}

function isEligiblePath(relativePath: string): boolean {
  const segments = relativePath.split("/");
  const lowered = segments.map((segment) => segment.toLowerCase());
  const name = lowered.at(-1) ?? "";
  return (
    isEligibleDirectory(segments.slice(0, -1).join("/")) &&
    !name.startsWith(".") &&
    !SECRET_FILE.test(name) &&
    !GENERATED_FILE.test(name) &&
    ALLOWED_EXTENSIONS.has(path.extname(name).toLowerCase())
  );
}

function hash(content: Buffer | string): string {
  return `sha256:${createHash("sha256").update(content).digest("hex")}`;
}

function version(metadata: { mtimeMs: number; size: number }): string {
  return `mtime:${Math.trunc(metadata.mtimeMs)}:size:${metadata.size}`;
}

function boundedUtf8(buffer: Buffer): string | undefined {
  try {
    return new TextDecoder("utf-8", { fatal: true }).decode(buffer);
  } catch {
    return undefined;
  }
}

function truncateUtf8(value: string, maximumBytes: number): { content: string; truncated: boolean } {
  if (Buffer.byteLength(value, "utf8") <= maximumBytes) return { content: value, truncated: false };
  let end = Math.min(value.length, maximumBytes);
  while (end > 0 && Buffer.byteLength(value.slice(0, end), "utf8") > maximumBytes) end -= 1;
  if (end > 0 && /[\uD800-\uDBFF]/u.test(value[end - 1] ?? "")) end -= 1;
  return { content: value.slice(0, end), truncated: true };
}

function frontmatterValue(content: string, key: string): string | undefined {
  if (!content.startsWith("---\n") && !content.startsWith("---\r\n")) return undefined;
  const normalized = content.replaceAll("\r\n", "\n");
  const end = normalized.indexOf("\n---", 4);
  if (end < 0) return undefined;
  for (const line of normalized.slice(4, end).split("\n")) {
    const separator = line.indexOf(":");
    if (separator < 0 || line.slice(0, separator).trim() !== key) continue;
    const value = line.slice(separator + 1).trim().replace(/^(?:"(.*)"|'(.*)')$/u, "$1$2");
    return value || undefined;
  }
  return undefined;
}

export class ProjectEvidenceAdapter {
  readonly #vault: RegistryVault;

  constructor(vault: RegistryVault) {
    this.#vault = vault;
  }

  async execute(call: ProjectToolCall): Promise<LocalToolResultPayload> {
    if (!["project_list", "project_search", "project_read"].includes(call.tool.name)) {
      return failure("tool_error", "Project Evidence received an unsupported tool call.");
    }
    const arguments_ = call.tool.arguments;
    if (!arguments_ || typeof arguments_ !== "object" || Array.isArray(arguments_)) {
      return failure("invalid_path", "Project Evidence arguments are invalid.");
    }
    const input = arguments_ as Record<string, unknown>;
    const id = projectId(input.projectId);
    if (!id) return failure("invalid_path", "Project ID is invalid.");
    const registration = await this.#registeredProject(id);
    if (!registration.ok) return registration.result;
    if (call.tool.name === "project_list") return this.#list(registration.project, input);
    if (call.tool.name === "project_search") return this.#search(registration.project, input);
    return this.#read(registration.project, input);
  }

  async #registeredProject(id: string): Promise<
    | { ok: true; project: RegisteredProject }
    | { ok: false; result: LocalToolResultPayload }
  > {
    const files = this.#vault.getFiles();
    const registry = files.find((candidate) => candidate.path === REGISTRY_PATH);
    if (!registry) {
      return { ok: false, result: failure("permission_denied", `Project '${id}' is not registered.`) };
    }
    const index = await this.#boundedVaultRead(registry);
    if (!index) return { ok: false, result: failure("malformed_control_file", "Project Registry is unreadable.") };
    const linkedDescriptors = new Set(
      [...index.matchAll(/\[\[([^\]|#]+)(?:#[^\]|]+)?(?:\|[^\]]+)?\]\]/gu)]
        .map((match) => match[1]?.trim())
        .filter((value): value is string => Boolean(value))
        .map((value) => value.endsWith(".md") ? value : `${value}.md`)
        .filter((value) => /^projects\/[^/.][^/]*\.md$/u.test(value) && value !== REGISTRY_PATH),
    );
    let registered: RegisteredProject | undefined;
    for (const descriptorPath of linkedDescriptors) {
      const descriptor = files.find((candidate) => candidate.path === descriptorPath);
      if (!descriptor) continue;
      const content = await this.#boundedVaultRead(descriptor);
      if (!content) continue;
      if (frontmatterValue(content, "project-id") !== id) continue;
      const configuredRoot = frontmatterValue(content, "project-root");
      if (!configuredRoot || !path.isAbsolute(configuredRoot)) {
        return { ok: false, result: failure("malformed_control_file", "Registered project root is invalid.") };
      }
      try {
        const root = await realpath(configuredRoot);
        const metadata = await stat(root);
        if (!metadata.isDirectory()) throw new Error("not a directory");
        if (registered) {
          return { ok: false, result: failure("malformed_control_file", `Project '${id}' has conflicting Registry entries.`) };
        }
        registered = { id, root };
      } catch {
        return { ok: false, result: failure("not_found", `Registered project '${id}' is unavailable.`) };
      }
    }
    if (registered) return { ok: true, project: registered };
    return { ok: false, result: failure("permission_denied", `Project '${id}' is not registered.`) };
  }

  async #boundedVaultRead(file: TFile): Promise<string | undefined> {
    if (file.stat.size > MAX_CONTROL_BYTES) return undefined;
    try {
      const content = await this.#vault.cachedRead(file);
      return Buffer.byteLength(content, "utf8") <= MAX_CONTROL_BYTES ? content : undefined;
    } catch {
      return undefined;
    }
  }

  async #list(project: RegisteredProject, input: Record<string, unknown>): Promise<LocalToolResultPayload> {
    const directory = safeRelativePath(input.directory, true);
    const limit = boundedInteger(input.limit, DEFAULT_LIST_LIMIT, MAX_LIST_LIMIT);
    if (directory === undefined || limit === undefined) return failure("invalid_path", "Project list bounds are invalid.");
    if (!isEligibleDirectory(directory)) return failure("permission_denied", "Project directory is excluded by read policy.");
    let scanned: ScanResult;
    try {
      scanned = await this.#scan(project, directory);
    } catch {
      return failure("permission_denied", "Registered project sources could not be listed.");
    }
    if (!scanned.ok) return scanned.result;
    return {
      ok: true,
      value: {
        type: "project_list",
        projectId: project.id,
        entries: scanned.files.slice(0, limit).map(({ absolutePath: _absolutePath, ...entry }) => entry),
        truncated: scanned.truncated || scanned.files.length > limit,
      },
    };
  }

  async #search(project: RegisteredProject, input: Record<string, unknown>): Promise<LocalToolResultPayload> {
    const query = typeof input.query === "string" ? input.query.trim() : "";
    const limit = boundedInteger(input.limit, DEFAULT_SEARCH_LIMIT, MAX_SEARCH_LIMIT);
    if (!query || Buffer.byteLength(query, "utf8") > MAX_QUERY_BYTES || limit === undefined) {
      return failure("invalid_path", "Project search bounds are invalid.");
    }
    let scanned: ScanResult;
    try {
      scanned = await this.#scan(project, "");
    } catch {
      return failure("permission_denied", "Registered project sources could not be searched.");
    }
    if (!scanned.ok) return scanned.result;
    const terms = query.toLocaleLowerCase().split(/\s+/u).filter(Boolean);
    const entries = [];
    for (const file of scanned.files) {
      if (entries.length >= limit) break;
      let buffer: Buffer;
      try {
        buffer = await readFile(file.absolutePath);
        if (
          version(await stat(file.absolutePath)) !== file.modifiedVersion ||
          hash(buffer) !== file.contentHash
        ) continue;
      } catch {
        continue;
      }
      const content = boundedUtf8(buffer);
      if (content === undefined) continue;
      const lines = content.replaceAll("\r\n", "\n").split("\n");
      const matching = lines.findIndex((line) => terms.every((term) => line.toLocaleLowerCase().includes(term)));
      if (matching < 0) continue;
      const snippet = truncateUtf8(lines[matching] ?? "", MAX_SNIPPET_BYTES);
      entries.push({
        path: file.path,
        modifiedVersion: file.modifiedVersion,
        contentHash: file.contentHash,
        snippets: [{ content: snippet.content, lineStart: matching + 1, lineEnd: matching + 1, truncated: snippet.truncated }],
      });
    }
    return {
      ok: true,
      value: {
        type: "project_search",
        projectId: project.id,
        entries,
        truncated: scanned.truncated || entries.length >= limit,
      },
    };
  }

  async #read(project: RegisteredProject, input: Record<string, unknown>): Promise<LocalToolResultPayload> {
    const relativePath = safeRelativePath(input.path);
    const lineStart = boundedInteger(input.lineStart, 1, Number.MAX_SAFE_INTEGER);
    const requestedEnd = boundedInteger(input.lineEnd, Number.MAX_SAFE_INTEGER, Number.MAX_SAFE_INTEGER);
    if (!relativePath || lineStart === undefined || requestedEnd === undefined || requestedEnd < lineStart) {
      return failure("invalid_path", "Project read path or line range is invalid.");
    }
    if (!isEligiblePath(relativePath)) return failure("permission_denied", "Project source is excluded by read policy.");
    const absolutePath = path.resolve(project.root, ...relativePath.split("/"));
    if (!isContained(project.root, absolutePath)) return failure("invalid_path", "Project path escapes the registered root.");
    let canonical: string;
    let metadata;
    try {
      canonical = await realpath(absolutePath);
      if (!isContained(project.root, canonical)) return failure("permission_denied", "Project source escapes the registered root.");
      const canonicalRelative = path.relative(project.root, canonical).split(path.sep).join("/");
      if (!isSameRelativeIdentity(relativePath, canonicalRelative)) {
        return failure("permission_denied", "Project source aliases are not readable.");
      }
      if (!isEligiblePath(canonicalRelative)) {
        return failure("permission_denied", "Project source resolves to an excluded path.");
      }
      metadata = await stat(canonical);
    } catch {
      return failure("not_found", "Project source was not found.");
    }
    if (!metadata.isFile()) return failure("unreadable_content", "Project source is not a regular file.");
    if (metadata.size > MAX_FILE_BYTES) return failure("response_too_large", "Project source exceeds the read limit.");
    let buffer: Buffer;
    let observed: Awaited<ReturnType<typeof stat>>;
    try {
      buffer = await readFile(canonical);
      observed = await stat(canonical);
    } catch {
      return failure("permission_denied", "Registered project source could not be read.");
    }
    if (observed.size !== metadata.size || observed.mtimeMs !== metadata.mtimeMs) {
      return failure("stale_evidence", "Project source changed during the bounded read.");
    }
    const content = boundedUtf8(buffer);
    if (content === undefined || content.includes("\u0000")) return failure("unreadable_content", "Project source is not supported UTF-8 text.");
    const lines = content.replaceAll("\r\n", "\n").split("\n");
    if (lineStart > lines.length) return failure("invalid_path", "Project read starts beyond the source.");
    const boundedEnd = Math.min(requestedEnd, lineStart + MAX_READ_LINES - 1, lines.length);
    const selected = lines.slice(lineStart - 1, boundedEnd).join("\n");
    const bounded = truncateUtf8(selected, MAX_READ_BYTES);
    const returnedLines = bounded.content ? bounded.content.split("\n").length : 0;
    const lineEnd = lineStart + Math.max(0, returnedLines - 1);
    return {
      ok: true,
      value: {
        type: "project_read",
        projectId: project.id,
        path: relativePath,
        evidencePath: `project/${project.id}/${relativePath}`,
        lineStart,
        lineEnd,
        modifiedVersion: version(observed),
        contentHash: hash(buffer),
        content: bounded.content,
        truncated: bounded.truncated || boundedEnd < Math.min(requestedEnd, lines.length) || boundedEnd < lines.length,
      },
    };
  }

  async #scan(project: RegisteredProject, directory: string): Promise<ScanResult> {
    const start = path.resolve(project.root, ...directory.split("/").filter(Boolean));
    if (!isContained(project.root, start)) return { ok: false, result: failure("invalid_path", "Project directory escapes the registered root.") };
    try {
      const canonical = await realpath(start);
      if (!isContained(project.root, canonical)) return { ok: false, result: failure("permission_denied", "Project directory escapes the registered root.") };
      const canonicalDirectory = path.relative(project.root, canonical).split(path.sep).join("/");
      if (!isSameRelativeIdentity(directory, canonicalDirectory)) {
        return { ok: false, result: failure("permission_denied", "Project directory aliases are not readable.") };
      }
      if (!isEligibleDirectory(canonicalDirectory)) {
        return { ok: false, result: failure("permission_denied", "Project directory resolves to an excluded path.") };
      }
      if (!(await stat(canonical)).isDirectory()) return { ok: false, result: failure("invalid_path", "Project directory is not a directory.") };
    } catch {
      return { ok: false, result: failure("not_found", "Project directory was not found.") };
    }
    const files: EligibleFile[] = [];
    const pending = [start];
    let visited = 0;
    let scannedBytes = 0;
    while (pending.length > 0 && visited < MAX_SCANNED_FILES && scannedBytes < MAX_SCAN_BYTES) {
      const current = pending.shift()!;
      const entries = (await readdir(current, { withFileTypes: true })).sort((left, right) => left.name.localeCompare(right.name));
      for (const entry of entries) {
        visited += 1;
        if (visited > MAX_SCANNED_FILES) break;
        const absolutePath = path.join(current, entry.name);
        const relativePath = path.relative(project.root, absolutePath).split(path.sep).join("/");
        if (entry.isSymbolicLink()) continue;
        if (entry.isDirectory()) {
          const segment = entry.name.toLowerCase();
          if (!entry.name.startsWith(".") && !EXCLUDED_SEGMENTS.has(segment) && !SECRET_SEGMENTS.has(segment)) pending.push(absolutePath);
          continue;
        }
        if (!entry.isFile() || !isEligiblePath(relativePath)) continue;
        const metadata = await lstat(absolutePath);
        if (metadata.size > MAX_FILE_BYTES) continue;
        if (scannedBytes + metadata.size > MAX_SCAN_BYTES) {
          scannedBytes = MAX_SCAN_BYTES;
          break;
        }
        const buffer = await readFile(absolutePath);
        const observed = await lstat(absolutePath);
        if (observed.size !== metadata.size || observed.mtimeMs !== metadata.mtimeMs) continue;
        const content = boundedUtf8(buffer);
        if (content === undefined || content.includes("\u0000")) continue;
        scannedBytes += buffer.byteLength;
        files.push({ absolutePath, path: relativePath, modifiedVersion: version(observed), contentHash: hash(buffer) });
        if (visited >= MAX_SCANNED_FILES || scannedBytes >= MAX_SCAN_BYTES) break;
      }
    }
    files.sort((left, right) => left.path.localeCompare(right.path));
    return {
      ok: true,
      files,
      truncated: pending.length > 0 || visited >= MAX_SCANNED_FILES || scannedBytes >= MAX_SCAN_BYTES,
    };
  }
}
