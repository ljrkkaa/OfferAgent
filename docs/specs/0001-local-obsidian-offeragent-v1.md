# OfferAgent v1: Local Obsidian Plugin with a Self-Designed TypeScript Agent Runtime

## Problem Statement

The user has an existing OfferAgent codebase derived from a server-oriented Python/Khoj application, but wants OfferAgent to become a single-user local product embedded directly in an Obsidian Vault. The current architecture carries multi-user, web-server, database, and legacy UI complexity that the local workflow does not need. It also does not provide the desired ownership model in which the user's own Agent controls the model loop while Obsidian remains the sole authority for Vault access and write permissions.

The replacement must support durable conversations, resumable Agent Runs, conservative evidence-based study maintenance, safe Vault changes, Codex subscription authentication without an OpenAI API key, optional hosted web search, and a polished but minimal Obsidian-native interface. It must preserve the Vault as the source of truth, avoid copying complete Vault files into runtime storage, and prevent the model or Runtime from bypassing plugin-enforced permissions.

## Solution

Replace the legacy application with a TypeScript monorepo containing an Obsidian desktop plugin, a plugin-owned local Runtime process, and a shared versioned protocol. Present the product through one minimal Agent Sidebar inspired by Claudian's restrained conversation-first interaction, while retaining OfferAgent's own single-Agent behavior and permission model.

The Runtime owns the self-designed Agent state machine, model interaction, tool loop, Runtime State, and resumable checkpoints. The Obsidian plugin owns all Vault capabilities, per-Vault permissions, Vault Change Batch validation and application, Git Checkpoints, and user interaction. The Runtime and plugin communicate through a loopback-only HTTP management interface and a resumable bidirectional WebSocket. The initial Model Provider is a replaceable Codex subscription adapter that uses the user's existing local Codex authentication; it does not introduce Codex Agent or an Agents SDK.

## User Stories

