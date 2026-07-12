"use client";

import { useEffect, useState } from "react";

import {
    applyVaultActionBatch,
    cancelVaultActionBatch,
    listVaultActionBatches,
    type VaultAction,
    type VaultActionBatch,
    type VaultActionCapability,
    type VaultActionStatus,
} from "@/app/common/vaultActions";
import { Button } from "@/components/ui/button";

interface VaultActionReviewProps {
    batch: VaultActionBatch;
    capability?: VaultActionCapability | null;
    onBatchChange?: (batch: VaultActionBatch) => void;
}

const STATUS_LABELS: Record<VaultActionStatus, string> = {
    pending: "等待确认",
    applying: "正在写入",
    applied: "已写入",
    cancelled: "已取消",
    conflict: "文件已变化，未写入",
    failed: "写入失败，已回滚",
    expired: "确认已过期",
    manual_review_required: "需要人工检查文件",
};

function actionLabel(action: VaultAction): string {
    if (action.op === "create_file") return "新建文件";
    if (action.op === "append_file")
        return action.heading ? `追加到「${action.heading}」` : "追加内容";
    return "精确替换";
}

function resultError(batch: VaultActionBatch): string | null {
    return typeof batch.result.error === "string" ? batch.result.error : null;
}

function resultFiles(batch: VaultActionBatch): string[] {
    return Array.isArray(batch.result.files)
        ? batch.result.files.filter((file): file is string => typeof file === "string")
        : [];
}

function resultRecoveryArtifacts(batch: VaultActionBatch): string[] {
    return Array.isArray(batch.result.recovery_artifacts)
        ? batch.result.recovery_artifacts.filter((file): file is string => typeof file === "string")
        : [];
}

