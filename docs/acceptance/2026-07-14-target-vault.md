# Ticket #16 real target Vault acceptance

Date: 2026-07-14 (Asia/Shanghai)

Result: PASS

This acceptance used the installed OfferAgent package in the real target Vault at
`E:\obsidian项目\面试胜利！`. It did not use the plugin-entry test stub or the fake
Provider. Temporary acceptance notes were deleted after the run; OfferAgent remains
enabled and the acceptance Conversations remain in Runtime State as the durable audit
trail.

## Versions and invariants

- OfferAgent branch before Ticket #16: `obsidan` at `06767dc48df94de2b2b64ad96e9441d1f82c1f32`
- Protected `master`: `574d5e623552b5f5dd7106ed7612b7464c2603ad`
- Plugin: `offeragent` 0.1.0, desktop-only, minimum Obsidian 1.6.0
- Obsidian desktop shell: 1.7.4; loaded Obsidian core: 1.12.7
- Node.js used by the installed Runtime: 25.9.0 (production bundles target Node.js 20)
- OS: Windows 11, version 10.0.26200
- Target Vault branch remained `main`.
- Target Vault index tree remained `ca20c4c677d5464ae6a5381b62f64ac58b49c1c6`.

## Real acceptance results

| Area | Result | Real evidence |
| --- | --- | --- |
| Install, enable, health, models | PASS | The confirmed deployment added `offeragent` exactly once to `.obsidian/community-plugins.json`, installed the four package files, and created checkpoint `refs/offeragent/checkpoints/target-vault-migration-9b7f1ad78c45426d`. The installed plugin loaded automatically after Obsidian restart. Runtime and Provider both reported connected; models were GPT-5.5, GPT-5.4, GPT-5.4-Mini, and GPT-5.3-Codex-Spark. |
| Real Codex Agent Run | PASS | Conversation `e389b5c2-1539-486d-9e78-569a96b016a8`, Run `73493d04-4f0c-4b95-96dc-9d688a2de2eb`, model `gpt-5.4`, completed with the exact requested answer `OfferAgent real Codex E2E OK`. The required Agent Contract read ran through the local tool loop. |
| Restart persistence and Stop | PASS | After a complete Obsidian exit, Runtime child exit, and fresh launch, the completed prompt and answer were restored. Run `83aad578-ddc6-48ce-bcdb-9025e196b2c0` was stopped from the sidebar and remained `cancelled` after restart; it did not resume or replay. |
| Search and Evidence Snapshot | PASS | Exact search for `OA_E2E_SEARCH_71F4` found only `interview/OfferAgent-E2E-Search-2026-07-14.md`. The only normal read used `lineStart: 2`, `lineEnd: 3`. Runtime State contained one matching Evidence Snapshot with those exact bounds and content; lines 4-5 were absent. |
| Trusted Vault apply, atomicity, checkpoint, undo | PASS | Normal create batch `offeragent-e2e-create-2026-07-14` auto-applied, produced a commit checkpoint, and undid cleanly. Before/after the batch, unrelated tracked diff hash stayed `af75574a1515cd57cfad3f919aa8a0257f4060db`, unrelated untracked-set hash stayed `a2ed79718d89e33074be97f018cd5d95271f6256`, branch stayed `main`, and the index tree was unchanged. |
| Control-file confirmation | PASS | Deployment preview reported only `.obsidian/community-plugins.json`; no file changed before explicit `--confirm-control-migration`. The confirmed batch added the plugin and checkpointed the control edit. Re-deployment after Obsidian reformatted the JSON returned `controlMigration: "unchanged"` and preserved the file byte-for-byte. Normal Agent discovery continued to exclude `agent.md` and `.obsidian` control files. |
| Ask Every Time | PASS | A normal exact-replace batch remained `pending`, left the source unchanged, and exposed one Apply/Reject decision. Reject completed the waiting Run with a durable rejected result and no write. |
| Read Only | PASS | A create proposal failed with typed `permission_denied`; no target file was created and no escalation field bypassed policy. |
| Interrupted write and explicit Resume | PASS | Pending batch `batch-replace-status-offeragent-e2e-search-2026-07-14` survived full Obsidian/Runtime shutdown twice with the same Conversation, Tool Call, batch identity, and pending action. A single later Apply produced one checkpoint and one file mutation; the Agent resumed and reread current state without replaying the side effect. |
| Stale evidence before Resume | PASS | Conversation `a4a179e4-0cd3-4a93-a846-6d4104a62393`, Run `0472bfa8-8a8a-47da-93c4-e21a3bb094bc`, read only lines 3-5, then the installed Runtime was terminated before the Run finished. Runtime restart durably recorded `agent_run.interrupted` at sequence 6 and the live sidebar exposed `Resume`. Before clicking it, all three read lines were edited outside OfferAgent. The same Run recorded `agent_run.resumed` at sequence 7, automatically reread the same bounds at sequences 10-11, marked the original Evidence Snapshot stale, and persisted a fresh snapshot. Version/hash changed from `mtime:1783990493218:size:119` / `sha256:8907f4ff…` to `mtime:1783990609642:size:135` / `sha256:0721ce40…`; the final answer used only `Status: MANUALLY-CHANGED-BEFORE-RESUME`, not the old status. |
| Guarded undo conflict | PASS | After a successful apply, a later edit changed the applied target. Undo returned `conflicted` with applied/current hashes and a guarded diff. The later edit remained byte-for-byte intact. |
| Direct URL read | PASS | The real `web_read` tool read `https://example.com/` and returned `Example Domain` with the source URL. |
| Hosted Web Search | PASS | Capability reprobe for `gpt-5.4` changed `unknown` to `available`. A real Hosted Web Search returned the official Obsidian homepage and persisted a structured citation titled `Obsidian - Sharpen your thinking` for an `https://obsidian.md/` URL. |
| Active theme and narrow sidebar | PASS | In the active Light theme, the live right sidebar was constrained to 320 px. Runtime stayed connected, GPT-5.4, Ask Every Time, and one citation remained visible. The input was 278.4 px wide, the primary button stayed inside the 320 px sidebar, and `scrollWidth <= clientWidth` (no horizontal overflow). The original 609.5 px width was restored afterward. |
| Diagnostics and secret hygiene | PASS | User-visible failures were typed and actionable (`transport_error`, `permission_denied`, `stale_evidence`, `invalid_path`). The sidebar and acceptance record contained no OAuth cache, bearer token, complete Vault file, or credential. Runtime State retained only bounded Evidence Snapshots and durable protocol metadata. |
| Scope | PASS | Installed Agent activity stayed within the v1 tools: Agent Contract, Vault list/search/read/propose, direct web read, and Provider-hosted search. No shell, arbitrary code, legacy server, vector database, or multi-agent promise was introduced. |