1. As a single Vault user, I want to open OfferAgent in the Obsidian right sidebar, so that I can work with the Agent without leaving my notes.
2. As a single Vault user, I want the plugin to start its local Runtime automatically, so that I do not have to run a separate server manually.
3. As a single Vault user, I want the Runtime to run without a visible terminal window, so that OfferAgent feels like one local product.
4. As a single Vault user, I want a clear diagnostic when Node.js cannot be found, so that I can repair the local installation.
5. As a single Vault user, I want the plugin to show whether the Runtime is starting, connected, interrupted, or unavailable, so that local failures are understandable.
6. As a single Vault user, I want the Runtime to stop safely when Obsidian closes, so that no orphan process remains on my machine.
7. As a single Vault user, I want active work checkpointed before shutdown, so that closing Obsidian does not discard meaningful progress.
8. As a single Vault user, I want OfferAgent to be desktop-only, so that its process and filesystem assumptions are explicit.
9. As a single Vault user, I want to create a new Conversation, so that unrelated goals do not share one history.
10. As a single Vault user, I want to reopen an earlier Conversation, so that long-running study work remains continuous across restarts.
11. As a single Vault user, I want Conversation history restored automatically, so that I do not have to reconstruct prior context.
12. As a single Vault user, I want each request represented as a distinct Agent Run, so that completion, interruption, and failure are unambiguous.
13. As a single Vault user, I want model output streamed into the current Conversation, so that the Agent feels responsive.
14. As a single Vault user, I want to stop an active Agent Run, so that I remain in control of model usage and changes.
15. As a single Vault user, I want an Interrupted Run to remain visible in its original Conversation, so that interruption does not look like failure or deletion.
16. As a single Vault user, I want to resume an Interrupted Run explicitly, so that OfferAgent never consumes model capacity or repeats work merely because Obsidian restarted.
17. As a single Vault user, I want a resumed run to continue from its last durable Run Checkpoint, so that completed tool side effects are not repeated.
18. As a single Vault user, I want unfinished streaming text treated as non-durable, so that a resumed run cannot rely on a partial model response.
19. As a single Vault user, I want Apply or Reject on a pending Vault Change Batch to resume the same Agent Run automatically, so that I do not need a second Resume action.
20. As a single Vault user, I want OfferAgent to use my existing Codex login, so that the initial version does not require an OpenAI API key.
21. As a single Vault user, I want the Codex subscription integration isolated behind a Model Provider, so that backend incompatibility can be repaired without rewriting the Agent.
22. As a single Vault user, I want to select an available model, so that I can choose the appropriate capability and speed.
23. As a single Vault user, I want a Fast Mode setting when the Provider supports it, so that I can optimize interactive work.
24. As a single Vault user, I want Provider authentication errors shown clearly, so that I can reauthenticate instead of receiving a generic Agent failure.
25. As a single Vault user, I want the Agent loop implemented by OfferAgent itself, so that Codex Agent behavior does not replace my designed Agent.
26. As a single Vault user, I want one unified native tool-calling loop, so that planning, tool execution, and final response share one coherent Agent Run.
27. As a single Vault user, I want the Agent to load the Vault's Agent Contract before acting, so that durable Vault-specific rules govern every run.
28. As a single Vault user, I want Agent Contract instructions to override Local Skills and model defaults, so that instruction priority is predictable.
29. As a single Vault user, I want Local Skills to provide workflows without becoming sub-agents, so that the product remains a single-Agent system.
30. As a single Vault user, I want Local Skills to be unable to grant new tools or permissions, so that instructions cannot bypass plugin policy.
31. As a single Vault user, I want the Agent to list eligible Vault content, so that it can discover relevant notes without unrestricted filesystem access.
32. As a single Vault user, I want keyword and exact-phrase Vault search, so that the Agent can locate relevant notes without embeddings or a vector database.
33. As a single Vault user, I want file-name, path, metadata, tag, and body matches ranked sensibly, so that likely notes appear first.
34. As a single Vault user, I want search results bounded by result and snippet limits, so that unrelated Vault content is not sent to the Runtime.
35. As a single Vault user, I want the Agent to read exact lines before treating a search match as Study Evidence, so that search snippets are not mistaken for verified evidence.
36. As a single Vault user, I want every Evidence Snapshot to include source path, line range, version, and content hash, so that reasoning is traceable.
37. As a single Vault user, I want changed source files to invalidate dependent checkpoints, so that resumed work never silently uses stale evidence.
38. As a single Vault user, I want Runtime State to store only bounded Evidence Snapshots, so that complete Vault files are not duplicated into the local database.
39. As a single Vault user, I want hidden control directories excluded from normal search, so that configuration content is not mixed into ordinary note discovery.
40. As a single Vault user, I want the Agent Contract loaded through a dedicated path rather than normal search, so that it is always applied intentionally.
41. As a single Vault user, I want Local Skill resources readable only from within their owning Skill directory, so that referenced resources work without path traversal.
42. As a single Vault user, I want direct web-page reading for URLs I provide, so that the Agent can inspect known sources without a search provider.
43. As a single Vault user, I want hosted web search when the current Provider actually supports it, so that the Agent can find current external information.
44. As a single Vault user, I want hosted web search capability probed at runtime, so that the Agent does not assume an undocumented backend feature exists.
45. As a single Vault user, I want unsupported hosted web search to degrade gracefully, so that one capability error does not fail the whole Agent Run.
46. As a single Vault user, I want web citations rendered as clickable sources, so that I can verify the Agent's external claims.
47. As a single Vault user, I want the Agent to propose a Vault Change Batch rather than write directly, so that all mutations pass through one controlled path.
48. As a single Vault user, I want one logical task represented by one atomic Vault Change Batch, so that related learning-state edits cannot be partially applied.
49. As a single Vault user, I want each batch limited to create, append, and exact replace operations, so that the initial write surface remains understandable and reversible.
50. As a single Vault user, I want all source versions validated before any action is applied, so that concurrent note edits do not produce mixed state.
51. As a single Vault user, I want a stale batch rejected as a whole, so that the Agent must reread evidence and replan instead of forcing old edits.
52. As a single Vault user, I want Trusted Vault mode for validated normal-note changes, so that routine maintenance can complete without repetitive confirmations.
53. As a single Vault user, I want Ask Every Time mode, so that I can require approval for every Vault Change Batch.
54. As a single Vault user, I want Read Only mode, so that I can prevent all Vault mutation while retaining conversation and research.
55. As a single Vault user, I want permission mode stored per Vault, so that authorization follows the Vault rather than a Conversation.
56. As a single Vault user, I want control-file changes to require explicit confirmation in every permission mode, so that the Agent cannot silently rewrite its own rules or plugin configuration.
57. As a single Vault user, I want the Runtime and Agent unable to change Vault Permission Mode, so that authorization remains owned by the plugin and me.
58. As a single Vault user, I want the Runtime unable to access files outside the Vault, so that model-directed tools stay within the product scope.
59. As a single Vault user, I want no Shell or arbitrary code-execution tool, so that a prompt cannot become general machine access.
60. As a single Vault user, I want a Git Checkpoint created before each applied batch, so that accepted and auto-applied changes are recoverable.
61. As a single Vault user, I want Git recovery to avoid branch switches and the existing staging area, so that OfferAgent does not disturb my Git workflow.
62. As a single Vault user, I want one-click undo when target files still match the applied result, so that I can safely reverse an Agent change.
63. As a single Vault user, I want a conflict diff instead of an overwrite when files changed after application, so that later manual work is preserved.
64. As a single Vault user, I want Git Checkpoint retention bounded by age and count, so that recovery data does not grow without limit.
65. As a single Vault user, I want Conversation and Agent Run data stored outside the Vault, so that internal runtime records do not pollute my notes.
66. As a single Vault user, I want database schema migrations to be explicit, so that upgrades preserve durable Conversation and Run state.
67. As a single Vault user, I want the sidebar to emphasize messages over execution internals, so that it remains pleasant for daily use.
68. As a single Vault user, I want ordinary tool calls shown as compact collapsed rows, so that detailed execution remains available without dominating the conversation.
69. As a single Vault user, I want Vault Change Batches to be the only strongly emphasized inline tool card, so that meaningful decisions attract attention.
70. As a single Vault user, I want user messages compact and Agent responses full width, so that long study answers remain readable.
71. As a single Vault user, I want the composer to contain context chips, input, model, permission state, and the current primary action, so that controls remain consolidated.
72. As a single Vault user, I want the interface to inherit Obsidian theme variables, so that it looks correct in light, dark, and custom themes.
73. As a single Vault user, I want common settings visible and advanced diagnostics collapsed, so that configuration is useful without becoming a dashboard.
74. As a single Vault user, I want recent changes and undo reachable contextually, so that recovery is available without a permanent task center.
75. As a single Vault user, I want loopback transport authenticated with a one-time token, so that another local page or process cannot casually control my Runtime.
76. As a single Vault user, I want reconnectable ordered events with acknowledgement and deduplication, so that temporary disconnections do not duplicate tool results.
77. As a single Vault user, I want logs and diagnostics to omit model credentials and complete Vault contents, so that troubleshooting does not create a new data leak.
78. As a single Vault user, I want the old Agent Contract reviewed before migration, so that legacy tool names and behavior do not silently govern the new Agent.
79. As a single Vault user, I want the legacy Obsidian CLI Skill rewritten to use OfferAgent Vault tools, so that production workflows do not depend on unavailable shell commands.
80. As a maintainer, I want the legacy Python, Django, Khoj, web UI, and old plugin implementations removed during the rewrite, so that the new product does not carry parallel compatibility layers.
81. As a maintainer, I want the plugin, Runtime, and protocol built from one TypeScript workspace, so that shared message contracts remain synchronized.
82. As a maintainer, I want the packaged Runtime shipped with the local plugin installation, so that deployment is repeatable for the target Vault.

