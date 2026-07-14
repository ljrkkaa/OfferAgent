export class StrictJsonError extends Error {
    constructor(message: string) {
        super(message);
        this.name = "StrictJsonError";
    }
}

export interface StrictJsonOptions {
    readonly maximumDepth?: number;
    readonly maximumNodes?: number;
    readonly maximumCharacters?: number;
}

/** Parse one JSON document while rejecting duplicate object members.
 *
 * Native JSON.parse deliberately accepts duplicate keys.  That is unsuitable
 * for signed manifests, durable journals and protocol messages because Python
 * rejects the same bytes.  This bounded recursive-descent parser preserves the
 * normal JavaScript JSON value shapes while making the accepted language
 * unambiguous across both runtimes.
 */
export function parseStrictJson(text: string, options: StrictJsonOptions = {}): unknown {
    if (typeof text !== "string") throw new StrictJsonError("JSON input must be text");
    const maximumDepth = boundedInteger(options.maximumDepth ?? 64, 1, 256, "maximumDepth");
    const maximumNodes = boundedInteger(options.maximumNodes ?? 100_000, 1, 1_000_000, "maximumNodes");
    const maximumCharacters = boundedInteger(
        options.maximumCharacters ?? 16 * 1024 * 1024,
        2,
        256 * 1024 * 1024,
        "maximumCharacters",
    );
    if (text.length < 1 || text.length > maximumCharacters) {
        throw new StrictJsonError("JSON input is outside the configured size limit");
    }
    return new Parser(text, maximumDepth, maximumNodes).parse();
}

class Parser {
    private readonly text: string;
    private readonly maximumDepth: number;
    private readonly maximumNodes: number;
    private offset = 0;
    private nodes = 0;

    constructor(text: string, maximumDepth: number, maximumNodes: number) {
        this.text = text;
        this.maximumDepth = maximumDepth;
        this.maximumNodes = maximumNodes;
    }

    parse(): unknown {
        this.whitespace();
        const value = this.value(0);
        this.whitespace();
        if (this.offset !== this.text.length) throw new StrictJsonError("JSON document has trailing content");
        return value;
    }

    private value(depth: number): unknown {
        if (depth > this.maximumDepth) throw new StrictJsonError("JSON exceeds the configured nesting depth");
        this.nodes += 1;
        if (this.nodes > this.maximumNodes) throw new StrictJsonError("JSON exceeds the configured node limit");
        const current = this.text[this.offset];
        if (current === "{") return this.object(depth);
        if (current === "[") return this.array(depth);
        if (current === '"') return this.string();
        if (current === "t") return this.literal("true", true);
        if (current === "f") return this.literal("false", false);
        if (current === "n") return this.literal("null", null);
        if (current === "-" || (current >= "0" && current <= "9")) return this.number();
        throw new StrictJsonError("JSON value is invalid");
    }

    private object(depth: number): Record<string, unknown> {
        this.offset += 1;
        this.whitespace();
        const result: Record<string, unknown> = {};
        const keys = new Set<string>();
        if (this.consume("}")) return result;
        while (true) {
            if (this.text[this.offset] !== '"') throw new StrictJsonError("JSON object key must be a string");
            const key = this.string();
            if (keys.has(key)) throw new StrictJsonError("JSON object contains a duplicate member");
            keys.add(key);
            this.whitespace();
            if (!this.consume(":")) throw new StrictJsonError("JSON object member is missing ':'");
            this.whitespace();
            const item = this.value(depth + 1);
            // Defining an own data property prevents the legacy __proto__ setter
            // from mutating the result prototype while preserving exact keys.
            Object.defineProperty(result, key, {
                configurable: true,
                enumerable: true,
                value: item,
                writable: true,
            });
            this.whitespace();
            if (this.consume("}")) return result;
            if (!this.consume(",")) throw new StrictJsonError("JSON object members must be comma-separated");
            this.whitespace();
        }
    }

    private array(depth: number): unknown[] {
        this.offset += 1;
        this.whitespace();
        const result: unknown[] = [];
        if (this.consume("]")) return result;
        while (true) {
            result.push(this.value(depth + 1));
            this.whitespace();
            if (this.consume("]")) return result;
            if (!this.consume(",")) throw new StrictJsonError("JSON array items must be comma-separated");
            this.whitespace();
        }
    }

    private string(): string {
        const start = this.offset;
        this.offset += 1;
        while (this.offset < this.text.length) {
            const code = this.text.charCodeAt(this.offset);
            if (code < 0x20) throw new StrictJsonError("JSON string contains a control character");
            if (code === 0x22) {
                this.offset += 1;
                try {
                    const value: unknown = JSON.parse(this.text.slice(start, this.offset));
                    if (typeof value !== "string") throw new StrictJsonError("JSON string is invalid");
                    return value;
                } catch (error) {
                    if (error instanceof StrictJsonError) throw error;
                    throw new StrictJsonError("JSON string is invalid");
                }
            }
            if (code === 0x5c) {
                this.offset += 1;
                const escape = this.text[this.offset];
                if ('"\\/bfnrt'.includes(escape)) {
                    this.offset += 1;
                    continue;
                }
                if (escape === "u" && /^[0-9a-fA-F]{4}$/.test(this.text.slice(this.offset + 1, this.offset + 5))) {
                    this.offset += 5;
                    continue;
                }
                throw new StrictJsonError("JSON string escape is invalid");
            }
            this.offset += 1;
        }
        throw new StrictJsonError("JSON string is unterminated");
    }

    private number(): number {
        const match = /-?(?:0|[1-9]\d*)(?:\.\d+)?(?:[eE][+-]?\d+)?/.exec(this.text.slice(this.offset));
        if (!match || match.index !== 0) throw new StrictJsonError("JSON number is invalid");
        this.offset += match[0].length;
        const value = Number(match[0]);
        if (!Number.isFinite(value)) throw new StrictJsonError("JSON number is not finite");
        return value;
    }

    private literal<T>(token: string, value: T): T {
        if (!this.text.startsWith(token, this.offset)) throw new StrictJsonError("JSON literal is invalid");
        this.offset += token.length;
        return value;
    }

    private whitespace(): void {
        while (this.offset < this.text.length && /[\x20\x09\x0a\x0d]/.test(this.text[this.offset])) {
            this.offset += 1;
        }
    }

    private consume(value: string): boolean {
        if (this.text[this.offset] !== value) return false;
        this.offset += 1;
        return true;
    }
}

function boundedInteger(value: number, minimum: number, maximum: number, name: string): number {
    if (!Number.isSafeInteger(value) || value < minimum || value > maximum) {
        throw new RangeError(`${name} is outside its supported range`);
    }
    return value;
}
