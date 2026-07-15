import { createHash, randomUUID } from "node:crypto";
import { mkdir, readFile, readdir, rename, stat, unlink, writeFile } from "node:fs/promises";
import path from "node:path";

const DEFAULT_MAX_IMAGE_BYTES = 10 * 1024 * 1024;
const DEFAULT_MAX_SUBMISSION_BYTES = 50 * 1024 * 1024;
export const DEFAULT_MAX_CONVERSATION_BYTES = 250 * 1024 * 1024;
export const DEFAULT_MAX_TOTAL_BYTES = 2 * 1024 * 1024 * 1024;
const MAX_SUBMISSION_IMAGES = 20;
const DEFAULT_TTL_MS = 24 * 60 * 60 * 1_000;

export type SupportedImageMediaType = "image/gif" | "image/jpeg" | "image/png" | "image/webp";
export type RunAttachmentErrorCode =
  | "attachment_missing"
  | "attachment_too_large"
  | "capacity_exceeded"
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
  messageId?: string;
  messageOrder?: number;
  ownedAt?: string;
  size: number;
}

export interface RunAttachmentMetadataStore {
  conversationExists?(conversationId: string): Promise<boolean>;
  claimAttachments(input: {
    agentRunId: string;
    attachments: Array<{ attachmentId: string; order: number }>;
    conversationId: string;
    messageId: string;
    ownedAt: string;
  }): Promise<void>;
  createAttachment(metadata: RunAttachmentMetadata): Promise<void>;
  deleteAttachment(attachmentId: string): Promise<void>;
  getAttachment(attachmentId: string): Promise<RunAttachmentMetadata | undefined>;
  getAttachmentUsage(conversationId: string): Promise<{
    conversationBytes: number;
    totalBytes: number;
  }>;
  getAttachmentByMessage(messageId: string, order: number): Promise<RunAttachmentMetadata | undefined>;
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

