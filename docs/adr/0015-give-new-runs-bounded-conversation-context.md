---
status: accepted
---

# Give new runs bounded Conversation Context

Every new Agent Run receives the ordered completed user/Agent Turn pairs from its Conversation so that follow-ups such as “continue” or “use that plan” retain their meaning. `Turn.input_blocks` and the completed root `RunState` are the durable message authority; Conversation Attachment claims, order, validated bytes, and deletion lifecycle remain solely owned by the Attachment Store. For each Run, the history adapter uses the immutable catalog model binding to rematerialize retained images into their original USER message. It never copies image bytes into Runtime events, projections, logs, or the Vault.

When the bound model context limit is approached, OfferAgent reserves the current user input, then retains the newest contiguous suffix of complete historical USER/ASSISTANT pairs. A historical Turn is included with all of its ordered images and answer or omitted as a whole. The current image submission is likewise indivisible; if its complete batch exceeds the image count, image byte, text, message, or model-window budget, the Run returns explicit context overflow instead of sending fewer pages. v1 does not generate a lossy model summary.

Selection occurs from durable metadata plus a bounded image-header dimension inspection before attachment bodies are loaded. The remaining image-count, image-byte, and catalog-token allowance is computed after reserving the current submission and base Run context, and only the selected suffix is fully materialized. Store materialization holds one cross-instance SQLite write reservation while it verifies the exact Turn claim sequence, Conversation ownership, metadata, hash, static decode, and decoded dimensions; claim release or Conversation deletion cannot overtake a returned batch. Decoded width, height, and catalog-selected `high`/`original` detail feed a conservative 32-pixel-patch vision-token estimate, so images consume the same catalog-bound model-window budget as text. History preparation has a local timeout, participates in Run cancellation, waits for any cooperative Store worker to exit before returning a timeout, and reports a corrupt image's ordered `imageIndex` without exposing bytes.

## Consequences

- Prior tool calls, tool results, and Evidence Snapshots are not inherited across Agent Runs.
- A new Run must reread Vault sources before relying on them for a current action.
- Provider adapters must represent both user and Agent messages, not only the current user message; images remain legal only on Store-attested USER messages.
- A text-only catalog model rejects retained image history before attachment or inference I/O rather than degrading it to metadata.
- Conversation deletion and startup recovery continue to operate on the same Attachment Store authority; there is no historical-image cache to reconcile.
- Provider context-overflow retry removes at least the oldest complete retained Turn before retrying, so it cannot resend an identical overfull Conversation projection.
