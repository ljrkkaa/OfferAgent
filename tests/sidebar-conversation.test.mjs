import assert from "node:assert/strict";
import { createRequire } from "node:module";
import path from "node:path";
import test from "node:test";
import { fileURLToPath } from "node:url";

const repositoryRoot = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..");
const require = createRequire(import.meta.url);
const { SidebarController } = require(
  path.join(repositoryRoot, "packages", "plugin", "dist", "sidebar-controller.js"),
);
const { RuntimeRequestError } = require(
  path.join(repositoryRoot, "packages", "plugin", "dist", "runtime-supervisor.js"),
);

test("the Sidebar selects a model and renders a streamed Agent Run", async () => {
  let unavailableSubscriber;
  const runtime = {
    cancelAgentRun() {},
    async updateConversationModel(conversationId, modelId) {
      return { id: conversationId, title: "Conversation A", modelId };
    },
    async createConversation(conversation) {
      return conversation;
    },
    async deleteConversation() {},
    onUnavailable(subscriber) {
      unavailableSubscriber = subscriber;
      return () => (unavailableSubscriber = undefined);
    },
    async start() {},
    async stop() {},
    async listModels() {
      return [
        { id: "model-a", label: "Model A" },
        { id: "model-b", label: "Model B" },
      ];
    },
    async listConversations() {
      return [{ id: "conversation-a", title: "Conversation A", modelId: "model-a" }];
    },
    async openConversation() {
      return {
        conversation: { id: "conversation-a", title: "Conversation A", modelId: "model-a" },
        messages: [],
        agentRuns: [],
        toolCalls: [],
      };
    },
    async *runAgent(request) {
      assert.equal(request.model, "model-b");
      assert.equal(request.input, "Tell me about yourself.");
      yield { type: "agent_run.started", model: request.model };
      yield {
        type: "tool_call.requested",
        toolCallId: "sidebar-tool-call",
        tool: { kind: "local", name: "vault_read", arguments: { path: "notes/a.md" } },
      };
      yield {
        type: "tool_call.completed",
        toolCallId: "sidebar-tool-call",
        tool: { kind: "local", name: "vault_read" },
        status: "completed",
      };
      yield { type: "agent_run.delta", delta: "Strong " };
      yield { type: "agent_run.delta", delta: "answer" };
      yield { type: "agent_run.completed", output: { role: "assistant", text: "Strong answer" } };
    },
  };
  const controller = new SidebarController(runtime);
  const observed = [];
  controller.subscribe((viewModel) => observed.push(structuredClone(viewModel)));

  await controller.start();
  assert.equal(controller.getViewModel().runtime.state, "connected");
  assert.deepEqual(controller.getViewModel().conversation.models, [
    { id: "model-a", label: "Model A" },
    { id: "model-b", label: "Model B" },
  ]);
  assert.equal(controller.getViewModel().conversation.selectedModelId, "model-a");

  controller.selectModel("model-b");
  await controller.sendMessage("Tell me about yourself.");

  const final = controller.getViewModel();
  assert.equal(final.conversation.runState, "idle");
  assert.deepEqual(final.conversation.messages, [
    { role: "user", text: "Tell me about yourself." },
    { role: "assistant", text: "Strong answer" },
  ]);
  assert.equal(final.conversation.agentRuns.at(-1).status, "completed");
  assert.deepEqual(final.conversation.toolCalls, [
    {
      id: "sidebar-tool-call",
      agentRunId: final.conversation.agentRuns.at(-1).id,
      name: "vault_read",
      arguments: { path: "notes/a.md" },
      status: "completed",
    },
  ]);
  assert.ok(
    observed.some(
      (viewModel) =>
        viewModel.conversation.runState === "streaming" &&
        viewModel.conversation.messages.at(-1)?.text === "Strong ",
    ),
  );
});

