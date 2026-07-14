import { Buffer } from "node:buffer";

import { parseStrictJson } from "./strict_json";

export const DEFAULT_MAX_FRAME_BYTES = 16 * 1024 * 1024;

export class ProtocolFrameError extends Error {
    constructor(message: string) {
        super(message);
        this.name = "ProtocolFrameError";
    }
}

export function encodeFrame(value: unknown, maxBytes = DEFAULT_MAX_FRAME_BYTES): Buffer {
    requireFrameLimit(maxBytes);
    const text = JSON.stringify(value);
    if (text === undefined) throw new ProtocolFrameError("JSON-RPC frame is not serializable");
    const payload = Buffer.from(text, "utf8");
    if (payload.length === 0 || payload.length > maxBytes) {
        throw new ProtocolFrameError("JSON-RPC frame exceeds the configured limit");
    }
    const frame = Buffer.allocUnsafe(payload.length + 4);
    frame.writeUInt32BE(payload.length, 0);
    payload.copy(frame, 4);
    return frame;
}

export class FrameDecoder {
    private readonly maxBytes: number;
    private buffer = Buffer.alloc(0);
    private expectedBytes: number | null = null;

    constructor(maxBytes = DEFAULT_MAX_FRAME_BYTES) {
        requireFrameLimit(maxBytes);
        this.maxBytes = maxBytes;
    }

    get bufferedBytes(): number {
        return this.buffer.length;
    }

    /** True when a subsequent frame has started but is not complete yet. */
    get hasPendingFrame(): boolean {
        return this.buffer.length !== 0 || this.expectedBytes !== null;
    }

    feed(chunk: Uint8Array): unknown[] {
        if (!(chunk instanceof Uint8Array)) throw new ProtocolFrameError("frame chunk must be bytes");
        if (chunk.byteLength === 0) return [];
        this.buffer = this.buffer.length === 0
            ? Buffer.from(chunk)
            : Buffer.concat([this.buffer, Buffer.from(chunk)], this.buffer.length + chunk.byteLength);
        const values: unknown[] = [];
        while (true) {
            if (this.expectedBytes === null) {
                if (this.buffer.length < 4) break;
                this.expectedBytes = this.buffer.readUInt32BE(0);
                this.buffer = this.buffer.subarray(4);
                if (this.expectedBytes === 0 || this.expectedBytes > this.maxBytes) {
                    this.reset();
                    throw new ProtocolFrameError("invalid JSON-RPC frame length");
                }
            }
            if (this.buffer.length < this.expectedBytes) break;
            const payload = this.buffer.subarray(0, this.expectedBytes);
            this.buffer = this.buffer.subarray(this.expectedBytes);
            this.expectedBytes = null;
            try {
                const decoded = new TextDecoder("utf-8", { fatal: true }).decode(payload);
                values.push(parseStrictJson(decoded, { maximumCharacters: this.maxBytes }));
            } catch (error) {
                this.reset();
                throw new ProtocolFrameError("JSON-RPC frame is not strict UTF-8 JSON");
            }
        }
        return values;
    }

    end(): void {
        if (this.buffer.length !== 0 || this.expectedBytes !== null) {
            this.reset();
            throw new ProtocolFrameError("stream ended in a partial JSON-RPC frame");
        }
    }

    reset(): void {
        this.buffer = Buffer.alloc(0);
        this.expectedBytes = null;
    }
}

function requireFrameLimit(value: number): void {
    if (!Number.isSafeInteger(value) || value < 2 || value > 0xffffffff) {
        throw new RangeError("max frame bytes must be an unsigned 32-bit value >= 2");
    }
}
