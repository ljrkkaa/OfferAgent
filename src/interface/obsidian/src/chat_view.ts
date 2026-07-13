import {
	ItemView,
	MarkdownRenderer,
	Scope,
	WorkspaceLeaf,
	setIcon,
	sanitizeHTMLToDom,
} from "obsidian";
import { KhojPaneView } from "src/pane_view";
import {
	KhojView,
	createCopyParentText,
	getLinkToEntry,
	pasteTextAtCursor,
} from "src/utils";
import { KhojSearchModal } from "src/search_modal";
import Khoj from "src/main";
import {
	FileInteractions,
	parseVaultActions,
	vaultActionReview,
	VaultAction,
	VaultActionResult,
} from "src/interact_with_files";
import { ChatRequest } from "./api";
import { ChatRuntime, StreamEvent } from "./chat_runtime";

interface ChatMessageState {
	newResponseTextEl: HTMLDivElement | null;
	newResponseEl: HTMLDivElement | null;
	loadingEllipsis: HTMLDivElement | null;
	references: { [key: string]: any };
	rawResponse: string;
	rawQuery: string;
	turnId: string;
	parentRetryCount?: number;
}

interface Location {
	region?: string;
	city?: string;
	countryName?: string;
	countryCode?: string;
	timezone: string;
}

interface RenderMessageOptions {
	chatBodyEl: Element;
	message: string;
	sender: string;
	turnId?: string;
	dt?: Date;
	raw?: boolean;
	willReplace?: boolean;
	isSystemMessage?: boolean;
}

function isRecord(value: unknown): value is Record<string, unknown> {
	return typeof value === "object" && value !== null;
}

export class KhojChatView extends KhojPaneView {
	result: string;
	waitingForLocation: boolean;
	location: Location = {
		timezone: Intl.DateTimeFormat().resolvedOptions().timeZone,
	};
	keyPressTimeout: NodeJS.Timeout | null = null;
	private startingMessage: string = this.getLearningMoment();
	chatMessageState: ChatMessageState;
	private fileAccessMode: "none" | "read" | "write" = "read"; // Track the current file access mode
	private fileInteractions: FileInteractions;
	private runtime: ChatRuntime;
	private pendingVaultActions: VaultAction[] = [];
	private pendingVaultActionConversationId: string | null = null;
	private pendingVaultActionMessage: Element | null = null;
	private pendingVaultActionButtons: HTMLDivElement | null = null;
	private isStreaming: boolean = false; // Flag to track streaming state

	constructor(leaf: WorkspaceLeaf, plugin: Khoj) {
		super(leaf, plugin);
		this.fileInteractions = new FileInteractions(this.app);
		this.runtime = new ChatRuntime(plugin.server);

		// Initialize file access mode from persisted settings
		this.fileAccessMode = this.setting.fileAccessMode ?? "read";

		this.waitingForLocation = false;

		// Register chat view keybindings
		this.scope = new Scope(this.app.scope);
		this.scope.register(["Ctrl", "Alt"], "n", (_) =>
			this.createNewConversation(),
		);
		this.scope.register(
			["Ctrl", "Alt"],
			"o",
			async (_) => await this.toggleChatSessions(),
		);
		this.scope.register(["Ctrl"], "f", (_) =>
			new KhojSearchModal(
				this.app,
				this.setting,
				this.plugin.server,
			).open(),
		);
		this.scope.register(["Ctrl"], "r", (_) => {
			this.activateView(KhojView.SIMILAR);
		});
	}

	getViewType(): string {
		return KhojView.CHAT;
	}

	getDisplayText(): string {
		return "OfferAgent Chat";
	}

	getIcon(): string {
		return "message-circle";
	}

	async chat() {
		// Never carry an unapproved write into the next user turn.
		await this.cancelPendingVaultActions();

		// Get text in chat input element
		let input_el = <HTMLTextAreaElement>(
			this.contentEl.getElementsByClassName("khoj-chat-input")[0]
		);

		// Clear text after extracting message to send
		let user_message = input_el.value.trim();

		// Store the message in the array if it's not empty
		if (user_message) {
			// Update starting message after sending a new message
			this.startingMessage = this.getLearningMoment();
			input_el.placeholder = this.startingMessage;

			// Clear input and resize
			input_el.value = "";
			this.autoResize();

			// Get and render chat response to user message
			await this.getChatResponse(user_message, user_message);
		}
	}

	async onOpen() {
		let { contentEl } = this;

		// The parent class handles creating the header and attaching the click on the "New Chat" button
		// We handle the rest of the interface here
		// Call the parent class's onOpen method first
		await super.onOpen();

		contentEl.addClass("khoj-chat");

		// Create the chat body
		let chatBodyEl = contentEl.createDiv({
			attr: { id: "khoj-chat-body", class: "khoj-chat-body" },
		});
		// Add chat input field
		let inputRow = contentEl.createDiv("khoj-input-row");

		let chatSessions = inputRow.createEl("button", {
			text: "Chat Sessions",
			attr: {
				class: "khoj-input-row-button clickable-icon",
				title: "Show Conversations (Ctrl+Alt+O)",
			},
		});
		chatSessions.addEventListener("click", async (_) => {
			await this.toggleChatSessions();
		});
		setIcon(chatSessions, "history");

		// Add file access mode button
		let fileAccessButton = inputRow.createEl("button", {
			text: "File Access",
			attr: {
				class: "khoj-input-row-button clickable-icon",
				title: "Toggle open file access",
			},
		});
		// Set initial icon based on persisted setting
		switch (this.fileAccessMode) {
			case "none":
				setIcon(fileAccessButton, "file-x");
				fileAccessButton.title = "Toggle open file access (No Access)";
				break;
			case "write":
				setIcon(fileAccessButton, "file-edit");
				fileAccessButton.title =
					"Toggle open file access (Read & Write)";
				break;
			case "read":
			default:
				setIcon(fileAccessButton, "file-search");
				fileAccessButton.title = "Toggle open file access (Read Only)";
				break;
		}
		fileAccessButton.addEventListener("click", async () => {
			// Cycle through modes: none -> read -> write -> none
			switch (this.fileAccessMode) {
				case "none":
					this.fileAccessMode = "read";
					setIcon(fileAccessButton, "file-search");
					fileAccessButton.title =
						"Toggle open file access (Read Only)";
					break;
				case "read":
					this.fileAccessMode = "write";
					setIcon(fileAccessButton, "file-edit");
					fileAccessButton.title =
						"Toggle open file access (Read & Write)";
					break;
				case "write":
					this.fileAccessMode = "none";
					setIcon(fileAccessButton, "file-x");
					fileAccessButton.title =
						"Toggle open file access (No Access)";
					break;
			}

			// Persist the updated mode to settings
			this.setting.fileAccessMode = this.fileAccessMode;
			await this.plugin.saveSettings();
		});

		let chatInput = inputRow.createEl("textarea", {
			attr: {
				id: "khoj-chat-input",
				autofocus: "autofocus",
				class: "khoj-chat-input option",
			},
		});
		chatInput.addEventListener("input", (_) => {
			this.onChatInput();
		});
		chatInput.addEventListener("keydown", (event) => {
			this.incrementalChat(event);
		});

		let send = inputRow.createEl("button", {
			text: "Send",
			attr: {
				id: "khoj-chat-send",
				class: "khoj-chat-send khoj-input-row-button clickable-icon",
			},
		});
		setIcon(send, "arrow-up-circle");
		let sendImg = <SVGElement>(
			send.getElementsByClassName("lucide-arrow-up-circle")[0]
		);
		sendImg.addEventListener("click", async (_) => {
			await this.chat();
		});

		// Get chat history from Khoj backend and set chat input state
		let getChatHistorySucessfully = await this.getChatHistory(chatBodyEl);

		let placeholderText: string = getChatHistorySucessfully
			? this.startingMessage
			: "Configure OfferAgent to enable chat";
		chatInput.placeholder = placeholderText;
		chatInput.disabled = !getChatHistorySucessfully;
		this.autoResize();

		// Scroll to bottom of chat messages and focus on chat input field, once messages rendered
		requestAnimationFrame(() => {
			// Ensure layout and paint have occurred
			requestAnimationFrame(() => {
				this.scrollChatToBottom();
				const chatInput = <HTMLTextAreaElement>(
					this.contentEl.getElementsByClassName("khoj-chat-input")[0]
				);
				chatInput?.focus();
			});
		});
	}

