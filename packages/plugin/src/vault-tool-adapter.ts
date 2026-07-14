import { createHash } from "node:crypto";
import { realpath } from "node:fs/promises";
import path from "node:path";
import type { MetadataCache, TFile, Vault } from "obsidian";
import type {
  AgentRunEvent,
  LocalToolResultPayload,
  MemoryTopicType,
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
const MAX_DAILY_TEMPLATE_BYTES = 32_768;
const DEFAULT_INTERVIEW_CATALOG_RESULTS = 10;
const MAX_INTERVIEW_CATALOG_RESULTS = 20;
const MAX_MEMORY_TOPICS = 100;
const MAX_MEMORY_READ_TOPICS = 5;
const MAX_MEMORY_TOPIC_BYTES = 32_768;
const MAX_MEMORY_READ_BYTES = 65_536;
const MAX_SKILL_NAME_LENGTH = 64;
const EXCLUDED_SEGMENTS = new Set([".git", ".obsidian", ".codex", "node_modules"]);
const MEMORY_TOPIC_PATH = /^memory\/(user|feedback|project|study)\/[^/.][^/]*\.md$/;

type VaultToolCall = Extract<AgentRunEvent, { type: "tool_call.requested" }>;
type VaultApi = Pick<Vault, "cachedRead" | "getFiles">;
type MetadataApi = Pick<MetadataCache, "getFileCache">;
type CanonicalizeVaultPath = (vaultPath: string) => Promise<string>;

export interface DailyNotesApi {
  formatDate(date: string, format: string): string;
  readConfiguration(): Promise<unknown>;
  resolveToday(): string;
}

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
  return async (vaultPath) => {
    let existingAncestor = path.resolve(basePath, vaultPath);
    const missingSegments: string[] = [];
    while (true) {
      try {
        const canonicalAncestor = await realpath(existingAncestor);
        return path.resolve(canonicalAncestor, ...missingSegments);
      } catch (error) {
        const code = (error as NodeJS.ErrnoException).code;
        if (code !== "ENOENT" && code !== "ENOTDIR") throw error;
        const parent = path.dirname(existingAncestor);
        if (parent === existingAncestor) throw error;
        missingSegments.unshift(path.basename(existingAncestor));
        existingAncestor = parent;
      }
    }
  };
}

function isContained(root: string, target: string): boolean {
  const relative = path.relative(root, target);
  return (
    relative === "" ||
    (!path.isAbsolute(relative) && relative !== ".." && !relative.startsWith(`..${path.sep}`))
  );
}

