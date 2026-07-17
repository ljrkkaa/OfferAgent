import { Buffer } from "node:buffer";
import { createHash } from "node:crypto";
import { lookup as dnsLookup } from "node:dns/promises";
import { createServer, request as httpRequest, type Server } from "node:http";
import { connect as netConnect, isIP } from "node:net";
import type { Duplex } from "node:stream";

import type { ExecutableToolCallDescriptor, ToolResultDescriptor, WebSourceRef } from "./generated_protocol";

export const RESEARCH_BROWSER_PARTITION = "persist:offeragent-research-v1";
const MAX_PAGE_BYTES = 65_536;
const MAX_FINGERPRINT_BYTES = 1_048_576;
const MAX_LINKS = 20;

export interface ResearchPageSnapshot {
    readonly url: string;
    readonly title: string;
    readonly text: string;
    readonly loginRequired: boolean;
    readonly links: readonly { title: string; url: string }[];
}

export interface ResearchPagePort {
    show(): void;
    open(url: string, signal: AbortSignal): Promise<void>;
    snapshot(signal: AbortSignal): Promise<ResearchPageSnapshot>;
    paginate(direction: "next" | "scroll", signal: AbortSignal): Promise<boolean>;
    back(signal: AbortSignal): Promise<void>;
    cancel(): void;
    close(): Promise<void>;
}

/** Read-only navigation actions over one isolated browser profile. */
export class ResearchBrowserAdapter {
    private readonly controllers = new Map<string, AbortController>();
    private readonly links = new Map<string, string>();
    private closed = false;

    constructor(private readonly page: ResearchPagePort) {}

    async execute(call: ExecutableToolCallDescriptor): Promise<ToolResultDescriptor> {
        if (call.name !== "research_browser.navigate" || call.version !== "1") {
            throw new Error(`unsupported Research Browser Tool: ${call.name}@${call.version}`);
        }
        if (this.closed) return failed(call, "resource.conflict", "Research Browser is closed.");
        const action = call.arguments.action;
        if (!["open", "read", "enumerate", "follow", "paginate", "back"].includes(String(action))) {
            return failed(call, "protocol.invalid_params", "Research Browser action is invalid.");
        }
        const controller = new AbortController();
        this.controllers.set(call.runId, controller);
        try {
            this.page.show();
            if (action === "open") return await this.open(call, controller.signal);
            if (action === "read") return await this.read(call, controller.signal);
            if (action === "enumerate") return await this.enumerate(call, controller.signal);
            if (action === "follow") return await this.follow(call, controller.signal);
            if (action === "paginate") return await this.paginate(call, controller.signal);
            return await this.back(call, controller.signal);
        } catch (error) {
            const message = controller.signal.aborted
                ? "Research Browser action was cancelled."
                : error instanceof UnsafeResearchUrlError || error instanceof OversizedResearchPageError
                    ? error.message : "Research Browser action failed.";
            const code = error instanceof UnsafeResearchUrlError ? "policy.denied"
                : error instanceof OversizedResearchPageError ? "protocol.message_too_large" : "resource.conflict";
            return failed(call, code, message, code === "resource.conflict");
        } finally {
            if (this.controllers.get(call.runId) === controller) this.controllers.delete(call.runId);
        }
    }

    cancelRun(runId: string): void {
        this.controllers.get(runId)?.abort();
        this.page.cancel();
    }

    async close(): Promise<void> {
        if (this.closed) return;
        this.closed = true;
        for (const controller of this.controllers.values()) controller.abort();
        this.controllers.clear();
        this.links.clear();
        this.page.cancel();
        await this.page.close();
    }

    private async open(call: ExecutableToolCallDescriptor, signal: AbortSignal): Promise<ToolResultDescriptor> {
        if (hasExtraKeys(call.arguments, ["action", "url"])) return invalidAction(call);
        const url = validatedPublicUrl(call.arguments.url);
        if (url === undefined) return failed(call, "policy.denied", "Research Browser accepts only public HTTP(S) URLs.");
        this.links.clear();
        await this.page.open(url.href, signal);
        return this.navigationResult(call, "open", await this.safeSnapshot(signal));
    }

