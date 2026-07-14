import assert from "node:assert/strict";
import { createHash } from "node:crypto";
import { mkdtemp, readdir, rm, utimes, writeFile } from "node:fs/promises";
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
