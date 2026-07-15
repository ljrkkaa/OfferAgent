import assert from "node:assert/strict";
import { createHash } from "node:crypto";
import { mkdir, mkdtemp, readdir, rm, utimes, writeFile } from "node:fs/promises";
import os from "node:os";
import path from "node:path";
import test from "node:test";
import { fileURLToPath, pathToFileURL } from "node:url";

const repositoryRoot = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..");
const modulePath = path.join(repositoryRoot, "packages", "runtime", "dist", "run-attachments.js");

const PNG = Buffer.concat([
  Buffer.from([0x89, 0x50, 0x4e, 0x47, 0x0d, 0x0a, 0x1a, 0x0a]),
  Buffer.from("offeragent-image-fixture"),
]);
const ANIMATED_GIF = Buffer.concat([
  Buffer.from("GIF89a", "ascii"),
  Buffer.from([1, 0, 1, 0, 0, 0, 0]),
  Buffer.from([0x2c, 0, 0, 0, 0, 1, 0, 1, 0, 0, 2, 2, 0x4c, 1, 0]),
  Buffer.from([0x2c, 0, 0, 0, 0, 1, 0, 1, 0, 0, 2, 2, 0x4c, 1, 0, 0x3b]),
]);

test("Run Attachments validate, bind, retain, and clean temporary image bytes", async (t) => {
  const {
    MemoryRunAttachmentMetadataStore,
    RunAttachmentError,
    RunAttachmentModule,
  } = await import(pathToFileURL(modulePath));
  const root = await mkdtemp(path.join(os.tmpdir(), "offeragent-attachments-"));
  const metadata = new MemoryRunAttachmentMetadataStore();
  let now = new Date("2026-07-15T00:00:00.000Z");
  const attachments = new RunAttachmentModule({
    directory: root,
    metadata,
    now: () => now,
    ttlMs: 60_000,
  });
  t.after(() => rm(root, { recursive: true, force: true }));

  const staged = await attachments.stage({
    agentRunId: "run-a",
    bytes: PNG,
    claimedMediaType: "image/png",
    conversationId: "conversation-a",
    fileName: "interview.png",
  });
  assert.match(staged.attachmentId, /^[a-f0-9-]{36}$/);
  assert.deepEqual(
    {
      contentHash: staged.contentHash,
      fileName: staged.fileName,
      mediaType: staged.mediaType,
      size: staged.size,
    },
    {
      contentHash: `sha256:${createHash("sha256").update(PNG).digest("hex")}`,
      fileName: "interview.png",
      mediaType: "image/png",
      size: PNG.length,
    },
  );
  assert.equal("bytes" in staged, false);

  const materialized = await attachments.materialize({
    agentRunId: "run-a",
    attachmentId: staged.attachmentId,
    conversationId: "conversation-a",
    order: 0,
  });
  assert.deepEqual(materialized.bytes, PNG);
  assert.equal(materialized.order, 0);

  await assert.rejects(
    attachments.materialize({
      agentRunId: "run-b",
      attachmentId: staged.attachmentId,
      conversationId: "conversation-a",
      order: 0,
    }),
    (error) => error instanceof RunAttachmentError && error.code === "ownership_mismatch",
  );
  await assert.rejects(
    attachments.stage({
      agentRunId: "run-b",
      bytes: PNG,
      claimedMediaType: "image/jpeg",
      conversationId: "conversation-a",
      fileName: "wrong.jpg",
    }),
    (error) => error instanceof RunAttachmentError && error.code === "invalid_image",
  );
  await assert.rejects(
    attachments.stage({
      agentRunId: "run-b",
      bytes: Buffer.alloc(10 * 1024 * 1024 + 1, 0),
      claimedMediaType: "image/png",
      conversationId: "conversation-a",
      fileName: "large.png",
    }),
    (error) => error instanceof RunAttachmentError && error.code === "attachment_too_large",
  );
  await assert.rejects(
    attachments.stage({
      agentRunId: "run-gif",
      bytes: ANIMATED_GIF,
      claimedMediaType: "image/gif",
      conversationId: "conversation-a",
      fileName: "animated.gif",
    }),
    (error) => error instanceof RunAttachmentError && /animated GIF/i.test(error.message),
  );

  const integrity = await attachments.stage({
    agentRunId: "run-integrity",
    bytes: PNG,
    claimedMediaType: "image/png",
    conversationId: "conversation-a",
    fileName: "integrity.png",
  });
  const tampered = Buffer.from(PNG);
  tampered[tampered.length - 1] ^= 0xff;
  await writeFile(path.join(root, integrity.attachmentId), tampered);
  await assert.rejects(
    attachments.materialize({
      agentRunId: "run-integrity",
      attachmentId: integrity.attachmentId,
      conversationId: "conversation-a",
      order: 0,
    }),
    (error) => error instanceof RunAttachmentError && error.code === "invalid_image",
  );
  await attachments.deleteRun("run-integrity");

  await attachments.retainInterruptedRun("run-a");
  assert.equal((await readdir(root)).length, 1);
  await attachments.deleteRun("run-a");
  assert.deepEqual(await readdir(root), []);

  const expiring = await attachments.stage({
    agentRunId: "run-expired",
    bytes: PNG,
    claimedMediaType: "image/png",
    conversationId: "conversation-expired",
    fileName: "expired.png",
  });
  now = new Date("2026-07-15T00:02:00.000Z");
  assert.deepEqual(await attachments.sweepExpired(), [expiring.attachmentId]);
  assert.deepEqual(await readdir(root), []);
  const orphan = path.join(root, "crash-orphan.tmp");
  await writeFile(orphan, PNG);
  await utimes(orphan, new Date("2026-07-14T00:00:00.000Z"), new Date("2026-07-14T00:00:00.000Z"));
  assert.deepEqual(await attachments.sweepExpired(), ["crash-orphan.tmp"]);
  assert.deepEqual(await readdir(root), []);

  await attachments.stage({
    agentRunId: "run-c",
    bytes: PNG,
    claimedMediaType: "image/png",
    conversationId: "conversation-c",
    fileName: "conversation.png",
  });
  await attachments.deleteConversation("conversation-c");
  assert.deepEqual(await readdir(root), []);
});