    private async read(call: ExecutableToolCallDescriptor, signal: AbortSignal): Promise<ToolResultDescriptor> {
        if (hasExtraKeys(call.arguments, ["action"])) return invalidAction(call);
        const snapshot = await this.safeSnapshot(signal);
        const bounded = boundedUtf8(normalizeText(snapshot.text), MAX_PAGE_BYTES);
        return succeeded(call, "Read untrusted rendered page content.", {
            ...navigationData("read", snapshot),
            text: bounded.content,
            truncated: bounded.truncated,
            sourceFingerprint: digest(normalizeText(snapshot.text)),
        }, [webSource(snapshot)]);
    }

    private async enumerate(call: ExecutableToolCallDescriptor, signal: AbortSignal): Promise<ToolResultDescriptor> {
        if (hasExtraKeys(call.arguments, ["action"])) return invalidAction(call);
        const snapshot = await this.safeSnapshot(signal);
        this.links.clear();
        const links: Array<{ targetId: string; title: string; url: string }> = [];
        for (const candidate of snapshot.links) {
            if (links.length >= MAX_LINKS) break;
            const url = safePublicUrl(candidate.url);
            if (url === undefined) continue;
            const targetId = `link_${links.length + 1}`;
            this.links.set(targetId, url);
            links.push({ targetId, title: boundedUtf8(candidate.title.trim() || url, 512).content, url });
        }
        return succeeded(call, "Enumerated public navigation targets from untrusted page content.", {
            ...navigationData("enumerate", snapshot),
            links,
        }, [webSource(snapshot)]);
    }

    private async follow(call: ExecutableToolCallDescriptor, signal: AbortSignal): Promise<ToolResultDescriptor> {
        if (hasExtraKeys(call.arguments, ["action", "targetId"]) ||
            typeof call.arguments.targetId !== "string") return invalidAction(call);
        const url = this.links.get(call.arguments.targetId);
        if (url === undefined) {
            return failed(call, "resource.not_found", "Research Browser target was not returned by the latest enumeration.");
        }
        this.links.clear();
        await this.page.open(url, signal);
        return this.navigationResult(call, "follow", await this.safeSnapshot(signal));
    }

    private async paginate(call: ExecutableToolCallDescriptor, signal: AbortSignal): Promise<ToolResultDescriptor> {
        const direction = call.arguments.direction;
        if (hasExtraKeys(call.arguments, ["action", "direction"]) ||
            (direction !== "next" && direction !== "scroll")) return invalidAction(call);
        this.links.clear();
        if (!(await this.page.paginate(direction, signal))) {
            return failed(call, "resource.not_found", `Research Browser has no ${direction} navigation target.`);
        }
        return this.navigationResult(call, "paginate", await this.safeSnapshot(signal));
    }

    private async back(call: ExecutableToolCallDescriptor, signal: AbortSignal): Promise<ToolResultDescriptor> {
        if (hasExtraKeys(call.arguments, ["action"])) return invalidAction(call);
        this.links.clear();
        await this.page.back(signal);
        return this.navigationResult(call, "back", await this.safeSnapshot(signal));
    }

    private async safeSnapshot(signal: AbortSignal): Promise<ResearchPageSnapshot> {
        const snapshot = await this.page.snapshot(signal);
        if (validatedPublicUrl(snapshot.url) === undefined) throw new UnsafeResearchUrlError("Browser reached an unsafe URL.");
        if (Buffer.byteLength(snapshot.text, "utf8") > MAX_FINGERPRINT_BYTES) {
            throw new OversizedResearchPageError("Rendered page exceeds the Research Browser fingerprint limit.");
        }
        return snapshot;
    }

    private navigationResult(
        call: ExecutableToolCallDescriptor,
        action: "open" | "follow" | "paginate" | "back",
        snapshot: ResearchPageSnapshot,
    ): ToolResultDescriptor {
        if (validatedPublicUrl(snapshot.url) === undefined) {
            return failed(call, "policy.denied", "Research Browser reached an unsafe URL.");
        }
        return succeeded(
            call,
            "Completed isolated read-only browser navigation.",
            navigationData(action, snapshot),
            [webSource(snapshot)],
        );
    }
}

interface ElectronSessionLike {
    setProxy(configuration: {
        mode: "fixed_servers";
        proxyRules: string;
        proxyBypassRules: string;
    }): Promise<void>;
    setPermissionRequestHandler(handler: (
        webContents: unknown,
        permission: string,
        callback: (allowed: boolean) => void,
    ) => void): void;
    on(name: "will-download", handler: (event: { preventDefault(): void }) => void): void;
    webRequest: {
        onBeforeRequest(handler: (
            details: { url: string },
            callback: (response: { cancel: boolean }) => void,
        ) => void): void;
    };
}

