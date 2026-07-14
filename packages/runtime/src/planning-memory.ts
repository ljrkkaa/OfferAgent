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
