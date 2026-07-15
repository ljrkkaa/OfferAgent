import { createHash } from "node:crypto";
import { lookup as dnsLookup } from "node:dns/promises";
import { createServer, request as httpRequest, type Server } from "node:http";
import { isIP } from "node:net";
import { connect as netConnect } from "node:net";
import type { Duplex } from "node:stream";
import type {
  AgentRunEvent,
  LocalToolResultPayload,
  ResearchBrowserAction,
  ResearchBrowserResult,
} from "@offeragent/protocol";

export const RESEARCH_BROWSER_PARTITION = "persist:offeragent-research";
const MAX_PAGE_BYTES = 65_536;
const MAX_READ_BYTES = 32_768;
const MAX_LINKS = 20;
const MAX_FINGERPRINT_BYTES = 1_048_576;
const RESEARCH_BROWSER_FAILURE_MESSAGE = "The Research Browser action failed.";

type ResearchToolCall = Extract<AgentRunEvent, { type: "tool_call.requested" }>;
type Failure = Extract<LocalToolResultPayload, { ok: false }>;

export interface ResearchPageSnapshot {
  links: Array<{ title: string; url: string }>;
  loginRequired: boolean;
  nextUrl?: string;
  scrollUrl?: string;
  sourceFingerprint?: string;
  text: string;
  title: string;
  url: string;
}

export interface ResearchPageAdapter {
  back(signal: AbortSignal): Promise<void>;
  cancel(): void;
  close(): Promise<void>;
  open(url: string, signal: AbortSignal): Promise<void>;
  paginate(direction: "next" | "scroll", signal: AbortSignal): Promise<boolean>;
  show(): void;
  snapshot(signal: AbortSignal): Promise<ResearchPageSnapshot>;
}

interface ElectronSessionLike {
  on(name: "will-download", handler: (event: { preventDefault(): void }) => void): void;
  setPermissionRequestHandler(
    handler: (webContents: unknown, permission: string, callback: (allowed: boolean) => void) => void,
  ): void;
  setProxy(configuration: {
    mode: "fixed_servers";
    proxyBypassRules: string;
    proxyRules: string;
  }): Promise<void>;
  webRequest: {
    onBeforeRequest(
      handler: (
        details: { method: string; resourceType?: string; url: string },
        callback: (response: { cancel: boolean }) => void,
      ) => void,
    ): void;
  };
}

interface ElectronWebContentsLike {
  canGoBack(): boolean;
  executeJavaScript(script: string, userGesture?: boolean): Promise<unknown>;
  goBack(): void;
  on(
    name: "will-navigate" | "will-redirect",
    handler: (event: { preventDefault(): void }, url: string) => void,
  ): void;
  once(
    name: "did-navigate-in-page" | "did-stop-loading",
    handler: () => void,
  ): void;
  removeListener(
    name: "did-navigate-in-page" | "did-stop-loading",
    handler: () => void,
  ): void;
  session: ElectronSessionLike;
  setWindowOpenHandler(handler: (details: unknown) => { action: "deny" }): void;
  stop(): void;
}

interface ElectronWindowLike {
  destroy(): void;
  focus(): void;
  isDestroyed(): boolean;
  loadURL(url: string): Promise<void>;
  show(): void;
  webContents: ElectronWebContentsLike;
}

export interface ElectronBridge {
  BrowserWindow: new (options: Record<string, unknown>) => ElectronWindowLike;
}

type ResolveHost = (hostname: string) => Promise<Array<{ address: string }>>;

class UnsafeResearchUrlError extends Error {}
class OversizedResearchPageError extends Error {}

function failure(code: Failure["error"]["code"], message: string): Failure {
  return { ok: false, error: { code, message } };
}

