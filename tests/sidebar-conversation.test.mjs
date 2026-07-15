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

test("the Sidebar presentation keeps activity, composer, and common settings compact", async () => {
  let fastModeEnabled = true;
  let vaultPermissionMode = "read_only";
  const longPath = `notes/${"deep/".repeat(20)}interview.md`;
  const runtime = {
    cancelAgentRun() {},
    async createConversation(conversation) { return conversation; },
    async deleteConversation() {},
    async listConversations() {
      return [{ id: "presentation-conversation", title: "Interview", modelId: "model-fast" }];
    },
    async listModels() {
      return [{ id: "model-fast", label: "Fast Model", supportsFastMode: true }];
    },
    onUnavailable() { return () => {}; },
    async openConversation() {
      return {
        conversation: { id: "presentation-conversation", title: "Interview", modelId: "model-fast" },
        messages: [
          {
            id: "message-user",
            agentRunId: "presentation-run",
            role: "user",
            text: "Review my interview notes.",
            sequence: 1,
          },
          {
            id: "message-one",
            agentRunId: "presentation-run",
            role: "assistant",
            text: "A very long answer remains readable.",
            sequence: 4,
          },
        ],
        agentRuns: [
          {
            id: "failed-presentation-run",
            modelId: "model-fast",
            status: "failed",
            error: { code: "provider_error", message: "The earlier Provider request failed." },
          },
          { id: "presentation-run", modelId: "model-fast", status: "interrupted" },
        ],
        toolCalls: [{
          id: "presentation-tool",
          agentRunId: "presentation-run",
          name: "vault_read",
          arguments: { path: longPath },
          status: "failed",
          error: { code: "not_found", message: "The requested note no longer exists." },
        }, {
          id: "presentation-skill",
          agentRunId: "presentation-run",
          name: "skill_read",
          arguments: { skill: "interview-coach", resource: "rubric.md" },
          status: "completed",
        }, {
          id: "presentation-list",
          agentRunId: "presentation-run",
          name: "vault_list",
          arguments: {},
          status: "completed",
        }, {
          id: "presentation-probe",
          agentRunId: "presentation-run",
          name: "hosted_web_search_probe",
          arguments: {},
          status: "completed",
        }],
      };
    },
    async *resumeAgentRun() {
      yield { type: "agent_run.resumed", model: "model-fast" };
      yield { type: "agent_run.completed", output: { role: "assistant", text: "Done" } };
    },
    async *runAgent() {},
    async start() {},
    async stop() {},
    async updateConversationModel(conversationId, modelId) {
      return { id: conversationId, title: "Interview", modelId };
    },
  };
  const controller = new SidebarController(runtime, undefined, {
    getFastModeEnabled: () => fastModeEnabled,
    getVaultPermissionMode: () => vaultPermissionMode,
  });
  const observed = [];
  controller.subscribe((viewModel) => observed.push(viewModel.presentation));

  await controller.start();
  const presentation = controller.getViewModel().presentation;
  assert.deepEqual(
    presentation.activities.map(({ id, target }) => ({ id, target })),
    [
      { id: "presentation-tool", target: longPath },
      { id: "presentation-skill", target: "interview-coach/rubric.md" },
      { id: "presentation-list", target: "Vault" },
      { id: "presentation-probe", target: "active model" },
    ],
  );
  assert.match(presentation.activities[0].details, /not_found.*no longer exists/s);
  assert.deepEqual(
    presentation.transcript.map((item) => item.kind),
    [
      "run_status",
      "message",
      "activity_error",
      "activity_summary",
      "message",
      "run_status",
    ],
  );
  assert.deepEqual(
    presentation.transcript
      .filter((item) => item.kind === "message")
      .map(({ message, presentation: messagePresentation }) => ({
        role: message.role,
        ...messagePresentation,
      })),
    [
      { role: "user", copyable: false, format: "plain_text", layout: "compact_user" },
      { role: "assistant", copyable: false, format: "markdown", layout: "full_width_agent" },
    ],
  );
  const activitySummary = presentation.transcript.find(
    (item) => item.kind === "activity_summary",
  );
  assert.equal(activitySummary.agentRunId, "presentation-run");
  assert.equal(activitySummary.activities.length, 3);
  assert.match(activitySummary.label, /3/);
  assert.equal(
    presentation.transcript.find((item) => item.kind === "activity_error")?.activity.id,
    "presentation-tool",
  );
  assert.equal(
    presentation.transcript.find(
      (item) => item.kind === "run_status" && item.agentRunId === "failed-presentation-run",
    )?.message,
    "The earlier Provider request failed.",
  );
  assert.deepEqual(presentation.composer.contextChips, [
    { kind: "scope", label: "Vault context" },
  ]);
  assert.deepEqual(presentation.composer.primaryAction, {
    agentRunId: "presentation-run",
    kind: "resume",
    label: "Resume",
  });
  assert.equal(presentation.composer.permissionMode, "read_only");
  assert.deepEqual(presentation.settings.model, { id: "model-fast", label: "Fast Model" });
  assert.deepEqual(presentation.settings.fastMode, { enabled: true });
  assert.equal(presentation.settings.providerStatus, "connected");
  assert.equal(presentation.settings.runtimeStatus, "connected");
  assert.equal(presentation.settings.permissionMode, "read_only");
  assert.match(presentation.settings.advanced.gitRetention, /30 days.*100/i);
  assert.match(presentation.settings.advanced.diagnostics, /connected/i);

  fastModeEnabled = false;
  vaultPermissionMode = "ask_every_time";
  controller.refreshPresentation();
  assert.equal(observed.at(-1).settings.fastMode.enabled, false);
  assert.equal(observed.at(-1).composer.permissionMode, "ask_every_time");
});

