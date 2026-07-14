import type { VaultAction } from "@offeragent/protocol";
import type { ModelConversationItem, ModelProvider } from "./model-provider";

export type MemoryTopicType = "feedback" | "project" | "study" | "user";

export interface MemoryTopicMetadata {
  description: string;
  modifiedVersion: string;
  name: string;
  path: string;
  type: MemoryTopicType;
}

export interface MemoryTopicBody {
  content: string;
  modifiedVersion: string;
  path: string;
}

export interface RecalledMemoryTopic extends MemoryTopicMetadata {
  content: string;
}

export interface MemorySelectionInput {
  conversationContext: Array<Extract<ModelConversationItem, { type: "assistant_message" | "user_message" }>>;
  request: string;
  topics: MemoryTopicMetadata[];
}

export interface SemanticMemorySelector {
  select(input: MemorySelectionInput): Promise<unknown>;
}

export interface PlanningMemoryDependencies {
  listTopics(): Promise<unknown>;
  readTopics(paths: string[]): Promise<unknown>;
  selector: SemanticMemorySelector;
}

export interface PlanningMemoryRecall {
  feedback: RecalledMemoryTopic[];
  planning: RecalledMemoryTopic[];
}

export const MEMORY_SELECTOR_INSTRUCTIONS =
  "OfferAgent Planning Memory semantic selector. Return only a JSON array of zero to five topic paths ordered by meaning-based relevance to the current request and conversation context. Do not use keyword routing and do not return explanations.";

export const MEMORY_CAPTURE_INSTRUCTIONS =
  "OfferAgent Planning Memory semantic fallback. Examine only the two new messages supplied for this Agent Run. Return a JSON array of at most three operations, or []. Each operation is either {kind:'upsert',path,type,name,description,content} or {kind:'delete',path}. Capture only durable user, feedback, project, or study understanding. Consolidate related facts into an existing recalled topic, replace corrected facts, and delete superseded topics. Do not use trigger phrases or keyword routing. Never return paths outside memory/user, memory/feedback, memory/project, or memory/study.";

export type MemoryCaptureOperation =
  | { kind: "delete"; path: string }
  | {
      content: string;
      description: string;
      kind: "upsert";
      name: string;
      path: string;
      type: MemoryTopicType;
    };

export class ProviderSemanticMemoryCapture {
  readonly #provider: ModelProvider;
  readonly #model: string;
  readonly #fastMode: boolean;
  readonly #signal: AbortSignal;

  constructor(options: { provider: ModelProvider; model: string; fastMode: boolean; signal: AbortSignal }) {
    this.#provider = options.provider;
    this.#model = options.model;
    this.#fastMode = options.fastMode;
    this.#signal = options.signal;
  }

  async extract(input: { newMessages: ModelConversationItem[]; recalledTopics: RecalledMemoryTopic[] }): Promise<MemoryCaptureOperation[]> {
    return this.#extract(input, false);
  }

  async extractStrict(input: { newMessages: ModelConversationItem[]; recalledTopics: RecalledMemoryTopic[] }): Promise<MemoryCaptureOperation[]> {
    return this.#extract(input, true);
  }

  async #extract(
    input: { newMessages: ModelConversationItem[]; recalledTopics: RecalledMemoryTopic[] },
    strict: boolean,
  ): Promise<MemoryCaptureOperation[]> {
    let output = "";
    for await (const event of this.#provider.stream({
      model: this.#model,
      ...(this.#fastMode ? { fastMode: true } : {}),
      input: [{ type: "user_message", text: JSON.stringify(input) }],
      instructions: MEMORY_CAPTURE_INSTRUCTIONS,
      signal: this.#signal,
      tools: [],
    })) {
      if (event.type !== "output_text.delta") continue;
      output += event.delta;
      if (Buffer.byteLength(output, "utf8") > 65_536) {
        if (strict) throw new Error("Planning Memory capture output exceeds 65536 UTF-8 bytes.");
        return [];
      }
    }
    let parsed: unknown;
    try {
      parsed = JSON.parse(output);
    } catch {
      if (strict) throw new Error("Planning Memory capture output is not valid JSON.");
      return [];
    }
    const operations = parseCaptureOperations(parsed);
    if (operations) return operations;
    if (strict) throw new Error("Planning Memory capture output contains invalid operations.");
    return [];
  }
}

export function validateCaptureOperations(value: unknown): MemoryCaptureOperation[] {
  return parseCaptureOperations(value) ?? [];
}

