import { Buffer } from "node:buffer";
import { createHash } from "node:crypto";
import { isIP } from "node:net";

const DIGEST = /^sha256:[0-9a-f]{64}$/u;
const MAX_CANONICAL_URL_BYTES = 2_048;
const MAX_CANONICAL_SOURCE_URLS = 20;
const MAX_RAW_SOURCE_URLS = 8;
const MAX_ORDERED_IMAGE_HASHES = 20;

export interface InterviewSourceIdentity {
    readonly canonicalUrls: readonly string[];
    readonly orderedImageContentHashes: readonly string[];
    readonly sourceFingerprint: string | null;
}

/** Convert untrusted Catalog source inputs into their stable, byte-free identity. */
export function normalizeInterviewSourceIdentity(value: unknown): InterviewSourceIdentity | null {
    if (!isRecord(value) || hasExtraKeys(value, ["sourceUrls", "orderedImageContentHashes"])) return null;
    const rawSourceUrls = value.sourceUrls ?? [];
    if (!Array.isArray(rawSourceUrls) || rawSourceUrls.length > MAX_RAW_SOURCE_URLS ||
        rawSourceUrls.some((item) => !boundedNonemptyString(item, MAX_CANONICAL_URL_BYTES))) return null;
    const normalizedUrls = rawSourceUrls.map((item) => normalizePublicUrl(String(item).trim()));
    if (normalizedUrls.some((item) => item === undefined)) return null;
    const canonicalUrls = [...new Set(normalizedUrls as string[])];

    const rawImageHashes = value.orderedImageContentHashes ?? [];
    if (!Array.isArray(rawImageHashes) || rawImageHashes.length > MAX_ORDERED_IMAGE_HASHES ||
        rawImageHashes.some((item) => typeof item !== "string" || !DIGEST.test(item))) return null;
    const orderedImageContentHashes = rawImageHashes as string[];
    return {
        canonicalUrls,
        orderedImageContentHashes,
        sourceFingerprint: orderedImageContentHashes.length > 0
            ? fingerprintOrderedImages(orderedImageContentHashes)
            : null,
    };
}

/** Prove that an Apply receipt is already canonical and internally consistent. */
export function validateCanonicalInterviewSourceIdentity(value: unknown): InterviewSourceIdentity | null {
    if (!isRecord(value) || hasExtraKeys(value, [
        "canonicalUrls", "orderedImageContentHashes", "sourceFingerprint",
    ])) return null;
    const canonicalUrls = value.canonicalUrls;
    if (!Array.isArray(canonicalUrls) || canonicalUrls.length > MAX_CANONICAL_SOURCE_URLS ||
        canonicalUrls.some((item) => typeof item !== "string" || normalizePublicUrl(item) !== item) ||
        new Set(canonicalUrls).size !== canonicalUrls.length) return null;
    const orderedImageContentHashes = value.orderedImageContentHashes;
    if (!Array.isArray(orderedImageContentHashes) || orderedImageContentHashes.length > MAX_ORDERED_IMAGE_HASHES ||
        orderedImageContentHashes.some((item) => typeof item !== "string" || !DIGEST.test(item))) return null;
    const sourceFingerprint = value.sourceFingerprint;
    const expectedFingerprint = orderedImageContentHashes.length > 0
        ? fingerprintOrderedImages(orderedImageContentHashes as string[])
        : null;
    if (sourceFingerprint !== expectedFingerprint) return null;
    return {
        canonicalUrls: canonicalUrls as string[],
        orderedImageContentHashes: orderedImageContentHashes as string[],
        sourceFingerprint: expectedFingerprint,
    };
}

function normalizePublicUrl(value: string): string | undefined {
    try {
        const url = new URL(value);
        if (!["http:", "https:"].includes(url.protocol) || url.username || url.password ||
            !isPublicHostname(url.hostname)) return undefined;
        url.hash = "";
        url.hostname = url.hostname.toLocaleLowerCase();
        const retained = [...url.searchParams.entries()]
            .map(([key, item], index) => ({ key, item, index }))
            .filter(({ key }) => !isDiscardedQueryParameter(key))
            .sort((left, right) => compareCodePoints(left.key, right.key) ||
                compareCodePoints(left.item, right.item) || left.index - right.index);
        url.search = "";
        for (const { key, item } of retained) url.searchParams.append(key, item);
        return Buffer.byteLength(url.href, "utf8") <= MAX_CANONICAL_URL_BYTES ? url.href : undefined;
    } catch {
        return undefined;
    }
}

function compareCodePoints(left: string, right: string): number {
    return left < right ? -1 : left > right ? 1 : 0;
}