test("one image draft survives staging failure and sends only an opaque Attachment ID", async () => {
  let rejectUpload = true;
  let runRequest;
  const runtime = {
    cancelAgentRun() {},
    async createConversation(conversation) { return conversation; },
    async deleteConversation() {},
    async listConversations() {
      return [{ id: "image-conversation", title: "Image", modelId: "model-a" }];
    },
    async listModels() { return [{ id: "model-a", label: "Model A" }]; },
    onUnavailable() { return () => {}; },
    async openConversation() {
      return {
        conversation: { id: "image-conversation", title: "Image", modelId: "model-a" },
        agentRuns: [], messages: [], toolCalls: [],
      };
    },
    async *resumeAgentRun() {},
    async *runAgent(request) {
      runRequest = request;
      yield { type: "agent_run.started", model: request.model };
      yield {
        type: "agent_run.completed",
        output: { role: "assistant", text: "Image understood." },
      };
    },
    async stageAttachment() {
      if (rejectUpload) throw new Error("Image signature is unsupported.");
      return {
        attachmentId: "opaque-attachment-id",
        fileName: "interview.png",
        mediaType: "image/png",
        size: 12,
      };
    },
    async start() {},
    async stop() {},
    async updateConversationModel(conversationId, modelId) {
      return { id: conversationId, title: "Image", modelId };
    },
  };
  const controller = new SidebarController(runtime);
  await controller.start();
  controller.setComposerDraft("Read this screenshot.");
  controller.attachImage({
    bytes: new Uint8Array([0x89, 0x50, 0x4e, 0x47]),
    fileName: "interview.png",
    mediaType: "image/png",
  });
  assert.equal(controller.getViewModel().presentation.composer.draftText, "Read this screenshot.");
  assert.deepEqual(controller.getViewModel().presentation.composer.attachment, {
    fileName: "interview.png",
    mediaType: "image/png",
    size: 4,
  });

  await assert.rejects(controller.sendMessage(), /unsupported/);
  assert.equal(controller.getViewModel().presentation.composer.draftText, "Read this screenshot.");
  assert.equal(controller.getViewModel().presentation.composer.attachment.fileName, "interview.png");

  rejectUpload = false;
  await controller.sendMessage();
  assert.deepEqual(runRequest.attachments, [{ attachmentId: "opaque-attachment-id", order: 0 }]);
  assert.equal("bytes" in runRequest, false);
  assert.equal(controller.getViewModel().presentation.composer.draftText, "");
  assert.equal(controller.getViewModel().presentation.composer.attachment, undefined);
});

test("a staged image is discarded and its draft survives when the Run never starts", async () => {
  const discarded = [];
  const runtime = {
    cancelAgentRun() {},
    async createConversation(conversation) { return conversation; },
    async deleteConversation() {},
    async discardAttachment(request) { discarded.push(request); },
    async listConversations() {
      return [{ id: "unstarted-image-conversation", title: "Image", modelId: "model-a" }];
    },
    async listModels() { return [{ id: "model-a", label: "Model A" }]; },
    onUnavailable() { return () => {}; },
    async openConversation() {
      return {
        conversation: { id: "unstarted-image-conversation", title: "Image", modelId: "model-a" },
        agentRuns: [], messages: [], toolCalls: [],
      };
    },
    async *resumeAgentRun() {},
    async *runAgent() { throw new Error("Runtime event connection is unavailable."); },
    async stageAttachment() {
      return {
        attachmentId: "unstarted-attachment",
        contentHash: "sha256:test",
        fileName: "interview.png",
        mediaType: "image/png",
        size: 4,
      };
    },
    async start() {},
    async stop() {},
    async updateConversationModel(conversationId, modelId) {
      return { id: conversationId, title: "Image", modelId };
    },
  };
  const controller = new SidebarController(runtime);
  await controller.start();
  controller.setComposerDraft("Keep this draft.");
  controller.attachImage({
    bytes: new Uint8Array([0x89, 0x50, 0x4e, 0x47]),
    fileName: "interview.png",
    mediaType: "image/png",
  });

  await controller.sendMessage();

  assert.equal(discarded.length, 1);
  assert.equal(discarded[0].attachmentId, "unstarted-attachment");
  assert.equal(controller.getViewModel().presentation.composer.draftText, "Keep this draft.");
  assert.equal(controller.getViewModel().presentation.composer.attachment.fileName, "interview.png");
  assert.deepEqual(controller.getViewModel().conversation.agentRuns, []);
  assert.deepEqual(controller.getViewModel().conversation.messages, []);
  assert.equal(controller.getViewModel().presentation.composer.primaryAction.kind, "send");
});

test("a started image Run restores the untouched submitted draft after vision failure", async () => {
  const runtime = {
    cancelAgentRun() {},
    async createConversation(conversation) { return conversation; },
    async deleteConversation() {},
    async discardAttachment() {},
    async listConversations() {
      return [{ id: "vision-failure-conversation", title: "Image", modelId: "text-model" }];
    },
    async listModels() { return [{ id: "text-model", label: "Text Model" }]; },
    onUnavailable() { return () => {}; },
    async openConversation() {
      return {
        conversation: {
          id: "vision-failure-conversation",
          title: "Image",
          modelId: "text-model",
        },
        agentRuns: [], messages: [], toolCalls: [],
      };
    },
    async *resumeAgentRun() {},
    async *runAgent(request) {
      yield { type: "agent_run.started", model: request.model };
      yield {
        type: "agent_run.failed",
        error: {
          code: "unsupported_capability",
          message: "The selected model does not support image input.",
        },
      };
    },
    async stageAttachment() {
      return {
        attachmentId: "started-vision-attachment",
        contentHash: "sha256:test",
        fileName: "interview.png",
        mediaType: "image/png",
        size: 4,
      };
    },
    async start() {},
    async stop() {},
    async updateConversationModel(conversationId, modelId) {
      return { id: conversationId, title: "Image", modelId };
    },
  };
  const controller = new SidebarController(runtime);
  await controller.start();
  controller.setComposerDraft("Read this interview screenshot.");
  controller.attachImage({
    bytes: new Uint8Array([0x89, 0x50, 0x4e, 0x47]),
    fileName: "interview.png",
    mediaType: "image/png",
  });

  await controller.sendMessage();

  assert.equal(
    controller.getViewModel().presentation.composer.draftText,
    "Read this interview screenshot.",
  );
  assert.equal(
    controller.getViewModel().presentation.composer.attachment.fileName,
    "interview.png",
  );
  assert.equal(controller.getViewModel().conversation.agentRuns[0].status, "failed");
  assert.match(controller.getViewModel().conversation.error.message, /does not support image/i);
});

