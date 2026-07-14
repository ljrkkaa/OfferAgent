import type { DailyNoteContextResult, LocalToolName, ModelDescriptor } from "@offeragent/protocol";
import {
  ModelProviderError,
  type ModelConversationItem,
  type ModelProvider,
  type ModelRequest,
  type ModelStreamEvent,
} from "./model-provider";
import {
  MEMORY_CAPTURE_INSTRUCTIONS,
  MEMORY_SELECTOR_INSTRUCTIONS,
  type MemorySelectionInput,
} from "./planning-memory";

function findLatest<T extends ModelConversationItem>(
  input: ModelConversationItem[],
  predicate: (item: ModelConversationItem) => item is T,
): T | undefined {
  for (let index = input.length - 1; index >= 0; index -= 1) {
    const item = input[index];
    if (predicate(item)) return item;
  }
  return undefined;
}

function userMessageBefore(
  input: ModelConversationItem[],
  item: ModelConversationItem,
): Extract<ModelConversationItem, { type: "user_message" }> | undefined {
  const beforeIndex = input.lastIndexOf(item);
  for (let index = beforeIndex - 1; index >= 0; index -= 1) {
    const candidate = input[index];
    if (candidate.type === "user_message") return candidate;
  }
  return undefined;
}

const MODEL: ModelDescriptor = {
  id: "fake-interview-model",
  label: "Fake Interview Model",
  supportsFastMode: true,
};

export class FakeModelProvider implements ModelProvider {
  readonly backendId = "fake";
  #toolCallSequence = 0;

  async listModels(): Promise<ModelDescriptor[]> {
    return [MODEL];
  }

