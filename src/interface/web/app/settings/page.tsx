"use client";

import Link from "next/link";
import styles from "./settings.module.css";

import { Suspense, useEffect, useState } from "react";
import { useToast } from "@/components/ui/use-toast";

import { useUserConfig, ModelOptions, UserConfig } from "../common/auth";
import { toTitleCase, useIsMobileWidth } from "../common/utils";

import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Card, CardContent, CardFooter, CardHeader } from "@/components/ui/card";
import { Switch } from "@/components/ui/switch";

import {
    DropdownMenu,
    DropdownMenuContent,
    DropdownMenuRadioGroup,
    DropdownMenuRadioItem,
    DropdownMenuTrigger,
} from "@/components/ui/dropdown-menu";
import {
    AlertDialog,
    AlertDialogAction,
    AlertDialogCancel,
    AlertDialogContent,
    AlertDialogDescription,
    AlertDialogFooter,
    AlertDialogHeader,
    AlertDialogTitle,
    AlertDialogTrigger,
} from "@/components/ui/alert-dialog";

import {
    Dialog,
    DialogContent,
    DialogHeader,
    DialogTitle,
    DialogTrigger,
} from "@/components/ui/dialog";

import { Table, TableBody, TableCell, TableRow } from "@/components/ui/table";

import {
    ChatCircleText,
    Key,
    UserCircle,
    Trash,
    Copy,
    CheckCircle,
    CloudSlash,
    Plus,
    FloppyDisk,
    CaretDown,
    MagnifyingGlass,
    Brain,
    EyeSlash,
    Eye,
    Download,
    TrashSimple,
} from "@phosphor-icons/react";

import Loading from "../components/loading/loading";

import { SidebarInset, SidebarProvider, SidebarTrigger } from "@/components/ui/sidebar";
import { AppSidebar } from "../components/appSidebar/appSidebar";
import { UserMemory, UserMemorySchema } from "../components/userMemory/userMemory";
import { Separator } from "@/components/ui/separator";
import { KhojLogoType } from "../components/logo/khojLogo";
import { Progress } from "@/components/ui/progress";

import JSZip from "jszip";
import { saveAs } from "file-saver";

interface DropdownComponentProps {
    items: ModelOptions[];
    selected: number;
    isActive?: boolean;
    callbackFunc: (value: string) => Promise<boolean>;
}

const DropdownComponent: React.FC<DropdownComponentProps> = ({
    items,
    selected,
    isActive,
    callbackFunc,
}) => {
    const [position, setPosition] = useState(selected?.toString() ?? "0");

    return (
        !!selected && (
            <div className="overflow-hidden shadow-md rounded-lg">
                <DropdownMenu>
                    <DropdownMenuTrigger asChild className="w-full rounded-lg">
                        <Button variant="outline" className="justify-start py-6 rounded-lg">
                            {items.find((item) => item.id.toString() === position)?.name}{" "}
                            <CaretDown className="h-4 w-4 ml-auto text-muted-foreground" />
                        </Button>
                    </DropdownMenuTrigger>
                    <DropdownMenuContent
                        style={{
                            maxHeight: "200px",
                            overflowY: "auto",
                            minWidth: "var(--radix-dropdown-menu-trigger-width)",
                        }}
                    >
                        <DropdownMenuRadioGroup
                            value={position}
                            onValueChange={async (value) => {
                                const previousPosition = position;
                                setPosition(value);
                                try {
                                    if (await callbackFunc(value)) return;
                                } catch (error) {
                                    console.error("Dropdown callback failed:", error);
                                }
                                setPosition(previousPosition);
                            }}
                        >
                            {items.map((item) => (
                                <DropdownMenuRadioItem
                                    key={item.id.toString()}
                                    value={item.id.toString()}
                                    disabled={!isActive && item.tier !== "free"}
                                >
                                    {item.name}{" "}
                                    {item.tier === "standard" && (
                                        <span className="text-green-500 ml-2">(standard)</span>
                                    )}
                                </DropdownMenuRadioItem>
                            ))}
                        </DropdownMenuRadioGroup>
                    </DropdownMenuContent>
                </DropdownMenu>
            </div>
        )
    );
};

interface TokenObject {
    token: string;
    name: string;
}