test("composer edits made during image staging survive the started Run", async () => {
  let finishStaging;
  let stagingStarted;
  const staging = new Promise((resolve) => { stagingStarted = resolve; });
  const runtime = {
    cancelAgentRun() {},
    async createConversation(conversation) { return conversation; },
    async deleteConversation() {},
    async discardAttachment() {},
    async listConversations() {
      return [{ id: "edited-draft-conversation", title: "Image", modelId: "text-model" }];
    },
    async listModels() { return [{ id: "text-model", label: "Text Model" }]; },
    onUnavailable() { return () => {}; },
    async openConversation() {
      return {
        conversation: {
          id: "edited-draft-conversation", title: "Image", modelId: "text-model",
        },
        agentRuns: [], messages: [], toolCalls: [],
      };
    },
    async *resumeAgentRun() {},
    async *runAgent(request) {
      yield { type: "agent_run.started", model: request.model };
      yield {
        type: "agent_run.failed",
        error: { code: "unsupported_capability", message: "Vision is unavailable." },
      };
    },
    async stageAttachment() {
      stagingStarted();
      return new Promise((resolve) => { finishStaging = resolve; });
    },
    async start() {},
    async stop() {},
    async updateConversationModel(conversationId, modelId) {
      return { id: conversationId, title: "Image", modelId };
    },
  };
  const controller = new SidebarController(runtime);
  await controller.start();
  controller.setComposerDraft("Submitted draft A");
  controller.attachImage({
    bytes: new Uint8Array([0x89, 0x50, 0x4e, 0x47]),
    fileName: "interview.png",
    mediaType: "image/png",
  });

  const sending = controller.sendMessage();
  await staging;
  controller.setComposerDraft("New draft B");
  finishStaging({
    attachmentId: "edited-draft-attachment",
    contentHash: "sha256:test",
    fileName: "interview.png",
    mediaType: "image/png",
    size: 4,
  });
  await sending;

  assert.equal(controller.getViewModel().presentation.composer.draftText, "New draft B");
  assert.equal(controller.getViewModel().presentation.composer.attachment.fileName, "interview.png");
});

test("the Sidebar serializes sends while an image upload is pending", async () => {
  let failUpload;
  let uploadStarted;
  const started = new Promise((resolve) => { uploadStarted = resolve; });
  const runtime = {
    cancelAgentRun() {},
    async createConversation(conversation) { return conversation; },
    async deleteConversation() {},
    async discardAttachment() {},
    async listConversations() {
      return [{ id: "upload-lock-conversation", title: "Image", modelId: "model-a" }];
    },
    async listModels() { return [{ id: "model-a", label: "Model A" }]; },
    onUnavailable() { return () => {}; },
    async openConversation() {
      return {
        conversation: { id: "upload-lock-conversation", title: "Image", modelId: "model-a" },
        agentRuns: [], messages: [], toolCalls: [],
      };
    },
    async *resumeAgentRun() {},
    async *runAgent() {},
    async stageAttachment() {
      uploadStarted();
      return new Promise((_resolve, reject) => { failUpload = reject; });
    },
    async start() {},
    async stop() {},
    async updateConversationModel(conversationId, modelId) {
      return { id: conversationId, title: "Image", modelId };
    },
  };
  const controller = new SidebarController(runtime);
  await controller.start();
  controller.setComposerDraft("Only once.");
  controller.attachImage({
    bytes: new Uint8Array([0x89, 0x50, 0x4e, 0x47]),
    fileName: "interview.png",
    mediaType: "image/png",
  });

  const firstSend = controller.sendMessage();
  await started;
  assert.equal(controller.getViewModel().presentation.composer.isSending, true);
  await assert.rejects(controller.sendMessage(), /Wait for the current Agent Run/);
  failUpload(new Error("Upload stopped for test."));
  await assert.rejects(firstSend, /Upload stopped/);
  assert.equal(controller.getViewModel().presentation.composer.isSending, false);
  assert.equal(controller.getViewModel().presentation.composer.draftText, "Only once.");
});

test("the Sidebar previews, removes, reorders, and submits an ordered image set", async () => {
  const stagedNames = [];
  let runRequest;
  const runtime = {
    cancelAgentRun() {},
    async createConversation(conversation) { return conversation; },
    async deleteConversation() {},
    async discardAttachment() {},
    async listConversations() {
      return [{ id: "ordered-images-conversation", title: "Images", modelId: "model-a" }];
    },
    async listModels() { return [{ id: "model-a", label: "Model A" }]; },
    onUnavailable() { return () => {}; },
    async openConversation() {
      return {
        conversation: { id: "ordered-images-conversation", title: "Images", modelId: "model-a" },
        agentRuns: [], messages: [], toolCalls: [],
      };
    },
    async *resumeAgentRun() {},
    async *runAgent(request) {
      runRequest = request;
      yield { type: "agent_run.started", model: request.model };
      yield { type: "agent_run.completed", output: { role: "assistant", text: "Done" } };
    },
    async stageAttachment({ fileName }) {
      stagedNames.push(fileName);
      return {
        attachmentId: `attachment-${fileName}`,
        contentHash: `sha256:${"a".repeat(64)}`,
        fileName,
        mediaType: "image/png",
        size: 8,
      };
    },
    async start() {},
    async stop() {},
    async updateConversationModel(conversationId, modelId) {
      return { id: conversationId, title: "Images", modelId };
    },
  };
  const controller = new SidebarController(runtime);
  await controller.start();
  for (const fileName of ["first.png", "second.png", "third.png"]) {
    controller.attachImage({ bytes: new Uint8Array(8), fileName, mediaType: "image/png" });
  }
  assert.deepEqual(
    controller.getViewModel().presentation.composer.attachments.map(({ fileName }) => fileName),
    ["first.png", "second.png", "third.png"],
  );
  controller.moveDraftImage(2, -1);
  controller.removeDraftImage(0);
  assert.deepEqual(
    controller.getViewModel().presentation.composer.attachments.map(({ fileName }) => fileName),
    ["third.png", "second.png"],
  );

  await controller.sendMessage("Treat these screenshots as one Interview Experience.");

  assert.deepEqual(stagedNames, ["third.png", "second.png"]);
  assert.deepEqual(runRequest.attachments, [
    { attachmentId: "attachment-third.png", order: 0 },
    { attachmentId: "attachment-second.png", order: 1 },
  ]);
  assert.deepEqual(controller.getViewModel().presentation.composer.attachments, []);
});

