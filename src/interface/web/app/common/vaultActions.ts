import { z } from "zod";

const createFileActionSchema = z
    .object({
        op: z.literal("create_file"),
        path: z.string().min(1),
        content: z.string(),
        mode: z.literal("create_only"),
    })
    .strict();

const appendFileActionSchema = z
    .object({
        op: z.literal("append_file"),
        path: z.string().min(1),
        content: z.string(),
        heading: z.string().nullable().optional(),
        mode: z.literal("append"),
    })
    .strict();

const replaceTextActionSchema = z
    .object({
        op: z.literal("replace_text"),
        path: z.string().min(1),
        find: z.string().min(1),
        replace: z.string(),
        mode: z.literal("replace"),
        reason: z.string().nullable().optional(),
    })
    .strict();

export const vaultActionSchema = z.discriminatedUnion("op", [
    createFileActionSchema,
    appendFileActionSchema,
    replaceTextActionSchema,
]);

export const vaultActionStatusSchema = z.enum([
    "pending",
    "applying",
    "applied",
    "cancelled",
    "conflict",
    "failed",
    "expired",
    "manual_review_required",
]);

const vaultActionPreviewSchema = z
    .object({
        op: z.enum(["create_file", "append_file", "replace_text"]),
        path: z.string().min(1),
        diff: z.string(),
        truncated: z.boolean(),
    })
    .strict();

export const vaultActionBatchSchema = z
    .object({
        id: z.string().uuid(),
        conversation_id: z.string().uuid(),
        turn_id: z.string().uuid(),
        status: vaultActionStatusSchema,
        actions: z.array(vaultActionSchema).min(1).max(20),
        previews: z.array(vaultActionPreviewSchema).min(1).max(20),
        expires_at: z.string().datetime({ offset: true }),
        result: z.record(z.unknown()),
    })
    .strict();

export const vaultActionCapabilitySchema = z
    .object({
        enabled: z.boolean(),
        review_required: z.literal(true),
        allowed_extensions: z.array(z.enum([".md", ".txt"])),
        csrf_token: z.string().min(32),
    })
    .strict();

const vaultActionBatchListSchema = z.array(vaultActionBatchSchema);

export type VaultAction = z.infer<typeof vaultActionSchema>;
export type VaultActionStatus = z.infer<typeof vaultActionStatusSchema>;
export type VaultActionBatch = z.infer<typeof vaultActionBatchSchema>;
export type VaultActionCapability = z.infer<typeof vaultActionCapabilitySchema>;

export class VaultActionApiError extends Error {
    constructor(
        public readonly status: number,
        message: string,
    ) {
        super(message);
        this.name = "VaultActionApiError";
    }
}

export function parseVaultActionBatch(value: unknown): VaultActionBatch {
    return vaultActionBatchSchema.parse(value);
}

export function parseVaultActionCapability(value: unknown): VaultActionCapability {
    return vaultActionCapabilitySchema.parse(value);
}

export function attachVaultActionBatch(
    target: { vaultActionBatch?: VaultActionBatch },
    value: unknown,
): VaultActionBatch {
    const batch = parseVaultActionBatch(value);
    target.vaultActionBatch = batch;
    return batch;
}

export function upsertVaultActionBatch(
    batches: VaultActionBatch[],
    batch: VaultActionBatch,
): VaultActionBatch[] {
    const index = batches.findIndex((candidate) => candidate.id === batch.id);
    if (index === -1) return [...batches, batch];
    return batches.map((candidate, candidateIndex) =>
        candidateIndex === index ? batch : candidate,
    );
}

export function vaultActionBatchForTurn(
    batches: VaultActionBatch[],
    turnId?: string,
): VaultActionBatch | undefined {
    if (!turnId) return undefined;
    for (let index = batches.length - 1; index >= 0; index -= 1) {
        if (batches[index].turn_id === turnId) return batches[index];
    }
    return undefined;
}

async function readJson(response: Response): Promise<unknown> {
    try {
        return await response.json();
    } catch {
        throw new VaultActionApiError(response.status, "Invalid VaultAction API response.");
    }
}

async function requireOk(response: Response): Promise<unknown> {
    const payload = await readJson(response);
    if (!response.ok) {
        const detail =
            typeof payload === "object" &&
            payload !== null &&
            "detail" in payload &&
            typeof payload.detail === "string"
                ? payload.detail
                : `VaultAction request failed (${response.status}).`;
        throw new VaultActionApiError(response.status, detail);
    }
    return payload;
}

export async function fetchVaultActionCapability(
    fetcher: typeof fetch = fetch,
): Promise<VaultActionCapability> {
    const response = await fetcher("/api/vault/actions/capabilities", {
        credentials: "same-origin",
        headers: { Accept: "application/json" },
    });
    return parseVaultActionCapability(await requireOk(response));
}

export async function listVaultActionBatches(
    conversationId: string,
    fetcher: typeof fetch = fetch,
): Promise<VaultActionBatch[]> {
    const response = await fetcher(
        `/api/vault/actions?conversation_id=${encodeURIComponent(conversationId)}`,
        {
            credentials: "same-origin",
            headers: { Accept: "application/json" },
        },
    );
    return vaultActionBatchListSchema.parse(await requireOk(response));
}

async function mutateVaultActionBatch(
    batchId: string,
    operation: "apply" | "cancel",
    csrfToken: string,
    fetcher: typeof fetch,
): Promise<VaultActionBatch> {
    const response = await fetcher(
        `/api/vault/actions/${encodeURIComponent(batchId)}/${operation}`,
        {
            method: "POST",
            credentials: "same-origin",
            headers: {
                Accept: "application/json",
                "X-Vault-CSRF": csrfToken,
            },
        },
    );
    return parseVaultActionBatch(await requireOk(response));
}

export async function applyVaultActionBatch(
    batchId: string,
    csrfToken: string,
    fetcher: typeof fetch = fetch,
): Promise<VaultActionBatch> {
    return mutateVaultActionBatch(batchId, "apply", csrfToken, fetcher);
}

export async function cancelVaultActionBatch(
    batchId: string,
    csrfToken: string,
    fetcher: typeof fetch = fetch,
): Promise<VaultActionBatch> {
    return mutateVaultActionBatch(batchId, "cancel", csrfToken, fetcher);
}
