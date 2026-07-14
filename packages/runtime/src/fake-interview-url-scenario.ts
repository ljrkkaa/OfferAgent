import type { ModelRequest, ModelStreamEvent } from "./model-provider";
import { toolResultFor } from "./fake-provider-conversation";
import {
  experienceIdentity,
  incrementFrequency,
} from "./fake-interview-evidence";
import {
  candidateReadStep,
  interviewOutput as output,
  interviewReadRequest as readRequest,
  interviewReadResultFor as readResultFor,
  matchingQuestionCandidates,
} from "./fake-interview-workflow";

export function interviewUrlIngestionEvent(
  request: ModelRequest,
  nextCallId: (prefix: string) => string,
): ModelStreamEvent {
  const webRead = toolResultFor(request.input, "web_read");
  if (!webRead) {
    return {
      type: "local_tool_call",
      callId: nextCallId("fake-url-web-read"),
      name: "web_read",
      arguments: {
        url: "https://example.com/shared/backend-42",
        maxBytes: 32_768,
      },
    };
  }
  if (!webRead.result.ok || webRead.result.value.type !== "web_read") {
    const errorMessage = webRead.result.ok
      ? undefined
      : webRead.result.error.message
          .replace(/^web_read received /u, "")
          .replace(/ from the page\.$/u, "");
    return webRead.result.ok
      ? output("The supplied interview page returned an invalid read result.")
      : output(`Could not read the supplied interview page: ${errorMessage}`);
  }
  const source = webRead.result.value;
  if (/sign\s*in|log\s*in|access denied/iu.test(source.content) || source.content.length < 120) {
    return output(
      "The supplied page did not contain enough interview evidence; no knowledge changes were proposed.",
    );
  }

  const catalog = toolResultFor(request.input, "interview_catalog");
  if (!catalog) {
    return {
      type: "local_tool_call",
      callId: nextCallId("fake-url-catalog"),
      name: "interview_catalog",
      arguments: {
        query: "Example Corp Backend Engineer distributed cache consistency",
        canonicalUrl: source.finalUrl,
        sourceFingerprint: source.sourceFingerprint,
        limit: 10,
      },
    };
  }
  if (!catalog.result.ok || catalog.result.value.type !== "interview_catalog") {
    return catalog.result.ok
      ? output("The Interview Catalog returned an invalid URL-ingestion result.")
      : output(`Interview Catalog failed: ${catalog.result.error.message}`);
  }

  const catalogValue = catalog.result.value;
  const candidatePaths = [
    ...catalogValue.experienceCandidates.map(({ path }) => path),
    ...catalogValue.questionCandidates.map(({ path }) => path),
  ];
  const candidateStep = candidateReadStep(request.input, candidatePaths, nextCallId);
  if (candidateStep) return candidateStep;

  const experienceEvidence = catalogValue.experienceCandidates.map((candidate) => {
    const candidateRead = readResultFor(request.input, candidate.path);
    if (!candidateRead?.result.ok || candidateRead.result.value.type !== "vault_read") {
      return { candidate };
    }
    return {
      candidate,
      identity: experienceIdentity(candidateRead.result.value.content),
    };
  });
  if (
    experienceEvidence.some(({ identity }) =>
      !identity?.candidate || !identity.date || !identity.round,
    )
  ) {
    return output("Ambiguous identity; no merge or Vault changes proposed.");
  }
  const matchingExperiences = experienceEvidence.filter(({ identity }) =>
    identity?.candidate === "candidate-b" &&
    identity.date === "2026-07-01" &&
    identity.round === "final",
  );
  if (matchingExperiences.length > 1) {
    return output("Ambiguous Interview Experience identity; no Vault changes proposed.");
  }
  if (matchingExperiences.length === 1) {
    const exactSourceMatch = matchingExperiences[0].candidate.matchKinds.includes("canonical-url") ||
      matchingExperiences[0].candidate.matchKinds.includes("source-fingerprint");
    return output(
      exactSourceMatch
        ? "Canonical duplicate Interview Experience suppressed; Question frequency is unchanged."
        : "Semantic repost duplicate Interview Experience suppressed; Question frequency is unchanged.",
    );
  }

  const recurringQuestions = matchingQuestionCandidates(
    request.input,
    catalogValue.questionCandidates,
    "distributedcacheconsistency",
  );
  if (recurringQuestions.length > 1) {
    return output("Ambiguous recurring Question identity; no URL ingestion changes proposed.");
  }
  const recurringQuestion = recurringQuestions[0];
  const experienceIndex = readResultFor(request.input, "experiences/index.md");
  if (!experienceIndex) return readRequest("experiences/index.md", nextCallId);
  if (!experienceIndex.result.ok || experienceIndex.result.value.type !== "vault_read") {
    return experienceIndex.result.ok
      ? output("The Experience index returned an invalid result.")
      : output(`The Experience index failed: ${experienceIndex.result.error.message}`);
  }
  const questionIndexDescriptor = catalogValue.indexes.find(({ kind }) => kind === "question");
  if (!questionIndexDescriptor) {
    return output("The Interview Catalog omitted the Question index.");
  }
  const questionIndex = !recurringQuestion && questionIndexDescriptor.exists
    ? readResultFor(request.input, "interview/index.md")
    : undefined;
  if (!recurringQuestion && questionIndexDescriptor.exists && !questionIndex) {
    return readRequest("interview/index.md", nextCallId);
  }
  if (
    questionIndex &&
    (!questionIndex.result.ok || questionIndex.result.value.type !== "vault_read")
  ) {
    return questionIndex.result.ok
      ? output("The Question index returned an invalid result.")
      : output(`The Question index failed: ${questionIndex.result.error.message}`);
  }
  const questionIndexValue = questionIndex?.result.ok &&
    questionIndex.result.value.type === "vault_read"
    ? questionIndex.result.value
    : undefined;

  const proposalResult = toolResultFor(request.input, "vault_propose_changes");
  if (!proposalResult) {
    const questionRead = recurringQuestion
      ? readResultFor(request.input, recurringQuestion.path)
      : undefined;
    if (
      recurringQuestion &&
      (!questionRead?.result.ok || questionRead.result.value.type !== "vault_read")
    ) {
      return output("Recurring Question evidence is unavailable.");
    }
    const questionReadValue = questionRead?.result.ok &&
      questionRead.result.value.type === "vault_read"
      ? questionRead.result.value
      : undefined;
    const incrementedQuestion = questionReadValue
      ? incrementFrequency(questionReadValue.content)
      : undefined;
    if (recurringQuestion && !incrementedQuestion) {
      return output("Recurring Question frequency is missing or invalid; no URL ingestion proposed.");
    }
    const questionReplacement = incrementedQuestion?.replace(
      /\s*$/u,
      "\n- [[experiences/url-interview]] - candidate-b, 2026-07-01 final round\n",
    );
    const indexReplacement = `${experienceIndex.result.value.content.trimEnd()}\n- [[url-interview]]\n`;
    const callId = nextCallId("fake-url-proposal");
    const batchId = `${callId}-batch`;
    return {
      type: "local_tool_call",
      callId,
      name: "vault_propose_changes",
      arguments: {
        batchId,
        idempotencyKey: batchId,
        task: "Ingest one URL-sourced Interview Experience and synchronize one recurring Question",
        actions: [
          {
            actionId: "create-url-interview",
            idempotencyKey: "create-url-interview",
            operation: "create",
            path: "experiences/url-interview.md",
            expectedVersion: "missing",
            content: `---\ntitle: Example Backend Interview\ntype: interview-experience\ncompany: Example Corp\nposition: Backend Engineer\ncandidate: candidate-b\nround: final\ndate: 2026-07-01\nsource-type: url\nsource-url: ${source.finalUrl}\nsource-fingerprint: ${source.sourceFingerprint}\nsource-title: ${source.sourceTitle ?? "Example Backend Interview"}\n---\n\n# Example Backend Interview\n\n## Summary\n\nCandidate B discussed consistency tradeoffs for distributed caches in an Example Corp backend final round.\n\n## Questions\n\n- [[interview/distributed-cache-consistency]]\n`,
          },
          recurringQuestion
            ? {
                actionId: "merge-recurring-url-question",
                idempotencyKey: "merge-recurring-url-question",
                operation: "exact_replace",
                path: recurringQuestion.path,
                expectedVersion: questionReadValue!.modifiedVersion,
                expectedContent: questionReadValue!.content,
                replacement: questionReplacement!,
              }
            : {
                actionId: "create-url-question",
                idempotencyKey: "create-url-question",
                operation: "create",
                path: "interview/distributed-cache-consistency.md",
                expectedVersion: "missing",
                content: "---\ntitle: Distributed cache consistency\ntype: interview-question\nanswer-state: needs-research\nfrequency: 1\n---\n\n# Distributed cache consistency\n\n## Occurrences\n\n- [[experiences/url-interview]] - candidate-b, 2026-07-01 final round\n",
              },
          {
            actionId: "update-url-experience-index",
            idempotencyKey: "update-url-experience-index",
            operation: "exact_replace",
            path: "experiences/index.md",
            expectedVersion: experienceIndex.result.value.modifiedVersion,
            expectedContent: experienceIndex.result.value.content,
            replacement: indexReplacement,
          },
          ...(!recurringQuestion
            ? [questionIndexDescriptor.exists
                ? {
                    actionId: "update-url-question-index",
                    idempotencyKey: "update-url-question-index",
                    operation: "exact_replace",
                    path: "interview/index.md",
                    expectedVersion: questionIndexValue!.modifiedVersion,
                    expectedContent: questionIndexValue!.content,
                    replacement: `${questionIndexValue!.content.trimEnd()}\n- [[distributed-cache-consistency]]\n`,
                  }
                : {
                    actionId: "create-url-question-index",
                    idempotencyKey: "create-url-question-index",
                    operation: "create",
                    path: "interview/index.md",
                    expectedVersion: "missing",
                    content: "# Interview Questions\n\n- [[distributed-cache-consistency]]\n",
                  }]
            : []),
        ],
      },
    };
  }

  if (!proposalResult.result.ok) {
    return output(`URL ingestion batch failed: ${proposalResult.result.error.message}`);
  }
  return proposalResult.result.value.type === "vault_propose_changes" &&
    proposalResult.result.value.decision === "applied"
    ? output("URL Interview Experience stored and recurring Question synchronized atomically.")
    : output("The URL ingestion batch was rejected.");
}
