---
status: accepted
issue: 74
---

# Interview Submission reconciliation specification

## Outcome

OfferAgent must reconcile one readable Interview Submission against the current Interview Catalog without
creating duplicate Experience notes, Question occurrences, frequency, or index links. The Python Harness remains
the only Agent Loop and semantic authority. The Obsidian plugin remains the only Vault reader/writer and validates
the proposed structure, exact review, conditional mutations, and its durable Batch Journal.

One root Run ends in exactly one of these outcomes:

1. **New event**: create one Experience and add each Question occurrence once.
2. **Existing event**: merge source-supported information into one existing Experience; never create another
   Experience for the same canonical URL, ordered-image fingerprint, or semantically established repost.
3. **Complete duplicate**: report that no write is needed and do not call `vault.changes.apply`.
4. **Ambiguous identity**: ask one focused clarification question and do not call `vault.changes.apply`.
5. **Reconciled prior write**: adopt the plugin Journal's definite applied or not-applied result without replaying
   the write.

## Authority boundaries

- `interview_catalog.search` discovers bounded candidates and exact source matches. It does not decide semantic
  Experience or Question identity.
- The Python Agent reads exact candidate content and decides repost, distinct event, semantic Question identity,
  or ambiguity. A fixed title-normalization or similarity threshold is not a production identity algorithm.
- A Python Interview Submission guard persists the Catalog candidate bindings and exact-read receipts needed by
  the chosen plan. It rejects source drift, a new Experience in the presence of an exact source match, an unread
  merge target, a truncated Catalog result, or a second distinct Apply for the root Run before `tool.started`.
- The plugin validates the plan against current Markdown, source bindings, target versions, and occurrence links.
  It never changes the Agent's semantic decision.

## Event and Question identity

An exact canonical URL or exact ordered-image source fingerprint is conclusive evidence of the same source event.
An obvious repost can also map to an existing Experience, but only after the Agent reads the candidate. Different
candidate, event date, round, or source-event evidence remains a distinct Experience even when Questions overlap.
Missing identity evidence is ambiguity, not permission to merge or create.

Question frequency represents unique Interview Experience occurrences, not mentions, submissions, URLs, or
replays. For every existing Question changed by an Interview Submission:

- the before and after note must contain each Experience occurrence link at most once;
- the stored frequency must equal the number of unique occurrence links in both states;
- adding the current Experience link changes frequency by exactly one;
- retaining an existing current-Experience link leaves frequency unchanged;
- a new Question starts with one occurrence, frequency `1`, and `needs-research`.

An inconsistent existing Question fails closed for explicit repair instead of guessing a corrected count.

## Structured review

An applied submission carries a bounded review plan. Every reviewed entity has two independent facts:

- identity disposition: `new` or `existing`;
- mutation disposition: `create`, `modify`, or `none`.

The plugin renders these as 新增, 合并, 修改, and 无操作 badges while still showing complete before/after
content for every mutation. A `none` item is informational, must be bound to an exact-read source, and cannot hide
an operation. A completely duplicate submission has no empty Batch; the Agent reports 无需写入 in the normal
Conversation result.

The Apply Batch may contain zero or one Experience mutation, zero or more Question mutations, and only the indexes
that actually change. It must contain at least one real operation. All mutations and informational no-op items are
covered by the existing argument hash and Review hash.

## Unknown-outcome reconciliation

The plugin must finish Journal migration and `VaultChangeCoordinator.reconcile()` before spawning a replacement
Worker. It then publishes one sealed recovery generation: each durable Batch record is hashed by exact bytes into
`.recovery-seals/current/<sha256(batchId)>.json`, with schema version, Worker recovery token, Batch ID, byte length,
and content hash. The fixed-size filename keeps a maximum-length Batch ID out of the already-deep Windows state
path. The plugin atomically publishes `.recovery-ready.json` with schema version `2` and the same token only after
every seal is durable. Reusing the bounded `current` directory avoids unbounded generation-path growth; the token
embedded in every seal and in the marker prevents a stale generation from being adopted.