function boundedUtf8(value: string, maximumBytes: number): { content: string; truncated: boolean } {
  if (Buffer.byteLength(value, "utf8") <= maximumBytes) return { content: value, truncated: false };
  let lower = 0;
  let upper = Math.min(value.length, maximumBytes);
  while (lower < upper) {
    const middle = Math.ceil((lower + upper) / 2);
    if (Buffer.byteLength(value.slice(0, middle), "utf8") <= maximumBytes) lower = middle;
    else upper = middle - 1;
  }
  let end = lower;
  if (
    end > 0 && end < value.length &&
    value.charCodeAt(end - 1) >= 0xD800 && value.charCodeAt(end - 1) <= 0xDBFF &&
    value.charCodeAt(end) >= 0xDC00 && value.charCodeAt(end) <= 0xDFFF
  ) end -= 1;
  return { content: value.slice(0, end), truncated: true };
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
    return !(a === 0 || a === 10 || a === 127 ||
      (a === 100 && b >= 64 && b <= 127) ||
      (a === 169 && b === 254) || (a === 172 && b >= 16 && b <= 31) ||
      (a === 192 && b === 0 && c === 0) || (a === 192 && b === 0 && c === 2) ||
      (a === 192 && b === 88 && c === 99) || (a === 192 && b === 168) ||
      (a === 198 && (b === 18 || b === 19)) || (a === 198 && b === 51 && c === 100) ||
      (a === 203 && b === 0 && c === 113) || a >= 224);
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
    if (hextets[0] === 0x2002) return isPublicAddress(
      `${hextets[1] >>> 8}.${hextets[1] & 0xff}.${hextets[2] >>> 8}.${hextets[2] & 0xff}`,
    );
    if (hextets[0] === 0x3fff && hextets[1] < 0x1000) return false;
    return true;
  }
  return false;
}

function validatedPublicUrl(value: unknown): URL | undefined {
  if (typeof value !== "string" || Buffer.byteLength(value, "utf8") > 2_048) return undefined;
  let url: URL;
  try {
    url = new URL(value);
  } catch {
    return undefined;
  }
  const hostname = url.hostname.toLowerCase().replace(/^\[|\]$/gu, "").replace(/\.$/u, "");
  if (
    (url.protocol !== "http:" && url.protocol !== "https:") ||
    url.username || url.password ||
    !hostname || hostname === "localhost" || hostname.endsWith(".localhost") ||
    hostname.endsWith(".local") || hostname.endsWith(".internal") ||
    (isIP(hostname) > 0 && !isPublicAddress(hostname))
  ) return undefined;
  return url;
}

function safePublicUrl(value: unknown): string | undefined {
  const url = validatedPublicUrl(value);
  if (!url) return undefined;
  url.hash = "";
  const sensitiveQueryKey = /^(?:access[_-]?token|api[_-]?key|assertion|auth(?:orization)?|client[_-]?secret|code|credential|id[_-]?token|jwt|key|password|refresh[_-]?token|samlresponse|secret|session(?:[_-]?(?:id|state|token))?|sid|sig(?:nature)?|sso[_-]?token|state|ticket|token)$/iu;
  for (const key of [...url.searchParams.keys()]) {
    if (sensitiveQueryKey.test(key)) url.searchParams.delete(key);
  }
  return url.href;
}

function normalizedRenderedText(value: string): string {
  return value
    .replace(/\r\n?/gu, "\n")
    .replace(/[\t ]+/gu, " ")
    .replace(/ *\n */gu, "\n")
    .replace(/\n{3,}/gu, "\n\n")
    .trim();
}

class PublicNetworkProxy {
  readonly #connect: typeof netConnect;
  readonly #resolveHost: ResolveHost;
  readonly #sockets = new Set<Duplex>();
  #closed = false;
  #server?: Server;
  #startPromise?: Promise<number>;

  constructor(resolveHost: ResolveHost, connect: typeof netConnect = netConnect) {
    this.#connect = connect;
    this.#resolveHost = resolveHost;
  }