test("one ordered image submission enforces count, order, and total-byte limits", async (t) => {
  const {
    MemoryRunAttachmentMetadataStore,
    orderedImageSubmissionMetadata,
    RunAttachmentError,
    RunAttachmentModule,
  } = await import(pathToFileURL(modulePath));
  const root = await mkdtemp(path.join(os.tmpdir(), "offeragent-run-attachment-batch-"));
  t.after(() => rm(root, { recursive: true, force: true }));
  const metadata = new MemoryRunAttachmentMetadataStore();
  const attachments = new RunAttachmentModule({
    directory: root,
    maxSubmissionBytes: PNG.length * 2,
    metadata,
  });
  const staged = [];
  for (let index = 0; index < 3; index += 1) {
    staged.push(await attachments.stage({
      agentRunId: "ordered-run",
      bytes: PNG,
      claimedMediaType: "image/png",
      conversationId: "ordered-conversation",
      fileName: `${index}.png`,
    }));
  }
  const references = staged.map(({ attachmentId }, order) => ({ attachmentId, order }));
  const ordered = await attachments.materializeSubmission({
    agentRunId: "ordered-run",
    attachments: references.slice(0, 2),
    conversationId: "ordered-conversation",
  });
  assert.deepEqual(ordered.map(({ fileName, order }) => ({ fileName, order })), [
    { fileName: "0.png", order: 0 },
    { fileName: "1.png", order: 1 },
  ]);
  const submission = orderedImageSubmissionMetadata(ordered);
  assert.deepEqual(submission, {
    imageCount: 2,
    sourceFingerprint: `sha256:${createHash("sha256")
      .update(`0\0${ordered[0].contentHash}\n1\0${ordered[1].contentHash}\n`, "utf8")
      .digest("hex")}`,
  });
  assert.notEqual(
    orderedImageSubmissionMetadata([
      { contentHash: `sha256:${"a".repeat(64)}`, order: 0 },
      { contentHash: `sha256:${"b".repeat(64)}`, order: 1 },
    ]).sourceFingerprint,
    orderedImageSubmissionMetadata([
      { contentHash: `sha256:${"b".repeat(64)}`, order: 0 },
      { contentHash: `sha256:${"a".repeat(64)}`, order: 1 },
    ]).sourceFingerprint,
  );
  await assert.rejects(
    attachments.materializeSubmission({
      agentRunId: "ordered-run",
      attachments: references,
      conversationId: "ordered-conversation",
    }),
    (error) => error instanceof RunAttachmentError && /50 MiB|total/i.test(error.message),
  );
  await assert.rejects(
    attachments.materializeSubmission({
      agentRunId: "ordered-run",
      attachments: [references[1], references[0]],
      conversationId: "ordered-conversation",
    }),
    (error) => error instanceof RunAttachmentError && /order/i.test(error.message),
  );
  await assert.rejects(
    attachments.materializeSubmission({
      agentRunId: "ordered-run",
      attachments: Array.from({ length: 21 }, (_, order) => ({
        attachmentId: staged[0].attachmentId,
        order,
      })),
      conversationId: "ordered-conversation",
    }),
    (error) => error instanceof RunAttachmentError && /20 images/i.test(error.message),
  );
});

