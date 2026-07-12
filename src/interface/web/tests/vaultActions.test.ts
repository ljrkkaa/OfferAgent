import assert from "node:assert/strict";
import { describe, test } from "node:test";

import {
    attachVaultActionBatch,
    applyVaultActionBatch,
    parseVaultActionBatch,
    parseVaultActionCapability,
    type VaultActionBatch,
    upsertVaultActionBatch,
    vaultActionBatchForTurn,
    VaultActionApiError,
} from "../app/common/vaultActions";

const validBatch = {
    id: "00000000-0000-0000-0000-000000000001",
    conversation_id: "00000000-0000-0000-0000-000000000002",
    turn_id: "00000000-0000-0000-0000-000000000003",
    status: "pending",
    actions: [
        {
            op: "create_file",
            path: "daily/2026-07-10.md",
            content: "# Daily\n",
            mode: "create_only",
        },
        {
            op: "append_file",
            path: "experiences/index.md",
            content: "- Daily\n",
            heading: "Recent",
            mode: "append",
        },
        {
            op: "replace_text",
            path: "progress.md",
            find: "old",
            replace: "new",
            reason: "refresh progress",
            mode: "replace",
        },
    ],
    previews: [
        {
            op: "create_file",
            path: "daily/2026-07-10.md",
            diff: "--- a/daily/2026-07-10.md\n+++ b/daily/2026-07-10.md\n",
            truncated: false,
        },
    ],
    expires_at: "2026-07-10T12:30:00+00:00",
    result: {},
};

describe("VaultAction protocol", () => {
    test("parses a complete strict batch", () => {
        const parsed = parseVaultActionBatch(validBatch);

        assert.equal(parsed.status, "pending");
        assert.deepEqual(
            parsed.actions.map((action) => action.op),
            ["create_file", "append_file", "replace_text"],
        );
    });

    test("rejects unknown fields and invalid action modes", () => {
        assert.throws(() => parseVaultActionBatch({ ...validBatch, absolute_root: "/secret" }));
        assert.throws(() =>
            parseVaultActionBatch({
                ...validBatch,
                actions: [{ ...validBatch.actions[0], mode: "overwrite" }],
            }),
        );
    });

    test("parses capability response without exposing a filesystem root", () => {
        const parsed = parseVaultActionCapability({
            enabled: true,
            review_required: true,
            allowed_extensions: [".md", ".txt"],
            csrf_token: "a".repeat(43),
        });

        assert.equal(parsed.enabled, true);
        assert.equal(Object.keys(parsed).includes("root"), false);
    });

    test("apply sends CSRF and parses the returned terminal batch", async () => {
        const calls: Array<{ url: string; init?: RequestInit }> = [];
        const fetcher = (async (input: RequestInfo | URL, init?: RequestInit) => {
            calls.push({ url: String(input), init });
            return new Response(JSON.stringify({ ...validBatch, status: "applied" }), {
                status: 200,
                headers: { "Content-Type": "application/json" },
            });
        }) as typeof fetch;

        const result = await applyVaultActionBatch(validBatch.id, "csrf-token", fetcher);

        assert.equal(result.status, "applied");
        assert.equal(calls[0].url, `/api/vault/actions/${validBatch.id}/apply`);
        assert.equal(new Headers(calls[0].init?.headers).get("X-Vault-CSRF"), "csrf-token");
        assert.equal(calls[0].init?.method, "POST");
    });

    test("surfaces server conflict details", async () => {
        const fetcher = (async () =>
            new Response(JSON.stringify({ detail: "File changed after preview." }), {
                status: 409,
                headers: { "Content-Type": "application/json" },
            })) as typeof fetch;

        await assert.rejects(
            applyVaultActionBatch(validBatch.id, "csrf-token", fetcher),
            (error: unknown) =>
                error instanceof VaultActionApiError &&
                error.status === 409 &&
                error.message === "File changed after preview.",
        );
    });

    test("attaches a streamed batch to the current assistant message", () => {
        const message: {
            vaultActionBatch?: VaultActionBatch;
            rawResponse: string;
            trainOfThought: string[];
            context: unknown[];
            onlineContext: Record<string, unknown>;
            completed: boolean;
            rawQuery: string;
            timestamp: string;
        } = {
            rawResponse: "",
            trainOfThought: [],
            context: [],
            onlineContext: {},
            completed: false,
            rawQuery: "写学习计划",
            timestamp: "2026-07-10T12:00:00Z",
        };

        attachVaultActionBatch(message, validBatch);

        assert.equal(message.vaultActionBatch?.id, validBatch.id);
    });

    test("upserts recovered batches and resolves the latest batch for a turn", () => {
        const first = parseVaultActionBatch(validBatch);
        const replaced = parseVaultActionBatch({ ...validBatch, status: "cancelled" });
        const newer = parseVaultActionBatch({
            ...validBatch,
            id: "00000000-0000-0000-0000-000000000004",
        });

        const batches = upsertVaultActionBatch(
            upsertVaultActionBatch(upsertVaultActionBatch([], first), replaced),
            newer,
        );

        assert.equal(batches.length, 2);
        assert.equal(batches[0].status, "cancelled");
        assert.equal(vaultActionBatchForTurn(batches, first.turn_id)?.id, newer.id);
    });
});
