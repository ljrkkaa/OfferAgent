# Ticket #24 real Vault acceptance record

Date: 2026-07-14 (Asia/Shanghai)

Repository branch: `obsidan`

Target Vault: `E:\obsidian项目\面试胜利！`

## Pre-migration gate

- `npm.cmd test`: 161 tests, 160 passed, 1 explicit live-only skip, 0 failed.
- `npm.cmd run build`: passed.
- `git diff --check`: passed.
- The Vault's root `agent.md` was read before migration.

## Contract migration

The deployment preview selected only `agent.md`. The applied migration record is:

- batch: `target-vault-migration-f12cb7ef67b761b1`
- state: `applied`
- checkpoint: `refs/offeragent/checkpoints/target-vault-migration-f12cb7ef67b761b1`
- checkpoint commit: `4b2df6e36e19c3513bb84c7dc01cec6ca9d75f42`
- `agent.md` before: `sha256:4320031668560c17d36d539675d86c997ec6d6d57b8cb36169ffca5cd34f526f`
- `agent.md` after: `sha256:28c04d1b9ba478c0ff3b29aed918fc347965079c2a3b8d2a6d59d2866e5171fb`

The migration preserved the pre-existing Vault state outside the authorized target. The recorded pre/post invariants were unchanged for branch (`main`), index tree (`ca20c4...`), tracked diff hash (`c1ff59...`), status hash (`64b3be...`), and untracked-set hash (`f242a1...`). A second deployment preview was clean.

The installed plugin files exactly matched the built artifacts after the final deployment:

| File | SHA-256 |
| --- | --- |
| `main.js` | `fa244751f07c0822979b8b432c4629ffe373ed109637543228dea3a87f257b68` |
| `manifest.json` | `3277f0a0fc663f83ca6098479a8bff54bc25f879212924282f862628a8515304` |
| `runtime.js` | `d150b53aee570ad61cf8d9f60f3b3241deea0717374f3cf3e16f3c0e8728012b` |
| `styles.css` | `9115509e0a52efbcea682a792ad2d1e631a829dadc3c0aeafeff84388cdbbef2` |

## Exact two-message live regression

Environment:

- Obsidian 1.12.7
- OfferAgent permission mode: Trusted Vault
- model: `gpt-5.5`
- conversation: `351003b4-a4d3-4246-8dde-daa118b6b434`
- `daily/2026-07-14.md` did not exist before the run
- `memory/` did not exist before the run

Message 1, exactly:

> 帮我做一个今天的学习日记

The agent resolved the Daily Note settings, searched and read relevant Daily, interview, and experience sources, and applied one batch:

- batch: `daily-study-plan-2026-07-14`
- task: `创建今天的学习日记/每日学习计划`
- target: `daily/2026-07-14.md`
- state: `applied`
- checkpoint: `refs/offeragent/checkpoints/daily-study-plan-2026-07-14`
- checkpoint commit: `0d3ae4217e13c7f3c3dcfd718185b79e4c16a246`
- before version: `missing`
- after version: `sha256:d50dc9faa63535f30898003e78458c1f4c08dfd01442444ffc37ab630e527b37`

The resulting Daily Note uses the configured template/frontmatter and contains only unchecked planning items plus source hints and an empty review area. The response explicitly stated that the plan was not completion evidence.

Message 2, exactly:

> 可以的

The same conversation retained context and replied that the plan was accepted, with no additional file change. It again distinguished future checked/explicit completion evidence from the plan itself.

## No false progress and explicit Study-State sync

The relevant status hashes before the two-message run were:

- `experiences/index.md`: `fe62de8ee41ab0652f76052ab7aff658c76bc7c0033667813a3c8dc8b7a9886`
- `interview/面试八股学习进度.md`: `3edebfb46169b2a2a7d46653712c5191976e432b683e93661ffab7a8b6c03fca`

Both hashes were identical after the two-message run and after the following explicit request:

> 请执行今天的学习状态同步

The agent read the Daily Note, found every check box unchecked and the review area empty, and reported that it changed neither `experiences/` nor `interview/`. This confirms that Daily Study Planning and Study-State sync remain distinct workflows.

Planning Memory was visible to the runtime through the default `planning_memory_list` step. No durable cross-day direction was produced by this request, so no Study Memory write was warranted and `memory/` remained absent. If such a direction had been produced, the contract requires it in the same change batch.

## Preview, versions, and rollback evidence

- The applied Daily batch records its target, before/after versions, state, and checkpoint in `state.db`.
- The Daily checkpoint contains no `daily/2026-07-14.md`, matching the recorded `missing` before version.
- The current file hash matches the batch's recorded after version.
- Therefore preview/version inspection is durable and the guarded rollback has a verified restorable pre-state without actually undoing the accepted user result.
- The contract migration has its own applied record and checkpoint, independent of the Daily batch.

## Runtime regressions found during live validation

Live validation exposed stateless Responses API replay gaps that unit fixtures had not exercised. Red tests were added before each correction. The runtime now:

- retains message-only final output when no text delta is emitted;
- replays encrypted reasoning items and provider function-call identifiers/status in response order;
- includes encrypted reasoning content for stateless requests;
- preserves assistant preambles and correct parallel call/result ordering;
- retries one empty provider response, then surfaces a visible provider error;
- accepts `daily_note_context` from the provider's local-tool allowlist.

No diagnostic hook or provider trace file remains in the repository or deployed runtime.

## Final gate

- `npm.cmd test`: 163 tests, 162 passed, 1 explicit live-only skip, 0 failed.
- `npm.cmd run build`: passed, including TypeScript typecheck and packaged plugin/runtime builds.
- `git diff --check`: passed.
- No temporary provider diagnostic hook or trace marker exists in source, tests, documentation, or the deployed runtime.
- The smoke test pins its initial local date, so this record remains reproducible after midnight.
- Review regressions verify out-of-order Responses `output_index` completion and stale-evidence pruning without disturbing reasoning/call/result replay order.
- Before a resolved Daily target is applied, semantic capture augments that same proposal with any missing durable memory actions; a later fallback excludes only Study operations while preserving unrelated User, Feedback, and Project capture. The regression uses a configured `journal/` target, and a committed-result/lost-checkpoint Resume case, to prove this is neither hardcoded to `daily/` nor split across transactions.
- The final built package was redeployed after review fixes; plugin reload succeeded, every installed artifact matched the build, and the subsequent deployment preview was clean.
