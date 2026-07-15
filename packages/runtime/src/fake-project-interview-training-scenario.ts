import type { VaultAction } from "@offeragent/protocol";
import type { ModelConversationItem, ModelRequest, ModelStreamEvent } from "./model-provider";
import { toolResultFor } from "./fake-provider-conversation";

const PROJECT_ID = "offeragent";
const PROFILE_PATH = "projects/offeragent/profile.md";
const INDEX_PATH = "projects/offeragent/index.md";
const ANSWER_PATH = "projects/offeragent/answers/cache-invalidation.md";

function output(delta: string): ModelStreamEvent {
  return { type: "output_text.delta", delta };
}

function latest<T extends ModelConversationItem>(
  input: ModelConversationItem[],
  predicate: (item: ModelConversationItem) => item is T,
): T | undefined {
  for (let index = input.length - 1; index >= 0; index -= 1) {
    const item = input[index];
    if (predicate(item)) return item;
  }
  return undefined;
}

function exactVaultRead(request: ModelRequest, path: string) {
  return toolResultFor(request.input, "vault_read", (call) =>
    (call.arguments as { path?: unknown }).path === path);
}

function exactProjectEvidence(
  request: ModelRequest,
  nextCallId: (prefix: string) => string,
): ModelStreamEvent | Extract<ModelConversationItem, { type: "local_tool_result" }> {
  const search = toolResultFor(request.input, "project_search");
  if (!search) {
    return {
      type: "local_tool_call",
      callId: nextCallId("fake-project-training-search"),
      name: "project_search",
      arguments: { projectId: PROJECT_ID, query: "stale cache invalidation expected version", limit: 5 },
    };
  }
  if (!search.result.ok || search.result.value.type !== "project_search") {
    return output("Project Interview Training stopped because the registered Project Evidence search failed.");
  }
  const candidate = search.result.value.entries[0];
  if (!candidate) {
    return output("Project Interview Training stopped because the registered project has no exact evidence for this question.");
  }
  const read = toolResultFor(request.input, "project_read", (call) => {
    const arguments_ = call.arguments as { path?: unknown; projectId?: unknown };
    return arguments_.projectId === PROJECT_ID && arguments_.path === candidate.path;
  });
  if (!read) {
    return {
      type: "local_tool_call",
      callId: nextCallId("fake-project-training-read"),
      name: "project_read",
      arguments: { projectId: PROJECT_ID, path: candidate.path, lineStart: 1, lineEnd: 80 },
    };
  }
  return read;
}

function evidenceFrom(
  value: ModelStreamEvent | Extract<ModelConversationItem, { type: "local_tool_result" }>,
) {
  if (value.type !== "local_tool_result") return undefined;
  if (!value.result.ok || value.result.value.type !== "project_read") return undefined;
  return value.result.value;
}

function firstQuestion(
  request: ModelRequest,
  nextCallId: (prefix: string) => string,
  currentUser: string,
): ModelStreamEvent {
  const userSelected = /(?:this exact|this question|use (?:this|the) question)/iu.test(currentUser);
  if (!userSelected) {
    const catalog = toolResultFor(request.input, "interview_catalog");
    if (!catalog) {
      return {
        type: "local_tool_call",
        callId: nextCallId("fake-project-training-catalog"),
        name: "interview_catalog",
        arguments: {
          query: "senior backend cache invalidation project risk recent experience retraining",
          limit: 5,
        },
      };
    }
    if (!catalog.result.ok || catalog.result.value.type !== "interview_catalog") {
      return output("I could not select a grounded training question because Interview Knowledge was unavailable.");
    }
    const selectedExperience = catalog.result.value.experienceCandidates[0];
    if (selectedExperience) {
      const experience = exactVaultRead(request, selectedExperience.path);
      if (!experience) {
        return {
          type: "local_tool_call",
          callId: nextCallId("fake-project-training-experience"),
          name: "vault_read",
          arguments: { path: selectedExperience.path },
        };
      }
      if (!experience.result.ok || experience.result.value.type !== "vault_read") {
        return output("I could not read the selected recent Experience exactly, so I did not invent a training question from its summary.");
      }
    }
  }

  const evidenceResult = exactProjectEvidence(request, nextCallId);
  if (evidenceResult.type !== "local_tool_result") return evidenceResult;
  const evidence = evidenceFrom(evidenceResult);
  if (!evidence || !/expectedVersion/u.test(evidence.content)) {
    return output("I could not support this training question with exact registered Project Evidence.");
  }
  return output(
    "[Project Interview Question]\nHow did you prevent a stale request from invalidating a newer cached value?",
  );
}

function evidenceFollowUp(
  request: ModelRequest,
  nextCallId: (prefix: string) => string,
): ModelStreamEvent {
  const evidenceResult = exactProjectEvidence(request, nextCallId);
  if (evidenceResult.type !== "local_tool_result") return evidenceResult;
  const evidence = evidenceFrom(evidenceResult);
  if (!evidence) return output("I cannot ground a follow-up because exact Project Evidence is unavailable.");
  return output(
    `[Evidence-grounded Follow-up]\nEvidence: the implementation compares the current entry version with the expected version before deletion. [Project Evidence: ${evidence.evidencePath}:${evidence.lineStart}-${evidence.lineEnd}]\nFailure case: what happens when a delayed invalidation arrives after a newer refresh?`,
  );
}