function parseCaptureOperations(value: unknown): MemoryCaptureOperation[] | undefined {
  if (!Array.isArray(value) || value.length > 3) return undefined;
  const operations: MemoryCaptureOperation[] = [];
  const paths = new Set<string>();
  for (const candidate of value) {
    if (!candidate || typeof candidate !== "object" || Array.isArray(candidate)) return undefined;
    const operation = candidate as Record<string, unknown>;
    const match = typeof operation.path === "string" ? MEMORY_PATH.exec(operation.path) : null;
    if (!match || paths.has(operation.path as string)) return undefined;
    if (operation.kind === "delete" && Object.keys(operation).every((key) => key === "kind" || key === "path")) {
      operations.push({ kind: "delete", path: operation.path as string });
    } else if (
      operation.kind === "upsert" && operation.type === match[1] &&
      typeof operation.name === "string" && operation.name.trim() && !operation.name.includes("\n") &&
      Buffer.byteLength(operation.name, "utf8") <= 128 &&
      typeof operation.description === "string" && operation.description.trim() && !operation.description.includes("\n") &&
      Buffer.byteLength(operation.description, "utf8") <= 512 &&
      typeof operation.content === "string" && operation.content.trim() &&
      Buffer.byteLength(operation.content, "utf8") <= MAX_TOPIC_BODY_BYTES
    ) {
      operations.push({
        kind: "upsert",
        path: operation.path as string,
        type: operation.type as MemoryTopicType,
        name: operation.name.trim(),
        description: operation.description.trim(),
        content: operation.content.trim(),
      });
    } else return undefined;
    paths.add(operation.path as string);
  }
  return operations;
}

