import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import path from "node:path";
import test from "node:test";
import { fileURLToPath } from "node:url";

const repositoryRoot = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..");
const recordPath = path.join(
  repositoryRoot,
  "docs",
  "acceptance",
  "2026-07-15-interview-submission-target-vault.md",
);

test("Ticket #33 records the complete real target-Vault Interview ingestion proof", async () => {
  const record = await readFile(recordPath, "utf8");

  assert.match(record, /Result: PASS/);
  assert.match(record, /## Text Interview Submission/);
  assert.match(record, /## URL Interview Submission/);
  assert.match(record, /## Ordered-image Interview Submission/);
  assert.match(record, /Source Fingerprint/i);
  assert.match(record, /Interview Question synchronization/i);
  assert.match(record, /answer-state: needs-research/i);
  assert.match(record, /refs\/offeragent\/checkpoints\//);
  assert.match(record, /Repeated fixture: PASS/);
  assert.match(record, /Guarded undo: PASS/);
  assert.match(record, /Attachment cleanup: PASS/);
  assert.match(record, /Temporary-note cleanup: PASS/);
  assert.match(record, /Unrelated Vault invariants: PASS/);
  assert.match(record, /Installed package hashes: PASS/);
  assert.match(record, /Deterministic test gate: PASS/);

  assert.doesNotMatch(record, /access[_ -]?token\s*[:=]\s*\S+/i);
  assert.doesNotMatch(record, /authorization\s*[:=]\s*bearer\s+\S+/i);
  assert.doesNotMatch(record, /--token\s+\S+/i);
});
