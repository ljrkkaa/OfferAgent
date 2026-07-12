"use client";

import useSWR, { mutate } from "swr";

import { ModelSelector } from "@/app/common/modelSelector";
import { ModelOptions } from "@/app/common/auth";
import { Label } from "@/components/ui/label";
import { Sheet, SheetContent } from "@/components/ui/sheet";
import {
    Sidebar,
    SidebarContent,
    SidebarGroup,
    SidebarGroupContent,
    SidebarGroupLabel,
    SidebarHeader,
    SidebarMenu,
    SidebarMenuItem,
} from "@/components/ui/sidebar";
import { Switch } from "@/components/ui/switch";
import { FilesMenu } from "../allConversations/allConversations";

interface ChatSideBarProps {
    conversationId: string;
    isOpen: boolean;
    isMobileWidth?: boolean;
    onOpenChange: (open: boolean) => void;
}

interface FastModeData {
    available: boolean;
    enabled: boolean;
}

const fetcher = async (url: string) => {
    const response = await fetch(url);
    if (!response.ok) throw new Error(`Failed to fetch ${url}: ${response.status}`);
    return response.json();
};

export function ChatSidebar(props: ChatSideBarProps) {
    if (props.isMobileWidth) {
        return (
            <Sheet open={props.isOpen} onOpenChange={props.onOpenChange}>
                <SheetContent className="w-[300px] bg-sidebar p-0 text-sidebar-foreground [&>button]:hidden">
                    <ChatSidebarInternal {...props} />
                </SheetContent>
            </Sheet>
        );
    }
    return <ChatSidebarInternal {...props} />;
}

function ChatSidebarInternal(props: ChatSideBarProps) {
    const { data: fastModeData } = useSWR<FastModeData>("/api/model/chat/fast", fetcher);

    async function handleModelSelect(model: ModelOptions) {
        const response = await fetch(`/api/model/chat?id=${model.id}`, { method: "POST" });
        if (!response.ok) {
            alert(`Failed to switch chat model to ${model.name}`);
            return false;
        }
        mutate("/api/settings?detailed=true");
        return true;
    }

    async function handleFastModeChange(enabled: boolean) {
        const response = await fetch(`/api/model/chat/fast?enabled=${enabled}`, { method: "POST" });
        if (!response.ok) {
            alert("Failed to switch fast mode");
            return;
        }
        mutate("/api/model/chat/fast");
    }

    return (
        <Sidebar
            collapsible="none"
            className={`ml-auto rounded-lg p-2 transition-all duration-300 ${
                props.isOpen ? "translate-x-0 w-[300px] relative" : "translate-x-full w-0 p-0 m-0"
            }`}
            variant="floating"
        >
            <SidebarHeader>Chat Options</SidebarHeader>
            <SidebarContent>
                <SidebarGroup>
                    <SidebarGroupLabel>Model</SidebarGroupLabel>
                    <SidebarGroupContent>
                        <SidebarMenu>
                            <SidebarMenuItem className="list-none">
                                <ModelSelector onSelect={handleModelSelect} />
                            </SidebarMenuItem>
                            {fastModeData?.available && (
                                <SidebarMenuItem className="list-none">
                                    <div className="flex items-center justify-between gap-2 py-2">
                                        <Label htmlFor="codex-fast-mode">Fast</Label>
                                        <Switch
                                            id="codex-fast-mode"
                                            checked={fastModeData.enabled}
                                            onCheckedChange={handleFastModeChange}
                                        />
                                    </div>
                                </SidebarMenuItem>
                            )}
                        </SidebarMenu>
                    </SidebarGroupContent>
                </SidebarGroup>
                <SidebarGroup>
                    <SidebarGroupLabel>Files</SidebarGroupLabel>
                    <SidebarGroupContent>
                        <FilesMenu
                            conversationId={props.conversationId}
                            uploadedFiles={[]}
                            isMobileWidth={props.isMobileWidth ?? false}
                        />
                    </SidebarGroupContent>
                </SidebarGroup>
            </SidebarContent>
        </Sidebar>
    );
}
