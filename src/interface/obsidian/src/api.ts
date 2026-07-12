export interface UserInfo {
	username?: string;
	photo?: string;
	has_documents?: boolean;
	email?: string;
}

export interface ModelOption {
	id: string;
	name: string;
}

export interface ServerUserConfig {
	selected_chat_model_config?: number;
}

export interface SearchApiResult {
	entry: string;
	additional: { file: string };
}

export interface ConversationSession {
	conversation_id: string;
	slug: string;
}

export interface ChatHistoryLog {
	by: string;
	message: string;
	turnId?: string;
	context?: object[];
	onlineContext?: object;
	images?: string[];
	created?: string | number;
	intent?: {
		type?: string;
		"inferred-queries"?: string[];
	};
}

export interface ChatHistoryResponse {
	conversation_id: string;
	slug?: string;
	agent?: unknown;
	chat: ChatHistoryLog[];
}

export interface ChatRequest {
	q: string;
	n: number;
	stream: true;
	conversation_id: string;
	city?: string;
	region?: string;
	country?: string;
	country_code?: string;
	timezone?: string;
	client_capabilities: { vaultActions: boolean };
}

type QueryValue = string | number | boolean | null | undefined;
type FetchImplementation = typeof fetch;

export class ServerError extends Error {
	constructor(
		message: string,
		readonly status: number,
		readonly responseBody: string,
	) {
		super(message);
		this.name = "ServerError";
	}
}

export class OfferAgentServer {
	private baseUrl: string;
	private apiKey: string;

	constructor(
		baseUrl: string,
		apiKey: string,
		private readonly fetchImplementation: FetchImplementation = fetch,
	) {
		this.baseUrl = "";
		this.apiKey = "";
		this.configure(baseUrl, apiKey);
	}

	configure(baseUrl: string, apiKey: string): void {
		this.baseUrl = baseUrl.trim().replace(/\/+$/, "");
		this.apiKey = apiKey.trim();
	}

	async probe(): Promise<{ connected: boolean; user: UserInfo | null }> {
		try {
			return { connected: true, user: await this.getCurrentUser() };
		} catch {
			return { connected: false, user: null };
		}
	}

	async getCurrentUser(): Promise<UserInfo> {
		return parseUserInfo(await this.getJson("/api/v1/user"));
	}

	async deleteContentByType(contentType: string): Promise<void> {
		await this.send(
			`/api/content/type/${encodeURIComponent(contentType)}`,
			{ method: "DELETE" },
			{ client: "obsidian" },
		);
	}

	async uploadContentBatch(
		files: { blob: Blob; path: string }[],
	): Promise<string> {
		const formData = new FormData();
		files.forEach((file) => formData.append("files", file.blob, file.path));
		return this.getText(
			"/api/content",
			{ method: "PATCH", body: formData },
			{ client: "obsidian" },
		);
	}

	async search(
		query: string,
		limit: number,
		rerank: boolean,
		signal?: AbortSignal,
	): Promise<SearchApiResult[]> {
		const value = await this.getJson(
			"/api/search",
			{ signal },
			{ q: query, n: limit, r: rerank, client: "obsidian" },
		);
		if (!Array.isArray(value) || !value.every(isSearchApiResult)) {
			throw new Error("Invalid search response");
		}
		return value;
	}

	async getChatModels(): Promise<ModelOption[]> {
		const value = await this.getJson("/api/model/chat/options");
		if (!Array.isArray(value) || !value.every(isChatModelResponse)) {
			throw new Error("Invalid chat models response");
		}
		return value.map((model) => ({
			id: model.id.toString(),
			name: model.name,
		}));
	}

	async getUserSettings(): Promise<ServerUserConfig> {
		return parseServerUserConfig(
			await this.getJson("/api/settings", {}, { detailed: true }),
		);
	}

	async updateChatModel(modelId: string): Promise<void> {
		await this.send("/api/model/chat", { method: "POST" }, { id: modelId });
	}

	async createConversation(): Promise<string> {
		const value = await this.getJson(
			"/api/chat/sessions",
			this.jsonRequest("POST", {}),
		);
		if (!isRecord(value) || typeof value.conversation_id !== "string") {
			throw new Error("Invalid session response");
		}
		return value.conversation_id;
	}

	async getConversations(): Promise<ConversationSession[]> {
		const value = await this.getJson(
			"/api/chat/sessions",
			{},
			{ client: "obsidian" },
		);
		if (!Array.isArray(value)) {
			throw new Error("Invalid chat sessions response");
		}
		return value.map(parseConversationSession);
	}

	async renameConversation(
		conversationId: string,
		title: string,
	): Promise<void> {
		await this.send(
			"/api/chat/title",
			{ method: "PATCH" },
			{ client: "obsidian", conversation_id: conversationId, title },
		);
	}

	async deleteConversation(conversationId: string): Promise<void> {
		await this.send(
			"/api/chat/history",
			{ method: "DELETE" },
			{ client: "obsidian", conversation_id: conversationId },
		);
	}

	async getChatHistory(
		conversationId?: string | null,
	): Promise<ChatHistoryResponse> {
		const value = await this.getJson(
			"/api/chat/history",
			{},
			{ client: "obsidian", conversation_id: conversationId },
		);
		return parseChatHistoryResponse(value);
	}

	async clearChatHistory(): Promise<string> {
		const value = await this.getJson(
			"/api/chat/history",
			{ method: "DELETE" },
			{ client: "obsidian" },
		);
		if (
			!isRecord(value) ||
			value.status !== "ok" ||
			typeof value.message !== "string"
		) {
			throw new Error("Invalid clear history response");
		}
		return value.message;
	}

