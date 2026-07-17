# ADR 0029: Migrate legacy Obsidian State through an offline Python plan

- Status: accepted
- Date: 2026-07-17
- Issue: #66

## Context

The `obsidan` branch persisted Conversations, Agent Runs, messages, retained images, tool checkpoints, Vault change batches, provider metadata, and plugin settings in a TypeScript/sql.js Runtime. The fused product has one Python harness with a different authoritative SQLite/event model, Conversation attachment store, permission policy, tool language, and recovery contract.

A mechanical database copy or branch merge would either make legacy effects replayable, leak credentials into new state, create UI-only history that cannot replay, or leave `agent.md` and the legacy `obsidian-cli` Skill instructing the new Agent to call tools that no longer exist.

## Decision

Use one offline Python migration module. The module opens only an immutable schema-v19 source snapshot and builds a closed plan before modifying a target.

The plan imports only:

- Conversations with at least one completed Run whose user and assistant messages are both present and consistently bound;
- those completed Turns as terminal Session/Turn/Run/RunState projections plus typed `turn.started`, `assistant.completed`, and `turn.completed` events;
- retained images owned by an imported user message, in contiguous message order, after size, hash, media, image-structure, submission, Conversation, and global-capacity validation;
- the legacy Vault permission mode through an explicit mapping to the closed plugin settings schema.

The plan never imports active, interrupted, failed, or cancelled Run progress; Run checkpoints; tool calls or results; Vault change batches; provider metadata; credentials, tokens, or secrets; unknown plugin fields; or Fast Mode. Every excluded legacy identity or setting key is included in the report without its sensitive value.

Source identities map deterministically to `ses_mig_*`, `turn_mig_*`, `run_mig_*`, and `art_mig_*` identities using the complete source hash. A receipt containing that hash makes a successful migration exactly-once. Dry-run and execute use the same plan.

Before activation, the migrator creates a read-only target backup, copies existing Python State to a sibling staging directory, applies the import there, verifies the source and target fingerprints again, and stages the closed plugin settings plus version-pinned Vault control templates. State, plugin data, `agent.md`, and `.codex/skills/obsidian-cli/SKILL.md` then switch as one rollback-capable authority change. Missing/corrupt images, capacity failures, identity collisions, custom control-file conflicts, target changes, unsupported schema, or any activation error leave the prior authorities in place.

The standard local installer invokes this migration only when both the known legacy `%LOCALAPPDATA%/OfferAgent/state.db` and the standard legacy `.obsidian/plugins/offeragent/data.json` exist. Ordinary updates continue to move current `data.json` opaquely. The updater's existing process gate keeps both old and new Workers stopped during the switch.

The target Contract uses the Python harness tool names (`agent_contract.read`, `skill.read`, `daily_note.context`, `planning_memory.*`, `interview_catalog.search`, `research_browser.navigate`, `vault.*`, `project.*`, and `vault.changes.apply`) and defines the new confirmation, Run interruption, replay, and recovery semantics. The old Skill name is retained only as a compatibility identity; it grants no shell or Obsidian CLI.

## Consequences

- Legacy chat history remains visible and replayable without treating old execution state as safe continuation state.
- Legacy side effects and secrets cannot silently cross the architecture boundary.
- A source database with an unknown or partially applied schema must be repaired or explicitly exported before migration.
- A customized legacy Contract or Skill is a target conflict and requires a human to reconcile it; migration does not overwrite an unrecognized control file.
- Backups consume local disk by design and are retained as read-only recovery evidence.
- Migration remains installer/CLI code and is excluded from the frozen Worker, preserving one production Agent Loop and a smaller Runtime closure.
