---
status: accepted
---

# Force confirmation for atomic Interview Submission ingestion

Interview ingestion remains a semantic task inside the single Python Agent Loop, even though it crosses retained USER images, public URLs, Catalog discovery and multi-file Vault writes. The runtime adds a byte-free, order-preserving image-hash manifest to the same USER input as the submission; it never moves images into SYSTEM input or asks TypeScript to interpret them. The Python Agent alone decides semantic readability, Experience identity, extraction and Question merging.

The plugin-owned `interview_catalog.search` normalizes public URLs, derives the current source identity from the ordered image hashes, and returns bounded Experience, Question and primary-index bindings with versions and hashes. These are discovery results only: the Agent must read exact candidates before making a semantic decision. Newly created knowledge uses `experiences/*.md` with `experiences/index.md` and `interview/*.md` with `interview/index.md`; legacy layouts remain discoverable but are not targets for new files.

The Agent submits the result through the existing `vault.changes.apply` boundary with `changeKind=interview_submission`; there is no dedicated TypeScript ingestion workflow or second write tool. The batch contains at most one Experience plus all related Question and index changes. It inherits the existing atomic apply, Git checkpoint, rollback, recovery and Guarded Undo behavior.

Unlike ordinary Vault content, an Interview Submission never auto-applies. In both `normal` and `trusted-workspace`, the plugin shows the complete preview and requires explicit confirmation; `read-only` remains a hard denial that confirmation cannot elevate. The decision is bound to the exact operations, batch ID, every source path/version/hash and every target path/version/hash or missing precondition. Any drift invalidates the entire batch before application, so rejection, conflict and unknown outcome cannot become a partial or blind retry.

## Consequences

- Trusted Workspace gains one deliberate, narrow confirmation exception because model interpretation can otherwise create durable, cross-index knowledge without a final user checkpoint.
- The runtime, Catalog and plugin enforce structural provenance and atomicity while the Python Agent remains the sole semantic authority.
- Ordered screenshot identity and normalized public URLs are stable without copying image bytes, full pages or unrelated personal information into the Vault.
- This decision narrows the automatic-write rule in ADR 0010 and preserves the single-loop boundary in ADR 0025 and ADR 0027.

## Alternatives considered

- A dedicated TypeScript Interview Submission workflow was rejected because it would duplicate semantic intent, readability and identity decisions outside the Python Agent Loop.
- Auto-applying the batch in `trusted-workspace` was rejected because ordinary file trust is not consent to persist a model's interpretation of a multi-source interview event.
- Letting Python write the Vault directly was rejected because it would bypass plugin-owned permissions, Obsidian versions, checkpoints and guarded recovery.
