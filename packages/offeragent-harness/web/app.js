(() => {
  "use strict";

  const PROTOCOL_VERSION = "1.0";
  const SCHEMA_HASH = "sha256:28c500ce7f0557958ee320492ac2f001ad4dcb4ba279992a4772d12b31308493";
  const CLIENT_VERSION = "0.1.0";
  const CODEX_SUBSCRIPTION_PROVIDER = "codex-subscription-experimental";
  const ARTIFACT_PAGE_BYTES = 65_536;
  const ARTIFACT_TOTAL_BYTES = 524_288;
  const POLL_INTERVAL_MS = 750;
  const state = {
    csrf: null,
    workspaceId: null,
    workerPid: null,
    initialized: false,
    capabilities: {},
    sessions: [],
    sessionId: null,
    sessionEpoch: 0,
    runIds: [],
    runs: new Map(),
    pendingSubmissions: [],
    cursors: new Map(),
    artifacts: new Map(),
    models: new Map(),
    configuredModelId: null,
    configuredAccountBinding: null,
    catalogAccountBinding: null,
    selectedModelKey: null,
    modelHealth: null,
    skillCatalog: null,
    skillStatus: null,
    shellCatalog: null,
    hookCatalog: null,
    artifactView: null,
    poll: null,
    pollTick: 0,
    stopped: false,
    commandPending: 0,
  };

  const el = (id) => document.getElementById(id);
  const sessionsEl = el("sessions");
  const timelineEl = el("timeline");
  const statusEl = el("status");
  const promptEl = el("prompt");
  const sendEl = el("send");
  const steerEl = el("steer");
  const cancelEl = el("cancel");
  const inspectorEl = el("inspector");
  const modelEl = el("model");
  const modelHealthEl = el("model-health");
  const modelStatusEl = el("model-status");

  async function boot() {
    try {
      const token = location.hash.slice(1);
      if (!/^[A-Za-z0-9_-]{20,512}$/.test(token)) {
        throw new Error("启动链接缺少有效的一次性令牌，请从 Obsidian 重新打开。");
      }
      const exchange = await jsonFetch("/auth/exchange", { token }, false);
      state.csrf = requiredText(exchange.csrfToken, "csrfToken");
      state.workspaceId = requiredText(exchange.workspaceId, "workspaceId");
      state.workerPid = integer(exchange.workerPid, "workerPid", 1);
      history.replaceState(null, "", "/");

      const result = await command(
        "initialize",
        {
          protocolVersion: PROTOCOL_VERSION,
          clientVersion: CLIENT_VERSION,
          workspaceId: state.workspaceId,
          capabilities: {
            eventReplay: true,
            multiSession: true,
            approvals: true,
            skills: true,
            shell: true,
            hooks: true,
            subagents: true,
            artifacts: true,
            loopbackWeb: true,
            contentBlocks: true,
            cancellation: true,
            diagnostics: true,
          },
          supportedProtocolRange: { minimum: PROTOCOL_VERSION, maximum: PROTOCOL_VERSION },
          requiredCapabilities: ["eventReplay", "multiSession", "approvals", "artifacts", "cancellation"],
          schemaHash: SCHEMA_HASH,
        },
        true,
      );
      if (
        result.protocolVersion !== PROTOCOL_VERSION ||
        result.schemaHash !== SCHEMA_HASH ||
        result.workspaceId !== state.workspaceId ||
        result.workerPid !== state.workerPid ||
        result.transport !== "loopback-http"
      ) {
        throw new Error("Runtime 身份或协议与页面不一致，请升级或回滚。");
      }
      if (!result.capabilities || typeof result.capabilities !== "object" || Array.isArray(result.capabilities)) {
        throw new Error("Runtime 未返回协商后的能力快照。");
      }
      state.capabilities = { ...result.capabilities };
      state.initialized = true;
      setStatus(`同一 Worker PID ${state.workerPid} · Runtime ${result.runtimeVersion}`);

      const controls = await Promise.allSettled([loadModels()]);
      if (controls[0].status === "rejected") {
        modelStatusEl.textContent = `模型目录不可用：${safeError(controls[0].reason)}`;
        modelStatusEl.classList.add("error");
      }
      await loadSessions({ selectIfNeeded: true });
      startPolling();
    } catch (error) {
      setStatus(safeError(error), true);
      renderEmpty("无法连接本地 Runtime", "请关闭此页面并从 Obsidian 的 OfferAgent 面板重新打开。", true);
    }
  }

  async function command(method, params, allowBeforeInitialize = false) {
    if (!allowBeforeInitialize && !state.initialized) throw new Error("本地 Runtime 尚未初始化");
    state.commandPending += 1;
    updateControls();
    try {
      const response = await jsonFetch("/api/command", { method, params }, true);
      if (!("result" in response)) throw new Error("Runtime 返回了无效命令结果");
      return response.result;
    } finally {
      state.commandPending -= 1;
      updateControls();
    }
  }

  async function jsonFetch(path, body, authenticated) {
    const headers = { "Content-Type": "application/json" };
    if (authenticated) headers["X-CSRF-Token"] = state.csrf;
    const response = await fetch(path, {
      method: "POST",
      credentials: "same-origin",
      cache: "no-store",
      headers,
      body: JSON.stringify(body),
    });
    const value = await response.json().catch(() => null);
    if (!response.ok || !value || typeof value !== "object" || Array.isArray(value)) {
      const code = typeof value?.error?.code === "string" ? ` (${value.error.code})` : "";
      const message =
        typeof value?.error?.userVisibleMessage === "string"
          ? value.error.userVisibleMessage
          : typeof value?.error?.message === "string"
            ? value.error.message
            : "本地 Runtime 请求失败";
      throw new Error(`${message}${code}`);
    }
    return value;
  }

  async function loadModels() {
    const [result, configSnapshot] = await Promise.all([
      command("models/list", { includeUnavailable: true }),
      command("config/get", { scope: "workspace" }),
    ]);
    const models = Array.isArray(result.models) ? result.models : [];
    const configuredModel = configSnapshot?.values?.model?.model;
    const configuredAccountBinding = configSnapshot?.values?.model?.account_binding;
    const catalogAccountBinding = result?.accountBinding;
    state.configuredModelId =
      typeof configuredModel === "string" && configuredModel.length > 0 && configuredModel.length <= 256
        ? configuredModel
        : null;
    state.configuredAccountBinding = /^sha256:[0-9a-f]{64}$/.test(configuredAccountBinding ?? "")
      ? configuredAccountBinding
      : null;
    state.catalogAccountBinding = /^sha256:[0-9a-f]{64}$/.test(catalogAccountBinding ?? "")
      ? catalogAccountBinding
      : null;
    state.models.clear();
    for (const descriptor of models) {
      if (!descriptor || typeof descriptor !== "object") continue;
      const provider = requiredText(descriptor.provider, "model.provider");
      if (provider !== CODEX_SUBSCRIPTION_PROVIDER) {
        throw new Error("Runtime 模型目录包含非 Codex Subscription Provider");
      }
      const model = requiredText(descriptor.model, "model.model");
      const key = model;
      state.models.set(key, {
        model,
        displayName: requiredText(descriptor.displayName, "model.displayName"),
        local: descriptor.local === true,
        available: descriptor.available === true,
        supportsStreaming: descriptor.supportsStreaming === true,
        supportsStructuredOutput: descriptor.supportsStructuredOutput === true,
        maxContextTokens: descriptor.maxContextTokens ?? null,
        accountBinding: state.catalogAccountBinding,
      });
    }
    renderModels();
    selectModel(state.configuredModelId);
  }

  function renderModels() {
    const options = [];
    for (const [key, descriptor] of state.models) {
      const suffix = `Codex Subscription${descriptor.available ? "" : " · 不可用"}`;
      const option = node("option", `${descriptor.displayName} — ${suffix}`);
      option.value = key;
      option.disabled = !descriptor.available ||
        key !== state.configuredModelId ||
        descriptor.accountBinding !== state.configuredAccountBinding;
      options.push(option);
    }
    if (!options.length) {
      const option = node("option", "没有已配置模型");
      option.value = "";
      option.disabled = true;
      options.push(option);
    }
    modelEl.replaceChildren(...options);
  }

  function selectModel(key) {
    const descriptor = key ? state.models.get(key) : null;
    state.selectedModelKey =
      key &&
      key === state.configuredModelId &&
      descriptor?.available &&
      descriptor.accountBinding === state.configuredAccountBinding
        ? key
        : null;
    modelEl.disabled = true;
    modelEl.value = state.selectedModelKey ?? "";
    modelHealthEl.disabled = state.selectedModelKey === null;
    state.modelHealth = null;
    if (state.selectedModelKey) {
      modelStatusEl.textContent = "Codex Subscription 模型 · 尚未检查";
      modelStatusEl.classList.remove("error", "healthy");
    } else {
      modelStatusEl.textContent = state.configuredModelId
        ? "请在 Obsidian 中为当前 Codex 账户重新选择模型"
        : "请先在 Obsidian 设置中选择当前目录中的模型";
      modelStatusEl.classList.add("error");
    }
    updateControls();
  }

  async function checkModelHealth() {
    const descriptor = selectedModel();
    if (!descriptor) throw new Error("尚未选择可用模型");
    modelStatusEl.textContent = "正在检查 Codex Subscription 模型…";
    modelStatusEl.classList.remove("error", "healthy");
    const result = await command("models/health", {
      provider: CODEX_SUBSCRIPTION_PROVIDER,
      model: descriptor.model,
      deadline: null,
      clientRequestId: opaque("req_model_health_"),
    });
    if (result.provider !== CODEX_SUBSCRIPTION_PROVIDER || result.model !== descriptor.model) {
      throw new Error("模型健康结果身份不匹配");
    }
    state.modelHealth = result;
    const latency = Number.isSafeInteger(result.latencyMs) ? ` · ${result.latencyMs} ms` : "";
    modelStatusEl.textContent = `${modelHealthText(result.status)}${latency}`;
    modelStatusEl.classList.toggle("healthy", result.status === "healthy");
    modelStatusEl.classList.toggle("error", !["healthy", "degraded"].includes(result.status));
  }

  async function loadSessions({ selectIfNeeded = false } = {}) {
    const result = await command("session/list", { cursor: null, limit: 100, includeDeleted: false });
    state.sessions = Array.isArray(result.sessions) ? result.sessions : [];
    renderSessions();
    if (!selectIfNeeded) return;
    const remembered = sessionStorage.getItem(sessionStorageKey());
    const candidate = state.sessions.find((session) => session.sessionId === remembered) ?? state.sessions[0];
    if (candidate) await selectSession(candidate.sessionId);
    else {
      state.sessionId = null;
      renderEmpty("开始一个本地会话", "你的 Session、事件和 Artifact 只保存在当前 Worker 的本地 SQLite 中。", false);
      updateControls();
    }
  }

  async function createSession() {
    const result = await command("session/create", { title: null, clientRequestId: opaque("req_") });
    await loadSessions();
    await selectSession(result.session.sessionId);
  }

  async function selectSession(sessionId) {
    const selectedSessionId = requiredText(sessionId, "sessionId");
    const epoch = ++state.sessionEpoch;
    state.sessionId = selectedSessionId;
    sessionStorage.setItem(sessionStorageKey(), state.sessionId);
    state.runIds = [];
    state.runs.clear();
    state.pendingSubmissions = [];
    state.cursors.clear();
    state.artifacts.clear();
    renderSessions();
    renderEmpty("正在加载会话", "正在从同一 Worker 的 SQLite 重放持久事件…", false);

    const detail = await command("session/get", { sessionId: state.sessionId, includeTurns: true });
    if (epoch !== state.sessionEpoch || state.sessionId !== selectedSessionId) return;
    const session = detail.session;
    if (!session || session.summary?.sessionId !== selectedSessionId || !Array.isArray(session.turns)) {
      throw new Error("Session 详情身份或结构无效");
    }
    upsertSessionSummary(session.summary);
    await replaySession(selectedSessionId, epoch);
    if (epoch !== state.sessionEpoch) return;
    renderTimeline();
  }

  async function renameSession() {
    const summary = currentSession();
    if (!summary) return;
    const proposed = window.prompt("新的会话标题", summary.title);
    if (proposed === null) return;
    const title = proposed.trim();
    if (!title || title.length > 512) throw new Error("会话标题必须为 1–512 个字符");
    const result = await command("session/rename", {
      sessionId: summary.sessionId,
      title,
      expectedUpdatedAt: summary.updatedAt,
    });
    upsertSessionSummary(result.session);
    renderSessions();
    setStatus(`会话已重命名为“${result.session.title}”`);
  }

  async function deleteSession() {
    const summary = currentSession();
    if (!summary) return;
    if (!window.confirm(`删除会话“${summary.title}”？活动 Run 会收到取消请求，历史将软删除。`)) return;
    const result = await command("session/delete", { sessionId: summary.sessionId, hardDelete: false });
    if (!result.deleted) throw new Error("Runtime 未确认会话删除");
    const cancelled = Array.isArray(result.activeRunsCancelRequested) ? result.activeRunsCancelRequested.length : 0;
    state.sessionId = null;
    state.sessionEpoch += 1;
    state.runIds = [];
    state.runs.clear();
    state.cursors.clear();
    sessionStorage.removeItem(sessionStorageKey());
    await loadSessions({ selectIfNeeded: true });
    setStatus(`会话已删除${cancelled ? `，已请求取消 ${cancelled} 个活动 Run` : ""}`);
  }

  async function compactSession() {
    if (!state.sessionId) return;
    const result = await command("session/compact", {
      sessionId: state.sessionId,
      throughTurnId: null,
      force: false,
    });
    if (result.boundaryArtifact) registerArtifacts([result.boundaryArtifact]);
    setStatus(
      result.compacted
        ? `会话已压缩，替换 ${result.replacedTurnCount} 个旧 Turn；原始 Event/Artifact 保留。`
        : "当前会话无需压缩。",
    );
    if (result.boundaryArtifact?.artifactId) await showArtifact(result.boundaryArtifact.artifactId);
  }

  async function forkRun(run) {
    if (!run.sessionId || !run.turnId) throw new Error("Run 缺少可 Fork 的 Session/Turn 身份");
    const result = await command("session/fork", {
      sessionId: run.sessionId,
      forkTurnId: run.turnId,
      forkRunId: run.runId,
      title: null,
      clientRequestId: opaque("req_"),
    });
    await loadSessions();
    await selectSession(result.session.sessionId);
    setStatus(`已从 Run ${shortId(run.runId)} 创建独立会话分支`);
  }

  async function retryRun(run) {
    if (!run.sessionId || !run.turnId || !terminal(run.status)) throw new Error("只能重试已结束的 Run");
    const result = await command("turn/retry", {
      sessionId: run.sessionId,
      turnId: run.turnId,
      sourceRunId: run.runId,
      idempotencyKey: opaque("retry_"),
      runConfig: null,
    });
    if (!state.runIds.includes(result.runId)) state.runIds.push(result.runId);
    const created = emptyRun(result.runId);
    created.sessionId = result.sessionId;
    created.turnId = result.turnId;
    state.runs.set(result.runId, created);
    await replaySession(state.sessionId, state.sessionEpoch);
    renderTimeline();
    setStatus(run.status === "interrupted" ? "已从安全持久状态创建恢复 Run" : "已创建新的重试 Run");
  }

  async function sendTurn(message) {
    const textValue = message.trim();
    if (!textValue) return;
    const model = selectedModel();
    if (!model) throw new Error("请先选择一个 Runtime 返回的可用模型");
    if (!state.sessionId) await createSession();
    const turnId = opaque("turn_");
    state.pendingSubmissions.push({ sessionId: state.sessionId, turnId, text: textValue });
    renderTimeline();
    let result;
    try {
      result = await command("turn/start", {
        sessionId: state.sessionId,
        turnId,
        idempotencyKey: opaque("turn_"),
        input: [{ type: "text", text: textValue }],
        runConfig: {
          model: model.model,
          reasoningEffort: el("reasoning").value,
          permissionMode: el("permission").value,
        },
        deadline: null,
      });
    } catch (error) {
      state.pendingSubmissions = state.pendingSubmissions.filter((pending) => pending.turnId !== turnId);
      renderTimeline();
      throw error;
    }
    if (!state.runIds.includes(result.runId)) state.runIds.push(result.runId);
    const run = emptyRun(result.runId);
    run.sessionId = result.sessionId;
    run.turnId = result.turnId;
    state.runs.set(result.runId, run);
    promptEl.value = "";
    renderTimeline();
    updateControls();
  }

  async function steerActive() {
    const run = activeRootRun();
    const textValue = promptEl.value.trim();
    if (!run || !textValue) return;
    const result = await command("turn/steer", {
      runId: run.runId,
      messageId: opaque("msg_"),
      input: [{ type: "text", text: textValue }],
      mode: "steer",
    });
    if (!result.accepted) throw new Error("Runtime 未接受本次 Run 调整");
    promptEl.value = "";
    setStatus(`调整将在事件序列 ${result.applyAfterSequence} 后应用`);
    updateControls();
  }

  async function cancelActive() {
    const run = activeRootRun();
    if (!run) return;
    await command("turn/cancel", {
      sessionId: run.sessionId,
      turnId: run.turnId,
      runId: run.runId,
      reason: "用户从本地 Web UI 取消",
    });
  }

  async function replaySession(sessionId = state.sessionId, epoch = state.sessionEpoch) {
    if (!sessionId) return;
    let cursors = Object.fromEntries(state.cursors);
    for (;;) {
      const result = await command("events/replay", {
        sessionId,
        runId: null,
        afterSequence: 0,
        runCursors: cursors,
        limit: 1000,
        types: [],
      });
      if (epoch !== state.sessionEpoch || sessionId !== state.sessionId) return;
      const events = Array.isArray(result.events) ? result.events : [];
      for (const event of events) reduceEvent(event);
      if (
        result.lastSequence !== null ||
        !result.runCursors ||
        typeof result.runCursors !== "object" ||
        Array.isArray(result.runCursors)
      ) {
        throw new Error("Session replay 返回了错误的游标形状");
      }
      const next = {};
      for (const [runId, sequence] of Object.entries(result.runCursors)) {
        if (!validRunId(runId)) throw new Error("Session replay Run 标识无效");
        next[runId] = integer(sequence, `runCursors.${runId}`, 0);
      }
      let advanced = false;
      for (const [runId, sequence] of Object.entries(cursors)) {
        if (!(runId in next) || next[runId] < sequence) throw new Error("Session replay Run 游标回退");
        if (next[runId] > sequence) advanced = true;
      }
      if (Object.keys(next).some((runId) => !(runId in cursors) && next[runId] > 0)) advanced = true;
      cursors = next;
      state.cursors = new Map(Object.entries(cursors));
      if (!result.hasMore) break;
      if (!events.length || !advanced) throw new Error("事件 replay 分页没有取得进展");
    }
  }

  function startPolling() {
    clearInterval(state.poll);
    state.poll = setInterval(async () => {
      if (state.stopped || state.commandPending) return;
      try {
        if (state.sessionId) {
          await replaySession();
          renderTimeline();
        }
        state.pollTick += 1;
        if (state.pollTick % 8 === 0) {
          await loadSessions();
        }
      } catch (error) {
        setStatus(safeError(error), true);
      }
    }, POLL_INTERVAL_MS);
  }

  function reduceEvent(event) {
    if (
      !event ||
      typeof event !== "object" ||
      event.workspaceId !== state.workspaceId ||
      !Number.isSafeInteger(event.sequence)
    ) {
      return;
    }
    if (event.type === "session.updated") {
      if (event.payload?.session) upsertSessionSummary(event.payload.session);
      if (event.runId == null) return;
    }
    if (event.runId == null) return;
    if (!validRunId(event.runId)) throw new Error("事件包含无效 Run ID");
    const run = state.runs.get(event.runId) ?? emptyRun(event.runId);
    if (run.lastSequence >= event.sequence) return;
    if (run.lastSequence + 1 !== event.sequence) throw new Error(`Run ${shortId(event.runId)} 的事件序列存在缺口`);
    if (
      run.lineageBound &&
      (run.sessionId !== event.sessionId || run.turnId !== event.turnId || run.parentRunId !== event.parentRunId)
    ) {
      throw new Error("Run lineage changed");
    }
    run.sessionId = event.sessionId;
    run.turnId = event.turnId;
    run.rootRunId = event.rootRunId;
    run.parentRunId = event.parentRunId;
    run.lineageBound = true;
    const payload = event.payload ?? {};

    if (event.type === "turn.started") {
      const content = contentProjection(payload.input);
      appendTimelineItem(run, {
        kind: "user_message",
        itemId: `turn:${run.turnId}`,
        sequence: event.sequence,
        source: "turn",
        blocks: content.text,
      });
      registerArtifacts(content.artifacts);
      run.startedAt = event.timestamp;
      run.status = "running";
      state.pendingSubmissions = state.pendingSubmissions.filter((pending) => pending.turnId !== run.turnId);
    } else if (event.type === "turn.steered") {
      const content = contentProjection(payload.input);
      appendTimelineItem(run, {
        kind: "user_message",
        itemId: `steer:${requiredText(payload.messageId, "messageId")}`,
        sequence: event.sequence,
        source: "steer",
        blocks: content.text,
      });
      registerArtifacts(content.artifacts);
    } else if (event.type === "phase.changed") {
      run.phase = payload.phase;
    } else if (event.type === "reasoning.summary") {
      const summary = requiredText(payload.summary, "reasoning summary");
      const partial = payload.partial === true;
      const existing = run.timeline.find((item) => item.kind === "reasoning");
      if (!existing) {
        appendTimelineItem(run, {
          kind: "reasoning",
          itemId: `reasoning:${event.sequence}`,
          sequence: event.sequence,
          summary,
          partial,
        });
      } else if (partial) {
        if (!existing.partial) throw new Error("已完成的推理摘要不能继续追加");
        existing.summary += summary;
      } else {
        if (!existing.partial || existing.summary !== summary) throw new Error("推理摘要最终快照不匹配");
        existing.partial = false;
      }
    } else if (event.type === "assistant.delta") {
      const index = integer(payload.blockIndex, "blockIndex", 0);
      const offset = integer(payload.offset, "offset", 0);
      const assistant = requireAssistantItem(run, event.sequence);
      while (assistant.blocks.length <= index) assistant.blocks.push("");
      if (assistant.blocks[index].length !== offset) throw new Error("文本 delta 不连续");
      assistant.blocks[index] += requiredText(payload.delta, "assistant delta");
    } else if (event.type === "assistant.completed") {
      applyAssistantContent(run, payload.content, event.sequence);
    } else if (event.type === "tool.calls.accepted") {
      if (!Array.isArray(payload.calls) || payload.calls.length === 0) throw new Error("工具接受事件缺少调用记录");
      for (const call of payload.calls) {
        const id = requiredText(call?.toolCallId, "toolCallId");
        appendTimelineItem(run, {
          kind: "tool_call",
          itemId: `tool:${id}`,
          sequence: event.sequence,
          toolCallId: id,
          name: requiredText(call.name, "tool name"),
          version: requiredText(call.version, "tool version"),
          arguments: call.arguments ?? {},
          status: "accepted",
          result: null,
          error: null,
          artifacts: [],
        });
      }
    } else if (event.type === "tool.started") {
      const call = payload.call ?? {};
      const id = requiredText(call.toolCallId, "toolCallId");
      const tool = requireToolItem(run, id);
      if (tool.name !== call.name || tool.version !== call.version) throw new Error("工具身份在开始后改变");
      tool.arguments = call.arguments ?? tool.arguments;
      tool.status = "running";
    } else if (event.type === "tool.completed" || event.type === "tool.failed") {
      const result = payload.result ?? {};
      const tool = requireToolItem(run, requiredText(result.toolCallId, "toolCallId"));
      registerArtifacts(result.artifactRefs);
      const artifactIds = uniqueStrings([
        ...(Array.isArray(payload.artifactIds) ? payload.artifactIds : []),
        ...(Array.isArray(result.artifactRefs) ? result.artifactRefs.map((item) => item.artifactId) : []),
      ]);
      tool.status = requiredText(result.status, "tool result status");
      tool.result = result;
      tool.error = result.error ?? null;
      tool.artifacts = uniqueStrings([...tool.artifacts, ...artifactIds]);
      const sourceRefs = Array.isArray(result.sourceRefs) ? result.sourceRefs : [];
      run.references = mergeRefs(run.references, sourceRefs);
      registerReferenceArtifacts(sourceRefs);
    } else if (event.type === "artifact.created") {
      registerArtifacts([payload.artifact]);
      if (payload.artifact?.artifactId) run.artifacts.add(payload.artifact.artifactId);
    } else if (event.type === "approval.required") {
      const approval = payload.approval ?? {};
      registerArtifacts([approval.diffArtifact]);
      const diffs = uniqueStrings([
        ...(Array.isArray(payload.diffArtifactIds) ? payload.diffArtifactIds : []),
        ...(approval.diffArtifact?.artifactId ? [approval.diffArtifact.artifactId] : []),
      ]);
      const approvalId = requiredText(approval.approvalId, "approvalId");
      appendTimelineItem(run, {
        kind: "approval",
        itemId: `approval:${approvalId}`,
        sequence: event.sequence,
        approvalId,
        ...approval,
        argsHash: approval.toolCall?.argsHash,
        explanation: requiredText(payload.explanation, "approval explanation"),
        diffs,
        status: "pending",
      });
    } else if (event.type === "approval.resolved" || event.type === "approval.expired") {
      const approval = requireApprovalItem(run, requiredText(payload.approvalId, "approvalId"));
      approval.status = payload.status ?? "expired";
    } else if (event.type === "references.updated") {
      run.references = payload.replace ? payload.references ?? [] : mergeRefs(run.references, payload.references ?? []);
      registerReferenceArtifacts(run.references);
    } else if (event.type === "usage.updated") {
      run.usage = payload.usage;
    } else if (event.type === "context.compacted") {
      registerArtifacts([payload.summaryArtifact]);
      if (payload.summaryArtifact?.artifactId) run.compactions.push(payload.summaryArtifact.artifactId);
    } else if (event.type.startsWith("subagent.")) {
      reduceSubagentEvent(run, event.type, payload, event.sequence);
    }

    if (event.type === "turn.completed") {
      run.status = "completed";
      run.completedAt = event.timestamp;
      run.usage = payload.usage ?? run.usage;
      applyAssistantContent(run, payload.assistantContent, event.sequence);
    } else if (event.type === "turn.cancelled") {
      run.status = "cancelled";
      run.completedAt = event.timestamp;
      run.usage = payload.usage ?? run.usage;
      applyPartialContent(run, payload.partialContent, event.sequence);
    } else if (event.type === "turn.failed") {
      run.status = "failed";
      run.completedAt = event.timestamp;
      run.usage = payload.usage ?? run.usage;
      run.error = payload.error ?? null;
      applyPartialContent(run, payload.partialContent, event.sequence);
    } else if (event.type === "turn.interrupted") {
      run.status = "interrupted";
      run.completedAt = event.timestamp;
      run.usage = payload.usage ?? run.usage;
      run.error = payload.error ?? null;
      applyPartialContent(run, payload.partialContent, event.sequence);
    }
    run.lastSequence = event.sequence;
    state.runs.set(run.runId, run);
    if (!state.runIds.includes(run.runId)) state.runIds.push(run.runId);
  }

  function reduceSubagentEvent(run, type, payload, sequence) {
    const childRunId = requiredText(payload.childRunId ?? payload.result?.runId, "childRunId");
    if (type === "subagent.queued") {
      appendTimelineItem(run, {
        kind: "subagent",
        itemId: `subagent:${childRunId}`,
        sequence,
        childRunId,
        agentName: requiredText(payload.agentName, "subagent agentName"),
        task: requiredText(payload.task, "subagent task"),
        depth: integer(payload.depth, "subagent depth", 1),
        status: "queued",
        message: null,
        summary: null,
      });
      return;
    }
    const subagent = requireSubagentItem(run, childRunId);
    if (type === "subagent.started") {
      subagent.agentName = requiredText(payload.agentName, "subagent agentName");
      subagent.status = "running";
    } else if (type === "subagent.progress") {
      subagent.status = "running";
      subagent.message = requiredText(payload.message, "subagent progress");
    } else if (type === "subagent.waiting") {
      subagent.status = "waiting";
      subagent.message = `等待：${requiredText(payload.reason, "subagent wait reason")}`;
    } else if (type === "subagent.result_available") {
      subagent.status = "result_available";
      subagent.summary = requiredText(payload.summary, "subagent summary");
      if (payload.resultArtifactId) run.artifacts.add(payload.resultArtifactId);
    } else if (type === "subagent.completed") {
      subagent.status = "completed";
      subagent.summary = requiredText(payload.result?.summary, "subagent summary");
      registerArtifacts(payload.result?.artifacts);
      for (const artifact of payload.result?.artifacts ?? []) run.artifacts.add(artifact.artifactId);
    } else if (type === "subagent.failed") {
      subagent.status = "failed";
      subagent.message = payload.error?.userVisibleMessage ?? "子任务失败";
    } else if (type === "subagent.cancelled") {
      subagent.status = "cancelled";
      subagent.message = payload.reason ?? null;
    } else if (type === "subagent.interrupted") {
      subagent.status = "interrupted";
      subagent.message = payload.error?.userVisibleMessage ?? "子任务中断";
    } else if (type === "subagent.orphaned") {
      subagent.status = "orphaned";
    } else if (type === "subagent.recovered") {
      subagent.status = "running";
    }
  }

  function applyAssistantContent(run, value, sequence) {
    const projection = contentProjection(value);
    const assistant = requireAssistantItem(run, sequence);
    assistant.blocks = projection.text;
    assistant.completed = true;
    registerArtifacts(projection.artifacts);
    for (const artifact of projection.artifacts) run.artifacts.add(artifact.artifactId);
  }

  function applyPartialContent(run, value, sequence) {
    const projection = contentProjection(value);
    if (projection.text.length) {
      const assistant = requireAssistantItem(run, sequence);
      assistant.blocks = projection.text;
      assistant.completed = true;
    }
    registerArtifacts(projection.artifacts);
  }

  function appendTimelineItem(run, item) {
    if (run.timeline.some((existing) => existing.itemId === item.itemId)) {
      throw new Error(`时间线条目重复：${item.itemId}`);
    }
    run.timeline.push(item);
    return item;
  }

  function requireAssistantItem(run, sequence) {
    const existing = run.timeline.find((item) => item.kind === "assistant_message");
    if (existing) return existing;
    return appendTimelineItem(run, {
      kind: "assistant_message",
      itemId: `assistant:${sequence}`,
      sequence,
      blocks: [],
      completed: false,
    });
  }

  function requireToolItem(run, toolCallId) {
    const tool = run.timeline.find((item) => item.kind === "tool_call" && item.toolCallId === toolCallId);
    if (!tool) throw new Error("工具事件缺少已接受调用");
    return tool;
  }

  function requireApprovalItem(run, approvalId) {
    const approval = run.timeline.find((item) => item.kind === "approval" && item.approvalId === approvalId);
    if (!approval) throw new Error("审批事件缺少请求");
    return approval;
  }

  function requireSubagentItem(run, childRunId) {
    const subagent = run.timeline.find((item) => item.kind === "subagent" && item.childRunId === childRunId);
    if (!subagent) throw new Error("子 Agent 事件缺少排队记录");
    return subagent;
  }

  async function resolveApproval(run, approval, decision, scope) {
    if (!approval.argsHash) throw new Error("审批缺少参数哈希绑定");
    await command("approval/resolve", {
      approvalId: approval.approvalId,
      decision,
      scope,
      expectedArgsHash: approval.argsHash,
      includeDescendants: false,
      comment: null,
    });
    await replaySession();
    renderTimeline();
  }

  async function showArtifact(artifactId) {
    state.artifactView = { artifactId: requiredText(artifactId, "artifactId"), text: "", nextOffset: 0, eof: false };
    await readArtifactPage();
  }

  async function readArtifactPage() {
    const view = state.artifactView;
    if (!view || view.eof || view.nextOffset >= ARTIFACT_TOTAL_BYTES) return;
    const requestedOffset = view.nextOffset;
    const result = await command("artifact/read", {
      artifactId: view.artifactId,
      offset: requestedOffset,
      maxBytes: ARTIFACT_PAGE_BYTES,
    });
    if (
      result.artifact?.artifactId !== view.artifactId ||
      result.offset !== requestedOffset ||
      !Number.isSafeInteger(result.nextOffset) ||
      result.nextOffset < requestedOffset ||
      (!result.eof && result.nextOffset <= requestedOffset) ||
      result.nextOffset > requestedOffset + ARTIFACT_PAGE_BYTES ||
      !["utf8", "base64"].includes(result.encoding) ||
      typeof result.content !== "string"
    ) {
      throw new Error("Artifact 分页结果无效");
    }
    registerArtifacts([result.artifact]);
    const mediaType = result.artifact.mediaType ?? "application/octet-stream";
    let pageText;
    if (result.encoding === "utf8") pageText = result.content;
    else if (isTextMediaType(mediaType)) pageText = decodeBase64Utf8(result.content);
    else pageText = `[${mediaType} 二进制内容未在页面内渲染；可在受信任的 Artifact 工具中查看元数据。]`;
    view.text += pageText;
    view.nextOffset = result.nextOffset;
    view.eof = result.eof === true || result.nextOffset >= ARTIFACT_TOTAL_BYTES;
    renderArtifactInspector();
  }

  function renderArtifactInspector() {
    const view = state.artifactView;
    if (!view) return;
    const artifact = state.artifacts.get(view.artifactId);
    openInspector(artifact?.title ?? `Artifact ${shortId(view.artifactId)}`);
    const metadata = node("dl", null, "diagnostic-grid");
    for (const [key, value] of [
      ["ID", view.artifactId],
      ["类型", artifact?.mediaType ?? "-"],
      ["大小", Number.isSafeInteger(artifact?.sizeBytes) ? `${artifact.sizeBytes} bytes` : "-"],
      ["状态", artifact?.state ?? "-"],
      ["已读取", `${view.nextOffset} bytes${view.eof ? " · 完成" : ""}`],
    ]) {
      metadata.append(node("dt", key), node("dd", String(value)));
    }
    const pre = node("pre", null, "artifact-content");
    pre.textContent = view.text;
    inspectorEl.append(metadata, pre);
    if (!view.eof) {
      const more = node("button", "加载下一页（最多 64 KiB）");
      more.type = "button";
      more.addEventListener("click", () => readArtifactPage().catch(showError));
      inspectorEl.append(more);
    } else if (view.nextOffset >= ARTIFACT_TOTAL_BYTES && artifact?.sizeBytes > ARTIFACT_TOTAL_BYTES) {
      inspectorEl.append(node("p", "页面预览已达到 512 KiB 安全上限；其余内容未载入。", "warning"));
    }
  }

  function renderSessions() {
    const buttons = state.sessions.map((session) => {
      const button = node("button", null, `session${session.sessionId === state.sessionId ? " active" : ""}`);
      button.type = "button";
      button.append(
        node("span", session.title || "未命名会话", "session-title"),
        node(
          "small",
          `${session.turnCount ?? 0} 轮${session.activeRunId ? " · 运行中" : ""}`,
          session.activeRunId ? "running" : "",
        ),
      );
      button.onclick = () => selectSession(session.sessionId).catch(showError);
      return button;
    });
    sessionsEl.replaceChildren(...buttons);
    updateControls();
  }

  function renderTimeline() {
    const runs = state.runIds
      .map((id) => state.runs.get(id))
      .filter((run) => run && run.parentRunId === null && (run.timeline.length > 0 || terminal(run.status) || run.error));
    const pending = state.pendingSubmissions.filter((submission) => submission.sessionId === state.sessionId);
    if (!runs.length && !pending.length) {
      renderEmpty("此会话尚无消息", "发送消息后，流式文本、工具、审批和引用会从持久事件投影。", false);
      updateControls();
      return;
    }
    const fragment = document.createDocumentFragment();
    for (const run of runs) fragment.append(renderRun(run));
    for (const submission of pending) fragment.append(renderPendingSubmission(submission));
    timelineEl.replaceChildren(fragment);
    timelineEl.scrollTop = timelineEl.scrollHeight;
    updateControls();
  }

  function renderPendingSubmission(submission) {
    const article = node("article", null, "run pending-run");
    article.append(message("你", [submission.text], "user"), node("p", "正在提交给 OfferAgent…", "thinking"));
    return article;
  }

  function renderRun(run) {
    const article = node("article", null, "run");
    for (const item of run.timeline) article.append(renderTimelineItem(run, item));
    if (!terminal(run.status)) article.append(node("p", phaseText(run.phase), "thinking"));
    if (run.error?.userVisibleMessage) article.append(node("p", run.error.userVisibleMessage, "error"));
    if (run.artifacts.size) article.append(renderArtifactLinks([...run.artifacts], "Run Artifacts"));
    if (run.references.length) article.append(renderReferences(run.references));
    if (run.compactions.length) article.append(renderArtifactLinks(run.compactions, "压缩摘要"));

    const controls = node("div", null, "run-actions");
    if (terminal(run.status) && !run.parentRunId) {
      const retry = node("button", run.status === "interrupted" ? "恢复（新 Run）" : "重试（新 Run）");
      retry.type = "button";
      retry.onclick = () => retryRun(run).catch(showError);
      controls.append(retry);
    }
    if (run.turnId && !run.parentRunId) {
      const fork = node("button", "从这里 Fork 会话");
      fork.type = "button";
      fork.onclick = () => forkRun(run).catch(showError);
      controls.append(fork);
    }
    if (controls.childElementCount) article.append(controls);

    const meta = node("div", null, "meta");
    const phase = run.phase ? ` · ${phaseText(run.phase).replace("…", "")}` : "";
    meta.append(
      node("span", `${statusText(run.status)}${phase}`),
      node("span", usageText(run.usage, run.startedAt, run.completedAt)),
    );
    article.append(meta);
    return article;
  }

  function renderTimelineItem(run, item) {
    if (item.kind === "user_message") {
      return message(item.source === "steer" ? "你（追加）" : "你", item.blocks, "user");
    }
    if (item.kind === "reasoning") {
      const details = node("details", null, "reasoning");
      details.append(node("summary", item.partial ? "推理摘要（生成中）" : "推理摘要"), node("p", item.summary));
      return details;
    }
    if (item.kind === "assistant_message") {
      const assistant = message("OfferAgent", item.blocks, "assistant");
      if (!item.completed) assistant.append(node("p", "正在生成回答…", "thinking"));
      return assistant;
    }
    if (item.kind === "tool_call") return renderTool(item);
    if (item.kind === "approval") return renderApproval(run, item);
    if (item.kind === "subagent") return renderSubagent(item);
    throw new Error("未知时间线条目");
  }

  function message(label, blocks, kind) {
    const box = node("div", null, `message ${kind}`);
    box.append(node("div", label, "label"));
    for (const textValue of blocks) box.append(node("div", textValue));
    return box;
  }

  function renderTool(tool) {
    const card = node("div", null, "tool");
    const head = node("div", null, "tool-head");
    head.append(node("span", tool.name ?? "tool"), node("span", statusText(tool.status)));
    card.append(head);
    const details = node("details");
    details.append(node("summary", "参数与结果"), node("pre", boundedJson(tool.arguments)));
    if (tool.result) details.append(node("pre", boundedJson(tool.result)));
    if (tool.error) details.append(node("pre", boundedJson(tool.error)));
    card.append(details);
    if (tool.artifacts.length) card.append(renderArtifactLinks(tool.artifacts, "查看工具 Artifact"));
    return card;
  }

  function renderSubagent(subagent) {
    const details = node("details", null, `subagent ${subagent.status}`);
    details.append(node("summary", `子 Agent · ${subagent.agentName} · ${statusText(subagent.status)}`));
    details.append(node("p", subagent.task));
    if (subagent.message) details.append(node("p", subagent.message));
    if (subagent.summary) details.append(node("pre", subagent.summary));
    return details;
  }

  function renderApproval(run, approval) {
    const card = node("div", null, `approval ${approval.status}`);
    card.append(
      node("strong", approval.status === "pending" ? "需要审批" : `审批：${approval.status}`),
      node("p", approval.explanation ?? ""),
    );
    if (approval.toolCall) {
      const details = node("details");
      details.append(node("summary", `${approval.toolCall.name ?? "工具"} 的参数`), node("pre", boundedJson(approval.toolCall.arguments)));
      card.append(details);
    }
    if (approval.diffs?.length) card.append(renderArtifactLinks(approval.diffs, "查看 Diff 内容"));
    if (approval.status === "pending") {
      const actions = node("div", null, "approval-actions");
      for (const [label, decision, scope, className] of [
        ["拒绝", "deny", "once", "deny"],
        ["允许一次", "allow_once", "once", ""],
        ["本轮允许", "allow_run", "run", ""],
        ["本会话允许", "allow_session", "session", ""],
      ]) {
        const button = node("button", label, className);
        button.type = "button";
        button.onclick = () => resolveApproval(run, approval, decision, scope).catch(showError);
        actions.append(button);
      }
      card.append(actions);
    }
    return card;
  }

  function renderArtifactLinks(artifactIds, label) {
    const box = node("div", null, "artifact-links");
    box.append(node("span", label));
    for (const artifactId of uniqueStrings(artifactIds)) {
      const artifact = state.artifacts.get(artifactId);
      const button = node("button", artifact?.title ?? shortId(artifactId));
      button.type = "button";
      button.title = artifactId;
      button.onclick = () => showArtifact(artifactId).catch(showError);
      box.append(button);
    }
    return box;
  }

  function renderReferences(references) {
    const box = node("details", null, "references");
    box.append(node("summary", `引用（${references.length}）`));
    const list = node("ol");
    for (const reference of references) {
      const item = node("li");
      item.append(node("span", referenceText(reference)));
      if (reference.type === "artifact" && reference.artifact?.artifactId) {
        const button = node("button", "查看 Artifact");
        button.type = "button";
        button.onclick = () => showArtifact(reference.artifact.artifactId).catch(showError);
        item.append(button);
      }
      list.append(item);
    }
    box.append(list);
    return box;
  }

  async function showDiagnostics() {
    const result = await command("diagnostics/get", { includeRecentErrors: true, includePaths: false });
    openInspector("本地诊断");
    const dl = node("dl", null, "diagnostic-grid");
    const runtime = result.runtime ?? {};
    for (const [key, value] of [
      ["状态", runtime.state],
      ["Worker PID", runtime.workerPid],
      ["Runtime", runtime.runtimeVersion],
      ["Core", runtime.coreVersion],
      ["协议", runtime.protocolVersion],
      ["Schema", runtime.schemaHash],
      ["SQLite 身份", runtime.databaseIdentity],
      ["索引", runtime.index?.state],
    ]) {
      dl.append(node("dt", key), node("dd", String(value ?? "-")));
    }
    inspectorEl.append(dl, node("pre", boundedJson(result.recentErrors ?? [])));
  }

  async function loadExtensionCatalogs() {
    const requests = [];
    const labels = [];
    if (state.capabilities.skills === true) {
      labels.push("skills", "skillStatus");
      requests.push(command("skills/list", {}), command("skills/status", {}));
    }
    if (state.capabilities.shell === true) {
      labels.push("shell");
      requests.push(command("shell/list", { includeDisabled: true }));
    }
    if (state.capabilities.hooks === true) {
      labels.push("hooks");
      requests.push(command("hooks/list", {}));
    }
    const values = await Promise.all(requests);
    for (let index = 0; index < labels.length; index += 1) {
      const label = labels[index];
      if (label === "skills") state.skillCatalog = values[index];
      else if (label === "skillStatus") state.skillStatus = values[index]?.status ?? null;
      else if (label === "shell") state.shellCatalog = values[index];
      else if (label === "hooks") state.hookCatalog = values[index];
    }
  }

  async function showCapabilities() {
    openInspector("能力与管理入口");
    inspectorEl.append(node("p", "正在读取 Skills / Shell / Hooks 的 Worker 状态…"));
    await loadExtensionCatalogs();
    openInspector("能力与管理入口");
    const intro = node(
      "p",
      "此页面读取同一 Worker 的真实管理目录。所有工具执行都由 Worker 的统一权限与审计链负责。",
    );
    inspectorEl.append(intro);
    const managed = [
      ["模型", "可用", "使用当前 Codex Subscription 目录；在输入区选择精确模型。"],
      ["Skills", state.capabilities.skills === true ? "目录已读取" : "未协商", "可在下方选择本页面新 Run 使用的已信任 Skills；信任确认在 Obsidian 设置中完成。"],
      ["Shell", state.capabilities.shell === true ? "目录已读取" : "未协商", "下方显示持久 profile、revision、信任与启停状态。"],
      ["Hooks", state.capabilities.hooks === true ? "目录已读取" : "未协商", "下方显示 layer、contentHash trust 与 Workspace command definitionHash 确认状态。"],
    ];
    for (const [name, status, action] of managed) {
      const section = node("section", null, "capability-card");
      section.append(node("h3", name), node("strong", status), node("p", action));
      inspectorEl.append(section);
    }
    renderSkillCatalogReadOnly();
    renderShellCatalogReadOnly();
    renderHookCatalogReadOnly();
    const refresh = node("button", "刷新模型与扩展状态");
    refresh.type = "button";
    refresh.onclick = () =>
      loadModels()
        .then(() => showCapabilities())
        .then(() => setStatus("模型与扩展状态已刷新"))
        .catch(showError);
    inspectorEl.append(refresh);
  }

  function renderSkillCatalogReadOnly() {
    if (!state.skillCatalog || !state.skillStatus) return;
    const section = node("section", null, "capability-card extension-catalog");
    section.append(
      node("h3", "SkillCatalog"),
      node(
        "p",
        `revision ${state.skillStatus.revision} · ${state.skillStatus.enabledCount}/${state.skillStatus.discoveredCount} enabled${state.skillStatus.partial ? " · partial" : ""}`,
      ),
    );
    for (const skill of Array.isArray(state.skillCatalog.skills) ? state.skillCatalog.skills : []) {
      if (!skill || typeof skill.name !== "string") continue;
      const row = node("div", null, "extension-row");
      row.append(
        node("span", skill.name),
        node("small", `${skill.layer} · ${skill.description}`),
      );
      section.append(row);
    }
    const diagnostics = Array.isArray(state.skillStatus.diagnostics) ? state.skillStatus.diagnostics : [];
    for (const diagnostic of diagnostics) {
      section.append(node("p", `${diagnostic?.severity ?? "warning"}: ${diagnostic?.code ?? "unknown"} — ${diagnostic?.message ?? ""}`, diagnostic?.severity === "error" ? "error" : "warning"));
    }
    inspectorEl.append(section);
  }

  function renderShellCatalogReadOnly() {
    if (!state.shellCatalog) return;
    const section = node("section", null, "capability-card extension-catalog");
    section.append(node("h3", "Shell profiles"), node("p", `catalog revision ${state.shellCatalog.revision}`));
    for (const record of Array.isArray(state.shellCatalog.profiles) ? state.shellCatalog.profiles : []) {
      const profile = record?.profile;
      if (!profile || typeof profile.profileId !== "string") continue;
      const row = node("div", null, "extension-row");
      row.append(
        node("span", profile.profileId),
        node("small", `${profile.executableId} · ${record.trust} · ${record.enabled === true ? "enabled" : "disabled"} · r${record.revision}`),
      );
      section.append(row);
    }
    if (!Array.isArray(state.shellCatalog.profiles) || !state.shellCatalog.profiles.length) {
      section.append(node("p", "没有已安装 Shell profile。"));
    }
    inspectorEl.append(section);
  }

  function renderHookCatalogReadOnly() {
    if (!state.hookCatalog) return;
    const section = node("section", null, "capability-card extension-catalog");
    section.append(node("h3", "Hook layers"), node("p", `catalog revision ${state.hookCatalog.revision}`));
    for (const layer of Array.isArray(state.hookCatalog.layers) ? state.hookCatalog.layers : []) {
      if (!layer || typeof layer.ownerId !== "string") continue;
      const row = node("div", null, "extension-row");
      const hooks = Array.isArray(layer.hooks) ? layer.hooks : [];
      const confirmations = layer.commandConfirmations && typeof layer.commandConfirmations === "object"
        ? layer.commandConfirmations
        : {};
      const pendingCommands = hooks.filter(
        (hook) => hook?.implementation === "command" && confirmations[hook.hookId] !== hook.definitionHash,
      ).length;
      row.append(
        node("span", `${layer.scope}:${layer.ownerId}`),
        node("small", `${layer.trust} · r${layer.recordRevision} · ${hooks.length} hooks${pendingCommands ? ` · ${pendingCommands} command confirmations pending` : ""}`),
      );
      section.append(row);
    }
    inspectorEl.append(section);
  }

  function openInspector(title) {
    inspectorEl.hidden = false;
    document.querySelector(".layout").classList.add("has-inspector");
    const heading = node("div", null, "inspector-heading");
    heading.append(node("h2", title));
    const close = node("button", "关闭");
    close.type = "button";
    close.onclick = () => {
      inspectorEl.hidden = true;
      document.querySelector(".layout").classList.remove("has-inspector");
    };
    heading.append(close);
    inspectorEl.replaceChildren(heading);
  }

  function renderEmpty(title, description, error) {
    const box = node("div", null, "empty");
    box.append(node("h2", title), node("p", description));
    timelineEl.replaceChildren(box);
    if (error) box.classList.add("error");
  }

  function setStatus(messageValue, error = false) {
    statusEl.textContent = messageValue;
    statusEl.classList.toggle("error", error);
  }

  function updateControls() {
    const active = activeRootRun();
    const busy = state.commandPending > 0;
    sendEl.disabled = busy || active !== null || selectedModel() === null;
    steerEl.disabled = busy || active === null || !promptEl.value.trim();
    cancelEl.disabled = busy || active === null;
    modelHealthEl.disabled = busy || selectedModel() === null;
    modelEl.disabled = true;
    for (const id of ["rename-session", "compact-session", "delete-session"]) {
      el(id).disabled = busy || !state.sessionId;
    }
  }

  function upsertSessionSummary(summary) {
    if (!summary || typeof summary.sessionId !== "string") return;
    const index = state.sessions.findIndex((session) => session.sessionId === summary.sessionId);
    if (summary.deleted) {
      if (index >= 0) state.sessions.splice(index, 1);
    } else if (index >= 0) state.sessions[index] = summary;
    else state.sessions.unshift(summary);
  }

  function currentSession() {
    return state.sessions.find((session) => session.sessionId === state.sessionId) ?? null;
  }

  function activeRootRun() {
    return (
      [...state.runs.values()]
        .reverse()
        .find((run) => !run.parentRunId && run.sessionId === state.sessionId && !terminal(run.status)) ?? null
    );
  }

  function selectedModel() {
    return state.selectedModelKey ? state.models.get(state.selectedModelKey) ?? null : null;
  }

  function registerArtifacts(artifacts) {
    if (!Array.isArray(artifacts)) return;
    for (const artifact of artifacts) {
      if (artifact && typeof artifact.artifactId === "string") state.artifacts.set(artifact.artifactId, artifact);
    }
  }

  function registerReferenceArtifacts(references) {
    for (const reference of references ?? []) {
      if (reference?.type === "artifact" && reference.artifact) registerArtifacts([reference.artifact]);
    }
  }

  function contentProjection(value) {
    const text = [];
    const artifacts = [];
    if (!Array.isArray(value)) return { text, artifacts };
    for (const block of value) {
      if (block?.type === "text") {
        text.push(String(block.text ?? ""));
      } else if (block?.type === "artifact" && block.artifact) {
        artifacts.push(block.artifact);
        text.push(block.preview ? String(block.preview) : `[Artifact: ${block.artifact.artifactId}]`);
      } else if (block?.type === "file" && block.file) {
        const lines = block.file.lineStart ? `:${block.file.lineStart}` : "";
        text.push(block.excerpt ? `${block.excerpt}\n[${block.file.path}${lines}]` : `[文件: ${block.file.path}${lines}]`);
      } else if (block?.type === "image" && block.artifact) {
        artifacts.push(block.artifact);
        text.push(block.altText ? `[图片: ${block.altText}]` : `[图片 Artifact: ${block.artifact.artifactId}]`);
      } else {
        text.push(`[${block?.type ?? "content"}]`);
      }
    }
    return { text, artifacts };
  }

  function mergeRefs(current, incoming) {
    const result = new Map();
    for (const item of [...current, ...incoming]) result.set(referenceKey(item), item);
    return [...result.values()];
  }

  function referenceKey(reference) {
    if (reference?.type === "vault") {
      return [
        "vault",
        reference.file?.workspaceId,
        reference.file?.path,
        reference.file?.lineStart ?? "",
        reference.file?.lineEnd ?? "",
        reference.file?.heading ?? "",
        reference.file?.blockId ?? "",
      ].join(":");
    }
    if (reference?.type === "artifact") return `artifact:${reference.artifact?.artifactId}`;
    return JSON.stringify(reference);
  }

  function referenceText(reference) {
    if (reference?.type === "vault") {
      const line = reference.file?.lineStart
        ? reference.file?.lineEnd && reference.file.lineEnd !== reference.file.lineStart
          ? `:${reference.file.lineStart}-${reference.file.lineEnd}`
          : `:${reference.file.lineStart}`
        : "";
      const freshness = reference.freshness ? ` · ${reference.freshness}` : "";
      return `${reference.label ?? reference.file?.path ?? "Vault"}${line}${freshness}`;
    }
    if (reference?.type === "artifact") return reference.label ?? reference.artifact?.title ?? reference.artifact?.artifactId;
    return "未知引用";
  }

  function node(tag, textValue, className) {
    const value = document.createElement(tag);
    if (textValue != null) value.textContent = textValue;
    if (className) value.className = className;
    return value;
  }

  function opaque(prefix) {
    const bytes = new Uint8Array(16);
    crypto.getRandomValues(bytes);
    return prefix + [...bytes].map((value) => value.toString(16).padStart(2, "0")).join("");
  }

  function requiredText(value, name) {
    if (typeof value !== "string" || !value) throw new Error(`无效 ${name}`);
    return value;
  }

  function integer(value, name, minimum) {
    if (!Number.isSafeInteger(value) || value < minimum) throw new Error(`无效 ${name}`);
    return value;
  }

  function emptyRun(runId) {
    return {
      runId,
      rootRunId: null,
      sessionId: null,
      turnId: null,
      parentRunId: null,
      lineageBound: false,
      status: "running",
      phase: null,
      timeline: [],
      artifacts: new Set(),
      references: [],
      compactions: [],
      usage: null,
      error: null,
      startedAt: null,
      completedAt: null,
      lastSequence: 0,
    };
  }

  function validRunId(value) {
    return typeof value === "string" && /^run_[A-Za-z0-9_-]{1,124}$/.test(value);
  }

  function uniqueStrings(values) {
    return [...new Set(values.filter((value) => typeof value === "string" && value.length <= 256))];
  }

  function terminal(status) {
    return ["completed", "cancelled", "failed", "interrupted", "orphaned"].includes(status);
  }

  function statusText(value) {
    return (
      {
        queued: "排队中",
        accepted: "已接受",
        running: "运行中",
        waiting: "等待中",
        result_available: "结果可用",
        completed: "已完成",
        cancelled: "已取消",
        failed: "失败",
        interrupted: "已中断",
        orphaned: "待恢复",
        succeeded: "成功",
        denied: "已拒绝",
        conflict: "冲突",
        pending: "待审批",
        timed_out: "超时",
        partial: "部分完成",
        unknown_outcome: "结果未知",
      }[value] ?? value ?? "未知"
    );
  }

  function phaseText(value) {
    return (
      {
        created: "已创建…",
        loading_context: "正在加载上下文…",
        selecting_memory: "正在读取固定 Vault Memory 文件…",
        planning: "正在规划…",
        validating_calls: "正在校验工具…",
        checking_policy: "正在检查权限…",
        awaiting_approval: "等待审批…",
        executing_tools: "正在执行工具…",
        recording_results: "正在记录工具结果…",
        waiting_children: "等待子任务…",
        composing: "正在组织回答…",
        persisting: "正在持久化…",
        cancelling: "正在取消…",
        terminal: "已结束",
      }[value] ?? "正在处理…"
    );
  }

  function usageText(value, startedAt, completedAt) {
    if (value) {
      const total = (value.inputTokens ?? 0) + (value.outputTokens ?? 0);
      const cached = value.cachedInputTokens ? ` · ${value.cachedInputTokens} cached` : "";
      const reasoning = value.reasoningTokens ? ` · ${value.reasoningTokens} reasoning` : "";
      const calls = ` · ${value.modelCalls ?? 0} model · ${value.toolCalls ?? 0} tools`;
      const cost = Number.isSafeInteger(value.costMicros) ? ` · ${(value.costMicros / 1_000_000).toFixed(6)} cost` : "";
      return `${total} tokens${cached}${reasoning}${calls}${cost} · ${durationText(value.wallTimeMs ?? 0)}`;
    }
    const elapsed = elapsedMs(startedAt, completedAt);
    return elapsed === null ? "" : durationText(elapsed);
  }

  function elapsedMs(startedAt, completedAt) {
    if (!startedAt) return null;
    const start = Date.parse(startedAt);
    const end = completedAt ? Date.parse(completedAt) : Date.now();
    return Number.isFinite(start) && Number.isFinite(end) && end >= start ? end - start : null;
  }

  function durationText(milliseconds) {
    if (!Number.isFinite(milliseconds) || milliseconds < 0) return "-";
    if (milliseconds < 1000) return `${Math.round(milliseconds)} ms`;
    if (milliseconds < 60_000) return `${(milliseconds / 1000).toFixed(1)} s`;
    return `${Math.floor(milliseconds / 60_000)}m ${Math.round((milliseconds % 60_000) / 1000)}s`;
  }

  function modelHealthText(value) {
    return (
      {
        healthy: "模型健康",
        degraded: "模型降级",
        unreachable: "模型不可达",
        auth_required: "模型需要认证",
        unsupported: "模型不受支持",
      }[value] ?? `模型状态：${value ?? "未知"}`
    );
  }

  function boundedJson(value) {
    const textValue = JSON.stringify(value, null, 2);
    return textValue.length > 16_384 ? `${textValue.slice(0, 16_384)}\n…` : textValue;
  }

  function isTextMediaType(value) {
    return /^text\//i.test(value) || /\/(?:json|xml|yaml|javascript|x-diff)(?:;|$)/i.test(value);
  }

  function decodeBase64Utf8(value) {
    if (!/^[A-Za-z0-9+/]*={0,2}$/.test(value) || value.length > 1_398_104) throw new Error("Artifact base64 内容无效");
    const binary = atob(value);
    const bytes = new Uint8Array(binary.length);
    for (let index = 0; index < binary.length; index += 1) bytes[index] = binary.charCodeAt(index);
    return new TextDecoder("utf-8", { fatal: false }).decode(bytes);
  }

  function sessionStorageKey() {
    return `offeragent.session.${state.workspaceId}`;
  }

  function shortId(value) {
    if (typeof value !== "string") return "-";
    return value.length > 18 ? `${value.slice(0, 9)}…${value.slice(-6)}` : value;
  }

  function safeError(error) {
    return error instanceof Error ? error.message : "本地 Runtime 操作失败";
  }

  function showError(error) {
    setStatus(safeError(error), true);
  }

  el("composer").addEventListener("submit", (event) => {
    event.preventDefault();
    sendTurn(promptEl.value).catch(showError);
  });
  promptEl.addEventListener("input", updateControls);
  promptEl.addEventListener("keydown", (event) => {
    if (event.key === "Enter" && !event.shiftKey && !event.isComposing) {
      event.preventDefault();
      if (activeRootRun()) steerActive().catch(showError);
      else sendTurn(promptEl.value).catch(showError);
    }
  });
  modelHealthEl.addEventListener("click", () => checkModelHealth().catch(showError));
  el("new-session").addEventListener("click", () => createSession().catch(showError));
  el("rename-session").addEventListener("click", () => renameSession().catch(showError));
  el("delete-session").addEventListener("click", () => deleteSession().catch(showError));
  el("compact-session").addEventListener("click", () => compactSession().catch(showError));
  steerEl.addEventListener("click", () => steerActive().catch(showError));
  cancelEl.addEventListener("click", () => cancelActive().catch(showError));
  el("capabilities").addEventListener("click", () => showCapabilities().catch(showError));
  el("diagnostics").addEventListener("click", () => showDiagnostics().catch(showError));
  window.addEventListener("beforeunload", () => {
    state.stopped = true;
    clearInterval(state.poll);
  });
  void boot();
})();