	async streamChat(
		body: ChatRequest,
		signal: AbortSignal,
	): Promise<Response> {
		return this.send("/api/chat", this.jsonRequest("POST", body, signal), {
			client: "obsidian",
		});
	}

	async deleteTurn(conversationId: string, turnId: string): Promise<void> {
		await this.send(
			"/api/chat/conversation/message",
			this.jsonRequest("DELETE", {
				conversation_id: conversationId,
				turn_id: turnId,
			}),
		);
	}

	private jsonRequest(
		method: string,
		body: unknown,
		signal?: AbortSignal,
	): RequestInit {
		return {
			method,
			body: JSON.stringify(body),
			headers: { "Content-Type": "application/json" },
			signal,
		};
	}

	private async getJson(
		path: string,
		init: RequestInit = {},
		query: Record<string, QueryValue> = {},
	): Promise<unknown> {
		const response = await this.send(path, init, query);
		try {
			return await response.json();
		} catch {
			throw new Error(`OfferAgent returned invalid JSON for ${path}`);
		}
	}

	private async getText(
		path: string,
		init: RequestInit = {},
		query: Record<string, QueryValue> = {},
	): Promise<string> {
		return (await this.send(path, init, query)).text();
	}

	private async send(
		path: string,
		init: RequestInit = {},
		query: Record<string, QueryValue> = {},
	): Promise<Response> {
		if (!this.baseUrl) throw new Error("OfferAgent URL is required");
		const url = new URL(path, `${this.baseUrl}/`);
		for (const [key, value] of Object.entries(query)) {
			if (value !== undefined && value !== null)
				url.searchParams.set(key, String(value));
		}

		const headers = new Headers(init.headers ?? {});
		if (this.apiKey) headers.set("Authorization", `Bearer ${this.apiKey}`);
		const response = await this.fetchImplementation(url.toString(), {
			...init,
			headers,
		});
		if (!response.ok) {
			const responseBody = await response.text().catch(() => "");
			throw new ServerError(
				`OfferAgent request failed: ${init.method ?? "GET"} ${url.pathname} (${response.status})`,
				response.status,
				responseBody,
			);
		}
		return response;
	}
}

function isRecord(value: unknown): value is Record<string, unknown> {
	return typeof value === "object" && value !== null;
}

function isStringArray(value: unknown): value is string[] {
	return (
		Array.isArray(value) && value.every((item) => typeof item === "string")
	);
}

function parseUserInfo(value: unknown): UserInfo {
	if (!isRecord(value)) throw new Error("Invalid user response");
	for (const key of ["username", "photo", "email"] as const) {
		if (
			value[key] !== undefined &&
			value[key] !== null &&
			typeof value[key] !== "string"
		) {
			throw new Error("Invalid user response");
		}
	}
	if (
		value.has_documents !== undefined &&
		typeof value.has_documents !== "boolean"
	) {
		throw new Error("Invalid user response");
	}
	return {
		username:
			typeof value.username === "string" ? value.username : undefined,
		photo: typeof value.photo === "string" ? value.photo : undefined,
		email: typeof value.email === "string" ? value.email : undefined,
		has_documents:
			typeof value.has_documents === "boolean"
				? value.has_documents
				: undefined,
	};
}

function isSearchApiResult(value: unknown): value is SearchApiResult {
	return (
		isRecord(value) &&
		typeof value.entry === "string" &&
		isRecord(value.additional) &&
		typeof value.additional.file === "string"
	);
}

function isChatModelResponse(
	value: unknown,
): value is { id: string | number; name: string } {
	return (
		isRecord(value) &&
		(typeof value.id === "string" || typeof value.id === "number") &&
		typeof value.name === "string"
	);
}

function parseServerUserConfig(value: unknown): ServerUserConfig {
	if (!isRecord(value)) throw new Error("Invalid server settings response");
	const selectedModel = value.selected_chat_model_config;
	if (selectedModel === undefined || selectedModel === null) return {};
	if (typeof selectedModel !== "number")
		throw new Error("Invalid server settings response");
	return { selected_chat_model_config: selectedModel };
}

function parseConversationSession(value: unknown): ConversationSession {
	if (!isRecord(value) || typeof value.conversation_id !== "string") {
		throw new Error("Invalid chat sessions response");
	}
	if (
		value.slug !== undefined &&
		value.slug !== null &&
		typeof value.slug !== "string"
	) {
		throw new Error("Invalid chat sessions response");
	}
	return {
		conversation_id: value.conversation_id,
		slug: typeof value.slug === "string" ? value.slug : "",
	};
}

function isChatHistoryLog(value: unknown): value is ChatHistoryLog {
	if (
		!isRecord(value) ||
		typeof value.by !== "string" ||
		typeof value.message !== "string"
	)
		return false;
	if (value.context !== undefined && !Array.isArray(value.context))
		return false;
	if (value.images !== undefined && !isStringArray(value.images))
		return false;
	if (value.intent !== undefined && !isRecord(value.intent)) return false;
	if (
		value.intent &&
		value.intent["inferred-queries"] !== undefined &&
		!isStringArray(value.intent["inferred-queries"])
	)
		return false;
	return true;
}

function parseChatHistoryResponse(value: unknown): ChatHistoryResponse {
	if (
		!isRecord(value) ||
		value.status !== "ok" ||
		!isRecord(value.response)
	) {
		throw new Error("Invalid chat history response");
	}
	const response = value.response;
	if (typeof response.conversation_id !== "string")
		throw new Error("Invalid chat history response");
	const chat = response.chat ?? [];
	if (!Array.isArray(chat) || !chat.every(isChatHistoryLog))
		throw new Error("Invalid chat history response");
	return {
		conversation_id: response.conversation_id,
		slug: typeof response.slug === "string" ? response.slug : undefined,
		agent: response.agent,
		chat,
	};
}