function feedbackAndRefinedAnswer(
  request: ModelRequest,
  nextCallId: (prefix: string) => string,
): ModelStreamEvent {
  const evidenceResult = exactProjectEvidence(request, nextCallId);
  if (evidenceResult.type !== "local_tool_result") return evidenceResult;
  const evidence = evidenceFrom(evidenceResult);
  if (!evidence) return output("I cannot give evidence-grounded feedback because exact Project Evidence is unavailable.");
  const citation = `[Project Evidence: ${evidence.evidencePath}:${evidence.lineStart}-${evidence.lineEnd}]`;
  return output([
    "[Training Feedback]",
    `Evidence: ${citation} supports the version comparison and conditional deletion. It does not provide production metrics or collaboration history.`,
    "- Project facts — covered: the version guard is stated accurately.",
    "- Design and tradeoffs — covered: stale invalidations become no-ops rather than deleting newer values.",
    "- Metrics — needs evidence: cache hit rate and stale-invalidation counts were suggested, not observed.",
    "- Failure cases — covered: delayed invalidation after refresh was identified.",
    "- Implementation — covered: compare current.version with expectedVersion before deletion.",
    "- Ownership — covered by registered-project solo ownership; no unsupported collaboration claim is added.",
    "Coaching suggestion: describe the ordering race first, then the invariant, code-level guard, and evidence gap in that order.",
    "",
    "[Refined Project Answer]",
    `I protected cache invalidation with an expected-version guard. Before deleting, the implementation compares the current entry version with the invalidation's expected version, so a delayed stale request becomes a no-op instead of removing a newer value. ${citation} I can explain the intended monitoring signals, but production latency, hit-rate, and incident outcomes remain evidence gaps.`,
    "",
    "Save this refined Training Outcome to the OfferAgent Project Interview Profile?",
  ].join("\n"));
}

function profileTrainingSection(citation: string): string {
  return [
    "## Training: Cache invalidation under stale requests",
    "",
    "### Stable facts",
    "",
    "- Registered personal project with solo ownership.",
    `- Cache invalidation uses an expected-version guard. ${citation}`,
    "",
    "### Weak points",
    "",
    "- Production latency, hit-rate, stale-invalidation count, and incident outcomes remain evidence gaps.",
    "",
    "### Retraining",
    "",
    "- Rehearse the race, invariant, implementation guard, and metric gap without inventing outcomes.",
  ].join("\n");
}

function profileContent(citation: string, current?: string): string {
  const section = profileTrainingSection(citation);
  if (current === undefined) {
    return [
      "---",
      "title: OfferAgent Project Interview Profile",
      "project-id: offeragent",
      "ownership: solo",
      "---",
      "",
      "# OfferAgent Project Interview Profile",
      "",
      section,
      "",
    ].join("\n");
  }
  const heading = "## Training: Cache invalidation under stale requests";
  const start = current.indexOf(heading);
  if (start < 0) return `${current.trimEnd()}\n\n${section}\n`;
  const nextSection = current.indexOf("\n## ", start + heading.length);
  return nextSection < 0
    ? `${current.slice(0, start)}${section}\n`
    : `${current.slice(0, start)}${section}${current.slice(nextSection)}`;
}

function indexContent(current?: string): string {
  const link = "- [Cache invalidation under stale requests](answers/cache-invalidation.md)";
  if (current !== undefined) {
    if (current.includes(link)) return current;
    return `${current.trimEnd()}\n${link}\n`;
  }
  return [
    "# OfferAgent Project Interview Answers",
    "",
    link,
    "",
  ].join("\n");
}

function answerContent(citation: string): string {
  return [
    "---",
    "title: Cache invalidation under stale requests",
    "project-id: offeragent",
    "type: project-answer",
    "---",
    "",
    "# Cache invalidation under stale requests",
    "",
    "## Training Outcome",
    "",
    "I protected cache invalidation with an expected-version guard. Before deletion, the current entry version is compared with the invalidation's expected version, so delayed stale invalidations become no-ops instead of deleting newer values.",
    "",
    `## Evidence\n\n- ${citation}`,
    "",
    "## Weak points",
    "",
    "- Metrics remain an evidence gap: production latency, hit rate, stale-invalidation count, and incident outcomes are not recorded.",
    "",
    "## Follow-ups and retraining",
    "",
    "- Explain the failure ordering and why the guard preserves the newer value.",
    "- Retrain after adding measured production outcomes.",
    "",
  ].join("\n");
}

function targetContent(
  read: Extract<ModelConversationItem, { type: "local_tool_result" }>,
): string | undefined {
  return read.result.ok && read.result.value.type === "vault_read"
    ? read.result.value.content
    : undefined;
}

