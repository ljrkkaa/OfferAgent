import assert from "node:assert/strict";
import { mkdtemp, rm, writeFile } from "node:fs/promises";
import os from "node:os";
import path from "node:path";
import test from "node:test";
import { fileURLToPath, pathToFileURL } from "node:url";

const repositoryRoot = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..");
const modulePath = path.join(
  repositoryRoot,
  "packages",
  "runtime",
  "dist",
  "codex-subscription-provider.js",
);

test("the Codex Provider sends ordered Responses image content without OCR", async (t) => {
  const temporaryDirectory = await mkdtemp(path.join(os.tmpdir(), "offeragent-codex-image-"));
  t.after(() => rm(temporaryDirectory, { recursive: true, force: true }));
  const authPath = path.join(temporaryDirectory, "auth.json");
  await writeFile(
    authPath,
    JSON.stringify({ tokens: { access_token: "test-token", account_id: "test-account" } }),
    "utf8",
  );

  const requests = [];
  const fakeFetch = async (input, init) => {
    requests.push({ input: String(input), init, body: JSON.parse(init.body) });
    return new Response(
      'data: {"type":"response.output_text.delta","delta":"image understood"}\n\n' +
        'data: {"type":"response.completed","response":{"status":"completed"}}\n\n' +
        "data: [DONE]\n\n",
      { status: 200, headers: { "content-type": "text/event-stream" } },
    );
  };
  const { CodexSubscriptionProvider } = await import(pathToFileURL(modulePath));
  const provider = new CodexSubscriptionProvider({
    authPath,
    baseUrl: "https://codex.test",
    fetch: fakeFetch,
  });
  const attachmentId = "attachment-opaque-id";
  const dataUrl = "data:image/png;base64,iVBORw0KGgo=";
  const historicalDataUrl = "data:image/png;base64,aGlzdG9yaWNhbA==";
  const events = [];
  for await (const event of provider.stream({
    model: "vision-model",
    signal: new AbortController().signal,
    instructions: "Understand the supplied image directly. Do not use OCR.",
    input: [
      {
        type: "user_message",
        text: "Earlier image question",
        attachments: [{
          attachmentId: "historical-cleaned-attachment",
          fileName: "old.png",
          mediaType: "image/png",
          order: 0,
          size: 10,
        }],
      },
      {
        type: "assistant_message",
        text: "Earlier image answer",
      },
      {
        type: "user_message",
        text: "What is visible?",
        attachments: [{
          attachmentId,
          fileName: "screen.png",
          mediaType: "image/png",
          order: 0,
          size: 12,
        }],
      },
    ],
    imageInputs: [
      {
        attachmentId: "historical-cleaned-attachment",
        dataUrl: historicalDataUrl,
        mediaType: "image/png",
        order: 0,
      },
      { attachmentId, dataUrl, mediaType: "image/png", order: 0 },
    ],
    imageSubmission: {
      imageCount: 1,
      sourceFingerprint: `sha256:${"a".repeat(64)}`,
    },
    tools: [],
  })) {
    events.push(event);
  }

  assert.equal(requests.length, 1);
  assert.equal(requests[0].input, "https://codex.test/responses");
  assert.deepEqual(requests[0].body.input, [
    {
      role: "user",
      content: [
        { type: "input_text", text: "Earlier image question" },
        { type: "input_image", image_url: historicalDataUrl },
      ],
    },
    { role: "assistant", content: [{ type: "output_text", text: "Earlier image answer" }] },
    {
      role: "user",
      content: [
        { type: "input_text", text: "What is visible?" },
        { type: "input_image", image_url: dataUrl },
      ],
    },
  ]);
  assert.equal(requests[0].init.body.includes(attachmentId), false);
  assert.match(requests[0].body.instructions, /Runtime-verified Interview Submission metadata/);
  assert.match(requests[0].body.instructions, /ordered image count: 1/);
  assert.match(requests[0].body.instructions, new RegExp(`source fingerprint: sha256:${"a".repeat(64)}`));
  assert.match(requests[0].body.instructions, /pass this exact fingerprint to interview_catalog/);
  assert.deepEqual(requests[0].body.tools, []);
  assert.deepEqual(events, [{ type: "output_text.delta", delta: "image understood" }]);
});

test("the Codex Provider classifies HTTP and streamed image rejection as unsupported", async (t) => {
  const temporaryDirectory = await mkdtemp(path.join(os.tmpdir(), "offeragent-codex-no-vision-"));
  t.after(() => rm(temporaryDirectory, { recursive: true, force: true }));
  const authPath = path.join(temporaryDirectory, "auth.json");
  await writeFile(
    authPath,
    JSON.stringify({ tokens: { access_token: "test-token", account_id: "test-account" } }),
    "utf8",
  );
  const { CodexSubscriptionProvider } = await import(pathToFileURL(modulePath));
  const responseFactories = [
    () => new Response('{"error":{"message":"Unsupported parameter: input_image"}}', {
        status: 400,
        headers: { "content-type": "application/json" },
      }),
    () => new Response(
        'data: {"type":"response.failed","response":{"error":{"message":"input_image is not supported"}}}\n\n',
        { status: 200, headers: { "content-type": "text/event-stream" } },
      ),
  ];
  for (const responseFactory of responseFactories) {
    const provider = new CodexSubscriptionProvider({
      authPath,
      baseUrl: "https://codex.test",
      fetch: async () => responseFactory(),
    });
    await assert.rejects(
      async () => {
        for await (const _event of provider.stream({
          model: "text-only-model",
          signal: new AbortController().signal,
          instructions: "Describe the image.",
          input: [{
            type: "user_message",
            text: "Describe it.",
            attachments: [{
              attachmentId: "attachment-a",
              fileName: "screen.png",
              mediaType: "image/png",
              order: 0,
              size: 12,
            }],
          }],
          imageInputs: [{
            attachmentId: "attachment-a",
            dataUrl: "data:image/png;base64,iVBORw0KGgo=",
            mediaType: "image/png",
            order: 0,
          }],
          tools: [],
        })) { /* consume */ }
      },
      (error) => error?.code === "unsupported_capability" && /vision|image/i.test(error.message),
    );
  }

  const invalidContentProvider = new CodexSubscriptionProvider({
    authPath,
    baseUrl: "https://codex.test",
    fetch: async () => new Response('{"error":{"message":"Invalid image input: invalid PNG data"}}', {
      status: 400,
      headers: { "content-type": "application/json" },
    }),
  });
  await assert.rejects(
    async () => {
      for await (const _event of invalidContentProvider.stream({
        model: "vision-model",
        signal: new AbortController().signal,
        instructions: "Describe the image.",
        input: [{
          type: "user_message",
          text: "Describe it.",
          attachments: [{
            attachmentId: "invalid-content-attachment",
            fileName: "broken.png",
            mediaType: "image/png",
            order: 0,
            size: 12,
          }],
        }],
        imageInputs: [{
          attachmentId: "invalid-content-attachment",
          dataUrl: "data:image/png;base64,broken",
          mediaType: "image/png",
          order: 0,
        }],
        tools: [],
      })) { /* consume */ }
    },
    (error) => error?.code === "provider_error" && error?.capability === undefined,
  );
});
