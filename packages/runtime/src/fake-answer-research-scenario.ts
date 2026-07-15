import type { ModelRequest, ModelStreamEvent } from "./model-provider";
import { frontmatterField } from "./fake-interview-evidence";
import { toolResultFor } from "./fake-provider-conversation";

const CURRENT_PUBLIC_SOURCE = "https://docs.example.com/distributed-cache-consistency";
const RENDERED_SOURCE = "https://dynamic.example/cache-consistency";

type AnswerState = "needs-research" | "draft" | "verified";

function output(delta: string): ModelStreamEvent {
  return { type: "output_text.delta", delta };
}

function userInput(request: ModelRequest): string {
  for (let index = request.input.length - 1; index >= 0; index -= 1) {
    const item = request.input[index];
    if (item.type === "user_message") return item.text;
  }
  return "";
}

function requestedState(input: string): AnswerState | undefined {
  const explicit = /Answer State[\s\S]*?to\s+(needs-research|draft|verified)/iu.exec(input)?.[1];
  if (explicit === "needs-research" || explicit === "draft" || explicit === "verified") {
    return explicit;
  }
  if (/\bverif(?:y|ied|ication)\b/iu.test(input)) return "verified";
  if (/\b(?:research|draft)\b/iu.test(input)) return "draft";
  return undefined;
}

function boundedQuery(input: string): string {
  const question = /Distributed cache consistency/iu.exec(input)?.[0] ?? "stated interview question";
  const company = /Example Corp/iu.test(input) ? "Example Corp" : "stated company";
  const position = /Backend Engineer/iu.test(input) ? "Backend Engineer" : "stated position";
  const direction = /distributed systems/iu.test(input)
    ? "distributed systems"
    : "stated technical direction";
  const date = /\d{4}-\d{2}-\d{2}/u.exec(input)?.[0] ?? "current date";
  return `${question} ${company} ${position} ${direction} ${date} interview question answer`;
}

function replaceSection(content: string, heading: string, body: string): string {
  const start = content.indexOf(heading);
  if (start < 0) return `${content.trimEnd()}\n\n${heading}\n\n${body.trim()}\n`;
  const bodyStart = start + heading.length;
  const nextHeading = content.indexOf("\n## ", bodyStart);
  const end = nextHeading < 0 ? content.length : nextHeading;
  return `${content.slice(0, bodyStart)}\n\n${body.trim()}\n${content.slice(end)}`;
}

function draftReplacement(content: string, source: string): string {
  const withState = content.replace(/^answer-state:\s*needs-research\s*$/mu, "answer-state: draft");
  return replaceSection(
    withState,
    "## Answer",
    [
      "Use versioned writes and compare-and-set at the authoritative store, then invalidate stale cache entries only after the authoritative commit succeeds.",
      "This keeps cache publication ordered with the source of truth while making stale writes detectable.",
      "",
      `Source: ${source}`,
    ].join("\n"),
  );
}

function verifiedReplacement(content: string, source: string): string {
  const withState = content.replace(/^answer-state:\s*draft\s*$/mu, "answer-state: verified");
  return replaceSection(
    withState,
    "## Verification",
    `Checked against current evidence: ${source}. The evidence supports versioned writes, compare-and-set, and post-commit invalidation.`,
  );
}

function transitionAllowed(current: AnswerState, requested: AnswerState): boolean {
  return (current === "needs-research" && requested === "draft") ||
    (current === "draft" && requested === "verified");
}

function evidenceStatus(content: string | undefined): "current" | "missing" | "conflicting" {
  if (!content || content.trim().length < 80) return "missing";
  if (/conflict(?:ing|s|ed)?/iu.test(content)) return "conflicting";
  return /versioned writes/iu.test(content) && /invalidation|invalidate/iu.test(content)
    ? "current"
    : "missing";
}

function sectionBody(content: string, heading: string): string | undefined {
  const start = content.indexOf(heading);
  if (start < 0) return undefined;
  const bodyStart = start + heading.length;
  const nextHeading = content.indexOf("\n## ", bodyStart);
  return content.slice(bodyStart, nextHeading < 0 ? content.length : nextHeading).trim();
}

function draftAgreesWithEvidence(question: string, evidence: string | undefined): boolean {
  const draft = sectionBody(question, "## Answer");
  if (!draft || draft.length < 40 || !evidence) return false;
  return /\bversioned writes\b/iu.test(draft) && /invalidate|invalidation/iu.test(draft) &&
    /\bversioned writes\b/iu.test(evidence) && /invalidate|invalidation/iu.test(evidence);
}