## Implementation Decisions

- OfferAgent is a single-user local product implemented as two cooperating processes: an Obsidian desktop plugin and a plugin-owned headless TypeScript Runtime.
- The plugin starts the Runtime with the installed Node.js executable, supplies a random loopback port and one-time token, performs health checks, and requests graceful shutdown.
- The Runtime exits when the parent plugin disappears. The first release does not install a Windows Service, scheduled task, or login startup entry.
- The plugin and Runtime communicate through small loopback HTTP management endpoints and one long-lived bidirectional WebSocket.
- Every protocol message carries a protocol version, event identifier, Conversation identifier, Agent Run identifier, and monotonically increasing sequence. Durable events are acknowledged and replayed from the last acknowledged sequence after reconnect.
- Protocol consumers deduplicate repeated events and tool requests using stable event and idempotency identifiers.
- The Runtime owns Agent execution, model interaction, tool selection, Run Checkpoints, and Runtime State. It does not own Vault access or write authorization.
- The plugin owns Vault capabilities, Vault Permission Mode, Vault Change Batch validation and application, Git recovery, and user confirmation.
- The Agent is a single self-designed state machine. No Codex Agent, Agents SDK, sub-agent runtime, or legacy planner/final dual-agent architecture is used.
- The unified Agent loop repeatedly obtains a Provider response, executes requested local or Provider-hosted tools, appends typed results, checkpoints committed steps, and continues until a final response or terminal state.
- Agent Run states include idle, running, waiting for a tool, waiting for confirmation, interrupted, completed, failed, and cancelled. Only an explicit user Resume transitions an Interrupted Run back to running.
- Applying or rejecting a pending Vault Change Batch supplies the outstanding tool result and resumes the same Agent Run automatically.
- Model integration sits behind a small Model Provider interface covering model discovery, streaming responses, local Function Tools, Provider-hosted tools, authentication status, and capability discovery.
- The first adapter is Codex Subscription Provider. It uses the user's existing local Codex OAuth state and private subscription backend rather than an OpenAI API key.
- Codex authentication material is never copied into SQLite, the Vault, logs, protocol events, or plugin settings.
- The private subscription backend is treated as replaceable and compatibility-sensitive. Backend-specific request encoding, token refresh, and response parsing stay inside the adapter.
- Provider tools and local tools use a discriminated contract. Local Function Tools are executed by OfferAgent; Provider-hosted tools are executed by the Provider.
- Hosted Web Search is exposed only after a capability probe succeeds for the current backend and model. Unsupported-tool errors cause one retry without Hosted Web Search and cache the capability as unavailable.
- Direct URL retrieval remains available through `web_read` even when Hosted Web Search is unavailable.
- Web Search results retain source metadata and URL citations as structured events that the Agent Sidebar renders as clickable links.
- The Agent loads the root Agent Contract before normal work. Instruction precedence is Agent Contract, then requested Local Skill, then model defaults.
- Local Skills are instruction packages, not Agents, plugins, or permission grants. `skill_read` allows only known Skill files and directly referenced resources whose resolved paths remain inside the owning Skill directory.
- Vault capabilities are implemented in the plugin through the official Obsidian TypeScript interfaces. The Runtime does not call Obsidian CLI and does not access the Vault through direct filesystem operations.
- Initial local tools are `vault_list`, `vault_search`, `vault_read`, `vault_propose_changes`, `skill_read`, and `web_read`.
- The first release has no Shell, arbitrary code execution, delete, move, direct write, OpenKB, embedding, semantic retrieval, or vector database tool.
- `vault_search` performs on-demand keyword and exact-phrase search. It ranks path and filename matches before title, frontmatter and tags, then body matches.
- Search returns bounded candidates and bounded context snippets. Exact evidence requires a subsequent `vault_read`.
- Normal search excludes hidden control directories and dependency folders. Agent Contract and Local Skill loading use dedicated flows.
- Evidence Snapshots contain only the bounded lines actually read plus source identity, line range, modified version, and hash. Complete Vault files and unused candidates are not persisted in Runtime State.
- A changed evidence source invalidates dependent resumable work. The Agent must reread and replan rather than silently reuse conclusions derived from stale content.
- Runtime State is stored in a local SQLite database owned exclusively by the Runtime. The schema includes Conversations, Messages, Agent Runs, Run Checkpoints, Tool Calls, Evidence Snapshots, Vault Change Batches, Provider Capabilities, durable events, and settings metadata.
- SQLite schema versions and migrations are explicit. Deleting a Conversation cascades through its Runs, checkpoints, evidence, pending batches, and event history.
- `vault_propose_changes` returns a Vault Change Batch containing one logical task and one or more typed Vault Actions. Initial operations are create, append, and exact replace.
- A Vault Change Batch is all-or-nothing. The plugin validates every path, operation, size limit, source version, and expected content before applying any action.
- A validation failure marks the entire batch stale, applies nothing, and returns a typed result requiring the Agent to reread and replan.
- The plugin stores Vault Permission Mode per Vault. Modes are Trusted Vault, Ask Every Time, and Read Only; Trusted Vault is the initial default for the target user.
- Trusted Vault automatically applies validated normal-content batches. Ask Every Time always requests confirmation. Read Only rejects all mutation requests.
- Agent Contract, Local Skill control content, and Obsidian configuration are control files and always require explicit confirmation, regardless of permission mode.
- Permission policy is enforced in plugin code. Prompts, Local Skills, Runtime settings, and model output cannot alter or bypass it.
- Before any approved or automatically applied batch, the recovery module creates a hidden Git Checkpoint containing only the target files' pre-change state through a temporary Git index.
- Git Checkpoint creation does not switch branches, alter the existing index, commit to the current branch, stage unrelated files, or expose Git as an Agent tool.
- The Runtime journal records the batch identifier, target paths, before and after hashes, Git reference, and transaction state. Git stores recoverable file content; SQLite stores the recovery decision state.
- Direct undo is allowed only while current targets match their post-application hashes. Otherwise the plugin shows a conflict diff and preserves later user edits.
- Git Checkpoints are retained for thirty days or the most recent one hundred batches, whichever limit is reached first.
- The first release has one Agent Sidebar rather than tabs, task boards, dashboards, or a separate run center.
- The sidebar contains a compact header, one scrolling Conversation, asymmetric user and Agent messages, collapsed tool activity, contextual Vault Change Batch cards, and one bottom composer.
- Common settings show Runtime and Provider status, model selection, Fast Mode when available, and Vault Permission Mode. Hosted Web Search probing, Git retention, and diagnostics are advanced settings.
- Neutral surfaces use Obsidian theme variables. A restrained OfferAgent accent identifies active state, while semantic colors are reserved for completion, error, and confirmation states.
- The plugin package includes the compiled plugin entry, manifest, styles, and a bundled Runtime script. Deployment copies this package into the target Vault's community plugin installation area.
- The rewrite directly removes the legacy Python, Django, Khoj, web application, and old plugin code when implementation begins. It does not add a compatibility layer or use the legacy code as the new runtime architecture.
- Before enabling the new Agent for real Vault work, migration updates the existing Agent Contract and the Vault-local Obsidian CLI Skill to the new tool, permission, confirmation, and resume semantics.