  async *stream(request: ModelRequest): AsyncIterable<ModelStreamEvent> {
    if (request.signal.aborted) {
      throw new ModelProviderError("transport_error", "The Agent Run was interrupted.");
    }
    if (request.model !== MODEL.id) {
      throw new ModelProviderError(
        "model_unavailable",
        `The selected model '${request.model}' is not available.`,
      );
    }
    const userInput = findLatest(
      request.input,
      (item): item is Extract<ModelConversationItem, { type: "user_message" }> =>
        item.type === "user_message",
    )?.text ?? "";
    if (request.instructions === MEMORY_CAPTURE_INSTRUCTIONS) {
      let durable = false;
      try {
        const payload = JSON.parse(userInput) as { newMessages?: Array<{ text?: string }> };
        const messages = payload.newMessages?.map(({ text }) => text ?? "").join("\n") ?? "";
        durable = /I will study retrieval evaluation across the next three days/i.test(messages) ||
          /planning_memory_foreground/.test(messages);
      } catch {
        durable = false;
      }
      yield {
        type: "output_text.delta",
        delta: durable ? JSON.stringify([{
          kind: "upsert",
          path: "memory/study/retrieval-evaluation.md",
          type: "study",
          name: "Retrieval evaluation",
          description: "Current cross-day retrieval evaluation direction",
          content: "Study retrieval evaluation across the next three days; keep daily schedules in Daily Notes.",
        }]) : "[]",
      };
      return;
    }
    if (request.instructions === MEMORY_SELECTOR_INSTRUCTIONS) {
      let selected: string[] = [];
      try {
        const selection = JSON.parse(userInput) as MemorySelectionInput;
        const context = [
          selection.request,
          ...selection.conversationContext.map(({ text }) => text),
        ].join(" ");
        const contextFeatures = textFeatures(context);
        const scored = selection.topics
          .map((topic, index) => ({
            index,
            path: topic.path,
            score: overlapScore(contextFeatures, textFeatures(`${topic.name} ${topic.description}`)),
          }))
        const maximumScore = Math.max(0, ...scored.map(({ score }) => score));
        selected = scored
          .filter(({ score }) => score >= Math.max(8, maximumScore * 0.2))
          .sort((left, right) => right.score - left.score || left.index - right.index)
          .slice(0, 5)
          .map(({ path }) => path);
      } catch {
        selected = [];
      }
      yield { type: "output_text.delta", delta: JSON.stringify(selected) };
      return;
    }
    if (userInput.trim() === "planning_memory_acceptance") {
      const feedback = /Relevant Feedback Memory:\n([\s\S]*?)(?:\n\nOther Relevant Planning Memory:|\n\nModel defaults)/.exec(
        request.instructions,
      )?.[1];
      const planning = /Other Relevant Planning Memory:\n([\s\S]*?)(?:\n\nModel defaults)/.exec(
        request.instructions,
      )?.[1];
      yield {
        type: "output_text.delta",
        delta: feedback
          ? "OfferAgent applied relevant Feedback Memory: planned study remains future work, not completed evidence."
          : planning
            ? "OfferAgent applied relevant Planning Memory to this response."
          : "OfferAgent found no relevant Planning Memory.",
      };
      return;
    }
    if (userInput.trim() === "planning_memory_foreground") {
      const applied = toolResultFor(request.input, "vault_propose_changes");
      if (!applied) {
        this.#toolCallSequence += 1;
        const batchId = `foreground-memory-batch-${this.#toolCallSequence}`;
        yield {
          type: "local_tool_call",
          callId: `fake-memory-foreground-${this.#toolCallSequence}`,
          name: "vault_propose_changes",
          arguments: {
            batchId,
            idempotencyKey: batchId,
            task: "Remember the durable project direction",
            actions: [
              {
                actionId: "foreground-memory-topic",
                idempotencyKey: "foreground-memory-topic",
                operation: "create",
                path: "memory/project/offeragent.md",
                expectedVersion: "missing",
                content: "---\nname: OfferAgent\ndescription: Current project direction\ntype: project\n---\n\nShip the local plugin.\n",
              },
              {
                actionId: "foreground-memory-index",
                idempotencyKey: "foreground-memory-index",
                operation: "create",
                path: "memory/MEMORY.md",
                expectedVersion: "missing",
                content: "# Planning Memory\n\n- [OfferAgent](project/offeragent.md) - Current project direction\n",
              },
            ],
          },
        };
        return;
      }
      yield { type: "output_text.delta", delta: "Saved the durable project direction." };
      return;
    }
    const precedence = /^planning_memory_precedence\s+(\w+)$/i.exec(userInput.trim());
    if (precedence) {
      const contract = /Agent Contract \(highest instruction priority\):\n([\s\S]*?)(?:\n\nRequested Local Skills|\n\nInstruction precedence)/.exec(
        request.instructions,
      )?.[1];
      const contractChoice = /PRECEDENCE_CHOICE:\s*(\w+)/.exec(contract ?? "")?.[1];
      const feedback = /Relevant Feedback Memory:\n([\s\S]*?)(?:\n\nOther Relevant Planning Memory:|\n\nModel defaults)/.exec(
        request.instructions,
      )?.[1];
      const memoryChoice = /PRECEDENCE_CHOICE:\s*(\w+)/.exec(feedback ?? "")?.[1];
      yield {
        type: "output_text.delta",
        delta: `PRECEDENCE_CHOICE: ${contractChoice ?? precedence[1] ?? memoryChoice ?? "default"}`,
      };
      return;
    }
    if (userInput.trim() === "conversation_context") {
      yield {
        type: "output_text.delta",
        delta: JSON.stringify(
          request.input.map((item) =>
            item.type === "user_message" || item.type === "assistant_message"
              ? {
                  role: item.type === "user_message" ? "user" : "assistant",
                  marker: item.text.slice(0, 1),
                  length: item.text.length,
                }
              : { role: item.type },
          ),
        ),
      };
      return;
    }
    if (userInput.trim() === "context_tool_demo") {
      const result = findLatest(
        request.input,
        (item): item is Extract<ModelConversationItem, { type: "local_tool_result" }> =>
          item.type === "local_tool_result" && item.callId.startsWith("fake-context-read-"),
      );
      if (!result) {
        this.#toolCallSequence += 1;
        yield {
          type: "local_tool_call",
          callId: `fake-context-read-${this.#toolCallSequence}`,
          name: "vault_read",
          arguments: { path: "notes/context.md", lineStart: 1, lineEnd: 1 },
        };
        return;
      }
      yield { type: "output_text.delta", delta: "Tool completed." };
      return;
    }
    if (
      userInput.trim() === "帮我做一个今天的学习日记" ||
      userInput.trim() === "今天改学 RAG evaluation" ||
      userInput.trim() === "帮我按 Agentic RL Project 做今天的学习日记"
    ) {
      yield {
        type: "output_text.delta",
        delta: "DAILY_STUDY_PLAN_PROPOSAL: 我会按 Obsidian 配置创建或保守填充今天的前瞻学习计划，并从 Vault 学习队列选取有来源的主题。",
      };
      return;
    }
    if (userInput.trim() === "可以的") {
      const priorResponse = findLatest(
        request.input,
        (item): item is Extract<ModelConversationItem, { type: "assistant_message" }> =>
          item.type === "assistant_message",
      );
      if (priorResponse?.text.includes("DAILY_STUDY_PLAN_PROPOSAL")) {
        const proposalRequest = userMessageBefore(request.input, priorResponse)?.text.trim() ?? "";
        const explicitDirection = proposalRequest === "今天改学 RAG evaluation"
          ? "RAG evaluation"
          : undefined;
        const change = toolResultFor(request.input, "vault_propose_changes");
        if (change?.result.ok && change.result.value.type === "vault_propose_changes") {
          const changeIndex = request.input.indexOf(change);
          const unverifiedTarget = change.result.value.decision === "applied"
            ? change.result.value.targets.find((target) => {
                const verification = toolResultForAfter(
                  request.input,
                  "vault_read",
                  changeIndex,
                  (call) => Boolean(call.arguments && typeof call.arguments === "object" &&
                    (call.arguments as { path?: unknown }).path === target.path),
                );
                return !verification?.result.ok || verification.result.value.type !== "vault_read" ||
                  verification.result.value.contentHash !== target.afterHash;
              })
            : undefined;
          if (unverifiedTarget) {
            this.#toolCallSequence += 1;
            yield {
              type: "local_tool_call",
              callId: `fake-daily-plan-verify-${this.#toolCallSequence}`,
              name: "vault_read",
              arguments: { path: unverifiedTarget.path },
            };
            return;
          }
          yield {
            type: "output_text.delta",
            delta: change.result.value.decision === "applied"
              ? `今日学习计划已写入，并已创建 Git checkpoint ${change.result.value.checkpointRef ?? ""}。计划项均为未完成状态。`
              : "今日学习计划批次未应用。",
          };
          return;
        }
        const daily = toolResultFor(request.input, "daily_note_context");
        if (!daily) {
          this.#toolCallSequence += 1;
          yield {
            type: "local_tool_call",
            callId: `fake-daily-plan-context-${this.#toolCallSequence}`,
            name: "daily_note_context",
            arguments: {},
          };
          return;
        }
        if (!daily.result.ok || daily.result.value.type !== "daily_note_context") {
          yield { type: "output_text.delta", delta: "无法解析今天的 Daily Note Context。" };
          return;
        }
        const context = daily.result.value;
        const targetRead = toolResultFor(
          request.input,
          "vault_read",
          (call) => Boolean(call.arguments && typeof call.arguments === "object" &&
            (call.arguments as { path?: unknown }).path === context.targetPath),
        );
        if (context.targetExists && !targetRead) {
          this.#toolCallSequence += 1;
          yield {
            type: "local_tool_call",
            callId: `fake-daily-plan-target-${this.#toolCallSequence}`,
            name: "vault_read",
            arguments: { path: context.targetPath },
          };
          return;
        }
        const recalledPlanningPath = recalledPlanningPathFrom(request.instructions, proposalRequest);
        const recalledPlanningRead = recalledPlanningPath ? toolResultFor(
          request.input,
          "vault_read",
          (call) => Boolean(call.arguments && typeof call.arguments === "object" &&
            (call.arguments as { path?: unknown }).path === recalledPlanningPath),
        ) : undefined;
        if (recalledPlanningPath && !recalledPlanningRead) {
          this.#toolCallSequence += 1;
          yield {
            type: "local_tool_call",
            callId: `fake-daily-plan-memory-${this.#toolCallSequence}`,
            name: "vault_read",
            arguments: { path: recalledPlanningPath },
          };
          return;
        }
        const needsMemoryIndex = Boolean(recalledPlanningPath || explicitDirection);
        const memoryIndexRead = needsMemoryIndex ? toolResultFor(
          request.input,
          "vault_read",
          (call) => Boolean(call.arguments && typeof call.arguments === "object" &&
            (call.arguments as { path?: unknown }).path === "memory/MEMORY.md"),
        ) : undefined;
        if (needsMemoryIndex && !memoryIndexRead) {
          this.#toolCallSequence += 1;
          yield {
            type: "local_tool_call",
            callId: `fake-daily-plan-index-${this.#toolCallSequence}`,
            name: "vault_read",
            arguments: { path: "memory/MEMORY.md" },
          };
          return;
        }
        const projectDerivedPath = recalledPlanningPath?.startsWith("memory/project/")
          ? "memory/study/project-informed-direction.md"
          : undefined;
        const projectDerivedRead = projectDerivedPath ? toolResultFor(
          request.input,
          "vault_read",
          (call) => Boolean(call.arguments && typeof call.arguments === "object" &&
            (call.arguments as { path?: unknown }).path === projectDerivedPath),
        ) : undefined;
        if (projectDerivedPath && !projectDerivedRead) {
          this.#toolCallSequence += 1;
          yield {
            type: "local_tool_call",
            callId: `fake-daily-plan-derived-memory-${this.#toolCallSequence}`,
            name: "vault_read",
            arguments: { path: projectDerivedPath },
          };
          return;
        }
        const usesPlanningMemory = Boolean(
          recalledPlanningPath &&
          recalledPlanningRead?.result.ok && recalledPlanningRead.result.value.type === "vault_read" &&
          memoryIndexRead?.result.ok && memoryIndexRead.result.value.type === "vault_read",
        );
        let sourcePath = explicitDirection
          ? "当前用户明确请求"
          : usesPlanningMemory ? recalledPlanningPath! : "interview/面试八股学习进度.md";
        let sourceRead = usesPlanningMemory ? recalledPlanningRead! : toolResultFor(
          request.input,
          "vault_read",
          (call) => Boolean(call.arguments && typeof call.arguments === "object" &&
            (call.arguments as { path?: unknown }).path === sourcePath),
        );
        if (!explicitDirection && !sourceRead) {
          this.#toolCallSequence += 1;
          yield {
            type: "local_tool_call",
            callId: `fake-daily-plan-source-${this.#toolCallSequence}`,
            name: "vault_read",
            arguments: { path: sourcePath },
          };
          return;
        }
        let topic = explicitDirection ?? (sourceRead?.result.ok && sourceRead.result.value.type === "vault_read"
          ? (usesPlanningMemory
              ? studyMemoryDirection(sourceRead.result.value.content)
              : dailyPlanTopic(sourceRead.result.value.content))
          : undefined);
        if (!topic) {
          const fallbackPath = "notes/example.md";
          const fallbackRead = toolResultFor(
            request.input,
            "vault_read",
            (call) => Boolean(call.arguments && typeof call.arguments === "object" &&
              (call.arguments as { path?: unknown }).path === fallbackPath),
          );
          if (!fallbackRead) {
            this.#toolCallSequence += 1;
            yield {
              type: "local_tool_call",
              callId: `fake-daily-plan-fallback-${this.#toolCallSequence}`,
              name: "vault_read",
              arguments: { path: fallbackPath },
            };
            return;
          }
          sourcePath = fallbackPath;
          sourceRead = fallbackRead;
          topic = sourceRead.result.ok && sourceRead.result.value.type === "vault_read"
            ? dailyPlanTopic(sourceRead.result.value.content)
            : undefined;
        }
        if ((!explicitDirection && (!sourceRead?.result.ok || sourceRead.result.value.type !== "vault_read")) || !topic) {
          yield { type: "output_text.delta", delta: "可选来源均不可用，未将未读取的内容标记为计划来源。" };
          return;
        }
        const planItems = [
          `- [ ] ${topic}（来源：${sourcePath}）`,
          "- [ ] 整理 3 个核心问答并进行一次口述自测",
          "\n> 本节是前瞻计划，不是学习完成证据。",
        ].join("\n");
        const action = dailyPlanAction(context, targetRead, planItems, Boolean(explicitDirection));
        if (action === null) {
          yield { type: "output_text.delta", delta: "今日学习计划已包含这些未完成计划项，无需重复写入。" };
          return;
        }
        if (!action) {
          yield { type: "output_text.delta", delta: "现有 Daily Note 无法安全读取，未提交修改。" };
          return;
        }
        this.#toolCallSequence += 1;
        const proposalSequence = this.#toolCallSequence;
        action.actionId = `${action.actionId}-${proposalSequence}`;
        action.idempotencyKey = `${action.idempotencyKey}-${proposalSequence}`;
        const actions: Record<string, unknown>[] = [action];
        const readableMemoryIndex = memoryIndexRead?.result.ok &&
          memoryIndexRead.result.value.type === "vault_read"
          ? memoryIndexRead.result.value
          : undefined;
        if (explicitDirection) {
          const memoryName = "RAG evaluation";
          const memoryDescription = "Current cross-day RAG evaluation direction";
          if (
            recalledPlanningPath?.startsWith("memory/study/") && recalledPlanningRead?.result.ok &&
            recalledPlanningRead.result.value.type === "vault_read" && readableMemoryIndex
          ) {
            const nextIndex = replaceMemoryIndexEntry(
              readableMemoryIndex.content,
              recalledPlanningPath,
              memoryName,
              memoryDescription,
            );
            if (!nextIndex) {
              yield { type: "output_text.delta", delta: "Planning Memory 索引无法安全同步，未提交修改。" };
              return;
            }
            actions.push(
              {
                actionId: `daily-study-direction-${proposalSequence}`,
                idempotencyKey: `daily-study-direction-${proposalSequence}`,
                operation: "exact_replace",
                path: recalledPlanningPath,
                expectedVersion: recalledPlanningRead.result.value.modifiedVersion,
                expectedContent: recalledPlanningRead.result.value.content,
                replacement: studyMemoryDocument(
                  memoryName,
                  memoryDescription,
                  explicitDirection,
                  context.resolvedDate,
                ),
              },
              {
                actionId: `daily-study-index-${proposalSequence}`,
                idempotencyKey: `daily-study-index-${proposalSequence}`,
                operation: "exact_replace",
                path: "memory/MEMORY.md",
                expectedVersion: readableMemoryIndex.modifiedVersion,
                expectedContent: readableMemoryIndex.content,
                replacement: nextIndex,
              },
            );
          } else if (
            readableMemoryIndex ||
            (memoryIndexRead && !memoryIndexRead.result.ok && memoryIndexRead.result.error.code === "not_found")
          ) {
            const memoryPath = "memory/study/rag-evaluation.md";
            const indexEntry = `- [${memoryName}](study/rag-evaluation.md) - ${memoryDescription}`;
            actions.push(
              {
                actionId: `daily-study-direction-${proposalSequence}`,
                idempotencyKey: `daily-study-direction-${proposalSequence}`,
                operation: "create",
                path: memoryPath,
                expectedVersion: "missing",
                content: studyMemoryDocument(
                  memoryName,
                  memoryDescription,
                  explicitDirection,
                  context.resolvedDate,
                ),
              },
              readableMemoryIndex
                ? {
                    actionId: `daily-study-index-${proposalSequence}`,
                    idempotencyKey: `daily-study-index-${proposalSequence}`,
                    operation: "exact_replace",
                    path: "memory/MEMORY.md",
                    expectedVersion: readableMemoryIndex.modifiedVersion,
                    expectedContent: readableMemoryIndex.content,
                    replacement: `${readableMemoryIndex.content.trimEnd()}\n${indexEntry}\n`,
                  }
                : {
                    actionId: `daily-study-index-${proposalSequence}`,
                    idempotencyKey: `daily-study-index-${proposalSequence}`,
                    operation: "create",
                    path: "memory/MEMORY.md",
                    expectedVersion: "missing",
                    content: `# Planning Memory\n\n${indexEntry}\n`,
                  },
            );
          } else {
            yield { type: "output_text.delta", delta: "Planning Memory 索引不可用，未提交修改。" };
            return;
          }
        } else if (
          usesPlanningMemory && recalledPlanningPath?.startsWith("memory/study/") && recalledPlanningRead?.result.ok &&
          recalledPlanningRead.result.value.type === "vault_read" && readableMemoryIndex
        ) {
          const frontmatter = /^---[\s\S]*?---/.exec(recalledPlanningRead.result.value.content)?.[0];
          if (!frontmatter) {
            yield { type: "output_text.delta", delta: "Study Memory 元数据无效，未提交修改。" };
            return;
          }
          actions.push(
            {
              actionId: `daily-study-direction-${proposalSequence}`,
              idempotencyKey: `daily-study-direction-${proposalSequence}`,
              operation: "exact_replace",
              path: recalledPlanningPath,
              expectedVersion: recalledPlanningRead.result.value.modifiedVersion,
              expectedContent: recalledPlanningRead.result.value.content,
              replacement: `${frontmatter}\n\nCurrent direction: ${topic}\nLast planned for ${context.resolvedDate}. Detailed schedules remain in Daily Notes.\n`,
            },
            {
              actionId: `daily-study-index-${proposalSequence}`,
              idempotencyKey: `daily-study-index-${proposalSequence}`,
              operation: "exact_replace",
              path: "memory/MEMORY.md",
              expectedVersion: readableMemoryIndex.modifiedVersion,
              expectedContent: readableMemoryIndex.content,
              replacement: readableMemoryIndex.content,
            },
          );
        } else if (
          usesPlanningMemory && recalledPlanningPath?.startsWith("memory/project/") && readableMemoryIndex
        ) {
          const memoryName = "Project-informed daily study direction";
          const memoryDescription = "Current study direction derived from relevant Project Memory";
          const indexEntry = `- [${memoryName}](study/project-informed-direction.md) - ${memoryDescription}`;
          if (projectDerivedRead?.result.ok && projectDerivedRead.result.value.type === "vault_read") {
            actions.push(
              {
                actionId: `daily-study-direction-${proposalSequence}`,
                idempotencyKey: `daily-study-direction-${proposalSequence}`,
                operation: "exact_replace",
                path: projectDerivedPath!,
                expectedVersion: projectDerivedRead.result.value.modifiedVersion,
                expectedContent: projectDerivedRead.result.value.content,
                replacement: studyMemoryDocument(memoryName, memoryDescription, topic, context.resolvedDate),
              },
              {
                actionId: `daily-study-index-${proposalSequence}`,
                idempotencyKey: `daily-study-index-${proposalSequence}`,
                operation: "exact_replace",
                path: "memory/MEMORY.md",
                expectedVersion: readableMemoryIndex.modifiedVersion,
                expectedContent: readableMemoryIndex.content,
                replacement: readableMemoryIndex.content,
              },
            );
          } else if (projectDerivedRead && !projectDerivedRead.result.ok && projectDerivedRead.result.error.code === "not_found") {
            actions.push(
              {
                actionId: `daily-study-direction-${proposalSequence}`,
                idempotencyKey: `daily-study-direction-${proposalSequence}`,
                operation: "create",
                path: projectDerivedPath!,
                expectedVersion: "missing",
                content: studyMemoryDocument(memoryName, memoryDescription, topic, context.resolvedDate),
              },
              {
                actionId: `daily-study-index-${proposalSequence}`,
                idempotencyKey: `daily-study-index-${proposalSequence}`,
                operation: "exact_replace",
                path: "memory/MEMORY.md",
                expectedVersion: readableMemoryIndex.modifiedVersion,
                expectedContent: readableMemoryIndex.content,
                replacement: `${readableMemoryIndex.content.trimEnd()}\n${indexEntry}\n`,
              },
            );
          } else {
            yield { type: "output_text.delta", delta: "Project 派生的 Study Memory 无法安全读取，未提交修改。" };
            return;
          }
        }
        yield {
          type: "local_tool_call",
          callId: `fake-daily-plan-change-${proposalSequence}`,
          name: "vault_propose_changes",
          arguments: {
            batchId: `daily-study-plan-batch-${proposalSequence}`,
            idempotencyKey: `daily-study-plan-batch-${proposalSequence}`,
            task: "Create or conservatively fill today's grounded Daily Study Plan",
            actions,
          },
        };
        return;
      }
      yield {
        type: "output_text.delta",
        delta: priorResponse
          ? `OfferAgent confirmed: ${priorResponse.text}`
          : "OfferAgent cannot confirm without prior context.",
      };
      return;
    }
    if (userInput.trim() === "hosted_search_demo") {
      yield {
        type: "hosted_web_search_call",
        callId: "fake-hosted-search",
        sources: [{ url: "https://example.com/source", title: "Example source" }],
      };
      yield { type: "output_text.delta", delta: "A cited answer [1]" };
      yield {
        type: "url_citation",
        citation: {
          url: "https://example.com/source",
          title: "Example source",
          startIndex: 15,
          endIndex: 18,
        },
      };
      return;
    }
    if (userInput.trim() === "citation_then_tool") {
      const toolResult = findLatest(
        request.input,
        (item): item is Extract<ModelConversationItem, { type: "local_tool_result" }> =>
          item.type === "local_tool_result" && item.callId.startsWith("fake-citation-read-"),
      );
      if (!toolResult) {
        this.#toolCallSequence += 1;
        yield { type: "output_text.delta", delta: "Old cited [1]" };
        yield {
          type: "url_citation",
          citation: {
            url: "https://example.com/old",
            title: "Old source",
            startIndex: 10,
            endIndex: 13,
          },
        };
        yield {
          type: "local_tool_call",
          callId: `fake-citation-read-${this.#toolCallSequence}`,
          name: "vault_read",
          arguments: { path: "notes/citation.md", lineStart: 1, lineEnd: 1 },
        };
        return;
      }
      yield { type: "output_text.delta", delta: "Final answer without a citation" };
      return;
    }
    const staleFlow = /^stale_evidence_flow\s+([^\s]+)$/i.exec(userInput.trim());
    if (staleFlow) {
      const calls = request.input.filter((item) => item.type === "local_tool_call");
      const results = request.input.filter((item) => item.type === "local_tool_result");
      const successfulRead = results.find(
        (item) => item.result.ok && item.result.value.type === "vault_read",
      );
      const staleResult = results.find(
        (item) => !item.result.ok && item.result.error.code === "stale_evidence",
      );
      const hasSearchCall = calls.some((item) => item.name === "vault_search");
      if (!successfulRead && !staleResult) {
        this.#toolCallSequence += 1;
        yield {
          type: "local_tool_call",
          callId: `fake-stale-read-${this.#toolCallSequence}`,
          name: "vault_read",
          arguments: { path: `${staleFlow[1]} `, lineStart: 1, lineEnd: 1 },
        };
        return;
      }
      if (successfulRead && !hasSearchCall) {
        this.#toolCallSequence += 1;
        yield {
          type: "local_tool_call",
          callId: `fake-stale-search-${this.#toolCallSequence}`,
          name: "vault_search",
          arguments: { query: "content", exactPhrase: true },
        };
        return;
      }
      if (staleResult && !successfulRead) {
        this.#toolCallSequence += 1;
        yield {
          type: "local_tool_call",
          callId: `fake-stale-reread-${this.#toolCallSequence}`,
          name: "vault_read",
          arguments: { path: staleFlow[1], lineStart: 1, lineEnd: 1 },
        };
        return;
      }
      if (staleResult && successfulRead) {
        const readContents = results
          .filter(
            (item) => item.result.ok && item.result.value.type === "vault_read",
          )
          .map((item) =>
            item.result.ok && item.result.value.type === "vault_read"
              ? item.result.value.content
              : "",
          );
        yield { type: "output_text.delta", delta: "OfferAgent received: " };
        yield { type: "output_text.delta", delta: JSON.stringify(readContents) };
        return;
      }
    }
    const toolResult = findLatest(
      request.input,
      (item): item is Extract<ModelConversationItem, { type: "local_tool_result" }> =>
        item.type === "local_tool_result",
    );
    if (!toolResult) {
      const toolRequest = fakeToolRequest(userInput);
      if (toolRequest) {
        this.#toolCallSequence += 1;
        yield {
          type: "local_tool_call",
          callId: `fake-${toolRequest.name}-call-${this.#toolCallSequence}`,
          name: toolRequest.name,
          arguments: toolRequest.arguments,
        };
        return;
      }
    }
    yield { type: "output_text.delta", delta: "OfferAgent received: " };
    if (request.signal.aborted) {
      throw new ModelProviderError("transport_error", "The Agent Run was interrupted.");
    }
    yield {
      type: "output_text.delta",
      delta: toolResult ? JSON.stringify(toolResult.result) : userInput,
    };
  }
}