test("a partial multi-image staging failure discards staged bytes and preserves the ordered draft", async () => {
  const discarded = [];
  const stagedNames = [];
  const runtime = {
    cancelAgentRun() {},
    async createConversation(conversation) { return conversation; },
    async deleteConversation() {},
    async discardAttachment(request) { discarded.push(request); },
    async listConversations() {
      return [{ id: "partial-staging-conversation", title: "Images", modelId: "model-a" }];
    },
    async listModels() { return [{ id: "model-a", label: "Model A" }]; },
    onUnavailable() { return () => {}; },
    async openConversation() {
      return {
        conversation: { id: "partial-staging-conversation", title: "Images", modelId: "model-a" },
        agentRuns: [], messages: [], toolCalls: [],
      };
    },
    async *resumeAgentRun() { },
    async *runAgent() { throw new Error("The Run must not start after staging failure."); },
    async stageAttachment({ fileName }) {
      stagedNames.push(fileName);
      if (fileName === "second.png") throw new Error("Animated GIF images are not supported.");
      return {
        attachmentId: `attachment-${fileName}`,
        contentHash: `sha256:${"a".repeat(64)}`,
        fileName, mediaType: "image/png", size: 8,
      };
    },
    async start() {}, async stop() {},
    async updateConversationModel(conversationId, modelId) {
      return { id: conversationId, title: "Images", modelId };
    },
  };
  const controller = new SidebarController(runtime);
  await controller.start();
  controller.setComposerDraft("Keep all three screenshots.");
  for (const fileName of ["first.png", "second.png", "third.png"]) {
    controller.attachImage({ bytes: new Uint8Array(8), fileName, mediaType: "image/png" });
  }

  await assert.rejects(controller.sendMessage(), /Animated GIF/);

  assert.deepEqual(stagedNames, ["first.png", "second.png"]);
  assert.deepEqual(discarded.map(({ attachmentId }) => attachmentId), ["attachment-first.png"]);
  assert.deepEqual(
    controller.getViewModel().presentation.composer.attachments.map(({ fileName }) => fileName),
    ["first.png", "second.png", "third.png"],
  );
  assert.equal(controller.getViewModel().presentation.composer.draftText, "Keep all three screenshots.");
  assert.deepEqual(controller.getViewModel().conversation.agentRuns, []);
});

test("the Sidebar rejects more than 20 images or more than 50 MiB without losing the draft", async () => {
  const runtime = {
    cancelAgentRun() {}, async createConversation(value) { return value; },
    async deleteConversation() {}, async discardAttachment() {},
    async listConversations() { return []; }, async listModels() { return []; },
    onUnavailable() { return () => {}; }, async openConversation() {},
    async *resumeAgentRun() {}, async *runAgent() {}, async stageAttachment() {},
    async start() {}, async stop() {},
  };
  const controller = new SidebarController(runtime);
  controller.setComposerDraft("Keep this submission draft.");
  for (let index = 0; index < 20; index += 1) {
    controller.attachImage({
      bytes: new Uint8Array(1), fileName: `${index}.png`, mediaType: "image/png",
    });
  }
  assert.throws(
    () => controller.attachImage({
      bytes: new Uint8Array(1), fileName: "overflow.png", mediaType: "image/png",
    }),
    /20 images/i,
  );
  for (let index = 0; index < 20; index += 1) controller.removeDraftImage(0);
  const tenMiB = new Uint8Array(10 * 1024 * 1024);
  for (let index = 0; index < 5; index += 1) {
    controller.attachImage({ bytes: tenMiB, fileName: `large-${index}.png`, mediaType: "image/png" });
  }
  assert.throws(
    () => controller.attachImage({
      bytes: new Uint8Array(1),
      fileName: "total-overflow.png",
      mediaType: "image/png",
    }),
    /50 MiB/i,
  );
  assert.equal(controller.getViewModel().presentation.composer.draftText, "Keep this submission draft.");
  assert.equal(controller.getViewModel().presentation.composer.attachments.length, 5);
});