	processOnlineReferences(referenceSection: HTMLElement, onlineContext: any) {
		let numOnlineReferences = 0;
		for (let subquery in onlineContext) {
			let onlineReference = onlineContext[subquery];
			if (onlineReference.organic && onlineReference.organic.length > 0) {
				numOnlineReferences += onlineReference.organic.length;
				for (let key in onlineReference.organic) {
					let reference = onlineReference.organic[key];
					let polishedReference = this.generateOnlineReference(
						referenceSection,
						reference,
						key,
					);
					referenceSection.appendChild(polishedReference);
				}
			}

			if (
				onlineReference.knowledgeGraph &&
				onlineReference.knowledgeGraph.length > 0
			) {
				numOnlineReferences += onlineReference.knowledgeGraph.length;
				for (let key in onlineReference.knowledgeGraph) {
					let reference = onlineReference.knowledgeGraph[key];
					let polishedReference = this.generateOnlineReference(
						referenceSection,
						reference,
						key,
					);
					referenceSection.appendChild(polishedReference);
				}
			}

			if (
				onlineReference.peopleAlsoAsk &&
				onlineReference.peopleAlsoAsk.length > 0
			) {
				numOnlineReferences += onlineReference.peopleAlsoAsk.length;
				for (let key in onlineReference.peopleAlsoAsk) {
					let reference = onlineReference.peopleAlsoAsk[key];
					let polishedReference = this.generateOnlineReference(
						referenceSection,
						reference,
						key,
					);
					referenceSection.appendChild(polishedReference);
				}
			}

			if (
				onlineReference.webpages &&
				onlineReference.webpages.length > 0
			) {
				numOnlineReferences += onlineReference.webpages.length;
				for (let key in onlineReference.webpages) {
					let reference = onlineReference.webpages[key];
					let polishedReference = this.generateOnlineReference(
						referenceSection,
						reference,
						key,
					);
					referenceSection.appendChild(polishedReference);
				}
			}
		}

		return numOnlineReferences;
	}

	generateOnlineReference(messageEl: Element, reference: any, index: string) {
		// Generate HTML for Chat Reference
		let title = reference.title || reference.link;
		let link = reference.link;
		let snippet = reference.snippet;
		let question = reference.question
			? `<b>Question:</b> ${reference.question}<br><br>`
			: "";

		let referenceButton = messageEl.createEl("button");
		let linkElement = referenceButton.createEl("a");
		linkElement.setAttribute("href", link);
		linkElement.setAttribute("target", "_blank");
		linkElement.setAttribute("rel", "noopener noreferrer");
		linkElement.classList.add("reference-link");
		linkElement.setAttribute("title", title);
		linkElement.textContent = title;

		referenceButton.id = `ref-${index}`;
		referenceButton.classList.add("reference-button");
		referenceButton.classList.add("collapsed");
		referenceButton.tabIndex = 0;

		// Add event listener to toggle full reference on click
		referenceButton.addEventListener("click", function () {
			if (this.classList.contains("collapsed")) {
				this.classList.remove("collapsed");
				this.classList.add("expanded");
				this.innerHTML =
					linkElement.outerHTML + `<br><br>${question + snippet}`;
			} else {
				this.classList.add("collapsed");
				this.classList.remove("expanded");
				this.innerHTML = linkElement.outerHTML;
			}
		});

		return referenceButton;
	}

