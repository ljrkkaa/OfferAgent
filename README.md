# OfferAgent

OfferAgent is a single-user, local interview study agent delivered as a desktop-only Obsidian plugin. The Vault remains the source of truth. A plugin-owned, headless TypeScript Runtime handles agent execution without receiving direct filesystem authority over the Vault.

The current implementation includes the product shell, durable Conversations, and a unified Agent Run loop:

- one TypeScript workspace for the shared protocol, Runtime, and Obsidian plugin;
- one minimal OfferAgent right sidebar;
- a bundled loopback-only Runtime with one-time-token authentication;
- hidden child-process startup on Windows, health monitoring, parent-loss detection, and graceful shutdown;
- actionable Node.js discovery diagnostics.
- a Runtime-owned Model Provider seam with deterministic test and Codex Subscription adapters;
- model selection plus streamed, single-run conversation in the Sidebar;
- typed auth, model, transport, and Provider failures over a versioned WebSocket protocol.
- durable Conversations, Messages, Agent Runs, and schema metadata in Runtime-owned SQLite;
- persisted Run-boundary events with stable identities, client acknowledgements, and reconnect replay;
- Conversation create/switch/reopen/delete controls plus visible Run status and cancellation.
- local `vault_list`, `vault_search`, and `vault_read` Function Tools executed only by the Obsidian plugin;
- on-demand keyword and exact-phrase search with path-first ranking and bounded candidate snippets;
- a mandatory root `agent.md` contract gate before each Agent Run's first Provider step, with Contract > requested Local Skill > model-default precedence;
- bounded `skill_read` access to registered `.codex/skills/*/SKILL.md` instructions and only their directly referenced in-skill resources;
- canonical-path containment for Agent Contract and Local Skill control files, with no Skill authority to add tools, permissions, or sub-agents;
- bounded Evidence Snapshots with source path, exact lines, modified version, SHA-256 hash, and stale-source invalidation;
- one confirmed, all-or-nothing `vault_propose_changes` batch for bounded create, append, and exact-replace operations;
- full-batch validation before confirmation and again before apply, with typed Apply all / Reject all results in the same Agent Run;
- hidden Git checkpoints created through an isolated temporary index without changing HEAD, the current branch, or the user's index;
- hash-guarded undo that refuses to overwrite later edits;
- compact expandable Vault tool activity in the Agent Sidebar.

The Codex adapter reuses the login cache managed by Codex CLI/desktop. It does not require an
OpenAI API key and never copies OAuth material into plugin settings or protocol events. Remaining
v1 capabilities are tracked in the subsequent GitHub tickets and are intentionally not exposed early.

## Requirements

- Node.js 20 or newer
- npm
- Obsidian desktop 1.6 or newer for manual plugin use

## Development

Install dependencies:

```powershell
npm.cmd install
```

Run type checking, compile all packages, and produce the installable plugin directory:

```powershell
npm.cmd run build
```

Run the smoke tests:

```powershell
npm.cmd test
```

The default suite uses the deterministic fake Provider. To opt into real subscription-backed
answer streaming and a local Function Tool round trip using the existing local Codex login:

```powershell
npm.cmd run test:live-codex
```

The production plugin package is emitted to `packages/plugin/dist/` and contains:

- `manifest.json`
- `main.js`
- `styles.css`
- `runtime.js`

For a local manual smoke test, copy those files to `.obsidian/plugins/offeragent/` inside a test Vault, enable OfferAgent in Obsidian, and use the ribbon icon or the `Open OfferAgent sidebar` command.

## Architecture

- `packages/protocol`: shared versioned local protocol types.
- `packages/runtime`: plugin-owned headless Runtime process.
- `packages/plugin`: Obsidian entry point, Runtime lifecycle owner, and sidebar controller/view.
- `tests`: black-box Runtime and public sidebar-controller smoke tests.
- `docs/specs`, `docs/adr`, and `CONTEXT.md`: accepted product specification, decisions, and domain language.

The v1 scope explicitly excludes the legacy Python/Django/Khoj server, Web UI, old Obsidian plugin, Shell tools, arbitrary code execution, vector databases, and multi-agent behavior.

Production Runtime State is stored outside the Vault at `%LOCALAPPDATA%\OfferAgent\state.db`.
The Runtime is the database's only owner; the plugin accesses it exclusively through the versioned
local protocol. Fake-Provider tests use isolated in-memory or temporary databases.
