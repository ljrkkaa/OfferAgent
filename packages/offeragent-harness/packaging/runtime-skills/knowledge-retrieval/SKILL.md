---
name: knowledge-retrieval
description: Answer questions from the compiled local LLM Wiki and PageIndex with page-level evidence. Use for Vault knowledge questions, cross-document synthesis, alias or entity lookup, source comparison, and any answer that must cite ingested documents and pages.
allowed-tools: ["grep","read"]
---

# Knowledge Retrieval

Use Wiki pages only for candidate navigation and original evidence pages for every final claim. A grep hit, source metadata, PageIndex summary, or Wiki statement is never proof, including for aliases, titles, and identity lookups.

## Mandatory completion gate

Do not emit `finalResponse` until all three conditions hold:

1. Every cited source has at least one successful `read` whose path contains `/evidence/pages/`.
2. The answer's assertions are present in those read evidence-page lines.
3. Every citation uses the exact final format defined below and names the Vault-relative source path, not a `knowledge/` path.

If any condition is false, continue with `grep` or `read`. Never replace a missing evidence read with model memory or a Wiki citation.

## Workflow

1. Read `knowledge/index.md` to confirm the current revision and available pages.
2. Form a small bounded term set from the user's literal concepts, entities, aliases observed in the index, and explicit source or time constraints. Do not use a fixed domain vocabulary.
3. Grep `knowledge` with an explicit Markdown glob, case policy, and `maxResults` of at most 12. Read the best summary, concept, or entity pages in full. Do not answer from a grep result.
4. Extract only complete structured citations of the form `[source:<sourceId>@<sourceHash>#node:<nodeId>#pages:<start>-<end>]`.
5. For each citation, read `knowledge/sources/<sourceId>.md` and extract its exact `Citation source` value and `Evidence` directory. Treat this generated source projection as the path authority; never reconstruct an object path from a hash.
6. From that Evidence directory, use the sibling `pageindex/nodes.jsonl` and grep the exact node ID. Confirm the node's page range, then read the required `<Evidence>/<page:04d>.md` files. Read adjacent or parent/child pages only when needed to resolve context. Before answering, require at least one successful evidence-page `read` for every cited source.
7. Separate navigation text (an alias, title, or Wiki label used to find the source) from the assertion that the answer must support. Within each source, use the evidence page containing the longest intact assertion span. Aliases and titles still require verification in an evidence page.
8. After drafting the answer, verify every answer assertion against its selected page and rerank the already-retrieved candidates when necessary. Do not cite a higher-ranked page merely because it belongs to the correct source when another candidate page directly contains the answer assertion; answer verification must never introduce a source that retrieval did not return.
9. Copy the source projection's exact `Citation source` value into the final citation; an object path, evidence path, or `knowledge/` path is invalid. Generate the answer solely from verified evidence-page text. Cite each claim as `[source:<Citation source value>; chapter:<PageIndex chapter title>; pages:<start>-<end>]`.

## Answer rules

- For cross-document questions, verify each contributing source independently.
- Present material source conflicts instead of silently selecting one claim.
- If Wiki content lacks an intact object, node, or evidence page, do not cite it.
- If the bounded search finds no sufficient evidence, say that the local knowledge base does not establish the answer. Do not fill the gap with model memory.
- Never cite a `knowledge/` navigation file as the source and never cite a whole chapter range when only one or two evidence pages support the claim.
- Before `finalResponse`, check that every source citation has a preceding successful evidence-page `read` in this Run. If not, continue with tools.
- Never modify `knowledge/` during retrieval and never search raw binary files.
