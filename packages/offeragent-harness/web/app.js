(() => {
  "use strict";

  const PROTOCOL_VERSION = "1.0";
  const SCHEMA_HASH = "sha256:d648da50bc84d5834578bfc70f980efaf3b165e721d951dbd26f0ae507cdc1ae";
  const CLIENT_VERSION = "0.1.0";
  const ARTIFACT_PAGE_BYTES = 65_536;
  const ARTIFACT_TOTAL_BYTES = 524_288;
  const MEMORY_EXPORT_PAGE_BYTES = 262_144;
  const MEMORY_EXPORT_TOTAL_BYTES = 8_388_608;
  const POLL_INTERVAL_MS = 750;
  const HEADLESS_STATES = new Set([
    "read_only",
    "pipe_client_tool",
    "ambiguous_pipe",
    "baseline_unreliable",
    "pending_approval",
    "approved",
    "active",
    "claimed",
    "denied",
    "expired",
    "revoked",
    "consumed",
  ]);
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
    cursors: new Map(),
    artifacts: new Map(),
    models: new Map(),
    selectedModelKey: null,
    modelHealth: null,
    selectedSkills: new Set(),
    skillCatalog: null,
    skillStatus: null,
    shellCatalog: null,
    hookCatalog: null,
    headlessVaultWrite: null,
    memorySettings: null,
    memories: [],
    memoryScope: "workspace",
    memoryNextAfterId: null,
    memoryExport: null,
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
  const writeRequiredEl = el("write-required");
  const writeTargetPathsEl = el("write-target-paths");
  const sendEl = el("send");
  const steerEl = el("steer");
  const cancelEl = el("cancel");
  const inspectorEl = el("inspector");
  const modelEl = el("model");
  const modelHealthEl = el("model-health");
  const modelStatusEl = el("model-status");
  const headlessWriteEl = el("headless-write");
  const headlessStatusEl = el("headless-status");
  const headlessArgsHashEl = el("headless-args-hash");
  const headlessRequestEl = el("headless-request");
  const headlessActivateEl = el("headless-activate");
  const headlessRevokeEl = el("headless-revoke");

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
            clientTools: false,
            eventReplay: true,
            multiSession: true,
            approvals: true,
            memory: true,
            skills: true,
            shell: true,
            hooks: true,
            subagents: true,
            artifacts: true,
            loopbackWeb: true,
            reverseRequests: false,
            contentBlocks: true,
            cancellation: true,
            diagnostics: true,
            headlessVaultWrite: true,
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

      const controls = await Promise.allSettled([loadModels(), loadHeadlessVaultWrite()]);
      if (controls[0].status === "rejected") {
        modelStatusEl.textContent = `模型目录不可用：${safeError(controls[0].reason)}`;
        modelStatusEl.classList.add("error");
      }
      if (controls[1].status === "rejected") {
        headlessWriteEl.hidden = false;
        headlessStatusEl.textContent = `Vault 写入状态不可用：${safeError(controls[2].reason)}`;
        headlessWriteEl.classList.add("danger");
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
    const result = await command("models/list", { provider: null, includeUnavailable: true });
    const models = Array.isArray(result.models) ? result.models : [];
    state.models.clear();
    for (const descriptor of models) {
      if (!descriptor || typeof descriptor !== "object") continue;
      const provider = requiredText(descriptor.provider, "model.provider");
      const model = requiredText(descriptor.model, "model.model");
      const key = modelKey(provider, model);
      state.models.set(key, {
        provider,
        model,
        displayName: requiredText(descriptor.displayName, "model.displayName"),
        local: descriptor.local === true,
        available: descriptor.available === true,
        supportsStreaming: descriptor.supportsStreaming === true,
        supportsStructuredOutput: descriptor.supportsStructuredOutput === true,
        maxContextTokens: descriptor.maxContextTokens ?? null,
      });
    }
    renderModels();
    const remembered = sessionStorage.getItem(modelStorageKey());
    const selected =
      (remembered && state.models.get(remembered)?.available ? remembered : null) ??
      [...state.models.entries()].find(([, descriptor]) => descriptor.available)?.[0] ??
      null;
    selectModel(selected);
  }

  function renderModels() {
    const options = [];
    for (const [key, descriptor] of state.models) {
      const suffix = `${descriptor.local ? "本地" : descriptor.provider}${descriptor.available ? "" : " · 不可用"}`;
      const option = node("option", `${descriptor.displayName} — ${suffix}`);
      option.value = key;
      option.disabled = !descriptor.available;
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
    state.selectedModelKey = key && state.models.get(key)?.available ? key : null;
    modelEl.disabled = state.models.size === 0;
    modelEl.value = state.selectedModelKey ?? "";
    modelHealthEl.disabled = state.selectedModelKey === null;
    state.modelHealth = null;
    if (state.selectedModelKey) {
      sessionStorage.setItem(modelStorageKey(), state.selectedModelKey);
      const descriptor = state.models.get(state.selectedModelKey);
      modelStatusEl.textContent = descriptor.local ? "本地模型 · 尚未检查" : "外部模型 Provider · 尚未检查";
      modelStatusEl.classList.remove("error", "healthy");
    } else {
      modelStatusEl.textContent = "请先在 Runtime/Obsidian 中配置一个可用模型";
      modelStatusEl.classList.add("error");
    }
    updateControls();
  }

  async function checkModelHealth() {
    const descriptor = selectedModel();
    if (!descriptor) throw new Error("尚未选择可用模型");
    modelStatusEl.textContent = "正在检查模型 Provider…";
    modelStatusEl.classList.remove("error", "healthy");
    const result = await command("models/health", {
      provider: descriptor.provider,
      model: descriptor.model,
      deadline: null,
      clientRequestId: opaque("req_model_health_"),
    });
    if (result.provider !== descriptor.provider || result.model !== descriptor.model) {
      throw new Error("模型健康结果身份不匹配");
    }
    state.modelHealth = result;
    const latency = Number.isSafeInteger(result.latencyMs) ? ` · ${result.latencyMs} ms` : "";
    modelStatusEl.textContent = `${modelHealthText(result.status)}${latency}`;
    modelStatusEl.classList.toggle("healthy", result.status === "healthy");
    modelStatusEl.classList.toggle("error", !["healthy", "degraded"].includes(result.status));
  }

  async function loadHeadlessVaultWrite() {
    if (state.capabilities.headlessVaultWrite !== true) {
      state.headlessVaultWrite = null;
      headlessWriteEl.hidden = true;
      return null;
    }
    const snapshot = validateHeadlessStatus(await command("vault/headless/status", {}));
    state.headlessVaultWrite = snapshot;
    renderHeadlessVaultWrite();
    return snapshot;
  }

  function validateHeadlessStatus(value) {
    if (!value || typeof value !== "object" || Array.isArray(value) || !HEADLESS_STATES.has(value.state)) {
      throw new Error("Runtime 返回了无效的 Web Vault 写入状态");
    }
    for (const key of ["baselineReliable", "canRequest", "canActivate", "canRevoke"]) {
      if (typeof value[key] !== "boolean") throw new Error(`Web Vault 写入状态缺少 ${key}`);
    }
    for (const key of ["pipeConnectionCount", "revision"]) {
      integer(value[key], `headless.${key}`, 0);
    }
    if (!/^sha256:[0-9a-f]{64}$/.test(value.baselineFingerprint)) {
      throw new Error("Web Vault 写入基线指纹无效");
    }
    if (typeof value.reasonCode !== "string" || !/^[a-z][a-z0-9_]{0,127}$/.test(value.reasonCode)) {
      throw new Error("Web Vault 写入 reasonCode 无效");
    }
    requiredText(value.userMessage, "headless.userMessage");
    const identity = [value.approvalId, value.operationId, value.argsHash, value.expiresAt];
    const present = identity.filter((item) => item !== null).length;
    if (present !== 0 && present !== identity.length) throw new Error("Web Vault 写入授权身份不完整");
    if (present) {
      if (!/^apr_[0-9a-f]{64}$/.test(value.approvalId)) throw new Error("Web Vault approvalId 无效");
      if (!/^op_headless_[0-9a-f]{32}$/.test(value.operationId)) throw new Error("Web Vault operationId 无效");
      if (!/^sha256:[0-9a-f]{64}$/.test(value.argsHash)) throw new Error("Web Vault argsHash 无效");
      requiredText(value.expiresAt, "headless.expiresAt");
      if (value.revision < 1) throw new Error("Web Vault 授权 revision 无效");
    } else if (value.revision !== 0) {
      throw new Error("无授权身份时 Web Vault revision 必须为 0");
    }
    return { ...value };
  }

  function renderHeadlessVaultWrite() {
    const snapshot = state.headlessVaultWrite;
    if (!snapshot || state.capabilities.headlessVaultWrite !== true) {
      headlessWriteEl.hidden = true;
      return;
    }
    headlessWriteEl.hidden = false;
    headlessStatusEl.textContent = snapshot.userMessage;
    headlessArgsHashEl.hidden = !snapshot.argsHash;
    headlessArgsHashEl.textContent = snapshot.argsHash ? `argsHash ${snapshot.argsHash}` : "";
    headlessRequestEl.hidden = !snapshot.canRequest;
    headlessActivateEl.hidden = !["pending_approval", "approved"].includes(snapshot.state);
    headlessActivateEl.textContent =
      snapshot.state === "pending_approval" ? "核对 argsHash 并批准一次" : "激活给下一 Turn";
    headlessRevokeEl.hidden = !snapshot.canRevoke;
    headlessWriteEl.classList.toggle(
      "active",
      ["pending_approval", "approved", "active", "claimed"].includes(snapshot.state),
    );
    headlessWriteEl.classList.toggle(
      "danger",
      ["ambiguous_pipe", "baseline_unreliable", "denied", "expired", "revoked"].includes(snapshot.state),
    );
    updateControls();
  }

  async function requestHeadlessVaultWrite() {
    if (
      !confirm(
        "第一次确认：Obsidian 已完全关闭，当前没有插件 Pipe 连接，并且本机磁盘中的 Vault 是权威版本。\n\n继续只会创建一个短期审批请求，不会直接写文件。",
      )
    ) {
      return;
    }
    const baseline = await loadHeadlessVaultWrite();
    if (
      !baseline ||
      !baseline.canRequest ||
      !baseline.baselineReliable ||
      baseline.pipeConnectionCount !== 0
    ) {
      throw new Error("Obsidian/Pipe 或 Workspace 基线已变化，当前不能请求 Web Vault 写入");
    }
    state.headlessVaultWrite = validateHeadlessStatus(
      await command("vault/headless/request", {
        clientRequestId: opaque("req_"),
        confirmation: "obsidian_closed_disk_authoritative",
        ttlSeconds: 300,
        expectedBaselineFingerprint: baseline.baselineFingerprint,
      }),
    );
    renderHeadlessVaultWrite();
    setStatus("审批请求已创建；请核对完整 argsHash 后进行第二次确认");
  }

  async function approveAndActivateHeadlessVaultWrite() {
    let snapshot = state.headlessVaultWrite;
    if (!snapshot || !["pending_approval", "approved"].includes(snapshot.state)) {
      snapshot = await loadHeadlessVaultWrite();
    }
    if (!snapshot || !["pending_approval", "approved"].includes(snapshot.state) || !snapshot.argsHash) {
      throw new Error("没有可批准或激活的 Web Vault 写入请求");
    }
    if (
      !confirm(
        `第二次确认：核对以下 argsHash，并只授权下一次完全一致的 turn/start。\n\n${snapshot.argsHash}\n\n真实写入仍会逐次展示 diff、校验 expectedHash，并要求 vault.transaction 审批。`,
      )
    ) {
      return;
    }
    if (snapshot.state === "pending_approval") {
      const resolved = await command("approval/resolve", {
        approvalId: snapshot.approvalId,
        decision: "allow_once",
        scope: "once",
        expectedArgsHash: snapshot.argsHash,
        includeDescendants: false,
        comment: "本地 Web 用户核对 argsHash 后批准一个 headless Turn",
      });
      if (resolved.runId != null || resolved.operationId !== snapshot.operationId || resolved.resumed !== false) {
        throw new Error("Runtime 将 headless 管理审批错误地绑定到了 Agent Run");
      }
      snapshot = await loadHeadlessVaultWrite();
    }
    if (!snapshot || snapshot.state !== "approved" || !snapshot.canActivate) {
      throw new Error("Web Vault 写入审批状态已变化，未执行激活");
    }
    state.headlessVaultWrite = validateHeadlessStatus(
      await command("vault/headless/activate", {
        clientRequestId: opaque("req_"),
        approvalId: snapshot.approvalId,
        expectedArgsHash: snapshot.argsHash,
        expectedRevision: snapshot.revision,
      }),
    );
    renderHeadlessVaultWrite();
    setStatus("一次性 Web Vault 写入授权已激活；只会由下一次标准权限 Turn 领取");
  }

  async function revokeHeadlessVaultWrite() {
    const snapshot = state.headlessVaultWrite;
    if (!snapshot?.canRevoke || !snapshot.approvalId) return;
    if (!confirm("撤销当前 Web Vault 写入授权？已领取但尚未执行的本地事务也会失权。")) return;
    state.headlessVaultWrite = validateHeadlessStatus(
      await command("vault/headless/revoke", {
        clientRequestId: opaque("req_"),
        approvalId: snapshot.approvalId,
        expectedRevision: snapshot.revision,
        reason: "用户从本地 Web UI 显式撤销 headless Vault 写入授权",
      }),
    );
    renderHeadlessVaultWrite();
    setStatus("Web Vault 写入授权已撤销");
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
    for (const turn of session.turns) seedTurnSnapshot(turn);
    await replaySession(selectedSessionId, epoch);
    if (epoch !== state.sessionEpoch) return;
    applySnapshotFallbacks();
    renderTimeline();
  }

  function seedTurnSnapshot(turn) {
    if (!turn || typeof turn.turnId !== "string" || !Array.isArray(turn.runs)) return;
    const input = contentProjection(turn.input);
    const assistant = contentProjection(turn.assistantContent);
    for (const snapshot of turn.runs) {
      if (!snapshot || typeof snapshot.runId !== "string") continue;
      const run = emptyRun(snapshot.runId);
      run.sessionId = snapshot.sessionId;
      run.turnId = snapshot.turnId;
      run.rootRunId = snapshot.rootRunId;
      run.parentRunId = snapshot.parentRunId;
      run.lineageBound = true;
      run.status = snapshot.status;
      run.phase = snapshot.phase;
      run.agentName = snapshot.agentName ?? "root";
      run.depth = Number.isSafeInteger(snapshot.depth) ? snapshot.depth : 0;
      run.startedAt = snapshot.startedAt ?? null;
      run.completedAt = snapshot.completedAt ?? null;
      run.usage = snapshot.usage ?? null;
      run.user = snapshot.parentRunId ? [] : input.text;
      run.snapshotAssistant = snapshot.runId === turn.selectedRunId ? assistant.text : [];
      registerArtifacts(input.artifacts);
      registerArtifacts(assistant.artifacts);
      state.runs.set(run.runId, run);
      if (!state.runIds.includes(run.runId)) state.runIds.push(run.runId);
    }
  }

  function applySnapshotFallbacks() {
    for (const run of state.runs.values()) {
      if (!run.assistant.length && run.snapshotAssistant.length) run.assistant = [...run.snapshotAssistant];
    }
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
    const writeIntent = await buildWriteIntent();
    const result = await command("turn/start", {
      sessionId: state.sessionId,
      turnId,
      idempotencyKey: opaque("turn_"),
      input: [{ type: "text", text: textValue }],
      clientContext: null,
      runConfig: {
        provider: model.provider,
        model: model.model,
        reasoningEffort: el("reasoning").value,
        permissionMode: el("permission").value,
        enabledSkills: [...state.selectedSkills].sort(),
      },
      writeIntent,
      deadline: null,
    });
    if (!state.runIds.includes(result.runId)) state.runIds.push(result.runId);
    const run = emptyRun(result.runId);
    run.sessionId = result.sessionId;
    run.turnId = result.turnId;
    run.user = [textValue];
    state.runs.set(result.runId, run);
    promptEl.value = "";
    writeRequiredEl.checked = false;
    writeTargetPathsEl.value = "";
    writeTargetPathsEl.hidden = true;
    renderTimeline();
    updateControls();
    if (state.capabilities.headlessVaultWrite === true) {
      void loadHeadlessVaultWrite().catch(showError);
    }
  }

  async function buildWriteIntent() {
    if (!writeRequiredEl.checked) return { kind: "none" };
    const targetPaths = [...new Set(
      writeTargetPathsEl.value.split(/\r?\n/u).map((pathValue) => pathValue.trim()).filter(Boolean),
    )].sort(compareUnicodeCodePoints);
    if (targetPaths.length === 0 || targetPaths.length > 20) {
      throw new Error("勾选写完成要求后，必须提供 1–20 个目标 Vault 路径");
    }
    for (const pathValue of targetPaths) requireSafeVaultPath(pathValue);
    const binding = { kind: "vault_write_required", targetPaths };
    // Closed write-intent schema in RFC 8785/JCS key order.
    const encoded = new TextEncoder().encode(JSON.stringify({
      kind: binding.kind,
      targetPaths: binding.targetPaths,
    }));
    const digest = new Uint8Array(await crypto.subtle.digest("SHA-256", encoded));
    const intentHash = `sha256:${[...digest].map((byte) => byte.toString(16).padStart(2, "0")).join("")}`;
    return { ...binding, intentHash };
  }

  function compareUnicodeCodePoints(left, right) {
    const leftPoints = Array.from(left, (character) => character.codePointAt(0));
    const rightPoints = Array.from(right, (character) => character.codePointAt(0));
    const length = Math.min(leftPoints.length, rightPoints.length);
    for (let index = 0; index < length; index += 1) {
      if (leftPoints[index] !== rightPoints[index]) return leftPoints[index] - rightPoints[index];
    }
    return leftPoints.length - rightPoints.length;
  }

  function requireSafeVaultPath(pathValue) {
    const scalarLength = Array.from(pathValue).length;
    const hasLoneSurrogate = Array.from(pathValue).some((character) => {
      const point = character.codePointAt(0);
      return character.length === 1 && point >= 0xd800 && point <= 0xdfff;
    });
    if (scalarLength > 1024 || hasLoneSurrogate || pathValue.startsWith("/") || /[\\:\x00-\x1f<>"|?*]/u.test(pathValue)) {
      throw new Error(`不安全的 Vault 目标路径：${pathValue}`);
    }
    const reserved = new Set([
      "CON", "CONIN$", "CONOUT$", "PRN", "AUX", "NUL",
      ...Array.from("123456789¹²³", (suffix) => `COM${suffix}`),
      ...Array.from("123456789¹²³", (suffix) => `LPT${suffix}`),
    ]);
    for (const component of pathValue.split("/")) {
      if (!component || component === "." || component === ".." || /[. ]$/u.test(component)) {
        throw new Error(`不安全的 Vault 目标路径：${pathValue}`);
      }
      if (reserved.has(component.split(".", 1)[0].toUpperCase())) {
        throw new Error(`不安全的 Vault 目标路径：${pathValue}`);
      }
    }
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
          await Promise.all([loadSessions(), loadHeadlessVaultWrite()]);
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
      run.user = content.text;
      registerArtifacts(content.artifacts);
      run.startedAt = event.timestamp;
      run.status = "running";
    } else if (event.type === "phase.changed") {
      run.phase = payload.phase;
    } else if (event.type === "reasoning.summary") {
      run.reasoning = String(payload.summary ?? "");
    } else if (event.type === "assistant.delta") {
      const index = integer(payload.blockIndex, "blockIndex", 0);
      const offset = integer(payload.offset, "offset", 0);
      while (run.assistant.length <= index) run.assistant.push("");
      if (run.assistant[index].length !== offset) throw new Error("文本 delta 不连续");
      run.assistant[index] += String(payload.delta ?? "");
    } else if (event.type === "assistant.completed") {
      applyAssistantContent(run, payload.content);
    } else if (event.type === "tool.queued" || event.type === "tool.started") {
      const call = payload.call ?? {};
      const id = call.toolCallId;
      if (typeof id === "string") {
        const tool = run.tools.get(id) ?? {
          id,
          name: call.name,
          arguments: call.arguments ?? {},
          status: event.type,
          progress: "",
          result: null,
          artifacts: [],
        };
        tool.name = call.name ?? tool.name;
        tool.arguments = call.arguments ?? tool.arguments;
        tool.status = event.type;
        run.tools.set(id, tool);
      }
    } else if (event.type === "tool.progress") {
      const tool = run.tools.get(payload.toolCallId);
      if (tool) {
        tool.status = event.type;
        tool.progress = payload.message ?? "";
        if (payload.artifact) {
          registerArtifacts([payload.artifact]);
          tool.artifacts.push(payload.artifact.artifactId);
        }
      }
    } else if (event.type === "tool.completed" || event.type === "tool.failed") {
      const result = payload.result ?? {};
      const tool = run.tools.get(result.toolCallId);
      registerArtifacts(result.artifactRefs);
      const artifactIds = uniqueStrings([
        ...(Array.isArray(payload.artifactIds) ? payload.artifactIds : []),
        ...(Array.isArray(result.artifactRefs) ? result.artifactRefs.map((item) => item.artifactId) : []),
      ]);
      if (tool) {
        tool.status = result.status;
        tool.result = result;
        tool.artifacts = uniqueStrings([...tool.artifacts, ...artifactIds]);
      }
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
      if (approval.approvalId) {
        run.approvals.set(approval.approvalId, {
          ...approval,
          argsHash: approval.toolCall?.argsHash,
          explanation: payload.explanation,
          diffs,
          status: "pending",
        });
      }
    } else if (event.type === "approval.resolved" || event.type === "approval.expired") {
      const approval = run.approvals.get(payload.approvalId);
      if (approval) approval.status = payload.status ?? "expired";
    } else if (event.type === "references.updated") {
      run.references = payload.replace ? payload.references ?? [] : mergeRefs(run.references, payload.references ?? []);
      registerReferenceArtifacts(run.references);
    } else if (event.type === "usage.updated") {
      run.usage = payload.usage;
    } else if (event.type === "context.compacted") {
      registerArtifacts([payload.summaryArtifact]);
      if (payload.summaryArtifact?.artifactId) run.compactions.push(payload.summaryArtifact.artifactId);
    } else if (event.type === "turn.steered") {
      run.steers.push({ messageId: payload.messageId, input: contentProjection(payload.input).text });
    } else if (event.type.startsWith("subagent.")) {
      reduceSubagentEvent(run, event.type, payload, event.timestamp);
    }

    if (event.type === "turn.completed") {
      run.status = "completed";
      run.completedAt = event.timestamp;
      run.usage = payload.usage ?? run.usage;
      applyAssistantContent(run, payload.assistantContent);
    } else if (event.type === "turn.cancelled") {
      run.status = "cancelled";
      run.completedAt = event.timestamp;
      run.usage = payload.usage ?? run.usage;
      applyPartialContent(run, payload.partialContent);
    } else if (event.type === "turn.failed") {
      run.status = "failed";
      run.completedAt = event.timestamp;
      run.usage = payload.usage ?? run.usage;
      run.error = payload.error ?? null;
      applyPartialContent(run, payload.partialContent);
    } else if (event.type === "turn.interrupted") {
      run.status = "interrupted";
      run.completedAt = event.timestamp;
      run.usage = payload.usage ?? run.usage;
      run.error = payload.error ?? null;
      applyPartialContent(run, payload.partialContent);
    }
    run.lastSequence = event.sequence;
    state.runs.set(run.runId, run);
    if (!state.runIds.includes(run.runId)) state.runIds.push(run.runId);
  }

  function reduceSubagentEvent(run, type, payload, timestamp) {
    const childRunId = payload.childRunId ?? payload.result?.runId ?? run.runId;
    let child = state.runs.get(childRunId);
    if (childRunId !== run.runId) {
      child = child ?? emptyRun(childRunId);
      child.sessionId = run.sessionId;
      child.turnId = run.turnId;
      child.rootRunId = run.rootRunId;
      child.parentRunId = payload.parentRunId ?? run.runId;
      child.lineageBound = false;
      state.runs.set(childRunId, child);
      if (!state.runIds.includes(childRunId)) state.runIds.push(childRunId);
    } else {
      child = run;
    }
    child.agentName = payload.agentName ?? child.agentName ?? "subagent";
    if (Number.isSafeInteger(payload.depth)) child.depth = payload.depth;
    if (typeof payload.task === "string") child.task = payload.task;
    if (type === "subagent.queued") child.status = "queued";
    else if (type === "subagent.started") {
      child.status = "running";
      child.startedAt = timestamp;
    } else if (type === "subagent.progress") {
      child.phase = payload.phase;
      child.progress = payload.message ?? "";
    } else if (type === "subagent.waiting") {
      child.phase = payload.reason === "children" ? "waiting_children" : child.phase;
      child.progress = `等待：${payload.reason ?? "依赖"}`;
    } else if (type === "subagent.result_available") {
      child.assistant = [String(payload.summary ?? "")];
      if (payload.resultArtifactId) child.artifacts.add(payload.resultArtifactId);
    } else if (type === "subagent.completed") {
      child.status = "completed";
      child.completedAt = timestamp;
      child.assistant = [String(payload.result?.summary ?? "")];
      child.usage = payload.result?.usage ?? child.usage;
      registerArtifacts(payload.result?.artifacts);
      for (const artifact of payload.result?.artifacts ?? []) child.artifacts.add(artifact.artifactId);
    } else if (type === "subagent.failed") {
      child.status = "failed";
      child.completedAt = timestamp;
      child.error = payload.error ?? null;
      child.usage = payload.usage ?? child.usage;
    } else if (type === "subagent.cancelled") {
      child.status = "cancelled";
      child.completedAt = timestamp;
      child.usage = payload.usage ?? child.usage;
    } else if (type === "subagent.interrupted") {
      child.status = "interrupted";
      child.completedAt = timestamp;
      child.error = payload.error ?? null;
    } else if (type === "subagent.orphaned") {
      child.status = "orphaned";
    } else if (type === "subagent.recovered") {
      child.status = "running";
    }
  }

  function applyAssistantContent(run, value) {
    const projection = contentProjection(value);
    if (projection.text.length) run.assistant = projection.text;
    registerArtifacts(projection.artifacts);
    for (const artifact of projection.artifacts) run.artifacts.add(artifact.artifactId);
  }

  function applyPartialContent(run, value) {
    const projection = contentProjection(value);
    if (projection.text.length) run.assistant = projection.text;
    registerArtifacts(projection.artifacts);
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
    const runs = state.runIds.map((id) => state.runs.get(id)).filter(Boolean);
    if (!runs.length) {
      renderEmpty("此会话尚无消息", "发送消息后，流式文本、工具、审批和引用会从持久事件投影。", false);
      updateControls();
      return;
    }
    const runIds = new Set(runs.map((run) => run.runId));
    const children = new Map();
    const roots = [];
    for (const run of runs) {
      if (run.parentRunId && runIds.has(run.parentRunId)) {
        const bucket = children.get(run.parentRunId) ?? [];
        bucket.push(run);
        children.set(run.parentRunId, bucket);
      } else {
        roots.push(run);
      }
    }
    const fragment = document.createDocumentFragment();
    for (const run of roots) fragment.append(renderRunTree(run, children, 0, new Set()));
    timelineEl.replaceChildren(fragment);
    timelineEl.scrollTop = timelineEl.scrollHeight;
    updateControls();
  }

  function renderRunTree(run, children, depth, ancestors) {
    const container = node("div", null, depth ? "child-run" : "root-run");
    container.dataset.runId = run.runId;
    container.dataset.parentRunId = run.parentRunId ?? "";
    container.append(renderRun(run));
    if (ancestors.has(run.runId)) {
      container.append(node("p", "检测到异常的 Subagent 层级循环，已停止渲染。", "error"));
      return container;
    }
    const nextAncestors = new Set(ancestors);
    nextAncestors.add(run.runId);
    for (const child of children.get(run.runId) ?? []) {
      container.append(renderRunTree(child, children, depth + 1, nextAncestors));
    }
    return container;
  }

  function renderRun(run) {
    const article = node("article", null, `run${run.parentRunId ? " subagent-run" : ""}`);
    if (!run.parentRunId && run.user.length) article.append(message("你", run.user, "user"));
    if (run.parentRunId) {
      const header = node("div", null, "subagent-header");
      header.append(
        node("strong", run.agentName || "子任务"),
        node("span", `Run ${shortId(run.runId)} · parent ${shortId(run.parentRunId)}`),
      );
      article.append(header);
      if (run.task) article.append(node("p", run.task, "subagent-task"));
    }
    const assistant = message(run.parentRunId ? "子任务结果" : "OfferAgent", run.assistant, "assistant");
    if (run.reasoning) {
      const details = node("details", null, "reasoning");
      details.append(node("summary", "推理摘要"), node("p", run.reasoning));
      assistant.prepend(details);
    }
    if (!run.assistant.length && !terminal(run.status)) assistant.append(node("p", run.progress || phaseText(run.phase)));
    if (run.error?.userVisibleMessage) assistant.append(node("p", run.error.userVisibleMessage, "error"));
    article.append(assistant);
    for (const tool of run.tools.values()) article.append(renderTool(tool));
    for (const approval of run.approvals.values()) article.append(renderApproval(run, approval));
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
    details.append(node("summary", tool.progress || "参数与结果"), node("pre", boundedJson(tool.arguments)));
    if (tool.result) details.append(node("pre", boundedJson(tool.result)));
    card.append(details);
    if (tool.artifacts.length) card.append(renderArtifactLinks(tool.artifacts, "查看工具 Artifact"));
    return card;
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

  async function showMemoryManager(scope = state.memoryScope, append = false) {
    if (state.capabilities.memory !== true) {
      openInspector("Memory 管理");
      inspectorEl.append(
        node("p", "当前 Worker 未协商 Memory 管理能力。请升级 Runtime 或在 Obsidian 诊断中检查协议能力。", "error"),
      );
      return;
    }
    if (!["session", "workspace", "profile"].includes(scope)) throw new Error("Memory scope 无效");
    if (scope === "session" && !state.sessionId) throw new Error("请先选择一个会话再查看 Session Memory");
    state.memoryScope = scope;
    const params = {
      scope,
      sessionId: scope === "session" ? state.sessionId : null,
      statuses: ["active", "proposed", "review_required", "superseded", "rejected", "deleted", "expired"],
      afterId: append ? state.memoryNextAfterId : null,
      limit: 100,
    };
    const [settingsResult, listResult] = await Promise.all([
      append && state.memorySettings ? Promise.resolve({ settings: state.memorySettings }) : command("memory/settings", {}),
      command("memory/list", params),
    ]);
    state.memorySettings = settingsResult.settings;
    const page = Array.isArray(listResult.memories) ? listResult.memories : [];
    state.memories = append
      ? [...new Map([...state.memories, ...page].map((memory) => [memory.memoryId, memory])).values()]
      : page;
    state.memoryNextAfterId = listResult.nextAfterId ?? null;
    renderMemoryManager();
  }

  function renderMemoryManager() {
    const settings = state.memorySettings;
    if (!settings) return;
    openInspector("Memory 管理");
    const settingsCard = node("section", null, "capability-card memory-settings");
    settingsCard.append(node("h3", "设置"));
    const enabled = checkboxControl("启用 Workspace/Session Memory", settings.enabled === true);
    const profile = checkboxControl("允许 Profile Memory 跨 Workspace", settings.profileMemoryEnabled === true);
    const autoAccept = checkboxControl(
      "自动接受用户明确、低敏感度偏好",
      settings.autoAcceptExplicitLowSensitivity === true,
    );
    const synchronizeSettings = () => {
      if (!enabled.input.checked) {
        profile.input.checked = false;
        autoAccept.input.checked = false;
      }
      profile.input.disabled = !enabled.input.checked;
      autoAccept.input.disabled = !enabled.input.checked;
    };
    enabled.input.addEventListener("change", synchronizeSettings);
    synchronizeSettings();
    settingsCard.append(enabled.label, profile.label, autoAccept.label);
    const settingsActions = node("div", null, "memory-actions");
    const apply = node("button", "应用设置");
    apply.type = "button";
    apply.onclick = () =>
      configureMemory(
        enabled.input.checked,
        profile.input.checked,
        autoAccept.input.checked,
        false,
      ).catch(showError);
    const disableAndPurge = node("button", "关闭并彻底清除", "deny");
    disableAndPurge.type = "button";
    disableAndPurge.onclick = () => {
      if (
        window.confirm(
          "这会关闭 Memory 并请求清除当前 Worker 管理的 Memory；该操作不可撤销。确认继续？",
        )
      ) {
        configureMemory(false, false, false, true).catch(showError);
      }
    };
    settingsActions.append(apply, disableAndPurge);
    settingsCard.append(
      settingsActions,
      node(
        "small",
        `Config revision ${settings.configLayerRevision} · Memory revision ${settings.memoryRevision}`,
      ),
    );
    inspectorEl.append(settingsCard);

    const toolbar = node("div", null, "memory-toolbar");
    const scope = node("select");
    scope.setAttribute?.("aria-label", "Memory scope");
    for (const [value, label] of [
      ["workspace", "Workspace"],
      ["session", "当前 Session"],
      ["profile", "Profile"],
    ]) {
      const option = node("option", label);
      option.value = value;
      option.disabled = value === "session" && !state.sessionId;
      scope.append(option);
    }
    scope.value = state.memoryScope;
    scope.addEventListener("change", () => showMemoryManager(scope.value, false).catch(showError));
    const refresh = node("button", "刷新");
    refresh.type = "button";
    refresh.onclick = () => showMemoryManager(state.memoryScope, false).catch(showError);
    const exportFormat = node("select");
    for (const value of ["markdown", "json"]) {
      const option = node("option", value.toUpperCase());
      option.value = value;
      exportFormat.append(option);
    }
    exportFormat.value = "markdown";
    const exportButton = node("button", "导出稳定快照");
    exportButton.type = "button";
    exportButton.onclick = () => startMemoryExport(exportFormat.value).catch(showError);
    toolbar.append(scope, refresh, exportFormat, exportButton);
    inspectorEl.append(toolbar);

    const list = node("div", null, "memory-list");
    for (const memory of state.memories) list.append(renderMemorySummary(memory));
    if (!state.memories.length) list.append(node("p", "此 scope 暂无 Memory。", "empty-inline"));
    inspectorEl.append(list);
    if (state.memoryNextAfterId) {
      const more = node("button", "加载下一页（最多 100 条）");
      more.type = "button";
      more.onclick = () => showMemoryManager(state.memoryScope, true).catch(showError);
      inspectorEl.append(more);
    }
  }

  function renderMemorySummary(memory) {
    const card = node("article", null, `memory-card ${memory.status ?? ""}`);
    const heading = node("div", null, "memory-card-heading");
    heading.append(
      node("strong", `${memory.scope ?? "memory"} · ${memory.status ?? "unknown"}`),
      node("span", `rev ${memory.revision ?? "-"}`),
    );
    card.append(
      heading,
      node("p", memory.contentPreview ?? ""),
      node(
        "small",
        `${memory.sensitivity ?? "-"} · confidence ${formatConfidence(memory.confidence)} · ${memory.updatedAt ?? ""}`,
      ),
    );
    if (Array.isArray(memory.reviewReasonCodes) && memory.reviewReasonCodes.length) {
      card.append(node("p", `需确认：${memory.reviewReasonCodes.join("、")}`, "warning"));
    }
    const view = node("button", "查看/管理");
    view.type = "button";
    view.onclick = () => showMemoryDetail(memory).catch(showError);
    card.append(view);
    return card;
  }

  async function configureMemory(enabled, profileMemoryEnabled, autoAccept, purge) {
    const settings = state.memorySettings;
    if (!settings) throw new Error("Memory 设置尚未加载");
    const result = await command("memory/configure", {
      enabled,
      profileMemoryEnabled,
      autoAcceptExplicitLowSensitivity: autoAccept,
      expectedConfigRevision: settings.configLayerRevision,
      clientRequestId: opaque("req_"),
      purge,
    });
    state.memorySettings = result.settings;
    setStatus(
      purge
        ? `Memory 已关闭并清除 ${result.purged} 条${result.purgeRemaining ? "；仍有待清除记录，请再次执行" : ""}`
        : "Memory 设置已按 config revision CAS 更新",
    );
    await showMemoryManager(state.memoryScope, false);
  }

  async function showMemoryDetail(summary) {
    const result = await command("memory/get", {
      memoryId: summary.memoryId,
      sessionId: summary.scope === "session" ? summary.sessionId : null,
    });
    const memory = result.memory;
    if (!memory || memory.memoryId !== summary.memoryId) throw new Error("Memory 详情身份不匹配");
    openInspector(`Memory ${shortId(memory.memoryId)}`);
    const back = node("button", "返回 Memory 列表");
    back.type = "button";
    back.onclick = () => renderMemoryManager();
    inspectorEl.append(back);
    const metadata = node("dl", null, "diagnostic-grid");
    for (const [key, value] of [
      ["Scope", memory.scope],
      ["状态", memory.status],
      ["敏感度", memory.sensitivity],
      ["置信度", formatConfidence(memory.confidence)],
      ["Revision", memory.revision],
      ["Content hash", memory.contentHash],
      ["更新时间", memory.updatedAt],
    ]) {
      metadata.append(node("dt", key), node("dd", String(value ?? "-")));
    }
    const content = node("textarea", null, "memory-editor");
    content.rows = 10;
    content.maxLength = 262_144;
    content.value = memory.content;
    const confidence = node("input");
    confidence.type = "number";
    confidence.min = "0";
    confidence.max = "1";
    confidence.step = "0.01";
    confidence.value = String(memory.confidence);
    const confirmation = renderMemoryConfirmations(memory);
    inspectorEl.append(metadata, node("h3", "内容"), content, node("label", "置信度"), confidence, confirmation.box);

    const actions = node("div", null, "memory-actions");
    if (["proposed", "review_required"].includes(memory.status)) {
      const approve = node("button", "确认并接受");
      approve.type = "button";
      approve.onclick = () => reviewMemory(memory, "approve", confirmation.value()).catch(showError);
      const reject = node("button", "拒绝提案", "deny");
      reject.type = "button";
      reject.onclick = () => reviewMemory(memory, "reject", confirmation.value()).catch(showError);
      actions.append(approve, reject);
    }
    if (!["deleted", "expired"].includes(memory.status)) {
      const save = node("button", "按 Revision 保存编辑");
      save.type = "button";
      save.onclick = () =>
        editMemory(memory, content.value, Number(confidence.value), confirmation.value()).catch(showError);
      const remove = node("button", "删除", "deny");
      remove.type = "button";
      remove.onclick = () => deleteMemory(memory).catch(showError);
      actions.append(save, remove);
    }
    inspectorEl.append(actions);
    const provenance = node("details", null, "memory-provenance");
    provenance.append(node("summary", `Provenance（${memory.provenances?.length ?? 0}）`));
    const provenanceText = node("pre");
    provenanceText.textContent = boundedJson(memory.provenances ?? []);
    provenance.append(provenanceText);
    inspectorEl.append(provenance);
  }

  function renderMemoryConfirmations(memory) {
    const box = node("fieldset", null, "memory-confirmations");
    box.append(node("legend", "显式确认（仅在对应风险确实可接受时勾选）"));
    const sensitive = checkboxControl("允许保存 private/敏感内容", false);
    const profile = checkboxControl("允许 Profile 跨 Workspace 共享", false);
    const external = checkboxControl("允许来自外部来源", false);
    const conflict = checkboxControl("允许冲突解决/覆盖旧 Memory", false);
    const supersede = node("select");
    const noSupersede = node("option", "不指定被替代 Memory");
    noSupersede.value = "";
    supersede.append(noSupersede);
    for (const memoryId of memory.conflictMemoryIds ?? []) {
      const option = node("option", `替代冲突记录 ${shortId(memoryId)}`);
      option.value = memoryId;
      supersede.append(option);
    }
    const reason = node("textarea");
    reason.rows = 2;
    reason.maxLength = 4096;
    reason.placeholder = "可选：说明确认理由";
    box.append(sensitive.label, profile.label, external.label, conflict.label);
    if ((memory.conflictMemoryIds?.length ?? 0) > 0) box.append(supersede);
    box.append(reason);
    if (memory.sensitivity === "private") box.append(node("p", "此记录为 private，接受或编辑通常需要敏感内容确认。", "warning"));
    return {
      box,
      value: () => ({
        allowSensitive: sensitive.input.checked,
        allowProfileSharing: profile.input.checked,
        allowExternalSource: external.input.checked,
        allowConflictResolution: conflict.input.checked,
        reason: reason.value.trim() || null,
        supersedeMemoryId: supersede.value || null,
      }),
    };
  }

  async function reviewMemory(memory, decision, confirmation) {
    const { supersedeMemoryId, ...reviewConfirmation } = confirmation;
    let expectedSupersedeRevision = null;
    if (decision === "approve" && supersedeMemoryId) {
      const target = await command("memory/get", {
        memoryId: supersedeMemoryId,
        sessionId: memory.scope === "session" ? memory.sessionId : null,
      });
      expectedSupersedeRevision = target.memory?.revision ?? null;
      if (!Number.isSafeInteger(expectedSupersedeRevision)) throw new Error("冲突 Memory 缺少可用 revision");
    }
    const result = await command("memory/review", {
      memoryId: memory.memoryId,
      sessionId: memory.scope === "session" ? memory.sessionId : null,
      decision,
      expectedRevision: memory.revision,
      clientRequestId: opaque("req_"),
      confirmation: reviewConfirmation,
      supersedeMemoryId: decision === "approve" ? supersedeMemoryId : null,
      expectedSupersedeRevision: decision === "approve" ? expectedSupersedeRevision : null,
    });
    setStatus(`Memory review 已提交：${result.status}`);
    await showMemoryManager(state.memoryScope, false);
  }

  async function editMemory(memory, content, confidence, confirmation) {
    const { supersedeMemoryId: _supersedeMemoryId, ...editConfirmation } = confirmation;
    if (!content.trim()) throw new Error("Memory 内容不能为空");
    if (!Number.isFinite(confidence) || confidence < 0 || confidence > 1) throw new Error("Memory 置信度必须在 0–1");
    const result = await command("memory/edit", {
      memoryId: memory.memoryId,
      sessionId: memory.scope === "session" ? memory.sessionId : null,
      content,
      confidence,
      expectedRevision: memory.revision,
      clientRequestId: opaque("req_"),
      confirmation: editConfirmation,
    });
    setStatus(`Memory 已编辑：${result.status} · revision ${result.revision ?? "-"}`);
    await showMemoryManager(state.memoryScope, false);
  }

  async function deleteMemory(memory) {
    if (!window.confirm("删除此 Memory？删除使用 revision CAS，已被并发修改时会拒绝。")) return;
    const result = await command("memory/delete", {
      memoryId: memory.memoryId,
      sessionId: memory.scope === "session" ? memory.sessionId : null,
      expectedRevision: memory.revision,
      clientRequestId: opaque("req_"),
    });
    setStatus(`Memory 已删除：${result.status}`);
    await showMemoryManager(state.memoryScope, false);
  }

  async function startMemoryExport(format) {
    if (!["markdown", "json"].includes(format)) throw new Error("Memory 导出格式无效");
    state.memoryExport = {
      format,
      includeSessionId: state.memoryScope === "session" ? state.sessionId : null,
      snapshotAt: null,
      contentHash: null,
      offset: 0,
      totalByteSize: null,
      exportedCount: null,
      eof: false,
      content: "",
    };
    await readMemoryExportPage();
  }

  async function readMemoryExportPage() {
    const view = state.memoryExport;
    if (!view || view.eof || view.offset >= MEMORY_EXPORT_TOTAL_BYTES) return;
    const requestedOffset = view.offset;
    const result = await command("memory/export", {
      format: view.format,
      includeSessionId: view.includeSessionId,
      snapshotAt: view.snapshotAt,
      expectedContentHash: view.contentHash,
      offset: requestedOffset,
      maxBytes: MEMORY_EXPORT_PAGE_BYTES,
    });
    const contentBytes = new TextEncoder().encode(result.content ?? "").length;
    if (
      result.format !== view.format ||
      result.offset !== requestedOffset ||
      !Number.isSafeInteger(result.nextOffset) ||
      result.nextOffset !== requestedOffset + contentBytes ||
      (!result.eof && result.nextOffset <= requestedOffset) ||
      !Number.isSafeInteger(result.totalByteSize) ||
      result.totalByteSize > MEMORY_EXPORT_TOTAL_BYTES ||
      result.nextOffset > result.totalByteSize ||
      result.eof !== (result.nextOffset === result.totalByteSize) ||
      (view.snapshotAt !== null && result.snapshotAt !== view.snapshotAt) ||
      (view.contentHash !== null && result.contentHash !== view.contentHash)
    ) {
      throw new Error("Memory 导出分页或快照证明无效");
    }
    view.snapshotAt = result.snapshotAt;
    view.contentHash = result.contentHash;
    view.offset = result.nextOffset;
    view.totalByteSize = result.totalByteSize;
    view.exportedCount = result.exportedCount;
    view.eof = result.eof;
    view.content += result.content;
    renderMemoryExport();
  }

  function renderMemoryExport() {
    const view = state.memoryExport;
    if (!view) return;
    openInspector("Memory 稳定快照导出");
    const metadata = node("dl", null, "diagnostic-grid");
    for (const [key, value] of [
      ["格式", view.format],
      ["Snapshot", view.snapshotAt],
      ["Content hash", view.contentHash],
      ["记录数", view.exportedCount],
      ["进度", `${view.offset}/${view.totalByteSize ?? "?"} bytes`],
    ]) {
      metadata.append(node("dt", key), node("dd", String(value ?? "-")));
    }
    const content = node("pre", null, "memory-export-content");
    content.textContent = view.content;
    inspectorEl.append(metadata, content);
    if (!view.eof) {
      const next = node("button", "读取下一页（快照/hash 固定）");
      next.type = "button";
      next.onclick = () => readMemoryExportPage().catch(showError);
      inspectorEl.append(next);
    } else {
      const download = node("button", "保存导出文件");
      download.type = "button";
      download.onclick = () => downloadMemoryExport(view);
      inspectorEl.append(download);
    }
  }

  function downloadMemoryExport(view) {
    if (!view.eof) throw new Error("Memory 导出尚未完成");
    const mediaType = view.format === "json" ? "application/json" : "text/markdown";
    const url = URL.createObjectURL(new Blob([view.content], { type: `${mediaType};charset=utf-8` }));
    const link = node("a");
    link.href = url;
    link.download = `offeragent-memory-${view.snapshotAt.replace(/[:.]/g, "-")}.${view.format === "json" ? "json" : "md"}`;
    link.click();
    setTimeout(() => URL.revokeObjectURL(url), 0);
  }

  function checkboxControl(labelText, checked) {
    const label = node("label", null, "checkbox-control");
    const input = node("input");
    input.type = "checkbox";
    input.checked = checked;
    label.append(input, node("span", labelText));
    return { label, input };
  }

  function formatConfidence(value) {
    return Number.isFinite(value) ? Number(value).toFixed(2) : "-";
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
    restoreSkillSelection();
  }

  function restoreSkillSelection() {
    const available = new Set(
      (Array.isArray(state.skillCatalog?.skills) ? state.skillCatalog.skills : [])
        .filter((skill) => skill?.enabled === true && typeof skill.name === "string")
        .map((skill) => skill.name),
    );
    let stored = [];
    try {
      const parsed = JSON.parse(sessionStorage.getItem(skillStorageKey()) ?? "[]");
      if (Array.isArray(parsed)) stored = parsed.filter((value) => typeof value === "string").slice(0, 256);
    } catch {
      stored = [];
    }
    state.selectedSkills = new Set(stored.filter((name) => available.has(name)));
    persistSkillSelection();
  }

  function persistSkillSelection() {
    sessionStorage.setItem(skillStorageKey(), JSON.stringify([...state.selectedSkills].sort()));
  }

  async function showCapabilities() {
    openInspector("能力与管理入口");
    inspectorEl.append(node("p", "正在读取 Skills / Shell / Hooks 的 Worker 状态…"));
    await loadExtensionCatalogs();
    openInspector("能力与管理入口");
    const intro = node(
      "p",
      "此页面读取同一 Worker 的真实管理目录。信任、安装、启停等持久管理变更必须回到认证 Obsidian Named Pipe；只有在 Obsidian 关闭并完成两次显式确认后，页面才可授权一个普通 Turn 走本地 Vault 事务。",
    );
    inspectorEl.append(intro);
    const managed = [
      ["模型", "可用", "使用 models/list 与 models/health；在输入区选择。"],
      ["Memory", state.capabilities.memory === true ? "可用" : "未协商", "使用 memory/settings/configure/list/get/review/edit/delete/export；点击顶栏 Memory 管理。"],
      ["Skills", state.capabilities.skills === true ? "目录已读取" : "未协商", "可在下方选择本页面新 Run 使用的已信任 Skills；信任确认在 Obsidian 设置中完成。"],
      ["Shell", state.capabilities.shell === true ? "目录已读取" : "未协商", "下方显示持久 profile、revision、信任与启停状态。"],
      ["Hooks", state.capabilities.hooks === true ? "目录已读取" : "未协商", "下方显示 layer、contentHash trust 与 Workspace command definitionHash 确认状态。"],
      ["无头 Vault 写入", state.capabilities.headlessVaultWrite === true ? "显式一次性授权" : "未协商", state.headlessVaultWrite?.userMessage ?? "状态尚未读取。"],
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
      const row = node("label", null, `extension-row${skill.enabled === true ? "" : " unavailable"}`);
      const checkbox = node("input");
      checkbox.type = "checkbox";
      checkbox.checked = state.selectedSkills.has(skill.name);
      checkbox.disabled = skill.enabled !== true;
      checkbox.addEventListener("change", () => {
        if (checkbox.checked && skill.enabled === true) state.selectedSkills.add(skill.name);
        else state.selectedSkills.delete(skill.name);
        persistSkillSelection();
      });
      row.append(
        checkbox,
        node("span", `${skill.name}@${skill.version}`),
        node("small", `${skill.layer} · ${skill.trustState}`),
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
    sendEl.disabled = busy || active !== null || selectedModel() === null ||
      (writeRequiredEl.checked && !writeTargetPathsEl.value.trim());
    steerEl.disabled = busy || active === null || !promptEl.value.trim();
    cancelEl.disabled = busy || active === null;
    modelHealthEl.disabled = busy || selectedModel() === null;
    modelEl.disabled = busy || state.models.size === 0;
    writeRequiredEl.disabled = busy || active !== null;
    writeTargetPathsEl.disabled = busy || active !== null;
    headlessRequestEl.disabled = busy || active !== null;
    headlessActivateEl.disabled = busy || active !== null;
    headlessRevokeEl.disabled = busy;
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
    if (reference?.type === "memory") return `memory:${reference.memoryId}`;
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
    if (reference?.type === "memory") return `${reference.label ?? "Memory"} · ${reference.scope}`;
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
      user: [],
      assistant: [],
      snapshotAssistant: [],
      reasoning: "",
      progress: "",
      task: "",
      agentName: "root",
      depth: 0,
      tools: new Map(),
      approvals: new Map(),
      artifacts: new Set(),
      references: [],
      compactions: [],
      steers: [],
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
        running: "运行中",
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
        "tool.queued": "已排队",
        "tool.started": "执行中",
        "tool.progress": "执行中",
      }[value] ?? value ?? "未知"
    );
  }

  function phaseText(value) {
    return (
      {
        created: "已创建…",
        loading_context: "正在加载上下文…",
        selecting_memory: "正在选择 Memory…",
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

  function modelKey(provider, model) {
    return `${encodeURIComponent(provider)}::${encodeURIComponent(model)}`;
  }

  function modelStorageKey() {
    return `offeragent.model.${state.workspaceId}`;
  }

  function skillStorageKey() {
    return `offeragent.skills.${state.workspaceId}`;
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
  writeRequiredEl.addEventListener("change", () => {
    writeTargetPathsEl.hidden = !writeRequiredEl.checked;
    updateControls();
  });
  writeTargetPathsEl.addEventListener("input", updateControls);
  promptEl.addEventListener("keydown", (event) => {
    if (event.key === "Enter" && !event.shiftKey && !event.isComposing) {
      event.preventDefault();
      if (activeRootRun()) steerActive().catch(showError);
      else sendTurn(promptEl.value).catch(showError);
    }
  });
  modelEl.addEventListener("change", () => selectModel(modelEl.value));
  modelHealthEl.addEventListener("click", () => checkModelHealth().catch(showError));
  el("new-session").addEventListener("click", () => createSession().catch(showError));
  el("rename-session").addEventListener("click", () => renameSession().catch(showError));
  el("delete-session").addEventListener("click", () => deleteSession().catch(showError));
  el("compact-session").addEventListener("click", () => compactSession().catch(showError));
  steerEl.addEventListener("click", () => steerActive().catch(showError));
  cancelEl.addEventListener("click", () => cancelActive().catch(showError));
  el("memory").addEventListener("click", () => showMemoryManager().catch(showError));
  headlessRequestEl.addEventListener("click", () => requestHeadlessVaultWrite().catch(showError));
  headlessActivateEl.addEventListener("click", () => approveAndActivateHeadlessVaultWrite().catch(showError));
  headlessRevokeEl.addEventListener("click", () => revokeHeadlessVaultWrite().catch(showError));
  el("capabilities").addEventListener("click", () => showCapabilities().catch(showError));
  el("diagnostics").addEventListener("click", () => showDiagnostics().catch(showError));
  window.addEventListener("beforeunload", () => {
    state.stopped = true;
    clearInterval(state.poll);
  });
  void boot();
})();