function dailyPlanTopic(content: string): string | undefined {
  const unfinished = /^\s*-\s*\[(?: |~)\]\s*(.+)$/m.exec(content)?.[1]?.trim();
  if (unfinished) return unfinished;
  if (/^\s*-\s*\[[^\]]+\]/m.test(content)) return undefined;
  return content.split(/\r?\n/).map((line) => line.trim()).find(
    (line) => line && !line.startsWith("#") && !/^[-*>]+$/.test(line),
  );
}

function recalledPlanningPathFrom(instructions: string, request: string): string | undefined {
  const requestFeatures = textFeatures(request);
  return [...instructions.matchAll(
    /^## (.+) \[(?:study|project)\] \((memory\/(?:study|project)\/[^)]+\.md)\)$/gm,
  )]
    .map((match, index) => ({
      index,
      path: match[2]!,
      score: overlapScore(requestFeatures, textFeatures(match[1]!)),
    }))
    .sort((left, right) => right.score - left.score || left.index - right.index)[0]?.path;
}

function studyMemoryDirection(content: string): string | undefined {
  const line = content.replace(/^---[\s\S]*?---\s*/, "").split(/\r?\n/)
    .map((line) => line.trim())
    .find((candidate) => candidate && !candidate.startsWith("Last planned for "));
  return /^Current direction:\s*(.+)$/.exec(line ?? "")?.[1]?.trim() ?? line;
}

