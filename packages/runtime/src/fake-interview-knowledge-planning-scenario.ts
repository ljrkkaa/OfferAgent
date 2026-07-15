import type { ModelRequest, ModelStreamEvent } from "./model-provider";
import { frontmatterField } from "./fake-interview-evidence";
import { toolResultFor } from "./fake-provider-conversation";

interface PlanningQuestion {
  answerState: "draft" | "needs-research" | "verified";
  content: string;
  learningState: "study-in-progress" | "study-todo";
  path: string;
  projectRisk: boolean;
  recentMatchingExperiences: number;
  recentPlan: boolean;
  title: string;
}

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

function exactRead(request: ModelRequest, path: string) {
  return toolResultFor(
    request.input,
    "vault_read",
    (call) => (call.arguments as { path?: unknown }).path === path,
  );
}

function nextExactRead(
  request: ModelRequest,
  paths: string[],
  nextCallId: (prefix: string) => string,
  prefix: string,
): ModelStreamEvent[] | undefined {
  const missing = paths.filter((candidate) => !exactRead(request, candidate));
  return missing.length > 0
    ? missing.map((path) => ({
        type: "local_tool_call",
        callId: nextCallId(prefix),
        name: "vault_read",
        arguments: { path },
      }))
    : undefined;
}

function projectPaths(content: string): string[] {
  return [...content.matchAll(/\[\[(projects\/[^\]|]+)(?:\|[^\]]+)?\]\]/giu)]
    .map((match) => match[1]!.endsWith(".md") ? match[1]! : `${match[1]}.md`)
    .slice(0, 3);
}

function explicitScopeMatch(input: string, question: PlanningQuestion): boolean {
  const statedGoal = /(?:study goal is|goal:)\s*([^.;]+?)(?:\s+readiness)?(?:[.;]|$)/iu.exec(input)?.[1]?.trim();
  if (!statedGoal) return true;
  const terms = statedGoal.toLocaleLowerCase().match(/[\p{L}\p{N}]{3,}/gu) ?? [];
  const evidence = `${question.title} ${question.content}`.toLocaleLowerCase();
  return terms.length > 0 && terms.every((term) => evidence.includes(term));
}

function planLabel(question: PlanningQuestion): string {
  const action = question.answerState === "needs-research"
    ? `Research answer: ${question.title}`
    : question.learningState === "study-in-progress"
      ? `Continue study and answer refinement: ${question.title}`
      : question.answerState === "verified"
        ? `Study and rehearse: ${question.title}`
        : `Study draft and refine answer: ${question.title}`;
  const reasons = [
    ...(question.projectRisk ? ["Project resume deep-dive priority"] : []),
    ...(question.recentPlan && question.recentMatchingExperiences > 0
      ? [`Justified review from ${question.recentMatchingExperiences} recent matching Experiences`]
      : []),
  ];
  return reasons.length > 0 ? `${action} — ${reasons.join("; ")}` : action;
}

function orderedSelection(input: string, questions: PlanningQuestion[]): PlanningQuestion[] {
  const scoped = questions.filter((question) => explicitScopeMatch(input, question));
  const usable = scoped.filter(
    (question) => !question.recentPlan || question.recentMatchingExperiences > 0 || question.projectRisk,
  );
  const take = (predicate: (question: PlanningQuestion) => boolean) => usable.filter(predicate);
  const ordered = [
    ...take((question) => question.projectRisk),
    ...take((question) => !question.projectRisk && question.recentMatchingExperiences > 1),
    ...take((question) => !question.projectRisk && question.recentMatchingExperiences <= 1 &&
      question.answerState === "needs-research"),
    ...take((question) => !question.projectRisk && question.recentMatchingExperiences <= 1 &&
      question.answerState !== "needs-research"),
  ];
  return ordered.slice(0, 3);
}

function planDocument(template: string | null, date: string, questions: PlanningQuestion[]): string {
  const items = questions.map((question) =>
    `- [ ] ${planLabel(question)} — Answer State: ${question.answerState}; Learning State: ${question.learningState}; Source: ${question.path}`,
  );
  const freshSection = [
    "## Daily Study Plan",
    "",
    ...items,
    "",
    "> This is a forward-looking plan. Only explicit Daily Note completion is Study Evidence.",
    "",
  ].join("\n");
  const base = (template ?? `# ${date}\n\n## Daily Study Plan\n`)
    .replaceAll("{{date}}", date)
    .replaceAll("{{title}}", date);
  const start = base.indexOf("## Daily Study Plan");
  if (start < 0) return `${base.trimEnd()}\n\n${freshSection}`;
  const nextHeading = base.indexOf("\n## ", start + "## Daily Study Plan".length);
  const end = nextHeading < 0 ? base.length : nextHeading;
  const existingSection = base.slice(start, end);
  const existingBody = existingSection.slice("## Daily Study Plan".length).trim();
  if (!existingBody) {
    return `${base.slice(0, start)}${freshSection}${nextHeading < 0 ? "" : base.slice(nextHeading + 1)}`;
  }
  const missingItems = items.filter((item, index) => !existingSection.includes(questions[index]!.title));
  if (missingItems.length === 0) return base;
  return `${base.slice(0, end).trimEnd()}\n${missingItems.join("\n")}\n${base.slice(end)}`;
}