test("the Sidebar preserves a typed model-catalog error", async () => {
  const runtime = {
    cancelAgentRun() {},
    async updateConversationModel(conversationId, modelId) {
      return { id: conversationId, title: "Conversation", modelId };
    },
    async createConversation(conversation) {
      return conversation;
    },
    async deleteConversation() {},
    onUnavailable() {
      return () => {};
    },
    async start() {},
    async stop() {},
    async listModels() {
      throw new RuntimeRequestError("auth_required", "Sign in to Codex and retry.");
    },
    async listConversations() {
      return [];
    },
    async openConversation() {
      throw new Error("not reached");
    },
    async *runAgent() {},
  };
  const controller = new SidebarController(runtime);
  await controller.start();
  assert.deepEqual(controller.getViewModel().conversation.error, {
    code: "auth_required",
    message: "Sign in to Codex and retry.",
  });
  assert.equal(controller.getViewModel().runtime.state, "connected");
});

test("the Sidebar creates, switches, and deletes Conversations", async () => {
  const conversations = [
    { id: "conversation-a", title: "Conversation A", modelId: "model-a" },
    { id: "conversation-b", title: "Conversation B", modelId: "model-a" },
  ];
  const runtime = {
    cancelAgentRun() {},
    async updateConversationModel(conversationId, modelId) {
      const conversation = conversations.find((candidate) => candidate.id === conversationId);
      conversation.modelId = modelId;
      return conversation;
    },
    async createConversation(conversation) {
      conversations.unshift(conversation);
      return conversation;
    },
    async deleteConversation(conversationId) {
      conversations.splice(
        conversations.findIndex((conversation) => conversation.id === conversationId),
        1,
      );
    },
    async listConversations() {
      return [...conversations];
    },
    async listModels() {
      return [{ id: "model-a", label: "Model A" }];
    },
    onUnavailable() {
      return () => {};
    },
    async openConversation(conversationId) {
      const conversation = conversations.find((candidate) => candidate.id === conversationId);
      return {
        conversation,
        agentRuns: [],
        messages: [
          {
            id: `message-${conversationId}`,
            agentRunId: `run-${conversationId}`,
            role: "user",
            text: `history ${conversationId}`,
            sequence: 1,
          },
        ],
      };
    },
    async *runAgent() {},
    async start() {},
    async stop() {},
  };
  const controller = new SidebarController(runtime);
  await controller.start();
  assert.equal(controller.getViewModel().conversation.activeConversationId, "conversation-a");

  await controller.openConversation("conversation-b");
  assert.deepEqual(controller.getViewModel().conversation.messages, [
    { role: "user", text: "history conversation-b" },
  ]);

  await controller.createConversation("Fresh Conversation");
  const createdId = controller.getViewModel().conversation.activeConversationId;
  assert.equal(controller.getViewModel().conversation.conversations[0].title, "Fresh Conversation");
  assert.deepEqual(controller.getViewModel().conversation.messages, []);

  await controller.deleteCurrentConversation();
  assert.notEqual(controller.getViewModel().conversation.activeConversationId, createdId);
  assert.equal(controller.getViewModel().conversation.conversations.length, 2);
});

test("stopping an active Sidebar run makes it visibly cancelled", async () => {
  let releaseRun;
  const cancelled = new Promise((resolve) => (releaseRun = resolve));
  let cancelledRequest;
  const runtime = {
    cancelAgentRun(request) {
      cancelledRequest = request;
      releaseRun();
    },
    async updateConversationModel(conversationId, modelId) {
      return { id: conversationId, title: "Stop test", modelId };
    },
    async createConversation(conversation) {
      return conversation;
    },
    async deleteConversation() {},
    async listConversations() {
      return [{ id: "conversation-stop", title: "Stop test", modelId: "model-a" }];
    },
    async listModels() {
      return [{ id: "model-a", label: "Model A" }];
    },
    onUnavailable() {
      return () => {};
    },
    async openConversation() {
      return {
        conversation: { id: "conversation-stop", title: "Stop test", modelId: "model-a" },
        agentRuns: [],
        messages: [],
      };
    },
    async *runAgent(request) {
      yield { type: "agent_run.started", model: request.model };
      yield {
        type: "tool_call.requested",
        toolCallId: "cancelled-sidebar-tool",
        tool: { kind: "local", name: "vault_read", arguments: { path: "notes/slow.md" } },
      };
      await cancelled;
      yield { type: "agent_run.cancelled" };
    },
    async start() {},
    async stop() {},
  };
  const controller = new SidebarController(runtime);
  await controller.start();
  const sending = controller.sendMessage("Cancel this run.");
  await new Promise((resolve) => setImmediate(resolve));
  assert.equal(controller.getViewModel().conversation.runState, "streaming");
  assert.equal(controller.getViewModel().conversation.agentRuns.at(-1).status, "running");
  controller.stopAgentRun();
  await sending;

  assert.equal(cancelledRequest.conversationId, "conversation-stop");
  assert.equal(controller.getViewModel().conversation.agentRuns.at(-1).status, "cancelled");
  assert.equal(controller.getViewModel().conversation.toolCalls.at(-1).status, "failed");
  assert.deepEqual(controller.getViewModel().conversation.messages, [
    { role: "user", text: "Cancel this run." },
  ]);
});

