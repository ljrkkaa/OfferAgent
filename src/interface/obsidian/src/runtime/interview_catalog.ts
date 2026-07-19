import { Buffer } from "node:buffer";
import { createHash } from "node:crypto";

import type { TFile } from "obsidian";

import type { ExecutableToolCallDescriptor, ToolResultDescriptor } from "./generated_protocol";
import { normalizeInterviewSourceIdentity } from "./interview_source_identity";
import { failed, hasExtraKeys, succeeded } from "./plugin_tool_results";

const EXPERIENCE_PATH = /^experiences\/[^/.][^/]*\.md$/u;
const QUESTION_PATH = /^interview\/[^/.][^/]*\.md$/u;
const LEGACY_EXPERIENCE_PATH = /^interviews\/experiences\/[^/.][^/]*\.md$/u;
const LEGACY_QUESTION_PATH = /^interviews\/questions\/[^/.][^/]*\.md$/u;
const DIGEST = /^sha256:[0-9a-f]{64}$/u;
const MAX_FILE_BYTES = 65_536;
const MAX_SCANNED_FILES = 1_000;
const MAX_EXPERIENCES = 50;
const MAX_QUESTIONS = 100;
const EXPERIENCE_INDEX_PATH = "experiences/index.md";
const QUESTION_INDEX_PATH = "interview/index.md";

export interface InterviewCatalogVaultPort {
    getFiles(): TFile[];
    getFileByPath(path: string): TFile | null;
    cachedRead(file: TFile): Promise<string>;
}

interface Snapshot {
    readonly content: string;
    readonly contentHash: string;
    readonly modifiedVersion: string;
}

interface ExperienceCandidate {
    readonly path: string;
    readonly experienceId: string;
    readonly sourceKind: string;
    readonly sourceUrl?: string;
    readonly sourceFingerprint?: string;
    readonly company?: string;
    readonly role?: string;
    readonly candidate?: string;
    readonly eventDate?: string;
    readonly round?: string;
    readonly exactSourceMatch: boolean;
    readonly contentHash: string;
    readonly modifiedVersion: string;
}

interface QuestionCandidate {
    readonly path: string;
    readonly questionId: string;
    readonly title: string;
    readonly answerState: "needs-research" | "draft" | "verified";
    readonly frequency: number;
    readonly matchedTerms: readonly string[];
    readonly contentHash: string;
    readonly modifiedVersion: string;
}

interface CatalogIndex {
    readonly kind: "experience" | "question";
    readonly path: string;
    readonly exists: boolean;
    readonly modifiedVersion: string;
    readonly contentHash?: string;
}

/** Candidate discovery only; the Python Agent owns semantic identity and merge decisions. */
export class InterviewCatalogAdapter {
    constructor(private readonly vault: InterviewCatalogVaultPort) {}