	generateReference(messageEl: Element, referenceJson: any, index: number) {
		let reference: string = referenceJson.hasOwnProperty("compiled")
			? referenceJson.compiled
			: referenceJson;
		let referenceFile = referenceJson.hasOwnProperty("file")
			? referenceJson.file
			: null;

		// Get all markdown and PDF files in vault
		const mdFiles = this.app.vault.getMarkdownFiles();
		const pdfFiles = this.app.vault
			.getFiles()
			.filter((file) => file.extension === "pdf");

		// Escape reference for HTML rendering
		reference = reference.split("\n").slice(1).join("\n");
		let escaped_ref = reference.replace(/"/g, "&quot;");

		// Generate HTML for Chat Reference
		let referenceButton = messageEl.createEl("button");

		if (referenceFile) {
			// Find vault file associated with current reference
			const linkToEntry = getLinkToEntry(
				mdFiles.concat(pdfFiles),
				referenceFile,
				reference,
			);

			const linkElement: Element = referenceButton.createEl("span");
			linkElement.setAttribute("title", escaped_ref);
			linkElement.textContent = referenceFile;
			if (linkElement && linkToEntry) {
				linkElement.classList.add("reference-link");
				linkElement.addEventListener("click", (event) => {
					event.stopPropagation();
					this.app.workspace.openLinkText(linkToEntry, "");
				});
			}
		}

		let referenceText = referenceButton.createDiv();
		referenceText.textContent = escaped_ref;

		referenceButton.id = `ref-${index}`;
		referenceButton.classList.add("reference-button");
		referenceButton.classList.add("collapsed");
		referenceButton.tabIndex = 0;

		// Add event listener to toggle full reference on click
		referenceButton.addEventListener("click", function () {
			if (this.classList.contains("collapsed")) {
				this.classList.remove("collapsed");
				this.classList.add("expanded");
			} else {
				this.classList.add("collapsed");
				this.classList.remove("expanded");
			}
		});

		return referenceButton;
	}

	formatHTMLMessage(message: string, willReplace = true) {
		// Remove any text between <s>[INST] and </s> tags. These are spurious instructions for some AI chat model.
		message = message.replace(/<s>\[INST\].+(<\/s>)?/g, "");

		// Convert markdown to a sanitized DOM fragment.
		let chatMessageBodyTextEl = this.contentEl.createDiv();
		chatMessageBodyTextEl.appendChild(this.renderMarkdown(message, this));

		// Add action buttons to each chat message, if they don't already exist
		if (willReplace === true) {
			this.renderActionButtons(message, chatMessageBodyTextEl);
		}

		return chatMessageBodyTextEl;
	}

	renderMarkdown(
		markdownText: string,
		component: ItemView,
	): DocumentFragment {
		// Render markdown to an unlinked DOM element
		let virtualChatMessageBodyTextEl = document.createElement("div");

		// Convert the message to html
		MarkdownRenderer.render(
			this.app,
			markdownText,
			virtualChatMessageBodyTextEl,
			"",
			component,
		);

		virtualChatMessageBodyTextEl
			.querySelectorAll("img")
			.forEach((image) => {
				const source = image.getAttribute("src") ?? "";
				if (!source.startsWith("app:") && !source.startsWith("data:"))
					image.remove();
			});

		return sanitizeHTMLToDom(virtualChatMessageBodyTextEl.innerHTML);
	}

	renderMessageWithReferences(
		chatEl: Element,
		message: string,
		sender: string,
		turnId: string,
		context?: object[],
		onlineContext?: object,
		dt?: Date,
		images?: string[],
	) {
		if (!message && !images?.length) return;

		const imageMarkdown =
			sender === "you"
				? images
						?.map(
							(image, index) =>
								`![Attached image ${index + 1}](${image})`,
						)
						.join("\n")
				: "";
		const displayMessage = imageMarkdown
			? `${imageMarkdown}\n\n${message}`
			: message;

		const chatMessageEl = this.renderMessage({
			chatBodyEl: chatEl,
			message: displayMessage,
			sender,
			dt,
			turnId,
		});

		// If no document or online context is provided, skip rendering the reference section
		if (
			(context == null || context.length == 0) &&
			(onlineContext == null ||
				(onlineContext && Object.keys(onlineContext).length == 0))
		) {
			return;
		}

		// If document or online context is provided, render the message with its references
		let references: any = {};
		if (!!context) references["notes"] = context;
		if (!!onlineContext) references["online"] = onlineContext;
		let chatMessageBodyEl = chatMessageEl.getElementsByClassName(
			"khoj-chat-message-text",
		)[0];
		chatMessageBodyEl.appendChild(this.createReferenceSection(references));
	}

	renderMessage({
		chatBodyEl,
		message,
		sender,
		dt,
		turnId,
		raw = false,
		willReplace = true,
		isSystemMessage = false,
	}: RenderMessageOptions): Element {
		let message_time = this.formatDate(dt ?? new Date());

		// Append message to conversation history HTML element.
		// The chat logs should display above the message input box to follow standard UI semantics
		let chatMessageEl = chatBodyEl.createDiv({
			attr: {
				"data-meta": message_time,
				class: `khoj-chat-message ${sender}`,
				...(turnId && { "data-turnid": turnId }),
			},
		});
		let chatMessageBodyEl = chatMessageEl.createDiv();
		chatMessageBodyEl.addClasses(["khoj-chat-message-text", sender]);
		let chatMessageBodyTextEl = chatMessageBodyEl.createDiv();

		// Remove Obsidian specific instructions sent alongside user query in between <SYSTEM></SYSTEM> tags
		if (sender === "you") {
			message = message.replace(/<SYSTEM>.*?<\/SYSTEM>/s, "");
		}

		if (raw) {
			chatMessageBodyTextEl.appendChild(sanitizeHTMLToDom(message));
		} else {
			chatMessageBodyTextEl.appendChild(
				this.renderMarkdown(message, this),
			);
		}

		// Add action buttons to each chat message element
		if (willReplace === true) {
			this.renderActionButtons(
				message,
				chatMessageBodyTextEl,
				isSystemMessage,
			);
		}

		// Remove user-select: none property to make text selectable
		chatMessageEl.style.userSelect = "text";

		// Scroll to bottom after inserting chat messages
		this.scrollChatToBottom();

		return chatMessageEl;
	}

	createKhojResponseDiv(dt?: Date): HTMLDivElement {
		let messageTime = this.formatDate(dt ?? new Date());

		// Append message to conversation history HTML element.
		// The chat logs should display above the message input box to follow standard UI semantics
		let chatBodyEl =
			this.contentEl.getElementsByClassName("khoj-chat-body")[0];
		let chatMessageEl = chatBodyEl.createDiv({
			attr: {
				"data-meta": messageTime,
				class: `khoj-chat-message khoj`,
			},
		});

		// Scroll to bottom after inserting chat messages
		this.scrollChatToBottom();

		return chatMessageEl;
	}

	async renderIncrementalMessage(
		htmlElement: HTMLDivElement,
		additionalMessage: string,
	) {
		this.chatMessageState.rawResponse += additionalMessage;

		htmlElement.replaceChildren(
			this.renderMarkdown(this.chatMessageState.rawResponse, this),
		);

		// Render action buttons for the message
		this.renderActionButtons(
			this.chatMessageState.rawResponse,
			htmlElement,
		);

		// Scroll to bottom of modal, till the send message input box
		this.scrollChatToBottom();
	}

	renderActionButtons(
		message: string,
		chatMessageBodyTextEl: HTMLElement,
		isSystemMessage: boolean = false,
	) {
		let copyButton = this.contentEl.createEl("button");
		copyButton.classList.add("chat-action-button");
		copyButton.title = "Copy Message to Clipboard";
		setIcon(copyButton, "copy-plus");
		copyButton.addEventListener("click", createCopyParentText(message));

		// Add button to paste into current buffer
		let pasteToFile = this.contentEl.createEl("button");
		pasteToFile.classList.add("chat-action-button");
		pasteToFile.title = "Paste Message to File";
		setIcon(pasteToFile, "clipboard-paste");
		pasteToFile.addEventListener("click", (event) => {
			pasteTextAtCursor(
				createCopyParentText(message, "clipboard-paste")(event),
			);
		});

		// Add edit button only for user messages
		let editButton = null;
		if (
			!isSystemMessage &&
			chatMessageBodyTextEl.closest(".khoj-chat-message.you")
		) {
			editButton = this.contentEl.createEl("button");
			editButton.classList.add("chat-action-button");
			editButton.title = "Edit Message";
			setIcon(editButton, "edit-3");
			editButton.addEventListener("click", async () => {
				const messageEl =
					chatMessageBodyTextEl.closest(".khoj-chat-message");
				if (messageEl) {
					// Get all messages up to this one
					const allMessages = Array.from(
						this.contentEl.getElementsByClassName(
							"khoj-chat-message",
						),
					);
					const currentIndex = allMessages.indexOf(
						messageEl as HTMLElement,
					);

					// Store reference to messages that need to be deleted from backend
					const messagesToDelete = allMessages.slice(currentIndex);
					const turnIdsToDelete = Array.from(
						new Set(
							messagesToDelete
								.map((message) =>
									message.getAttribute("data-turnid"),
								)
								.filter((turnId): turnId is string => !!turnId),
						),
					);

					for (const turnId of turnIdsToDelete) {
						if (!(await this.deleteTurnFromBackend(turnId))) {
							return;
						}
					}

					messagesToDelete.forEach((message) =>
						message.classList.add("deleting"),
					);

					// Wait for animation to complete
					await new Promise((resolve) => setTimeout(resolve, 300));
					for (let i = messagesToDelete.length - 1; i >= 0; i--) {
						messagesToDelete[i].remove();
					}

					// Get the message content without the emoji if it exists
					let messageContent = message;
					const emojiRegex = /^[^\p{L}\p{N}]+\s*/u;
					messageContent = messageContent.replace(emojiRegex, "");

					// Set the message in the input field
					const chatInput = this.contentEl.querySelector(
						".khoj-chat-input",
					) as HTMLTextAreaElement;
					if (chatInput) {
						chatInput.value = messageContent;
						chatInput.focus();
					}
				}
			});
		}

		// Add delete button
		let deleteButton = null;
		if (!isSystemMessage) {
			deleteButton = this.contentEl.createEl("button");
			deleteButton.classList.add("chat-action-button");
			deleteButton.title = "Delete Message";
			setIcon(deleteButton, "trash-2");
			deleteButton.addEventListener("click", () => {
				const messageEl =
					chatMessageBodyTextEl.closest(".khoj-chat-message");
				if (messageEl) {
					// Ask for confirmation before deleting
					if (
						confirm("Are you sure you want to delete this message?")
					) {
						this.deleteMessage(messageEl as HTMLElement);
					}
				}
			});
		}

		// Append buttons to parent element
		chatMessageBodyTextEl.append(copyButton, pasteToFile);
		if (editButton) {
			chatMessageBodyTextEl.append(editButton);
		}
		if (deleteButton) {
			chatMessageBodyTextEl.append(deleteButton);
		}
	}

	formatDate(date: Date): string {
		// Format date in HH:MM, DD MMM YYYY format
		let time_string = date.toLocaleTimeString("en-IN", {
			hour: "2-digit",
			minute: "2-digit",
			hour12: false,
		});
		let date_string = date
			.toLocaleString("en-IN", {
				year: "numeric",
				month: "short",
				day: "2-digit",
			})
			.replace(/-/g, " ");
		return `${time_string}, ${date_string}`;
	}

	getLearningMoment(): string {
		return "Ask about your notes or anything else.";
	}

	async createNewConversation() {
		let chatBodyEl = this.contentEl.getElementsByClassName(
			"khoj-chat-body",
		)[0] as HTMLElement;
		chatBodyEl.innerHTML = "";
		chatBodyEl.dataset.conversationId = "";
		chatBodyEl.dataset.conversationTitle = "";
		this.selectConversation(null);
		this.startingMessage = this.getLearningMoment();

		// Update the placeholder of the chat input
		const chatInput = this.contentEl.querySelector(
			".khoj-chat-input",
		) as HTMLTextAreaElement;
		if (chatInput) {
			chatInput.placeholder = this.startingMessage;
		}

		try {
			const conversationId = await this.runtime.createConversation();
			chatBodyEl.dataset.conversationId = conversationId;
			if (chatInput) {
				chatInput.removeAttribute("disabled");
				chatInput.focus();
			}
		} catch (error) {
			console.error("Error creating session:", error);
			this.renderMessage({
				chatBodyEl,
				message: "Failed to create conversation.",
				sender: "khoj",
				isSystemMessage: true,
			});
			return;
		}

		this.renderMessage({
			chatBodyEl,
			message: "Hey, what's up?",
			sender: "khoj",
			isSystemMessage: true,
		});
	}

	async toggleChatSessions(forceShow: boolean = false): Promise<boolean> {
		let chatBodyEl = this.contentEl.getElementsByClassName(
			"khoj-chat-body",
		)[0] as HTMLElement;
		if (
			!forceShow &&
			this.contentEl.getElementsByClassName("side-panel")?.length > 0
		) {
			chatBodyEl.innerHTML = "";
			return this.getChatHistory(chatBodyEl);
		}
		chatBodyEl.innerHTML = "";
		const sidePanelEl = chatBodyEl.createDiv("side-panel");
		const newConversationEl = sidePanelEl.createDiv("new-conversation");
		const conversationHeaderTitleEl = newConversationEl.createDiv(
			"conversation-header-title",
		);
		conversationHeaderTitleEl.textContent = "Conversations";

		const newConversationButtonEl = newConversationEl.createEl("button");
		newConversationButtonEl.classList.add("new-conversation-button");
		newConversationButtonEl.classList.add("side-panel-button");
		newConversationButtonEl.addEventListener("click", (_) =>
			this.createNewConversation(),
		);
		setIcon(newConversationButtonEl, "plus");
		newConversationButtonEl.innerHTML += "New";
		newConversationButtonEl.title = "New Conversation (Ctrl+Alt+N)";

		const existingConversationsEl = sidePanelEl.createDiv(
			"existing-conversations",
		);
		const conversationListEl =
			existingConversationsEl.createDiv("conversation-list");
		const conversationListBodyHeaderEl = conversationListEl.createDiv(
			"conversation-list-header",
		);
		const conversationListBodyEl = conversationListEl.createDiv(
			"conversation-list-body",
		);

		try {
			const conversations = await this.plugin.server.getConversations();
			let conversationId = chatBodyEl.dataset.conversationId;

			if (conversations.length > 0) {
				conversationListBodyHeaderEl.style.display = "block";
				for (let conversation of conversations) {
					let conversationSessionEl = this.contentEl.createEl("div");
					let incomingConversationId = conversation.conversation_id;
					conversationSessionEl.classList.add("conversation-session");
					if (incomingConversationId == conversationId) {
						conversationSessionEl.classList.add(
							"selected-conversation",
						);
					}
					const conversationTitle =
						conversation.slug.split("<SYSTEM>")[0].trim() ||
						`New conversation 🌱`;
					const conversationSessionTitleEl =
						conversationSessionEl.createDiv(
							"conversation-session-title",
						);
					conversationSessionTitleEl.textContent = conversationTitle;
					conversationSessionTitleEl.addEventListener("click", () => {
						chatBodyEl.innerHTML = "";
						chatBodyEl.dataset.conversationId =
							incomingConversationId;
						chatBodyEl.dataset.conversationTitle =
							conversationTitle;
						this.selectConversation(incomingConversationId);
						this.getChatHistory(chatBodyEl);
					});

					let conversationMenuEl = this.contentEl.createEl("div");
					conversationMenuEl = this.addConversationMenu(
						conversationMenuEl,
						conversationSessionEl,
						conversationTitle,
						conversationSessionTitleEl,
						chatBodyEl,
						incomingConversationId,
						incomingConversationId == conversationId,
					);

					conversationSessionEl.appendChild(conversationMenuEl);
					conversationListBodyEl.appendChild(conversationSessionEl);
				}
			}
		} catch (err) {
			console.error("Error fetching chat sessions:", err);
			return false;
		}
		return true;
	}

	addConversationMenu(
		conversationMenuEl: HTMLDivElement,
		conversationSessionEl: HTMLElement,
		conversationTitle: string,
		conversationSessionTitleEl: HTMLElement,
		chatBodyEl: HTMLElement,
		incomingConversationId: string,
		selectedConversation: boolean,
	) {
		conversationMenuEl.classList.add("conversation-menu");

		let editConversationTitleButtonEl = this.contentEl.createEl("button");
		setIcon(editConversationTitleButtonEl, "edit");
		editConversationTitleButtonEl.title = "Rename";
		editConversationTitleButtonEl.classList.add(
			"edit-title-button",
			"three-dot-menu-button-item",
			"clickable-icon",
		);
		if (selectedConversation)
			editConversationTitleButtonEl.classList.add(
				"selected-conversation",
			);
		editConversationTitleButtonEl.addEventListener("click", (event) => {
			event.stopPropagation();

			let conversationMenuChildren = conversationMenuEl.children;
			let totalItems = conversationMenuChildren.length;

			for (let i = totalItems - 1; i >= 0; i--) {
				conversationMenuChildren[i].remove();
			}

			// Create a dialog box to get new title for conversation
			let editConversationTitleInputEl = this.contentEl.createEl("input");
			editConversationTitleInputEl.classList.add(
				"conversation-title-input",
			);
			editConversationTitleInputEl.value = conversationTitle;
			editConversationTitleInputEl.addEventListener(
				"click",
				function (event) {
					event.stopPropagation();
				},
			);
			editConversationTitleInputEl.addEventListener(
				"keydown",
				function (event) {
					if (event.key === "Enter") {
						event.preventDefault();
						editConversationTitleSaveButtonEl.click();
					}
				},
			);
			let editConversationTitleSaveButtonEl =
				this.contentEl.createEl("button");
			conversationSessionTitleEl.replaceWith(
				editConversationTitleInputEl,
			);
			editConversationTitleSaveButtonEl.innerHTML = "Save";
			editConversationTitleSaveButtonEl.classList.add(
				"three-dot-menu-button-item",
				"clickable-icon",
			);
			if (selectedConversation)
				editConversationTitleSaveButtonEl.classList.add(
					"selected-conversation",
				);
			editConversationTitleSaveButtonEl.addEventListener(
				"click",
				async (event) => {
					event.stopPropagation();
					let newTitle = editConversationTitleInputEl.value;
					if (newTitle != null) {
						try {
							await this.plugin.server.renameConversation(
								incomingConversationId,
								newTitle,
							);
						} catch (error) {
							console.error(
								"Failed to rename conversation:",
								error,
							);
							this.flashStatusInChatInput(
								"Failed to rename conversation",
							);
							return;
						}

						const newConversationSessionTitleEl =
							conversationSessionEl.createDiv(
								"conversation-session-title",
							);
						newConversationSessionTitleEl.textContent = newTitle;
						newConversationSessionTitleEl.addEventListener(
							"click",
							() => {
								chatBodyEl.innerHTML = "";
								chatBodyEl.dataset.conversationId =
									incomingConversationId;
								chatBodyEl.dataset.conversationTitle = newTitle;
								this.selectConversation(incomingConversationId);
								this.getChatHistory(chatBodyEl);
							},
						);

						let newConversationMenuEl =
							this.contentEl.createEl("div");
						newConversationMenuEl = this.addConversationMenu(
							newConversationMenuEl,
							conversationSessionEl,
							newTitle,
							newConversationSessionTitleEl,
							chatBodyEl,
							incomingConversationId,
							selectedConversation,
						);

						conversationMenuEl.replaceWith(newConversationMenuEl);
						editConversationTitleInputEl.replaceWith(
							newConversationSessionTitleEl,
						);
					}
				},
			);
			conversationMenuEl.appendChild(editConversationTitleSaveButtonEl);
		});

		conversationMenuEl.appendChild(editConversationTitleButtonEl);

		let deleteConversationButtonEl = this.contentEl.createEl("button");
		setIcon(deleteConversationButtonEl, "trash");
		deleteConversationButtonEl.title = "Delete";
		deleteConversationButtonEl.classList.add(
			"delete-conversation-button",
			"three-dot-menu-button-item",
			"clickable-icon",
		);
		if (selectedConversation)
			deleteConversationButtonEl.classList.add("selected-conversation");
		deleteConversationButtonEl.addEventListener("click", async () => {
			// Ask for confirmation before deleting chat session
			let confirmation = confirm(
				"Are you sure you want to delete this chat session?",
			);
			if (!confirmation) return;

			try {
				await this.plugin.server.deleteConversation(
					incomingConversationId,
				);

				if (
					selectedConversation ||
					chatBodyEl.dataset.conversationId === incomingConversationId
				) {
					chatBodyEl.innerHTML = "";
					chatBodyEl.dataset.conversationId = "";
					chatBodyEl.dataset.conversationTitle = "";
					this.selectConversation(null);
				}
				await this.toggleChatSessions(true);
			} catch (error) {
				console.error("Failed to delete conversation:", error);
				this.flashStatusInChatInput("Failed to delete conversation");
			}
		});

		conversationMenuEl.appendChild(deleteConversationButtonEl);
		return conversationMenuEl;
	}

	async getChatHistory(chatBodyEl: HTMLElement): Promise<boolean> {
		try {
			const chatHistory = await this.runtime.loadHistory(
				chatBodyEl.dataset.conversationId,
			);

			// Render conversation history, if any
			chatBodyEl.dataset.conversationId = chatHistory.conversation_id;
			chatBodyEl.dataset.conversationTitle =
				chatHistory.slug || `New conversation 🌱`;

			chatHistory.chat.forEach((chatLog) => {
				this.renderMessageWithReferences(
					chatBodyEl,
					chatLog.message,
					chatLog.by,
					chatLog.turnId ?? "",
					chatLog.context,
					chatLog.onlineContext,
					chatLog.created ? new Date(chatLog.created) : undefined,
					chatLog.by === "you" ? chatLog.images : undefined,
				);
			});

			// Update starting message after loading history
			this.startingMessage = this.getLearningMoment();

			// Update the placeholder of the chat input
			const chatInput = this.contentEl.querySelector(
				".khoj-chat-input",
			) as HTMLTextAreaElement;
			if (chatInput) {
				chatInput.placeholder = this.startingMessage;
				chatInput.removeAttribute("disabled");
			}
		} catch (err) {
			let errorMsg =
				"Unable to get response from OfferAgent server. Ensure the server is running and the OfferAgent URL is correct.";
			this.renderMessage({
				chatBodyEl,
				message: errorMsg,
				sender: "khoj",
				isSystemMessage: true,
			});
			return false;
		}
		return true;
	}

	async processStreamEvent(event: StreamEvent): Promise<void> {
		if (event.type === "start_llm_response") {
			this.isStreaming = true;
			const chatInput = <HTMLTextAreaElement>(
				this.contentEl.getElementsByClassName("khoj-chat-input")[0]
			);
			if (chatInput) chatInput.style.overflowY = "hidden";
		} else if (event.type === "status" && typeof event.data === "string") {
			this.handleStreamResponse(
				this.chatMessageState.newResponseTextEl,
				event.data,
				this.chatMessageState.loadingEllipsis,
			);
		} else if (event.type === "vault_actions") {
			const actions = parseVaultActions(event.data);
			const conversationId = this.runtime.currentConversationId;
			if (
				this.fileAccessMode !== "write" ||
				!actions ||
				actions.length === 0 ||
				!conversationId
			) {
				const warning =
					"OfferAgent returned invalid or unauthorized vault actions. No files were changed.";
				this.chatMessageState.rawResponse += `\n\n⚠️ ${warning}`;
				this.handleStreamResponse(
					this.chatMessageState.newResponseTextEl,
					this.chatMessageState.rawResponse,
					this.chatMessageState.loadingEllipsis,
				);
			} else {
				if (this.pendingVaultActionConversationId !== conversationId) {
					this.discardPendingVaultActions();
					this.pendingVaultActionConversationId = conversationId;
				}
				this.pendingVaultActions.push(...actions);
			}
		} else if (event.type === "end_llm_response") {
			this.isStreaming = false;
			this.autoResize();
		} else if (event.type === "end_response") {
			this.isStreaming = false;
			this.autoResize();
			this.finalizeChatBodyResponse(
				this.chatMessageState.references,
				this.chatMessageState.newResponseTextEl,
				this.chatMessageState.turnId,
			);
			this.showPendingVaultActions(this.chatMessageState.newResponseEl);

			const liveQuery = this.chatMessageState.rawQuery;
			this.chatMessageState = {
				newResponseTextEl: null,
				newResponseEl: null,
				loadingEllipsis: null,
				references: {},
				rawResponse: "",
				rawQuery: liveQuery,
				turnId: "",
				parentRetryCount: 0,
			};
		} else if (event.type === "references" && isRecord(event.data)) {
			this.chatMessageState.references = {
				notes: Array.isArray(event.data.context)
					? event.data.context
					: [],
				online: isRecord(event.data.onlineContext)
					? event.data.onlineContext
					: {},
			};
		} else if (event.type === "message") {
			const message =
				typeof event.data === "string"
					? event.data
					: isRecord(event.data) &&
						  typeof event.data.response === "string"
						? event.data.response
						: "";
			this.chatMessageState.rawResponse += message;
			this.handleStreamResponse(
				this.chatMessageState.newResponseTextEl,
				this.chatMessageState.rawResponse,
				this.chatMessageState.loadingEllipsis,
			);
		} else if (
			event.type === "metadata" &&
			isRecord(event.data) &&
			typeof event.data.turnId === "string"
		) {
			this.chatMessageState.turnId = event.data.turnId;
		}
	}

	async getChatResponse(
		query: string | undefined | null,
		displayQuery: string | undefined | null,
		displayUserMessage: boolean = true,
	): Promise<void> {
		// Exit if query is empty
		if (!query || query === "") return;
		this.discardPendingVaultActions();

		// Get chat body element
		let chatBodyEl = this.contentEl.getElementsByClassName(
			"khoj-chat-body",
		)[0] as HTMLElement;

		// Render user query as chat message with display version only if displayUserMessage is true
		if (displayUserMessage) {
			this.renderMessage({
				chatBodyEl,
				message: displayQuery || query,
				sender: "you",
			});
		}

		let conversationId = chatBodyEl.dataset.conversationId;

		try {
			if (conversationId) {
				this.selectConversation(conversationId);
			} else {
				conversationId = await this.runtime.createConversation();
				chatBodyEl.dataset.conversationId = conversationId;
			}
		} catch (error) {
			console.error("Error creating session:", error);
			this.flashStatusInChatInput("Failed to create session");
			return;
		}

		// Get open files content if we have access
		const openFilesContent = await this.getOpenFilesContent();

		const finalQuery = query.trim() + openFilesContent;

		const body: Omit<ChatRequest, "conversation_id"> = {
			q: finalQuery,
			n: this.setting.resultsCount,
			stream: true,
			...(!!this.location &&
				this.location.city && { city: this.location.city }),
			...(!!this.location &&
				this.location.region && { region: this.location.region }),
			...(!!this.location &&
				this.location.countryName && {
					country: this.location.countryName,
				}),
			...(!!this.location &&
				this.location.countryCode && {
					country_code: this.location.countryCode,
				}),
			...(!!this.location &&
				this.location.timezone && { timezone: this.location.timezone }),
			client_capabilities: {
				vaultActions: this.fileAccessMode === "write",
			},
		};

		let newResponseEl = this.createKhojResponseDiv();
		let newResponseTextEl = newResponseEl.createDiv();
		newResponseTextEl.classList.add("khoj-chat-message-text", "khoj");

		// Temporary status message to indicate that Khoj is thinking
		let loadingEllipsis = this.createLoadingEllipse();
		newResponseTextEl.appendChild(loadingEllipsis);

		// Set chat message state
		this.chatMessageState = {
			newResponseEl: newResponseEl,
			newResponseTextEl: newResponseTextEl,
			loadingEllipsis: loadingEllipsis,
			references: {},
			rawQuery: query,
			rawResponse: "",
			turnId: "",
		};

		try {
			await this.runtime.send(body, (event) =>
				this.processStreamEvent(event),
			);
		} catch (err) {
			if (err instanceof Error && err.name === "AbortError") return;
			console.error(`OfferAgent chat response failed with\n${err}`);
			let errorMsg =
				"Sorry, unable to get response from OfferAgent backend. Retry after checking the OfferAgent URL and server.";
			newResponseTextEl.textContent = errorMsg;
		}
	}

	flashStatusInChatInput(message: string) {
		// Get chat input element and original placeholder
		let chatInput = <HTMLTextAreaElement>(
			this.contentEl.getElementsByClassName("khoj-chat-input")[0]
		);
		let originalPlaceholder = chatInput.placeholder;
		// Set placeholder to message
		chatInput.placeholder = message;
		// Reset placeholder after 2 seconds
		setTimeout(() => {
			chatInput.placeholder = originalPlaceholder;
		}, 2000);
	}

	async clearConversationHistory() {
		let chatBody = this.contentEl.getElementsByClassName(
			"khoj-chat-body",
		)[0] as HTMLElement;

		try {
			const message = await this.plugin.server.clearChatHistory();
			this.selectConversation(null);
			chatBody.dataset.conversationId = "";
			chatBody.innerHTML = "";
			this.flashStatusInChatInput(message);
		} catch (err) {
			this.flashStatusInChatInput("Failed to clear conversation history");
		}
	}

	sendMessageTimeout: NodeJS.Timeout | undefined;

	cancelSendMessage() {
		// Cancel the auto-send chat message timer if the stop-send-button is clicked
		clearTimeout(this.sendMessageTimeout);
		this.runtime.cancel();

		// Revert to showing send-button and hide the stop-send-button
		let sendButton = <HTMLButtonElement>(
			this.contentEl.getElementsByClassName("khoj-chat-send")[0]
		);
		setIcon(sendButton, "arrow-up-circle");
		let sendImg = <SVGElement>(
			sendButton.getElementsByClassName("lucide-arrow-up-circle")[0]
		);
		sendImg.addEventListener("click", async (_) => {
			await this.chat();
		});
	}

	incrementalChat(event: KeyboardEvent) {
		const chatInput = <HTMLTextAreaElement>(
			this.contentEl.getElementsByClassName("khoj-chat-input")[0]
		);
		const trimmedValue = chatInput.value.trim();

		if (event.key === "Enter" && !event.shiftKey) {
			if (!trimmedValue) {
				event.preventDefault();
				return;
			}

			// Otherwise, send message as normal
			event.preventDefault();
			this.chat();
		}
	}

	onChatInput() {
		const chatInput = <HTMLTextAreaElement>(
			this.contentEl.getElementsByClassName("khoj-chat-input")[0]
		);
		chatInput.value = chatInput.value.trimStart();
		this.autoResize();
	}

	autoResize() {
		const chatInput = <HTMLTextAreaElement>(
			this.contentEl.getElementsByClassName("khoj-chat-input")[0]
		);

		// Skip resizing completely during active streaming to avoid UI jumps
		if (this.isStreaming) {
			return;
		}

		// Reset height to auto to get the correct scrollHeight
		chatInput.style.height = "auto";

		// Calculate new height based on content with a larger maximum height
		const maxHeight = 400;
		const newHeight = Math.min(chatInput.scrollHeight, maxHeight);
		chatInput.style.height = newHeight + "px";
		// Add overflow-y: auto only if content exceeds max height
		if (chatInput.scrollHeight > maxHeight) {
			chatInput.style.overflowY = "auto";
		} else {
			chatInput.style.overflowY = "hidden";
		}

	}

	scrollChatToBottom() {
		const chat_body_el =
			this.contentEl.getElementsByClassName("khoj-chat-body")[0];
		if (!!chat_body_el) chat_body_el.scrollTop = chat_body_el.scrollHeight;
	}

	createLoadingEllipse() {
		// Temporary status message to indicate that Khoj is thinking
		let loadingEllipsis = this.contentEl.createEl("div");
		loadingEllipsis.classList.add("lds-ellipsis");

		let firstEllipsis = this.contentEl.createEl("div");
		firstEllipsis.classList.add("lds-ellipsis-item");

		let secondEllipsis = this.contentEl.createEl("div");
		secondEllipsis.classList.add("lds-ellipsis-item");

		let thirdEllipsis = this.contentEl.createEl("div");
		thirdEllipsis.classList.add("lds-ellipsis-item");

		let fourthEllipsis = this.contentEl.createEl("div");
		fourthEllipsis.classList.add("lds-ellipsis-item");

		loadingEllipsis.appendChild(firstEllipsis);
		loadingEllipsis.appendChild(secondEllipsis);
		loadingEllipsis.appendChild(thirdEllipsis);
		loadingEllipsis.appendChild(fourthEllipsis);

		return loadingEllipsis;
	}

	handleStreamResponse(
		newResponseElement: HTMLElement | null,
		rawResponse: string,
		loadingEllipsis: HTMLElement | null,
	) {
		if (!newResponseElement) return;

		// Remove loading ellipsis if it exists
		if (
			newResponseElement.getElementsByClassName("lds-ellipsis").length >
				0 &&
			loadingEllipsis
		)
			newResponseElement.removeChild(loadingEllipsis);

		// Always replace the content completely
		newResponseElement.innerHTML = "";
		const messageEl = this.formatHTMLMessage(rawResponse);
		messageEl.classList.add("khoj-message-new-content");
		newResponseElement.appendChild(messageEl);

		// Remove the animation class after the animation completes
		setTimeout(() => {
			newResponseElement.classList.remove("khoj-message-new-content");
		}, 300);
	}

	finalizeChatBodyResponse(
		references: object,
		newResponseElement: HTMLElement | null,
		turnId: string,
	) {
		if (
			!!newResponseElement &&
			references != null &&
			Object.keys(references).length > 0
		) {
			newResponseElement.appendChild(
				this.createReferenceSection(references),
			);
		}
		if (!!newResponseElement && turnId) {
			// Set the turnId for the new response and the previous user message
			newResponseElement.parentElement?.setAttribute(
				"data-turnid",
				turnId,
			);
			newResponseElement.parentElement?.previousElementSibling?.setAttribute(
				"data-turnid",
				turnId,
			);
		}
		this.scrollChatToBottom();
		let chatInput =
			this.contentEl.getElementsByClassName("khoj-chat-input")[0];
		if (chatInput) chatInput.removeAttribute("disabled");
	}

	createReferenceSection(references: any) {
		let referenceSection = this.contentEl.createEl("div");
		referenceSection.classList.add("reference-section");
		referenceSection.classList.add("collapsed");

		let numReferences = 0;

		if (references.hasOwnProperty("notes")) {
			numReferences += references["notes"].length;

			references["notes"].forEach((reference: any, index: number) => {
				let polishedReference = this.generateReference(
					referenceSection,
					reference,
					index,
				);
				referenceSection.appendChild(polishedReference);
			});
		}
		if (references.hasOwnProperty("online")) {
			numReferences += this.processOnlineReferences(
				referenceSection,
				references["online"],
			);
		}

		let referenceExpandButton = this.contentEl.createEl("button");
		referenceExpandButton.classList.add("reference-expand-button");
		referenceExpandButton.innerHTML =
			numReferences == 1 ? "1 reference" : `${numReferences} references`;

		referenceExpandButton.addEventListener("click", function () {
			if (referenceSection.classList.contains("collapsed")) {
				referenceSection.classList.remove("collapsed");
				referenceSection.classList.add("expanded");
			} else {
				referenceSection.classList.add("collapsed");
				referenceSection.classList.remove("expanded");
			}
		});

		let referencesDiv = this.contentEl.createEl("div");
		referencesDiv.classList.add("references");
		referencesDiv.appendChild(referenceExpandButton);
		referencesDiv.appendChild(referenceSection);

		return referencesDiv;
	}

	private async deleteTurnFromBackend(turnId: string): Promise<boolean> {
		const chatBodyEl = this.contentEl.getElementsByClassName(
			"khoj-chat-body",
		)[0] as HTMLElement;
		const conversationId = chatBodyEl.dataset.conversationId;

		if (!conversationId) {
			this.flashStatusInChatInput("Failed to delete message");
			return false;
		}

		try {
			await this.plugin.server.deleteTurn(conversationId, turnId);
		} catch (error) {
			console.error("Error deleting message:", error);
			this.flashStatusInChatInput("Error deleting message");
			return false;
		}

		return true;
	}

	async deleteMessage(
		messageEl: HTMLElement,
		skipPaired: boolean = false,
		skipBackend: boolean = false,
	): Promise<boolean> {
		const messageContainer = messageEl.closest(".khoj-chat-message");
		if (!messageContainer) return false;

		let pairedMessageContainer: Element | null = null;
		if (!skipPaired) {
			const messages = Array.from(
				document.getElementsByClassName("khoj-chat-message"),
			);
			const currentIndex = messages.indexOf(
				messageContainer as HTMLElement,
			);

			if (
				messageContainer.classList.contains("you") &&
				currentIndex < messages.length - 1
			) {
				pairedMessageContainer = messages[currentIndex + 1];
			} else if (
				messageContainer.classList.contains("khoj") &&
				currentIndex > 0
			) {
				pairedMessageContainer = messages[currentIndex - 1];
			}
		}

		messageContainer.classList.add("deleting");
		if (pairedMessageContainer) {
			pairedMessageContainer.classList.add("deleting");
		}

		const turnId = messageContainer.getAttribute("data-turnid");
		if (!skipBackend && turnId) {
			if (!(await this.deleteTurnFromBackend(turnId))) {
				messageContainer.classList.remove("deleting");
				pairedMessageContainer?.classList.remove("deleting");
				return false;
			}
		}

		setTimeout(() => {
			messageContainer.remove();
			pairedMessageContainer?.remove();
		}, 300);

		return true;
	}

	private async getOpenFilesContent(): Promise<string> {
		return this.fileInteractions.getOpenFilesContent(this.fileAccessMode);
	}

	async onClose(): Promise<void> {
		this.runtime.cancel();
		await this.cancelPendingVaultActions();
	}

	private selectConversation(conversationId: string | null): void {
		if (conversationId !== this.runtime.currentConversationId)
			this.discardPendingVaultActions();
		this.runtime.selectConversation(conversationId);
	}

	private discardPendingVaultActions(): void {
		this.pendingVaultActions = [];
		this.pendingVaultActionConversationId = null;
		this.pendingVaultActionMessage = null;
		this.pendingVaultActionButtons?.remove();
		this.pendingVaultActionButtons = null;
	}

	private showPendingVaultActions(message: Element | null): void {
		if (!message || this.pendingVaultActions.length === 0) return;

		this.pendingVaultActionButtons?.remove();
		this.pendingVaultActionMessage = message;
		const container = message.createDiv({
			cls: "vault-action-confirmation",
		});
		this.pendingVaultActionButtons = container;

		container.createDiv({
			cls: "vault-action-summary",
			text: `Review ${this.pendingVaultActions.length} local vault change${this.pendingVaultActions.length === 1 ? "" : "s"}`,
		});
		const list = container.createDiv({ cls: "vault-action-list" });
		for (const action of this.pendingVaultActions) {
			const review = vaultActionReview(action);
			const item = list.createEl("details", { cls: "vault-action-item" });
			item.createEl("summary", { text: review.summary });
			item.createEl("pre", {
				cls: "vault-action-payload",
				text: review.details,
			});
		}

		const buttons = container.createDiv({ cls: "vault-action-buttons" });
		const applyButton = buttons.createEl("button", {
			text: "Apply",
			cls: ["vault-action-button", "vault-action-apply"],
		});
		const cancelButton = buttons.createEl("button", {
			text: "Cancel",
			cls: ["vault-action-button", "vault-action-cancel"],
		});
		applyButton.addEventListener(
			"click",
			() => void this.applyPendingVaultActions(),
		);
		cancelButton.addEventListener(
			"click",
			() => void this.cancelPendingVaultActions(),
		);
		container.scrollIntoView({ behavior: "smooth", block: "center" });
	}

	public async applyPendingVaultActions(): Promise<void> {
		const actions = this.pendingVaultActions;
		const message = this.pendingVaultActionMessage;
		const buttons = this.pendingVaultActionButtons;
		if (actions.length === 0 || !message) return;
		if (
			this.pendingVaultActionConversationId !==
			this.runtime.currentConversationId
		) {
			this.discardPendingVaultActions();
			message.createDiv({
				cls: ["vault-action-result", "error"],
				text: "Stale vault changes were discarded because the conversation changed. No files were changed.",
			});
			return;
		}

		this.discardPendingVaultActions();
		buttons
			?.querySelectorAll("button")
			.forEach((button) => button.setAttribute("disabled", "true"));

		const results =
			this.fileAccessMode === "write"
				? await this.fileInteractions.applyVaultActions(actions)
				: actions.map((action) => ({
						action,
						success: false,
						status: "not_applied" as const,
						path: action.path,
						error: "File access is no longer in Read & Write mode.",
					}));

		buttons?.remove();
		const success = results.every((result) => result.success);
		message.createDiv({
			cls: ["vault-action-result", success ? "success" : "error"],
			text: this.formatVaultActionResults(results),
		});
	}

	public async cancelPendingVaultActions(): Promise<void> {
		const count = this.pendingVaultActions.length;
		const message = this.pendingVaultActionMessage;
		this.discardPendingVaultActions();
		if (count > 0 && message) {
			message.createDiv({
				cls: ["vault-action-result", "cancelled"],
				text: `Cancelled ${count} local vault change${count === 1 ? "" : "s"}. No files were changed.`,
			});
		}
	}

	private formatVaultActionResults(results: VaultActionResult[]): string {
		const status = results.some(
			(result) => result.status === "manual_review_required",
		)
			? "Manual review required; some created paths or concurrent edits were preserved"
			: results.every((result) => result.status === "applied")
				? "Applied"
				: results.every((result) => result.status === "rolled_back")
					? "Changes rolled back"
					: "No changes applied";
		const details = results.map((result) =>
			result.status === "applied"
				? `✓ ${result.action.op}: ${result.path} [applied]`
				: `✗ ${result.action.op}: ${result.path} [${result.status}] (${result.error || "unknown error"})`,
		);
		return `${status}:\n${details.join("\n")}`;
	}

}