export function interviewAnswerResearchEvent(
  request: ModelRequest,
  nextCallId: (prefix: string) => string,
): ModelStreamEvent {
  const input = userInput(request);
  const requested = requestedState(input);
  if (!requested) {
    return output("No Answer research was started; here is a concise summary of the current conversation.");
  }

  const catalog = toolResultFor(request.input, "interview_catalog");
  if (!catalog) {
    return {
      type: "local_tool_call",
      callId: nextCallId("fake-answer-catalog"),
      name: "interview_catalog",
      arguments: { query: boundedQuery(input), limit: 5 },
    };
  }
  if (!catalog.result.ok || catalog.result.value.type !== "interview_catalog") {
    return output("Answer research stopped because the bounded Interview Catalog lookup failed.");
  }
  const candidates = catalog.result.value.questionCandidates;
  const selected = candidates.find(({ title }) => input.toLocaleLowerCase().includes(title.toLocaleLowerCase())) ??
    (candidates.length === 1 ? candidates[0] : undefined);
  if (!selected) {
    return output("Answer research found no unambiguous Question in the requested bound; Answer State is unchanged.");
  }

  const questionRead = toolResultFor(
    request.input,
    "vault_read",
    (call) => (call.arguments as { path?: unknown }).path === selected.path,
  );
  if (!questionRead) {
    return {
      type: "local_tool_call",
      callId: nextCallId("fake-answer-question-read"),
      name: "vault_read",
      arguments: { path: selected.path },
    };
  }
  if (!questionRead.result.ok || questionRead.result.value.type !== "vault_read") {
    return output("The exact Question could not be read; Answer State is unchanged.");
  }
  const question = questionRead.result.value;
  const current = frontmatterField(question.content, "answer-state") as AnswerState | undefined;
  if (!current || !["needs-research", "draft", "verified"].includes(current)) {
    return output("The Question has an invalid Answer State; it is unchanged.");
  }
  if (!transitionAllowed(current, requested)) {
    return output(current === requested
      ? `Answer State is already ${current}; it is unchanged.`
      : `The Answer State transition from ${current} to ${requested} is invalid; it is unchanged.`);
  }

  let evidenceContent: string | undefined;
  let source: string;
  if (/Vault evidence/iu.test(input)) {
    const search = toolResultFor(request.input, "vault_search");
    if (!search) {
      return {
        type: "local_tool_call",
        callId: nextCallId("fake-answer-vault-search"),
        name: "vault_search",
        arguments: { query: "distributed cache consistency versioned writes invalidation" },
      };
    }
    if (!search.result.ok || search.result.value.type !== "vault_search") {
      return output(`Vault evidence is unavailable; Answer State remains ${current} unchanged.`);
    }
    const evidencePath = search.result.value.entries[0]?.path;
    if (!evidencePath) return output(`Vault evidence is missing; Answer State remains ${current} unchanged.`);
    const evidenceRead = toolResultFor(
      request.input,
      "vault_read",
      (call) => (call.arguments as { path?: unknown }).path === evidencePath,
    );
    if (!evidenceRead) {
      return {
        type: "local_tool_call",
        callId: nextCallId("fake-answer-vault-read"),
        name: "vault_read",
        arguments: { path: evidencePath },
      };
    }
    if (evidenceRead.result.ok && evidenceRead.result.value.type === "vault_read") {
      evidenceContent = evidenceRead.result.value.content;
    }
    source = evidencePath;
  } else if (/Research Browser|rendered/iu.test(input)) {
    const opened = toolResultFor(
      request.input,
      "research_browser",
      (call) => (call.arguments as { action?: unknown }).action === "open",
    );
    if (!opened) {
      return {
        type: "local_tool_call",
        callId: nextCallId("fake-answer-browser-open"),
        name: "research_browser",
        arguments: { action: "open", url: RENDERED_SOURCE },
      };
    }
    const rendered = toolResultFor(
      request.input,
      "research_browser",
      (call) => (call.arguments as { action?: unknown }).action === "read",
    );
    if (!rendered) {
      return {
        type: "local_tool_call",
        callId: nextCallId("fake-answer-browser-read"),
        name: "research_browser",
        arguments: { action: "read", maxBytes: 32_768 },
      };
    }
    if (rendered.result.ok && rendered.result.value.type === "research_browser" &&
        rendered.result.value.status === "ready") {
      evidenceContent = rendered.result.value.content;
      source = rendered.result.value.url;
    } else {
      source = RENDERED_SOURCE;
    }
  } else {
    const web = toolResultFor(
      request.input,
      "web_read",
      (call) => (call.arguments as { url?: unknown }).url === CURRENT_PUBLIC_SOURCE,
    );
    if (!web) {
      return {
        type: "local_tool_call",
        callId: nextCallId("fake-answer-web-read"),
        name: "web_read",
        arguments: { url: CURRENT_PUBLIC_SOURCE, maxBytes: 32_768 },
      };
    }
    if (web.result.ok && web.result.value.type === "web_read") evidenceContent = web.result.value.content;
    source = CURRENT_PUBLIC_SOURCE;
  }

  const status = evidenceStatus(evidenceContent);
  if (status === "missing") {
    return output(`Current evidence is missing or unavailable; Answer State remains ${current} unchanged.`);
  }
  if (status === "conflicting") {
    return output(`The exact evidence is conflicting; Answer State remains ${current} unchanged.`);
  }
  if (requested === "verified" && !draftAgreesWithEvidence(question.content, evidenceContent)) {
    return output("The current draft does not agree with the exact evidence; Answer State remains draft unchanged.");
  }

  const proposal = toolResultFor(request.input, "vault_propose_changes");
  if (!proposal) {
    const replacement = requested === "draft"
      ? draftReplacement(question.content, source)
      : verifiedReplacement(question.content, source);
    const sequence = nextCallId("fake-answer-proposal");
    return {
      type: "local_tool_call",
      callId: sequence,
      name: "vault_propose_changes",
      arguments: {
        batchId: sequence,
        idempotencyKey: sequence,
        task: `Advance Answer State from ${current} to ${requested} using exact evidence`,
        actions: [{
          actionId: "advance-answer-state",
          idempotencyKey: "advance-answer-state",
          operation: "exact_replace",
          path: selected.path,
          expectedVersion: question.modifiedVersion,
          expectedContent: question.content,
          replacement,
        }],
      },
    };
  }
  if (proposal.result.ok && proposal.result.value.type === "vault_propose_changes" &&
      proposal.result.value.decision === "applied") {
    return output(`Answer State advanced from ${current} to ${requested} using exact current evidence; Learning State is unchanged.`);
  }
  return output(`The atomic Answer update was not applied; Answer State remains ${current} unchanged.`);
}
