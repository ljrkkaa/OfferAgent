---
status: accepted
---

# Build interview knowledge through Catalog candidates and one Vault batch

OfferAgent keeps interview ingestion inside the single Python Agent Loop. A plugin-owned
`interview_catalog.search` tool scans only structured Interview Experience and Interview Question notes, returns
bounded metadata plus current versions and hashes, and identifies exact URL or source-fingerprint matches. It does
not decide semantic identity. The Agent reads exact candidates, preserves distinct candidate/date/round/event
identities, and makes the semantic merge decision with the current Interview Submission in context.

One logical submission produces at most one `vault.changes.apply` proposal containing the Experience, deduplicated
Questions, occurrence/frequency changes, and indexes. A repeated source event neither creates a second Experience
nor increments Question frequency. New questions start at `needs-research`; Answer State remains independent from
Learning State. The batch stores structured context and minimal Source Metadata, never raw screenshots, rendered
pages, or Conversation transcripts.

Public research remains an Agent-selected evidence path. Dynamic or authenticated pages use the isolated Research
Browser from ADR 0019. Research pages enter the typed provenance model as clickable Web Source References, while
their rendered content is explicitly untrusted and cannot introduce instructions or broaden the requested scope.

## Consequences

- Candidate discovery is deterministic, bounded, versioned, and easy to test; semantic identity remains with the
  Agent instead of becoming a brittle filename or keyword algorithm.
- Experience and Question consistency inherits the existing confirmation, journal, rollback, recovery, guarded undo,
  and hash-conflict behavior of Vault Change Batches.
- Text, URL, and ordered images stay one submission unless the user explicitly asks to split them.
- The catalog has no write API, hidden copy, vector database, background crawler, or second Agent Loop.

## Alternatives considered

- A fixed TypeScript ingestion workflow was rejected because it would duplicate intent handling and semantic
  planning outside the Python Agent.
- Deduplicating only by company, role, or overlapping questions was rejected because it collapses distinct source
  events and corrupts frequency.
- Persisting screenshots or complete web pages was rejected because it duplicates retained Conversation Attachments
  and expands the Vault into an unnecessary source archive.
