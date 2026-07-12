const assert = require("node:assert/strict");
const path = require("node:path");
const test = require("node:test");
const { buildSync } = require("esbuild");

function loadModule(entry) {
	const output = buildSync({
		entryPoints: [path.join(__dirname, `../src/${entry}`)],
		bundle: true,
		format: "cjs",
		platform: "node",
		write: false,
	}).outputFiles[0].text;
	const compiled = { exports: {} };
	new Function("require", "module", "exports", output)(
		require,
		compiled,
		compiled.exports,
	);
	return compiled.exports;
}

test("server adapter owns URL normalization and optional authentication", async () => {
	const { OfferAgentServer } = loadModule("api.ts");
	const requests = [];
	const fakeFetch = async (url, init) => {
		requests.push({ url, init });
		return new Response(JSON.stringify({ email: "default@example.com" }), {
			status: 200,
			headers: { "Content-Type": "application/json" },
		});
	};
	const server = new OfferAgentServer(
		"http://127.0.0.1:42110///",
		"",
		fakeFetch,
	);

	await server.getCurrentUser();
	server.configure("http://127.0.0.1:42110/", "secret-token");
	await server.getCurrentUser();

	assert.equal(requests[0].url, "http://127.0.0.1:42110/api/v1/user");
	assert.equal(requests[0].init.headers.get("Authorization"), null);
	assert.equal(
		requests[1].init.headers.get("Authorization"),
		"Bearer secret-token",
	);
});

test("server adapter reports HTTP failures with status and response body", async () => {
	const { OfferAgentServer, ServerError } = loadModule("api.ts");
	const server = new OfferAgentServer(
		"http://localhost:42110",
		"token",
		async () => new Response("denied", { status: 403 }),
	);

	await assert.rejects(
		server.getCurrentUser(),
		(error) =>
			error instanceof ServerError &&
			error.status === 403 &&
			error.responseBody === "denied",
	);
});

test("search response validation lives at the server boundary", async () => {
	const { OfferAgentServer } = loadModule("api.ts");
	const urls = [];
	const server = new OfferAgentServer(
		"http://localhost:42110",
		"",
		async (url) => {
			urls.push(url);
			return new Response(
				JSON.stringify([
					{ entry: "Redis", additional: { file: "notes.md" } },
				]),
				{
					status: 200,
					headers: { "Content-Type": "application/json" },
				},
			);
		},
	);

	const results = await server.search("redis ttl", 7, true);

	assert.equal(results[0].additional.file, "notes.md");
	const url = new URL(urls[0]);
	assert.equal(url.searchParams.get("q"), "redis ttl");
	assert.equal(url.searchParams.get("n"), "7");
	assert.equal(url.searchParams.get("r"), "true");
	assert.equal(url.searchParams.get("client"), "obsidian");
});

test("server normalizes a new conversation without a title", async () => {
	const { OfferAgentServer } = loadModule("api.ts");
	const server = new OfferAgentServer(
		"http://localhost:42110",
		"",
		async () =>
			new Response(
				JSON.stringify([
					{ conversation_id: "conversation-1", slug: null },
				]),
				{
					status: 200,
					headers: { "Content-Type": "application/json" },
				},
			),
	);

	const conversations = await server.getConversations();

	assert.deepEqual(conversations, [
		{ conversation_id: "conversation-1", slug: "" },
	]);
});

test("chat history preserves user image attachments", async () => {
	const { OfferAgentServer } = loadModule("api.ts");
	const image = "data:image/webp;base64,aW1hZ2U=";
	const server = new OfferAgentServer(
		"http://localhost:42110",
		"",
		async () =>
			new Response(
				JSON.stringify({
					status: "ok",
					response: {
						conversation_id: "conversation-1",
						chat: [{ by: "you", message: "look", images: [image] }],
					},
				}),
				{
					status: 200,
					headers: { "Content-Type": "application/json" },
				},
			),
	);

	const history = await server.getChatHistory("conversation-1");

	assert.deepEqual(history.chat[0].images, [image]);
});

