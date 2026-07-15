export { RuntimeSupervisor } from "./runtime-supervisor";

import { randomUUID } from "node:crypto";
import type {
  AgentRunRecord,
  ConversationMessage,
  ConversationSummary,
  ModelDescriptor,
  PersistedRunAttachmentMetadata,
  StagedRunAttachment,
  ProviderErrorCode,
  ToolCallRecord,
  VaultChangeBatchProposal,
  VaultUndoResultPayload,
  VaultUndoConflict,
  LocalToolResultPayload,
  VaultToolErrorCode,
  WebCitation,
} from "@offeragent/protocol";
import { generateConversationTitle } from "@offeragent/protocol";
import { RuntimeRequestError, type RuntimeClient } from "./runtime-supervisor";
import type { VaultPermissionMode } from "./vault-change-coordinator";

export type RuntimeViewState = "connected" | "idle" | "starting" | "unavailable";

export interface SidebarViewModel {
  conversation: {
    activeConversationId?: string;
    agentRuns: AgentRunRecord[];
    conversations: ConversationSummary[];
    error?: { code: ProviderErrorCode | VaultToolErrorCode; message: string };
    messages: Array<{
      agentRunId: string;
      attachments?: PersistedRunAttachmentMetadata[];
      citations?: WebCitation[];
      id?: string;
      role: "assistant" | "user";
      text: string;
    }>;
    models: ModelDescriptor[];
    runState: "idle" | "streaming";
    selectedModelId?: string;
    toolCalls: ToolCallRecord[];
    vaultChanges: Array<{
      actions: Array<{ operation: string; path: string }>;
      batchId: string;
      conflicts?: VaultUndoConflict[];
      message?: string;
      status: "applied" | "applying" | "conflicted" | "expired" | "failed" | "pending" | "rejected" | "rejecting" | "undone";
      task: string;
      toolCallId: string;
    }>;
  };
  runtime: {
    message?: string;
    state: RuntimeViewState;
  };
  presentation: {
    activities: Array<{
      action: string;
      details: string;
      id: string;
      label: string;
      status: ToolCallRecord["status"];
      target?: string;
    }>;
    composer: {
      attachment?: { fileName: string; mediaType: string; size: number };
      attachments: Array<{
        fileName: string;
        mediaType: string;
        previewBytes: Uint8Array;
        size: number;
      }>;
      contextChips: Array<{ kind: "scope"; label: string }>;
      draftText: string;
      isPreparingAttachments: boolean;
      isSending: boolean;
      permissionMode: VaultPermissionMode;
      primaryAction: {
        agentRunId?: string;
        kind: "resume" | "send" | "stop";
        label: "Resume" | "Send" | "Stop";
      };
    };
    settings: {
      advanced: {
        diagnostics: string;
        gitRetention: string;
        hostedWebSearch: string;
      };
      fastMode?: { enabled: boolean };
      model?: { id: string; label: string };
      permissionMode: VaultPermissionMode;
      providerStatus: "connected" | "unavailable";
      runtimeStatus: RuntimeViewState;
    };
    transcriptScroll: {
      hasNewContent: boolean;
      mode: "following" | "frozen";
    };
    transcript: Array<
      | {
          kind: "activity_error";
          activity: SidebarViewModel["presentation"]["activities"][number];
        }
      | {
          activities: SidebarViewModel["presentation"]["activities"];
          agentRunId: string;
          kind: "activity_summary";
          label: string;
        }
      | {
          key: string;
          kind: "message";
          message: SidebarViewModel["conversation"]["messages"][number];
          presentation: {
            copyable: boolean;
            format: "markdown" | "plain_text";
            layout: "compact_user" | "full_width_agent";
          };
        }
      | {
          kind: "run_status";
          agentRunId: string;
          label: string;
          message?: string;
          revision?: { enabled: boolean; label: "放入输入框" };
          status: Exclude<AgentRunRecord["status"], "completed" | "running">;
        }
      | {
          kind: "vault_change";
          change: SidebarViewModel["conversation"]["vaultChanges"][number];
        }
    >;
  };
  title: "OfferAgent";
}

type Subscriber = (viewModel: SidebarViewModel) => void;

export interface SidebarEnvironment {
  getFastModeEnabled(): boolean;
  getVaultPermissionMode(): VaultPermissionMode;
}

const DEFAULT_ENVIRONMENT: SidebarEnvironment = {
  getFastModeEnabled: () => false,
  getVaultPermissionMode: () => "trusted_vault",
};

const TOOL_ACTIONS: Record<ToolCallRecord["name"], string> = {
  agent_contract_read: "Read contract",
  daily_note_context: "Resolve Daily Note",
  interview_catalog: "Search interview catalog",
  planning_memory_list: "Scan Planning Memory",
  planning_memory_read: "Recall Planning Memory",
  project_list: "List Project Evidence",
  project_read: "Read Project Evidence",
  project_search: "Search Project Evidence",
  research_browser: "Research in browser",
  hosted_web_search_probe: "Probe web search",
  skill_read: "Read skill",
  vault_list: "List",
  vault_propose_changes: "Change Vault",
  vault_read: "Read",
  vault_search: "Search",
  web_read: "Read web page",
};

function toolTarget(call: ToolCallRecord): string | undefined {
  if (!call.arguments || typeof call.arguments !== "object" || Array.isArray(call.arguments)) {
    return call.name === "agent_contract_read" ? "agent.md" : undefined;
  }
  const arguments_ = call.arguments as Record<string, unknown>;
  if (call.name === "skill_read" && typeof arguments_.skill === "string") {
    return typeof arguments_.resource === "string"
      ? `${arguments_.skill}/${arguments_.resource}`
      : arguments_.skill;
  }
  for (const key of ["path", "directory", "query", "url"]) {
    if (typeof arguments_[key] === "string" && arguments_[key]) return arguments_[key];
  }
  if (call.name === "agent_contract_read") return "agent.md";
  if (call.name === "vault_list") return "Vault";
  if (call.name === "hosted_web_search_probe") return "active model";
  return undefined;
}

function providerHealthFailure(code: ProviderErrorCode): boolean {
  return code === "auth_required" || code === "provider_error" || code === "transport_error";
}

export interface VaultChangeDecisionClient {
  acknowledge?(toolCallId: string): Promise<void>;
  cancel(toolCallId: string): void;
  decide(toolCallId: string, decision: "apply" | "reject"): Promise<LocalToolResultPayload>;
  rehydrate?(toolCallId: string): Promise<{
    proposal?: VaultChangeBatchProposal;
    result?: LocalToolResultPayload;
  }>;
  undo(batchId: string): Promise<VaultUndoResultPayload>;
}