function changeAction(
  read: Extract<ModelConversationItem, { type: "local_tool_result" }>,
  path: string,
  content: string,
  actionId: string,
): VaultAction {
  if (read.result.ok && read.result.value.type === "vault_read") {
    return {
      actionId,
      idempotencyKey: actionId,
      operation: "exact_replace",
      path,
      expectedVersion: read.result.value.modifiedVersion,
      expectedContent: read.result.value.content,
      replacement: content,
    };
  }
  return {
    actionId,
    idempotencyKey: actionId,
    operation: "create",
    path,
    expectedVersion: "missing",
    content,
  };
}

function persistConfirmedOutcome(
  request: ModelRequest,
  nextCallId: (prefix: string) => string,
  currentUser: string,
): ModelStreamEvent {
  if (/\b(?:no|decline|reject)\b|do not|don't/iu.test(currentUser)) {
    return output("The refined Training Outcome was not saved. No Vault change was proposed.");
  }
  if (!/\b(?:yes|confirm|save|apply)\b/iu.test(currentUser)) {
    return output("I am still waiting for an explicit save or decline decision; no Vault change was proposed.");
  }
  const evidenceResult = exactProjectEvidence(request, nextCallId);
  if (evidenceResult.type !== "local_tool_result") return evidenceResult;
  const evidence = evidenceFrom(evidenceResult);
  if (!evidence) {
    return output("The Training Outcome was not proposed because its exact Project Evidence could not be revalidated.");
  }
  const targetReads = new Map<string, Extract<ModelConversationItem, { type: "local_tool_result" }>>();
  for (const path of [PROFILE_PATH, INDEX_PATH, ANSWER_PATH]) {
    const targetRead = exactVaultRead(request, path);
    if (!targetRead) {
      return {
        type: "local_tool_call",
        callId: nextCallId("fake-project-training-target"),
        name: "vault_read",
        arguments: { path },
      };
    }
    if (targetRead.result.ok && targetRead.result.value.type !== "vault_read") {
      return output(`The Training Outcome was not proposed because '${path}' returned an invalid exact-read result.`);
    }
    if (!targetRead.result.ok && targetRead.result.error.code !== "not_found") {
      return output(`The Training Outcome was not proposed because '${path}' could not be read safely.`);
    }
    targetReads.set(path, targetRead);
  }
  const proposal = toolResultFor(request.input, "vault_propose_changes");
  if (!proposal) {
    const citation = `[Project Evidence: ${evidence.evidencePath}:${evidence.lineStart}-${evidence.lineEnd}]`;
    const batchId = nextCallId("fake-project-training-proposal");
    const profileRead = targetReads.get(PROFILE_PATH)!;
    const indexRead = targetReads.get(INDEX_PATH)!;
    const answerRead = targetReads.get(ANSWER_PATH)!;
    return {
      type: "local_tool_call",
      callId: batchId,
      name: "vault_propose_changes",
      arguments: {
        batchId,
        idempotencyKey: batchId,
        task: "Persist one user-confirmed OfferAgent Project Interview Training Outcome",
        actions: [
          changeAction(
            profileRead,
            PROFILE_PATH,
            profileContent(citation, targetContent(profileRead)),
            "upsert-offeragent-interview-profile",
          ),
          changeAction(
            indexRead,
            INDEX_PATH,
            indexContent(targetContent(indexRead)),
            "upsert-offeragent-interview-index",
          ),
          changeAction(
            answerRead,
            ANSWER_PATH,
            answerContent(citation),
            "upsert-offeragent-project-answer",
          ),
        ],
      },
    };
  }
  if (proposal.result.ok && proposal.result.value.type === "vault_propose_changes" &&
      proposal.result.value.decision === "applied") {
    return output(`The confirmed Training Outcome was applied atomically under projects/offeragent at checkpoint ${proposal.result.value.checkpointRef}.`);
  }
  return output("The confirmed Training Outcome batch was not applied; no Project Interview Profile content was changed.");
}

export function projectInterviewTrainingEvent(
  request: ModelRequest,
  nextCallId: (prefix: string) => string,
): ModelStreamEvent {
  const currentUser = latest(
    request.input,
    (item): item is Extract<ModelConversationItem, { type: "user_message" }> =>
      item.type === "user_message",
  )?.text ?? "";
  const previousAssistant = latest(
    request.input,
    (item): item is Extract<ModelConversationItem, { type: "assistant_message" }> =>
      item.type === "assistant_message",
  )?.text ?? "";

  if (previousAssistant.includes("[Refined Project Answer]")) {
    return persistConfirmedOutcome(request, nextCallId, currentUser);
  }
  if (previousAssistant.includes("[Evidence-grounded Follow-up]")) {
    return feedbackAndRefinedAnswer(request, nextCallId);
  }
  if (previousAssistant.includes("[Project Interview Question]")) {
    return evidenceFollowUp(request, nextCallId);
  }
  return firstQuestion(request, nextCallId, currentUser);
}
