# Ticket #33 Target-Vault Interview Submission Acceptance

Result: PASS

- Date: 2026-07-15 (Asia/Shanghai)
- Target Vault: `E:\obsidian项目\面试胜利！`
- Installed UI: Obsidian 1.12.7, OfferAgent Sidebar, Codex subscription provider, model `gpt-5.4`
- Repository fixed base: `6d21285075be14f4b1fdb9c0d328726a12b918f9`
- Target branch/head/index tree: `main` / `a3c75fc864560d61bfb78b4334c19aa34aa3475a` / `ca20c4c677d5464ae6a5381b62f64ac58b49c1c6`

The target `agent.md` was read before deployment or mutation. Deployment first returned a confirmation-required preview for `agent.md`; the confirmed control migration installed the package and created `refs/offeragent/checkpoints/target-vault-migration-33b94371cbf13367`. Subsequent deploys reported the control migration unchanged.

## Live defects found and closed

The installed acceptance run found three Runtime defects that deterministic fake-provider coverage had not exposed:

1. The Codex local-tool decoder omitted the protocol's `interview_catalog` tool, silently discarding a valid model tool call.
2. The generic 8 KiB tool-argument cap rejected a legitimate atomic multi-note `vault_propose_changes` payload, despite the Vault coordinator allowing bounded 128 KiB batches.
3. After a successful proposal, the Runtime treated the batch's own target hashes as external stale evidence and failed the run before its final response.

Each defect received an end-to-end red test through the Runtime WebSocket seam before the narrow fix. The final provider test covers `interview_catalog`, a greater-than-8-KiB bounded proposal, preservation of the 8-KiB limit for other tools, and successful completion after an applied proposal changes previously read evidence.

## Text Interview Submission

- Completed run: `30c1a656-987b-40c6-8609-10e7bc7dd158`
- Batch: `ticket-33-text-final`
- Checkpoint: `refs/offeragent/checkpoints/ticket-33-text-final` (`cdd559a06e99d3c689623be6aa5ebb50a9f7ed56`)
- One experience, two new questions, and both indexes were applied as one five-action batch.
- Both questions contained `answer-state: needs-research` and exactly one occurrence.
- The experience retained only the synthetic marker `OA-T33-TEXT-71F4` and concise source metadata; no date was inferred.
- Interview Question synchronization: PASS.
- Guarded undo: PASS. Both index hashes returned to the exact baseline and all three temporary notes disappeared.

An earlier diagnostic batch, `ticket-33-interview-ingest`, also applied atomically and was guarded-undone; it is retained only as evidence of the post-apply stale-evidence defect fixed above.

## URL Interview Submission

- Public fixture: `https://interviewexperiences.in/experience/uber/uber-software-engineer-backend-role-interview-experience`
- Completed run: `ed343694-4139-4e1b-ac19-538b131131ae`
- Tools included `web_read` followed by `interview_catalog` before the proposal.
- Batch: `ticket-33-url-final`
- Checkpoint: `refs/offeragent/checkpoints/ticket-33-url-final` (`74dcd5564acb5bef400c9a9efe1ae2f5aea5a52b`)
- Canonical Source Fingerprint: `sha256:f80d83395ede4797c616a550635bafe733b4d410c6fe57484f53fee71ec20984`.
- Exactly two representative questions were synchronized with `answer-state: needs-research`, `frequency: 1`, and one occurrence each.
- The model conservatively normalized "Top K trending items" without inventing a hashtag-specific claim.
- Guarded undo: PASS. Both index hashes returned to baseline and all three URL-derived notes disappeared.

Non-blocking observed model-output caveat: the temporary experience used `stags` instead of `tags` in one frontmatter key. The acceptance requirements for source provenance, question state/frequency, atomicity, checkpointing, and cleanup all passed, and the malformed temporary note was removed by guarded Undo.

## Ordered-image Interview Submission

- Files, in submitted order:
  1. `oa-t33-page-1.png` (56,792 bytes)
  2. `oa-t33-page-2.png` (53,159 bytes)
