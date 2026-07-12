"use client";

import Link from "next/link";
import { Suspense, useEffect, useState } from "react";
import {
    Brain,
    CaretDown,
    ChatCircleText,
    CheckCircle,
    CloudSlash,
    MagnifyingGlass,
} from "@phosphor-icons/react";

import { useToast } from "@/components/ui/use-toast";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardFooter, CardHeader } from "@/components/ui/card";
import {
    Dialog,
    DialogContent,
    DialogHeader,
    DialogTitle,
    DialogTrigger,
} from "@/components/ui/dialog";
import {
    DropdownMenu,
    DropdownMenuContent,
    DropdownMenuRadioGroup,
    DropdownMenuRadioItem,
    DropdownMenuTrigger,
} from "@/components/ui/dropdown-menu";
import { Separator } from "@/components/ui/separator";
import { SidebarInset, SidebarProvider, SidebarTrigger } from "@/components/ui/sidebar";
import { Switch } from "@/components/ui/switch";

import { ModelOptions, useUserConfig, UserConfig } from "../common/auth";
import { useIsMobileWidth } from "../common/utils";
import { AppSidebar } from "../components/appSidebar/appSidebar";
import Loading from "../components/loading/loading";
import { KhojLogoType } from "../components/logo/khojLogo";
import { UserMemory, UserMemorySchema } from "../components/userMemory/userMemory";
import styles from "./settings.module.css";

interface DropdownComponentProps {
    items: ModelOptions[];
    selected: number;
    callbackFunc: (value: string) => Promise<boolean>;
}

function DropdownComponent({ items, selected, callbackFunc }: DropdownComponentProps) {
    const [position, setPosition] = useState(selected?.toString() ?? "0");

    if (!selected) return null;
    return (
        <div className="overflow-hidden shadow-md rounded-lg">
            <DropdownMenu>
                <DropdownMenuTrigger asChild className="w-full rounded-lg">
                    <Button variant="outline" className="justify-start py-6 rounded-lg">
                        {items.find((item) => item.id.toString() === position)?.name}
                        <CaretDown className="h-4 w-4 ml-auto text-muted-foreground" />
                    </Button>
                </DropdownMenuTrigger>
                <DropdownMenuContent className="max-h-[200px] overflow-y-auto min-w-[var(--radix-dropdown-menu-trigger-width)]">
                    <DropdownMenuRadioGroup
                        value={position}
                        onValueChange={async (value) => {
                            const previous = position;
                            setPosition(value);
                            if (!(await callbackFunc(value))) setPosition(previous);
                        }}
                    >
                        {items.map((item) => (
                            <DropdownMenuRadioItem
                                key={item.id.toString()}
                                value={item.id.toString()}
                            >
                                {item.name}
                            </DropdownMenuRadioItem>
                        ))}
                    </DropdownMenuRadioGroup>
                </DropdownMenuContent>
            </DropdownMenu>
        </div>
    );
}

function isUserMemory(memory: unknown): memory is UserMemorySchema {
    return (
        typeof memory === "object" &&
        memory !== null &&
        typeof (memory as UserMemorySchema).id === "string" &&
        typeof (memory as UserMemorySchema).raw === "string" &&
        typeof (memory as UserMemorySchema).created_at === "string"
    );
}