test("a failed Agent Run remains visible with its typed error", async () => {
  const runtime = {
    cancelAgentRun() {},
    async createConversation(conversation) {
      return conversation;
    },
    async deleteConversation() {},
    async listConversations() {
      return [{ id: "conversation-fail", title: "Failure", modelId: "model-a" }];
    },
    async listModels() {
      return [{ id: "model-a", label: "Model A" }];
    },
    onUnavailable() {
      return () => {};
    },
    async openConversation() {
      return {
        conversation: { id: "conversation-fail", title: "Failure", modelId: "model-a" },
        agentRuns: [],
        messages: [],
      };
    },
    async *runAgent() {
      yield { type: "agent_run.started", model: "model-a" };
      yield {
        type: "agent_run.failed",
        error: { code: "provider_error", message: "Provider rejected the request." },
      };
    },
    async start() {},
    async stop() {},
    async updateConversationModel(conversationId, modelId) {
      return { id: conversationId, title: "Failure", modelId };
    },
  };
  const controller = new SidebarController(runtime);
  await controller.start();
  await controller.sendMessage("Fail this run.");
  assert.equal(controller.getViewModel().conversation.agentRuns.at(-1).status, "failed");
  assert.deepEqual(controller.getViewModel().conversation.error, {
    code: "provider_error",
    message: "Provider rejected the request.",
  });
  assert.deepEqual(controller.getViewModel().conversation.messages, [
    { role: "user", text: "Fail this run." },
  ]);
});

test("the Sidebar exposes one whole-batch decision and guarded undo", async () => {
  let releaseDecision;
  const decisionMade = new Promise((resolve) => (releaseDecision = resolve));
  const proposal = {
    batchId: "sidebar-batch",
    idempotencyKey: "sidebar-batch-key",
    task: "Update interview progress",
    actions: [
      {
        actionId: "sidebar-action",
        idempotencyKey: "sidebar-action-key",
        operation: "append",
        path: "notes/progress.md",
        expectedVersion: "mtime:1:size:4",
        content: "done\n",
      },
    ],
  };
  const runtime = {
    cancelAgentRun() {},
    async createConversation(conversation) {
      return conversation;
    },
    async deleteConversation() {},
    async listConversations() {
      return [{ id: "conversation-change", title: "Changes", modelId: "model-a" }];
    },
    async listModels() {
      return [{ id: "model-a", label: "Model A" }];
    },
    onUnavailable() {
      return () => {};
    },
    async openConversation() {
      return {
        conversation: { id: "conversation-change", title: "Changes", modelId: "model-a" },
        agentRuns: [],
        messages: [],
        toolCalls: [],
      };
    },
    async *runAgent() {
      yield { type: "agent_run.started", model: "model-a" };
      yield {
        type: "tool_call.requested",
        toolCallId: "sidebar-change-call",
        tool: { kind: "local", name: "vault_propose_changes", arguments: proposal },
      };
      await decisionMade;
      yield {
        type: "tool_call.completed",
        toolCallId: "sidebar-change-call",
        tool: { kind: "local", name: "vault_propose_changes" },
        status: "completed",
      };
      yield { type: "agent_run.delta", delta: "Applied safely" };
      yield {
        type: "agent_run.completed",
        output: { role: "assistant", text: "Applied safely" },
      };
    },
    async start() {},
    async stop() {},
    async updateConversationModel(conversationId, modelId) {
      return { id: conversationId, title: "Changes", modelId };
    },
  };
  const decisions = [];
  const changeClient = {
    async decide(toolCallId, decision) {
      decisions.push({ toolCallId, decision });
      releaseDecision();
      return {
        ok: true,
        value: {
          type: "vault_propose_changes",
          batchId: proposal.batchId,
          decision: "applied",
          checkpointRef: "refs/offeragent/checkpoints/sidebar-batch",
          targets: [
            { path: "notes/progress.md", beforeHash: "sha256:before", afterHash: "sha256:after" },
          ],
        },
      };
    },
    async undo(batchId) {
      assert.equal(batchId, proposal.batchId);
      return { ok: true, value: { type: "vault_change_undo", batchId, status: "undone" } };
    },
  };
  const controller = new SidebarController(runtime, changeClient);
  await controller.start();
  const sending = controller.sendMessage("Update my progress.");
  await new Promise((resolve) => setImmediate(resolve));
  assert.deepEqual(controller.getViewModel().conversation.vaultChanges, [
    {
      toolCallId: "sidebar-change-call",
      batchId: "sidebar-batch",
      task: "Update interview progress",
      status: "pending",
      actions: [{ operation: "append", path: "notes/progress.md" }],
    },
  ]);
  await controller.decideVaultChange("sidebar-change-call", "apply");
  await sending;
  assert.deepEqual(decisions, [{ toolCallId: "sidebar-change-call", decision: "apply" }]);
  assert.equal(controller.getViewModel().conversation.vaultChanges[0].status, "applied");
  await controller.undoVaultChange("sidebar-batch");
  assert.equal(controller.getViewModel().conversation.vaultChanges[0].status, "undone");
});

