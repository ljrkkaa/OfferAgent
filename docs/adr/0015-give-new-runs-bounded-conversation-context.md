---
status: accepted
---

# Give new runs bounded Conversation Context

Every new Agent Run receives the ordered user and Agent messages from its Conversation so that follow-ups such as “continue” or “use that plan” retain their meaning. When the Provider context limit is approached, OfferAgent removes the oldest complete turns first and always retains the current user message; v1 does not generate a lossy model summary.

## Consequences

- Prior tool calls, tool results, and Evidence Snapshots are not inherited across Agent Runs.
- A new Run must reread Vault sources before relying on them for a current action.
- Provider adapters must represent both user and Agent messages, not only the current user message.