export class SidebarController {
  readonly #environment: SidebarEnvironment;
  readonly #runtime: RuntimeClient;
  readonly #vaultChanges?: VaultChangeDecisionClient;
  readonly #subscribers = new Set<Subscriber>();
  #viewModel: Omit<SidebarViewModel, "presentation"> = {
    title: "OfferAgent",
    conversation: {
      agentRuns: [],
      conversations: [],
      messages: [],
      models: [],
      runState: "idle",
      toolCalls: [],
      vaultChanges: [],
    },
    runtime: { state: "idle" },
  };
  #activeRun?: { agentRunId: string; conversationId: string };
  #providerStatus: "connected" | "unavailable" = "unavailable";
  #draftText = "";
  #draftImages: Array<{ bytes: Uint8Array; fileName: string; mediaType: string }> = [];
  #draftRevision = 0;
  #attachmentImportPending = false;
  #sendPending = false;
  readonly #optimisticAttachmentPreviews = new Map<string, Map<number, Uint8Array>>();
  #transcriptFollowMode: "following" | "frozen" = "following";
  #transcriptHasNewContent = false;
  readonly #recoveredToolResults = new Map<string, {
    eventId: string;
    result: LocalToolResultPayload;
    toolCallId: string;
  }>();

  constructor(
    runtime: RuntimeClient,
    vaultChanges?: VaultChangeDecisionClient,
    environment: SidebarEnvironment = DEFAULT_ENVIRONMENT,
  ) {
    this.#environment = environment;
    this.#runtime = runtime;
    this.#vaultChanges = vaultChanges;
    this.#runtime.onUnavailable((message) => {
      this.#providerStatus = "unavailable";
      this.#update({ state: "unavailable", message });
    });
  }

  getViewModel(): SidebarViewModel {
    return { ...this.#viewModel, presentation: this.#presentation() };
  }

  subscribe(subscriber: Subscriber): () => void {
    this.#subscribers.add(subscriber);
    subscriber(this.getViewModel());
    return () => this.#subscribers.delete(subscriber);
  }

  refreshPresentation(): void {
    for (const subscriber of this.#subscribers) subscriber(this.getViewModel());
  }

  setComposerDraft(text: string): void {
    this.#draftText = text;
    this.#draftRevision += 1;
  }

  setTranscriptNearBottom(nearBottom: boolean): void {
    const mode = nearBottom ? "following" : "frozen";
    const hasNewContent = nearBottom ? false : this.#transcriptHasNewContent;
    if (
      mode === this.#transcriptFollowMode &&
      hasNewContent === this.#transcriptHasNewContent
    ) return;
    this.#transcriptFollowMode = mode;
    this.#transcriptHasNewContent = hasNewContent;
    this.refreshPresentation();
  }

  resumeTranscriptFollowing(): void {
    if (this.#transcriptFollowMode === "following" && !this.#transcriptHasNewContent) return;
    this.#transcriptFollowMode = "following";
    this.#transcriptHasNewContent = false;
    this.refreshPresentation();
  }

  #rejectImage(message: string): never {
    this.#updateConversation({
      ...this.#viewModel.conversation,
      error: { code: "provider_error", message },
    });
    throw new Error(message);
  }

  #validateImageBatch(images: Array<{ mediaType: string; size: number }>): void {
    if (this.#draftImages.length + images.length > 20) {
      this.#rejectImage("An Interview Submission accepts at most 20 images.");
    }
    let totalBytes = this.#draftImages.reduce(
      (total, draft) => total + draft.bytes.byteLength,
      0,
    );
    for (const image of images) {
      if (!new Set(["image/gif", "image/jpeg", "image/png", "image/webp"]).has(image.mediaType)) {
        this.#rejectImage("Choose a PNG, JPEG, WEBP, or GIF image.");
      }
      if (!Number.isSafeInteger(image.size) || image.size <= 0 || image.size > 10 * 1024 * 1024) {
        this.#rejectImage("The image must be non-empty and no larger than 10 MiB.");
      }
      totalBytes += image.size;
    }
    if (totalBytes > 50 * 1024 * 1024) {
      this.#rejectImage("An Interview Submission accepts at most 50 MiB of images in total.");
    }
  }

  #presentMessages(
    messages: ConversationMessage[],
  ): SidebarViewModel["conversation"]["messages"] {
    return messages.map(({ id, agentRunId, role, text, citations, attachments }) => ({
      id,
      agentRunId,
      role,
      text,
      ...(citations ? { citations } : {}),
      ...(attachments?.length ? { attachments: attachments.map((attachment) => ({ ...attachment })) } : {}),
    }));
  }

  async readMessageAttachment(
    message: { agentRunId: string; id?: string },
    order: number,
  ): Promise<Uint8Array> {
    if (message.id) {
      const conversationId = this.#viewModel.conversation.activeConversationId;
      if (!conversationId) throw new Error("Choose a Conversation before loading its image.");
      return this.#runtime.readConversationAttachment({ conversationId, messageId: message.id, order });
    }
    const bytes = this.#optimisticAttachmentPreviews.get(message.agentRunId)?.get(order);
    if (!bytes) throw new Error("The message image is not available yet.");
    return new Uint8Array(bytes);
  }

  releaseOptimisticAttachmentPreview(agentRunId: string, order: number): void {
    const previews = this.#optimisticAttachmentPreviews.get(agentRunId);
    if (!previews) return;
    previews.delete(order);
    if (previews.size === 0) this.#optimisticAttachmentPreviews.delete(agentRunId);
  }

  async #reconcilePersistedUserMessage(conversationId: string, agentRunId: string): Promise<void> {
    try {
      const snapshot = await this.#runtime.openConversation(conversationId);
      const persisted = snapshot.messages.find(
        (message) => message.agentRunId === agentRunId && message.role === "user",
      );
      if (!persisted) return;
      const local = this.#viewModel.conversation.messages.find(
        (message) => message.agentRunId === agentRunId && message.role === "user",
      );
      if (!local) return;
      local.id = persisted.id;
      if (persisted.attachments?.length) {
        local.attachments = persisted.attachments.map((attachment) => ({ ...attachment }));
      }
      this.refreshPresentation();
    } catch {
      // Keep the optimistic bytes until a later Conversation snapshot can supply the durable ID.
    }
  }

  beginAttachmentImport(
    files: Array<{ mediaType: string; size: number }> = [],
  ): () => void {
    if (
      this.#attachmentImportPending || this.#sendPending ||
      this.#viewModel.conversation.runState === "streaming"
    ) {
      throw new Error("Wait for the current attachment import or Agent Run to finish.");
    }
    if (files.length > 0) this.#validateImageBatch(files);
    this.#attachmentImportPending = true;
    this.refreshPresentation();
    let finished = false;
    return () => {
      if (finished) return;
      finished = true;
      this.#attachmentImportPending = false;
      this.refreshPresentation();
    };
  }

  attachImages(images: Array<{ bytes: Uint8Array; fileName: string; mediaType: string }>): void {
    if (images.length === 0) return;
    this.#validateImageBatch(images.map((image) => ({
      mediaType: image.mediaType,
      size: image.bytes.byteLength,
    })));
    this.#draftImages.push(...images.map((image) => ({
      bytes: new Uint8Array(image.bytes),
      fileName: image.fileName,
      mediaType: image.mediaType,
    })));
    this.#draftRevision += 1;
    this.refreshPresentation();
  }

  attachImage(image: { bytes: Uint8Array; fileName: string; mediaType: string }): void {
    this.attachImages([image]);
  }

  removeDraftImage(index = 0): void {
    if (index < 0 || index >= this.#draftImages.length) return;
    this.#draftImages.splice(index, 1);
    this.#draftRevision += 1;
    this.refreshPresentation();
  }

  moveDraftImage(index: number, offset: -1 | 1): void {
    const target = index + offset;
    this.reorderDraftImage(index, target);
  }

  reorderDraftImage(index: number, target: number): void {
    if (
      index < 0 || index >= this.#draftImages.length ||
      target < 0 || target >= this.#draftImages.length || index === target
    ) {
      return;
    }
    const [image] = this.#draftImages.splice(index, 1);
    this.#draftImages.splice(target, 0, image);
    this.#draftRevision += 1;
    this.refreshPresentation();
  }

  async start(): Promise<void> {
    this.#optimisticAttachmentPreviews.clear();
    this.#update({ state: "starting" });
    try {
      await this.#runtime.start();
      this.#update({ state: "connected" });
      try {
        const models = await this.#runtime.listModels();
        this.#providerStatus = "connected";
        let conversations = await this.#runtime.listConversations();
        if (!conversations.some(({ archived }) => !archived) && models[0]) {
          conversations = [
            await this.#runtime.createConversation({
              id: randomUUID(),
              title: "新对话",
              modelId: models[0].id,
              titleOrigin: "placeholder",
              archived: false,
              updatedAt: new Date().toISOString(),
            }),
            ...conversations,
          ];
        }
        const active = conversations.find(({ archived }) => !archived);
        const snapshot = active ? await this.#runtime.openConversation(active.id) : undefined;
        const messages = snapshot ? this.#presentMessages(snapshot.messages) : [];
        this.#recoveredToolResults.clear();
        if (snapshot) await this.#rehydratePendingVaultChanges(snapshot.toolCalls ?? []);
        const toolCalls = this.#toolCallsWithRecoveredFailures(snapshot?.toolCalls ?? []);
        this.#updateConversation({
          ...this.#viewModel.conversation,
          models,
          conversations,
          activeConversationId: snapshot?.conversation.id,
          selectedModelId: snapshot?.conversation.modelId ?? models[0]?.id,
          messages,
          agentRuns: snapshot?.agentRuns ?? [],
          toolCalls,
          vaultChanges: this.#changesFromToolCalls(toolCalls),
          error: undefined,
        });
      } catch (error) {
        if (error instanceof RuntimeRequestError) {
          this.#providerStatus = providerHealthFailure(error.code) ? "unavailable" : "connected";
        }
        const message = error instanceof Error ? error.message : String(error);
        this.#updateConversation({
          ...this.#viewModel.conversation,
          error: {
            code: error instanceof RuntimeRequestError ? error.code : "provider_error",
            message,
          },
        });
      }
    } catch (error) {
      this.#providerStatus = "unavailable";
      const message = error instanceof Error ? error.message : String(error);
      this.#update({ state: "unavailable", message });
      throw error;
    }
  }

  async createConversation(title = "新对话"): Promise<void> {
    if (this.#attachmentImportPending || this.#sendPending) {
      throw new Error("Wait for attachment import or sending to finish before creating a Conversation.");
    }
    const modelId = this.#viewModel.conversation.selectedModelId;
    if (!modelId) throw new Error("Choose an available model before creating a Conversation.");
    const conversation = await this.#runtime.createConversation({
      id: randomUUID(),
      title,
      modelId,
      titleOrigin: title === "新对话" ? "placeholder" : "manual",
      archived: false,
      updatedAt: new Date().toISOString(),
    });
    this.#optimisticAttachmentPreviews.clear();
    this.#recoveredToolResults.clear();
    this.#resetTranscriptFollowing();
    this.#updateConversation({
      ...this.#viewModel.conversation,
      activeConversationId: conversation.id,
      agentRuns: [],
      conversations: [conversation, ...this.#viewModel.conversation.conversations],
      messages: [],
      runState: "idle",
      selectedModelId: conversation.modelId,
      toolCalls: [],
      vaultChanges: [],
      error: undefined,
    });
  }

  async openConversation(conversationId: string): Promise<void> {
    if (this.#viewModel.conversation.runState === "streaming") {
      throw new Error("Stop the current Agent Run before switching Conversations.");
    }
    if (this.#attachmentImportPending || this.#sendPending) {
      throw new Error("Wait for attachment import or sending to finish before switching Conversations.");
    }
    const snapshot = await this.#runtime.openConversation(conversationId);
    this.#optimisticAttachmentPreviews.clear();
    const messages = this.#presentMessages(snapshot.messages);
    this.#recoveredToolResults.clear();
    this.#resetTranscriptFollowing();
    await this.#rehydratePendingVaultChanges(snapshot.toolCalls ?? []);
    const toolCalls = this.#toolCallsWithRecoveredFailures(snapshot.toolCalls ?? []);
    this.#updateConversation({
      ...this.#viewModel.conversation,
      activeConversationId: snapshot.conversation.id,
      conversations: this.#viewModel.conversation.conversations.map((conversation) =>
        conversation.id === snapshot.conversation.id ? snapshot.conversation : conversation
      ),
      agentRuns: snapshot.agentRuns,
      messages,
      runState: "idle",
      selectedModelId: snapshot.conversation.modelId,
      toolCalls,
      vaultChanges: this.#changesFromToolCalls(toolCalls),
      error: undefined,
    });
  }

  async deleteCurrentConversation(): Promise<void> {
    const conversationId = this.#viewModel.conversation.activeConversationId;
    if (!conversationId) return;
    if (this.#viewModel.conversation.runState === "streaming") {
      throw new Error("Stop the current Agent Run before deleting its Conversation.");
    }
    if (this.#attachmentImportPending || this.#sendPending) {
      throw new Error("Wait for attachment import or sending to finish before deleting this Conversation.");
    }
    const proposalCalls = this.#viewModel.conversation.toolCalls.filter(
      (call) => call.name === "vault_propose_changes",
    );
    for (const call of proposalCalls) {
      if (call.name !== "vault_propose_changes") continue;
      if (call.status === "requested") this.#vaultChanges?.cancel(call.id);
      await this.#vaultChanges?.acknowledge?.(call.id);
    }
    await this.#runtime.deleteConversation(conversationId);
    this.#optimisticAttachmentPreviews.clear();
    this.#recoveredToolResults.clear();
    const conversations = this.#viewModel.conversation.conversations.filter(
      (conversation) => conversation.id !== conversationId,
    );
    this.#updateConversation({
      ...this.#viewModel.conversation,
      activeConversationId: undefined,
      agentRuns: [],
      conversations,
      messages: [],
      runState: "idle",
      toolCalls: [],
      vaultChanges: [],
      error: undefined,
    });
    const next = conversations.find(({ archived }) => !archived);
    if (next) await this.openConversation(next.id);
    else await this.createConversation();
  }

  async renameConversation(conversationId: string, title: string): Promise<void> {
    const trimmed = title.trim();
    if (!trimmed) return;
    const updated = await this.#runtime.updateConversation(conversationId, {
      title: trimmed,
      titleOrigin: "manual",
    });
    this.#replaceConversationSummary(updated);
  }

  async setConversationArchived(conversationId: string, archived: boolean): Promise<void> {
    if (this.#viewModel.conversation.runState === "streaming") {
      throw new Error("Stop the current Agent Run before archiving Conversations.");
    }
    const updated = await this.#runtime.updateConversation(conversationId, { archived });
    this.#replaceConversationSummary(updated);
    if (archived && this.#viewModel.conversation.activeConversationId === conversationId) {
      const next = this.#viewModel.conversation.conversations.find(
        (conversation) => !conversation.archived && conversation.id !== conversationId,
      );
      if (next) await this.openConversation(next.id);
      else await this.createConversation();
    }
  }

  async selectModel(modelId: string): Promise<void> {
    if (!this.#viewModel.conversation.models.some((model) => model.id === modelId)) {
      throw new Error(`The selected model '${modelId}' is unavailable.`);
    }
    this.#updateConversation({
      ...this.#viewModel.conversation,
      selectedModelId: modelId,
      error: undefined,
    });
    const conversationId = this.#viewModel.conversation.activeConversationId;
    if (!conversationId) return;
    try {
      const updated = await this.#runtime.updateConversationModel(conversationId, modelId);
      this.#updateConversation({
        ...this.#viewModel.conversation,
        conversations: this.#viewModel.conversation.conversations.map((conversation) =>
          conversation.id === updated.id ? updated : conversation,
        ),
      });
    } catch (error) {
      this.#updateConversation({
        ...this.#viewModel.conversation,
        error: {
          code: "provider_error",
          message: error instanceof Error ? error.message : String(error),
        },
      });
    }
  }

  #replaceConversationSummary(updated: ConversationSummary): void {
    this.#updateConversation({
      ...this.#viewModel.conversation,
      conversations: this.#viewModel.conversation.conversations
        .map((conversation) => conversation.id === updated.id ? updated : conversation)
        .sort((left, right) => right.updatedAt.localeCompare(left.updatedAt)),
      selectedModelId: this.#viewModel.conversation.activeConversationId === updated.id
        ? updated.modelId
        : this.#viewModel.conversation.selectedModelId,
    });
  }

  async sendMessage(input = this.#draftText): Promise<void> {
    const text = input.trim();
    const selectedModelId = this.#viewModel.conversation.selectedModelId;
    const conversationId = this.#viewModel.conversation.activeConversationId;
    if (this.#attachmentImportPending) {
      throw new Error("Wait until all selected images are ready before sending.");
    }
    if (!text && this.#draftImages.length === 0) return;
    if (!selectedModelId) throw new Error("Choose an available model before sending a message.");
    if (!conversationId) throw new Error("Create a Conversation before sending a message.");
    if (this.#viewModel.conversation.runState === "streaming" || this.#sendPending) {
      throw new Error("Wait for the current Agent Run to finish.");
    }
    this.#sendPending = true;
    this.refreshPresentation();
    const activeSummary = this.#viewModel.conversation.conversations.find(
      ({ id }) => id === conversationId,
    );
    const automaticTitle = activeSummary?.titleOrigin === "placeholder"
      ? generateConversationTitle({
          text,
          imageFileName: this.#draftImages[0]?.fileName,
        })
      : undefined;
    const agentRunId = randomUUID();
    const submittedDraft = {
      images: this.#draftImages.map((image) => ({
        ...image,
        bytes: new Uint8Array(image.bytes),
      })),
      text: this.#draftText,
    };
    const submittedDraftRevision = this.#draftRevision;
    let clearedDraftRevision: number | undefined;
    let restoreAfterVaultFailure = false;
    const restoreSubmittedDraft = (): void => {
      if (clearedDraftRevision === undefined || this.#draftRevision !== clearedDraftRevision) return;
      this.#draftText = submittedDraft.text;
      this.#draftImages = submittedDraft.images;
      this.#draftRevision += 1;
    };
    let attachments: Array<{ attachmentId: string; order: number }> | undefined;
    const stagedAttachments: Array<StagedRunAttachment & { order: number }> = [];
    let runStarted = false;
    const discardUnstartedAttachments = async (): Promise<void> => {
      if (runStarted || !attachments) return;
      await Promise.all(attachments.map((attachment) => this.#runtime.discardAttachment({
        agentRunId,
        attachmentId: attachment.attachmentId,
        conversationId,
      }).catch(() => undefined)));
    };
    if (submittedDraft.images.length > 0) {
      attachments = [];
      try {
        for (const [order, image] of submittedDraft.images.entries()) {
          const staged = await this.#runtime.stageAttachment({
            agentRunId,
            bytes: image.bytes,
            conversationId,
            fileName: image.fileName,
            mediaType: image.mediaType,
          });
          stagedAttachments.push({ ...staged, order });
          attachments.push({ attachmentId: staged.attachmentId, order });
        }
      } catch (error) {
        await discardUnstartedAttachments();
        this.#sendPending = false;
        this.#updateConversation({
          ...this.#viewModel.conversation,
          error: {
            code: "provider_error",
            message: error instanceof Error ? error.message : String(error),
          },
        });
        throw error;
      }
    }
    const messages = [
      ...this.#viewModel.conversation.messages,
      {
        agentRunId,
        role: "user" as const,
        text,
        ...(stagedAttachments.length > 0
          ? {
              attachments: stagedAttachments.map(({ attachmentId: _attachmentId, ...metadata }) => ({
                ...metadata,
              })),
            }
          : {}),
      },
      { agentRunId, role: "assistant" as const, text: "" },
    ];
    if (submittedDraft.images.length > 0) {
      this.#optimisticAttachmentPreviews.set(agentRunId, new Map(
        submittedDraft.images.map((image, order) => [order, new Uint8Array(image.bytes)]),
      ));
    }
    const agentRuns = [
      ...this.#viewModel.conversation.agentRuns,
      { id: agentRunId, modelId: selectedModelId, status: "running" as const },
    ];
    this.#activeRun = { agentRunId, conversationId };
    this.#sendPending = false;
    const activityTimestamp = new Date().toISOString();
    this.#updateConversation({
      ...this.#viewModel.conversation,
      conversations: this.#viewModel.conversation.conversations
        .map((conversation) => conversation.id === conversationId
          ? { ...conversation, updatedAt: activityTimestamp }
          : conversation)
        .sort((left, right) => right.updatedAt.localeCompare(left.updatedAt)),
      agentRuns,
      messages,
      runState: "streaming",
      error: undefined,
    });

    try {
      for await (const event of this.#runtime.runAgent({
        conversationId,
        agentRunId,
        model: selectedModelId,
        ...(this.#viewModel.conversation.models.find(({ id }) => id === selectedModelId)
          ?.supportsFastMode && this.#environment.getFastModeEnabled()
          ? { fastMode: true }
          : {}),
        input: text,
        ...(attachments ? { attachments } : {}),
      })) {
        if (event.type === "agent_run.started") {
          runStarted = true;
          if (stagedAttachments.length > 0) {
            await this.#reconcilePersistedUserMessage(conversationId, agentRunId);
          }
          if (automaticTitle) {
            try {
              const updated = await this.#runtime.updateConversation(conversationId, {
                title: automaticTitle,
                titleOrigin: "automatic",
              });
              this.#replaceConversationSummary(updated);
            } catch (error) {
              this.#updateConversation({
                ...this.#viewModel.conversation,
                error: {
                  code: "provider_error",
                  message: error instanceof Error ? error.message : String(error),
                },
              });
            }
          }
          if (this.#draftRevision === submittedDraftRevision) {
            this.#draftText = "";
            this.#draftImages = [];
            this.#draftRevision += 1;
            clearedDraftRevision = this.#draftRevision;
          }
          this.#providerStatus = "connected";
          this.refreshPresentation();
        } else if (event.type === "agent_run.delta") {
          this.#appendAgentDelta(messages, agentRunId, event.delta);
        } else if (event.type === "agent_run.completed") {
          messages[messages.length - 1] = { agentRunId, ...event.output };
          if (restoreAfterVaultFailure) restoreSubmittedDraft();
          this.#setRunStatus(agentRunId, "completed");
        } else if (event.type === "tool_call.requested") {
          const requestedChange = this.#requestedChange(event.toolCallId, event.tool.name, event.tool.arguments);
          this.#updateConversation({
            ...this.#viewModel.conversation,
            toolCalls: [
              ...this.#viewModel.conversation.toolCalls,
              {
                id: event.toolCallId,
                agentRunId,
                name: event.tool.name,
                arguments: event.tool.arguments,
                status: "requested",
              },
            ],
            vaultChanges: requestedChange
              ? [...this.#viewModel.conversation.vaultChanges, requestedChange]
              : this.#viewModel.conversation.vaultChanges,
          });
        } else if (event.type === "tool_call.completed") {
          if (event.tool.name === "vault_propose_changes") {
            restoreAfterVaultFailure = event.status === "failed";
            await this.#vaultChanges?.acknowledge?.(event.toolCallId);
          }
          const vaultChanges =
            event.status === "failed"
              ? this.#viewModel.conversation.vaultChanges.map((change) =>
                  change.toolCallId === event.toolCallId
                    ? { ...change, status: "failed" as const, message: event.error?.message }
                    : change,
                )
              : this.#viewModel.conversation.vaultChanges.map((change) =>
                  change.toolCallId === event.toolCallId && change.status === "pending"
                    ? { ...change, status: "applied" as const }
                    : change,
                );
          this.#updateConversation({
            ...this.#viewModel.conversation,
            toolCalls: this.#viewModel.conversation.toolCalls.map((call) =>
              call.id === event.toolCallId
                ? {
                    ...call,
                    status: event.status,
                    ...(event.error ? { error: event.error } : {}),
                  }
                : call,
            ),
            vaultChanges,
            error:
              event.status === "failed" && event.error
                ? { code: event.error.code, message: event.error.message }
                : this.#viewModel.conversation.error,
          });
        } else if (event.type === "agent_run.failed") {
          if (providerHealthFailure(event.error.code)) this.#providerStatus = "unavailable";
          const vaultChanges = this.#cancelPendingVaultChanges(agentRunId);
          if (runStarted) {
            messages.pop();
            restoreSubmittedDraft();
          }
          else messages.splice(-2, 2);
          this.#updateConversation({
            ...this.#viewModel.conversation,
            messages: [...messages],
            runState: "idle",
            agentRuns: runStarted
              ? this.#runsWithStatus(agentRunId, "failed", event.error)
              : this.#viewModel.conversation.agentRuns.filter(({ id }) => id !== agentRunId),
            toolCalls: this.#terminalizedToolCalls(agentRunId),
            vaultChanges,
            error: event.error,
          });
        } else if (event.type === "agent_run.cancelled" || event.type === "agent_run.interrupted") {
          const cancelled = event.type === "agent_run.cancelled";
          const vaultChanges = cancelled
            ? this.#cancelPendingVaultChanges(agentRunId)
            : this.#viewModel.conversation.vaultChanges;
          if (cancelled && event.output?.text.length) {
            messages[messages.length - 1] = { agentRunId, ...event.output };
          } else {
            messages.pop();
          }
          this.#updateConversation({
            ...this.#viewModel.conversation,
            messages: [...messages],
            runState: "idle",
            toolCalls: cancelled
              ? this.#terminalizedToolCalls(agentRunId)
              : this.#interruptedToolCalls(agentRunId),
            vaultChanges,
            agentRuns: this.#runsWithStatus(
              agentRunId,
              cancelled ? "cancelled" : "interrupted",
            ),
          });
        }
      }
      await discardUnstartedAttachments();
      this.#updateConversation({
        ...this.#viewModel.conversation,
        messages: [...messages],
        runState: "idle",
      });
    } catch (error) {
      await discardUnstartedAttachments();
      this.#providerStatus = "unavailable";
      if (runStarted) {
        messages.pop();
        restoreSubmittedDraft();
      }
      else messages.splice(-2, 2);
      const message = error instanceof Error ? error.message : String(error);
      this.#updateConversation({
        ...this.#viewModel.conversation,
        messages: [...messages],
        runState: "idle",
        toolCalls: this.#interruptedToolCalls(agentRunId),
        vaultChanges: this.#viewModel.conversation.vaultChanges,
        agentRuns: runStarted
          ? this.#runsWithStatus(agentRunId, "interrupted")
          : this.#viewModel.conversation.agentRuns.filter(({ id }) => id !== agentRunId),
        error: { code: "transport_error", message },
      });
    } finally {
      this.#sendPending = false;
      if (this.#activeRun?.agentRunId === agentRunId) this.#activeRun = undefined;
    }
  }

  async resumeAgentRun(agentRunId: string): Promise<void> {
    await this.#resumeAgentRun(agentRunId, this.#recoveredToolResults.get(agentRunId));
  }

  async #resumeAgentRun(
    agentRunId: string,
    recoveredToolResult?: {
      eventId: string;
      result: LocalToolResultPayload;
      toolCallId: string;
    },
  ): Promise<void> {
    const conversationId = this.#viewModel.conversation.activeConversationId;
    if (!conversationId) throw new Error("Create a Conversation before resuming an Agent Run.");
    if (this.#viewModel.conversation.runState === "streaming") {
      throw new Error("Wait for the current Agent Run to finish.");
    }
    const run = this.#viewModel.conversation.agentRuns.find((candidate) => candidate.id === agentRunId);
    if (!run || run.status !== "interrupted") {
      throw new Error(`Agent Run '${agentRunId}' is not Interrupted.`);
    }
    const messages = [
      ...this.#viewModel.conversation.messages,
      { agentRunId, role: "assistant" as const, text: "" },
    ];
    this.#activeRun = { agentRunId, conversationId };
    this.#updateConversation({
      ...this.#viewModel.conversation,
      agentRuns: this.#runsWithStatus(agentRunId, "running"),
      messages,
      runState: "streaming",
      error: undefined,
    });
    try {
      for await (const event of this.#runtime.resumeAgentRun({
        conversationId,
        agentRunId,
        ...(recoveredToolResult ? { recoveredToolResult } : {}),
      })) {
        if (event.type === "agent_run.resumed") {
          this.#providerStatus = "connected";
          this.refreshPresentation();
        } else if (event.type === "agent_run.delta") {
          this.#appendAgentDelta(messages, agentRunId, event.delta);
        } else if (event.type === "agent_run.completed") {
          messages[messages.length - 1] = { agentRunId, ...event.output };
          this.#recoveredToolResults.delete(agentRunId);
          this.#setRunStatus(agentRunId, "completed");
        } else if (event.type === "tool_call.requested") {
          const requestedChange = this.#requestedChange(
            event.toolCallId,
            event.tool.name,
            event.tool.arguments,
          );
          this.#updateConversation({
            ...this.#viewModel.conversation,
            toolCalls: [
              ...this.#viewModel.conversation.toolCalls,
              {
                id: event.toolCallId,
                agentRunId,
                name: event.tool.name,
                arguments: event.tool.arguments,
                status: "requested",
              },
            ],
            vaultChanges: requestedChange
              ? [...this.#viewModel.conversation.vaultChanges, requestedChange]
              : this.#viewModel.conversation.vaultChanges,
          });
        } else if (event.type === "tool_call.completed") {
          if (event.tool.name === "vault_propose_changes") {
            await this.#vaultChanges?.acknowledge?.(event.toolCallId);
          }
          if (this.#recoveredToolResults.get(agentRunId)?.toolCallId === event.toolCallId) {
            this.#recoveredToolResults.delete(agentRunId);
          }
          this.#updateConversation({
            ...this.#viewModel.conversation,
            toolCalls: this.#viewModel.conversation.toolCalls.map((call) =>
              call.id === event.toolCallId
                ? {
                    ...call,
                    status: event.status,
                    ...(event.error ? { error: event.error } : {}),
                  }
                : call
            ),
            error:
              event.status === "failed" && event.error
                ? { code: event.error.code, message: event.error.message }
                : this.#viewModel.conversation.error,
          });
        } else if (event.type === "agent_run.failed") {
          if (providerHealthFailure(event.error.code)) this.#providerStatus = "unavailable";
          this.#recoveredToolResults.delete(agentRunId);
          messages.pop();
          this.#updateConversation({
            ...this.#viewModel.conversation,
            messages: [...messages],
            agentRuns: this.#runsWithStatus(agentRunId, "failed", event.error),
            runState: "idle",
            error: event.error,
          });
        } else if (event.type === "agent_run.cancelled" || event.type === "agent_run.interrupted") {
          const cancelled = event.type === "agent_run.cancelled";
          if (cancelled) this.#recoveredToolResults.delete(agentRunId);
          if (cancelled && event.output?.text.length) {
            messages[messages.length - 1] = { agentRunId, ...event.output };
          } else {
            messages.pop();
          }
          this.#updateConversation({
            ...this.#viewModel.conversation,
            messages: [...messages],
            agentRuns: this.#runsWithStatus(agentRunId, cancelled ? "cancelled" : "interrupted"),
            runState: "idle",
            toolCalls: cancelled
              ? this.#terminalizedToolCalls(agentRunId)
              : this.#interruptedToolCalls(agentRunId, recoveredToolResult?.toolCallId),
            vaultChanges: cancelled
              ? this.#cancelPendingVaultChanges(agentRunId)
              : this.#viewModel.conversation.vaultChanges,
          });
        }
      }
      this.#updateConversation({
        ...this.#viewModel.conversation,
        messages: [...messages],
        runState: "idle",
      });
    } catch (error) {
      this.#providerStatus = "unavailable";
      messages.pop();
      this.#updateConversation({
        ...this.#viewModel.conversation,
        messages: [...messages],
        runState: "idle",
        agentRuns: this.#runsWithStatus(agentRunId, "interrupted"),
        toolCalls: this.#interruptedToolCalls(agentRunId, recoveredToolResult?.toolCallId),
        error: {
          code: "transport_error",
          message: error instanceof Error ? error.message : String(error),
        },
      });
    } finally {
      if (this.#activeRun?.agentRunId === agentRunId) this.#activeRun = undefined;
    }
  }

  stopAgentRun(): void {
    if (!this.#activeRun) return;
    this.#runtime.cancelAgentRun(this.#activeRun);
  }

  canReviseStoppedRun(agentRunId: string): boolean {
    const run = this.#viewModel.conversation.agentRuns.find(({ id }) => id === agentRunId);
    const prompt = this.#viewModel.conversation.messages.find(
      (message) => message.agentRunId === agentRunId && message.role === "user",
    )?.text;
    if (!run || run.status !== "cancelled" || !prompt) return false;
    if (this.#draftImages.length > 0) return false;
    return this.#draftText.length === 0 || this.#draftText === prompt;
  }

  reviseStoppedRun(agentRunId: string): boolean {
    if (!this.canReviseStoppedRun(agentRunId)) return false;
    const prompt = this.#viewModel.conversation.messages.find(
      (message) => message.agentRunId === agentRunId && message.role === "user",
    )?.text;
    if (!prompt) return false;
    if (this.#draftText === prompt) return true;
    this.#draftText = prompt;
    this.#draftRevision += 1;
    this.refreshPresentation();
    return true;
  }

  async decideVaultChange(toolCallId: string, decision: "apply" | "reject"): Promise<void> {
    if (!this.#vaultChanges) throw new Error("Vault Change decisions are unavailable.");
    const call = this.#viewModel.conversation.toolCalls.find((candidate) => candidate.id === toolCallId);
    const run = call
      ? this.#viewModel.conversation.agentRuns.find((candidate) => candidate.id === call.agentRunId)
      : undefined;
    this.#setVaultChangeStatus(toolCallId, decision === "apply" ? "applying" : "rejecting");
    let result: LocalToolResultPayload;
    try {
      result = await this.#vaultChanges.decide(toolCallId, decision);
    } catch (error) {
      const message = error instanceof Error ? error.message : String(error);
      this.#setVaultChangeStatus(toolCallId, "pending");
      this.#updateConversation({
        ...this.#viewModel.conversation,
        error: { code: "tool_error", message },
      });
      return;
    }
    if (!result.ok) {
      this.#setVaultChangeStatus(toolCallId, "failed");
      this.#updateConversation({
        ...this.#viewModel.conversation,
        error: { code: result.error.code, message: result.error.message },
      });
    } else if (result.value.type === "vault_propose_changes") {
      this.#setVaultChangeStatus(toolCallId, result.value.decision);
    } else {
      throw new Error("The Vault Change decision returned the wrong Tool Result type.");
    }
    if (call && run?.status === "interrupted") {
      const recoveredToolResult = {
        eventId: randomUUID(),
        toolCallId,
        result,
      };
      this.#recoveredToolResults.set(run.id, recoveredToolResult);
      await this.#resumeAgentRun(run.id, recoveredToolResult);
    }
  }

  async undoVaultChange(batchId: string): Promise<void> {
    if (!this.#vaultChanges) throw new Error("Vault Change undo is unavailable.");
    const result = await this.#vaultChanges.undo(batchId);
    const change = this.#viewModel.conversation.vaultChanges.find(
      (candidate) => candidate.batchId === batchId,
    );
    if (!change) return;
    if (result.ok) this.#setVaultChangeStatus(change.toolCallId, "undone");
    else {
      if (result.error.code === "undo_conflict" && result.error.conflicts) {
        this.#updateConversation({
          ...this.#viewModel.conversation,
          vaultChanges: this.#viewModel.conversation.vaultChanges.map((candidate) =>
            candidate.toolCallId === change.toolCallId
              ? { ...candidate, status: "conflicted", conflicts: result.error.conflicts }
              : candidate,
          ),
        });
      }
      this.#updateConversation({
        ...this.#viewModel.conversation,
        error: { code: "provider_error", message: result.error.message },
      });
    }
  }

  async stop(): Promise<void> {
    await this.#runtime.stop();
    this.#update({ state: "idle" });
  }

  #update(runtime: SidebarViewModel["runtime"]): void {
    this.#viewModel = { ...this.#viewModel, runtime };
    for (const subscriber of this.#subscribers) subscriber(this.getViewModel());
  }

  async #rehydratePendingVaultChanges(calls: ToolCallRecord[]): Promise<void> {
    if (!this.#vaultChanges?.rehydrate) return;
    for (const call of calls) {
      if (
        call.name === "vault_propose_changes" &&
        call.status === "requested" &&
        call.vaultChangeState === "pending"
      ) {
        const restored = await this.#vaultChanges.rehydrate(call.id);
        if (restored.result) {
          this.#recoveredToolResults.set(call.agentRunId, {
            eventId: randomUUID(),
            toolCallId: call.id,
            result: restored.result,
          });
        }
      } else if (call.name === "vault_propose_changes") {
        await this.#vaultChanges.acknowledge?.(call.id);
      }
    }
  }

  #toolCallsWithRecoveredFailures(calls: ToolCallRecord[]): ToolCallRecord[] {
    const recoveredIds = new Set(
      [...this.#recoveredToolResults.values()].map(({ toolCallId }) => toolCallId),
    );
    return calls.map((call) =>
      recoveredIds.has(call.id) && call.status === "requested"
        ? { ...call, status: "failed" }
        : call,
    );
  }

  #updateConversation(conversation: SidebarViewModel["conversation"]): void {
    this.#viewModel = { ...this.#viewModel, conversation };
    for (const subscriber of this.#subscribers) subscriber(this.getViewModel());
  }

  #appendAgentDelta(
    messages: SidebarViewModel["conversation"]["messages"],
    agentRunId: string,
    delta: string,
  ): void {
    this.#markTranscriptContentAdded();
    messages[messages.length - 1] = {
      agentRunId,
      role: "assistant",
      text: messages[messages.length - 1].text + delta,
    };
    this.#updateConversation({ ...this.#viewModel.conversation, messages: [...messages] });
  }

  #markTranscriptContentAdded(): void {
    if (this.#transcriptFollowMode === "frozen") this.#transcriptHasNewContent = true;
  }

  #resetTranscriptFollowing(): void {
    this.#transcriptFollowMode = "following";
    this.#transcriptHasNewContent = false;
  }

  #presentation(): SidebarViewModel["presentation"] {
    const activities = this.#viewModel.conversation.toolCalls.flatMap((call) => {
      if (call.name === "vault_propose_changes") return [];
      const action = TOOL_ACTIONS[call.name];
      const target = toolTarget(call);
      return [{
        action,
        details: JSON.stringify(
          {
            arguments: call.arguments,
            status: call.status,
            ...(call.error ? { error: call.error } : {}),
          },
          null,
          2,
        ),
        id: call.id,
        label: `${action}${target ? ` ${target}` : ""} · ${call.status}`,
        status: call.status,
        ...(target ? { target } : {}),
      }];
    });
    const resumable = [...this.#viewModel.conversation.agentRuns]
      .reverse()
      .find(
        (run) =>
          run.status === "interrupted" &&
          !this.#viewModel.conversation.toolCalls.some(
            (call) =>
              call.agentRunId === run.id &&
              call.name === "vault_propose_changes" &&
              call.status === "requested",
          ),
      );
    const primaryAction = this.#viewModel.conversation.runState === "streaming"
      ? {
          ...(this.#activeRun ? { agentRunId: this.#activeRun.agentRunId } : {}),
          kind: "stop" as const,
          label: "Stop" as const,
        }
      : resumable
        ? { agentRunId: resumable.id, kind: "resume" as const, label: "Resume" as const }
        : { kind: "send" as const, label: "Send" as const };
    const selectedModel = this.#viewModel.conversation.models.find(
      ({ id }) => id === this.#viewModel.conversation.selectedModelId,
    );
    const permissionMode = this.#environment.getVaultPermissionMode();
    const activityById = new Map(activities.map((activity) => [activity.id, activity]));
    const changeByToolCallId = new Map(
      this.#viewModel.conversation.vaultChanges.map((change) => [change.toolCallId, change]),
    );
    const transcript: SidebarViewModel["presentation"]["transcript"] = [];
    const runStatusById = new Map(
      this.#viewModel.conversation.agentRuns.map((run) => [run.id, run.status]),
    );
    const messageKeys = new Map<
      SidebarViewModel["conversation"]["messages"][number],
      string
    >();
    const messageOccurrences = new Map<string, number>();
    for (const message of this.#viewModel.conversation.messages) {
      const group = `${message.agentRunId}:${message.role}`;
      const occurrence = messageOccurrences.get(group) ?? 0;
      messageOccurrences.set(group, occurrence + 1);
      messageKeys.set(
        message,
        message.id ? `persisted:${message.id}` : `ephemeral:${group}:${occurrence}`,
      );
    }
    const presentMessage = (
      message: SidebarViewModel["conversation"]["messages"][number],
    ): Extract<SidebarViewModel["presentation"]["transcript"][number], { kind: "message" }> => {
      const key = messageKeys.get(message);
      if (!key) throw new Error("Sidebar message presentation requires a stable key.");
      return {
        key,
        kind: "message",
        message,
        presentation: message.role === "assistant"
          ? {
              copyable: runStatusById.get(message.agentRunId) === "completed",
              format: "markdown",
              layout: "full_width_agent",
            }
          : { copyable: false, format: "plain_text", layout: "compact_user" },
      };
    };
    const includedMessages = new Set<SidebarViewModel["conversation"]["messages"][number]>();
    const includedCalls = new Set<string>();
    const latestRun = this.#viewModel.conversation.agentRuns.at(-1);
    for (const run of this.#viewModel.conversation.agentRuns) {
      const runMessages = this.#viewModel.conversation.messages.filter(
        (message) => message.agentRunId === run.id,
      );
      const originatingPrompt = runMessages.find(({ role }) => role === "user")?.text;
      for (const message of runMessages.filter(({ role }) => role === "user")) {
        includedMessages.add(message);
        transcript.push(presentMessage(message));
      }
      const summarizedActivities: SidebarViewModel["presentation"]["activities"] = [];
      for (const call of this.#viewModel.conversation.toolCalls.filter(
        (candidate) => candidate.agentRunId === run.id,
      )) {
        includedCalls.add(call.id);
        const change = changeByToolCallId.get(call.id);
        const activity = activityById.get(call.id);
        if (change) transcript.push({ kind: "vault_change", change });
        else if (activity?.status === "failed") {
          transcript.push({ kind: "activity_error", activity });
        } else if (activity) summarizedActivities.push(activity);
      }
      if (summarizedActivities.length > 0) {
        transcript.push({
          activities: summarizedActivities,
          agentRunId: run.id,
          kind: "activity_summary",
          label: `${summarizedActivities.length} 个工具活动`,
        });
      }
      for (const message of runMessages.filter(({ role }) => role === "assistant")) {
        includedMessages.add(message);
        transcript.push(presentMessage(message));
      }
      if (run.status !== "completed" && run.status !== "running") {
        const labels = {
          cancelled: "已停止",
          failed: "Run failed.",
          interrupted: "Run interrupted. Resume when ready.",
        } as const;
        transcript.push({
          kind: "run_status",
          agentRunId: run.id,
          label: labels[run.status],
          status: run.status,
          ...(run.status === "cancelled" && originatingPrompt
            ? {
                revision: {
                  enabled:
                    this.#draftImages.length === 0 &&
                    (this.#draftText.length === 0 ||
                      this.#draftText === originatingPrompt),
                  label: "放入输入框" as const,
                },
              }
            : {}),
          ...(run.error?.message
            ? { message: run.error.message }
            : latestRun?.id === run.id && this.#viewModel.conversation.error
              ? { message: this.#viewModel.conversation.error.message }
            : {}),
        });
      }
    }
    for (const message of this.#viewModel.conversation.messages) {
      if (!includedMessages.has(message)) transcript.push(presentMessage(message));
    }
    const unmatchedActivities = new Map<string, SidebarViewModel["presentation"]["activities"]>();
    for (const call of this.#viewModel.conversation.toolCalls) {
      if (includedCalls.has(call.id)) continue;
      const change = changeByToolCallId.get(call.id);
      const activity = activityById.get(call.id);
      if (change) transcript.push({ kind: "vault_change", change });
      else if (activity?.status === "failed") {
        transcript.push({ kind: "activity_error", activity });
      } else if (activity) {
        const group = unmatchedActivities.get(call.agentRunId) ?? [];
        group.push(activity);
        unmatchedActivities.set(call.agentRunId, group);
      }
    }
    for (const [agentRunId, groupedActivities] of unmatchedActivities) {
      transcript.push({
        activities: groupedActivities,
        agentRunId,
        kind: "activity_summary",
        label: `${groupedActivities.length} 个工具活动`,
      });
    }
    return {
      activities,
      composer: {
        ...(this.#draftImages[0]
          ? {
              attachment: {
                fileName: this.#draftImages[0].fileName,
                mediaType: this.#draftImages[0].mediaType,
                size: this.#draftImages[0].bytes.byteLength,
              },
            }
          : {}),
        attachments: this.#draftImages.map((image) => ({
          fileName: image.fileName,
          mediaType: image.mediaType,
          previewBytes: new Uint8Array(image.bytes),
          size: image.bytes.byteLength,
        })),
        contextChips: [{ kind: "scope", label: "Vault context" }],
        draftText: this.#draftText,
        isPreparingAttachments: this.#attachmentImportPending,
        isSending: this.#sendPending,
        permissionMode,
        primaryAction,
      },
      settings: {
        advanced: {
          diagnostics:
            this.#viewModel.runtime.message ?? `Runtime is ${this.#viewModel.runtime.state}.`,
          gitRetention: "Git Checkpoints: 30 days or the most recent 100 batches.",
          hostedWebSearch: "Hosted Web Search capability is probed per backend and model.",
        },
        ...(selectedModel?.supportsFastMode
          ? { fastMode: { enabled: this.#environment.getFastModeEnabled() } }
          : {}),
        ...(selectedModel ? { model: { id: selectedModel.id, label: selectedModel.label } } : {}),
        permissionMode,
        providerStatus: this.#providerStatus,
        runtimeStatus: this.#viewModel.runtime.state,
      },
      transcriptScroll: {
        hasNewContent: this.#transcriptHasNewContent,
        mode: this.#transcriptFollowMode,
      },
      transcript,
    };
  }

  #runsWithStatus(
    agentRunId: string,
    status: AgentRunRecord["status"],
    error?: AgentRunRecord["error"],
  ): AgentRunRecord[] {
    return this.#viewModel.conversation.agentRuns.map((run) => {
      if (run.id !== agentRunId) return run;
      const { error: _previousError, ...withoutError } = run;
      return { ...withoutError, status, ...(error ? { error } : {}) };
    });
  }

  #terminalizedToolCalls(agentRunId: string): ToolCallRecord[] {
    return this.#viewModel.conversation.toolCalls.map((call) =>
      call.agentRunId === agentRunId && call.status === "requested"
        ? { ...call, status: "failed" }
        : call,
    );
  }

  #interruptedToolCalls(agentRunId: string, recoveredToolCallId?: string): ToolCallRecord[] {
    return this.#viewModel.conversation.toolCalls.map((call) =>
      call.agentRunId === agentRunId &&
      call.status === "requested" &&
      (call.name !== "vault_propose_changes" || call.id === recoveredToolCallId)
        ? { ...call, status: "failed" }
        : call,
    );
  }

  #setRunStatus(agentRunId: string, status: AgentRunRecord["status"]): void {
    this.#updateConversation({
      ...this.#viewModel.conversation,
      agentRuns: this.#runsWithStatus(agentRunId, status),
    });
  }

  #setVaultChangeStatus(
    toolCallId: string,
    status: SidebarViewModel["conversation"]["vaultChanges"][number]["status"],
  ): void {
    this.#updateConversation({
      ...this.#viewModel.conversation,
      vaultChanges: this.#vaultChangesWithStatus(toolCallId, status),
    });
  }

  #vaultChangesWithStatus(
    toolCallId: string,
    status: SidebarViewModel["conversation"]["vaultChanges"][number]["status"],
  ): SidebarViewModel["conversation"]["vaultChanges"] {
    return this.#viewModel.conversation.vaultChanges.map((change) =>
      change.toolCallId === toolCallId ? { ...change, status } : change,
    );
  }

  #cancelPendingVaultChanges(
    agentRunId: string,
  ): SidebarViewModel["conversation"]["vaultChanges"] {
    const pendingIds = new Set(
      this.#viewModel.conversation.toolCalls
        .filter(
          (call) =>
            call.agentRunId === agentRunId &&
            call.name === "vault_propose_changes" &&
            call.status === "requested",
        )
        .map((call) => call.id),
    );
    if (pendingIds.size === 0) return this.#viewModel.conversation.vaultChanges;
    for (const toolCallId of pendingIds) this.#vaultChanges?.cancel(toolCallId);
    return this.#viewModel.conversation.vaultChanges.map((change) =>
      pendingIds.has(change.toolCallId) && change.status === "pending"
        ? { ...change, status: "failed" }
        : change,
    );
  }

  #requestedChange(
    toolCallId: string,
    name: ToolCallRecord["name"],
    arguments_: unknown,
  ): SidebarViewModel["conversation"]["vaultChanges"][number] | undefined {
    if (name !== "vault_propose_changes" || !arguments_ || typeof arguments_ !== "object") {
      return undefined;
    }
    const proposal = arguments_ as Partial<VaultChangeBatchProposal>;
    if (
      typeof proposal.batchId !== "string" ||
      typeof proposal.task !== "string" ||
      !Array.isArray(proposal.actions)
    ) {
      return undefined;
    }
    return {
      toolCallId,
      batchId: proposal.batchId,
      task: proposal.task,
      status: "pending",
      actions: proposal.actions
        .filter(
          (action) =>
            action && typeof action.path === "string" && typeof action.operation === "string",
        )
        .map((action) => ({ path: action.path, operation: action.operation })),
    };
  }

  #changesFromToolCalls(
    calls: ToolCallRecord[],
  ): SidebarViewModel["conversation"]["vaultChanges"] {
    return calls.flatMap((call) => {
      const change = this.#requestedChange(call.id, call.name, call.arguments);
      if (!change) return [];
      return [
        {
          ...change,
          status: this.#vaultChangeStatus(call),
          ...(call.error ? { message: call.error.message } : {}),
        },
      ];
    });
  }

  #vaultChangeStatus(
    call: ToolCallRecord,
  ): SidebarViewModel["conversation"]["vaultChanges"][number]["status"] {
    if (
      call.vaultChangeState === "applied" ||
      call.vaultChangeState === "expired" ||
      call.vaultChangeState === "rejected" ||
      call.vaultChangeState === "undone"
    ) {
      return call.vaultChangeState;
    }
    if (
      call.vaultChangeState === "recovery_failed" ||
      call.vaultChangeState === "rolled_back" ||
      call.status === "failed"
    ) {
      return "failed";
    }
    if (call.status === "requested") return "pending";
    return call.decision ?? "applied";
  }
}