test("Conversation-owned attachments survive terminal cleanup and support transactional deletion", async (t) => {
  const {
    MemoryRunAttachmentMetadataStore,
    RunAttachmentModule,
  } = await import(pathToFileURL(modulePath));
  const root = await mkdtemp(path.join(os.tmpdir(), "offeragent-conversation-attachments-"));
  const metadata = new MemoryRunAttachmentMetadataStore();
  let now = new Date("2026-07-15T00:00:00.000Z");
  const attachments = new RunAttachmentModule({
    directory: root,
    metadata,
    now: () => now,
    ttlMs: 60_000,
  });
  t.after(() => rm(root, { recursive: true, force: true }));

  const staged = await attachments.stage({
    agentRunId: "owned-run",
    bytes: PNG,
    claimedMediaType: "image/png",
    conversationId: "owned-conversation",
    fileName: "owned.png",
  });
  await metadata.claimAttachments({
    agentRunId: "owned-run",
    attachments: [{ attachmentId: staged.attachmentId, order: 0 }],
    conversationId: "owned-conversation",
    messageId: "owned-message",
    ownedAt: now.toISOString(),
  });

  await attachments.deleteRun("owned-run");
  now = new Date("2026-07-15T00:02:00.000Z");
  assert.deepEqual(await attachments.sweepExpired(), []);
  assert.deepEqual(await readdir(root), [staged.attachmentId]);
  const historical = await attachments.materializeOwned({
    conversationId: "owned-conversation",
    messageId: "owned-message",
    order: 0,
  });
  assert.deepEqual(historical.bytes, PNG);
  assert.equal(historical.fileName, "owned.png");
  await assert.rejects(
    attachments.discard({
      agentRunId: "owned-run",
      attachmentId: staged.attachmentId,
      conversationId: "owned-conversation",
    }),
    (error) => error?.code === "ownership_mismatch",
  );
  const other = await attachments.stage({
    agentRunId: "other-run",
    bytes: PNG,
    claimedMediaType: "image/png",
    conversationId: "other-conversation",
    fileName: "other.png",
  });
  await metadata.claimAttachments({
    agentRunId: "other-run",
    attachments: [{ attachmentId: other.attachmentId, order: 0 }],
    conversationId: "other-conversation",
    messageId: "other-message",
    ownedAt: now.toISOString(),
  });

  const deletion = await attachments.prepareConversationDeletion("owned-conversation");
  assert.deepEqual(
    new Set(await readdir(root)),
    new Set([`${staged.attachmentId}.deleting`, other.attachmentId]),
  );
  const restartedAttachments = new RunAttachmentModule({ directory: root, metadata });
  await restartedAttachments.recoverPendingDeletions();
  assert.deepEqual(
    new Set(await readdir(root)),
    new Set([staged.attachmentId, other.attachmentId]),
  );

  const committedDeletion = await restartedAttachments.prepareConversationDeletion("owned-conversation");
  await committedDeletion.commit();
  assert.deepEqual(await readdir(root), [other.attachmentId]);
  assert.equal(await metadata.getAttachment(staged.attachmentId), undefined);
  assert.equal((await metadata.getAttachment(other.attachmentId)).conversationId, "other-conversation");
  await restartedAttachments.deleteConversation("other-conversation");
  assert.deepEqual(await readdir(root), []);
});