## Defects found and fixed during real acceptance

1. Obsidian's renderer returns numeric timer handles. Runtime startup used Node-only
   `.unref()` calls and stopped after its health handshake. Timer unref is now optional,
   with the packaged-plugin smoke test running under browser-style numeric timers.
2. The existing Runtime State contained legacy compact durable-event payloads. Replay now
   reconstructs the protocol envelope from durable table columns without changing current
   full-event records.
3. The existing database identified itself as schema v10 while its
   `protocol_responses` table lacked two additive columns. Startup now runs the idempotent
   protocol-column repair even when the schema version is already current.
4. Deployment previously installed but did not enable the plugin, and Obsidian JSON
   formatting could look like a repeated control migration. The confirmed batch now
   preserves existing plugin IDs, enables OfferAgent, and preserves already-enabled JSON
   byte-for-byte.
5. Deployment previously migrated controls before attempting the package installation.
   Package installation now completes first, so an injected installation failure cannot
   enable OfferAgent or modify the Agent Contract or Vault-local Skill.

## Verification

- `npm.cmd run build`: PASS
- `npm.cmd test`: 136 tests, 135 passed, 1 live-only test skipped, 0 failed
- `npm.cmd audit --audit-level=high`: 0 vulnerabilities
- The skipped automated live test requires `OFFERAGENT_LIVE_CODEX=1`; this record instead
  includes multiple real installed Codex runs through the Obsidian sidebar.
- Final target cleanup: all four temporary acceptance paths were absent, permission mode
  was restored to Trusted Vault, OfferAgent was enabled exactly once, one installed Runtime
  process was healthy, and the target branch/index invariants still held.

## Visual-capture limitation

The Electron window rendered correctly on screen and the live DOM/layout metrics above
passed. Windows `CopyFromScreen` and `PrintWindow`, and Electron CDP screenshot capture,
returned black frames or timed out under this GPU-rendered session, so no misleading image
is attached. This limitation affects only screenshot evidence, not the exercised UI,
layout measurements, or interaction results.