    async execute(call: ExecutableToolCallDescriptor): Promise<ToolResultDescriptor> {
        if (call.name !== "interview_catalog.search" || call.version !== "1") {
            throw new Error(`unsupported Interview Catalog Tool: ${call.name}@${call.version}`);
        }
        const query = parseQuery(call.arguments);
        if (query === null) {
            return failed(call, "protocol.invalid_params", "Interview Catalog search arguments are invalid.");
        }
        try {
            const experiences: ExperienceCandidate[] = [];
            const questions: QuestionCandidate[] = [];
            let scanned = 0;
            let truncated = false;
            const candidates = this.vault.getFiles()
                .filter((file) => file.extension.toLocaleLowerCase() === "md" &&
                    (isExperiencePath(file.path) || isQuestionPath(file.path)))
                .sort((left, right) => left.path.localeCompare(right.path));
            for (const file of candidates) {
                if (scanned >= MAX_SCANNED_FILES) {
                    truncated = true;
                    break;
                }
                scanned += 1;
                const snapshot = await stableRead(this.vault, file);
                if (isExperiencePath(file.path)) {
                    const candidate = experienceMetadata(file.path, snapshot, query);
                    if (candidate !== null && relevantExperience(candidate, query)) experiences.push(candidate);
                } else {
                    const candidate = questionMetadata(file.path, snapshot, query.questionTerms);
                    if (candidate !== null && relevantQuestion(candidate, query.questionTerms)) questions.push(candidate);
                }
            }
            experiences.sort((left, right) => Number(right.exactSourceMatch) - Number(left.exactSourceMatch) ||
                identityRelevance(right, query) - identityRelevance(left, query) || left.path.localeCompare(right.path));
            questions.sort((left, right) => right.matchedTerms.length - left.matchedTerms.length ||
                left.path.localeCompare(right.path));
            if (experiences.length > MAX_EXPERIENCES || questions.length > MAX_QUESTIONS) truncated = true;
            const indexes = await Promise.all([
                indexDescriptor(this.vault, "experience", EXPERIENCE_INDEX_PATH),
                indexDescriptor(this.vault, "question", QUESTION_INDEX_PATH),
            ]);
            return succeeded(call, "Discovered bounded Interview Catalog candidates without making semantic merge decisions.", {
                normalizedSource: {
                    canonicalUrls: query.sourceUrls,
                    sourceFingerprint: query.sourceFingerprint,
                    orderedImageContentHashes: query.orderedImageContentHashes,
                },
                experienceCandidates: experiences.slice(0, MAX_EXPERIENCES),
                questionCandidates: questions.slice(0, MAX_QUESTIONS),
                indexes,
                truncated,
            });
        } catch (error) {
            const oversized = error instanceof RangeError;
            return failed(
                call,
                oversized ? "protocol.message_too_large" : "resource.conflict",
                oversized
                    ? "Interview Catalog contains an oversized candidate."
                    : "Interview Catalog changed during candidate discovery.",
                !oversized,
            );
        }
    }
}

function isExperiencePath(path: string): boolean {
    return (EXPERIENCE_PATH.test(path) && path !== EXPERIENCE_INDEX_PATH) || LEGACY_EXPERIENCE_PATH.test(path);
}

function isQuestionPath(path: string): boolean {
    return (QUESTION_PATH.test(path) && path !== QUESTION_INDEX_PATH) || LEGACY_QUESTION_PATH.test(path);
}

interface CatalogQuery {
    readonly sourceUrls: readonly string[];
    readonly sourceFingerprint: string | null;
    readonly orderedImageContentHashes: readonly string[];
    readonly company?: string;
    readonly role?: string;
    readonly questionTerms: readonly string[];
}

function parseQuery(value: Readonly<Record<string, unknown>>): CatalogQuery | null {
    if (hasExtraKeys(value, [
        "sourceUrls", "orderedImageContentHashes", "company", "role", "questionTerms",
    ])) return null;
    const sourceIdentity = normalizeInterviewSourceIdentity({
        sourceUrls: value.sourceUrls ?? [],
        orderedImageContentHashes: value.orderedImageContentHashes ?? [],
    });
    if (sourceIdentity === null) return null;
    const company = optionalBounded(value.company, 128);
    const role = optionalBounded(value.role, 128);
    if ((value.company !== undefined && company === undefined) || (value.role !== undefined && role === undefined)) return null;
    const rawTerms = value.questionTerms ?? [];
    if (!Array.isArray(rawTerms) || rawTerms.length > 20 || rawTerms.some((term) => optionalBounded(term, 256) === undefined)) {
        return null;
    }
    const questionTerms = [...new Set((rawTerms as string[]).map((term) => term.trim()))];
    if (questionTerms.length !== rawTerms.length) return null;
    return {
        sourceUrls: sourceIdentity.canonicalUrls,
        sourceFingerprint: sourceIdentity.sourceFingerprint,
        orderedImageContentHashes: sourceIdentity.orderedImageContentHashes,
        company,
        role,
        questionTerms,
    };
}