test("attachment staging is quota-bounded, deletion-safe, and namespaced by State", async (t) => {
  const {
    DEFAULT_MAX_CONVERSATION_BYTES,
    DEFAULT_MAX_TOTAL_BYTES,
    attachmentDirectoryForState,
    MemoryRunAttachmentMetadataStore,
    RunAttachmentModule,
  } = await import(pathToFileURL(modulePath));
  assert.equal(DEFAULT_MAX_CONVERSATION_BYTES, 250 * 1024 * 1024);
  assert.equal(DEFAULT_MAX_TOTAL_BYTES, 2 * 1024 * 1024 * 1024);
  assert.notEqual(
    attachmentDirectoryForState("C:/vault-a/state.db", "C:/local"),
    attachmentDirectoryForState("C:/vault-b/state.db", "C:/local"),
  );
  assert.equal(
    attachmentDirectoryForState("C:/vault-a/state.db", "C:/local"),
    attachmentDirectoryForState("C:/vault-a/state.db", "C:/local"),
  );

  const root = await mkdtemp(path.join(os.tmpdir(), "offeragent-attachment-capacity-"));
  const metadata = new MemoryRunAttachmentMetadataStore();
  const attachments = new RunAttachmentModule({
    directory: root,
    maxConversationBytes: PNG.length,
    maxTotalBytes: PNG.length * 2,
    metadata,
  });
  t.after(() => rm(root, { recursive: true, force: true }));
  await attachments.stage({
    agentRunId: "quota-a-1",
    bytes: PNG,
    claimedMediaType: "image/png",
    conversationId: "quota-a",
    fileName: "a.png",
  });
  await assert.rejects(
    attachments.stage({
      agentRunId: "quota-a-2",
      bytes: PNG,
      claimedMediaType: "image/png",
      conversationId: "quota-a",
      fileName: "a2.png",
    }),
    (error) =>
      error?.code === "capacity_exceeded" &&
      /start a new Conversation/i.test(error.message) &&
      /delete this Conversation/i.test(error.message),
  );
  await attachments.stage({
    agentRunId: "quota-b-1",
    bytes: PNG,
    claimedMediaType: "image/png",
    conversationId: "quota-b",
    fileName: "b.png",
  });
  await assert.rejects(
    attachments.stage({
      agentRunId: "quota-c-1",
      bytes: PNG,
      claimedMediaType: "image/png",
      conversationId: "quota-c",
      fileName: "c.png",
    }),
    (error) => error?.code === "capacity_exceeded" && /delete|clean/i.test(error.message),
  );
  assert.equal((await readdir(root)).length, 2);
  await attachments.deleteRun("quota-a-1");
  await attachments.deleteRun("quota-b-1");

  const deletion = await attachments.prepareConversationDeletion("deleting-conversation");
  await assert.rejects(
    attachments.stage({
      agentRunId: "late-stage",
      bytes: PNG,
      claimedMediaType: "image/png",
      conversationId: "deleting-conversation",
      fileName: "late.png",
    }),
    (error) => error?.code === "ownership_mismatch" && /delet/i.test(error.message),
  );
  await deletion.rollback();
  const afterRollback = await attachments.stage({
    agentRunId: "after-rollback",
    bytes: PNG,
    claimedMediaType: "image/png",
    conversationId: "deleting-conversation",
    fileName: "after.png",
  });
  assert.ok(afterRollback.attachmentId);
});

test("Conversation deletion reports attachment cleanup failures", async (t) => {
  const {
    MemoryRunAttachmentMetadataStore,
    RunAttachmentModule,
  } = await import(pathToFileURL(modulePath));
  const root = await mkdtemp(path.join(os.tmpdir(), "offeragent-attachment-delete-failure-"));
  const backingMetadata = new MemoryRunAttachmentMetadataStore();
  const metadata = new Proxy(backingMetadata, {
    get(target, property) {
      if (property === "deleteAttachment") {
        return async () => { throw new Error("injected metadata cleanup failure"); };
      }
      const value = Reflect.get(target, property);
      return typeof value === "function" ? value.bind(target) : value;
    },
  });
  const attachments = new RunAttachmentModule({ directory: root, metadata });
  t.after(() => rm(root, { recursive: true, force: true }));
  await attachments.stage({
    agentRunId: "cleanup-run",
    bytes: PNG,
    claimedMediaType: "image/png",
    conversationId: "cleanup-conversation",
    fileName: "cleanup.png",
  });
  const deletion = await attachments.prepareConversationDeletion("cleanup-conversation");
  await assert.rejects(deletion.commit(), /injected metadata cleanup failure/);
});

test("a failed post-delete cleanup remains recoverable without restoring a deleted Conversation", async (t) => {
  const {
    MemoryRunAttachmentMetadataStore,
    RunAttachmentModule,
  } = await import(pathToFileURL(modulePath));
  const root = await mkdtemp(path.join(os.tmpdir(), "offeragent-attachment-delete-recovery-"));
  const backingMetadata = new MemoryRunAttachmentMetadataStore();
  let failCleanup = true;
  const metadata = new Proxy(backingMetadata, {
    get(target, property) {
      if (property === "conversationExists") return async () => false;
      if (property === "deleteAttachment") {
        return async (...arguments_) => {
          if (failCleanup) throw new Error("transient metadata cleanup failure");
          return target.deleteAttachment(...arguments_);
        };
      }
      const value = Reflect.get(target, property);
      return typeof value === "function" ? value.bind(target) : value;
    },
  });
  const attachments = new RunAttachmentModule({ directory: root, metadata });
  t.after(() => rm(root, { recursive: true, force: true }));
  const staged = await attachments.stage({
    agentRunId: "recovery-run",
    bytes: PNG,
    claimedMediaType: "image/png",
    conversationId: "deleted-conversation",
    fileName: "recovery.png",
  });
  const deletion = await attachments.prepareConversationDeletion("deleted-conversation");
  await assert.rejects(deletion.commit(), /transient metadata cleanup failure/);
  assert.deepEqual(await readdir(root), [`${staged.attachmentId}.deleting`]);

  failCleanup = false;
  await attachments.recoverPendingDeletions();
  assert.deepEqual(await readdir(root), []);
  assert.equal(await backingMetadata.getAttachment(staged.attachmentId), undefined);
});