function studyMemoryDocument(
  name: string,
  description: string,
  direction: string,
  resolvedDate: string,
): string {
  return [
    "---",
    `name: ${JSON.stringify(name)}`,
    `description: ${JSON.stringify(description)}`,
    "type: study",
    "---",
    "",
    `Current direction: ${direction}`,
    `Last planned for ${resolvedDate}. Detailed schedules remain in Daily Notes.`,
    "",
  ].join("\n");
}

function replaceMemoryIndexEntry(
  content: string,
  memoryPath: string,
  name: string,
  description: string,
): string | undefined {
  const relativePath = memoryPath.replace(/^memory\//, "");
  let replaced = false;
  const lines = content.split(/\r?\n/).map((line) => {
    const target = /^-\s+\[[^\]]+\]\(([^)]+)\)\s+-\s+.+$/.exec(line)?.[1];
    if (target !== relativePath) return line;
    replaced = true;
    return `- [${name}](${relativePath}) - ${description}`;
  });
  return replaced ? lines.join("\n") : undefined;
}

function toolResultFor(
  input: ModelConversationItem[],
  name: LocalToolName,
  predicate: (call: Extract<ModelConversationItem, { type: "local_tool_call" }>) => boolean = () => true,
): Extract<ModelConversationItem, { type: "local_tool_result" }> | undefined {
  for (let index = input.length - 1; index >= 0; index -= 1) {
    const result = input[index];
    if (result.type !== "local_tool_result") continue;
    const call = input.find(
      (candidate): candidate is Extract<ModelConversationItem, { type: "local_tool_call" }> =>
        candidate.type === "local_tool_call" &&
        candidate.callId === result.callId &&
        candidate.name === name,
    );
    if (call && predicate(call)) return result;
  }
  return undefined;
}