async function indexDescriptor(
    vault: InterviewCatalogVaultPort,
    kind: CatalogIndex["kind"],
    path: string,
): Promise<CatalogIndex> {
    const file = vault.getFileByPath(path);
    if (file === null) return { kind, path, exists: false, modifiedVersion: "missing" };
    const snapshot = await stableRead(vault, file);
    return {
        kind,
        path,
        exists: true,
        modifiedVersion: snapshot.modifiedVersion,
        contentHash: snapshot.contentHash,
    };
}

async function stableRead(vault: InterviewCatalogVaultPort, file: TFile): Promise<Snapshot> {
    if (file.stat.size < 1 || file.stat.size > MAX_FILE_BYTES) throw new RangeError("candidate is oversized");
    const before = modifiedVersion(file);
    const content = await vault.cachedRead(file);
    const after = vault.getFileByPath(file.path);
    if (after === null || modifiedVersion(after) !== before) throw new Error("candidate changed");
    const size = Buffer.byteLength(content, "utf8");
    if (!content.trim() || content.includes("\0") || size < 1 || size > MAX_FILE_BYTES) {
        throw new RangeError("candidate content is invalid");
    }
    return { content, contentHash: digest(content), modifiedVersion: before };
}

function experienceMetadata(path: string, snapshot: Snapshot, query: CatalogQuery): ExperienceCandidate | null {
    const values = frontmatter(snapshot.content);
    if (values === null || values.get("type") !== "interview-experience") return null;
    const experienceId = identifier(values.get("experience-id"));
    const sourceKind = bounded(values.get("source-kind"), 32);
    if (experienceId === undefined || sourceKind === undefined) return null;
    const rawUrl = bounded(values.get("source-url"), 2_048);
    const sourceUrl = rawUrl === undefined ? undefined : normalizeInterviewSourceIdentity({
        sourceUrls: [rawUrl],
    })?.canonicalUrls[0];
    if (rawUrl !== undefined && sourceUrl === undefined) return null;
    const sourceFingerprint = bounded(values.get("source-fingerprint"), 71);
    if (sourceFingerprint !== undefined && !DIGEST.test(sourceFingerprint)) return null;
    const candidate = bounded(values.get("candidate"), 128);
    const eventDate = bounded(values.get("event-date"), 10);
    if (eventDate !== undefined && eventDate !== "unknown" &&
        !/^\d{4}-\d{2}-\d{2}$/u.test(eventDate)) return null;
    const exactSourceMatch = (sourceUrl !== undefined && query.sourceUrls.includes(sourceUrl)) ||
        (query.sourceFingerprint !== null && query.sourceFingerprint === sourceFingerprint);
    return {
        path, experienceId, sourceKind,
        ...(sourceUrl === undefined ? {} : { sourceUrl }),
        ...(sourceFingerprint === undefined ? {} : { sourceFingerprint }),
        ...optionalCandidateField("company", bounded(values.get("company"), 128)),
        ...optionalCandidateField("role", bounded(values.get("role"), 128)),
        ...optionalCandidateField("candidate", candidate),
        ...optionalCandidateField("eventDate", eventDate),
        ...optionalCandidateField("round", bounded(values.get("round"), 128)),
        exactSourceMatch,
        contentHash: snapshot.contentHash,
        modifiedVersion: snapshot.modifiedVersion,
    };
}

function optionalCandidateField<Key extends "company" | "role" | "candidate" | "eventDate" | "round">(
    key: Key,
    value: string | undefined,
): Partial<Record<Key, string>> {
    return value === undefined ? {} : { [key]: value } as Record<Key, string>;
}