- Actual Sidebar previews: PASS. Both preview byte lengths matched the submitted PNG byte lengths before Send.
- Completed run: `0f17c74b-e643-4b11-aa6d-bce4586cf5cf`
- Batch: `ticket-33-image-final`
- Checkpoint: `refs/offeragent/checkpoints/ticket-33-image-final` (`0c1173213327ea208f9a6f005862cb3be4ebf7ff`)
- Runtime-derived ordered Source Fingerprint: `sha256:e53dd32bc15081afca72b2e506d2018d9187c98e2531a2bc83aa02e5a6f3efa8`.
- The experience stored ordered image count `2`, the exact fingerprint, and visible marker `OA-T33-IMG-92C1`; no image bytes were written to the Vault.
- Exactly two questions were created with `answer-state: needs-research`, `frequency: 1`, and one occurrence each.
- Interview Question synchronization: PASS.

Repeated fixture: PASS.

- Clean exact-repeat run: `460a3c1b-56d9-4c1c-9f24-cc528fc78e8b`.
- The composer was verified empty, then the same two in-memory File objects were attached in the same order.
- `interview_catalog` received the identical fingerprint `sha256:e53dd32bc15081afca72b2e506d2018d9187c98e2531a2bc83aa02e5a6f3efa8` and returned the existing experience via Source Fingerprint dedup.
- No `vault_propose_changes` call, no additional batch/checkpoint, no index hash change, and no frequency/occurrence increment occurred.
- One preceding repeat attempt had a typed transient transport failure and safely cleaned its attachments. A retry accidentally contained the restored pair plus a newly appended pair; its four-image fingerprint was independently reproduced and was not used as dedup acceptance evidence.

Guarded undo: PASS. The first image batch was undone only after the clean exact-repeat proof; both index hashes returned to baseline and all three image-derived notes disappeared.

Attachment cleanup: PASS. `%LOCALAPPDATA%\OfferAgent\attachments` contained two files only while each image run was active and contained zero files after every completed or failed terminal run.

## Cleanup and invariants

- Temporary-note cleanup: PASS. Searches for all text, URL, and image fixture markers/fingerprints returned zero matches after Undo.
- Unrelated Vault invariants: PASS.
  - Branch, HEAD, and index tree remained `main`, `a3c75fc864560d61bfb78b4334c19aa34aa3475a`, and `ca20c4c677d5464ae6a5381b62f64ac58b49c1c6`.
  - The tracked unstaged diff hash returned to the pre-acceptance value `af75574a1515cd57cfad3f919aa8a0257f4060db`.
  - The staged diff hash remained the empty hash `e69de29bb2d1d6434b8b29ae775ad8c2e48c5391`.
  - `experiences/index.md` returned to `fe62de8ee41ab0652f76052ab7aff658c76bcb7c0033667813a3c8dc8b7a9886`.
  - `interview/index.md` returned to `2f299dc7c7ceda7114cad1653edbe44fe07d1bf1899ffe9d2158662fb0bc1b80`.
  - `.obsidian/community-plugins.json` remained `62034f355aad88ac9aa1b5cb75dbd0e1e21f2bb80f63848b7ac809f013c8f941`.
  - `.obsidian/plugins/offeragent/data.json` remained `08c5139e3e5932319753040242a2764b2ec75d84b550dac2048d14b2fd057a3f`.
  - The only durable target changes were the explicitly confirmed `agent.md` migration, installed OfferAgent package, and append-only acceptance checkpoints.

Installed package hashes: PASS.

- `main.js`: `3942060cff52ee1f6351697bfa6e8705bbba559b5efcfd7509480a0378bb93f5`
- `manifest.json`: `3277f0a0fc663f83ca6098479a8bff54bc25f879212924282f862628a8515304`
- `runtime.js`: `de39ae023e8492e367fc60197d272718be6aaf6c792396f271c738bf6a3466b6`
- `styles.css`: `70fbe534210ec8728de009eceec176fe416da1775880b9f0fd09630b7c785dbf`

Each installed hash matched its freshly built repository artifact. The installed plugin remained enabled, the Runtime remained connected, permission mode remained `trusted_vault`, and exactly one Sidebar leaf was used for the acceptance interactions.

Deterministic test gate: PASS.

- Focused provider/Runtime regression: `node --test tests/agent-run.test.mjs`
- Durable acceptance-record contract: `node --test tests/target-vault-interview-acceptance-record.test.mjs`
- Full repository test/build/audit gates are run from the final ticket commit candidate before review and publication.
