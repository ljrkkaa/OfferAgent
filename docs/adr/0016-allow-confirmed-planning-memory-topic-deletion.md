---
status: accepted
---

# Allow confirmed Planning Memory topic deletion

Planning Memory consolidation must remove superseded understanding instead of preserving an append-only transcript. A Vault Change Batch may therefore delete an existing file only when its canonical path matches `memory/{user,feedback,project,study}/*.md`, the same batch synchronizes `memory/MEMORY.md`, and the plugin validates the topic metadata and index link relationship.

This scoped operation supersedes the blanket no-delete statements in ADR 0009 and ADR 0010 only for Planning Memory topics. It does not permit deletion of ordinary Vault content, control files, the memory index, directories, or external paths.

## Consequences

- Planning Memory deletion is confirm-only in every permission mode; `trusted_vault` never auto-applies it.
- The delete and index update remain one atomic batch with checkpoint, rollback, restart recovery, and guarded undo.
- A malformed or stale index rejects the whole batch before mutation.
- No move or general-purpose file deletion capability is introduced.
