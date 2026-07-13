# OfferAgent

OfferAgent is a single-user, local interview study agent delivered as a desktop-only Obsidian plugin. The Vault remains the source of truth. A plugin-owned, headless TypeScript Runtime handles agent execution without receiving direct filesystem authority over the Vault.

The current implementation is the first product-shell slice:

- one TypeScript workspace for the shared protocol, Runtime, and Obsidian plugin;
- one minimal OfferAgent right sidebar;
- a bundled loopback-only Runtime with one-time-token authentication;
- hidden child-process startup on Windows, health monitoring, parent-loss detection, and graceful shutdown;
- actionable Node.js discovery diagnostics.

Later capabilities are tracked in GitHub Issues #3-#16 and are intentionally not exposed early.

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