function toolResultForAfter(
  input: ModelConversationItem[],
  name: LocalToolName,
  afterIndex: number,
  predicate: (call: Extract<ModelConversationItem, { type: "local_tool_call" }>) => boolean,
): Extract<ModelConversationItem, { type: "local_tool_result" }> | undefined {
  for (let index = input.length - 1; index > afterIndex; index -= 1) {
    const result = input[index];
    if (result.type !== "local_tool_result") continue;
    const call = input.find(
      (candidate, callIndex): candidate is Extract<ModelConversationItem, { type: "local_tool_call" }> =>
        callIndex > afterIndex &&
        candidate.type === "local_tool_call" &&
        candidate.callId === result.callId &&
        candidate.name === name,
    );
    if (call && predicate(call)) return result;
  }
  return undefined;
}

function dailyPlanAction(
  context: DailyNoteContextResult,
  targetRead: Extract<ModelConversationItem, { type: "local_tool_result" }> | undefined,
  planItems: string,
  replaceExisting = false,
): Record<string, unknown> | null | undefined {
  const heading = "## 今日学习计划";
  if (!context.targetExists) {
    const template = (context.templateContent ?? `# ${context.resolvedDate}\n\n${heading}\n`)
      .replaceAll("{{date}}", context.resolvedDate)
      .replaceAll("{{title}}", context.resolvedDate);
    const content = template.includes(heading)
      ? template.replace(`${heading}\n`, `${heading}\n\n${planItems}\n`)
      : `${template.trimEnd()}\n\n${heading}\n\n${planItems}\n`;
    return {
      actionId: "daily-study-plan-create",
      idempotencyKey: "daily-study-plan-create",
      operation: "create",
      path: context.targetPath,
      expectedVersion: "missing",
      content,
    };
  }
  if (!targetRead?.result.ok || targetRead.result.value.type !== "vault_read") return undefined;
  const current = targetRead.result.value.content;
  const sectionStart = current.indexOf(heading);
  if (sectionStart >= 0) {
    const nextHeading = current.indexOf("\n## ", sectionStart + heading.length);
    const sectionEnd = nextHeading >= 0 ? nextHeading : current.length;
    const currentSection = current.slice(sectionStart, sectionEnd);
    if (replaceExisting && currentSection.trim() !== heading) {
      return {
        actionId: "daily-study-plan-replace",
        idempotencyKey: "daily-study-plan-replace",
        operation: "exact_replace",
        path: context.targetPath,
        expectedVersion: targetRead.result.value.modifiedVersion,
        expectedContent: currentSection,
        replacement: `${heading}\n\n${planItems}\n`,
      };
    }
    if (currentSection.trim() === heading) {
      return {
        actionId: "daily-study-plan-fill",
        idempotencyKey: "daily-study-plan-fill",
        operation: "exact_replace",
        path: context.targetPath,
        expectedVersion: targetRead.result.value.modifiedVersion,
        expectedContent: currentSection,
        replacement: `${heading}\n\n${planItems}\n`,
      };
    }
    const missingPlanLines = planItems
      .split("\n")
      .filter((line) => line.trim() && !currentSection.includes(line));
    if (missingPlanLines.length === 0) return null;
    return {
      actionId: "daily-study-plan-merge",
      idempotencyKey: "daily-study-plan-merge",
      operation: "exact_replace",
      path: context.targetPath,
      expectedVersion: targetRead.result.value.modifiedVersion,
      expectedContent: currentSection,
      replacement: `${currentSection.trimEnd()}\n${missingPlanLines.join("\n")}\n`,
    };
  }
  return {
    actionId: "daily-study-plan-append",
    idempotencyKey: "daily-study-plan-append",
    operation: "append",
    path: context.targetPath,
    expectedVersion: targetRead.result.value.modifiedVersion,
    content: `\n\n${heading}\n\n${planItems}\n`,
  };
}