test("stream frames use an explicit event envelope", () => {
	const { parseStreamFrame } = loadModule("chat_runtime.ts");

	assert.throws(
		() => parseStreamFrame("plain text"),
		/Invalid OfferAgent stream event/,
	);
	assert.throws(
		() => parseStreamFrame('{"answer":42}'),
		/Invalid OfferAgent stream event/,
	);
	assert.deepEqual(
		parseStreamFrame(
			'{"type":"message","data":"{\\"type\\":\\"invoice\\"}"}',
		),
		{
			type: "message",
			data: '{"type":"invoice"}',
		},
	);
	assert.deepEqual(parseStreamFrame('{"type":"status","data":"thinking"}'), {
		type: "status",
		data: "thinking",
	});
	assert.throws(
		() =>
			parseStreamFrame(
				'{"type":"status","data":"thinking","extra":true}',
			),
		/Invalid OfferAgent stream event/,
	);
	assert.throws(
		() => parseStreamFrame('{"type":"legacy_event","data":{}}'),
		/Invalid OfferAgent stream event/,
	);
	assert.throws(
		() => parseStreamFrame('{"type":"status","data":{}}'),
		/Invalid status event/,
	);
});

test("runtime decodes events split across network chunks", async () => {
	const { readStreamEvents, STREAM_EVENT_DELIMITER } =
		loadModule("chat_runtime.ts");
	const encoder = new TextEncoder();
	const payload = [
		'{"type":"metadata","data":{"turnId":"turn-1"}}',
		'{"type":"message","data":"hello "}',
		'{"type":"message","data":"world"}',
		'{"type":"end_response","data":""}',
	].join(STREAM_EVENT_DELIMITER);
	const chunks = [
		payload.slice(0, 17),
		payload.slice(17, 61),
		payload.slice(61),
	];
	const response = new Response(
		new ReadableStream({
			start(controller) {
				chunks.forEach((chunk) =>
					controller.enqueue(encoder.encode(chunk)),
				);
				controller.close();
			},
		}),
	);
	const events = [];

	await readStreamEvents(response, (event) => events.push(event));

	assert.deepEqual(events, [
		{ type: "metadata", data: { turnId: "turn-1" } },
		{ type: "message", data: "hello " },
		{ type: "message", data: "world" },
		{ type: "end_response", data: "" },
	]);
});

test("runtime cancellation aborts the active server request", async () => {
	const { ChatRuntime } = loadModule("chat_runtime.ts");
	let capturedSignal;
	const server = {
		async streamChat(_body, signal) {
			capturedSignal = signal;
			return new Promise((_resolve, reject) => {
				signal.addEventListener("abort", () =>
					reject(new DOMException("cancelled", "AbortError")),
				);
			});
		},
	};
	const runtime = new ChatRuntime(server);
	runtime.selectConversation("conversation-1");
	const pending = runtime.send(
		{
			q: "hello",
			n: 5,
			stream: true,
			client_capabilities: { vaultActions: false },
		},
		() => {},
	);

	await Promise.resolve();
	runtime.cancel();

	await assert.rejects(pending, (error) => error.name === "AbortError");
	assert.equal(capturedSignal.aborted, true);
});

test("switching conversations aborts the old stream and drops stale events", async () => {
	const { ChatRuntime, STREAM_EVENT_DELIMITER } =
		loadModule("chat_runtime.ts");
	const encoder = new TextEncoder();
	let streamController;
	let capturedSignal;
	const server = {
		async streamChat(_body, signal) {
			capturedSignal = signal;
			return new Response(
				new ReadableStream({
					start(controller) {
						streamController = controller;
					},
				}),
			);
		},
	};
	const runtime = new ChatRuntime(server);
	const events = [];
	runtime.selectConversation("conversation-1");
	const pending = runtime.send(
		{
			q: "hello",
			n: 5,
			stream: true,
			client_capabilities: { vaultActions: true },
		},
		(event) => events.push(event),
	);

	await Promise.resolve();
	runtime.selectConversation("conversation-2");
	streamController.enqueue(
		encoder.encode(
			`{"type":"message","data":"stale"}${STREAM_EVENT_DELIMITER}`,
		),
	);
	streamController.close();
	await pending;

	assert.equal(capturedSignal.aborted, true);
	assert.deepEqual(events, []);
	assert.equal(runtime.currentConversationId, "conversation-2");
});

test("out-of-order conversation creation cannot replace the latest selection", async () => {
	const { ChatRuntime } = loadModule("chat_runtime.ts");
	const resolvers = [];
	const server = {
		createConversation() {
			return new Promise((resolve) => resolvers.push(resolve));
		},
	};
	const runtime = new ChatRuntime(server);

	const first = runtime.createConversation();
	const second = runtime.createConversation();
	resolvers[1]("conversation-2");
	assert.equal(await second, "conversation-2");
	resolvers[0]("conversation-1");

	await assert.rejects(first, /Conversation selection changed/);
	assert.equal(runtime.currentConversationId, "conversation-2");
});