function isRecentExperience(content: string, today: string): boolean {
  const value = frontmatterField(content, "date");
  if (!value) return false;
  const date = new Date(`${value}T12:00:00Z`);
  const current = new Date(`${today}T12:00:00Z`);
  if (Number.isNaN(date.getTime()) || Number.isNaN(current.getTime()) || date > current) return false;
  const lowerBound = new Date(current);
  lowerBound.setUTCMonth(lowerBound.getUTCMonth() - 6);
  return date >= lowerBound;
}

export function interviewKnowledgePlanningEvent(
  request: ModelRequest,
  nextCallId: (prefix: string) => string,
): ModelStreamEvent | ModelStreamEvent[] {
  const input = userInput(request);
  if (!/Daily Study Plan|study plan/iu.test(input)) {
    return output("No Daily Study Plan was started; here is the requested summary.");
  }

  const daily = toolResultFor(request.input, "daily_note_context");
  if (!daily) {
    return {
      type: "local_tool_call",
      callId: nextCallId("fake-knowledge-plan-daily"),
      name: "daily_note_context",
      arguments: {},
    };
  }
  if (!daily.result.ok || daily.result.value.type !== "daily_note_context") {
    return output("Daily Study Plan could not resolve the configured Daily Note; no change was proposed.");
  }
  const context = daily.result.value;

  const catalog = toolResultFor(request.input, "interview_catalog");
  if (!catalog) {
    return {
      type: "local_tool_call",
      callId: nextCallId("fake-knowledge-plan-catalog"),
      name: "interview_catalog",
      arguments: { query: input.slice(0, 512), limit: 5 },
    };
  }
  if (!catalog.result.ok || catalog.result.value.type !== "interview_catalog") {
    return output("The bounded Interview Catalog context is unavailable; no Daily plan was proposed.");
  }
  const candidates = catalog.result.value.questionCandidates.slice(0, 5);
  const candidateRead = nextExactRead(
    request,
    candidates.map(({ path }) => path),
    nextCallId,
    "fake-knowledge-plan-question",
  );
  if (candidateRead) return candidateRead;
  const experiences = catalog.result.value.experienceCandidates.slice(0, 5);
  const experienceRead = nextExactRead(
    request,
    experiences.map(({ path }) => path),
    nextCallId,
    "fake-knowledge-plan-experience",
  );
  if (experienceRead) return experienceRead;

  const recentSearch = toolResultFor(request.input, "vault_search");
  if (!recentSearch) {
    return {
      type: "local_tool_call",
      callId: nextCallId("fake-knowledge-plan-recent-search"),
      name: "vault_search",
      arguments: { query: "Daily Study Plan", limit: 3 },
    };
  }
  const recentPaths = recentSearch.result.ok && recentSearch.result.value.type === "vault_search"
    ? recentSearch.result.value.entries.map(({ path }) => path).slice(0, 3)
    : [];
  const recentRead = nextExactRead(request, recentPaths, nextCallId, "fake-knowledge-plan-recent-read");
  if (recentRead) return recentRead;

  const registryPath = "projects/index.md";
  const registry = exactRead(request, registryPath);
  if (!registry) {
    return {
      type: "local_tool_call",
      callId: nextCallId("fake-knowledge-plan-project-registry"),
      name: "vault_read",
      arguments: { path: registryPath },
    };
  }
  const registryContent = registry.result.ok && registry.result.value.type === "vault_read"
    ? registry.result.value.content
    : "";
  const registeredPaths = projectPaths(registryContent);
  const projectRead = nextExactRead(request, registeredPaths, nextCallId, "fake-knowledge-plan-project-read");
  if (projectRead) return projectRead;

  const recentContent = recentPaths.map((path) => {
    const read = exactRead(request, path);
    return read?.result.ok && read.result.value.type === "vault_read" ? read.result.value.content : "";
  }).join("\n");
  const projectContent = registeredPaths.map((path) => {
    const read = exactRead(request, path);
    return read?.result.ok && read.result.value.type === "vault_read" ? read.result.value.content : "";
  }).join("\n");
  const recentExperiences = experiences.flatMap((experience) => {
    const read = exactRead(request, experience.path);
    if (!read?.result.ok || read.result.value.type !== "vault_read" ||
        !isRecentExperience(read.result.value.content, context.resolvedDate)) return [];
    const content = read.result.value.content;
    const company = frontmatterField(content, "company");
    const position = frontmatterField(content, "position");
    if ((company && /\bCorp\b/iu.test(input) && !input.toLocaleLowerCase().includes(company.toLocaleLowerCase())) ||
        (position && /\bEngineer\b/iu.test(input) && !input.toLocaleLowerCase().includes(position.toLocaleLowerCase()))) {
      return [];
    }
    return [content];
  });
  const questions = candidates.flatMap((candidate): PlanningQuestion[] => {
    const read = exactRead(request, candidate.path);
    if (!read?.result.ok || read.result.value.type !== "vault_read") return [];
    const content = read.result.value.content;
    const answerState = frontmatterField(content, "answer-state") ?? candidate.answerState;
    const learningState = frontmatterField(content, "learning-state");
    if ((answerState !== "needs-research" && answerState !== "draft" && answerState !== "verified") ||
        (learningState !== "study-todo" && learningState !== "study-in-progress")) return [];
    const projectName = frontmatterField(content, "project");
    const projectRisk = /high/iu.test(frontmatterField(content, "resume-deep-dive-risk") ?? "") &&
      Boolean(projectName && registryContent.toLocaleLowerCase().includes(projectName.toLocaleLowerCase())) ||
      projectContent.toLocaleLowerCase().includes(candidate.title.toLocaleLowerCase());
    return [{
      answerState,
      content,
      learningState,
      path: candidate.path,
      projectRisk,
      recentMatchingExperiences: recentExperiences.filter((experience) =>
        experience.includes(`[[${candidate.path.replace(/\.md$/u, "")}]]`) ||
        experience.toLocaleLowerCase().includes(candidate.title.toLocaleLowerCase()),
      ).length,
      recentPlan: recentContent.toLocaleLowerCase().includes(candidate.title.toLocaleLowerCase()),
      title: candidate.title,
    }];
  });
  const selected = orderedSelection(input, questions);
  if (selected.length === 0) {
    return output("No bounded Interview Knowledge gap was suitable for today's plan; no change was proposed.");
  }

  const targetRead = context.targetExists ? exactRead(request, context.targetPath) : undefined;
  if (context.targetExists && !targetRead) {
    return {
      type: "local_tool_call",
      callId: nextCallId("fake-knowledge-plan-target-read"),
      name: "vault_read",
      arguments: { path: context.targetPath },
    };
  }
  const currentTarget = targetRead?.result.ok && targetRead.result.value.type === "vault_read"
    ? targetRead.result.value
    : undefined;
  if (context.targetExists && !currentTarget) {
    return output("The current Daily Note could not be read exactly; no change was proposed.");
  }
  const replacement = planDocument(
    currentTarget?.content ?? context.templateContent,
    context.resolvedDate,
    selected,
  );
  const proposal = toolResultFor(request.input, "vault_propose_changes");
  if (!proposal) {
    const sequence = nextCallId("fake-knowledge-plan-proposal");
    return {
      type: "local_tool_call",
      callId: sequence,
      name: "vault_propose_changes",
      arguments: {
        batchId: sequence,
        idempotencyKey: sequence,
        task: "Create one bounded Interview Knowledge Daily Study Plan",
        actions: [currentTarget
          ? {
              actionId: "update-knowledge-plan",
              idempotencyKey: "update-knowledge-plan",
              operation: "exact_replace",
              path: context.targetPath,
              expectedVersion: currentTarget.modifiedVersion,
              expectedContent: currentTarget.content,
              replacement,
            }
          : {
              actionId: "create-knowledge-plan",
              idempotencyKey: "create-knowledge-plan",
              operation: "create",
              path: context.targetPath,
              expectedVersion: "missing",
              content: replacement,
            }],
      },
    };
  }
  if (proposal.result.ok && proposal.result.value.type === "vault_propose_changes" &&
      proposal.result.value.decision === "applied") {
    return output("The bounded Interview Knowledge Daily Study Plan was applied. It records no Study Evidence and changes no Answer or Learning State.");
  }
  return output("The Daily Study Plan batch was not applied; Interview Knowledge states are unchanged.");
}
