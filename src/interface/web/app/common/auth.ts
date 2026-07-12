"use client";

import useSWR from "swr";

export interface UserProfile {
    email: string;
    username: string;
    photo: string;
    has_documents: boolean;
    detail: string;
    khoj_version: string;
}

const fetcher = async (url: string) => {
    const response = await window.fetch(url);
    const data = await response.json().catch(() => ({}));
    if (!response.ok && !(response.status === 403 && data?.detail === "Forbidden")) {
        throw new Error(
            data?.detail || data?.error || `Failed to fetch ${url}: ${response.status}`,
        );
    }
    return data;
};

export function useAuthenticatedData() {
    const { data, error, isLoading } = useSWR<UserProfile>("/api/v1/user", fetcher, {
        revalidateOnFocus: false,
    });

    if (data?.detail === "Forbidden") {
        return { data: null, error: "Forbidden", isLoading: false };
    }

    return { data, error, isLoading };
}

export interface ModelOptions {
    id: number;
    name: string;
    description: string;
    strengths: string;
}
export interface SyncedContent {
    computer: boolean;
}

export interface UserConfig {
    // user info
    username: string;
    user_photo: string | null;
    given_name: string;
    // user content settings
    enabled_content_source: SyncedContent;
    has_documents: boolean;
    enable_memory: boolean;
    server_memory_mode: "disabled" | "enabled_default_off" | "enabled_default_on";
    // user model settings
    chat_model_options: ModelOptions[];
    selected_chat_model_config: number;
    // server settings
    khoj_version: string;
    anonymous_mode: boolean;
    detail: string;
}

export function useUserConfig(detailed: boolean = false) {
    const url = `/api/settings?detailed=${detailed}`;
    const { data, error, isLoading } = useSWR<UserConfig>(url, fetcher, {
        revalidateOnFocus: false,
    });

    if (error || !data || data?.detail === "Forbidden") {
        return { data: null, error, isLoading };
    }

    return { data, error, isLoading };
}

export function useChatModelOptions() {
    const { data, error, isLoading } = useSWR<ModelOptions[]>(`/api/model/chat/options`, fetcher, {
        revalidateOnFocus: false,
    });

    return { models: data, error, isLoading };
}