interface ElectronWebContentsLike {
    session: ElectronSessionLike;
    executeJavaScript(script: string, userGesture?: boolean): Promise<unknown>;
    setWindowOpenHandler(handler: () => { action: "deny" }): void;
    on(name: "will-navigate" | "will-redirect", handler: (
        event: { preventDefault(): void },
        url: string,
    ) => void): void;
    once(name: "did-stop-loading" | "did-navigate-in-page", handler: () => void): void;
    removeListener(name: "did-stop-loading" | "did-navigate-in-page", handler: () => void): void;
    canGoBack(): boolean;
    goBack(): void;
    stop(): void;
}

interface ElectronWindowLike {
    readonly webContents: ElectronWebContentsLike;
    loadURL(url: string): Promise<void>;
    show(): void;
    focus(): void;
    destroy(): void;
    isDestroyed(): boolean;
}

interface ElectronBridge {
    BrowserWindow: new (options: Record<string, unknown>) => ElectronWindowLike;
}

type ResolveHost = (hostname: string) => Promise<readonly { address: string }[]>;

/** Pins every outbound connection to a validated public DNS result, preventing DNS rebinding to local services. */
class PublicNetworkProxy {
    private readonly sockets = new Set<Duplex>();
    private server?: Server;
    private startPromise?: Promise<number>;
    private closed = false;

    constructor(
        private readonly resolveHost: ResolveHost,
        private readonly connect: typeof netConnect = netConnect,
    ) {}

    async start(): Promise<number> {
        if (this.closed) throw new Error("Research Browser network proxy is closed.");
        if (this.startPromise !== undefined) return this.startPromise;
        this.startPromise = new Promise<number>((resolve, reject) => {
            const server = createServer((request, response) => {
                void this.forwardHttp(request, response).catch(() => {
                    if (!response.headersSent) response.writeHead(502);
                    response.end();
                });
            });
            this.server = server;
            server.on("connection", (socket) => this.track(socket));
            server.on("connect", (request, client, head) => {
                this.track(client);
                void this.tunnel(request.url, client, head).catch(() => client.destroy());
            });
            server.on("clientError", (_error, socket) => socket.destroy());
            server.once("error", reject);
            server.listen(0, "127.0.0.1", () => {
                server.off("error", reject);
                const address = server.address();
                if (address === null || typeof address === "string") {
                    reject(new Error("Research Browser proxy did not bind a local port."));
                    return;
                }
                resolve(address.port);
            });
        });
        return this.startPromise;
    }

    async close(): Promise<void> {
        this.closed = true;
        for (const socket of this.sockets) socket.destroy();
        await this.startPromise?.catch(() => undefined);
        const server = this.server;
        this.server = undefined;
        if (server !== undefined) await new Promise<void>((resolve) => server.close(() => resolve()));
        this.sockets.clear();
    }

    private async publicEndpoint(hostname: string): Promise<{ address: string; family: 4 | 6 }> {
        const normalized = hostname.toLocaleLowerCase().replace(/^\[|\]$/gu, "").replace(/\.$/u, "");
        if (!normalized || normalized === "localhost" || normalized.endsWith(".localhost") ||
            normalized.endsWith(".local") || normalized.endsWith(".internal")) {
            throw new UnsafeResearchUrlError("Research Browser proxy refused a local hostname.");
        }
        if (isIP(normalized) > 0) {
            if (!publicAddress(normalized)) throw new UnsafeResearchUrlError("Research Browser refused a private address.");
            return { address: normalized, family: isIP(normalized) as 4 | 6 };
        }
        const addresses = await this.resolveHost(normalized);
        if (this.closed || addresses.length === 0 || addresses.some(({ address }) => !publicAddress(address))) {
            throw new UnsafeResearchUrlError("Research Browser proxy refused a private DNS result.");
        }
        const family = isIP(addresses[0].address);
        if (family !== 4 && family !== 6) throw new UnsafeResearchUrlError("Research Browser DNS result is invalid.");
        return { address: addresses[0].address, family };
    }