## Testing Decisions

- Tests assert external behavior at module interfaces and protocol seams. They must not assert private class layout, internal helper calls, SQL statement shape, or renderer implementation details.
- The primary black-box seam is the versioned local Runtime protocol. A test harness starts the real Runtime with a deterministic fake Model Provider and a simulated plugin peer, then drives Conversation creation, Agent Runs, tool calls, streaming, acknowledgement, reconnect, interruption, resume, cancellation, and final responses through HTTP and WebSocket only.
- The second required seam is the Vault Change Coordinator interface because Vault mutation, permission enforcement, and Git recovery are plugin-owned side effects that cannot be safely hidden behind the Runtime protocol. Tests submit complete batches and observe validated results, resulting Vault content, permission decisions, and recovery behavior.
- The third seam is the Agent Sidebar controller/view-model. Tests feed typed protocol events and assert user-visible state, available actions, collapsed activity summaries, confirmation cards, citations, interruption prompts, and status messages without inspecting DOM implementation internals.
- These seams reflect responsibilities explicitly accepted during the design session and do not require another design interview.
- Model Provider contract tests run the same behavior suite against a deterministic fake adapter and recorded backend response fixtures. Live Codex subscription smoke tests are opt-in and never run in default CI because they require personal authentication and an unstable private backend.
- Agent Runtime tests verify one unified tool loop, correct typed tool routing, maximum-step handling, final response completion, failure propagation, cancellation, and checkpoint placement.
- Resume tests crash or disconnect after every durable transition and verify that committed tool side effects are not repeated, incomplete model streams are rerun, pending confirmations survive, and stale Evidence Snapshots cause replanning.
- Protocol tests inject duplicate, delayed, out-of-order, and reconnect-replayed events and verify sequence enforcement, acknowledgement, deduplication, and version mismatch diagnostics.
- Local authentication tests verify loopback binding, one-time token rejection, token rotation on restart, and refusal of unauthenticated HTTP and WebSocket clients.
- Vault Tool Adapter tests use representative Markdown, frontmatter, tags, links, hidden directories, control files, and large notes to verify bounded output and scope rules.
- Search tests assert ranking tiers, exact phrase matching, default and maximum result limits, snippet limits, excluded paths, stable line numbers, and the rule that search results alone do not become Evidence Snapshots.
- Skill tests cover valid direct resource references, missing resources, absolute paths, parent traversal, symlink or normalization escape attempts, and attempts to read another Skill's directory.
- Evidence tests verify bounded storage, path and line attribution, hash recording, invalidation on source changes, and cascading cleanup when a Conversation is deleted.
- Vault Change Batch tests cover create, append, exact replace, multiple-file atomicity, stale source hashes, invalid paths, unsupported operations, size limits, and rollback after a mid-apply failure.
- Permission tests form a matrix across Trusted Vault, Ask Every Time, and Read Only, including normal notes, control files, Vault-external paths, delete or move attempts, and Runtime attempts to alter permissions.
- Git integration tests use temporary repositories containing unstaged and staged user changes. They verify temporary-index isolation, hidden checkpoint creation, no branch switch, no index mutation, clean undo, conflict detection, crash recovery from applying state, and retention cleanup.
- SQLite tests verify schema migration, transactionality, cascade deletion, checkpoint durability, concurrent reconnect reads, and the absence of credentials or complete Vault files.
- Hosted Web Search tests cover available, unavailable, and unknown capability states; successful citations; unsupported-tool fallback; reprobe; and preservation of `web_read` when search is unavailable.
- UI behavior tests cover light and dark themes, narrow sidebar widths, long messages, long file names, keyboard operation of expandable tool rows, visible focus, accessible labels, and clickable citations.
- Manual acceptance testing in the target Vault verifies plugin installation, Runtime discovery on Windows, existing Codex authentication, representative Vault search, normal Trusted Vault auto-apply, forced control-file confirmation, Git undo, restart recovery, and an interrupted run resumed by explicit user action.
- Existing agent and conversation tests are prior art for intent, but the TypeScript rewrite replaces rather than ports tests that assert legacy Khoj, Django, planner JSON, or multi-user behavior.

