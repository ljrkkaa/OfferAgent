---
status: accepted
---

# Keep Project Interview Training in Conversation until confirmed

Project Interview Training is a multi-turn behavior of the single Python Agent Loop, not a second workflow engine or
durable training state machine. The current Conversation retains the question, user answer, factual challenge,
technical follow-up, feedback, and refined answer. The Agent selects an explicit user question when present; otherwise
it semantically combines the target role, recent Interview Questions, registered-project risks, Feedback Memory, and
retraining needs. It asks exactly one question per user turn.

Only a `projects/index.md` registration authorizes Project Ownership. Every factual challenge and Project Answer uses
bounded `project.search` followed by version-bound `project.read`; missing, excluded, stale, or contradictory evidence
stops the corresponding first-person claim. Project Answers remain separate from general Interview Question answers.

No training transcript enters the Vault. After the user explicitly confirms the refined Training Outcome, one
`vault.changes.apply` batch updates the project's stable profile, answer index, and only the answer that was actually
trained. The profile retains stable facts, weak points, likely follow-ups, and retraining items. Cancellation,
interruption, replay before confirmation, or a discussion-only turn performs no write.

## Consequences

- Conversation replay is the training continuity mechanism; there is no duplicate Runtime store or training cursor.
- Evidence citations and gaps survive in the saved Project Answer without inventing responsibilities, metrics, or
  production outcomes.
- Existing Vault confirmation, hash binding, journal recovery, idempotency, and guarded undo apply to the complete
  Training Outcome.
- Feedback stays dimensional and actionable without a numeric total score.

## Alternatives considered

- A fixed question sequence or scorecard workflow was rejected because it ignores current goals and creates a second
  planner.
- Saving every answer turn was rejected because it would archive the transcript and persist unconfirmed claims.
- Generating a complete answer library up front was rejected because only actually trained and confirmed questions
  have a valid Training Outcome.