function isUserMemory(memory: unknown): memory is UserMemorySchema {
    return (
        typeof memory === "object" &&
        memory !== null &&
        typeof (memory as UserMemorySchema).id === "number" &&
        typeof (memory as UserMemorySchema).raw === "string" &&
        typeof (memory as UserMemorySchema).created_at === "string"
    );
}

function isExportedConversation(conversation: unknown) {
    return (
        typeof conversation === "object" &&
        conversation !== null &&
        ((conversation as { title?: unknown }).title === null ||
            typeof (conversation as { title?: unknown }).title === "string") &&
        typeof (conversation as { agent?: unknown }).agent === "string" &&
        typeof (conversation as { created_at?: unknown }).created_at === "string" &&
        typeof (conversation as { updated_at?: unknown }).updated_at === "string" &&
        typeof (conversation as { conversation_log?: unknown }).conversation_log === "object" &&
        Array.isArray((conversation as { file_filters?: unknown }).file_filters)
    );
}

const useApiKeys = () => {
    const [apiKeys, setApiKeys] = useState<TokenObject[]>([]);
    const { toast } = useToast();

    const generateAPIKey = async () => {
        try {
            const response = await fetch(`/auth/token`, {
                method: "POST",
                headers: {
                    "Content-Type": "application/json",
                },
            });
            if (!response.ok) throw new Error("Failed to generate API key");
            const tokenObj = await response.json();
            if (typeof tokenObj?.token !== "string" || typeof tokenObj?.name !== "string") {
                throw new Error("Invalid API key response");
            }
            setApiKeys((prevKeys) => [...prevKeys, tokenObj]);
        } catch (error) {
            console.error("Error generating API key:", error);
            toast({
                title: "API Key",
                description: "Failed to generate API key. Please try again.",
                variant: "destructive",
            });
        }
    };

    const copyAPIKey = async (token: string) => {
        try {
            await navigator.clipboard.writeText(token);
            toast({
                title: "🔑 API Key",
                description: "Copied to clipboard",
            });
        } catch (error) {
            console.error("Error copying API key:", error);
            toast({
                title: "API Key",
                description: "Failed to copy API key.",
                variant: "destructive",
            });
        }
    };

    const deleteAPIKey = async (token: string) => {
        try {
            const response = await fetch(`/auth/token?token=${encodeURIComponent(token)}`, {
                method: "DELETE",
            });
            if (!response.ok) {
                throw new Error("Failed to delete API key");
            }
            setApiKeys((prevKeys) => prevKeys.filter((key) => key.token !== token));
            toast({
                title: "API Key",
                description: "Deleted API key",
            });
        } catch (error) {
            console.error("Error deleting API key:", error);
            toast({
                title: "API Key",
                description: "Failed to delete API key. Please try again.",
                variant: "destructive",
            });
        }
    };

    useEffect(() => {
        const listApiKeys = async () => {
            try {
                const response = await fetch(`/auth/token`);
                if (!response.ok) throw new Error("Failed to list API keys");
                const tokens = await response.json();
                if (
                    !Array.isArray(tokens) ||
                    !tokens.every(
                        (token) =>
                            typeof token?.token === "string" && typeof token?.name === "string",
                    )
                ) {
                    throw new Error("Invalid API key list response");
                }
                setApiKeys(tokens);
            } catch (error) {
                console.error("Error listing API keys:", error);
                toast({
                    title: "API Key",
                    description: "Failed to load API keys.",
                    variant: "destructive",
                });
            }
        };

        listApiKeys();
    }, [toast]);

    return {
        apiKeys,
        generateAPIKey,
        copyAPIKey,
        deleteAPIKey,
    };
};