    private async forwardHttp(
        request: import("node:http").IncomingMessage,
        response: import("node:http").ServerResponse,
    ): Promise<void> {
        const url = validatedPublicUrl(request.url);
        if (url === undefined || url.protocol !== "http:") {
            throw new UnsafeResearchUrlError("Research Browser proxy refused an unsafe HTTP request.");
        }
        const destination = await this.publicEndpoint(url.hostname);
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
        upstream.on("socket", (socket) => this.track(socket));
        upstream.once("error", () => {
            if (!response.headersSent) response.writeHead(502);
            response.end();
        });
        request.pipe(upstream);
    }

    private async tunnel(authority: string | undefined, client: Duplex, head: Buffer): Promise<void> {
        if (authority === undefined || Buffer.byteLength(authority, "utf8") > 2_048) {
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
        const destination = await this.publicEndpoint(target.hostname);
        const port = target.port ? Number(target.port) : 443;
        if (!Number.isSafeInteger(port) || port < 1 || port > 65_535) {
            client.destroy();
            return;
        }
        const upstream = this.connect({ host: destination.address, family: destination.family, port });
        this.track(upstream);
        upstream.once("connect", () => {
            client.write("HTTP/1.1 200 Connection Established\r\n\r\n");
            if (head.length > 0) upstream.write(head);
            client.pipe(upstream);
            upstream.pipe(client);
        });
        upstream.once("error", () => client.destroy());
    }

    private track(socket: Duplex): void {
        if (this.closed) {
            socket.destroy();
            return;
        }
        this.sockets.add(socket);
        socket.once("close", () => this.sockets.delete(socket));
    }
}

/** Electron implementation uses a separate partition and exposes no general interaction primitive. */
export class ElectronResearchPagePort implements ResearchPagePort {
    private window?: ElectronWindowLike;
    private closed = false;
    private readonly proxy: PublicNetworkProxy;

    constructor(
        private readonly bridge: ElectronBridge,
        private readonly resolveHost: ResolveHost = (hostname) => dnsLookup(hostname, { all: true, verbatim: true }),
    ) {
        this.proxy = new PublicNetworkProxy(resolveHost);
    }

    show(): void {
        const window = this.ensureWindow();
        window.show();
        window.focus();
    }

    async open(url: string, signal: AbortSignal): Promise<void> {
        if (!(await this.safeNetworkUrl(url))) throw new UnsafeResearchUrlError("Research Browser refused a private address.");
        const window = this.ensureWindow();
        const proxyPort = await withAbort(this.proxy.start(), signal, () => this.cancel());
        await withAbort(window.webContents.session.setProxy({
            mode: "fixed_servers",
            proxyRules: `http=127.0.0.1:${proxyPort};https=127.0.0.1:${proxyPort}`,
            proxyBypassRules: "<-loopback>",
        }), signal, () => this.cancel());
        await withAbort(window.loadURL(url), signal, () => this.cancel());
    }

    async snapshot(signal: AbortSignal): Promise<ResearchPageSnapshot> {
        const value = await withAbort(this.ensureWindow().webContents.executeJavaScript(SNAPSHOT_SCRIPT, false), signal,
            () => this.cancel());
        if (!isRecord(value) || typeof value.url !== "string" || typeof value.title !== "string" ||
            typeof value.text !== "string" || typeof value.loginRequired !== "boolean" || !Array.isArray(value.links) ||
            value.links.length > 100 || value.links.some((link) => !isRecord(link) ||
                typeof link.title !== "string" || typeof link.url !== "string")) {
            throw new Error("Research Browser returned a malformed page snapshot.");
        }
        return value as unknown as ResearchPageSnapshot;
    }

    async paginate(direction: "next" | "scroll", signal: AbortSignal): Promise<boolean> {
        if (direction === "scroll") {
            const moved = await withAbort(this.ensureWindow().webContents.executeJavaScript(SCROLL_SCRIPT, false), signal,
                () => this.cancel());
            return moved === true;
        }
        const next = await withAbort(this.ensureWindow().webContents.executeJavaScript(NEXT_SCRIPT, false), signal,
            () => this.cancel());
        const url = validatedPublicUrl(next);
        if (url === undefined) return false;
        await this.open(url.href, signal);
        return true;
    }

    async back(signal: AbortSignal): Promise<void> {
        const contents = this.ensureWindow().webContents;
        if (!contents.canGoBack()) throw new Error("Research Browser has no previous page.");
        await withAbort(waitForNavigation(contents, () => contents.goBack()), signal, () => this.cancel());
    }

