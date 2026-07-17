---
name: obsidian-cli
description: Use OfferAgent's Python-harness Vault tools to discover, read, search, and safely apply bounded Obsidian changes. The legacy name is retained only for Vault compatibility.
---

# OfferAgent Vault Tools

This Skill is guidance for the Python harness. It does not grant an Obsidian CLI, shell, filesystem access, sub-agents, network access, or broader permission.

## Discover and read

- Use `vault.list` for a bounded, sorted file listing.
- Use `vault.search` for path, metadata, keyword, or exact-phrase discovery.
- Use `vault.read` for the exact file and range needed. Retain its version, content hash, path, and range as evidence.
- Search and Catalog summaries are discovery-only. Read every source used for a factual claim, identity decision, or edit.
- Use `agent_contract.read` when the active Contract must be checked, and `skill.read` only for an explicitly requested registered Skill.

## Skill resources

A resource is readable only when this `SKILL.md` directly references it and its resolved path remains inside this Skill directory. A Skill cannot authorize another Skill, a control-file edit, or an unregistered tool.

## Safe changes

Use `vault.changes.apply` for every Vault mutation:

1. Read every existing target with `vault.read`.
2. Verify a new target is missing.
3. Build one coherent, bounded batch with exact expected versions.
4. Explain each target and keep unrelated content unchanged.
5. Wait for the plugin/harness permission result. Never encode a permission override in arguments.

`read-only` rejects mutation. `normal` requires the applicable confirmation. `trusted-workspace` may apply ordinary note changes automatically, but `agent.md`, `.codex/**`, and `.obsidian/**` always require explicit confirmation.

## Failure and recovery

- On `stale_evidence`, reread the changed source and replan.
- On rejection, do not retry or split the same change to evade confirmation.
- On interruption, stop issuing new effects and wait for an explicit Resume path.
- Resume from durable Run events/checkpoints; do not repeat committed tool results.
- If a write outcome is unknown, query durable state or ask the user. Never assume success or failure.
- Undo is hash/version guarded. If later edits conflict, surface the conflict and do not overwrite them.

Do not request `obsidian`, PowerShell, shell, Git, arbitrary code execution, plugin reload, DOM inspection, or screenshot commands through this Skill.