  async claimAttachments(input: {
    agentRunId: string;
    attachments: Array<{ attachmentId: string; order: number }>;
    conversationId: string;
    messageId: string;
    ownedAt: string;
  }): Promise<void> {
    const claimed = input.attachments.map(({ attachmentId, order }) => {
      const metadata = this.#items.get(attachmentId);
      if (
        !metadata || metadata.agentRunId !== input.agentRunId ||
        metadata.conversationId !== input.conversationId || metadata.messageId
      ) {
        throw new RunAttachmentError(
          "ownership_mismatch",
          "The Run Attachment cannot be promoted to this Conversation message.",
        );
      }
      return { metadata, order };
    });
    for (const { metadata, order } of claimed) {
      this.#items.set(metadata.attachmentId, {
        ...metadata,
        messageId: input.messageId,
        messageOrder: order,
        ownedAt: input.ownedAt,
      });
    }
  }

  async deleteAttachment(attachmentId: string): Promise<void> {
    this.#items.delete(attachmentId);
  }

  async getAttachment(attachmentId: string): Promise<RunAttachmentMetadata | undefined> {
    const metadata = this.#items.get(attachmentId);
    return metadata ? { ...metadata } : undefined;
  }

  async getAttachmentUsage(conversationId: string): Promise<{
    conversationBytes: number;
    totalBytes: number;
  }> {
    let conversationBytes = 0;
    let totalBytes = 0;
    for (const item of this.#items.values()) {
      totalBytes += item.size;
      if (item.conversationId === conversationId) conversationBytes += item.size;
    }
    return { conversationBytes, totalBytes };
  }

  async getAttachmentByMessage(
    messageId: string,
    order: number,
  ): Promise<RunAttachmentMetadata | undefined> {
    const metadata = [...this.#items.values()].find(
      (item) => item.messageId === messageId && item.messageOrder === order,
    );
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

export function attachmentDirectoryForState(statePath: string, dataRoot: string): string {
  const resolvedStatePath = path.resolve(statePath);
  const normalizedStatePath = process.platform === "win32"
    ? resolvedStatePath.toLowerCase()
    : resolvedStatePath;
  const namespace = createHash("sha256").update(normalizedStatePath, "utf8").digest("hex").slice(0, 24);
  return path.join(path.resolve(dataRoot), "OfferAgent", "attachments", namespace);
}

export async function migrateLegacyAttachmentDirectory(input: {
  directory: string;
  legacyDirectory: string;
  metadata: RunAttachmentMetadataStore;
}): Promise<string[]> {
  const directory = path.resolve(input.directory);
  const legacyDirectory = path.resolve(input.legacyDirectory);
  if (directory === legacyDirectory) return [];
  const entries = await readdir(legacyDirectory, { withFileTypes: true }).catch(
    (error: NodeJS.ErrnoException) => error.code === "ENOENT" ? [] : Promise.reject(error),
  );
  const moved: string[] = [];
  for (const entry of entries) {
    if (!entry.isFile() || entry.name.endsWith(".tmp")) continue;
    const deleting = entry.name.endsWith(".deleting");
    const attachmentId = deleting ? entry.name.slice(0, -".deleting".length) : entry.name;
    if (!attachmentId || !await input.metadata.getAttachment(attachmentId)) continue;
    await mkdir(directory, { recursive: true });
    const target = path.join(directory, entry.name);
    const targetExists = await stat(target).then(() => true, () => false);
    if (targetExists) continue;
    try {
      await rename(path.join(legacyDirectory, entry.name), target);
      moved.push(entry.name);
    } catch (error) {
      if ((error as NodeJS.ErrnoException).code !== "ENOENT") throw error;
    }
  }
  return moved;
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
  readonly #deletingConversations = new Set<string>();
  readonly #maxConversationBytes: number;
  readonly #maxImageBytes: number;
  readonly #maxSubmissionBytes: number;
  readonly #maxTotalBytes: number;
  readonly #metadata: RunAttachmentMetadataStore;
  #mutationTail: Promise<void> = Promise.resolve();
  readonly #now: () => Date;
  readonly #removeFile: (filePath: string) => Promise<void>;
  readonly #ttlMs: number;

  constructor(options: {
    directory: string;
    maxConversationBytes?: number;
    maxImageBytes?: number;
    maxSubmissionBytes?: number;
    maxTotalBytes?: number;
    metadata: RunAttachmentMetadataStore;
    now?: () => Date;
    removeFile?: (filePath: string) => Promise<void>;
    ttlMs?: number;
  }) {
    this.#directory = path.resolve(options.directory);
    this.#maxConversationBytes = options.maxConversationBytes ?? DEFAULT_MAX_CONVERSATION_BYTES;
    this.#maxImageBytes = options.maxImageBytes ?? DEFAULT_MAX_IMAGE_BYTES;
    this.#maxSubmissionBytes = options.maxSubmissionBytes ?? DEFAULT_MAX_SUBMISSION_BYTES;
    this.#maxTotalBytes = options.maxTotalBytes ?? DEFAULT_MAX_TOTAL_BYTES;
    this.#metadata = options.metadata;
    this.#now = options.now ?? (() => new Date());
    this.#removeFile = options.removeFile ?? unlink;
    this.#ttlMs = options.ttlMs ?? DEFAULT_TTL_MS;
  }

  async stage(input: {
    agentRunId: string;
    bytes: Uint8Array;
    claimedMediaType?: string;
    conversationId: string;
    fileName: string;
  }): Promise<StagedRunAttachment> {
    return this.#withMutation(() => this.#stage(input));
  }

  async #stage(input: {
    agentRunId: string;
    bytes: Uint8Array;
    claimedMediaType?: string;
    conversationId: string;
    fileName: string;
  }): Promise<StagedRunAttachment> {
    if (this.#deletingConversations.has(input.conversationId)) {
      throw new RunAttachmentError(
        "ownership_mismatch",
        "The Conversation is being deleted; wait or choose another Conversation before attaching images.",
      );
    }
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
    const usage = await this.#metadata.getAttachmentUsage(input.conversationId);
    if (usage.conversationBytes + input.bytes.byteLength > this.#maxConversationBytes) {
      throw new RunAttachmentError(
        "capacity_exceeded",
        "This Conversation has reached its attachment capacity. Start a new Conversation, or delete this Conversation when you no longer need its retained images.",
      );
    }
    if (usage.totalBytes + input.bytes.byteLength > this.#maxTotalBytes) {
      throw new RunAttachmentError(
        "capacity_exceeded",
        "OfferAgent has reached its total attachment capacity. Delete old Conversations to clean up space.",
      );
    }
    await mkdir(this.#directory, { recursive: true });
    const attachmentId = randomUUID();
    const temporaryPath = this.#pathFor(`${attachmentId}.tmp`);
    const finalPath = this.#pathFor(attachmentId);
    await writeFile(temporaryPath, input.bytes, { flag: "wx", mode: 0o600 });
    try {
      await rename(temporaryPath, finalPath);
    } catch (error) {
      await this.#removeFile(temporaryPath).catch(() => undefined);
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
      await this.#removeFile(finalPath).catch(() => undefined);
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
    const bytes = await this.#readValidated(metadata);
    const {
      agentRunId: _run,
      conversationId: _conversation,
      createdAt: _created,
      messageId: _message,
      messageOrder: _messageOrder,
      ownedAt: _ownedAt,
      ...staged
    } = metadata;
    return { ...staged, bytes, order: input.order };
  }

  async materializeOwned(input: {
    conversationId: string;
    messageId: string;
    order: number;
  }): Promise<MaterializedRunAttachment> {
    const metadata = await this.#metadata.getAttachmentByMessage(input.messageId, input.order);
    if (!metadata) {
      throw new RunAttachmentError("attachment_missing", "The Conversation Attachment is missing.");
    }
    if (metadata.conversationId !== input.conversationId) {
      throw new RunAttachmentError(
        "ownership_mismatch",
        "The Conversation Attachment does not belong to this Conversation.",
      );
    }
    const bytes = await this.#readValidated(metadata);
    const {
      agentRunId: _run,
      conversationId: _conversation,
      createdAt: _created,
      messageId: _message,
      messageOrder: _messageOrder,
      ownedAt: _ownedAt,
      ...staged
    } = metadata;
    return { ...staged, bytes, order: input.order };
  }

  async #readValidated(metadata: RunAttachmentMetadata): Promise<Buffer> {
    const bytes = await readFile(this.#pathFor(metadata.attachmentId)).catch((error: unknown) => {
      throw new RunAttachmentError(
        "attachment_missing",
        "The Attachment bytes are missing or expired.",
        { cause: error },
      );
    });
    if (
      bytes.byteLength !== metadata.size ||
      detectedMediaType(bytes) !== metadata.mediaType ||
      contentHash(bytes) !== metadata.contentHash
    ) {
      throw new RunAttachmentError("invalid_image", "The stored Attachment failed validation.");
    }
    return bytes;
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
    await this.#delete(
      (await this.#metadata.listAttachmentsByRun(agentRunId)).filter(({ messageId }) => !messageId),
    );
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
      metadata.conversationId !== input.conversationId ||
      metadata.messageId
    ) {
      throw new RunAttachmentError(
        "ownership_mismatch",
        "The Run Attachment does not belong to this draft or is already owned by a message.",
      );
    }
    await this.#delete([metadata]);
  }

  async deleteConversation(conversationId: string): Promise<void> {
    const deletion = await this.prepareConversationDeletion(conversationId);
    await deletion.commit();
  }

  async prepareConversationDeletion(conversationId: string): Promise<{
    commit(): Promise<void>;
    rollback(): Promise<void>;
  }> {
    return this.#withMutation(async () => {
      if (this.#deletingConversations.has(conversationId)) {
        throw new RunAttachmentError("ownership_mismatch", "The Conversation is already being deleted.");
      }
      this.#deletingConversations.add(conversationId);
      try {
        return await this.#prepareConversationDeletion(conversationId);
      } catch (error) {
        this.#deletingConversations.delete(conversationId);
        throw error;
      }
    });
  }

  async #prepareConversationDeletion(conversationId: string): Promise<{
    commit(): Promise<void>;
    rollback(): Promise<void>;
  }> {
    const items = await this.#metadata.listAttachmentsByConversation(conversationId);
    const moved: RunAttachmentMetadata[] = [];
    try {
      for (const item of items) {
        try {
          await rename(this.#pathFor(item.attachmentId), this.#pathFor(`${item.attachmentId}.deleting`));
          moved.push(item);
        } catch (error) {
          if ((error as NodeJS.ErrnoException).code !== "ENOENT") throw error;
        }
      }
    } catch (error) {
      await Promise.all(moved.map((item) =>
        rename(this.#pathFor(`${item.attachmentId}.deleting`), this.#pathFor(item.attachmentId))
          .catch(() => undefined)
      ));
      throw error;
    }
    let settled = false;
    return {
      commit: async () => {
        await this.#withMutation(async () => {
          if (settled) return;
          const cleanupErrors: unknown[] = [];
          const movedIds = new Set(moved.map(({ attachmentId }) => attachmentId));
          for (const item of items) {
            try {
              await this.#metadata.deleteAttachment(item.attachmentId);
            } catch (error) {
              cleanupErrors.push(error);
              continue;
            }
            if (!movedIds.has(item.attachmentId)) continue;
            try {
              await this.#removeFile(this.#pathFor(`${item.attachmentId}.deleting`));
            } catch (error) {
              cleanupErrors.push(error);
            }
          }
          settled = true;
          this.#deletingConversations.delete(conversationId);
          if (cleanupErrors.length > 0) {
            throw new AggregateError(
              cleanupErrors,
              `Conversation '${conversationId}' attachment cleanup failed: ${cleanupErrors
                .map((error) => error instanceof Error ? error.message : String(error))
                .join("; ")}`,
            );
          }
        });
      },
      rollback: async () => {
        await this.#withMutation(async () => {
          if (settled) return;
          settled = true;
          await Promise.all(moved.map((item) =>
            rename(this.#pathFor(`${item.attachmentId}.deleting`), this.#pathFor(item.attachmentId))
          ));
          this.#deletingConversations.delete(conversationId);
        });
      },
    };
  }

  async recoverPendingDeletions(): Promise<void> {
    const entries = await readdir(this.#directory, { withFileTypes: true }).catch(
      (error: NodeJS.ErrnoException) => error.code === "ENOENT" ? [] : Promise.reject(error),
    );
    for (const entry of entries) {
      if (!entry.isFile() || !entry.name.endsWith(".deleting")) continue;
      const attachmentId = entry.name.slice(0, -".deleting".length);
      const tombstonePath = this.#pathFor(entry.name);
      const metadata = await this.#metadata.getAttachment(attachmentId);
      if (!metadata) {
        await this.#removeFile(tombstonePath).catch((error: NodeJS.ErrnoException) => {
          if (error.code !== "ENOENT") throw error;
        });
        continue;
      }
      if (
        this.#metadata.conversationExists &&
        !await this.#metadata.conversationExists(metadata.conversationId)
      ) {
        await this.#metadata.deleteAttachment(attachmentId);
        await this.#removeFile(tombstonePath).catch((error: NodeJS.ErrnoException) => {
          if (error.code !== "ENOENT") throw error;
        });
        continue;
      }
      const finalPath = this.#pathFor(attachmentId);
      const finalExists = await stat(finalPath).then(() => true, () => false);
      if (finalExists) {
        await this.#removeFile(tombstonePath).catch((error: NodeJS.ErrnoException) => {
          if (error.code !== "ENOENT") throw error;
        });
      }
      else await rename(tombstonePath, finalPath);
    }
  }

  async sweepExpired(): Promise<string[]> {
    const cutoff = new Date(this.#now().getTime() - this.#ttlMs).toISOString();
    const expired = (await this.#metadata.listAttachmentsCreatedBefore(cutoff))
      .filter(({ messageId }) => !messageId);
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
      await this.#removeFile(filePath).catch((error: NodeJS.ErrnoException) => {
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
        await this.#removeFile(this.#pathFor(item.attachmentId));
      } catch (error) {
        if ((error as NodeJS.ErrnoException).code !== "ENOENT") throw error;
      }
      await this.#metadata.deleteAttachment(item.attachmentId);
    }
  }

  async #withMutation<T>(operation: () => Promise<T>): Promise<T> {
    const previous = this.#mutationTail;
    let release!: () => void;
    this.#mutationTail = new Promise<void>((resolve) => {
      release = resolve;
    });
    await previous;
    try {
      return await operation();
    } finally {
      release();
    }
  }
}