function isPublicHostname(value: string): boolean {
    const hostname = value.toLocaleLowerCase().replace(/^\[|\]$/gu, "");
    const version = isIP(hostname);
    if (version === 4) return isPublicIpv4(hostname);
    if (version === 6) return isPublicIpv6(hostname);
    if (!hostname.includes(".") || hostname.startsWith(".") || hostname.endsWith(".")) return false;
    return !/\.(?:home|internal|invalid|lan|local|localhost|test|example)$/iu.test(hostname);
}

function isPublicIpv4(value: string): boolean {
    const [first, second, third] = value.split(".").map(Number);
    return first !== 0 && first !== 10 && first !== 127 && first < 224 &&
        !(first === 100 && second >= 64 && second <= 127) &&
        !(first === 169 && second === 254) &&
        !(first === 172 && second >= 16 && second <= 31) &&
        !(first === 192 && (second === 0 || second === 168)) &&
        !(first === 198 && (second === 18 || second === 19 || (second === 51 && third === 100))) &&
        !(first === 203 && second === 0 && third === 113);
}

function isPublicIpv6(value: string): boolean {
    if (value.startsWith("::ffff:")) {
        const tail = value.slice("::ffff:".length);
        if (isIP(tail) === 4) return isPublicIpv4(tail);
        const mapped = /^([0-9a-f]{1,4}):([0-9a-f]{1,4})$/iu.exec(tail);
        if (mapped === null) return false;
        const high = Number.parseInt(mapped[1], 16);
        const low = Number.parseInt(mapped[2], 16);
        return isPublicIpv4(`${high >>> 8}.${high & 0xff}.${low >>> 8}.${low & 0xff}`);
    }
    const segments = parseIpv6Segments(value);
    if (segments === null) return false;
    const [first, second] = segments;
    // Be deliberately conservative for literal addresses: accept the current
    // Global Unicast 2000::/3 allocation, then remove IANA special-purpose
    // ranges that are not globally reachable. Domain names remain available
    // for future allocations without silently widening this network boundary.
    return (first & 0xe000) === 0x2000 &&
        !(first === 0x2001 && second <= 0x01ff) &&
        !(first === 0x2001 && second === 0x0db8) &&
        first !== 0x2002 &&
        first !== 0x3ffe &&
        !(first === 0x3fff && (second & 0xf000) === 0);
}

function parseIpv6Segments(value: string): readonly number[] | null {
    const halves = value.split("::");
    if (halves.length > 2) return null;
    const parseHalf = (half: string): number[] | null => {
        if (!half) return [];
        const raw = half.split(":");
        if (raw.some((item) => !/^[0-9a-f]{1,4}$/iu.test(item))) return null;
        return raw.map((item) => Number.parseInt(item, 16));
    };
    const left = parseHalf(halves[0]);
    const right = parseHalf(halves[1] ?? "");
    if (left === null || right === null) return null;
    if (halves.length === 1) return left.length === 8 ? left : null;
    const missing = 8 - left.length - right.length;
    return missing >= 1 ? [...left, ...Array<number>(missing).fill(0), ...right] : null;
}

function isDiscardedQueryParameter(value: string): boolean {
    return /^(utm_.+|spm|from|source|ref|fbclid|gclid|dclid|yclid|mc_cid|mc_eid|igshid|msclkid|ttclid|twclid)$/iu
        .test(value) ||
        /^(?:.*token|(?:.*[_-])?secret|password|passwd|pwd|code|(?:oauth|authorization)[_-]?code|jwt|nonce|auth(?:orization)?|api[_-]?key|credential|signature|sig|session(?:id)?|sid|ticket|assertion|samlresponse|expires?|expiry|awsaccesskeyid|googleaccessid|key-pair-id|policy|x-amz-.+|x-goog-.+)$/iu
            .test(value);
}

function fingerprintOrderedImages(contentHashes: readonly string[]): string {
    const hash = createHash("sha256");
    contentHashes.forEach((contentHash, order) => hash.update(`${order}\0${contentHash}\n`, "utf8"));
    return `sha256:${hash.digest("hex")}`;
}

function boundedNonemptyString(value: unknown, maximumBytes: number): value is string {
    return typeof value === "string" && Boolean(value.trim()) &&
        Buffer.byteLength(value.trim(), "utf8") <= maximumBytes;
}

function isRecord(value: unknown): value is Readonly<Record<string, unknown>> {
    return typeof value === "object" && value !== null && !Array.isArray(value);
}

function hasExtraKeys(value: Readonly<Record<string, unknown>>, allowed: readonly string[]): boolean {
    const keys = new Set(allowed);
    return Object.keys(value).some((key) => !keys.has(key));
}
