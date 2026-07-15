import assert from "node:assert/strict";
import test from "node:test";

import { generateConversationTitle } from "../packages/protocol/dist/index.js";

test("Conversation titles are local, useful, bounded, and image-aware", () => {
  assert.equal(
    generateConversationTitle({ text: "请帮我分析一下：如何设计 Agent 记忆？后续补充" }),
    "如何设计 Agent 记忆",
  );
  assert.equal(
    generateConversationTitle({ text: "```text\nPlease help me explain event loop behavior.\n```" }),
    "Explain event loop behavior",
  );
  assert.equal(
    generateConversationTitle({ text: "https://example.com/interview\nAnalyze the backend interview" }),
    "Analyze the backend interview",
  );
  assert.equal(
    generateConversationTitle({ text: "https://example.com/interview" }),
    "https://example.com/interview",
  );
  assert.equal(
    generateConversationTitle({ text: "A".repeat(80) }),
    "A".repeat(56),
  );
  assert.equal(
    generateConversationTitle({ text: "", imageFileName: "backend-round.png" }),
    "backend-round",
  );
  assert.equal(
    generateConversationTitle({ text: "", date: new Date("2026-07-15T00:00:00Z") }),
    "图片分析 · 7月15日",
  );
  assert.equal(
    generateConversationTitle({ text: "请帮我分析一下" }),
    "请帮我分析一下",
    "a text-only prompt must not fall back to an image title",
  );
});