## Out of Scope

- Multi-user accounts, remote hosting, LAN access, cloud synchronization, or a shared Runtime.
- A Windows Service, background daemon after Obsidian closes, startup task, scheduler, or execution while the plugin is disconnected.
- Codex Agent, Claude Agent SDK, OpenAI Agents SDK, sub-agents, multi-agent coordination, or an agent marketplace.
- Multiple Provider management in the first release, even though the Model Provider seam permits later adapters.
- OpenAI API-key billing flows in the first release.
- Shell access, arbitrary code execution, general filesystem access, MCP management, and external command execution.
- Delete and move Vault operations.
- OpenKB, embeddings, vector search, semantic indexing, persistent full-text indexes, and knowledge-base administration.
- A task center, dashboard, calendar, multi-tab chat UI, inline note editing, Plan Mode switch, or permanent approval queue.
- Partial selection within a Vault Change Batch.
- Automatic resolution of undo conflicts by overwriting later manual edits.
- Automatic modification of the target Vault before the migration and acceptance stage.
- Compatibility with the legacy Python, Khoj, Django, web UI, or old Obsidian plugin implementation.
- A guarantee that the private Codex subscription backend will remain stable or that subscription usage has a particular billing treatment.

## Further Notes

- The accepted architecture is captured by the existing ADR sequence covering the two-process local product, Runtime ownership, connected-plugin requirement, durable resume, SQLite, bounded evidence, local protocol, child-process lifecycle, atomic changes, permissions, Git recovery, keyword search, hosted web-search probing, and the single minimal Agent Sidebar.
- Claudian is a visual and interaction reference only. OfferAgent does not adopt Claudian's agent architecture, CLI execution model, multi-provider UI, multi-tab model, MCP surface, or inline-edit scope.
- The current target Vault already uses Git and may contain unrelated dirty work. Implementation and acceptance must preserve the current branch, worktree changes, and staging area.
- The current code is logically single-agent but separates planning and final model calls. The rewrite intentionally replaces that shape with one native tool-calling Agent loop rather than translating the legacy planner prompt.
- The target Agent Contract and Vault-local Obsidian CLI Skill remain migration requirements and are not modified by this specification step.
