import { createHash, randomUUID } from "node:crypto";
import { mkdir, readFile, readdir, rename, stat, unlink, writeFile } from "node:fs/promises";
import path from "node:path";

const DEFAULT_MAX_IMAGE_BYTES = 10 * 1024 * 1024;
const DEFAULT_MAX_SUBMISSION_BYTES = 50 * 1024 * 1024;
const MAX_SUBMISSION_IMAGES = 20;
const DEFAULT_TTL_MS = 24 * 60 * 60 * 1_000;

export type SupportedImageMediaType = "image/gif" | "image/jpeg" | "image/png" | "image/webp";
export type RunAttachmentErrorCode =
  | "attachment_missing"
  | "attachment_too_large"
  | "invalid_image"
  | "ownership_mismatch";

export interface RunAttachmentMetadata {
  agentRunId: string;
  attachmentId: string;
  contentHash: string;
  conversationId: string;
  createdAt: string;
  fileName: string;
  mediaType: SupportedImageMediaType;
  size: number;
}

export interface RunAttachmentMetadataStore {
  createAttachment(metadata: RunAttachmentMetadata): Promise<void>;
  deleteAttachment(attachmentId: string): Promise<void>;
  getAttachment(attachmentId: string): Promise<RunAttachmentMetadata | undefined>;
  listAttachmentsByConversation(conversationId: string): Promise<RunAttachmentMetadata[]>;
  listAttachmentsByRun(agentRunId: string): Promise<RunAttachmentMetadata[]>;
  listAttachmentsCreatedBefore(isoTimestamp: string): Promise<RunAttachmentMetadata[]>;
}

export class RunAttachmentError extends Error {
  readonly code: RunAttachmentErrorCode;

  constructor(code: RunAttachmentErrorCode, message: string, options?: ErrorOptions) {
    super(message, options);
    this.name = "RunAttachmentError";
    this.code = code;
  }
}

export class MemoryRunAttachmentMetadataStore implements RunAttachmentMetadataStore {
  readonly #items = new Map<string, RunAttachmentMetadata>();

  async createAttachment(metadata: RunAttachmentMetadata): Promise<void> {
    if (this.#items.has(metadata.attachmentId)) {
      throw new Error(`Attachment '${metadata.attachmentId}' already exists.`);
    }
    this.#items.set(metadata.attachmentId, { ...metadata });
  }

  async deleteAttachment(attachmentId: string): Promise<void> {
    this.#items.delete(attachmentId);
  }

  async getAttachment(attachmentId: string): Promise<RunAttachmentMetadata | undefined> {
    const metadata = this.#items.get(attachmentId);
    return metadata ? { ...metadata } : undefined;
  }