  async start(): Promise<number> {
    if (this.#closed) throw new Error("The Research Browser proxy is closed.");
    if (this.#startPromise) return this.#startPromise;
    this.#startPromise = new Promise<number>((resolve, reject) => {
      const server = createServer((request, response) => {
        void this.#forwardHttp(request, response).catch(() => {
          if (!response.headersSent) response.writeHead(502);
          response.end();
        });
      });
      this.#server = server;
      server.on("connection", (socket) => this.#track(socket));
      server.on("connect", (request, client, head) => {
        this.#track(client);
        void this.#tunnel(request.url, client, head).catch(() => { client.destroy(); });
      });
      server.on("clientError", (_error, socket) => { socket.destroy(); });
      server.once("error", reject);
      server.listen(0, "127.0.0.1", () => {
        server.off("error", reject);
        const address = server.address();
        if (!address || typeof address === "string") {
          reject(new Error("The Research Browser proxy did not bind a local port."));
          return;
        }
        resolve(address.port);
      });
    });
    return this.#startPromise;
  }

  async close(): Promise<void> {
    this.#closed = true;
    for (const socket of this.#sockets) socket.destroy();
    try {
      await this.#startPromise;
    } catch {
      // A failed or cancelled startup still needs its partially created server closed.
    }
    for (const socket of this.#sockets) socket.destroy();
    this.#sockets.clear();
    const server = this.#server;
    this.#server = undefined;
    this.#startPromise = undefined;
    if (!server) return;
    await new Promise<void>((resolve) => server.close(() => resolve()));
  }

  async #publicAddress(hostname: string): Promise<{ address: string; family: 4 | 6 }> {
    if (this.#closed) throw new Error("The Research Browser proxy is closed.");
    const normalized = hostname.toLowerCase().replace(/^\[|\]$/gu, "").replace(/\.$/u, "");
    if (!normalized || normalized === "localhost" || normalized.endsWith(".localhost") ||
        normalized.endsWith(".local") || normalized.endsWith(".internal")) {
      throw new UnsafeResearchUrlError("The Research Browser proxy refused a local hostname.");
    }
    if (isIP(normalized) > 0) {
      if (!isPublicAddress(normalized)) throw new UnsafeResearchUrlError("The Research Browser proxy refused a private address.");
      return { address: normalized, family: isIP(normalized) as 4 | 6 };
    }
    const addresses = await this.#resolveHost(normalized);
    if (this.#closed) throw new Error("The Research Browser proxy is closed.");
    if (addresses.length === 0 || addresses.some(({ address }) => !isPublicAddress(address))) {
      throw new UnsafeResearchUrlError("The Research Browser proxy refused a private DNS result.");
    }
    return { address: addresses[0].address, family: isIP(addresses[0].address) as 4 | 6 };
  }

  async #forwardHttp(
    request: import("node:http").IncomingMessage,
    response: import("node:http").ServerResponse,
  ): Promise<void> {
    const url = validatedPublicUrl(request.url);
    if (!url) throw new UnsafeResearchUrlError("The Research Browser proxy refused an unsafe request URL.");
    if (url.protocol !== "http:") throw new UnsafeResearchUrlError("The Research Browser proxy expected an HTTP request.");
    const destination = await this.#publicAddress(url.hostname);
    if (this.#closed) throw new Error("The Research Browser proxy is closed.");
    const headers: Record<string, string | string[] | undefined> = { ...request.headers, host: url.host };
    delete headers["proxy-connection"];
    const upstream = httpRequest({
      host: destination.address,
      family: destination.family,
      port: url.port ? Number(url.port) : 80,
      method: request.method,
      path: `${url.pathname}${url.search}`,
      headers,
    }, (upstreamResponse) => {
      response.writeHead(upstreamResponse.statusCode ?? 502, upstreamResponse.headers);
      upstreamResponse.pipe(response);
    });
    upstream.on("socket", (socket) => this.#track(socket));
    upstream.once("error", () => {
      if (!response.headersSent) response.writeHead(502);
      response.end();
    });
    request.pipe(upstream);
  }

  async #tunnel(authority: string | undefined, client: Duplex, head: Buffer): Promise<void> {
    if (!authority || Buffer.byteLength(authority, "utf8") > 2_048) {
      client.destroy();
      return;
    }
    let target: URL;
    try {
      target = new URL(`https://${authority}`);
    } catch {
      client.destroy();
      return;
    }
    const destination = await this.#publicAddress(target.hostname);
    if (this.#closed) {
      client.destroy();
      return;
    }
    const port = target.port ? Number(target.port) : 443;
    if (!Number.isInteger(port) || port < 1 || port > 65_535) {
      client.destroy();
      return;
    }
    const upstream = this.#connect({ host: destination.address, family: destination.family, port });
    this.#track(upstream);
    upstream.once("connect", () => {
      client.write("HTTP/1.1 200 Connection Established\r\n\r\n");
      if (head.length > 0) upstream.write(head);
      client.pipe(upstream);
      upstream.pipe(client);
    });
    upstream.once("error", () => { client.destroy(); });
  }

  #track(socket: Duplex): void {
    if (this.#closed) {
      socket.destroy();
      return;
    }
    this.#sockets.add(socket);
    socket.once("close", () => this.#sockets.delete(socket));
  }
}

function successful(value: ResearchBrowserResult): LocalToolResultPayload {
  return { ok: true, value };
}

export class ResearchBrowser {
  readonly #adapter: ResearchPageAdapter;
  readonly #controllers = new Map<string, AbortController>();
  #closed = false;
  #links = new Map<string, string>();

  constructor(adapter: ResearchPageAdapter) {
    this.#adapter = adapter;
  }

  async execute(call: ResearchToolCall): Promise<LocalToolResultPayload> {
    if (call.tool.name !== "research_browser") {
      return failure("permission_denied", "The Research Browser received an unavailable action.");
    }
    if (this.#closed) return failure("tool_error", "The Research Browser is closed.");
    if (!call.tool.arguments || typeof call.tool.arguments !== "object" || Array.isArray(call.tool.arguments)) {
      return failure("request_too_large", "research_browser arguments must be an object.");
    }
    const input = call.tool.arguments as Record<string, unknown>;
    const action = input.action;
    if (!(["open", "read", "enumerate", "follow", "paginate", "back"] as unknown[]).includes(action)) {
      return failure("permission_denied", "Research Browser actions are limited to open, read, enumerate, follow, paginate, and back.");
    }
    const controller = new AbortController();
    this.#controllers.set(call.agentRunId, controller);
    try {
      this.#adapter.show();
      if (action === "open") return await this.#open(input, controller.signal);
      if (action === "read") return await this.#read(input, controller.signal);
      if (action === "enumerate") return await this.#enumerate(input, controller.signal);
      if (action === "follow") return await this.#follow(input, controller.signal);
      if (action === "paginate") return await this.#paginate(input, controller.signal);
      return await this.#back(input, controller.signal);
    } catch (error) {
      return failure(
        error instanceof UnsafeResearchUrlError
          ? "unsafe_url"
          : error instanceof OversizedResearchPageError
            ? "response_too_large"
            : "tool_error",
        controller.signal.aborted
          ? "The Research Browser action was cancelled."
          : error instanceof UnsafeResearchUrlError || error instanceof OversizedResearchPageError
            ? error.message
            : RESEARCH_BROWSER_FAILURE_MESSAGE,
      );
    } finally {
      if (this.#controllers.get(call.agentRunId) === controller) {
        this.#controllers.delete(call.agentRunId);
      }
    }
  }

  cancelRun(agentRunId: string): void {
    const controller = this.#controllers.get(agentRunId);
    if (!controller) return;
    controller.abort();
    this.#adapter.cancel();
  }

  async close(): Promise<void> {
    if (this.#closed) return;
    this.#closed = true;
    for (const controller of this.#controllers.values()) controller.abort();
    this.#controllers.clear();
    this.#links.clear();
    this.#adapter.cancel();
    await this.#adapter.close();
  }

  async #navigationResult(
    action: ResearchBrowserAction,
    signal: AbortSignal,
  ): Promise<LocalToolResultPayload> {
    const page = await this.#adapter.snapshot(signal);
    const url = safePublicUrl(page.url);
    if (!url) return failure("unsafe_url", "The Research Browser reached an unsafe URL.");
    const title = boundedUtf8(page.title.trim() || url, 512).content;
    return successful({
      action,
      ...(page.loginRequired
        ? { message: "Complete login or security checks manually in the visible Research Browser, then retry the read-only action." }
        : {}),
      status: page.loginRequired ? "login_required" : "ready",
      title,
      type: "research_browser",
      untrusted: true,
      url,
    });
  }

  async #open(input: Record<string, unknown>, signal: AbortSignal): Promise<LocalToolResultPayload> {
    if (Object.keys(input).some((key) => !["action", "url"].includes(key))) {
      return failure("permission_denied", "The open action accepts only a public URL.");
    }
    const url = validatedPublicUrl(input.url);
    if (!url) return failure("unsafe_url", "The Research Browser accepts only public HTTP(S) URLs without credentials.");
    this.#links.clear();
    await this.#adapter.open(url.href, signal);
    return this.#navigationResult("open", signal);
  }

  async #read(input: Record<string, unknown>, signal: AbortSignal): Promise<LocalToolResultPayload> {
    if (Object.keys(input).some((key) => !["action", "maxBytes"].includes(key))) {
      return failure("permission_denied", "The read action does not accept page instructions or scripts.");
    }
    const maxBytes = input.maxBytes === undefined ? MAX_READ_BYTES : input.maxBytes;
    if (!Number.isInteger(maxBytes) || (maxBytes as number) < 1 || (maxBytes as number) > MAX_READ_BYTES) {
      return failure("request_too_large", `research_browser read maxBytes must be from 1 through ${MAX_READ_BYTES}.`);
    }
    const page = await this.#adapter.snapshot(signal);
    if (page.loginRequired) return this.#navigationResult("read", signal);
    const url = safePublicUrl(page.url);
    if (!url) return failure("unsafe_url", "The Research Browser reached an unsafe URL.");
    const normalizedText = normalizedRenderedText(page.text);
    const pageText = boundedUtf8(normalizedText, MAX_PAGE_BYTES).content;
    const content = boundedUtf8(pageText, maxBytes as number);
    return successful({
      action: "read",
      content: content.content,
      sourceFingerprint: page.sourceFingerprint ??
        `sha256:${createHash("sha256").update(normalizedText, "utf8").digest("hex")}`,
      status: "ready",
      title: boundedUtf8(page.title.trim() || url, 512).content,
      truncated: content.truncated || Buffer.byteLength(normalizedText, "utf8") > MAX_PAGE_BYTES,
      type: "research_browser",
      untrusted: true,
      url,
    });
  }

  async #enumerate(input: Record<string, unknown>, signal: AbortSignal): Promise<LocalToolResultPayload> {
    if (Object.keys(input).some((key) => !["action", "limit"].includes(key))) {
      return failure("permission_denied", "The enumerate action accepts only a bounded result limit.");
    }
    const limit = input.limit === undefined ? 10 : input.limit;
    if (!Number.isInteger(limit) || (limit as number) < 1 || (limit as number) > MAX_LINKS) {
      return failure("request_too_large", `research_browser enumerate limit must be from 1 through ${MAX_LINKS}.`);
    }
    const page = await this.#adapter.snapshot(signal);
    if (page.loginRequired) return this.#navigationResult("enumerate", signal);
    const url = safePublicUrl(page.url);
    if (!url) return failure("unsafe_url", "The Research Browser reached an unsafe URL.");
    const candidates = page.links.flatMap((entry) => {
      const navigationUrl = validatedPublicUrl(entry.url);
      const displayUrl = safePublicUrl(entry.url);
      const title = boundedUtf8(entry.title.trim(), 512).content;
      return navigationUrl && displayUrl && title
        ? [{ displayUrl, navigationUrl: navigationUrl.href, title }]
        : [];
    });
    const unique = [...new Map(candidates.map((entry) => [entry.navigationUrl, entry])).values()];
    const selected = unique.slice(0, limit as number);
    this.#links = new Map(selected.map((entry, index) => [`result-${index + 1}`, entry.navigationUrl]));
    return successful({
      action: "enumerate",
      entries: selected.map((entry, index) => ({
        id: `result-${index + 1}`,
        title: entry.title,
        url: entry.displayUrl,
      })),
      status: "ready",
      title: boundedUtf8(page.title.trim() || url, 512).content,
      truncated: unique.length > selected.length,
      type: "research_browser",
      untrusted: true,
      url,
    });
  }

  async #follow(input: Record<string, unknown>, signal: AbortSignal): Promise<LocalToolResultPayload> {
    if (Object.keys(input).some((key) => !["action", "targetId"].includes(key))) {
      return failure("permission_denied", "The follow action accepts only an enumerated target ID.");
    }
    const url = typeof input.targetId === "string" ? this.#links.get(input.targetId) : undefined;
    if (!url) return failure("not_found", "The Research Browser target was not returned by the latest enumeration.");
    this.#links.clear();
    await this.#adapter.open(url, signal);
    return this.#navigationResult("follow", signal);
  }

  async #paginate(input: Record<string, unknown>, signal: AbortSignal): Promise<LocalToolResultPayload> {
    if (Object.keys(input).some((key) => !["action", "direction"].includes(key)) ||
        (input.direction !== "next" && input.direction !== "scroll")) {
      return failure("permission_denied", "Pagination is limited to next or scroll.");
    }
    this.#links.clear();
    const moved = await this.#adapter.paginate(input.direction, signal);
    if (!moved) return failure("not_found", `The page has no ${input.direction} navigation target.`);
    return this.#navigationResult("paginate", signal);
  }

  async #back(input: Record<string, unknown>, signal: AbortSignal): Promise<LocalToolResultPayload> {
    if (Object.keys(input).some((key) => key !== "action")) {
      return failure("permission_denied", "The back action accepts no additional arguments.");
    }
    this.#links.clear();
    await this.#adapter.back(signal);
    return this.#navigationResult("back", signal);
  }
}