function questionMetadata(path: string, snapshot: Snapshot, terms: readonly string[]): QuestionCandidate | null {
    const values = frontmatter(snapshot.content);
    if (values === null || values.get("type") !== "interview-question") return null;
    const questionId = identifier(values.get("question-id"));
    const title = bounded(values.get("title"), 512);
    const answerState = values.get("answer-state");
    const frequency = Number(values.get("frequency"));
    if (questionId === undefined || title === undefined ||
        !["needs-research", "draft", "verified"].includes(answerState ?? "") ||
        !Number.isSafeInteger(frequency) || frequency < 1) return null;
    const normalizedTitle = semanticText(title);
    const matchedTerms = terms.filter((term) => tokenOverlap(normalizedTitle, semanticText(term)));
    return {
        path, questionId, title,
        answerState: answerState as QuestionCandidate["answerState"],
        frequency,
        matchedTerms,
        contentHash: snapshot.contentHash,
        modifiedVersion: snapshot.modifiedVersion,
    };
}

function relevantExperience(candidate: ExperienceCandidate, query: CatalogQuery): boolean {
    if (candidate.exactSourceMatch) return true;
    if (query.company === undefined && query.role === undefined) return true;
    return (query.company !== undefined && semanticText(candidate.company ?? "") === semanticText(query.company)) ||
        (query.role !== undefined && semanticText(candidate.role ?? "") === semanticText(query.role));
}

function identityRelevance(candidate: ExperienceCandidate, query: CatalogQuery): number {
    return Number(query.company !== undefined && semanticText(candidate.company ?? "") === semanticText(query.company)) +
        Number(query.role !== undefined && semanticText(candidate.role ?? "") === semanticText(query.role));
}

function relevantQuestion(candidate: QuestionCandidate, terms: readonly string[]): boolean {
    return terms.length === 0 || candidate.matchedTerms.length > 0;
}

function tokenOverlap(left: string, right: string): boolean {
    if (!left || !right) return false;
    if (left.includes(right) || right.includes(left)) return true;
    const leftTokens = new Set(left.split(" ").filter((token) => token.length >= 2));
    return right.split(" ").some((token) => token.length >= 2 && leftTokens.has(token));
}

function semanticText(value: string): string {
    return value.toLocaleLowerCase().replace(/[^\p{L}\p{N}]+/gu, " ").trim();
}

function frontmatter(content: string): Map<string, string> | null {
    const lines = content.replace(/\r\n/gu, "\n").split("\n");
    if (lines[0] !== "---") return null;
    const closing = lines.slice(1, 65).findIndex((line) => line === "---");
    if (closing < 0) return null;
    const values = new Map<string, string>();
    for (const line of lines.slice(1, closing + 1)) {
        if (!line.trim()) continue;
        const match = /^([a-z][a-z0-9-]*):\s*(.*)$/u.exec(line);
        if (match === null || values.has(match[1])) return null;
        const value = scalar(match[2]);
        if (value === null) return null;
        values.set(match[1], value);
    }
    return values;
}

function scalar(raw: string): string | null {
    const value = raw.trim();
    if (value.startsWith('"')) {
        try {
            const parsed = JSON.parse(value);
            return typeof parsed === "string" ? parsed : null;
        } catch {
            return null;
        }
    }
    if (value.startsWith("'")) {
        return value.endsWith("'") && value.length >= 2 ? value.slice(1, -1).replace(/''/gu, "'") : null;
    }
    return !/[\[\]{}\n\r]/u.test(value) ? value : null;
}

function optionalBounded(value: unknown, maximumBytes: number): string | undefined {
    return typeof value === "string" && value.trim() && Buffer.byteLength(value.trim(), "utf8") <= maximumBytes
        ? value.trim() : undefined;
}

function bounded(value: string | undefined, maximumBytes: number): string | undefined {
    return value !== undefined && value && Buffer.byteLength(value, "utf8") <= maximumBytes ? value : undefined;
}

function identifier(value: string | undefined): string | undefined {
    return value !== undefined && /^[A-Za-z0-9][A-Za-z0-9_-]{0,127}$/u.test(value) ? value : undefined;
}

function modifiedVersion(file: TFile): string {
    return `mtime:${file.stat.mtime}:size:${file.stat.size}`;
}

function digest(content: string): string {
    return `sha256:${createHash("sha256").update(content, "utf8").digest("hex")}`;
}