    cancel(): void {
        if (this.window && !this.window.isDestroyed()) this.window.webContents.stop();
    }

    async close(): Promise<void> {
        this.closed = true;
        if (this.window && !this.window.isDestroyed()) this.window.destroy();
        this.window = undefined;
        await this.proxy.close();
    }

    private ensureWindow(): ElectronWindowLike {
        if (this.closed) throw new Error("Research Browser is closed.");
        if (this.window && !this.window.isDestroyed()) return this.window;
        const window = new this.bridge.BrowserWindow({
            title: "OfferAgent Research Browser — manual login only",
            show: true,
            width: 1_100,
            height: 800,
            webPreferences: {
                partition: RESEARCH_BROWSER_PARTITION,
                nodeIntegration: false,
                contextIsolation: true,
                sandbox: true,
            },
        });
        window.webContents.setWindowOpenHandler(() => ({ action: "deny" }));
        const navigationGuard = (event: { preventDefault(): void }, url: string): void => {
            if (validatedPublicUrl(url) === undefined) event.preventDefault();
        };
        window.webContents.on("will-navigate", navigationGuard);
        window.webContents.on("will-redirect", navigationGuard);
        window.webContents.session.setPermissionRequestHandler((_contents, _permission, callback) => callback(false));
        window.webContents.session.on("will-download", (event) => event.preventDefault());
        window.webContents.session.webRequest.onBeforeRequest((details, callback) => {
            void this.safeNetworkUrl(details.url).then(
                (safe) => callback({ cancel: !safe }),
                () => callback({ cancel: true }),
            );
        });
        this.window = window;
        return window;
    }

    private async safeNetworkUrl(value: unknown): Promise<boolean> {
        const url = validatedPublicUrl(value);
        if (this.closed || url === undefined) return false;
        const hostname = url.hostname.replace(/^\[|\]$/gu, "");
        if (isIP(hostname) > 0) return publicAddress(hostname);
        try {
            const addresses = await this.resolveHost(hostname);
            return !this.closed && addresses.length > 0 && addresses.every(({ address }) => publicAddress(address));
        } catch {
            return false;
        }
    }
}

class UnavailableResearchPagePort implements ResearchPagePort {
    show(): void {}
    async open(): Promise<void> { throw new Error("Electron Research Browser is unavailable."); }
    async snapshot(): Promise<ResearchPageSnapshot> { throw new Error("Electron Research Browser is unavailable."); }
    async paginate(): Promise<boolean> { throw new Error("Electron Research Browser is unavailable."); }
    async back(): Promise<void> { throw new Error("Electron Research Browser is unavailable."); }
    cancel(): void {}
    async close(): Promise<void> {}
}

export function createHostResearchBrowser(host: unknown = globalThis): ResearchBrowserAdapter {
    const candidate = host as { require?: (name: string) => unknown; window?: { require?: (name: string) => unknown } };
    const hostRequire = candidate.require ?? candidate.window?.require;
    if (hostRequire !== undefined) {
        try {
            const electron = hostRequire("electron") as Partial<ElectronBridge> & { remote?: Partial<ElectronBridge> };
            const BrowserWindow = electron.BrowserWindow ?? electron.remote?.BrowserWindow;
            if (typeof BrowserWindow === "function") {
                return new ResearchBrowserAdapter(new ElectronResearchPagePort({ BrowserWindow }));
            }
        } catch {
            // Tests and non-Electron hosts retain the explicit unavailable adapter.
        }
    }
    return new ResearchBrowserAdapter(new UnavailableResearchPagePort());
}

class UnsafeResearchUrlError extends Error {}
class OversizedResearchPageError extends Error {}

function navigationData(action: string, snapshot: ResearchPageSnapshot): Record<string, unknown> {
    const url = safePublicUrl(snapshot.url);
    if (url === undefined) throw new UnsafeResearchUrlError("Research Browser reached an unsafe URL.");
    return {
        action,
        status: snapshot.loginRequired ? "login_required" : "ready",
        title: boundedUtf8(snapshot.title.trim() || url, 512).content,
        url,
        untrusted: true,
        ...(snapshot.loginRequired ? {
            message: "Complete login or security checks manually in the visible isolated browser, then retry.",
        } : {}),
    };
}

function webSource(snapshot: ResearchPageSnapshot): WebSourceRef {
    const url = safePublicUrl(snapshot.url);
    if (url === undefined) throw new UnsafeResearchUrlError("Research Browser reached an unsafe URL.");
    return {
        type: "web",
        url,
        contentHash: digest(normalizeText(snapshot.text)),
        title: boundedUtf8(snapshot.title.trim() || url, 512).content,
        freshness: "fresh",
        label: "Research Browser page",
    };
}

function validatedPublicUrl(value: unknown): URL | undefined {
    if (typeof value !== "string" || Buffer.byteLength(value, "utf8") > 2_048) return undefined;
    try {
        const url = new URL(value);
        const hostname = url.hostname.toLocaleLowerCase().replace(/^\[|\]$/gu, "").replace(/\.$/u, "");
        if (!["http:", "https:"].includes(url.protocol) || url.username || url.password || !hostname ||
            hostname === "localhost" || hostname.endsWith(".localhost") || hostname.endsWith(".local") ||
            hostname.endsWith(".internal") || (isIP(hostname) > 0 && !publicAddress(hostname))) return undefined;
        return url;
    } catch {
        return undefined;
    }
}

function safePublicUrl(value: unknown): string | undefined {
    const url = validatedPublicUrl(value);
    if (url === undefined) return undefined;
    url.hash = "";
    for (const key of [...url.searchParams.keys()]) {
        if (/^(access[_-]?token|api[_-]?key|auth(?:orization)?|client[_-]?secret|code|credential|id[_-]?token|jwt|password|refresh[_-]?token|samlresponse|secret|session(?:[_-]?(?:id|state|token))?|sid|signature|sso[_-]?token|state|ticket|token)$/iu.test(key)) {
            url.searchParams.delete(key);
        }
    }
    return url.href;
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
                const value = Number.parseInt(field, 16);
                if (!/^[0-9a-f]{1,4}$/iu.test(field) || !Number.isInteger(value)) return undefined;
                values.push(value);
            }
        }
        return values;
    };
    const left = parseHalf(halves[0]);
    const right = parseHalf(halves[1] ?? "");
    if (left === undefined || right === undefined) return undefined;
    if (halves.length === 1) return left.length === 8 ? left : undefined;
    const omitted = 8 - left.length - right.length;
    return omitted > 0 ? [...left, ...Array<number>(omitted).fill(0), ...right] : undefined;
}