test("one attachment import is atomic and blocks Send until every image is decoded", async () => {
  const runtime = {
    cancelAgentRun() {}, async createConversation(value) { return value; },
    async deleteConversation() {}, async discardAttachment() {},
    async listConversations() {
      return [{ id: "atomic-import", title: "Images", modelId: "model-a" }];
    },
    async listModels() { return [{ id: "model-a", label: "Model A" }]; },
    onUnavailable() { return () => {}; },
    async openConversation() {
      return {
        conversation: { id: "atomic-import", title: "Images", modelId: "model-a" },
        agentRuns: [], messages: [], toolCalls: [],
      };
    },
    async *resumeAgentRun() {}, async *runAgent() {}, async stageAttachment() {},
    async start() {}, async stop() {},
    async updateConversationModel(conversationId, modelId) {
      return { id: conversationId, title: "Images", modelId };
    },
  };
  const controller = new SidebarController(runtime);
  await controller.start();
  const finishImport = controller.beginAttachmentImport();

  assert.equal(controller.getViewModel().presentation.composer.isPreparingAttachments, true);
  await assert.rejects(controller.sendMessage("Do not send a partial selection."), /images are ready/i);
  assert.throws(
    () => controller.attachImages([
      { bytes: new Uint8Array(8), fileName: "valid.png", mediaType: "image/png" },
      { bytes: new Uint8Array(8), fileName: "invalid.bmp", mediaType: "image/bmp" },
    ]),
    /PNG, JPEG, WEBP, or GIF/,
  );
  assert.deepEqual(controller.getViewModel().presentation.composer.attachments, []);

  controller.attachImages([
    { bytes: new Uint8Array([1, 2, 3]), fileName: "first.png", mediaType: "image/png" },
    { bytes: new Uint8Array([4, 5, 6]), fileName: "second.png", mediaType: "image/png" },
  ]);
  finishImport();

  const composer = controller.getViewModel().presentation.composer;
  assert.equal(composer.isPreparingAttachments, false);
  assert.deepEqual(composer.attachments.map(({ fileName }) => fileName), ["first.png", "second.png"]);
  assert.deepEqual([...composer.attachments[0].previewBytes], [1, 2, 3]);
});

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
        { id: "model-b", label: "Model B", supportsFastMode: true },
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
      assert.equal(request.fastMode, true);
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
  const controller = new SidebarController(runtime, undefined, {
    getFastModeEnabled: () => true,
    getVaultPermissionMode: () => "trusted_vault",
  });
  const observed = [];
  controller.subscribe((viewModel) => observed.push(structuredClone(viewModel)));

  await controller.start();
  assert.equal(controller.getViewModel().runtime.state, "connected");
  assert.deepEqual(controller.getViewModel().conversation.models, [
    { id: "model-a", label: "Model A" },
    { id: "model-b", label: "Model B", supportsFastMode: true },
  ]);
  assert.equal(controller.getViewModel().conversation.selectedModelId, "model-a");

  controller.selectModel("model-b");
  await controller.sendMessage("Tell me about yourself.");

  const final = controller.getViewModel();
  const finalRunId = final.conversation.agentRuns.at(-1).id;
  assert.equal(final.conversation.runState, "idle");
  assert.deepEqual(final.conversation.messages, [
    { agentRunId: finalRunId, role: "user", text: "Tell me about yourself." },
    { agentRunId: finalRunId, role: "assistant", text: "Strong answer" },
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

test("streaming preserves a new draft while Transcript following freezes and resumes", async () => {
  let releaseSecondDelta;
  let releaseCompletion;
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
      return [{ id: "model-a", label: "Model A" }];
    },
    async listConversations() {
      return [{ id: "conversation-a", title: "Conversation", modelId: "model-a" }];
    },
    async openConversation() {
      return {
        conversation: { id: "conversation-a", title: "Conversation", modelId: "model-a" },
        messages: [],
        agentRuns: [],
        toolCalls: [],
      };
    },
    async *runAgent() {
      yield { type: "agent_run.started", model: "model-a" };
      yield { type: "agent_run.delta", delta: "First" };
      await new Promise((resolve) => (releaseSecondDelta = resolve));
      yield { type: "agent_run.delta", delta: " second" };
      await new Promise((resolve) => (releaseCompletion = resolve));
      yield {
        type: "agent_run.completed",
        output: { role: "assistant", text: "First second" },
      };
    },
  };
  const controller = new SidebarController(runtime);
  await controller.start();

  const send = controller.sendMessage("Original prompt");
  while (!releaseSecondDelta) await new Promise((resolve) => setImmediate(resolve));
  assert.deepEqual(controller.getViewModel().presentation.transcriptScroll, {
    hasNewContent: false,
    mode: "following",
  });

  controller.setComposerDraft("Draft for the next turn");
  controller.setTranscriptNearBottom(false);
  releaseSecondDelta();
  while (!releaseCompletion) await new Promise((resolve) => setImmediate(resolve));

  const frozen = controller.getViewModel().presentation;
  assert.equal(frozen.composer.draftText, "Draft for the next turn");
  assert.deepEqual(frozen.transcriptScroll, {
    hasNewContent: true,
    mode: "frozen",
  });

  releaseCompletion();
  await send;
  assert.equal(controller.getViewModel().presentation.composer.draftText, "Draft for the next turn");
  assert.deepEqual(controller.getViewModel().presentation.transcriptScroll, {
    hasNewContent: true,
    mode: "frozen",
  });

  controller.resumeTranscriptFollowing();
  assert.deepEqual(controller.getViewModel().presentation.transcriptScroll, {
    hasNewContent: false,
    mode: "following",
  });
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
  assert.equal(controller.getViewModel().presentation.settings.providerStatus, "unavailable");

  runtime.listModels = async () => {
    throw new RuntimeRequestError("model_unavailable", "The selected model was retired.");
  };
  const modelErrorController = new SidebarController(runtime);
  await modelErrorController.start();
  assert.equal(
    modelErrorController.getViewModel().presentation.settings.providerStatus,
    "connected",
  );
});

test("the Sidebar resumes one restored Interrupted Run only after an explicit user action", async () => {
  let resumeRequest;
  const runtime = {
    cancelAgentRun() {},
    async updateConversationModel(conversationId, modelId) {
      return { id: conversationId, title: "Resume", modelId };
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
      return [{ id: "model-a", label: "Model A" }];
    },
    async listConversations() {
      return [{ id: "resume-conversation", title: "Resume", modelId: "model-a" }];
    },
    async openConversation() {
      return {
        conversation: { id: "resume-conversation", title: "Resume", modelId: "model-a" },
        messages: [
          { id: "user-one", agentRunId: "resume-run", role: "user", text: "continue", sequence: 1 },
          {
            id: "partial-assistant",
            agentRunId: "resume-run",
            role: "assistant",
            text: "partial before interruption",
            sequence: 2,
          },
        ],
        agentRuns: [{ id: "resume-run", modelId: "model-a", status: "interrupted" }],
        toolCalls: [],
      };
    },
    async *runAgent() {
      throw new Error("A restored Run must not start automatically.");
    },
    async *resumeAgentRun(request) {
      resumeRequest = request;
      yield { type: "agent_run.resumed", model: "model-a" };
      yield { type: "agent_run.delta", delta: "continued" };
      yield { type: "agent_run.completed", output: { role: "assistant", text: "continued" } };
    },
  };
  const controller = new SidebarController(runtime);
  await controller.start();
  assert.equal(resumeRequest, undefined);
  assert.equal(controller.getViewModel().conversation.agentRuns[0].status, "interrupted");

  await controller.resumeAgentRun("resume-run");
  assert.deepEqual(resumeRequest, {
    conversationId: "resume-conversation",
    agentRunId: "resume-run",
  });
  assert.equal(controller.getViewModel().conversation.agentRuns[0].status, "completed");
  assert.equal(controller.getViewModel().conversation.messages.at(-1).text, "continued");
  const resumedMessages = controller.getViewModel().presentation.transcript.filter(
    (item) => item.kind === "message" && item.message.role === "assistant",
  );
  assert.deepEqual(resumedMessages.map(({ message }) => message.text), [
    "partial before interruption",
    "continued",
  ]);
  assert.equal(new Set(resumedMessages.map(({ key }) => key)).size, 2);
  assert.equal(resumedMessages[0].key, "persisted:partial-assistant");
});

