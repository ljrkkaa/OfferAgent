# Ticket #50 real target Vault acceptance

Date: 2026-07-15 (Asia/Shanghai)

Result: PASS

This acceptance used the packaged OfferAgent plugin and the real Codex Provider in the
target Vault at `E:\obsidian项目\面试胜利！`. It exercised the installed Obsidian UI rather
than a test DOM. All dedicated acceptance Conversations, attachment bytes, and the
unapplied temporary Vault proposal were removed afterward.

## Package and target invariants

- Initial Ticket #50 snapshot before milestone-review fixes: `obsidan` at
  `ed6290585a3e064d9c52ab79a228eab41aaf6781`. The fixes and this updated record are
  amended into that same one-ticket commit.
- Deployment preview returned `confirmationRequired: false`, no control files, and no
  installation. Confirmed deployment returned `controlMigration: "unchanged"` and
  `installed: true`.
- Installed `main.js` SHA-256:
  `6ea2493d619ea14d3200518ecaa0be9331b3de7e1e6afd30100dd1812e40a8a6`.
- Installed `manifest.json` SHA-256:
  `3277f0a0fc663f83ca6098479a8bff54bc25f879212924282f862628a8515304`.
- Installed `styles.css` SHA-256:
  `ba976acc83de5d455e2b856fd96bfe4d6d036eab89d0dbcc06ccd7663197e22b`.
- Installed `runtime.js` SHA-256:
  `57b3bc658fdd2c2162a5b75baa6220db4ba191c099dc193d4aa27b0bb7f2ffd7`.
- Every installed hash matched the corresponding package artifact exactly.
- The target Vault remained on `main` at
  `a3c75fc864560d61bfb78b4334c19aa34aa3475a`; its index tree remained
  `ca20c4c677d5464ae6a5381b62f64ac58b49c1c6`.
- The target Vault already had unrelated tracked and untracked user work. Acceptance did
  not stage it, alter its branch or index, or leave an acceptance note behind.

## Spec #40 manual scenarios

| # | Result | Real target evidence |
| --- | --- | --- |
| 1 | PASS | The installed Sidebar opened with Chinese controls, labels, empty state, permissions, tool statuses, and settings. A healthy Runtime rendered no persistent connected banner. Light and dark themes passed at both required widths; measurements are below. |
| 2 | PASS | A 32 x 24 bitmap was placed on the Windows image clipboard, the real Obsidian textarea was focused, and an actual `Ctrl+V` keystroke produced an unsaved `image.png` preview. The preview exposed Chinese move/remove labels and was sent successfully. The final package was also given two clipboard images: both appeared in one `display:flex`, `overflow-x:auto` horizontal strip as separate compact cards. |
| 3 | PASS | Obsidian exited through its main window, the original Runtime child exited, and a fresh Obsidian process started a new Runtime child. `Ticket #50 图片验收` reopened automatically with four messages and two persisted image references. The second reference loaded at 32 x 24; the first remained correctly lazy while outside the viewport. The earlier image had already been reused in the second Agent Run without another paste. |
| 4 | PASS | During streaming, the transcript followed while at the bottom. After scrolling to the top, its scroll height grew from 633 to 676 px without changing `scrollTop` from zero; `新内容` then became visible. Clicking it restored the bottom position (`scrollTop` 300 for a 757 px transcript with a 458 px viewport). |
| 5 | PASS | `+` pinned the active note `interview/面试-RAG-稠密向量模型如何选型.md`; typing `@daily/2026-07-14` selected `daily/2026-07-14.md`. Both appeared simultaneously as named chips with Chinese open/remove labels, and both were removed successfully. |
| 6 | PASS | A real Agent Run searched the Vault and read only `daily/2026-07-14.md:1-3`. The answer listed exactly one source with that range and bounded snippet. Its button label was `打开来源 daily/2026-07-14.md 第 1 到 3 行`; clicking opened that note, after which the original active note was restored. |
| 7 | PASS | The textarea stayed enabled during a streaming Run and accepted `流式期间保留的用户草稿` without creating a queued Run. Stop persisted the partial answer as `cancelled`. With a later draft present, `放入输入框` stayed enabled and exposed `当前草稿或图片会在确认后被原提示词替换`; it invoked the exact confirmation `当前草稿和图片会被已停止运行的原提示词替换。继续吗？`, then replaced the draft with the stopped Run's original prompt only after confirmation. The final package also exposed `放入输入框` on the latest completed user message. With a protected draft present it invoked `当前草稿和图片会被最近一条用户消息替换。继续吗？`, restored `Reply exactly READY`, allowed editing it to `Reply exactly RESENT`, and submitted a second real Run. The resulting transcript contained two user messages and two completed Provider answers (`READY`, `RESENT`); the temporary Conversation was then deleted. |
| 8 | PASS | New image and source Conversations received distinct automatic titles, then were renamed to `Ticket #50 图片验收` and `Ticket #50 来源验收`. Searching for the latter left exactly one of 58 current items visible. Archive hid it from current Conversations, `显示已归档` exposed it with a `恢复` action, restore returned it, and confirmed deletion removed each acceptance Conversation. In the final package, Settings was nested under the header `⋯`, and every row nested Rename/Archive/Delete under one labelled `⋯`; at 320 px there were no direct row actions and no overflow. Deleting the inactive Stop-and-Revise acceptance Conversation left the active conflict Conversation unchanged. |
| 9 | PASS | Ordinary tool groups rendered as closed `details` elements and their summaries opened with a real Enter keypress. In `每次询问` mode a real proposal showed `Vault 变更：等待确认` with `全部应用` and `全部拒绝` while the target file was absent; Reject changed it to `已拒绝` and the file remained absent. The final package then auto-applied a dedicated create, the note was edited outside OfferAgent, and Undo rendered `Vault 变更：存在冲突` plus the guarded current/checkpoint diff. The later edit `later-user-edit-must-survive` remained byte-for-byte intact. The note was deleted during cleanup and permission was restored to `信任 Vault`. |
| 10 | PASS | The image Conversation owned two 262-byte attachment files before deletion. Confirmed UI deletion removed the Conversation and reduced the attachment directory to zero files, while the source and approval acceptance Conversations remained present. Those two Conversations were then deleted separately and the user's original `事件循环复习` Conversation was restored. |