export default function SettingsView() {
    const { data: initialUserConfig } = useUserConfig(true);
    const [userConfig, setUserConfig] = useState<UserConfig | null>(null);
    const [memories, setMemories] = useState<UserMemorySchema[]>([]);
    const [enableMemory, setEnableMemory] = useState(true);
    const [serverMemoryMode, setServerMemoryMode] = useState("enabled_default_on");
    const { toast } = useToast();
    const isMobileWidth = useIsMobileWidth();
    const cardClassName =
        "w-full lg:w-5/12 grid grid-flow-column border border-gray-300 shadow-md rounded-lg dark:border-none border-opacity-50 dark:bg-muted";

    useEffect(() => {
        setUserConfig(initialUserConfig);
        setEnableMemory(initialUserConfig?.enable_memory ?? true);
        setServerMemoryMode(initialUserConfig?.server_memory_mode ?? "enabled_default_on");
    }, [initialUserConfig]);

    const updateModel = async (id: string) => {
        const selected = userConfig?.chat_model_options.find((model) => model.id.toString() === id);
        const response = await fetch(`/api/model/chat?id=${encodeURIComponent(id)}`, {
            method: "POST",
        });
        const data = await response.json().catch(() => ({}));
        if (!response.ok || data.status === "error") {
            toast({
                description: `❌ Failed to switch model to ${selected?.name}.`,
                variant: "destructive",
            });
            return false;
        }
        setUserConfig((current) =>
            current ? { ...current, selected_chat_model_config: Number(id) } : current,
        );
        toast({ title: `✅ Switched model to ${selected?.name}` });
        return true;
    };

    const fetchMemories = async () => {
        const response = await fetch("/api/memories");
        const data = response.ok ? await response.json() : [];
        if (!Array.isArray(data) || !data.every(isUserMemory)) {
            setMemories([]);
            toast({
                title: "Error",
                description: "Failed to fetch memories.",
                variant: "destructive",
            });
            return;
        }
        setMemories(data);
    };

    const handleDeleteMemory = async (id: string) => {
        const response = await fetch(`/api/memories/${id}`, { method: "DELETE" });
        if (!response.ok) return false;
        setMemories((current) => current.filter((memory) => memory.id !== id));
        return true;
    };

    const handleUpdateMemory = async (id: string, raw: string) => {
        const response = await fetch(`/api/memories/${id}`, {
            method: "PUT",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ raw, memory_id: id }),
        });
        if (!response.ok) return false;
        const updatedMemory = await response.json();
        if (!isUserMemory(updatedMemory)) return false;
        setMemories((current) =>
            current.map((memory) => (memory.id === id ? updatedMemory : memory)),
        );
        return true;
    };

    const handleToggleMemory = async (enabled: boolean) => {
        const response = await fetch(`/api/user/memory?enable_memory=${enabled}`, {
            method: "PATCH",
        });
        if (!response.ok) {
            toast({
                title: "Error",
                description: "Failed to update memory setting.",
                variant: "destructive",
            });
            return;
        }
        setEnableMemory(enabled);
    };

    const disconnectContent = async () => {
        const response = await fetch("/api/content/source/computer", { method: "DELETE" });
        if (!response.ok) {
            toast({
                title: "Error",
                description: "Failed to clear synced files.",
                variant: "destructive",
            });
            return;
        }
        setUserConfig((current) =>
            current
                ? {
                      ...current,
                      enabled_content_source: {
                          ...current.enabled_content_source,
                          computer: false,
                      },
                  }
                : current,
        );
    };

    if (!userConfig) return <Loading />;

    return (
        <SidebarProvider>
            <AppSidebar conversationId="" />
            <SidebarInset>
                <header className="flex h-16 shrink-0 items-center gap-2 border-b px-4">
                    <SidebarTrigger className="-ml-1" />
                    <Separator orientation="vertical" className="mr-2 h-4" />
                    {isMobileWidth ? (
                        <Link className="p-0 no-underline" href="/">
                            <KhojLogoType className="h-auto w-32 max-w-full" />
                        </Link>
                    ) : (
                        <h2 className="text-lg">Settings</h2>
                    )}
                </header>
                <div className={styles.page}>
                    <title>Settings</title>
                    <div className={styles.content}>
                        <div className={`${styles.contentBody} mx-10 my-2`}>
                            <Suspense fallback={<Loading />}>
                                <div className="grid grid-flow-column sm:grid-flow-row gap-16 m-8">
                                    <section className="grid gap-8">
                                        <h1 className="text-2xl">Content</h1>
                                        <Card className={cardClassName}>
                                            <CardHeader className="flex flex-row text-xl">
                                                <Brain className="h-8 w-8 mr-2" /> Knowledge Base
                                                {userConfig.enabled_content_source.computer && (
                                                    <CheckCircle
                                                        className="h-6 w-6 ml-auto text-green-500"
                                                        weight="fill"
                                                    />
                                                )}
                                            </CardHeader>
                                            <CardContent className="pb-12 text-gray-400">
                                                Manage and search your indexed files.
                                            </CardContent>
                                            <CardFooter className="flex gap-4">
                                                <Button
                                                    variant="outline"
                                                    size="sm"
                                                    onClick={() =>
                                                        (window.location.href = "/search")
                                                    }
                                                >
                                                    <MagnifyingGlass className="h-5 w-5 mr-1" />{" "}
                                                    Search
                                                </Button>
                                                {userConfig.enabled_content_source.computer && (
                                                    <Button
                                                        variant="outline"
                                                        size="sm"
                                                        onClick={disconnectContent}
                                                    >
                                                        <CloudSlash className="h-5 w-5 mr-1" />{" "}
                                                        Clear All
                                                    </Button>
                                                )}
                                            </CardFooter>
                                        </Card>
                                    </section>

                                    {userConfig.chat_model_options.length > 0 && (
                                        <section className="grid gap-8">
                                            <h1 className="text-2xl">Model</h1>
                                            <Card className={cardClassName}>
                                                <CardHeader className="text-xl flex flex-row">
                                                    <ChatCircleText className="h-7 w-7 mr-2" /> Chat
                                                </CardHeader>
                                                <CardContent className="grid gap-8">
                                                    <DropdownComponent
                                                        items={userConfig.chat_model_options}
                                                        selected={
                                                            userConfig.selected_chat_model_config
                                                        }
                                                        callbackFunc={updateModel}
                                                    />
                                                </CardContent>
                                            </Card>
                                        </section>
                                    )}

                                    <section className="grid gap-8">
                                        <h1 className="text-2xl">Memory</h1>
                                        <Card className={cardClassName}>
                                            <CardHeader className="text-xl flex flex-row">
                                                <Brain className="h-7 w-7 mr-2" /> Long-term Memory
                                            </CardHeader>
                                            <CardContent className="grid gap-4">
                                                <div className="flex items-center justify-between">
                                                    <label htmlFor="enable-memory">
                                                        Enable Memory
                                                    </label>
                                                    <Switch
                                                        id="enable-memory"
                                                        checked={enableMemory}
                                                        onCheckedChange={handleToggleMemory}
                                                        disabled={serverMemoryMode === "disabled"}
                                                    />
                                                </div>
                                            </CardContent>
                                            <CardFooter>
                                                <Dialog
                                                    onOpenChange={(open) => open && fetchMemories()}
                                                >
                                                    <DialogTrigger asChild>
                                                        <Button variant="outline">
                                                            Browse Memories
                                                        </Button>
                                                    </DialogTrigger>
                                                    <DialogContent className="max-w-2xl max-h-[80vh] overflow-y-auto">
                                                        <DialogHeader>
                                                            <DialogTitle>Your Memories</DialogTitle>
                                                        </DialogHeader>
                                                        <div className="grid gap-4 py-4">
                                                            {memories.map((memory) => (
                                                                <UserMemory
                                                                    key={memory.id}
                                                                    memory={memory}
                                                                    onDelete={handleDeleteMemory}
                                                                    onUpdate={handleUpdateMemory}
                                                                />
                                                            ))}
                                                            {memories.length === 0 && (
                                                                <p>No memories found</p>
                                                            )}
                                                        </div>
                                                    </DialogContent>
                                                </Dialog>
                                            </CardFooter>
                                        </Card>
                                    </section>
                                </div>
                            </Suspense>
                        </div>
                    </div>
                </div>
            </SidebarInset>
        </SidebarProvider>
    );
}