test("failed and cancelled Vault Change requests cannot remain actionable", async () => {
  let releaseCancellation;
  const cancellation = new Promise((resolve) => (releaseCancellation = resolve));
  let cancelledToolCallId;
  const proposal = {
    batchId: "terminal-batch",
    idempotencyKey: "terminal-batch-key",
    task: "Do not apply after termination",
    actions: [
      {
        actionId: "terminal-action",
        idempotencyKey: "terminal-action-key",
        operation: "create",
        path: "notes/terminal.md",
        expectedVersion: "missing",
        content: "never written\n",
      },
    ],
  };
  const runtime = {
    cancelAgentRun() {
      releaseCancellation();
    },
    async createConversation(conversation) { return conversation; },
    async deleteConversation() {},
    async listConversations() {
      return [{ id: "conversation-terminal", title: "Terminal", modelId: "model-a" }];
    },
    async listModels() { return [{ id: "model-a", label: "Model A" }]; },
    onUnavailable() { return () => {}; },
    async openConversation() {
      return {
        conversation: { id: "conversation-terminal", title: "Terminal", modelId: "model-a" },
        agentRuns: [],
        messages: [],
        toolCalls: [],
      };
    },
    async *runAgent() {
      yield { type: "agent_run.started", model: "model-a" };
      yield {
        type: "tool_call.requested",
        toolCallId: "terminal-change-call",
        tool: { kind: "local", name: "vault_propose_changes", arguments: proposal },
      };
      await cancellation;
      yield { type: "agent_run.cancelled" };
    },
    async start() {},
    async stop() {},
    async updateConversationModel(conversationId, modelId) {
      return { id: conversationId, title: "Terminal", modelId };
    },
  };
  const changes = {
    cancel(toolCallId) { cancelledToolCallId = toolCallId; },
    async decide() { throw new Error("cancelled change must not be decided"); },
    async undo() { throw new Error("cancelled change was never applied"); },
  };
  const controller = new SidebarController(runtime, changes);
  await controller.start();
  const sending = controller.sendMessage("Propose then stop.");
  await new Promise((resolve) => setImmediate(resolve));
  assert.equal(controller.getViewModel().conversation.vaultChanges[0].status, "pending");
  controller.stopAgentRun();
  await sending;
  assert.equal(cancelledToolCallId, "terminal-change-call");
  assert.equal(controller.getViewModel().conversation.vaultChanges[0].status, "failed");
});