## Theme, width, and accessibility evidence

| Theme | Width | Sidebar overflow | Page overflow | Composer/send inside Sidebar |
| --- | ---: | ---: | ---: | --- |
| Light (`moonstone`) | 320 px | 0 px | 0 px | Yes |
| Light (`moonstone`) | 480 px | 0 px | 0 px | Yes |
| Dark (`obsidian`) | 320 px | 0 px | 0 px | Yes |
| Dark (`obsidian`) | 480 px | 0 px | 0 px | Yes |

- The original Light theme and 621.5 px right-sidebar width were restored after the
  four measurements.
- The hidden file picker had `tabIndex=-1`; visible controls stayed in the keyboard
  order. The `+` button exposed `添加上下文或图片` and attachment icon controls exposed
  Chinese move/remove labels.
- A focused tool summary with `2 个工具活动，按回车展开详情` opened after a real Enter
  keypress and updated `aria-expanded` to `true`.
- Provider/model product names and external Provider errors stayed verbatim; OfferAgent
  controls, local validation messages, permissions, statuses, and actions were Chinese.

## Milestone-review fixes revalidated here

- A generic text-only prompt such as `请帮我分析一下` now keeps that text as its title
  instead of receiving an image-only fallback title.
- Deleting an inactive Conversation uses its ID directly and keeps the current transcript;
  UI failures are surfaced as a Notice instead of an unhandled rejection.
- Conversation attachment cleanup errors are aggregated and reported instead of silently
  claiming that private image bytes were deleted. A post-delete cleanup failure leaves a
  recoverable tombstone and no longer makes the already-durable Conversation deletion look
  unsuccessful to the Sidebar.
- Stop-and-Revise confirmation, both overflow-menu levels, the horizontal image strip,
  and a real guarded conflict were all exercised in the final installed package.
- Stop-and-Revise is unavailable during an atomic image import. Open, Rename, and
  Archive/Restore failures in the history drawer now surface Chinese Notices, and Open
  closes the drawer only after navigation succeeds.
- The latest user message exposes a keyboard-focusable `放入输入框` action on hover/focus;
  replacing a non-empty draft or images requires explicit confirmation.

## Verification and cleanup

- `npm.cmd run build`: PASS, including typecheck, protocol/runtime/plugin builds, and
  package assembly.
- `node --test --test-concurrency=2 tests/*.test.mjs`: 281 total, 280 passed, 0 failed,
  1 explicitly skipped live-Codex test.
- Initial milestone Standards and Spec findings were fixed before the final re-review.
- OfferAgent remained enabled exactly once and its replacement Runtime was healthy after
  the full Obsidian restart.
- Final target permission: `trusted_vault`; final theme: Light; final attachment file
  count: 0; temporary approval and conflict paths absent.
- The target branch, HEAD, and index tree matched the pre-acceptance baseline shown above.