export default function VaultActionReview({
    batch,
    capability,
    onBatchChange,
}: VaultActionReviewProps) {
    const [operation, setOperation] = useState<"apply" | "cancel" | null>(null);
    const [error, setError] = useState<string | null>(null);

    useEffect(() => {
        if (batch.status !== "pending") return;
        const expiresAt = Date.parse(batch.expires_at);
        if (!Number.isFinite(expiresAt)) return;
        const delay = Math.max(0, expiresAt - Date.now() + 250);
        const timer = window.setTimeout(
            async () => {
                try {
                    const batches = await listVaultActionBatches(batch.conversation_id);
                    const refreshed = batches.find((candidate) => candidate.id === batch.id);
                    if (refreshed) onBatchChange?.(refreshed);
                } catch (refreshError) {
                    console.error("Failed to refresh expired VaultAction batch", refreshError);
                }
            },
            Math.min(delay, 2_147_483_647),
        );
        return () => window.clearTimeout(timer);
    }, [batch.conversation_id, batch.expires_at, batch.id, batch.status, onBatchChange]);

    useEffect(() => {
        if (batch.status !== "applying") return;
        let inFlight = false;
        const refresh = async () => {
            if (inFlight) return;
            inFlight = true;
            try {
                const batches = await listVaultActionBatches(batch.conversation_id);
                const refreshed = batches.find((candidate) => candidate.id === batch.id);
                if (refreshed) onBatchChange?.(refreshed);
            } catch (refreshError) {
                console.error("Failed to recover applying VaultAction batch", refreshError);
            } finally {
                inFlight = false;
            }
        };
        void refresh();
        const timer = window.setInterval(refresh, 2_000);
        return () => window.clearInterval(timer);
    }, [batch.conversation_id, batch.id, batch.status, onBatchChange]);

    async function refreshBatchAfterError() {
        try {
            const batches = await listVaultActionBatches(batch.conversation_id);
            const refreshed = batches.find((candidate) => candidate.id === batch.id);
            if (refreshed) onBatchChange?.(refreshed);
        } catch (refreshError) {
            console.error("Failed to refresh VaultAction batch", refreshError);
        }
    }

    async function runOperation(nextOperation: "apply" | "cancel") {
        if (!capability?.csrf_token || operation) return;
        setOperation(nextOperation);
        setError(null);
        try {
            const updated =
                nextOperation === "apply"
                    ? await applyVaultActionBatch(batch.id, capability.csrf_token)
                    : await cancelVaultActionBatch(batch.id, capability.csrf_token);
            onBatchChange?.(updated);
        } catch (operationError) {
            setError(
                operationError instanceof Error ? operationError.message : "VaultAction 操作失败。",
            );
            await refreshBatchAfterError();
        } finally {
            setOperation(null);
        }
    }

    const isPending = batch.status === "pending";
    const files = resultFiles(batch);
    const recoveryArtifacts = resultRecoveryArtifacts(batch);
    const serverError = resultError(batch);
    const expiresAt = new Date(batch.expires_at).toLocaleString("zh-CN", {
        hour12: false,
    });

    return (
        <section
            className="my-3 overflow-hidden rounded-xl border border-amber-300 bg-amber-50 text-left text-slate-900 shadow-sm dark:border-amber-800 dark:bg-amber-950/30 dark:text-slate-100"
            aria-label="文件修改审核"
        >
            <div className="flex flex-wrap items-start justify-between gap-2 border-b border-amber-200 px-4 py-3 dark:border-amber-900">
                <div>
                    <h3 className="font-semibold">待审核的文件修改</h3>
                    <p className="mt-1 text-xs text-slate-600 dark:text-slate-300">
                        批次 {batch.id.slice(0, 8)} · {batch.actions.length} 个操作 · 到期时间{" "}
                        {expiresAt}
                    </p>
                </div>
                <span
                    className="rounded-full border border-current px-2 py-1 text-xs font-medium"
                    aria-live="polite"
                >
                    {STATUS_LABELS[batch.status]}
                </span>
            </div>

            <div className="space-y-3 p-4">
                <ol className="space-y-2 text-sm">
                    {batch.actions.map((action, index) => (
                        <li key={`${action.path}-${index}`} className="flex flex-wrap gap-x-2">
                            <span className="font-medium">
                                {index + 1}. {actionLabel(action)}
                            </span>
                            <code className="break-all text-xs">{action.path}</code>
                        </li>
                    ))}
                </ol>

                <div className="space-y-2">
                    {batch.previews.map((preview) => (
                        <details
                            key={preview.path}
                            className="rounded-lg border border-slate-300 bg-white/80 dark:border-slate-700 dark:bg-slate-950/60"
                            open={batch.previews.length === 1}
                        >
                            <summary className="cursor-pointer px-3 py-2 text-sm font-medium">
                                查看差异：{preview.path}
                            </summary>
                            <pre className="max-h-80 overflow-auto border-t border-slate-200 p-3 text-xs leading-5 dark:border-slate-800">
                                {preview.diff || "（内容无可显示差异）"}
                            </pre>
                            {preview.truncated && (
                                <div className="border-t border-slate-200 px-3 py-2 dark:border-slate-800">
                                    <p className="text-xs text-amber-700 dark:text-amber-300">
                                        差异过大，预览已截断；确认前请展开审核完整内容。
                                    </p>
                                    <details className="mt-2 rounded-md border border-amber-300 dark:border-amber-800">
                                        <summary className="cursor-pointer px-3 py-2 text-xs font-semibold">
                                            查看 {preview.path} 的完整修改内容
                                        </summary>
                                        <div className="max-h-96 space-y-3 overflow-auto border-t border-amber-200 p-3 dark:border-amber-900">
                                            {batch.actions
                                                .filter((action) => action.path === preview.path)
                                                .map((action, index) => (
                                                    <div key={`${action.op}-${index}`}>
                                                        <p className="mb-1 text-xs font-medium">
                                                            {index + 1}. {actionLabel(action)}
                                                        </p>
                                                        {action.op === "replace_text" ? (
                                                            <div className="space-y-2">
                                                                <div>
                                                                    <p className="text-xs">
                                                                        查找：
                                                                    </p>
                                                                    <pre className="whitespace-pre-wrap break-words text-xs">
                                                                        {action.find}
                                                                    </pre>
                                                                </div>
                                                                <div>
                                                                    <p className="text-xs">
                                                                        替换为：
                                                                    </p>
                                                                    <pre className="whitespace-pre-wrap break-words text-xs">
                                                                        {action.replace}
                                                                    </pre>
                                                                </div>
                                                            </div>
                                                        ) : (
                                                            <pre className="whitespace-pre-wrap break-words text-xs">
                                                                {action.content}
                                                            </pre>
                                                        )}
                                                    </div>
                                                ))}
                                        </div>
                                    </details>
                                </div>
                            )}
                        </details>
                    ))}
                </div>

                {(error || serverError) && (
                    <p
                        className="rounded-md bg-rose-100 px-3 py-2 text-sm text-rose-800 dark:bg-rose-950 dark:text-rose-200"
                        role="alert"
                    >
                        {error || serverError}
                    </p>
                )}

                {files.length > 0 && (
                    <p className="text-sm text-slate-600 dark:text-slate-300">
                        涉及文件：{files.join("、")}
                    </p>
                )}

                {recoveryArtifacts.length > 0 && (
                    <details className="rounded-md bg-slate-100 px-3 py-2 text-sm dark:bg-slate-900">
                        <summary className="cursor-pointer font-medium">
                            安全恢复副本（{recoveryArtifacts.length}）
                        </summary>
                        <p className="mt-2 text-xs text-slate-600 dark:text-slate-300">
                            为防止覆盖同时发生的外部编辑，OfferAgent 保留了被替换的旧
                            inode，不会自动删除。
                        </p>
                        <ul className="mt-2 list-disc space-y-1 pl-5 text-xs">
                            {recoveryArtifacts.map((artifact) => (
                                <li key={artifact}>
                                    <code className="break-all">{artifact}</code>
                                </li>
                            ))}
                        </ul>
                    </details>
                )}

                {isPending && (
                    <div className="flex flex-wrap items-center gap-2 border-t border-amber-200 pt-3 dark:border-amber-900">
                        <Button
                            type="button"
                            onClick={() => runOperation("apply")}
                            disabled={operation !== null || capability?.enabled !== true}
                        >
                            {operation === "apply" ? "正在写入…" : "确认并写入"}
                        </Button>
                        <Button
                            type="button"
                            variant="outline"
                            onClick={() => runOperation("cancel")}
                            disabled={operation !== null || !capability?.csrf_token}
                        >
                            {operation === "cancel" ? "正在取消…" : "取消修改"}
                        </Button>
                        {capability?.enabled !== true && (
                            <span className="text-xs text-amber-800 dark:text-amber-200">
                                当前 Web 写入能力未启用，不能确认写入。
                            </span>
                        )}
                    </div>
                )}
            </div>
        </section>
    );
}