test("a proposal lost before plugin persistence fails only on explicit Resume", async () => {
  const recoveredResult = {
    ok: false,
    error: {
      code: "plugin_disconnected",
      message: "The proposal was not durably prepared.",
    },
  };
  const resumeRequests = [];
  const runtime = {
    cancelAgentRun() {},
    async createConversation(conversation) { return conversation; },
    async deleteConversation() {},
    async listConversations() {
      return [{ id: "lost-conversation", title: "Lost", modelId: "model-a" }];
    },
    async listModels() { return [{ id: "model-a", label: "Model A" }]; },
    onUnavailable() { return () => {}; },
    async openConversation() {
      return {
        conversation: { id: "lost-conversation", title: "Lost", modelId: "model-a" },
        messages: [{ id: "user", role: "user", text: "Change it", sequence: 1 }],
        agentRuns: [{ id: "lost-run", status: "interrupted", modelId: "model-a" }],
        toolCalls: [{
          id: "lost-tool",
          agentRunId: "lost-run",
          name: "vault_propose_changes",
          arguments: {
            batchId: "lost-batch",
            idempotencyKey: "lost-key",
            task: "Lost proposal",
            actions: [{ path: "notes/lost.md" }],
          },
          status: "requested",
          vaultChangeState: "pending",
        }],
      };
    },
    async *resumeAgentRun(request) {
      resumeRequests.push(request);
      yield { type: "agent_run.resumed", model: "model-a" };
      yield {
        type: "tool_call.completed",
        toolCallId: "lost-tool",
        tool: { kind: "local", name: "vault_propose_changes" },
        status: "failed",
        error: recoveredResult.error,
      };
      yield { type: "agent_run.completed", output: { role: "assistant", text: "Replanned" } };
    },
    async *runAgent() {},
    async start() {},
    async stop() {},
    async updateConversationModel(conversationId, modelId) {
      return { id: conversationId, title: "Lost", modelId };
    },
  };
  const controller = new SidebarController(runtime, {
    async acknowledge() {},
    cancel() {},
    async decide() { throw new Error("not used"); },
    async rehydrate() {
      return { result: recoveredResult };
    },
    async undo() { throw new Error("not used"); },
  });

  await controller.start();
  assert.equal(resumeRequests.length, 0);
  assert.equal(controller.getViewModel().conversation.agentRuns[0].status, "interrupted");
  assert.equal(controller.getViewModel().conversation.toolCalls[0].status, "failed");

  await controller.resumeAgentRun("lost-run");
  assert.equal(resumeRequests.length, 1);
  assert.deepEqual(resumeRequests[0].recoveredToolResult, {
    eventId: resumeRequests[0].recoveredToolResult.eventId,
    toolCallId: "lost-tool",
    result: recoveredResult,
  });
  assert.equal(controller.getViewModel().conversation.agentRuns[0].status, "completed");
});

test("a failed Apply on a restored confirmation still continues without a second Resume", async () => {
  const proposal = {
    batchId: "restored-batch",
    idempotencyKey: "restored-batch-key",
    task: "Restore pending confirmation",
    actions: [{
      actionId: "restored-action",
      idempotencyKey: "restored-action-key",
      operation: "append",
      path: "notes/restored.md",
      expectedVersion: "mtime:1:size:4",
    }],
  };
  const recoveredResult = {
    ok: false,
    error: { code: "stale_evidence", message: "The source changed while offline." },
  };
  const resumeRequests = [];
  const runtime = {
    cancelAgentRun() {},
    async createConversation(conversation) { return conversation; },
    async deleteConversation() {},
    async listConversations() {
      return [{ id: "restored-conversation", title: "Restored", modelId: "model-a" }];
    },
    async listModels() { return [{ id: "model-a", label: "Model A" }]; },
    onUnavailable() { return () => {}; },
    async openConversation() {
      return {
        conversation: { id: "restored-conversation", title: "Restored", modelId: "model-a" },
        messages: [{ id: "user", role: "user", text: "Change it", sequence: 1 }],
        agentRuns: [{ id: "restored-run", status: "interrupted", modelId: "model-a" }],
        toolCalls: [{
          id: "restored-tool",
          agentRunId: "restored-run",
          name: "vault_propose_changes",
          arguments: proposal,
          status: "requested",
          vaultChangeState: "pending",
        }],
      };
    },
    async *resumeAgentRun(request) {
      resumeRequests.push(request);
      yield { type: "agent_run.resumed", model: "model-a" };
      yield {
        type: "tool_call.completed",
        toolCallId: "restored-tool",
        tool: { kind: "local", name: "vault_propose_changes" },
        status: "failed",
        error: recoveredResult.error,
      };
      yield { type: "agent_run.completed", output: { role: "assistant", text: "Replanned" } };
    },
    async *runAgent() {},
    async start() {},
    async stop() {},
    async updateConversationModel(conversationId, modelId) {
      return { id: conversationId, title: "Restored", modelId };
    },
  };
  const rehydrated = [];
  const changes = {
    cancel() {},
    async rehydrate(toolCallId) { rehydrated.push(toolCallId); return { proposal }; },
    async decide() { return recoveredResult; },
    async undo() { throw new Error("not used"); },
  };
  const controller = new SidebarController(runtime, changes);
  await controller.start();
  assert.deepEqual(rehydrated, ["restored-tool"]);
  await controller.decideVaultChange("restored-tool", "apply");
  assert.equal(resumeRequests.length, 1);
  assert.equal(resumeRequests[0].agentRunId, "restored-run");
  assert.deepEqual(resumeRequests[0].recoveredToolResult, {
    eventId: resumeRequests[0].recoveredToolResult.eventId,
    toolCallId: "restored-tool",
    result: recoveredResult,
  });
  assert.equal(controller.getViewModel().conversation.agentRuns[0].status, "completed");
  assert.equal(controller.getViewModel().conversation.messages.at(-1).text, "Replanned");

  const restoredAfterHandoff = new SidebarController(runtime, {
    ...changes,
    async rehydrate(toolCallId) {
      rehydrated.push(toolCallId);
      return { proposal, result: recoveredResult };
    },
  });
  await restoredAfterHandoff.start();
  assert.equal(resumeRequests.length, 1);
  assert.equal(restoredAfterHandoff.getViewModel().conversation.agentRuns[0].status, "interrupted");
  await restoredAfterHandoff.resumeAgentRun("restored-run");
  assert.equal(resumeRequests.length, 2);
  assert.equal(restoredAfterHandoff.getViewModel().conversation.agentRuns[0].status, "completed");
});