function ApiKeyCard() {
    const { apiKeys, generateAPIKey, copyAPIKey, deleteAPIKey } = useApiKeys();
    const [visibleApiKeys, setVisibleApiKeys] = useState<Set<string>>(new Set());
    const { toast } = useToast();

    return (
        <Card className="grid grid-flow-column border border-gray-300 shadow-md rounded-lg dark:bg-muted dark:border-none border-opacity-50 lg:w-2/3">
            <CardHeader className="text-xl grid grid-flow-col grid-cols-[1fr_auto] pb-0">
                <span className="flex flex-wrap">
                    <Key className="h-7 w-7 mr-2" />
                    API Keys
                </span>
                <Button variant="secondary" className="!mt-0" onClick={generateAPIKey}>
                    <Plus weight="bold" className="h-5 w-5 mr-2" />
                    Generate Key
                </Button>
            </CardHeader>
            <CardContent className="overflow-hidden grid gap-6">
                <p className="text-md text-gray-400">
                    Access Khoj from Obsidian or local API clients.
                </p>
                <Table>
                    <TableBody>
                        {apiKeys.map((key) => (
                            <TableRow key={key.token}>
                                <TableCell className="pl-0 py-3">{key.name}</TableCell>
                                <TableCell className="grid grid-flow-col grid-cols-[1fr_auto] bg-secondary dark:bg-background rounded-xl p-3 m-1">
                                    <span className="font-mono text-left w-[50px] md:w-[400px]">
                                        {visibleApiKeys.has(key.token)
                                            ? key.token
                                            : `${key.token.slice(0, 6)}...${key.token.slice(-4)}`}
                                    </span>
                                    <div className="grid grid-flow-col">
                                        {visibleApiKeys.has(key.token) ? (
                                            <EyeSlash
                                                weight="bold"
                                                className="h-4 w-4 mr-2 hover:bg-primary/40"
                                                onClick={() =>
                                                    setVisibleApiKeys((prev) => {
                                                        const next = new Set(prev);
                                                        next.delete(key.token);
                                                        return next;
                                                    })
                                                }
                                            />
                                        ) : (
                                            <Eye
                                                weight="bold"
                                                className="h-4 w-4 mr-2 hover:bg-primary/40"
                                                onClick={() =>
                                                    setVisibleApiKeys(
                                                        new Set([...visibleApiKeys, key.token]),
                                                    )
                                                }
                                            />
                                        )}
                                        <Copy
                                            weight="bold"
                                            className="h-4 w-4 mr-2 hover:bg-primary/40"
                                            onClick={() => {
                                                copyAPIKey(key.token);
                                            }}
                                        />
                                        <Trash
                                            weight="bold"
                                            className="h-4 w-4 mr-2 md:ml-4 text-red-400 hover:bg-primary/40"
                                            onClick={() => {
                                                deleteAPIKey(key.token);
                                            }}
                                        />
                                    </div>
                                </TableCell>
                            </TableRow>
                        ))}
                    </TableBody>
                </Table>
            </CardContent>
            <CardFooter className="flex flex-wrap gap-4" />
        </Card>
    );
}