export class ElectronResearchPageAdapter implements ResearchPageAdapter {
  readonly #bridge: ElectronBridge;
  readonly #proxy: PublicNetworkProxy;
  readonly #resolveHost: ResolveHost;
  #closed = false;
  #window?: ElectronWindowLike;

  constructor(
    bridge: ElectronBridge,
    resolveHost: ResolveHost = (hostname) => dnsLookup(hostname, { all: true, verbatim: true }),
    connect: typeof netConnect = netConnect,
  ) {
    this.#bridge = bridge;
    this.#resolveHost = resolveHost;
    this.#proxy = new PublicNetworkProxy(resolveHost, connect);
  }

  show(): void {
    const window = this.#ensureWindow();
    window.show();
    window.focus();
  }

  async open(url: string, signal: AbortSignal): Promise<void> {
    this.#assertOpen();
    const safe = await this.#withAbort(this.#isSafeNetworkUrl(url), signal);
    this.#assertOpen();
    if (!safe) {
      throw new UnsafeResearchUrlError("The Research Browser refused a host that resolves to a private address.");
    }
    const window = this.#ensureWindow();
    const proxyPort = await this.#withAbort(this.#proxy.start(), signal);
    this.#assertOpen();
    await this.#withAbort(window.webContents.session.setProxy({
      mode: "fixed_servers",
      proxyRules: `http=127.0.0.1:${proxyPort};https=127.0.0.1:${proxyPort}`,
      proxyBypassRules: "<-loopback>",
    }), signal);
    this.#assertOpen();
    await this.#withAbort(window.loadURL(url), signal);
    this.#assertOpen();
  }

  async snapshot(signal: AbortSignal): Promise<ResearchPageSnapshot> {
    const value = await this.#withAbort(
      this.#ensureWindow().webContents.executeJavaScript(`(() => {
        const encoder = new TextEncoder();
        const maximumBytes = 1048576;
        const parts = [];
        let bytes = 0;
        let visited = 0;
        let contentTooLarge = false;
        const append = (value) => {
          let offset = 0;
          while (offset < value.length) {
            let end = Math.min(value.length, offset + 4096);
            if (end < value.length && value.charCodeAt(end - 1) >= 0xD800 &&
                value.charCodeAt(end - 1) <= 0xDBFF) end -= 1;
            const chunk = value.slice(offset, end);
            const chunkBytes = encoder.encode(chunk).byteLength;
            if (bytes + chunkBytes > maximumBytes) return false;
            parts.push(chunk);
            bytes += chunkBytes;
            offset = end;
          }
          return true;
        };
        const root = document.body || document.documentElement;
        const stack = [{ node: root, exiting: false, block: false }];
        outer: while (stack.length > 0) {
          const frame = stack.pop();
          if (frame.exiting) {
            if (frame.block && !append('\\n')) { contentTooLarge = true; break; }
            continue;
          }
          visited += 1;
          if (visited > 100000) { contentTooLarge = true; break; }
          const node = frame.node;
          if (node.nodeType === Node.TEXT_NODE) {
            if (!append(String(node.nodeValue || ''))) { contentTooLarge = true; break; }
            continue;
          }
          if (node.nodeType !== Node.ELEMENT_NODE) continue;
          const element = node;
          if (/^(?:SCRIPT|STYLE|NOSCRIPT|SVG)$/i.test(element.tagName) || element.hidden ||
              element.getAttribute('aria-hidden') === 'true') continue;
          const style = getComputedStyle(element);
          if (style.display === 'none' || style.visibility === 'hidden') continue;
          if (element.tagName === 'BR') {
            if (!append('\\n')) contentTooLarge = true;
            continue;
          }
          const block = !style.display.startsWith('inline') && style.display !== 'contents';
          if (block && !append('\\n')) { contentTooLarge = true; break; }
          stack.push({ node: element, exiting: true, block });
          for (let child = element.lastChild; child; child = child.previousSibling) {
            if (stack.length + visited >= 100000) { contentTooLarge = true; break outer; }
            stack.push({ node: child, exiting: false, block: false });
          }
        }
        const fullText = contentTooLarge ? '' : parts.join('');
        const hasPasswordInput = Boolean(document.querySelector('input[type="password"]'));
        const links = [];
        for (const link of document.querySelectorAll('a[href]')) {
          if (links.length >= 100) break;
          const titleParts = [];
          let titleLength = 0;
          const titleWalker = document.createTreeWalker(link, NodeFilter.SHOW_TEXT);
          while (titleWalker.nextNode() && titleLength < 512) {
            const part = String(titleWalker.currentNode.nodeValue || '').slice(0, 512 - titleLength);
            titleParts.push(part);
            titleLength += part.length;
          }
          links.push({
            title: String(titleParts.join('') || link.getAttribute('aria-label') || '').trim().slice(0, 512),
            url: (() => {
            const raw = String(link.getAttribute('href') || '');
            if (encoder.encode(raw).byteLength > 2048) return '';
            try {
              const resolved = String(new URL(raw, document.baseURI).href);
              return encoder.encode(resolved).byteLength <= 2048 ? resolved : '';
            } catch { return ''; }
            })(),
          });
        }
        return { contentTooLarge, fullText, hasPasswordInput, links, title: String(document.title || "").slice(0, 512), url: String(location.href) };
      })()`,
      false),
      signal,
    );
    if (!value || typeof value !== "object") throw new Error("The Research Browser returned an invalid page snapshot.");
    const snapshot = value as Partial<ResearchPageSnapshot> & {
      contentTooLarge?: unknown;
      fullText?: unknown;
      hasPasswordInput?: unknown;
    };
    if (
      typeof snapshot.url !== "string" || typeof snapshot.title !== "string" ||
      typeof snapshot.contentTooLarge !== "boolean" || typeof snapshot.fullText !== "string" ||
      typeof snapshot.hasPasswordInput !== "boolean" ||
      !Array.isArray(snapshot.links)
    ) throw new Error("The Research Browser returned a malformed rendered page.");
    if (snapshot.contentTooLarge || Buffer.byteLength(snapshot.fullText, "utf8") > MAX_FINGERPRINT_BYTES) {
      throw new OversizedResearchPageError("The rendered page exceeds the Research Browser fingerprint limit.");
    }
    const normalizedText = normalizedRenderedText(snapshot.fullText);
    return {
      links: snapshot.links.flatMap((entry) =>
        entry && typeof entry.title === "string" && Buffer.byteLength(entry.title, "utf8") <= 512 &&
          typeof entry.url === "string" && Buffer.byteLength(entry.url, "utf8") <= 2_048
          ? [entry]
          : []),
      loginRequired: snapshot.hasPasswordInput ||
        /security check|verify (?:you are|your identity)|captcha/iu.test(normalizedText.slice(0, 4_096)),
      sourceFingerprint: `sha256:${createHash("sha256").update(normalizedText, "utf8").digest("hex")}`,
      text: boundedUtf8(normalizedText, MAX_PAGE_BYTES).content,
      title: snapshot.title,
      url: snapshot.url,
    };
  }

  async back(signal: AbortSignal): Promise<void> {
    const contents = this.#ensureWindow().webContents;
    if (!contents.canGoBack()) throw new Error("The Research Browser has no previous page.");
    await this.#waitForNavigation(() => contents.goBack(), signal);
  }

  async paginate(direction: "next" | "scroll", signal: AbortSignal): Promise<boolean> {
    const result = await this.#withAbort(
      this.#ensureWindow().webContents.executeJavaScript(
        direction === "scroll"
          ? `(() => new Promise((resolve) => {
              const before = window.scrollY;
              const beforeHeight = document.documentElement.scrollHeight;
              const beforeChildren = document.body?.childElementCount || 0;
              let done = false;
              let timer;
              const finish = () => {
                if (done) return;
                done = true;
                clearTimeout(timer);
                observer.disconnect();
                resolve(window.scrollY !== before || document.documentElement.scrollHeight !== beforeHeight ||
                  (document.body?.childElementCount || 0) !== beforeChildren);
              };
              const observer = new MutationObserver(finish);
              observer.observe(document.body || document.documentElement, { childList: true, subtree: true });
              window.scrollBy(0, Math.max(window.innerHeight * 0.9, 400));
              timer = setTimeout(finish, 1500);
            }))()`
          : `(() => {
              const target = document.querySelector('a[href][rel="next"], a[href][aria-label*="next" i]');
              if (!target) return '';
              const raw = String(target.getAttribute('href') || '');
              if (new TextEncoder().encode(raw).byteLength > 2048) return '';
              try {
                const resolved = String(new URL(raw, document.baseURI).href);
                return new TextEncoder().encode(resolved).byteLength <= 2048 ? resolved : '';
              } catch { return ''; }
            })()`,
        false,
      ),
      signal,
    );
    if (direction === "scroll") return result === true;
    const nextUrl = validatedPublicUrl(result);
    if (!nextUrl) return false;
    await this.open(nextUrl.href, signal);
    return true;
  }

  cancel(): void {
    if (this.#window && !this.#window.isDestroyed()) this.#window.webContents.stop();
  }

  async close(): Promise<void> {
    this.#closed = true;
    if (this.#window && !this.#window.isDestroyed()) this.#window.destroy();
    this.#window = undefined;
    await this.#proxy.close();
  }

  #ensureWindow(): ElectronWindowLike {
    this.#assertOpen();
    if (this.#window && !this.#window.isDestroyed()) return this.#window;
    const window = new this.#bridge.BrowserWindow({
      title: "OfferAgent Research Browser - manual login only",
      show: true,
      width: 1100,
      height: 800,
      webPreferences: {
        partition: RESEARCH_BROWSER_PARTITION,
        nodeIntegration: false,
        contextIsolation: true,
        sandbox: true,
      },
    });
    window.webContents.setWindowOpenHandler(() => ({ action: "deny" }));
    const guardNavigation = (event: { preventDefault(): void }, url: string): void => {
      if (!validatedPublicUrl(url)) event.preventDefault();
    };
    window.webContents.on("will-navigate", guardNavigation);
    window.webContents.on("will-redirect", guardNavigation);
    window.webContents.session.setPermissionRequestHandler((_contents, _permission, callback) => callback(false));
    window.webContents.session.on("will-download", (event) => event.preventDefault());
    window.webContents.session.webRequest.onBeforeRequest((details, callback) => {
      void this.#isSafeNetworkUrl(details.url).then(
        (safe) => callback({ cancel: !safe }),
        () => callback({ cancel: true }),
      );
    });
    this.#window = window;
    return window;
  }

  async #withAbort<T>(operation: Promise<T>, signal: AbortSignal): Promise<T> {
    if (signal.aborted) throw new Error("The Research Browser action was cancelled.");
    return await new Promise<T>((resolve, reject) => {
      const abort = () => {
        this.cancel();
        reject(new Error("The Research Browser action was cancelled."));
      };
      signal.addEventListener("abort", abort, { once: true });
      operation.then(resolve, reject).finally(() => signal.removeEventListener("abort", abort));
    });
  }

  async #isSafeNetworkUrl(value: unknown): Promise<boolean> {
    if (this.#closed) return false;
    if (typeof value !== "string" || Buffer.byteLength(value, "utf8") > 2_048) return false;
    let networkUrl: URL;
    try {
      networkUrl = new URL(value);
    } catch {
      return false;
    }
    const validationUrl = new URL(networkUrl.href);
    if (validationUrl.protocol === "ws:") validationUrl.protocol = "http:";
    else if (validationUrl.protocol === "wss:") validationUrl.protocol = "https:";
    const safe = validatedPublicUrl(validationUrl.href);
    if (!safe) return false;
    const hostname = safe.hostname.replace(/^\[|\]$/gu, "");
    if (isIP(hostname) > 0) return isPublicAddress(hostname);
    try {
      const addresses = await this.#resolveHost(hostname);
      return !this.#closed && addresses.length > 0 && addresses.every(({ address }) => isPublicAddress(address));
    } catch {
      return false;
    }
  }

  async #waitForNavigation(action: () => void, signal: AbortSignal): Promise<void> {
    const contents = this.#ensureWindow().webContents;
    await this.#withAbort(new Promise<void>((resolve, reject) => {
      const complete = (): void => {
        cleanup();
        resolve();
      };
      const timeout = setTimeout(() => {
        cleanup();
        reject(new Error("The Research Browser navigation timed out."));
      }, 15_000);
      const cleanup = (): void => {
        clearTimeout(timeout);
        contents.removeListener("did-stop-loading", complete);
        contents.removeListener("did-navigate-in-page", complete);
      };
      contents.once("did-stop-loading", complete);
      contents.once("did-navigate-in-page", complete);
      action();
    }), signal);
  }

  #assertOpen(): void {
    if (this.#closed) throw new Error("The Research Browser is closed.");
  }
}

class UnavailableResearchPageAdapter implements ResearchPageAdapter {
  async open(): Promise<void> { throw new Error("The Electron Research Browser is unavailable."); }
  async snapshot(): Promise<ResearchPageSnapshot> { throw new Error("The Electron Research Browser is unavailable."); }
  async back(): Promise<void> { throw new Error("The Electron Research Browser is unavailable."); }
  async paginate(): Promise<boolean> { throw new Error("The Electron Research Browser is unavailable."); }
  show(): void {}
  cancel(): void {}
  async close(): Promise<void> {}
}

export function createHostResearchBrowser(
  host: unknown = globalThis,
  resolveHost?: ResolveHost,
): ResearchBrowser {
  const candidate = host as {
    require?: (name: string) => unknown;
    window?: { require?: (name: string) => unknown };
  };
  const hostRequire = candidate.require ?? candidate.window?.require;
  if (hostRequire) {
    try {
      const electron = hostRequire("electron") as Partial<ElectronBridge> & {
        remote?: Partial<ElectronBridge>;
      };
      const BrowserWindow = electron.BrowserWindow ?? electron.remote?.BrowserWindow;
      if (typeof BrowserWindow === "function") {
        return new ResearchBrowser(new ElectronResearchPageAdapter({ BrowserWindow }, resolveHost));
      }
    } catch {
      // Obsidian test and non-Electron hosts use the explicit unavailable adapter.
    }
  }
  return new ResearchBrowser(new UnavailableResearchPageAdapter());
}