export function buildMemoryChangeActions(input: {
  operations: MemoryCaptureOperation[];
  topics: MemoryTopicMetadata[];
  bodies: RecalledMemoryTopic[];
  index?: { content: string; modifiedVersion: string };
}): { actions: VaultAction[]; changedPaths: string[] } | undefined {
  if (input.operations.length === 0) return undefined;
  const topics = new Map(input.topics.map((topic) => [topic.path, topic]));
  const bodies = new Map(input.bodies.map((topic) => [topic.path, topic]));
  const actions: VaultAction[] = [];
  for (const [sequence, operation] of input.operations.entries()) {
    const existing = bodies.get(operation.path);
    if (operation.kind === "delete") {
      if (!existing) continue;
      actions.push({
        actionId: `memory-delete-${sequence + 1}`,
        idempotencyKey: `memory-delete-${sequence + 1}`,
        operation: "delete",
        path: operation.path,
        expectedVersion: existing.modifiedVersion,
      });
      topics.delete(operation.path);
      continue;
    }
    const content = `---\nname: ${JSON.stringify(operation.name)}\ndescription: ${JSON.stringify(operation.description)}\ntype: ${operation.type}\n---\n\n${operation.content}\n`;
    const topicAction: VaultAction = existing ? {
      actionId: `memory-update-${sequence + 1}`,
      idempotencyKey: `memory-update-${sequence + 1}`,
      operation: "exact_replace",
      path: operation.path,
      expectedVersion: existing.modifiedVersion,
      expectedContent: existing.content,
      replacement: content,
    } : {
      actionId: `memory-create-${sequence + 1}`,
      idempotencyKey: `memory-create-${sequence + 1}`,
      operation: "create",
      path: operation.path,
      expectedVersion: "missing",
      content,
    };
    if (!(existing && content === existing.content)) actions.push(topicAction);
    topics.set(operation.path, {
      path: operation.path,
      type: operation.type,
      name: operation.name,
      description: operation.description,
      modifiedVersion: existing?.modifiedVersion ?? "missing",
    });
  }
  if (actions.length === 0) return undefined;
  const indexContent = `# Planning Memory\n\n${[...topics.values()]
    .sort((left, right) => left.path.localeCompare(right.path))
    .map((topic) => `- [${topic.name}](${topic.path.replace(/^memory\//, "")}) - ${topic.description}`)
    .join("\n")}\n`;
  actions.push(input.index ? {
    actionId: "memory-index-update",
    idempotencyKey: "memory-index-update",
    operation: "exact_replace",
    path: "memory/MEMORY.md",
    expectedVersion: input.index.modifiedVersion,
    expectedContent: input.index.content,
    replacement: indexContent,
  } : {
    actionId: "memory-index-create",
    idempotencyKey: "memory-index-create",
    operation: "create",
    path: "memory/MEMORY.md",
    expectedVersion: "missing",
    content: indexContent,
  });
  return { actions, changedPaths: actions.slice(0, -1).map((action) => action.path) };
}

export class ProviderSemanticMemorySelector implements SemanticMemorySelector {
  readonly #fastMode: boolean;
  readonly #model: string;
  readonly #provider: ModelProvider;
  readonly #signal: AbortSignal;

  constructor(options: {
    fastMode: boolean;
    model: string;
    provider: ModelProvider;
    signal: AbortSignal;
  }) {
    this.#fastMode = options.fastMode;
    this.#model = options.model;
    this.#provider = options.provider;
    this.#signal = options.signal;
  }

  async select(input: MemorySelectionInput): Promise<unknown> {
    let output = "";
    for await (const event of this.#provider.stream({
      model: this.#model,
      ...(this.#fastMode ? { fastMode: true } : {}),
      input: [{ type: "user_message", text: JSON.stringify(input) }],
      instructions: MEMORY_SELECTOR_INSTRUCTIONS,
      signal: this.#signal,
      tools: [],
    })) {
      if (event.type !== "output_text.delta") continue;
      output += event.delta;
      if (Buffer.byteLength(output, "utf8") > 8_192) return [];
    }
    try {
      return JSON.parse(output) as unknown;
    } catch {
      return [];
    }
  }
}

const MEMORY_PATH = /^memory\/(user|feedback|project|study)\/[^/.][^/]*\.md$/;
const MAX_TOPIC_BODY_BYTES = 32_768;
const MAX_TOPICS = 5;

function validMetadata(value: unknown): value is MemoryTopicMetadata {
  if (!value || typeof value !== "object" || Array.isArray(value)) return false;
  const topic = value as Record<string, unknown>;
  const match = typeof topic.path === "string" ? MEMORY_PATH.exec(topic.path) : null;
  return Boolean(
    match &&
      Buffer.byteLength(topic.path as string, "utf8") <= 512 &&
      topic.type === match![1] &&
      typeof topic.name === "string" &&
      topic.name.trim() &&
      Buffer.byteLength(topic.name, "utf8") <= 128 &&
      typeof topic.description === "string" &&
      topic.description.trim() &&
      Buffer.byteLength(topic.description, "utf8") <= 512 &&
      typeof topic.modifiedVersion === "string" &&
      topic.modifiedVersion,
  );
}

function validBody(value: unknown): value is MemoryTopicBody {
  if (!value || typeof value !== "object" || Array.isArray(value)) return false;
  const body = value as Record<string, unknown>;
  return (
    typeof body.path === "string" &&
    MEMORY_PATH.test(body.path) &&
    typeof body.modifiedVersion === "string" &&
    typeof body.content === "string" &&
    Buffer.byteLength(body.content, "utf8") <= MAX_TOPIC_BODY_BYTES
  );
}

export class PlanningMemoryModule {
  readonly #dependencies: PlanningMemoryDependencies;

  constructor(dependencies: PlanningMemoryDependencies) {
    this.#dependencies = dependencies;
  }

  async recall(input: Omit<MemorySelectionInput, "topics">): Promise<PlanningMemoryRecall> {
    const listed = await this.#dependencies.listTopics();
    if (!Array.isArray(listed)) return { feedback: [], planning: [] };
    const topics = listed.filter(validMetadata);
    if (topics.length === 0) return { feedback: [], planning: [] };
    const metadataByPath = new Map(topics.map((topic) => [topic.path, topic]));
    const selection = await this.#dependencies.selector.select({ ...input, topics });
    if (!Array.isArray(selection)) return { feedback: [], planning: [] };
    const paths: string[] = [];
    for (const candidate of selection) {
      if (
        typeof candidate === "string" &&
        metadataByPath.has(candidate) &&
        !paths.includes(candidate)
      ) {
        paths.push(candidate);
      }
      if (paths.length === MAX_TOPICS) break;
    }
    if (paths.length === 0) return { feedback: [], planning: [] };
    const read = await this.#dependencies.readTopics(paths);
    if (!Array.isArray(read)) return { feedback: [], planning: [] };
    const bodies = new Map(
      read
        .filter(validBody)
        .map((body) => [body.path, body]),
    );
    const recalled = paths.flatMap((topicPath): RecalledMemoryTopic[] => {
      const metadata = metadataByPath.get(topicPath);
      const body = bodies.get(topicPath);
      if (!metadata || !body || metadata.modifiedVersion !== body.modifiedVersion) return [];
      return [{ ...metadata, content: body.content }];
    });
    return {
      feedback: recalled.filter(({ type }) => type === "feedback"),
      planning: recalled.filter(({ type }) => type !== "feedback"),
    };
  }
}
