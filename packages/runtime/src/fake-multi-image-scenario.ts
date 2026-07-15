import type { ModelRequest, ModelStreamEvent } from "./model-provider";
import { frontmatterField, incrementFrequency } from "./fake-interview-evidence";
import { toolResultFor } from "./fake-provider-conversation";
import {
  candidateReadStep,
  interviewOutput as output,
  interviewReadRequest as readRequest,
  interviewReadResultFor as readResultFor,
  matchingQuestionCandidates,
} from "./fake-interview-workflow";

export function multiImageInterviewEvent(
  request: ModelRequest,
  nextCallId: (prefix: string) => string,
): ModelStreamEvent {
  const submission = request.imageSubmission;
  const owningMessage = [...request.input].reverse().find(
    (item) =>
      item.type === "user_message" &&
      item.attachments?.length === submission?.imageCount,
  );
  const images = owningMessage?.type === "user_message"
    ? [...(owningMessage.attachments ?? [])]
        .sort((left, right) => left.order - right.order)
        .map(({ attachmentId }) =>
          request.imageInputs?.find((image) => image.attachmentId === attachmentId)
        )
    : [];
  if (
    images.length < 2 ||
    !submission ||
    submission.imageCount !== images.length ||
    !/^sha256:[a-f0-9]{64}$/u.test(submission.sourceFingerprint) ||
    !owningMessage ||
    owningMessage.type !== "user_message" ||
    images.some((image, order) =>
      !image ||
      image.order !== order ||
      owningMessage.attachments?.[order]?.order !== order ||
      owningMessage.attachments?.[order]?.attachmentId !== image.attachmentId ||
      !image.dataUrl.startsWith(`data:${image.mediaType};base64,`)
    )
  ) {
    return output("The ordered multi-image Interview Submission is invalid.");
  }

  const fingerprint = submission.sourceFingerprint;
  const catalog = toolResultFor(request.input, "interview_catalog");
  if (!catalog) {
    return {
      type: "local_tool_call",
      callId: nextCallId("fake-multi-image-catalog"),
      name: "interview_catalog",
      arguments: {
        query: "backend distributed cache consistency queue backpressure",
        sourceFingerprint: fingerprint,
        limit: 10,
      },
    };
  }
  if (!catalog.result.ok || catalog.result.value.type !== "interview_catalog") {
    return catalog.result.ok
      ? output("The Interview Catalog returned an invalid multi-image result.")
      : output(`Interview Catalog failed: ${catalog.result.error.message}`);
  }
  const catalogValue = catalog.result.value;
  const candidatePaths = [
    ...catalogValue.experienceCandidates.map(({ path }) => path),
    ...catalogValue.questionCandidates.map(({ path }) => path),
  ];
  const candidateStep = candidateReadStep(request.input, candidatePaths, nextCallId);
  if (candidateStep) return candidateStep;

  const exactDuplicate = catalogValue.experienceCandidates.find((candidate) => {
    if (!candidate.matchKinds.includes("source-fingerprint")) return false;
    const read = readResultFor(request.input, candidate.path);
    return read?.result.ok && read.result.value.type === "vault_read" &&
      frontmatterField(read.result.value.content, "source-fingerprint") === fingerprint;
  });
  if (exactDuplicate) {
    return output(
      "Duplicate screenshot Interview Experience suppressed; Question frequency and indexes are unchanged.",
    );
  }

  const recurring = matchingQuestionCandidates(
    request.input,
    catalogValue.questionCandidates,
    "distributedcacheconsistency",
  );
  if (recurring.length !== 1) {
    return output("The recurring Interview Question identity is ambiguous; no changes proposed.");
  }
  const recurringRead = readResultFor(request.input, recurring[0].path);
  if (!recurringRead?.result.ok || recurringRead.result.value.type !== "vault_read") {
    return output("Recurring Interview Question evidence is unavailable.");
  }
  const incremented = incrementFrequency(recurringRead.result.value.content);
  if (!incremented) return output("Recurring Question frequency is invalid; no changes proposed.");

  const experienceIndexDescriptor = catalogValue.indexes.find(({ kind }) => kind === "experience");
  const questionIndexDescriptor = catalogValue.indexes.find(({ kind }) => kind === "question");
  if (!experienceIndexDescriptor || !questionIndexDescriptor) {
    return output("The Interview Catalog omitted an index.");
  }
  const indexReads = [experienceIndexDescriptor, questionIndexDescriptor].map((descriptor) => ({
    descriptor,
    read: readResultFor(request.input, descriptor.path),
  }));
  for (const { descriptor, read } of indexReads) {
    if (!read) return readRequest(descriptor.path, nextCallId);
    if (!read.result.ok || read.result.value.type !== "vault_read") {
      return read.result.ok
        ? output("An Interview index returned an invalid result.")
        : output(`Interview index failed: ${read.result.error.message}`);
    }
  }

  const proposal = toolResultFor(request.input, "vault_propose_changes");
  if (!proposal) {
    const experienceIndex = indexReads[0].read!;
    const questionIndex = indexReads[1].read!;
    if (
      !experienceIndex.result.ok || experienceIndex.result.value.type !== "vault_read" ||
      !questionIndex.result.ok || questionIndex.result.value.type !== "vault_read"
    ) {
      return output("Interview index evidence is unavailable.");
    }
    const occurrence = "- [[experiences/multi-image-backend-interview]] — ordered screenshot submission\n";
    const recurringReplacement = `${incremented.trimEnd()}\n${occurrence}`;
    const batchId = nextCallId("fake-multi-image-batch");
    return {
      type: "local_tool_call",
      callId: nextCallId("fake-multi-image-proposal"),
      name: "vault_propose_changes",
      arguments: {
        batchId,
        idempotencyKey: batchId,
        task: "Ingest one ordered multi-image Interview Experience and synchronize Questions",
        actions: [
          {
            actionId: "create-multi-image-experience",
            idempotencyKey: "create-multi-image-experience",
            operation: "create",
            path: "experiences/multi-image-backend-interview.md",
            expectedVersion: "missing",
            content: `---\ntitle: Multi-image backend interview\ntype: interview-experience\nsource-type: screenshots\nsource-fingerprint: ${fingerprint}\n---\n\n# Multi-image backend interview\n\n## Summary\n\nA single ordered screenshot submission covered cache consistency and queue backpressure.\n\n## Questions\n\n- [[interview/distributed-cache-consistency]]\n- [[interview/queue-backpressure]]\n`,
          },
          {
            actionId: "merge-recurring-cache-question",
            idempotencyKey: "merge-recurring-cache-question",
            operation: "exact_replace",
            path: recurring[0].path,
            expectedVersion: recurringRead.result.value.modifiedVersion,
            expectedContent: recurringRead.result.value.content,
            replacement: recurringReplacement,
          },
          {
            actionId: "create-queue-backpressure-question",
            idempotencyKey: "create-queue-backpressure-question",
            operation: "create",
            path: "interview/queue-backpressure.md",
            expectedVersion: "missing",
            content: "---\ntitle: Explain queue backpressure\ntype: interview-question\nanswer-state: needs-research\nfrequency: 1\n---\n\n# Explain queue backpressure\n\nSeen in [[experiences/multi-image-backend-interview]].\n",
          },
          {
            actionId: "update-multi-image-experience-index",
            idempotencyKey: "update-multi-image-experience-index",
            operation: "exact_replace",
            path: experienceIndexDescriptor.path,
            expectedVersion: experienceIndex.result.value.modifiedVersion,
            expectedContent: experienceIndex.result.value.content,
            replacement: `${experienceIndex.result.value.content.trimEnd()}\n- [[multi-image-backend-interview]]\n`,
          },
          {
            actionId: "update-multi-image-question-index",
            idempotencyKey: "update-multi-image-question-index",
            operation: "exact_replace",
            path: questionIndexDescriptor.path,
            expectedVersion: questionIndex.result.value.modifiedVersion,
            expectedContent: questionIndex.result.value.content,
            replacement: `${questionIndex.result.value.content.trimEnd()}\n- [[queue-backpressure]]\n`,
          },
        ],
      },
    };
  }
  if (!proposal.result.ok) {
    return output(`Atomic multi-image ingestion failed: ${proposal.result.error.message}`);
  }
  return proposal.result.value.type === "vault_propose_changes" &&
    proposal.result.value.decision === "applied"
    ? output("One ordered multi-image Interview Experience and its Questions were applied atomically.")
    : output("The multi-image Interview Submission was not applied.");
}