function textFeatures(value: string): Set<string> {
  const normalized = value.toLocaleLowerCase().replace(/\s+/g, " ").trim();
  const features = new Set(normalized.match(/[\p{L}\p{N}]{2,}/gu) ?? []);
  const compact = normalized.replace(/[^\p{L}\p{N}]/gu, "");
  for (let size = 2; size <= 4; size += 1) {
    for (let index = 0; index + size <= compact.length; index += 1) {
      features.add(compact.slice(index, index + size));
    }
  }
  return features;
}

function overlapScore(left: Set<string>, right: Set<string>): number {
  let score = 0;
  for (const feature of right) if (left.has(feature)) score += feature.length;
  return score;
}

function fakeToolRequest(input: string): { name: LocalToolName; arguments: unknown } | undefined {
  const dailyContext = /^daily_note_context(?:\s+([^\s]+))?$/i.exec(input.trim());
  if (dailyContext) {
    return {
      name: "daily_note_context",
      arguments: dailyContext[1] ? { date: dailyContext[1] } : {},
    };
  }
  const webRead = /^web_read\s+(.+)$/i.exec(input.trim());
  if (webRead) {
    return { name: "web_read", arguments: { url: webRead[1].trim() } };
  }
  const change = /^vault_propose_changes\s+([\s\S]+)$/i.exec(input.trim());
  if (change) {
    try {
      return { name: "vault_propose_changes", arguments: JSON.parse(change[1]) as unknown };
    } catch {
      return { name: "vault_propose_changes", arguments: change[1] };
    }
  }
  const skill = /^skill_read\s+([^\s]+)(?:\s+(.+))?$/i.exec(input.trim());
  if (skill) {
    return {
      name: "skill_read",
      arguments: {
        skill: skill[1],
        ...(skill[2] ? { resource: skill[2].trim() } : {}),
      },
    };
  }
  const read = /^vault_read\s+([^\s]+)(?:\s+(\d+)-(\d+))?$/i.exec(input.trim());
  if (read) {
    return {
      name: "vault_read",
      arguments: {
        path: read[1],
        ...(read[2] ? { lineStart: Number(read[2]), lineEnd: Number(read[3]) } : {}),
      },
    };
  }
  if (/^vault_list(?:\s|$)/i.test(input.trim())) {
    const directory = input.trim().slice("vault_list".length).trim();
    return {
      name: "vault_list",
      arguments: directory ? { directory } : {},
    };
  }
  const search = /^vault_search\s+(.+)$/i.exec(input.trim());
  if (search) {
    return {
      name: "vault_search",
      arguments: { query: search[1] },
    };
  }
  return undefined;
}
