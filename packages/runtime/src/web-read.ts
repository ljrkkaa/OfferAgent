import { lookup as dnsLookup } from "node:dns/promises";
import { isIP, type LookupFunction } from "node:net";
import type { LocalToolResultPayload, VaultToolErrorCode } from "@offeragent/protocol";
import { Agent, fetch as undiciFetch } from "undici";

const DEFAULT_CONTENT_BYTES = 32_768;
const DEFAULT_REDIRECTS = 3;
const DEFAULT_RESPONSE_BYTES = 1_048_576;
const DEFAULT_TIMEOUT_MS = 15_000;
const REDIRECT_STATUSES = new Set([301, 302, 303, 307, 308]);

type Fetch = typeof fetch;
type Resolve = (hostname: string) => Promise<Array<{ address: string }>>;

interface WebReaderOptions {
  fetch?: Fetch;
  maxRedirects?: number;
  maxResponseBytes?: number;
  resolve?: Resolve;
  timeoutMs?: number;
}

function failure(code: VaultToolErrorCode, message: string): LocalToolResultPayload {
  return { ok: false, error: { code, message } };
}

function parseIpv6Hextets(address: string): number[] | undefined {
  const halves = address.split("::");
  if (halves.length > 2) return undefined;
  const parseHalf = (half: string): number[] | undefined => {
    if (!half) return [];
    const fields = half.split(":");
    const values: number[] = [];
    for (const [index, field] of fields.entries()) {
      if (field.includes(".")) {
        if (index !== fields.length - 1 || isIP(field) !== 4) return undefined;
        const octets = field.split(".").map(Number);
        values.push((octets[0] << 8) | octets[1], (octets[2] << 8) | octets[3]);
      } else {
        values.push(Number.parseInt(field, 16));
      }
    }
    return values;
  };
  const left = parseHalf(halves[0]);
  const right = parseHalf(halves[1] ?? "");
  if (!left || !right) return undefined;
  if (halves.length === 1) return left.length === 8 ? left : undefined;
  const omitted = 8 - left.length - right.length;
  return omitted > 0 ? [...left, ...Array<number>(omitted).fill(0), ...right] : undefined;
}

function isPublicAddress(address: string): boolean {
  const normalized = address.toLowerCase().split("%")[0];
  if (isIP(normalized) === 4) {
    const [a, b, c] = normalized.split(".").map(Number);
    return !(
      a === 0 ||
      a === 10 ||
      a === 127 ||
      (a === 100 && b >= 64 && b <= 127) ||
      (a === 169 && b === 254) ||
      (a === 172 && b >= 16 && b <= 31) ||
      (a === 192 && b === 0 && c === 0) ||
      (a === 192 && b === 0 && c === 2) ||
      (a === 192 && b === 88 && c === 99) ||
      (a === 192 && b === 168) ||
      (a === 198 && (b === 18 || b === 19)) ||
      (a === 198 && b === 51 && c === 100) ||
      (a === 203 && b === 0 && c === 113) ||
      a >= 224
    );
  }
  if (isIP(normalized) === 6) {
    const hextets = parseIpv6Hextets(normalized);
    if (!hextets) return false;
    const embeddedIpv4 = `${hextets[6] >>> 8}.${hextets[6] & 0xff}.${hextets[7] >>> 8}.${hextets[7] & 0xff}`;
    if (hextets.slice(0, 5).every((value) => value === 0) && hextets[5] === 0xffff) {
      return isPublicAddress(embeddedIpv4);
    }
    if (hextets.slice(0, 6).every((value) => value === 0)) return false;
    if ((hextets[0] & 0xe000) !== 0x2000) return false;
    if (hextets[0] === 0x2001 && hextets[1] < 0x0200) return false;
    if (hextets[0] === 0x2001 && hextets[1] === 0x0db8) return false;
    if (hextets[0] === 0x2002) {
      const relayIpv4 = `${hextets[1] >>> 8}.${hextets[1] & 0xff}.${hextets[2] >>> 8}.${hextets[2] & 0xff}`;
      return isPublicAddress(relayIpv4);
    }
    if (hextets[0] === 0x3fff && hextets[1] < 0x1000) return false;
    return true;
  }
  return false;
}