  async listAttachmentsByConversation(conversationId: string): Promise<RunAttachmentMetadata[]> {
    return [...this.#items.values()]
      .filter((item) => item.conversationId === conversationId)
      .map((item) => ({ ...item }));
  }

  async listAttachmentsByRun(agentRunId: string): Promise<RunAttachmentMetadata[]> {
    return [...this.#items.values()]
      .filter((item) => item.agentRunId === agentRunId)
      .map((item) => ({ ...item }));
  }

  async listAttachmentsCreatedBefore(isoTimestamp: string): Promise<RunAttachmentMetadata[]> {
    return [...this.#items.values()]
      .filter((item) => item.createdAt < isoTimestamp)
      .map((item) => ({ ...item }));
  }
}

export interface StagedRunAttachment extends Omit<RunAttachmentMetadata, "agentRunId" | "conversationId" | "createdAt"> {}

export interface MaterializedRunAttachment extends StagedRunAttachment {
  bytes: Buffer;
  order: number;
}

export interface ImageSubmissionMetadata {
  imageCount: number;
  sourceFingerprint: string;
}

export function orderedImageSubmissionMetadata(
  attachments: Array<Pick<MaterializedRunAttachment, "contentHash" | "order">>,
): ImageSubmissionMetadata {
  const ordered = [...attachments].sort((left, right) => left.order - right.order);
  const hash = createHash("sha256");
  for (const attachment of ordered) {
    hash.update(`${attachment.order}\0${attachment.contentHash}\n`, "utf8");
  }
  return {
    imageCount: ordered.length,
    sourceFingerprint: `sha256:${hash.digest("hex")}`,
  };
}

function detectedMediaType(bytes: Uint8Array): SupportedImageMediaType | undefined {
  if (
    bytes.length >= 8 &&
    bytes[0] === 0x89 && bytes[1] === 0x50 && bytes[2] === 0x4e && bytes[3] === 0x47 &&
    bytes[4] === 0x0d && bytes[5] === 0x0a && bytes[6] === 0x1a && bytes[7] === 0x0a
  ) return "image/png";
  if (bytes.length >= 3 && bytes[0] === 0xff && bytes[1] === 0xd8 && bytes[2] === 0xff) {
    return "image/jpeg";
  }
  if (
    bytes.length >= 12 &&
    Buffer.from(bytes.subarray(0, 4)).toString("ascii") === "RIFF" &&
    Buffer.from(bytes.subarray(8, 12)).toString("ascii") === "WEBP"
  ) return "image/webp";
  const header = bytes.length >= 6 ? Buffer.from(bytes.subarray(0, 6)).toString("ascii") : "";
  if (header === "GIF87a" || header === "GIF89a") return "image/gif";
  return undefined;
}

function skipGifSubBlocks(bytes: Uint8Array, start: number): number {
  let offset = start;
  while (offset < bytes.length) {
    const size = bytes[offset];
    offset += 1;
    if (size === 0) return offset;
    offset += size;
  }
  return bytes.length;
}

function isAnimatedGif(bytes: Uint8Array): boolean {
  if (bytes.length < 13) return false;
  const logicalScreenPacked = bytes[10];
  let offset = 13 + ((logicalScreenPacked & 0x80) !== 0
    ? 3 * (1 << ((logicalScreenPacked & 0x07) + 1))
    : 0);
  let images = 0;
  while (offset < bytes.length) {
    const marker = bytes[offset];
    if (marker === 0x3b) return false;
    if (marker === 0x21) {
      if (offset + 2 >= bytes.length) return false;
      offset = skipGifSubBlocks(bytes, offset + 2);
      continue;
    }
    if (marker !== 0x2c || offset + 10 > bytes.length) return false;
    images += 1;
    if (images > 1) return true;
    const imagePacked = bytes[offset + 9];
    offset += 10;
    if ((imagePacked & 0x80) !== 0) {
      offset += 3 * (1 << ((imagePacked & 0x07) + 1));
    }
    if (offset >= bytes.length) return false;
    offset = skipGifSubBlocks(bytes, offset + 1);
  }
  return false;
}

function contentHash(bytes: Uint8Array): string {
  return `sha256:${createHash("sha256").update(bytes).digest("hex")}`;
}

export class RunAttachmentModule {
  readonly #directory: string;
  readonly #maxImageBytes: number;
  readonly #maxSubmissionBytes: number;
  readonly #metadata: RunAttachmentMetadataStore;
  readonly #now: () => Date;
  readonly #ttlMs: number;

  constructor(options: {
    directory: string;
    maxImageBytes?: number;
    maxSubmissionBytes?: number;
    metadata: RunAttachmentMetadataStore;
    now?: () => Date;
    ttlMs?: number;
  }) {
    this.#directory = path.resolve(options.directory);
    this.#maxImageBytes = options.maxImageBytes ?? DEFAULT_MAX_IMAGE_BYTES;
    this.#maxSubmissionBytes = options.maxSubmissionBytes ?? DEFAULT_MAX_SUBMISSION_BYTES;
    this.#metadata = options.metadata;
    this.#now = options.now ?? (() => new Date());
    this.#ttlMs = options.ttlMs ?? DEFAULT_TTL_MS;
  }

  async stage(input: {
    agentRunId: string;
    bytes: Uint8Array;
    claimedMediaType?: string;
    conversationId: string;
    fileName: string;
  }): Promise<StagedRunAttachment> {
    if (input.bytes.byteLength > this.#maxImageBytes) {
      throw new RunAttachmentError(
        "attachment_too_large",
        `The image exceeds the ${this.#maxImageBytes} byte per-image limit.`,
      );
    }
    const mediaType = detectedMediaType(input.bytes);
    if (!mediaType || (input.claimedMediaType && input.claimedMediaType !== mediaType)) {
      throw new RunAttachmentError(
        "invalid_image",
        "The attachment signature does not match a supported PNG, JPEG, WEBP, or GIF image.",
      );
    }
    if (mediaType === "image/gif" && isAnimatedGif(input.bytes)) {
      throw new RunAttachmentError(
        "invalid_image",
        "Animated GIF images are not supported. Attach a static GIF, PNG, JPEG, or WEBP image.",
      );
    }
    const fileName = path.basename(input.fileName).slice(0, 255);
    if (!fileName) {
      throw new RunAttachmentError("invalid_image", "The image file name is missing.");
    }
    await mkdir(this.#directory, { recursive: true });
    const attachmentId = randomUUID();
    const temporaryPath = this.#pathFor(`${attachmentId}.tmp`);
    const finalPath = this.#pathFor(attachmentId);
    await writeFile(temporaryPath, input.bytes, { flag: "wx", mode: 0o600 });
    try {
      await rename(temporaryPath, finalPath);
    } catch (error) {
      await unlink(temporaryPath).catch(() => undefined);
      throw error;
    }
    const metadata: RunAttachmentMetadata = {
      agentRunId: input.agentRunId,
      attachmentId,
      contentHash: contentHash(input.bytes),
      conversationId: input.conversationId,
      createdAt: this.#now().toISOString(),
      fileName,
      mediaType,
      size: input.bytes.byteLength,
    };
    try {
      await this.#metadata.createAttachment(metadata);
    } catch (error) {
      await unlink(finalPath).catch(() => undefined);
      throw error;
    }
    const { agentRunId: _run, conversationId: _conversation, createdAt: _created, ...staged } = metadata;
    return staged;
  }

  async materialize(input: {
    agentRunId: string;
    attachmentId: string;
    conversationId: string;
    order: number;
  }): Promise<MaterializedRunAttachment> {
    const metadata = await this.#metadata.getAttachment(input.attachmentId);
    if (!metadata) {
      throw new RunAttachmentError("attachment_missing", "The Run Attachment is missing or expired.");
    }
    if (
      metadata.agentRunId !== input.agentRunId ||
      metadata.conversationId !== input.conversationId
    ) {
      throw new RunAttachmentError(
        "ownership_mismatch",
        "The Run Attachment does not belong to this Conversation and Agent Run.",
      );
    }
    const bytes = await readFile(this.#pathFor(metadata.attachmentId)).catch((error: unknown) => {
      throw new RunAttachmentError(
        "attachment_missing",
        "The Run Attachment bytes are missing or expired.",
        { cause: error },
      );
    });
    if (
      bytes.byteLength !== metadata.size ||
      detectedMediaType(bytes) !== metadata.mediaType ||
      contentHash(bytes) !== metadata.contentHash
    ) {
      throw new RunAttachmentError("invalid_image", "The staged Run Attachment failed validation.");
    }
    const { agentRunId: _run, conversationId: _conversation, createdAt: _created, ...staged } = metadata;
    return { ...staged, bytes, order: input.order };
  }

  async materializeSubmission(input: {
    agentRunId: string;
    attachments: Array<{ attachmentId: string; order: number }>;
    conversationId: string;
  }): Promise<MaterializedRunAttachment[]> {
    if (input.attachments.length === 0 || input.attachments.length > MAX_SUBMISSION_IMAGES) {
      throw new RunAttachmentError(
        "attachment_too_large",
        `An Interview Submission accepts between 1 and ${MAX_SUBMISSION_IMAGES} images.`,
      );
    }
    const attachmentIds = new Set<string>();
    for (const [index, attachment] of input.attachments.entries()) {
      if (attachment.order !== index) {
        throw new RunAttachmentError(
          "invalid_image",
          "Run Attachments must use one contiguous submitted image order.",
        );
      }
      if (attachmentIds.has(attachment.attachmentId)) {
        throw new RunAttachmentError(
          "ownership_mismatch",
          "A Run Attachment cannot be reused within one Interview Submission.",
        );
      }
      attachmentIds.add(attachment.attachmentId);
    }
    let declaredBytes = 0;
    for (const attachment of input.attachments) {
      const metadata = await this.#metadata.getAttachment(attachment.attachmentId);
      if (!metadata) {
        throw new RunAttachmentError("attachment_missing", "The Run Attachment is missing or expired.");
      }
      if (metadata.agentRunId !== input.agentRunId || metadata.conversationId !== input.conversationId) {
        throw new RunAttachmentError(
          "ownership_mismatch",
          "The Run Attachment does not belong to this Conversation and Agent Run.",
        );
      }
      declaredBytes += metadata.size;
    }
    if (declaredBytes > this.#maxSubmissionBytes) {
      throw new RunAttachmentError(
        "attachment_too_large",
        "The ordered image submission exceeds the 50 MiB total limit.",
      );
    }
    return Promise.all(input.attachments.map((attachment) =>
      this.materialize({
        agentRunId: input.agentRunId,
        attachmentId: attachment.attachmentId,
        conversationId: input.conversationId,
        order: attachment.order,
      })
    ));
  }

  async retainInterruptedRun(agentRunId: string): Promise<void> {
    await this.#metadata.listAttachmentsByRun(agentRunId);
  }

  async deleteRun(agentRunId: string): Promise<void> {
    await this.#delete(await this.#metadata.listAttachmentsByRun(agentRunId));
  }

  async discard(input: {
    agentRunId: string;
    attachmentId: string;
    conversationId: string;
  }): Promise<void> {
    const metadata = await this.#metadata.getAttachment(input.attachmentId);
    if (!metadata) return;
    if (
      metadata.agentRunId !== input.agentRunId ||
      metadata.conversationId !== input.conversationId
    ) {
      throw new RunAttachmentError(
        "ownership_mismatch",
        "The Run Attachment does not belong to this Conversation and Agent Run.",
      );
    }
    await this.#delete([metadata]);
  }

  async deleteConversation(conversationId: string): Promise<void> {
    await this.#delete(await this.#metadata.listAttachmentsByConversation(conversationId));
  }

  async sweepExpired(): Promise<string[]> {
    const cutoff = new Date(this.#now().getTime() - this.#ttlMs).toISOString();
    const expired = await this.#metadata.listAttachmentsCreatedBefore(cutoff);
    await this.#delete(expired);
    const removed = expired.map(({ attachmentId }) => attachmentId);
    const entries = await readdir(this.#directory, { withFileTypes: true }).catch(
      (error: NodeJS.ErrnoException) => error.code === "ENOENT" ? [] : Promise.reject(error),
    );
    for (const entry of entries) {
      if (!entry.isFile()) continue;
      const filePath = this.#pathFor(entry.name);
      const information = await stat(filePath).catch(() => undefined);
      if (!information || information.mtimeMs >= new Date(cutoff).getTime()) continue;
      const attachmentId = entry.name.endsWith(".tmp") ? undefined : entry.name;
      if (attachmentId && await this.#metadata.getAttachment(attachmentId)) continue;
      await unlink(filePath).catch((error: NodeJS.ErrnoException) => {
        if (error.code !== "ENOENT") throw error;
      });
      removed.push(entry.name);
    }
    return removed;
  }

  #pathFor(attachmentId: string): string {
    return path.join(this.#directory, attachmentId);
  }

  async #delete(items: RunAttachmentMetadata[]): Promise<void> {
    for (const item of items) {
      try {
        await unlink(this.#pathFor(item.attachmentId));
      } catch (error) {
        if ((error as NodeJS.ErrnoException).code !== "ENOENT") throw error;
      }
      await this.#metadata.deleteAttachment(item.attachmentId);
    }
  }
}
