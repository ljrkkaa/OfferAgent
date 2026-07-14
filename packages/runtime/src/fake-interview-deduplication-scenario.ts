import type {
  ModelConversationItem,
  ModelRequest,
  ModelStreamEvent,
} from "./model-provider";
import { toolResultFor } from "./fake-provider-conversation";

function output(delta: string): ModelStreamEvent {
  return { type: "output_text.delta", delta };
}

function readResultFor(input: ModelConversationItem[], path: string) {
  return toolResultFor(
    input,
    "vault_read",
    (call) => (call.arguments as { path?: unknown }).path === path,
  );
}

function readRequest(path: string, nextCallId: (prefix: string) => string): ModelStreamEvent {
  return {
    type: "local_tool_call",
    callId: nextCallId("fake-dedup-read"),
    name: "vault_read",
    arguments: { path },
  };
}

function frontmatterField(content: string, name: string): string | undefined {
  const frontmatter = /^---\r?\n([\s\S]*?)\r?\n---(?:\r?\n|$)/u.exec(content)?.[1];
  if (!frontmatter) return undefined;
  const escaped = name.replace(/[.*+?^${}()|[\]\\]/gu, "\\$&");
  return new RegExp(`^${escaped}:\\s*(.+?)\\s*$`, "mu").exec(frontmatter)?.[1]?.trim();
}

function questionIdentity(content: string): string | undefined {
  const title = frontmatterField(content, "title") ?? /^#\s+(.+)$/mu.exec(content)?.[1]?.trim();
  return title?.toLocaleLowerCase().replace(/[^\p{L}\p{N}]+/gu, "");
}

function experienceIdentity(content: string) {
  const candidate = frontmatterField(content, "candidate")?.toLocaleLowerCase();
  const date = frontmatterField(content, "date");
  const round = frontmatterField(content, "round")?.toLocaleLowerCase();
  return {
    ...(candidate ? { candidate } : {}),
    ...(date ? { date } : {}),
    ...(round ? { round } : {}),
  };
}