test("a failed Vault Change tool event disables its confirmation card", async () => {
  const proposal = {
    batchId: "failed-batch",
    idempotencyKey: "failed-batch-key",
    task: "Reject a stale proposal",
    actions: [{
      actionId: "failed-action",
      idempotencyKey: "failed-action-key",
      operation: "append",
      path: "notes/stale.md",
      expectedVersion: "mtime:old",
      content: "stale\n",
    }],
  };
  const runtime = {
    cancelAgentRun() {},
    async createConversation(conversation) { return conversation; },
    async deleteConversation() {},
    async listConversations() {
      return [{ id: "conversation-failed-change", title: "Failed change", modelId: "model-a" }];
    },
    async listModels() { return [{ id: "model-a", label: "Model A" }]; },
    onUnavailable() { return () => {}; },
    async openConversation() {
      return {
        conversation: { id: "conversation-failed-change", title: "Failed change", modelId: "model-a" },
        agentRuns: [], messages: [], toolCalls: [],
      };
    },
    async *runAgent() {
      yield { type: "agent_run.started", model: "model-a" };
      yield {
        type: "tool_call.requested",
        toolCallId: "failed-change-call",
        tool: { kind: "local", name: "vault_propose_changes", arguments: proposal },
      };
      yield {
        type: "tool_call.completed",
        toolCallId: "failed-change-call",
        tool: { kind: "local", name: "vault_propose_changes" },
        status: "failed",
        error: { code: "stale_evidence", message: "The proposal is stale." },
      };
      yield {
        type: "agent_run.completed",
        output: { role: "assistant", text: "I will re-read the file." },
      };
    },
    async start() {}, async stop() {},
    async updateConversationModel(conversationId, modelId) {
      return { id: conversationId, title: "Failed change", modelId };
    },
  };
  const controller = new SidebarController(runtime);
  await controller.start();
  await controller.sendMessage("Try a stale change.");
  assert.equal(controller.getViewModel().conversation.vaultChanges[0].status, "failed");
});

test("guarded undo exposes a conflict diff instead of overwriting later edits", async () => {
  const runtime = {
    cancelAgentRun() {},
    async createConversation(conversation) { return conversation; },
    async deleteConversation() {},
    async listConversations() {
      return [{ id: "conversation-conflict", title: "Conflict", modelId: "model-a" }];
    },
    async listModels() { return [{ id: "model-a", label: "Model A" }]; },
    onUnavailable() { return () => {}; },
    async openConversation() {
      return {
        conversation: { id: "conversation-conflict", title: "Conflict", modelId: "model-a" },
        agentRuns: [],
        messages: [],
        toolCalls: [{
          id: "conflict-call",
          agentRunId: "conflict-run",
          name: "vault_propose_changes",
          arguments: {
            batchId: "conflict-batch",
            task: "Update a note",
            actions: [{ operation: "append", path: "notes/conflict.md" }],
          },
          status: "completed",
          vaultChangeState: "applied",
        }],
      };
    },
    async *runAgent() {},
    async start() {}, async stop() {},
    async updateConversationModel(conversationId, modelId) {
      return { id: conversationId, title: "Conflict", modelId };
    },
  };
  const changes = {
    cancel() {},
    async decide() { throw new Error("already applied"); },
    async undo() {
      return {
        ok: false,
        error: {
          code: "undo_conflict",
          message: "Later edits must be preserved.",
          conflicts: [{
            path: "notes/conflict.md",
            appliedHash: "sha256:applied",
            currentHash: "sha256:current",
            diff: "--- current/notes/conflict.md\n+++ checkpoint/notes/conflict.md\n-later edit\n+before",
          }],
        },
      };
    },
  };
  const controller = new SidebarController(runtime, changes);
  await controller.start();
  assert.equal(controller.getViewModel().conversation.vaultChanges[0].status, "applied");
  await controller.undoVaultChange("conflict-batch");
  const change = controller.getViewModel().conversation.vaultChanges[0];
  assert.equal(change.status, "conflicted");
  assert.match(change.conflicts[0].diff, /later edit/);
});
