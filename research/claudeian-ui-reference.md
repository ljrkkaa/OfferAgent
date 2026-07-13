# Claudian UI reference for OfferAgent

Research snapshot: 2026-07-14, official repository commit [`58fe976`](https://github.com/YishenTu/claudian/tree/58fe97602e84c70d53f423abdad7137a4122ae87). Only first-party sources are used.

## Exact identification

The project the user called “claudeian” is **Claudian**, by Yishen Tu. Its Obsidian plugin ID is `realclaudian`; the official manifest describes it as desktop-only. Sources: [official repository/README](https://github.com/YishenTu/claudian/blob/58fe97602e84c70d53f423abdad7137a4122ae87/README.md#L1-L34), [official manifest](https://github.com/YishenTu/claudian/blob/58fe97602e84c70d53f423abdad7137a4122ae87/manifest.json).

## Source facts

1. **The core surface is an Obsidian chat sidebar.** It opens from the ribbon or command palette. Claudian also supports inline edit, commands/skills, mentions, plan mode, conversation history and resume, but these are separate capabilities rather than prerequisites for the sidebar. [README features](https://github.com/YishenTu/claudian/blob/58fe97602e84c70d53f423abdad7137a4122ae87/README.md#L11-L27)

2. **The visual hierarchy is deliberately quiet.** The official preview uses a slim branded header, a single scrolling conversation column, inline work events, and a persistent composer at the bottom; it avoids a dashboard or card grid. [Official preview image](https://github.com/YishenTu/claudian/blob/58fe97602e84c70d53f423abdad7137a4122ae87/assets/Preview.png)

3. **User and assistant messages are visually asymmetric.** The user message is a compact right-aligned bubble, while the assistant response is transparent and full width. The message stream uses modest 12 px gaps and inherits Obsidian typography/colors. [Message styles](https://github.com/YishenTu/claudian/blob/58fe97602e84c70d53f423abdad7137a4122ae87/src/style/components/messages.css#L11-L79)

4. **Tool activity is rendered as compact, expandable rows.** Each row has an icon, tool name, truncated summary and status; details are hidden by default and reveal beneath a subtle left border. Running/completed/error/blocked states use semantic colors. The collapse interaction supports click, Enter/Space and `aria-expanded`. [Tool-call styles](https://github.com/YishenTu/claudian/blob/58fe97602e84c70d53f423abdad7137a4122ae87/src/style/components/toolcalls.css#L1-L124), [tool renderer](https://github.com/YishenTu/claudian/blob/58fe97602e84c70d53f423abdad7137a4122ae87/src/features/chat/rendering/ToolCallRenderer.ts#L835-L866), [collapse behavior](https://github.com/YishenTu/claudian/blob/58fe97602e84c70d53f423abdad7137a4122ae87/src/features/chat/rendering/collapsible.ts#L1-L84)

5. **The composer is one bordered surface, not a stack of panels.** It contains the textarea and a compact toolbar; attached context appears as removable 24 px pill chips above the text. Controls are mostly transparent until hover and use Obsidian theme variables. [Composer styles](https://github.com/YishenTu/claudian/blob/58fe97602e84c70d53f423abdad7137a4122ae87/src/style/components/input.css#L17-L128), [context-chip styles](https://github.com/YishenTu/claudian/blob/58fe97602e84c70d53f423abdad7137a4122ae87/src/style/components/context-tray.css#L1-L38)

6. **Conversation operations are compact icon actions.** The current implementation provides new tab, new conversation and history controls around the active composer/navigation area; history is a dropdown rather than a permanent second pane. [View construction](https://github.com/YishenTu/claudian/blob/58fe97602e84c70d53f423abdad7137a4122ae87/src/features/chat/ClaudianView.ts#L289-L369)

7. **The project maintains a small brand accent while delegating the rest to Obsidian.** Claudian defines one active-provider accent, then relies on `--text-*`, `--background-*` and other Obsidian variables for most surfaces. [Color tokens](https://github.com/YishenTu/claudian/blob/58fe97602e84c70d53f423abdad7137a4122ae87/src/style/base/variables.css#L1-L46), [header styles](https://github.com/YishenTu/claudian/blob/58fe97602e84c70d53f423abdad7137a4122ae87/src/style/components/header.css#L1-L27)

## Design inference for OfferAgent

These are recommendations derived from the sources above, not claims about Claudian:

- Use **one right sidebar only**: small `OfferAgent` header with a warm accent mark, status dot, history icon and new-conversation icon; no separate task center.
- Keep the body to one conversation timeline. Use a right-aligned user bubble and full-width transparent Agent responses so long study answers remain readable.
- Render `vault_search`, `vault_read`, `skill_read`, `web_read` and provider web search as one-line, collapsed work events. Show only verb + target + state; reveal evidence/details on demand.
- Treat a `Vault Change Batch` as the one exceptional inline card: summary, affected files, validation state, and Apply/Reject only when confirmation is required. Do not introduce a global approvals screen.
- Put context chips, textarea, model selector, permission indicator and Send/Stop inside one bottom composer. When interrupted, replace the normal secondary action with a clearly accented **Resume** action; keep the conversation in place.
- Copy the restraint, not the feature count: omit Claudian-style multi-tab UI, MCP controls, slash-command chrome, plan-mode toggles, inline editor and dashboard for v1. OfferAgent’s existing single-agent and per-Vault permission model makes those controls unnecessary.
- Use Obsidian theme tokens for all neutral surfaces and one restrained warm accent for active/running states. Reserve green/red/orange for completed/error/waiting-for-confirmation so status meaning stays stronger than branding.

## Minimal sidebar anatomy

```text
┌ OfferAgent              ●  history  new ┐
│                                            │
│ user bubble                                │
│                                            │
│ Agent answer, full width                   │
│  ▸ Search  interview/…              ✓      │
│  ▸ Read    daily/2026-07-14.md       ✓      │
│                                            │
│ [Change batch: 2 files]     Apply / Reject │
│                                            │
│ [daily note ×]                              │
│ Ask OfferAgent…                             │
│ model        trusted vault          Send   │
└────────────────────────────────────────────┘
```

This preserves Claudian’s strongest interaction pattern—conversation first, execution details progressively disclosed—while matching OfferAgent’s simpler single-user, single-agent scope.