The Worker receives the original absolute plugin Journal path and the unpredictable recovery token as bootstrap
material. It validates every original path component before resolving links, so a symlink or Windows junction
cannot be hidden by early canonicalization. The directory must be a real, non-reparse directory contained in the
selected Vault's plugin configuration tree.

A Python `RecoveryLookup` reads only the marker, the exact Batch seal, and `<batchId>.json` from that internal
directory. It does not enumerate Vault notes, execute a ToolCall, mutate the Journal, or relax the plugin-owned
Vault boundary. Absence is authoritative only after the current marker is validated and no seal exists. A present
record is authoritative only when its exact bytes and length match its current-token seal. The lookup validates
the complete original binding: schema version, change kind, batch ID, root Run, ToolCall, Workspace, Run, argument
hash, idempotency key, Review hash, target identities, and the same safe-path contract as the plugin. Path identity
uses locale-independent lowercase folding and result state hashes use Unicode code-point ordering in both
runtimes. A create target's after hash is independently derived from the original content; outcomes that require
Vault bytes (`append`, `replace`, and `patch`) remain exclusively plugin-attested instead of making Python a second
Vault reader.

The plugin writes `prepared` before any checkpoint or Vault side effect. After the pre-spawn plugin reconciliation,
the following mapping is authoritative for either a Harness `STARTED` or `UNKNOWN` invocation:

| Plugin Journal observation | Harness result |
| --- | --- |
| no record | definite not-applied result; continue without replay |
| `applied` | reconstruct and adopt the successful result |
| `rejected` or `rolled_back` | reconstruct and adopt a definite not-applied result |
| `prepared`, `applying`, `undoing`, `undone`, `recovery_failed`, malformed, or unavailable | manual review |

`RecoveryCoordinator` may complete a Harness `STARTED` or `UNKNOWN` Invocation Journal only from such a definite
lookup result. `RecoveryPlanApplier` persists that result and the resumed Run state in one Unit of Work. No state
permits automatic replay of `vault.changes.apply`.

## Failure and migration behavior

- Rejection, stale source/target versions, replay, and recovery cannot add a second Experience link or frequency.
- A conflicting durable Batch binding is an error, never evidence for the current call.
- Existing plugin Journal v2 records remain readable; no source branch or old Runtime becomes a second authority.
- Legacy or corrupt Question occurrence counts require a separately reviewed repair.
- No vector database, background crawler, deduplication service, fixed semantic threshold, empty write Batch, or
  TypeScript Agent workflow is introduced.

## Implementation tickets

These are ordered TDD slices owned by GitHub issue #74; they are not separate architecture layers.

1. **Exact duplicate and ambiguity**
   - Persist Catalog candidate bindings and exact `vault.read` receipts in the Python root-Run authority.
   - Reject a new Experience when an exact source match exists.
   - Prove same URL, same ordered screenshots, complete duplicate, and ambiguous Question identity finish without an
     Apply or empty Batch.
2. **Distinct event with shared Questions**
   - Add the structured review plan to the plugin Tool schema and Python guard.
   - Create one Experience, reuse existing Questions, add each new occurrence once, and update frequency/indexes
     atomically.
   - Prove repeated mention inside the same Experience does not add another occurrence.
3. **Same-event and repost merge**
   - Allow a submission to modify one existing Experience instead of requiring creation.
   - Validate exact-read bindings, occurrence arithmetic, and only the indexes that actually change.
   - Render new/existing and create/modify/none facts as 新增/合并/修改/无操作 in the Review Modal.
4. **Unknown-result reconciliation**
   - Gate Worker spawn on plugin Journal migration/reconciliation.
   - Add the strict Python plugin-Journal `RecoveryLookup` and allow definite lookup results to complete Harness
     `STARTED` or `UNKNOWN` invocations atomically.
   - Prove applied, absent, rejected, rolled-back, binding-conflict, malformed, and manual-review states without any
     write replay.
5. **Product closure**
   - Run deterministic Scripted Model/Fake Plugin scenarios and a real production-composition recovery round trip.
   - Run full Python/TypeScript tests, type checks, architecture gates, frozen artifact smoke where affected, and an
     independent Standards + Spec review before the local commit and issue closure.