function parseSafeUrl(value: unknown): URL | undefined {
  if (typeof value !== "string" || value.length === 0 || value.length > 2_048) return undefined;
  let url: URL;
  try {
    url = new URL(value);
  } catch {
    return undefined;
  }
  if ((url.protocol !== "http:" && url.protocol !== "https:") || url.username || url.password) {
    return undefined;
  }
  const hostname = url.hostname
    .toLowerCase()
    .replace(/^\[|\]$/g, "")
    .replace(/\.$/, "");
  if (
    !hostname ||
    hostname === "localhost" ||
    hostname.endsWith(".localhost") ||
    hostname.endsWith(".local") ||
    hostname.endsWith(".internal") ||
    (isIP(hostname) > 0 && !isPublicAddress(hostname))
  ) {
    return undefined;
  }
  return url;
}

function decodeEntities(value: string): string {
  const named: Record<string, string> = {
    amp: "&",
    apos: "'",
    gt: ">",
    lt: "<",
    nbsp: " ",
    quot: '"',
  };
  return value.replace(/&(#x[\da-f]+|#\d+|[a-z]+);/gi, (match, entity: string) => {
    if (entity[0] !== "#") return named[entity.toLowerCase()] ?? match;
    const hexadecimal = entity[1]?.toLowerCase() === "x";
    const codePoint = Number.parseInt(entity.slice(hexadecimal ? 2 : 1), hexadecimal ? 16 : 10);
    try {
      return Number.isFinite(codePoint) ? String.fromCodePoint(codePoint) : match;
    } catch {
      return match;
    }
  });
}

function normalizedText(value: string): string {
  return decodeEntities(value)
    .replace(/\r\n?/g, "\n")
    .replace(/[\t ]+/g, " ")
    .replace(/ *\n */g, "\n")
    .replace(/\n{3,}/g, "\n\n")
    .trim();
}

function extractHtml(html: string): { content: string; title?: string } {
  const titleMatch = /<title\b[^>]*>([\s\S]*?)<\/title>/i.exec(html);
  const title = titleMatch ? normalizedText(titleMatch[1].replace(/<[^>]+>/g, " ")) : undefined;
  const content = normalizedText(
    html
      .replace(/<!--[\s\S]*?-->/g, " ")
      .replace(/<(script|style|noscript|svg)\b[^>]*>[\s\S]*?<\/\1>/gi, " ")
      .replace(/<\/?(?:article|aside|blockquote|br|div|footer|h[1-6]|header|li|main|nav|ol|p|pre|section|table|tr|ul)\b[^>]*>/gi, "\n")
      .replace(/<[^>]+>/g, " "),
  );
  return { content, ...(title ? { title } : {}) };
}

function truncateUtf8(value: string, maxBytes: number): { content: string; truncated: boolean } {
  if (Buffer.byteLength(value, "utf8") <= maxBytes) return { content: value, truncated: false };
  let content = "";
  let bytes = 0;
  for (const character of value) {
    const size = Buffer.byteLength(character, "utf8");
    if (bytes + size > maxBytes) break;
    content += character;
    bytes += size;
  }
  return { content, truncated: true };
}

async function cancelResponseBody(response: Response): Promise<void> {
  try {
    await response.body?.cancel();
  } catch {
    // The response is already rejected; cancellation is best-effort cleanup.
  }
}

export class WebReader {
  readonly #fetch: Fetch;
  readonly #maxRedirects: number;
  readonly #maxResponseBytes: number;
  readonly #resolve?: Resolve;
  readonly #timeoutMs: number;

