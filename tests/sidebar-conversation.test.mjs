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
      };
    },
    async *runAgent(request) {
      assert.equal(request.model, "model-b");
      assert.equal(request.input, "Tell me about yourself.");
      yield { type: "agent_run.started", model: request.model };
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