export function interviewDeduplicationEvent(
  request: ModelRequest,
  nextCallId: (prefix: string) => string,
): ModelStreamEvent {
  const catalog = toolResultFor(request.input, "interview_catalog");
  if (!catalog) {
    return {
      type: "local_tool_call",
      callId: nextCallId("fake-dedup-catalog"),
      name: "interview_catalog",
      arguments: {
        query: "Example backend distributed cache consistency",
        canonicalUrl: "https://example.com/interviews/backend-42",
        sourceFingerprint: `sha256:${"b".repeat(64)}`,
        limit: 10,
      },
    };
  }
  if (!catalog.result.ok || catalog.result.value.type !== "interview_catalog") {
    return catalog.result.ok
      ? output("The Interview Catalog returned an invalid deduplication result.")
      : output(`Interview Catalog failed: ${catalog.result.error.message}`);
  }

  const catalogValue = catalog.result.value;
  const candidatePaths = [
    ...catalogValue.experienceCandidates.map(({ path }) => path),
    ...catalogValue.questionCandidates.map(({ path }) => path),
  ];
  for (const candidatePath of candidatePaths) {
    const candidateRead = readResultFor(request.input, candidatePath);
    if (!candidateRead) return readRequest(candidatePath, nextCallId);
    if (!candidateRead.result.ok || candidateRead.result.value.type !== "vault_read") {
      return candidateRead.result.ok
        ? output("Candidate evidence returned an invalid result.")
        : output(`Candidate evidence failed: ${candidateRead.result.error.message}`);
    }
  }

  const proposalResult = toolResultFor(request.input, "vault_propose_changes");
  const experienceEvidence = catalogValue.experienceCandidates.map((candidate) => {
    const candidateRead = readResultFor(request.input, candidate.path);
    if (!candidateRead?.result.ok || candidateRead.result.value.type !== "vault_read") {
      return { candidate };
    }
    return {
      candidate,
      read: candidateRead.result.value,
      identity: experienceIdentity(candidateRead.result.value.content),
    };
  });
  const matchingExperiences = experienceEvidence.filter(({ identity }) =>
    identity?.candidate === "candidate-b" &&
    identity.date === "2026-07-01" &&
    identity.round === "final",
  );
  if (matchingExperiences.length > 1) {
    return output("Ambiguous identity; no merge or Vault changes proposed.");
  }
  if (matchingExperiences.length === 1) {
    const duplicate = matchingExperiences[0];
    if (!duplicate.read) return output("Duplicate evidence is unavailable.");
    const exactSourceMatch = duplicate.candidate.matchKinds.includes("canonical-url") ||
      duplicate.candidate.matchKinds.includes("source-fingerprint");
    const existingSourceUrl = frontmatterField(duplicate.read.content, "source-url") ??
      frontmatterField(duplicate.read.content, "sourceUrl");
    if (
      exactSourceMatch &&
      !proposalResult &&
      !existingSourceUrl
    ) {
      const replacement = duplicate.read.content.replace(
        /^---\r?\n/u,
        "---\nsource-url: https://example.com/interviews/backend-42\n",
      );
      return {
        type: "local_tool_call",
        callId: nextCallId("fake-dedup-metadata"),
        name: "vault_propose_changes",
        arguments: {
          batchId: "dedup-metadata-only",
          idempotencyKey: "dedup-metadata-only",
          task: "Fill supported minimal Source Metadata on one duplicate Interview Experience",
          actions: [{
            actionId: "fill-source-url",
            idempotencyKey: "fill-source-url",
            operation: "exact_replace",
            path: duplicate.candidate.path,
            expectedVersion: duplicate.read.modifiedVersion,
            expectedContent: duplicate.read.content,
            replacement,
          }],
        },
      };
    }
    if (!proposalResult) {
      return output("Semantic duplicate Interview Experience suppressed after exact evidence review; Question frequency is unchanged.");
    }
    if (!proposalResult.result.ok) {
      return output(`Atomic deduplication batch failed: ${proposalResult.result.error.message}`);
    }
    return proposalResult.result.value.type === "vault_propose_changes" &&
      proposalResult.result.value.decision === "applied"
      ? output("Duplicate Interview Experience suppressed; minimal Source Metadata updated without changing Question frequency.")
      : output("The duplicate metadata update was rejected.");
  }
  if (
    experienceEvidence.some(({ identity }) =>
      !identity?.candidate || !identity.date || !identity.round,
    )
  ) {
    return output("Ambiguous identity; no merge or Vault changes proposed.");
  }
  const targetQuestionIdentity = "distributedcacheconsistency";
  const recurringQuestions = catalogValue.questionCandidates.filter((candidate) => {
    const candidateRead = readResultFor(request.input, candidate.path);
    return candidateRead?.result.ok &&
      candidateRead.result.value.type === "vault_read" &&
      questionIdentity(candidateRead.result.value.content) === targetQuestionIdentity;
  });
  if (recurringQuestions.length !== 1) {
    return output("Ambiguous recurring Question identity; no merge or Vault changes proposed.");
  }
  const recurringQuestion = recurringQuestions[0];

  const experienceIndex = readResultFor(request.input, "experiences/index.md");
  if (!experienceIndex) return readRequest("experiences/index.md", nextCallId);
  if (!experienceIndex.result.ok || experienceIndex.result.value.type !== "vault_read") {
    return experienceIndex.result.ok
      ? output("The Experience index returned an invalid result.")
      : output(`The Experience index failed: ${experienceIndex.result.error.message}`);
  }

  if (!proposalResult) {
    const questionRead = readResultFor(request.input, recurringQuestion.path);
    if (!questionRead?.result.ok || questionRead.result.value.type !== "vault_read") {
      return output("Recurring Question evidence is unavailable.");
    }
    const frequencyMatch = /^frequency:\s*(\d+)\s*$/mu.exec(questionRead.result.value.content);
    const currentFrequency = frequencyMatch ? Number(frequencyMatch[1]) : Number.NaN;
    if (!frequencyMatch || !Number.isSafeInteger(currentFrequency) || currentFrequency < 0) {
      return output("Recurring Question frequency is missing or invalid; no merge proposed.");
    }
    const questionReplacement = questionRead.result.value.content
      .replace(frequencyMatch[0], `frequency: ${currentFrequency + 1}`)
      .replace(
        /\s*$/u,
        "\n- [[experiences/backend-final-round]] — candidate-b, 2026-07-01 final round\n",
      );
    const indexReplacement = `${experienceIndex.result.value.content.trimEnd()}\n- [[backend-final-round]]\n`;
    const callId = nextCallId("fake-dedup-proposal");
    const batchId = `${callId}-batch`;
    return {
      type: "local_tool_call",
      callId,
      name: "vault_propose_changes",
      arguments: {
        batchId,
        idempotencyKey: batchId,
        task: "Keep one distinct Interview Experience and synchronize one recurring Interview Question",
        actions: [
          {
            actionId: "create-distinct-final-round",
            idempotencyKey: "create-distinct-final-round",
            operation: "create",
            path: "experiences/backend-final-round.md",
            expectedVersion: "missing",
            content: "---\ntitle: Backend final round\ntype: interview-experience\ncompany: Example Corp\nposition: Backend Engineer\ncandidate: candidate-b\nround: final\ndate: 2026-07-01\n---\n\n# Backend final round\n\n- [[interview/distributed-cache-consistency]]\n",
          },
          {
            actionId: "merge-recurring-question",
            idempotencyKey: "merge-recurring-question",
            operation: "exact_replace",
            path: recurringQuestion.path,
            expectedVersion: questionRead.result.value.modifiedVersion,
            expectedContent: questionRead.result.value.content,
            replacement: questionReplacement,
          },
          {
            actionId: "update-experience-index",
            idempotencyKey: "update-experience-index",
            operation: "exact_replace",
            path: "experiences/index.md",
            expectedVersion: experienceIndex.result.value.modifiedVersion,
            expectedContent: experienceIndex.result.value.content,
            replacement: indexReplacement,
          },
        ],
      },
    };
  }

  if (!proposalResult.result.ok) {
    return output(`Atomic deduplication batch failed: ${proposalResult.result.error.message}`);
  }
  return proposalResult.result.value.type === "vault_propose_changes" &&
    proposalResult.result.value.decision === "applied"
    ? output("Distinct Interview Experience stored and one recurring Question occurrence synchronized atomically.")
    : output("The atomic deduplication batch was rejected.");
}