function isIsoDate(value: unknown): value is string {
  if (typeof value !== "string" || !/^\d{4}-\d{2}-\d{2}$/.test(value)) return false;
  const [year, month, day] = value.split("-").map(Number);
  const date = new Date(Date.UTC(year, month - 1, day));
  return (
    date.getUTCFullYear() === year &&
    date.getUTCMonth() === month - 1 &&
    date.getUTCDate() === day
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
  readonly #dailyNotes?: DailyNotesApi;
  readonly #vault: VaultApi;

  constructor(
    vault: VaultApi,
    metadata?: MetadataApi,
    canonicalize: CanonicalizeVaultPath | undefined = defaultCanonicalizer(vault),
    dailyNotes?: DailyNotesApi,
  ) {
    this.#vault = vault;
    this.#metadata = metadata;
    this.#canonicalize = canonicalize;
    this.#dailyNotes = dailyNotes;
  }

  async execute(call: VaultToolCall): Promise<LocalToolResultPayload> {
    try {
      if (call.tool.name === "vault_list") return await this.#list(call.tool.arguments);
      if (call.tool.name === "vault_search") return await this.#search(call.tool.arguments);
      if (call.tool.name === "daily_note_context") {
        return await this.#dailyNoteContext(call.tool.arguments);
      }
      if (call.tool.name === "interview_catalog") {
        return await this.#interviewCatalog(call.tool.arguments);
      }
      if (call.tool.name === "planning_memory_list") {
        return await this.#listPlanningMemory(call.tool.arguments);
      }
      if (call.tool.name === "planning_memory_read") {
        return await this.#readPlanningMemory(call.tool.arguments);
      }
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

  async #listPlanningMemory(arguments_: unknown): Promise<LocalToolResultPayload> {
    if (
      !arguments_ ||
      typeof arguments_ !== "object" ||
      Array.isArray(arguments_) ||
      Object.keys(arguments_).length > 0
    ) {
      return failure("request_too_large", "planning_memory_list accepts an empty object.");
    }
    const topics = this.#vault
      .getFiles()
      .filter((candidate) => MEMORY_TOPIC_PATH.test(candidate.path))
      .sort((left, right) => left.path.localeCompare(right.path))
      .flatMap((candidate) => {
        const match = MEMORY_TOPIC_PATH.exec(candidate.path);
        const frontmatter = this.#metadata?.getFileCache(candidate)?.frontmatter;
        if (!match || !frontmatter || typeof frontmatter !== "object") return [];
        const { name, description, type } = frontmatter as Record<string, unknown>;
        if (
          type !== match[1] ||
          typeof name !== "string" ||
          !name.trim() ||
          Buffer.byteLength(name, "utf8") > 128 ||
          typeof description !== "string" ||
          !description.trim() ||
          Buffer.byteLength(description, "utf8") > 512
        ) {
          return [];
        }
        return [{
          path: candidate.path,
          name: name.trim(),
          description: description.trim(),
          type: type as MemoryTopicType,
          modifiedVersion: fileVersion(candidate),
        }];
      });
    return {
      ok: true,
      value: {
        type: "planning_memory_list",
        topics: topics.slice(0, MAX_MEMORY_TOPICS),
        truncated: topics.length > MAX_MEMORY_TOPICS,
      },
    };
  }

  async #interviewCatalog(arguments_: unknown): Promise<LocalToolResultPayload> {
    if (!arguments_ || typeof arguments_ !== "object" || Array.isArray(arguments_)) {
      return failure("request_too_large", "interview_catalog arguments must be an object.");
    }
    const input = arguments_ as { limit?: unknown; query?: unknown };
    if (Object.keys(input).some((key) => key !== "limit" && key !== "query")) {
      return failure("request_too_large", "interview_catalog accepts only query and limit.");
    }
    const query = typeof input.query === "string" ? input.query.trim().toLocaleLowerCase() : "";
    if (!query || Buffer.byteLength(query, "utf8") > MAX_SEARCH_QUERY_BYTES) {
      return failure(
        "request_too_large",
        `interview_catalog query must be between 1 and ${MAX_SEARCH_QUERY_BYTES} UTF-8 bytes.`,
      );
    }
    const limit = input.limit === undefined ? DEFAULT_INTERVIEW_CATALOG_RESULTS : input.limit;
    if (
      !Number.isInteger(limit) ||
      (limit as number) < 1 ||
      (limit as number) > MAX_INTERVIEW_CATALOG_RESULTS
    ) {
      return failure(
        "request_too_large",
        `interview_catalog limit must be between 1 and ${MAX_INTERVIEW_CATALOG_RESULTS}.`,
      );
    }

    const terms = query.split(/\s+/u).filter(Boolean);
    const score = (value: string): number =>
      terms.reduce((total, term) => total + occurrences(value, [term]), 0);
    const experienceMatches: Array<{
      company?: string;
      contentHash: string;
      date?: string;
      modifiedVersion: string;
      path: string;
      position?: string;
      round?: string;
      score: number;
      title: string;
    }> = [];
    const questionMatches: Array<{
      answerState?: "draft" | "needs-research" | "verified";
      contentHash: string;
      modifiedVersion: string;
      path: string;
      score: number;
      title: string;
    }> = [];

    for (const file of this.#vault.getFiles()) {
      const isExperience = /^experiences\/[^/]+\.md$/u.test(file.path) && file.path !== "experiences/index.md";
      const isQuestion = /^interview\/[^/]+\.md$/u.test(file.path) && file.path !== "interview/index.md";
      if ((!isExperience && !isQuestion) || !isReadableFile(file) || !safePath(file.path)) continue;
      const content = await this.#vault.cachedRead(file);
      const frontmatter = this.#metadata?.getFileCache(file)?.frontmatter ?? {};
      const metadata = frontmatter && typeof frontmatter === "object" ? frontmatter : {};
      const searchable = `${file.path} ${metadataValues(metadata).join(" ")} ${content}`.toLocaleLowerCase();
      const matchScore = score(searchable);
      if (matchScore === 0) continue;
      const textField = (name: string): string | undefined => {
        const value = (metadata as Record<string, unknown>)[name];
        return typeof value === "string" && value.trim()
          ? boundedUtf8(value.trim(), 256).content
          : undefined;
      };
      const title = textField("title") ?? file.path.split("/").at(-1)!.replace(/\.md$/iu, "");
      const common = {
        path: file.path,
        title,
        modifiedVersion: fileVersion(file),
        contentHash: contentHash(content),
        score: matchScore,
      };
      if (isExperience) {
        experienceMatches.push({
          ...common,
          ...(textField("company") ? { company: textField("company") } : {}),
          ...(textField("position") ? { position: textField("position") } : {}),
          ...(textField("round") ? { round: textField("round") } : {}),
          ...(textField("date") ? { date: textField("date") } : {}),
        });
      } else {
        const answerState = textField("answer-state");
        questionMatches.push({
          ...common,
          ...(answerState === "needs-research" || answerState === "draft" || answerState === "verified"
            ? { answerState }
            : {}),
        });
      }
    }

    const byScoreThenPath = <T extends { path: string; score: number }>(left: T, right: T) =>
      right.score - left.score || left.path.localeCompare(right.path);
    experienceMatches.sort(byScoreThenPath);
    questionMatches.sort(byScoreThenPath);
    const selectedExperiences = experienceMatches.slice(0, limit as number).map(({ score: _, ...entry }) => entry);
    const selectedQuestions = questionMatches.slice(0, limit as number).map(({ score: _, ...entry }) => entry);
    const indexes = [];
    for (const [kind, indexPath] of [
      ["experience", "experiences/index.md"],
      ["question", "interview/index.md"],
    ] as const) {
      const file = this.#vault.getFiles().find((candidate) => candidate.path === indexPath);
      if (!file || !isReadableFile(file)) {
        indexes.push({
          kind,
          path: indexPath,
          exists: false,
          modifiedVersion: "missing",
        });
        continue;
      }
      const content = await this.#vault.cachedRead(file);
      indexes.push({
        kind,
        path: indexPath,
        exists: true,
        modifiedVersion: fileVersion(file),
        contentHash: contentHash(content),
      });
    }
    return {
      ok: true,
      value: {
        type: "interview_catalog",
        experienceCandidates: selectedExperiences,
        questionCandidates: selectedQuestions,
        indexes,
        truncated:
          experienceMatches.length > selectedExperiences.length ||
          questionMatches.length > selectedQuestions.length,
      },
    };
  }

  async #readPlanningMemory(arguments_: unknown): Promise<LocalToolResultPayload> {
    if (
      !arguments_ ||
      typeof arguments_ !== "object" ||
      Array.isArray(arguments_) ||
      Object.keys(arguments_).some((key) => key !== "paths")
    ) {
      return failure("request_too_large", "planning_memory_read requires only selected topic paths.");
    }
    const paths = (arguments_ as { paths?: unknown }).paths;
    if (
      !Array.isArray(paths) ||
      paths.length === 0 ||
      paths.length > MAX_MEMORY_READ_TOPICS ||
      paths.some((candidate) => typeof candidate !== "string" || !MEMORY_TOPIC_PATH.test(candidate)) ||
      new Set(paths).size !== paths.length
    ) {
      return failure("invalid_path", "Select between one and five distinct Planning Memory topic paths.");
    }
    const filesByPath = new Map(this.#vault.getFiles().map((candidate) => [candidate.path, candidate]));
    const files = paths.map((candidate) => filesByPath.get(candidate));
    if (files.some((candidate) => !candidate || !isReadableFile(candidate))) {
      return failure("not_found", "A selected Planning Memory topic is missing or unreadable.");
    }
    if (!this.#canonicalize) {
      return failure("tool_error", "Planning Memory path containment could not be verified safely.");
    }
    try {
      const root = await this.#canonicalize("");
      const canonicalTopics = await Promise.all(
        files.map((candidate) => this.#canonicalize!(candidate!.path)),
      );
      if (canonicalTopics.some((candidate) => !isContained(root, candidate))) {
        return failure("invalid_path", "A selected Planning Memory topic resolves outside the Vault root.");
      }
    } catch {
      return failure("invalid_path", "A selected Planning Memory topic could not be resolved safely.");
    }
    const contents = await Promise.all(files.map((candidate) => this.#vault.cachedRead(candidate!)));
    if (
      contents.some((content) => Buffer.byteLength(content, "utf8") > MAX_MEMORY_TOPIC_BYTES) ||
      contents.reduce((total, content) => total + Buffer.byteLength(content, "utf8"), 0) >
        MAX_MEMORY_READ_BYTES
    ) {
      return failure("response_too_large", "Selected Planning Memory topic content is too large.");
    }
    return {
      ok: true,
      value: {
        type: "planning_memory_read",
        topics: files.map((candidate, index) => ({
          path: candidate!.path,
          content: contents[index]!,
          modifiedVersion: fileVersion(candidate!),
        })),
      },
    };
  }

  async #dailyNoteContext(arguments_: unknown): Promise<LocalToolResultPayload> {
    if (
      !arguments_ ||
      typeof arguments_ !== "object" ||
      Array.isArray(arguments_) ||
      Object.keys(arguments_).some((key) => key !== "date")
    ) {
      return failure(
        "request_too_large",
        "daily_note_context accepts an object containing only an optional ISO date.",
      );
    }
    if (!this.#dailyNotes) {
      return failure(
        "not_found",
        "Daily Notes configuration is unavailable. Enable and configure Obsidian Daily Notes.",
      );
    }
    const input = arguments_ as { date?: unknown };
    const resolvedDate = input.date === undefined ? this.#dailyNotes.resolveToday() : input.date;
    if (!isIsoDate(resolvedDate)) {
      return failure("request_too_large", "daily_note_context date must be a valid YYYY-MM-DD date.");
    }
    let configuration: unknown;
    try {
      configuration = await this.#dailyNotes.readConfiguration();
    } catch {
      return failure(
        "malformed_control_file",
        "Daily Notes configuration could not be read or parsed. Review the Daily Notes settings in Obsidian.",
      );
    }
    if (!configuration || typeof configuration !== "object" || Array.isArray(configuration)) {
      return failure(
        "malformed_control_file",
        "Daily Notes configuration is missing or invalid. Configure a folder and date format in Obsidian.",
      );
    }
    const options = configuration as { folder?: unknown; format?: unknown; template?: unknown };
    const folder = safePath(options.folder, true);
    if (folder === undefined) {
      return failure("invalid_path", "The configured Daily Notes folder is invalid or outside the Vault.");
    }
    if (
      typeof options.format !== "string" ||
      !options.format.trim() ||
      Buffer.byteLength(options.format, "utf8") > 128
    ) {
      return failure(
        "malformed_control_file",
        "The configured Daily Notes date format is missing or invalid.",
      );
    }
    const dateFormat = options.format.trim();
    const formattedDate = this.#dailyNotes.formatDate(resolvedDate, dateFormat);
    if (typeof formattedDate !== "string" || !formattedDate.trim()) {
      return failure("malformed_control_file", "The configured Daily Notes date format could not be resolved.");
    }
    const filename = formattedDate.endsWith(".md") ? formattedDate : `${formattedDate}.md`;
    const targetPath = safePath(folder ? `${folder}/${filename}` : filename);
    if (!targetPath) {
      return failure("invalid_path", "The configured Daily Note path is invalid or outside the Vault.");
    }

    const target = this.#vault.getFiles().find((candidate) => candidate.path === targetPath);
    if (target && !isReadableFile(target)) {
      return failure("unreadable_content", `Daily Note target '${targetPath}' is not a Markdown file.`);
    }
    const templateSetting: string | null =
      typeof options.template === "string" && options.template.trim()
        ? safePath(options.template.trim()) ?? null
        : null;
    if (typeof options.template !== "undefined" && options.template !== "" && !templateSetting) {
      return failure("invalid_path", "The configured Daily Notes template path is invalid.");
    }
    const template = templateSetting
      ? this.#vault.getFiles().find(
          (candidate) =>
            candidate.path === templateSetting ||
            (!path.posix.extname(templateSetting) && candidate.path === `${templateSetting}.md`),
        )
      : undefined;

    if (!this.#canonicalize) {
      return failure("tool_error", "Daily Note path containment could not be verified safely.");
    }
    const containmentPaths = new Set<string>([folder || ""]);
    if (target) containmentPaths.add(target.path);
    if (template) containmentPaths.add(template.path);
    if (templateSetting) {
      const templateParent = path.posix.dirname(templateSetting);
      containmentPaths.add(templateParent === "." ? "" : templateParent);
    }
    try {
      const vaultRoot = await this.#canonicalize("");
      const canonicalPaths = await Promise.all(
        [...containmentPaths].map((candidate) => this.#canonicalize!(candidate)),
      );
      if (canonicalPaths.some((candidate) => !isContained(vaultRoot, candidate))) {
        return failure("invalid_path", "A configured Daily Note path resolves outside the Vault root.");
      }
    } catch {
      return failure("invalid_path", "A configured Daily Note path could not be resolved safely.");
    }

    const templateContent = template ? await this.#vault.cachedRead(template) : null;
    if (templateContent !== null && Buffer.byteLength(templateContent, "utf8") > MAX_DAILY_TEMPLATE_BYTES) {
      return failure(
        "response_too_large",
        `The configured Daily Notes template exceeds ${MAX_DAILY_TEMPLATE_BYTES} UTF-8 bytes.`,
      );
    }
    return {
      ok: true,
      value: {
        type: "daily_note_context",
        resolvedDate,
        dateFormat,
        targetPath,
        targetExists: Boolean(target),
        targetVersion: target ? fileVersion(target) : "missing",
        templatePath: template?.path ?? templateSetting,
        templateContent,
        templateVersion: template ? fileVersion(template) : null,
      },
    };
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