function publicAddress(address: string): boolean {
    const normalized = address.toLocaleLowerCase().split("%")[0];
    if (isIP(normalized) === 4) {
        const [a, b, c] = normalized.split(".").map(Number);
        return !(a === 0 || a === 10 || a === 127 || (a === 100 && b >= 64 && b <= 127) ||
            (a === 169 && b === 254) || (a === 172 && b >= 16 && b <= 31) ||
            (a === 192 && b === 168) || (a === 192 && b === 0 && c <= 2) ||
            (a === 192 && b === 88 && c === 99) ||
            (a === 198 && (b === 18 || b === 19 || (b === 51 && c === 100))) ||
            (a === 203 && b === 0 && c === 113) || a >= 224);
    }
    if (isIP(normalized) === 6) {
        const hextets = parseIpv6Hextets(normalized);
        if (hextets === undefined) return false;
        const embeddedIpv4 = `${hextets[6] >>> 8}.${hextets[6] & 0xff}.${hextets[7] >>> 8}.${hextets[7] & 0xff}`;
        if (hextets.slice(0, 5).every((value) => value === 0) && hextets[5] === 0xffff) {
            return publicAddress(embeddedIpv4);
        }
        if (hextets.slice(0, 6).every((value) => value === 0)) return false;
        if ((hextets[0] & 0xe000) !== 0x2000) return false;
        if (hextets[0] === 0x2001 && hextets[1] < 0x0200) return false;
        if (hextets[0] === 0x2001 && hextets[1] === 0x0db8) return false;
        if (hextets[0] === 0x2002) return publicAddress(
            `${hextets[1] >>> 8}.${hextets[1] & 0xff}.${hextets[2] >>> 8}.${hextets[2] & 0xff}`,
        );
        if (hextets[0] === 0x3fff && hextets[1] < 0x1000) return false;
        return true;
    }
    return false;
}

