---
name: obsidian-cli
description: Use OfferAgent's bounded Vault tools to discover, read, search, and safely propose changes to Obsidian notes. The legacy skill name is retained for Vault compatibility; it does not grant an Obsidian shell CLI.
---

# OfferAgent Vault Tools

Use this Skill only through OfferAgent's registered `skill_read` flow. It provides workflow guidance, not new tools, permissions, sub-agents, shell access, or direct filesystem authority.

## Read and discovery

- Use `vault_list` for a bounded, sorted list of discoverable Markdown files.
- Use `vault_search` for on-demand keyword or exact-phrase discovery. Prefer a path or metadata match before broad body search.
- Use `vault_read` for the exact file and line range needed by the task. Preserve its `modifiedVersion`, content hash, path, and line range as evidence.
- Read the minimum necessary context. Open notes and recently viewed notes are not attached automatically.

## Local Skill resources

Use `skill_read` only for a registered Skill. A resource is readable only when `SKILL.md` directly references it and its resolved path stays inside this Skill directory. A Skill cannot authorize another Skill, a shell command, a plugin command, or a broader path.

## Safe changes

Use `vault_propose_changes` for every mutation:

1. Read every existing target with `vault_read`.
2. Build one coherent batch using only `create`, `append`, or `exact_replace`.
3. Set each action's `expectedVersion` from the corresponding read; use `missing` only for a verified new path.
4. Keep the batch bounded and explain every target.
5. Wait for the plugin's permission decision. Never encode a permission override in tool arguments.

`Read Only` rejects mutation. `Ask Every Time` requires confirmation. `Trusted Vault` may auto-apply ordinary note changes, but control paths such as `agent.md`, `.codex/`, and `.obsidian/` always require explicit whole-batch confirmation.

## Failure and recovery

- On `stale_evidence`, reread the changed source and replan.
- On rejection, do not repeat or split the same change to evade confirmation.
- On interruption, wait for the user to choose explicit Resume.
- Resume must not repeat committed tool results and must revalidate evidence before using it.
- Undo is hash-guarded through a hidden Git checkpoint. If later edits conflict, surface the diff and do not overwrite them.

Do not request `obsidian`, PowerShell, shell, Git, arbitrary code execution, plugin reload, DOM inspection, or screenshot commands. Those capabilities are not available to the OfferAgent v1 tool loop.