export default function SettingsView() {
    const { data: initialUserConfig } = useUserConfig(true);
    const [userConfig, setUserConfig] = useState<UserConfig | null>(null);
    const [name, setName] = useState<string | undefined>(undefined);
    const [memories, setMemories] = useState<UserMemorySchema[]>([]);
    const [enableMemory, setEnableMemory] = useState<boolean>(true);
    const [serverMemoryMode, setServerMemoryMode] = useState<string>("enabled_default_on");
    const [isExporting, setIsExporting] = useState(false);
    const [exportProgress, setExportProgress] = useState(0);
    const [exportedConversations, setExportedConversations] = useState(0);
    const [totalConversations, setTotalConversations] = useState(0);
    const { toast } = useToast();
    const isMobileWidth = useIsMobileWidth();

    const title = "Settings";

    const cardClassName =
        "w-full lg:w-5/12 grid grid-flow-column border border-gray-300 shadow-md rounded-lg border dark:border-none border-opacity-50 dark:bg-muted";

    useEffect(() => {
        setUserConfig(initialUserConfig);
        setName(initialUserConfig?.given_name);
        setEnableMemory(initialUserConfig?.enable_memory ?? true);
        setServerMemoryMode(initialUserConfig?.server_memory_mode ?? "enabled_default_on");
    }, [initialUserConfig]);

    const saveName = async () => {
        if (!name) return;
        try {
            const response = await fetch(`/api/user/name?name=${encodeURIComponent(name)}`, {
                method: "PATCH",
                headers: {
                    "Content-Type": "application/json",
                },
            });
            if (!response.ok) throw new Error("Failed to update name");

            setUserConfig((currentUserConfig) =>
                currentUserConfig ? { ...currentUserConfig, given_name: name } : currentUserConfig,
            );

            // Notify user of name change
            toast({
                title: `✅ Updated Profile`,
                description: `You name has been updated to ${name}`,
            });
        } catch (error) {
            console.error("Error updating name:", error);
            toast({
                title: "⚠️ Failed to Update Profile",
                description: "Failed to update name. Try again or contact team@khoj.dev",
            });
        }
    };

    const updateModel = (modelType: string) => async (id: string) => {
        // Get the selected model from the options
        const modelOptions = userConfig?.chat_model_options;

        const selectedModel = modelOptions?.find((model) => model.id.toString() === id);
        const modelName = selectedModel?.name;

        // Check if the model is free tier or if the user is active
        if (!userConfig?.is_active && selectedModel?.tier !== "free") {
            toast({
                title: `Model Update`,
                description: `This account cannot switch ${modelType} model to ${modelName}.`,
                variant: "destructive",
            });
            return false;
        }

        try {
            const response = await fetch(`/api/model/${modelType}?id=${encodeURIComponent(id)}`, {
                method: "POST",
                headers: {
                    "Content-Type": "application/json",
                },
            });
            const data = await response.json().catch(() => ({}));

            if (!response.ok || data.status === "error")
                throw new Error(`Failed to switch ${modelType} model to ${modelName}`);

            if (modelType === "chat") {
                setUserConfig((currentUserConfig) =>
                    currentUserConfig
                        ? { ...currentUserConfig, selected_chat_model_config: Number(id) }
                        : currentUserConfig,
                );
            }

            toast({
                title: `✅ Switched ${modelType} model to ${modelName}`,
            });
            return true;
        } catch (error) {
            console.error(`Failed to update ${modelType} model to ${modelName}:`, error);
            toast({
                description: `❌ Failed to switch ${modelType} model to ${modelName}. Try again.`,
                variant: "destructive",
            });
            return false;
        }
    };

    const exportChats = async () => {
        try {
            setIsExporting(true);

            // Get total conversation count
            const statsResponse = await fetch("/api/chat/stats");
            if (!statsResponse.ok) throw new Error("Failed to fetch chat export stats");
            const stats = await statsResponse.json();
            const total = stats.num_conversations;
            if (!Number.isInteger(total) || total < 0) {
                throw new Error("Invalid chat export stats response");
            }
            setTotalConversations(total);

            // Create zip file
            const zip = new JSZip();
            const conversations = [];

            // Fetch all conversations in batches of 10
            for (let page = 0; page * 10 < total; page++) {
                const response = await fetch(`/api/chat/export?page=${page}`);
                if (!response.ok) throw new Error(`Failed to export chat page ${page}`);
                const data = await response.json();
                if (!Array.isArray(data) || !data.every(isExportedConversation)) {
                    throw new Error(`Invalid chat export page ${page}`);
                }
                conversations.push(...data);

                const exportedCount = Math.min(conversations.length, total);
                setExportedConversations(exportedCount);
                setExportProgress((exportedCount / total) * 100);
            }

            // Add conversations to zip
            zip.file("conversations.json", JSON.stringify(conversations, null, 2));

            // Generate and download zip
            const content = await zip.generateAsync({ type: "blob" });
            saveAs(content, "khoj-conversations.zip");

            toast({
                title: "Export Complete",
                description: `Successfully exported ${conversations.length} conversations`,
            });
        } catch (error) {
            console.error("Error exporting chats:", error);
            toast({
                title: "Export Failed",
                description: "Failed to export chats. Please try again.",
                variant: "destructive",
            });
        } finally {
            setIsExporting(false);
            setExportProgress(0);
            setExportedConversations(0);
            setTotalConversations(0);
        }
    };

    const fetchMemories = async () => {
        try {
            console.log("Fetching memories...");
            const response = await fetch("/api/memories");
            if (!response.ok) throw new Error("Failed to fetch memories");
            const data = await response.json();
            if (!Array.isArray(data) || !data.every(isUserMemory)) {
                throw new Error("Invalid memories response");
            }
            setMemories(data);
        } catch (error) {
            console.error("Error fetching memories:", error);
            setMemories([]);
            toast({
                title: "Error",
                description: "Failed to fetch memories. Please try again.",
                variant: "destructive",
            });
        }
    };

    const handleDeleteMemory = async (id: number) => {
        try {
            const response = await fetch(`/api/memories/${id}`, {
                method: "DELETE",
            });
            if (!response.ok) throw new Error("Failed to delete memory");
            setMemories((currentMemories) => currentMemories.filter((memory) => memory.id !== id));
            return true;
        } catch (error) {
            console.error("Error deleting memory:", error);
            toast({
                title: "Error",
                description: "Failed to delete memory. Please try again.",
                variant: "destructive",
            });
            return false;
        }
    };

    const handleUpdateMemory = async (id: number, raw: string) => {
        try {
            const response = await fetch(`/api/memories/${id}`, {
                method: "PUT",
                headers: {
                    "Content-Type": "application/json",
                },
                body: JSON.stringify({ raw, memory_id: id }),
            });
            if (!response.ok) throw new Error("Failed to update memory");
            const updatedMemory: UserMemorySchema = await response.json();
            if (!isUserMemory(updatedMemory)) {
                throw new Error("Invalid memory update response");
            }
            setMemories((currentMemories) =>
                currentMemories.map((memory) => (memory.id === id ? updatedMemory : memory)),
            );
            return true;
        } catch (error) {
            console.error("Error updating memory:", error);
            toast({
                title: "Error",
                description: "Failed to update memory. Please try again.",
                variant: "destructive",
            });
            return false;
        }
    };

    const handleToggleMemory = async (enabled: boolean) => {
        try {
            const response = await fetch(`/api/user/memory?enable_memory=${enabled}`, {
                method: "PATCH",
            });
            if (!response.ok) throw new Error("Failed to update memory setting");
            setEnableMemory(enabled);
            toast({
                title: enabled ? "Memory enabled" : "Memory disabled",
                description: enabled
                    ? "Khoj will learn and remember from your conversations."
                    : "Khoj will no longer learn or remember from your conversations.",
            });
        } catch (error) {
            console.error("Error toggling memory:", error);
            toast({
                title: "Error",
                description: "Failed to update memory setting. Please try again.",
                variant: "destructive",
            });
        }
    };

    const syncContent = async (type: string) => {
        try {
            const response = await fetch(`/api/content?t=${type}`, {
                method: "PATCH",
                headers: {
                    "Content-Type": "application/json",
                },
            });
            if (!response.ok) throw new Error(`Failed to sync content from ${type}`);

            toast({
                title: `🔄 Syncing ${type}`,
                description: `Your ${type} content is being synced.`,
            });
        } catch (error) {
            console.error("Error syncing content:", error);
            toast({
                title: `⚠️ Failed to Sync ${type}`,
                description: `Failed to sync ${type} content. Try again or contact team@khoj.dev`,
            });
        }
    };

    const disconnectContent = async (source: string) => {
        try {
            const response = await fetch(`/api/content/source/${source}`, {
                method: "DELETE",
                headers: {
                    "Content-Type": "application/json",
                },
            });
            if (!response.ok) throw new Error(`Failed to disconnect ${source}`);

            if (source === "computer") {
                setUserConfig((currentUserConfig) =>
                    currentUserConfig
                        ? {
                              ...currentUserConfig,
                              enabled_content_source: {
                                  ...currentUserConfig.enabled_content_source,
                                  computer: false,
                              },
                          }
                        : currentUserConfig,
                );
            }

            // Notify user about disconnecting content source
            if (source === "computer") {
                toast({
                    title: `✅ Deleted Synced Files`,
                    description: "Your synced documents have been deleted.",
                });
            } else {
                toast({
                    title: `✅ Disconnected ${source}`,
                    description: `Your ${source} integration to Khoj has been disconnected.`,
                });
            }
        } catch (error) {
            console.error(`Error disconnecting ${source}:`, error);
            toast({
                title: `⚠️ Failed to Disconnect ${source}`,
                description: `Failed to disconnect from ${source}. Try again or contact team@khoj.dev`,
            });
        }
    };

    if (!userConfig) return <Loading />;

    return (
        <SidebarProvider>
            <AppSidebar conversationId={""} />
            <SidebarInset>
                <header className="flex h-16 shrink-0 items-center gap-2 border-b px-4">
                    <SidebarTrigger className="-ml-1" />
                    <Separator orientation="vertical" className="mr-2 h-4" />
                    {isMobileWidth ? (
                        <Link className="p-0 no-underline" href="/">
                            <KhojLogoType className="h-auto w-16" />
                        </Link>
                    ) : (
                        <h2 className="text-lg">Settings</h2>
                    )}
                </header>
                <div className={styles.page}>
                    <title>{title}</title>
                    <div className={styles.content}>
                        <div className={`${styles.contentBody} mx-10 my-2`}>
                            <Suspense fallback={<Loading />}>
                                <div
                                    id="content"
                                    className="grid grid-flow-column sm:grid-flow-row gap-16 m-8"
                                >
                                    <div className="section grid gap-8">
                                        <div className="text-2xl">Profile</div>
                                        <div className="cards flex flex-wrap gap-16">
                                            <Card className={cardClassName}>
                                                <CardHeader className="text-xl flex flex-row">
                                                    <UserCircle className="h-7 w-7 mr-2" />
                                                    Name
                                                </CardHeader>
                                                <CardContent className="overflow-hidden">
                                                    <p className="pb-4 text-gray-400">
                                                        What should Khoj refer to you as?
                                                    </p>
                                                    <Input
                                                        type="text"
                                                        onChange={(e) => setName(e.target.value)}
                                                        value={name || ""}
                                                        className="w-full border border-gray-300 rounded-lg p-4 py-6"
                                                    />
                                                </CardContent>
                                                <CardFooter className="flex flex-wrap gap-4">
                                                    <Button
                                                        variant="outline"
                                                        size="sm"
                                                        onClick={saveName}
                                                        disabled={name === userConfig.given_name}
                                                    >
                                                        <FloppyDisk className="h-5 w-5 inline mr-2" />
                                                        Save
                                                    </Button>
                                                </CardFooter>
                                            </Card>
                                        </div>
                                    </div>
                                    <div className="section grid gap-8">
                                        <div className="text-2xl">Content</div>
                                        <div className="cards flex flex-wrap gap-16">
                                            <Card id="computer" className={cardClassName}>
                                                <CardHeader className="flex flex-row text-xl">
                                                    <Brain className="h-8 w-8 mr-2" />
                                                    Knowledge Base
                                                    {userConfig.enabled_content_source.computer && (
                                                        <CheckCircle
                                                            className="h-6 w-6 ml-auto text-green-500"
                                                            weight="fill"
                                                        />
                                                    )}
                                                </CardHeader>
                                                <CardContent className="overflow-hidden pb-12 text-gray-400">
                                                    Manage and search through your digital brain.
                                                </CardContent>
                                                <CardFooter className="flex flex-wrap gap-4">
                                                    <Button
                                                        variant="outline"
                                                        size="sm"
                                                        title="Search thorugh files"
                                                        onClick={() =>
                                                            (window.location.href = "/search")
                                                        }
                                                    >
                                                        <MagnifyingGlass className="h-5 w-5 inline mr-1" />
                                                        Search
                                                    </Button>
                                                    <Button
                                                        variant="outline"
                                                        size="sm"
                                                        className={`${userConfig.enabled_content_source.computer || "hidden"}`}
                                                        onClick={() =>
                                                            disconnectContent("computer")
                                                        }
                                                    >
                                                        <CloudSlash className="h-5 w-5 inline mr-1" />
                                                        Clear All
                                                    </Button>
                                                </CardFooter>
                                            </Card>
                                        </div>
                                    </div>
                                    <div className="section grid gap-8">
                                        <div className="text-2xl">Models</div>
                                        <div className="cards flex flex-wrap gap-16">
                                            {userConfig.chat_model_options.length > 0 && (
                                                <Card className={cardClassName}>
                                                    <CardHeader className="text-xl flex flex-row">
                                                        <ChatCircleText className="h-7 w-7 mr-2" />
                                                        Chat
                                                    </CardHeader>
                                                    <CardContent className="overflow-hidden pb-12 grid gap-8 h-fit">
                                                        <p className="text-gray-400">
                                                            Pick the chat model to generate text
                                                            responses
                                                        </p>
                                                        <DropdownComponent
                                                            items={userConfig.chat_model_options}
                                                            selected={
                                                                userConfig.selected_chat_model_config
                                                            }
                                                            isActive={userConfig.is_active}
                                                            callbackFunc={updateModel("chat")}
                                                        />
                                                    </CardContent>
                                                    <CardFooter className="flex flex-wrap gap-4">
                                                        {!userConfig.is_active && (
                                                            <p className="text-gray-400">
                                                                {userConfig.chat_model_options.some(
                                                                    (model) =>
                                                                        model.tier === "free",
                                                                )
                                                                    ? "Free models available"
                                                                    : "Model switching unavailable"}
                                                            </p>
                                                        )}
                                                    </CardFooter>
                                                </Card>
                                            )}
                                        </div>
                                    </div>
                                    <div className="section grid gap-8">
                                        <div id="clients" className="text-2xl">
                                            Clients
                                        </div>
                                        <div className="cards flex flex-col flex-wrap gap-8">
                                            {!userConfig.anonymous_mode && <ApiKeyCard />}
                                        </div>
                                    </div>
                                    <div className="section grid gap-8">
                                        <div id="account" className="text-2xl">
                                            Account
                                        </div>
                                        <div className="cards flex flex-wrap gap-16">
                                            <Card className={cardClassName}>
                                                <CardHeader className="text-xl flex flex-row">
                                                    <Download className="h-7 w-7 mr-2" />
                                                    Export Data
                                                </CardHeader>
                                                <CardContent className="overflow-hidden">
                                                    <p className="pb-4 text-gray-400">
                                                        Download all your chat conversations
                                                    </p>
                                                    {exportProgress > 0 && (
                                                        <div className="w-full mt-4">
                                                            <Progress
                                                                value={exportProgress}
                                                                className="w-full"
                                                            />
                                                            <p className="text-sm text-gray-500 mt-2">
                                                                Exported {exportedConversations} of{" "}
                                                                {totalConversations} conversations
                                                            </p>
                                                        </div>
                                                    )}
                                                </CardContent>
                                                <CardFooter className="flex flex-wrap gap-4">
                                                    <Button
                                                        variant="outline"
                                                        onClick={exportChats}
                                                        disabled={isExporting}
                                                    >
                                                        <Download className="h-5 w-5 mr-2" />
                                                        {isExporting
                                                            ? "Exporting..."
                                                            : "Export Chats"}
                                                    </Button>
                                                </CardFooter>
                                            </Card>
                                            <Card className={cardClassName}>
                                                <CardHeader className="text-xl flex flex-row">
                                                    <Brain className="h-7 w-7 mr-2" />
                                                    Memories
                                                </CardHeader>
                                                <CardContent className="overflow-hidden">
                                                    <p className="pb-4 text-gray-400">
                                                        View and manage your long-term memories
                                                    </p>
                                                    <div className="flex items-center justify-between">
                                                        <label
                                                            htmlFor="enable-memory"
                                                            className={`text-sm font-medium leading-none ${serverMemoryMode === "disabled" ? "text-gray-400" : ""}`}
                                                        >
                                                            Enable Memory
                                                        </label>
                                                        <Switch
                                                            id="enable-memory"
                                                            checked={enableMemory}
                                                            onCheckedChange={(checked) =>
                                                                handleToggleMemory(checked)
                                                            }
                                                            disabled={
                                                                serverMemoryMode === "disabled"
                                                            }
                                                        />
                                                    </div>
                                                    {serverMemoryMode === "disabled" && (
                                                        <p className="text-xs text-gray-400 mt-2">
                                                            Memory has been disabled by the server
                                                            administrator.
                                                        </p>
                                                    )}
                                                </CardContent>
                                                <CardFooter className="flex flex-wrap gap-4">
                                                    <Dialog
                                                        onOpenChange={(open) =>
                                                            open && fetchMemories()
                                                        }
                                                    >
                                                        <DialogTrigger asChild>
                                                            <Button variant="outline">
                                                                <Brain className="h-5 w-5 mr-2" />
                                                                Browse Memories
                                                            </Button>
                                                        </DialogTrigger>
                                                        <DialogContent className="max-w-2xl max-h-[80vh] overflow-y-auto">
                                                            <DialogHeader>
                                                                <DialogTitle>
                                                                    Your Memories
                                                                </DialogTitle>
                                                            </DialogHeader>
                                                            <div className="grid gap-4 py-4">
                                                                {memories.map((memory) => (
                                                                    <UserMemory
                                                                        key={memory.id}
                                                                        memory={memory}
                                                                        onDelete={
                                                                            handleDeleteMemory
                                                                        }
                                                                        onUpdate={
                                                                            handleUpdateMemory
                                                                        }
                                                                    />
                                                                ))}
                                                                {memories.length === 0 && (
                                                                    <p className="text-center text-gray-500">
                                                                        No memories found
                                                                    </p>
                                                                )}
                                                            </div>
                                                        </DialogContent>
                                                    </Dialog>
                                                </CardFooter>
                                            </Card>
                                            <Card className={cardClassName}>
                                                <CardHeader className="text-xl flex flex-row">
                                                    <TrashSimple className="h-7 w-7 mr-2 text-red-500" />
                                                    Delete Account
                                                </CardHeader>
                                                <CardContent className="overflow-hidden">
                                                    <p className="pb-4 text-gray-400">
                                                        This will delete all your account data,
                                                        including conversations, agents, and any
                                                        assets you{"'"}ve generated. Be sure to
                                                        export before you do this if you want to
                                                        keep your information.
                                                    </p>
                                                </CardContent>
                                                <CardFooter className="flex flex-wrap gap-4">
                                                    <AlertDialog>
                                                        <AlertDialogTrigger asChild>
                                                            <Button
                                                                variant="outline"
                                                                className="text-red-500 hover:text-red-600 hover:bg-red-50"
                                                            >
                                                                <TrashSimple className="h-5 w-5 mr-2" />
                                                                Delete Account
                                                            </Button>
                                                        </AlertDialogTrigger>
                                                        <AlertDialogContent>
                                                            <AlertDialogHeader>
                                                                <AlertDialogTitle>
                                                                    Are you absolutely sure?
                                                                </AlertDialogTitle>
                                                                <AlertDialogDescription>
                                                                    This action is irreversible.
                                                                    This will permanently delete
                                                                    your account and remove all your
                                                                    data from our servers.
                                                                </AlertDialogDescription>
                                                            </AlertDialogHeader>
                                                            <AlertDialogFooter>
                                                                <AlertDialogCancel>
                                                                    Cancel
                                                                </AlertDialogCancel>
                                                                <AlertDialogAction
                                                                    className="bg-red-500 hover:bg-red-600"
                                                                    onClick={async () => {
                                                                        try {
                                                                            const response =
                                                                                await fetch(
                                                                                    "/api/self",
                                                                                    {
                                                                                        method: "DELETE",
                                                                                    },
                                                                                );
                                                                            if (!response.ok)
                                                                                throw new Error(
                                                                                    "Failed to delete account",
                                                                                );

                                                                            toast({
                                                                                title: "Account Deleted",
                                                                                description:
                                                                                    "Your account has been successfully deleted.",
                                                                            });

                                                                            // Redirect to home page after successful deletion
                                                                            window.location.href =
                                                                                "/";
                                                                        } catch (error) {
                                                                            console.error(
                                                                                "Error deleting account:",
                                                                                error,
                                                                            );
                                                                            toast({
                                                                                title: "Error",
                                                                                description:
                                                                                    "Failed to delete account. Please try again or contact support.",
                                                                                variant:
                                                                                    "destructive",
                                                                            });
                                                                        }
                                                                    }}
                                                                >
                                                                    <TrashSimple className="h-5 w-5 mr-2" />
                                                                    Delete Account
                                                                </AlertDialogAction>
                                                            </AlertDialogFooter>
                                                        </AlertDialogContent>
                                                    </AlertDialog>
                                                </CardFooter>
                                            </Card>
                                        </div>
                                    </div>
                                </div>
                            </Suspense>
                        </div>
                    </div>
                </div>
            </SidebarInset>
        </SidebarProvider>
    );
}