test("a recovered decision survives disconnect after resumed until tool completion", async () => {
  const proposal = {
    batchId: "retry-handoff-batch",
    idempotencyKey: "retry-handoff-key",
    task: "Retry durable handoff",
    actions: [{
      actionId: "retry-handoff-action",
      idempotencyKey: "retry-handoff-action-key",
      operation: "create",
      path: "notes/retry.md",
      expectedVersion: "missing",
    }],
  };
  const recoveredResult = {
    ok: true,
    value: {
      type: "vault_propose_changes",
      batchId: proposal.batchId,
      decision: "rejected",
      targets: [{ path: "notes/retry.md", beforeHash: "missing", afterHash: "missing" }],
    },
  };
  const resumeRequests = [];
  const runtime = {
    cancelAgentRun() {},
    async createConversation(conversation) { return conversation; },
    async deleteConversation() {},
    async listConversations() {
      return [{ id: "retry-handoff-conversation", title: "Retry", modelId: "model-a" }];
    },
    async listModels() { return [{ id: "model-a", label: "Model A" }]; },
    onUnavailable() { return () => {}; },
    async openConversation() {
      return {
        conversation: { id: "retry-handoff-conversation", title: "Retry", modelId: "model-a" },
        messages: [{ id: "user", role: "user", text: "Change it", sequence: 1 }],
        agentRuns: [{ id: "retry-handoff-run", status: "interrupted", modelId: "model-a" }],
        toolCalls: [{
          id: "retry-handoff-tool",
          agentRunId: "retry-handoff-run",
          name: "vault_propose_changes",
          arguments: proposal,
          status: "requested",
          vaultChangeState: "pending",
        }],
      };
    },
    async *resumeAgentRun(request) {
      resumeRequests.push(structuredClone(request));
      yield { type: "agent_run.resumed", model: "model-a" };
      if (resumeRequests.length === 1) {
        throw new Error("Injected disconnect after resumed");
      }
      yield {
        type: "tool_call.completed",
        toolCallId: "retry-handoff-tool",
        tool: { kind: "local", name: "vault_propose_changes" },
        status: "completed",
      };
      yield { type: "agent_run.completed", output: { role: "assistant", text: "Continued" } };
    },
    async *runAgent() {},
    async start() {},
    async stop() {},
    async updateConversationModel(conversationId, modelId) {
      return { id: conversationId, title: "Retry", modelId };
    },
  };
  const controller = new SidebarController(runtime, {
    async acknowledge() {},
    cancel() {},
    async decide() { return recoveredResult; },
    async rehydrate() {
      return { proposal, result: recoveredResult };
    },
    async undo() { throw new Error("not used"); },
  });

  await controller.start();
  assert.equal(resumeRequests.length, 0);
  await controller.resumeAgentRun("retry-handoff-run");
  assert.equal(resumeRequests.length, 1);
  assert.equal(controller.getViewModel().conversation.agentRuns[0].status, "interrupted");
  assert.equal(controller.getViewModel().conversation.toolCalls[0].status, "failed");

  await controller.resumeAgentRun("retry-handoff-run");
  assert.equal(resumeRequests.length, 2);
  assert.deepEqual(resumeRequests[1].recoveredToolResult, resumeRequests[0].recoveredToolResult);
  assert.equal(controller.getViewModel().conversation.agentRuns[0].status, "completed");
});

test("a live decision handoff survives disconnect after resumed until tool completion", async () => {
  const proposal = {
    batchId: "live-handoff-batch",
    idempotencyKey: "live-handoff-key",
    task: "Retry live decision handoff",
    actions: [{
      actionId: "live-handoff-action",
      idempotencyKey: "live-handoff-action-key",
      operation: "create",
      path: "notes/live-retry.md",
      expectedVersion: "missing",
    }],
  };
  const decisionResult = {
    ok: true,
    value: {
      type: "vault_propose_changes",
      batchId: proposal.batchId,
      decision: "rejected",
      targets: [{ path: "notes/live-retry.md", beforeHash: "missing", afterHash: "missing" }],
    },
  };
  const resumeRequests = [];
  const runtime = {
    cancelAgentRun() {},
    async createConversation(conversation) { return conversation; },
    async deleteConversation() {},
    async listConversations() {
      return [{ id: "live-handoff-conversation", title: "Live retry", modelId: "model-a" }];
    },
    async listModels() { return [{ id: "model-a", label: "Model A" }]; },
    onUnavailable() { return () => {}; },
    async openConversation() {
      return {
        conversation: { id: "live-handoff-conversation", title: "Live retry", modelId: "model-a" },
        messages: [{ id: "user", role: "user", text: "Change it", sequence: 1 }],
        agentRuns: [{ id: "live-handoff-run", status: "interrupted", modelId: "model-a" }],
        toolCalls: [{
          id: "live-handoff-tool",
          agentRunId: "live-handoff-run",
          name: "vault_propose_changes",
          arguments: proposal,
          status: "requested",
          vaultChangeState: "pending",
        }],
      };
    },
    async *resumeAgentRun(request) {
      resumeRequests.push(structuredClone(request));
      yield { type: "agent_run.resumed", model: "model-a" };
      if (resumeRequests.length === 1) throw new Error("Injected live handoff disconnect");
      yield {
        type: "tool_call.completed",
        toolCallId: "live-handoff-tool",
        tool: { kind: "local", name: "vault_propose_changes" },
        status: "completed",
      };
      yield { type: "agent_run.completed", output: { role: "assistant", text: "Continued" } };
    },
    async *runAgent() {},
    async start() {},
    async stop() {},
    async updateConversationModel(conversationId, modelId) {
      return { id: conversationId, title: "Live retry", modelId };
    },
  };
  const controller = new SidebarController(runtime, {
    async acknowledge() {},
    cancel() {},
    async decide() { return decisionResult; },
    async rehydrate() { return { proposal }; },
    async undo() { throw new Error("not used"); },
  });

  await controller.start();
  await controller.decideVaultChange("live-handoff-tool", "reject");
  assert.equal(resumeRequests.length, 1);
  assert.equal(controller.getViewModel().conversation.agentRuns[0].status, "interrupted");
  assert.equal(controller.getViewModel().conversation.toolCalls[0].status, "failed");

  await controller.resumeAgentRun("live-handoff-run");
  assert.equal(resumeRequests.length, 2);
  assert.deepEqual(resumeRequests[1].recoveredToolResult, resumeRequests[0].recoveredToolResult);
  assert.equal(controller.getViewModel().conversation.agentRuns[0].status, "completed");
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
    {
      id: "message-conversation-b",
      agentRunId: "run-conversation-b",
      role: "user",
      text: "history conversation-b",
    },
  ]);

  await controller.createConversation("Fresh Conversation");
  const createdId = controller.getViewModel().conversation.activeConversationId;
  assert.equal(controller.getViewModel().conversation.conversations[0].title, "Fresh Conversation");
  assert.deepEqual(controller.getViewModel().conversation.messages, []);

  await controller.deleteCurrentConversation();
  assert.notEqual(controller.getViewModel().conversation.activeConversationId, createdId);
  assert.equal(controller.getViewModel().conversation.conversations.length, 2);
});

