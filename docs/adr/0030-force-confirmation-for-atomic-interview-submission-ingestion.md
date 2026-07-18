---
status: accepted
---

# Force confirmation for atomic Interview Submission ingestion

Interview ingestion remains a semantic task inside the single Python Agent Loop, even though it crosses retained USER images, public URLs, Catalog discovery and multi-file Vault writes. The runtime adds a byte-free, order-preserving image-hash manifest to the same USER input as the submission; it never moves images into SYSTEM input or asks TypeScript to interpret them. The Python Agent alone decides semantic readability, Experience identity, extraction and Question merging.

The manifest shown to the model is not itself authorization. During Run preparation the Python Harness derives a durable root-Run authority from the Conversation Attachment Store's materialized attachment order, content hashes and the captured local date. A per-root-Run plugin-execution boundary requires `interview_catalog.search` to carry that exact ordered manifest, records the successful normalized source as a Catalog receipt, and permits at most one distinct `changeKind=interview_submission` Apply bound to the same receipt. Rejection, conflict, failure and unknown outcome consume that Run's single proposal slot; only an exact invocation replay remains valid. Recovery reuses and verifies the durable authority instead of recomputing the date or trusting model text. New production capability snapshots use schema v2. The explicit v1 reader retains the original v1 fingerprint shape and accepts only Runs that have not entered any Tool stage; it reconstructs authority from immutable Turn input, Store materialization and the original `BudgetCheckpoint.started_at`, never the restart date. A v1 Run with pending or completed Tool activity is terminated through the normal recovery-failure path because its old proof cannot reconstruct the private Catalog receipt and single Apply claim safely. Unsupported snapshot versions also fail closed.

The plugin-owned `interview_catalog.search` uses one shared Interview source-identity module to normalize public URLs, derive the current source identity from the ordered image hashes, and return bounded Experience, Question and primary-index bindings with versions and hashes. The Apply boundary validates the already-normalized identity with that same module; URL and fingerprint algorithms are not copied between Catalog and Vault mutation code. Catalog results are discovery results only: the Agent must read exact candidates before making a semantic decision. Newly created knowledge uses `experiences/*.md` with `experiences/index.md` and `interview/*.md` with `interview/index.md`; legacy layouts remain discoverable but are not targets for new files.

The Agent submits the result through the existing `vault.changes.apply` boundary with `changeKind=interview_submission`; there is no dedicated TypeScript ingestion workflow or second write tool. The batch contains at most one Experience plus all related Question and index changes. It inherits the existing atomic apply, Git checkpoint, rollback, recovery and Guarded Undo behavior.

Unlike ordinary Vault content, an Interview Submission never auto-applies. In both `normal` and `trusted-workspace`, the plugin shows every target's complete before and after content in a scrollable Obsidian Review Modal and requires explicit confirmation; `read-only` remains a hard denial that confirmation cannot elevate. A Review hash binds the exact operations, batch ID, source receipt, every source path/version/hash and every target path/version/hash or missing precondition actually displayed. Truncated previews cannot authorize writes. Any drift invalidates the entire batch before application, so rejection, conflict and unknown outcome cannot become a partial or blind retry.

Forward writes, compensation and Guarded Undo use the same conditional mutation port. The unconditional `restore(path, content)` seam is forbidden because a user or another plugin can edit between a preliminary read and an unconditional modify or delete. Existing-file restoration uses `Vault.process()` with an expected content identity and fails closed on drift by returning the callback's current content; it does not rely on undocumented exception-abort behavior or promise zero metadata writes. Obsidian's public Vault API does not provide compare-and-delete, compare-and-rename or a multi-file transaction. Therefore reversing a created file to absence is unsupported by the production adapter: it preserves the file, records the exact manual-review path and latches later writes instead of calling unconditional `Vault.delete()`.

"Atomic" here means one complete user decision plus a durable, drift-aware recovery state machine. Git Checkpoints, the Journal and serialized execution do not make several Vault files switch instantaneously for Obsidian Sync, other plugins or external editors. The primary-source capability analysis is recorded in `docs/architecture/obsidian-conditional-vault-mutations.md`.

## Consequences

- Trusted Workspace gains one deliberate, narrow confirmation exception because model interpretation can otherwise create durable, cross-index knowledge without a final user checkpoint.
- The runtime, Catalog and plugin enforce structural provenance and atomicity while the Python Agent remains the sole semantic authority.
- Ordered screenshot identity and normalized public URLs are stable without copying image bytes, full pages or unrelated personal information into the Vault.
- A root Agent Run cannot split one Interview Submission across several proposal attempts or change its attachment identity between Catalog and Apply.
- Pristine schema-v1 in-flight Runs remain recoverable without relabeling their durable proof; v1 Runs with prior Tool activity fail closed, and all newly prepared Runs bind Interview authority in schema v2.
- Guarded recovery never overwrites a concurrently edited file through an unconditional restore; unsupported reverse deletion becomes explicit manual review.
- Logical atomicity is narrower than a native multi-file transaction, matching the guarantees available from the official Obsidian API.
- This decision narrows the automatic-write rule in ADR 0010 and preserves the single-loop boundary in ADR 0025 and ADR 0027.

## Alternatives considered

- A dedicated TypeScript Interview Submission workflow was rejected because it would duplicate semantic intent, readability and identity decisions outside the Python Agent Loop.
- Auto-applying the batch in `trusted-workspace` was rejected because ordinary file trust is not consent to persist a model's interpretation of a multi-source interview event.
- Letting Python write the Vault directly was rejected because it would bypass plugin-owned permissions, Obsidian versions, checkpoints and guarded recovery.