  constructor(options: WebReaderOptions = {}) {
    if (options.fetch) {
      this.#fetch = options.fetch;
    } else {
      const safeLookup: LookupFunction = (hostname, lookupOptions, callback) => {
        void dnsLookup(hostname, { all: true, verbatim: true }).then(
          (addresses) => {
            if (addresses.length === 0 || addresses.some(({ address }) => !isPublicAddress(address))) {
              callback(new Error("web_read refused a private DNS result."), "", 0);
              return;
            }
            if (lookupOptions.all) callback(null, addresses);
            else callback(null, addresses[0].address, addresses[0].family);
          },
          (error: NodeJS.ErrnoException) => callback(error, "", 0),
        );
      };
      const dispatcher = new Agent({ connect: { lookup: safeLookup } });
      this.#fetch = ((input: URL | RequestInfo, init?: RequestInit) =>
        undiciFetch(input as string, {
          ...(init as Parameters<typeof undiciFetch>[1]),
          dispatcher,
        }) as unknown as Promise<Response>) as Fetch;
    }
    this.#maxRedirects = options.maxRedirects ?? DEFAULT_REDIRECTS;
    this.#maxResponseBytes = options.maxResponseBytes ?? DEFAULT_RESPONSE_BYTES;
    this.#resolve = options.resolve;
    this.#timeoutMs = options.timeoutMs ?? DEFAULT_TIMEOUT_MS;
  }

  async execute(arguments_: unknown, signal?: AbortSignal): Promise<LocalToolResultPayload> {
    const request = arguments_ as { maxBytes?: unknown; url?: unknown } | undefined;
    const initialUrl = parseSafeUrl(request?.url);
    if (!initialUrl) {
      return failure("unsafe_url", "web_read accepts only public HTTP or HTTPS URLs without credentials.");
    }
    const maxBytes = request?.maxBytes === undefined ? DEFAULT_CONTENT_BYTES : request.maxBytes;
    if (!Number.isInteger(maxBytes) || (maxBytes as number) < 1 || (maxBytes as number) > 65_536) {
      return failure("request_too_large", "web_read maxBytes must be an integer from 1 through 65536.");
    }
    if (signal?.aborted) {
      return failure("tool_error", "web_read was cancelled with its Agent Run.");
    }

    const visited = new Set<string>();
    let current = initialUrl;
    for (let redirectCount = 0; redirectCount <= this.#maxRedirects; redirectCount += 1) {
      if (signal?.aborted) {
        return failure("tool_error", "web_read was cancelled with its Agent Run.");
      }
      if (visited.has(current.href)) {
        return failure("redirect_error", "web_read stopped because the page redirected in a loop.");
      }
      visited.add(current.href);
      if (this.#resolve) {
        let addresses: Array<{ address: string }>;
        try {
          addresses = await this.#resolve(current.hostname);
        } catch {
          return failure("unreadable_content", "web_read could not resolve the page host.");
        }
        if (addresses.length === 0 || addresses.some(({ address }) => !isPublicAddress(address))) {
          return failure("unsafe_url", "web_read refused a host that resolves to a private address.");
        }
      }

      let response: Response;
      const controller = new AbortController();
      const abortFromRun = () => controller.abort(signal?.reason);
      signal?.addEventListener("abort", abortFromRun, { once: true });
      const timeout = setTimeout(() => controller.abort(), this.#timeoutMs);
      timeout.unref();
      const cleanup = () => {
        clearTimeout(timeout);
        signal?.removeEventListener("abort", abortFromRun);
      };
      try {
        response = await this.#fetch(current, {
          headers: { accept: "text/html, text/plain;q=0.9, application/xhtml+xml;q=0.8" },
          redirect: "manual",
          signal: controller.signal,
        });
      } catch (error) {
        cleanup();
        return failure(
          signal?.aborted ? "tool_error" : "unreadable_content",
          signal?.aborted
            ? "web_read was cancelled with its Agent Run."
            : controller.signal.aborted
              ? "web_read timed out while fetching the page."
              : "web_read could not fetch the page because of a network error.",
        );
      }

      if (REDIRECT_STATUSES.has(response.status)) {
        await cancelResponseBody(response);
        const location = response.headers.get("location");
        if (!location || redirectCount === this.#maxRedirects) {
          cleanup();
          return failure("redirect_error", "web_read stopped after too many or malformed redirects.");
        }
        let redirected: URL | undefined;
        try {
          redirected = parseSafeUrl(new URL(location, current).href);
        } catch {
          cleanup();
          return failure("redirect_error", "web_read received a malformed redirect target.");
        }
        if (!redirected) {
          cleanup();
          return failure("unsafe_url", "web_read refused an unsafe redirect target.");
        }
        cleanup();
        current = redirected;
        continue;
      }
      if (!response.ok) {
        await cancelResponseBody(response);
        cleanup();
        return failure("unreadable_content", `web_read received HTTP ${response.status} from the page.`);
      }

      const contentTypeHeader = response.headers.get("content-type") ?? "";
      const contentType = contentTypeHeader.split(";", 1)[0].trim().toLowerCase();
      if (!new Set(["text/html", "text/plain", "application/xhtml+xml"]).has(contentType)) {
        await cancelResponseBody(response);
        cleanup();
        return failure("unreadable_content", "web_read cannot extract this response content type.");
      }
      const declaredLength = Number(response.headers.get("content-length"));
      if (Number.isFinite(declaredLength) && declaredLength > this.#maxResponseBytes) {
        await cancelResponseBody(response);
        cleanup();
        return failure("response_too_large", "web_read refused an oversized response.");
      }
      if (!response.body) {
        cleanup();
        return failure("unreadable_content", "web_read received an empty response body.");
      }

      const reader = response.body.getReader();
      const chunks: Uint8Array[] = [];
      let responseBytes = 0;
      try {
        while (true) {
          const { done, value } = await reader.read();
          if (done) break;
          responseBytes += value.byteLength;
          if (responseBytes > this.#maxResponseBytes) {
            await reader.cancel();
            return failure("response_too_large", "web_read stopped reading an oversized response.");
          }
          chunks.push(value);
        }
      } catch (error) {
        return failure(
          signal?.aborted ? "tool_error" : "unreadable_content",
          signal?.aborted
            ? "web_read was cancelled with its Agent Run."
            : controller.signal.aborted
              ? "web_read timed out while reading the page."
              : "web_read could not read the response body.",
        );
      } finally {
        cleanup();
      }
      const bytes = new Uint8Array(responseBytes);
      let offset = 0;
      for (const chunk of chunks) {
        bytes.set(chunk, offset);
        offset += chunk.byteLength;
      }
      const charset = /(?:^|;)\s*charset\s*=\s*["']?([^;"'\s]+)/i.exec(contentTypeHeader)?.[1]
        ?? "utf-8";
      if (charset.length > 64) {
        return failure("unreadable_content", "web_read received an invalid response charset.");
      }
      let decoded: string;
      try {
        decoded = new TextDecoder(charset, { fatal: true }).decode(bytes);
      } catch {
        return failure("unreadable_content", "web_read could not decode the page charset.");
      }
      const extracted = contentType === "text/plain"
        ? { content: normalizedText(decoded) }
        : extractHtml(decoded);
      if (!extracted.content) {
        return failure("unreadable_content", "web_read could not extract readable text from the page.");
      }
      const bounded = truncateUtf8(extracted.content, maxBytes as number);
      return {
        ok: true,
        value: {
          type: "web_read",
          url: initialUrl.href,
          finalUrl: current.href,
          contentType,
          ...(extracted.title ? { sourceTitle: truncateUtf8(extracted.title, 512).content } : {}),
          content: bounded.content,
          truncated: bounded.truncated,
        },
      };
    }
    return failure("redirect_error", "web_read stopped after too many redirects.");
  }
}