function normalizeText(value: string): string {
    return value.replace(/\r\n?/gu, "\n").replace(/[\t ]+/gu, " ").replace(/ *\n */gu, "\n")
        .replace(/\n{3,}/gu, "\n\n").trim();
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
    if (end > 0 && end < value.length && /[\uD800-\uDBFF]/u.test(value[end - 1]) &&
        /[\uDC00-\uDFFF]/u.test(value[end])) end -= 1;
    return { content: value.slice(0, end), truncated: true };
}

function digest(value: string): string {
    return `sha256:${createHash("sha256").update(value, "utf8").digest("hex")}`;
}

async function withAbort<T>(operation: Promise<T>, signal: AbortSignal, cancel: () => void): Promise<T> {
    if (signal.aborted) throw new Error("Research Browser action was cancelled.");
    return await new Promise<T>((resolve, reject) => {
        const abort = (): void => {
            cancel();
            reject(new Error("Research Browser action was cancelled."));
        };
        signal.addEventListener("abort", abort, { once: true });
        operation.then(resolve, reject).finally(() => signal.removeEventListener("abort", abort));
    });
}

function waitForNavigation(contents: ElectronWebContentsLike, action: () => void): Promise<void> {
    return new Promise((resolve, reject) => {
        const complete = (): void => { cleanup(); resolve(); };
        const timeout = setTimeout(() => { cleanup(); reject(new Error("Research Browser navigation timed out.")); }, 15_000);
        const cleanup = (): void => {
            clearTimeout(timeout);
            contents.removeListener("did-stop-loading", complete);
            contents.removeListener("did-navigate-in-page", complete);
        };
        contents.once("did-stop-loading", complete);
        contents.once("did-navigate-in-page", complete);
        action();
    });
}

function succeeded(
    call: ExecutableToolCallDescriptor,
    summary: string,
    data: Record<string, unknown>,
    sourceRefs: ToolResultDescriptor["sourceRefs"] = [],
): ToolResultDescriptor {
    return { toolCallId: call.toolCallId, status: "succeeded", summary, data, sourceRefs, retryable: false };
}

function failed(
    call: ExecutableToolCallDescriptor,
    code: "protocol.invalid_params" | "protocol.message_too_large" | "resource.not_found" |
        "resource.conflict" | "policy.denied",
    message: string,
    retryable = false,
): ToolResultDescriptor {
    return {
        toolCallId: call.toolCallId,
        status: "failed",
        summary: message,
        data: {},
        retryable,
        error: { code, retryable, cancelled: false, userVisibleMessage: message, details: {} },
    };
}

function invalidAction(call: ExecutableToolCallDescriptor): ToolResultDescriptor {
    return failed(call, "protocol.invalid_params", "Research Browser action arguments are invalid.");
}

function hasExtraKeys(value: Readonly<Record<string, unknown>>, allowed: readonly string[]): boolean {
    return Object.keys(value).some((key) => !allowed.includes(key));
}

function isRecord(value: unknown): value is Record<string, unknown> {
    return value !== null && typeof value === "object" && !Array.isArray(value);
}

const SNAPSHOT_SCRIPT = `(() => {
    const root = document.body || document.documentElement;
    const text = String(root?.innerText || '').slice(0, 1048577);
    const links = Array.from(document.querySelectorAll('a[href]')).slice(0, 100).map((link) => ({
        title: String(link.textContent || link.getAttribute('aria-label') || '').trim().slice(0, 512),
        url: (() => { try { return String(new URL(link.getAttribute('href') || '', document.baseURI).href).slice(0, 2048); }
            catch { return ''; } })(),
    }));
    return {
        url: String(location.href).slice(0, 2048),
        title: String(document.title || '').slice(0, 512),
        text,
        loginRequired: Boolean(document.querySelector('input[type="password"]')) ||
            /captcha|security check|verify (?:you are|your identity)/iu.test(text.slice(0, 4096)),
        links,
    };
})()`;

const NEXT_SCRIPT = `(() => {
    const link = document.querySelector('a[href][rel="next"], a[href][aria-label*="next" i]');
    try { return link ? String(new URL(link.getAttribute('href') || '', document.baseURI).href).slice(0, 2048) : ''; }
    catch { return ''; }
})()`;

const SCROLL_SCRIPT = `(() => {
    const before = window.scrollY;
    window.scrollBy(0, Math.max(window.innerHeight * 0.9, 400));
    return window.scrollY !== before;
})()`;