test("pending deletion recovery reports a tombstone unlink failure", async (t) => {
  const {
    MemoryRunAttachmentMetadataStore,
    RunAttachmentModule,
  } = await import(pathToFileURL(modulePath));
  const root = await mkdtemp(path.join(os.tmpdir(), "offeragent-attachment-tombstone-failure-"));
  const metadata = new MemoryRunAttachmentMetadataStore();
  const attachments = new RunAttachmentModule({
    directory: root,
    metadata,
    removeFile: async (filePath) => {
      if (filePath.endsWith(".deleting")) throw new Error("injected tombstone unlink failure");
      await rm(filePath);
    },
  });
  t.after(() => rm(root, { recursive: true, force: true }));
  const staged = await attachments.stage({
    agentRunId: "tombstone-run",
    bytes: PNG,
    claimedMediaType: "image/png",
    conversationId: "deleted-conversation",
    fileName: "tombstone.png",
  });
  await attachments.prepareConversationDeletion("deleted-conversation");
  await metadata.deleteAttachment(staged.attachmentId);

  await assert.rejects(
    attachments.recoverPendingDeletions(),
    /injected tombstone unlink failure/,
  );
  assert.deepEqual(await readdir(root), [`${staged.attachmentId}.deleting`]);
});

test("legacy default attachment bytes migrate only when owned by the current State", async (t) => {
  const {
    MemoryRunAttachmentMetadataStore,
    RunAttachmentModule,
    migrateLegacyAttachmentDirectory,
  } = await import(pathToFileURL(modulePath));
  const root = await mkdtemp(path.join(os.tmpdir(), "offeragent-legacy-attachments-"));
  const legacyDirectory = path.join(root, "OfferAgent", "attachments");
  const directory = path.join(legacyDirectory, "state-namespace");
  const metadata = new MemoryRunAttachmentMetadataStore();
  t.after(() => rm(root, { recursive: true, force: true }));
  await mkdir(legacyDirectory, { recursive: true });

  const ownedId = "11111111-1111-4111-8111-111111111111";
  const deletingId = "22222222-2222-4222-8222-222222222222";
  const foreignId = "33333333-3333-4333-8333-333333333333";
  for (const [attachmentId, conversationId] of [[ownedId, "owned"], [deletingId, "deleting"]]) {
    await metadata.createAttachment({
      agentRunId: `${conversationId}-run`,
      attachmentId,
      contentHash: `sha256:${createHash("sha256").update(PNG).digest("hex")}`,
      conversationId,
      createdAt: "2026-07-15T00:00:00.000Z",
      fileName: `${conversationId}.png`,
      mediaType: "image/png",
      size: PNG.byteLength,
    });
  }
  await writeFile(path.join(legacyDirectory, ownedId), PNG);
  await writeFile(path.join(legacyDirectory, `${deletingId}.deleting`), PNG);
  await writeFile(path.join(legacyDirectory, foreignId), PNG);

  assert.deepEqual(
    new Set(await migrateLegacyAttachmentDirectory({ directory, legacyDirectory, metadata })),
    new Set([ownedId, `${deletingId}.deleting`]),
  );
  assert.deepEqual(await readdir(legacyDirectory), [foreignId, "state-namespace"]);
  assert.deepEqual(new Set(await readdir(directory)), new Set([ownedId, `${deletingId}.deleting`]));

  const attachments = new RunAttachmentModule({ directory, metadata });
  await attachments.recoverPendingDeletions();
  assert.deepEqual(new Set(await readdir(directory)), new Set([ownedId, deletingId]));
  assert.deepEqual(
    (await attachments.materialize({
      agentRunId: "owned-run",
      attachmentId: ownedId,
      conversationId: "owned",
      order: 0,
    })).bytes,
    PNG,
  );
});