test("deleting a Conversation discards its durable pending proposal before Runtime state", async () => {
  const order = [];
  const proposal = {
    batchId: "delete-pending-batch",
    idempotencyKey: "delete-pending-key",
    task: "Delete pending state",
    actions: [{
      actionId: "delete-pending-action",
      idempotencyKey: "delete-pending-action-key",
      operation: "create",
      path: "notes/delete.md",
      expectedVersion: "missing",
    }],
  };
  const runtime = {
    cancelAgentRun() {},
    async createConversation(conversation) { return conversation; },
    async deleteConversation() { order.push("runtime-delete"); },
    async listConversations() {
      return [{ id: "delete-conversation", title: "Delete", modelId: "model-a" }];
    },
    async listModels() { return [{ id: "model-a", label: "Model A" }]; },
    onUnavailable() { return () => {}; },
    async openConversation() {
      return {
        conversation: { id: "delete-conversation", title: "Delete", modelId: "model-a" },
        agentRuns: [{ id: "delete-run", status: "interrupted", modelId: "model-a" }],
        messages: [],
        toolCalls: [{
          id: "delete-tool",
          agentRunId: "delete-run",
          name: "vault_propose_changes",
          arguments: proposal,
          status: "requested",
          vaultChangeState: "pending",
        }],
      };
    },
    async *runAgent() {},
    async start() {},
    async stop() {},
    async updateConversationModel(conversationId, modelId) {
      return { id: conversationId, title: "Delete", modelId };
    },
  };
  const changes = {
    async acknowledge(toolCallId) { order.push(`ack:${toolCallId}`); },
    cancel(toolCallId) { order.push(`cancel:${toolCallId}`); },
    async decide() { throw new Error("not used"); },
    async rehydrate() { return { proposal }; },
    async undo() { throw new Error("not used"); },
  };
  const controller = new SidebarController(runtime, changes);
  await controller.start();
  order.length = 0;
  await controller.deleteCurrentConversation();
  assert.deepEqual(order.slice(0, 3), ["cancel:delete-tool", "ack:delete-tool", "runtime-delete"]);
});

test("stopping an active Sidebar run makes it visibly cancelled", async () => {
  const order = [];
  let releaseRun;
  const cancelled = new Promise((resolve) => (releaseRun = resolve));
  let cancelledRequest;
  const runtime = {
    cancelAgentRun(request) {
      cancelledRequest = request;
      order.push("runtime-cancel");
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
        tool: {
          kind: "local",
          name: "vault_propose_changes",
          arguments: {
            batchId: "cancelled-sidebar-batch",
            idempotencyKey: "cancelled-sidebar-key",
            task: "Cancel safely",
            actions: [{
              actionId: "cancelled-sidebar-action",
              idempotencyKey: "cancelled-sidebar-action-key",
              operation: "create",
              path: "notes/slow.md",
              expectedVersion: "missing",
              content: "slow\n",
            }],
          },
        },
      };
      await cancelled;
      yield { type: "agent_run.cancelled" };
    },
    async start() {},
    async stop() {},
  };
  const controller = new SidebarController(runtime, {
    cancel(toolCallId) { order.push(`proposal-cleanup:${toolCallId}`); },
    async decide() { throw new Error("not used"); },
    async undo() { throw new Error("not used"); },
  });
  await controller.start();
  const sending = controller.sendMessage("Cancel this run.");
  await new Promise((resolve) => setImmediate(resolve));
  assert.equal(controller.getViewModel().conversation.runState, "streaming");
  assert.equal(controller.getViewModel().conversation.agentRuns.at(-1).status, "running");
  controller.stopAgentRun();
  assert.deepEqual(order, ["runtime-cancel"]);
  await sending;

  assert.equal(cancelledRequest.conversationId, "conversation-stop");
  assert.equal(controller.getViewModel().conversation.agentRuns.at(-1).status, "cancelled");
  assert.equal(controller.getViewModel().conversation.toolCalls.at(-1).status, "failed");
  assert.deepEqual(order, ["runtime-cancel", "proposal-cleanup:cancelled-sidebar-tool"]);
  assert.deepEqual(controller.getViewModel().conversation.messages, [
    {
      agentRunId: controller.getViewModel().conversation.agentRuns.at(-1).id,
      role: "user",
      text: "Cancel this run.",
    },
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
    {
      agentRunId: controller.getViewModel().conversation.agentRuns.at(-1).id,
      role: "user",
      text: "Fail this run.",
    },
  ]);
  assert.equal(controller.getViewModel().presentation.settings.providerStatus, "unavailable");

  runtime.runAgent = async function* runInstructionFailure() {
    yield { type: "agent_run.started", model: "model-a" };
    yield {
      type: "agent_run.failed",
      error: { code: "instruction_error", message: "The local Agent Contract is invalid." },
    };
  };
  const instructionController = new SidebarController(runtime);
  await instructionController.start();
  await instructionController.sendMessage("Use the local contract.");
  assert.equal(
    instructionController.getViewModel().presentation.settings.providerStatus,
    "connected",
  );
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

test("a failed Vault Change tool event disables its card and restores the ordered image draft", async () => {
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
    async discardAttachment() {},
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
    async stageAttachment({ fileName, mediaType, bytes }) {
      return {
        attachmentId: `attachment-${fileName}`,
        contentHash: `sha256:${"a".repeat(64)}`,
        fileName,
        mediaType,
        size: bytes.byteLength,
      };
    },
    async start() {}, async stop() {},
    async updateConversationModel(conversationId, modelId) {
      return { id: conversationId, title: "Failed change", modelId };
    },
  };
  const controller = new SidebarController(runtime);
  await controller.start();
  controller.setComposerDraft("Keep this submission if the Vault rejects it.");
  controller.attachImages([
    { bytes: new Uint8Array([1, 2, 3]), fileName: "first.png", mediaType: "image/png" },
    { bytes: new Uint8Array([4, 5, 6]), fileName: "second.png", mediaType: "image/png" },
  ]);
  await controller.sendMessage("Try a stale change.");
  assert.equal(controller.getViewModel().conversation.vaultChanges[0].status, "failed");
  assert.equal(
    controller.getViewModel().presentation.composer.draftText,
    "Keep this submission if the Vault rejects it.",
  );
  assert.deepEqual(
    controller.getViewModel().presentation.composer.attachments.map(({ fileName }) => fileName),
    ["first.png", "second.png"],
  );
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
