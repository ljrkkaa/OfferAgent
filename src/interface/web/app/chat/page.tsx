"use client";

import styles from "./chat.module.css";
import React, { Suspense, useCallback, useEffect, useRef, useState } from "react";
import useWebSocket from "react-use-websocket";

import ChatHistory from "../components/chatHistory/chatHistory";
import { useRouter, useSearchParams } from "next/navigation";
import Link from "next/link";
import Loading from "../components/loading/loading";

import { generateNewTitle, processMessageChunk } from "../common/chatFunctions";
import {
    fetchVaultActionCapability,
    listVaultActionBatches,
    upsertVaultActionBatch,
    type VaultActionBatch,
    type VaultActionCapability,
} from "../common/vaultActions";

import "katex/dist/katex.min.css";

import { Context, OnlineContext, StreamMessage } from "../components/chatMessage/chatMessage";
import { useIsMobileWidth, welcomeConsole } from "../common/utils";
import { AttachedFileText, ChatInputArea } from "../components/chatInputArea/chatInputArea";
import { useAuthenticatedData } from "../common/auth";
import { AgentData } from "@/app/common/agent";
import { ChatSessionActionMenu } from "../components/allConversations/allConversations";
import { SidebarInset, SidebarProvider, SidebarTrigger } from "@/components/ui/sidebar";
import { AppSidebar } from "../components/appSidebar/appSidebar";
import { Separator } from "@/components/ui/separator";
import { KhojLogoType } from "../components/logo/khojLogo";
import { Button } from "@/components/ui/button";
import { Joystick } from "@phosphor-icons/react";
import { useToast } from "@/components/ui/use-toast";
import { ChatSidebar } from "../components/chatSidebar/chatSidebar";

interface ChatBodyDataProps {
    setTitle: (title: string) => void;
    onConversationIdChange?: (conversationId: string) => void;
    setQueryToProcess: (query: string, attachments?: QueuedQueryAttachments) => void;
    streamedMessages: StreamMessage[];
    setStreamedMessages: (messages: StreamMessage[]) => void;
    setUploadedFiles: (files: AttachedFileText[] | undefined) => void;
    isMobileWidth?: boolean;
    isLoggedIn: boolean;
    setImages: (images: string[]) => void;
    setTriggeredAbort: (triggeredAbort: boolean, newMessage?: string) => void;
    isChatSideBarOpen: boolean;
    setIsChatSideBarOpen: (open: boolean) => void;
    isParentProcessing?: boolean;
    onRetryMessage?: (query: string, turnId?: string) => void;
    vaultActionBatches: VaultActionBatch[];
    vaultActionCapability: VaultActionCapability | null;
    onVaultActionBatchChange: (batch: VaultActionBatch) => void;
}

type QueuedQueryAttachments = {
    images?: string[];
    uploadedFiles?: AttachedFileText[];
};

type PendingChatRequest = {
    query: string;
    images: string[];
    uploadedFiles?: AttachedFileText[];
};

function ChatBodyData(props: ChatBodyDataProps) {
    const router = useRouter();
    const searchParams = useSearchParams();
    const conversationId =
        searchParams.get("conversationId") ||
        (typeof window !== "undefined"
            ? new URLSearchParams(window.location.search).get("conversationId")
            : null);
    const initialQuery =
        searchParams.get("q") ||
        (typeof window !== "undefined" ? new URLSearchParams(window.location.search).get("q") : "");
    const [message, setMessage] = useState("");
    const [images, setImages] = useState<string[]>([]);
    const [processingMessage, setProcessingMessage] = useState(false);
    const [agentMetadata, setAgentMetadata] = useState<AgentData | null>(null);
    const chatInputRef = useRef<HTMLTextAreaElement>(null);
    const processedInitialQueryRef = useRef<string | null>(null);

    const setQueryToProcess = props.setQueryToProcess;
    const onConversationIdChange = props.onConversationIdChange;
    const setParentImages = props.setImages;
    const setUploadedFiles = props.setUploadedFiles;
    const streamedMessages = props.streamedMessages;

    const chatHistoryCustomClassName = props.isMobileWidth ? "w-full" : "w-4/6";

    useEffect(() => {
        if (images.length > 0) {
            const encodedImages = images.map((image) => encodeURIComponent(image));
            setParentImages(encodedImages);
        }
    }, [images, setParentImages]);

    useEffect(() => {
        let encodedImages: string[] = [];
        let uploadedFiles: AttachedFileText[] | undefined;

        const storedImages = localStorage.getItem("images");
        if (storedImages) {
            const parsedImages: string[] = JSON.parse(storedImages);
            setImages(parsedImages);
            encodedImages = parsedImages.map((img: string) => encodeURIComponent(img));
            setParentImages(encodedImages);
            localStorage.removeItem("images");
        }

        const storedUploadedFiles = localStorage.getItem("uploadedFiles");

        if (storedUploadedFiles) {
            const parsedFiles = storedUploadedFiles ? JSON.parse(storedUploadedFiles) : [];
            uploadedFiles = [];
            for (const file of parsedFiles) {
                uploadedFiles.push({
                    name: file.name,
                    file_type: file.file_type,
                    content: file.content,
                    size: file.size,
                });
            }
            localStorage.removeItem("uploadedFiles");
            setUploadedFiles(uploadedFiles);
        }

        const storedMessage = localStorage.getItem("message");
        const messageToProcess = initialQuery || storedMessage;
        const initialQueryKey = `${conversationId}:${messageToProcess || ""}`;
        if (messageToProcess && processedInitialQueryRef.current !== initialQueryKey) {
            processedInitialQueryRef.current = initialQueryKey;
            localStorage.removeItem("message");
            setProcessingMessage(true);
            setQueryToProcess(messageToProcess, { images: encodedImages, uploadedFiles });

            if (initialQuery && typeof window !== "undefined") {
                const params = new URLSearchParams(window.location.search);
                params.delete("q");
                router.replace(`/chat?${params.toString()}`, { scroll: false });
            }
        }
    }, [
        setQueryToProcess,
        setParentImages,
        setUploadedFiles,
        conversationId,
        initialQuery,
        router,
    ]);

    const queueMessage = (nextMessage: string, nextImages: string[] = []) => {
        const imagesForMessage = nextImages.length > 0 ? nextImages : images;
        setMessage(nextMessage);
        setProcessingMessage(true);
        setQueryToProcess(nextMessage, {
            images: imagesForMessage.map((image) => encodeURIComponent(image)),
        });
    };

    useEffect(() => {
        if (conversationId) {
            onConversationIdChange?.(conversationId);
        }
    }, [conversationId, onConversationIdChange]);

    useEffect(() => {
        if (
            streamedMessages &&
            streamedMessages.length > 0 &&
            streamedMessages[streamedMessages.length - 1].completed
        ) {
            setProcessingMessage(false);
            setImages([]); // Reset images after processing
            setUploadedFiles(undefined); // Reset uploaded files after processing
        } else {
            setMessage("");
        }
    }, [streamedMessages, setUploadedFiles]);

    if (!conversationId) return <Loading />;

    return (
        <div className="flex flex-row h-full w-full">
            <div className="flex flex-col h-full w-full">
                <div className={false ? styles.chatBody : styles.chatBodyFull}>
                    <ChatHistory
                        conversationId={conversationId}
                        setTitle={props.setTitle}
                        setAgent={setAgentMetadata}
                        pendingMessage={processingMessage ? message : ""}
                        incomingMessages={props.streamedMessages}
                        setIncomingMessages={props.setStreamedMessages}
                        customClassName={chatHistoryCustomClassName}
                        setIsChatSideBarOpen={props.setIsChatSideBarOpen}
                        onRetryMessage={props.onRetryMessage}
                        vaultActionBatches={props.vaultActionBatches}
                        vaultActionCapability={props.vaultActionCapability}
                        onVaultActionBatchChange={props.onVaultActionBatchChange}
                    />
                </div>
                <div
                    className={`${styles.inputBox} print-hidden p-1 md:px-2 shadow-md bg-background align-middle items-center justify-center dark:bg-neutral-700 dark:border-0 dark:shadow-sm rounded-2xl md:rounded-xl h-fit ${chatHistoryCustomClassName} mr-auto ml-auto mt-auto`}
                >
                    <ChatInputArea
                        agentColor={agentMetadata?.color}
                        isLoggedIn={props.isLoggedIn}
                        sendMessage={queueMessage}
                        sendImage={(image) => setImages((prevImages) => [...prevImages, image])}
                        sendDisabled={props.isParentProcessing || false}
                        isMobileWidth={props.isMobileWidth}
                        setUploadedFiles={setUploadedFiles}
                        ref={chatInputRef}
                        setTriggeredAbort={props.setTriggeredAbort}
                    />
                </div>
            </div>
            <div className="print-hidden">
                <ChatSidebar
                    conversationId={conversationId}
                    isOpen={props.isChatSideBarOpen}
                    onOpenChange={props.setIsChatSideBarOpen}
                    isMobileWidth={props.isMobileWidth}
                    onSummarizeSelected={() => queueMessage("/summarize")}
                />
            </div>
        </div>
    );
}

export default function Chat() {
    const defaultTitle = "OfferAgent - Chat";
    const [title, setTitle] = useState(defaultTitle);
    const [conversationId, setConversationID] = useState<string | null>(null);
    const [messages, setMessages] = useState<StreamMessage[]>([]);
    const [queryToProcess, setQueryToProcess] = useState<string>("");
    const [processQuerySignal, setProcessQuerySignal] = useState(false);
    const [uploadedFiles, setUploadedFiles] = useState<AttachedFileText[] | undefined>(undefined);
    const [images, setImages] = useState<string[]>([]);
    const [pendingRequest, setPendingRequest] = useState<PendingChatRequest | null>(null);
    const [vaultActionCapability, setVaultActionCapability] =
        useState<VaultActionCapability | null>(null);
    const [vaultActionCapabilityLoaded, setVaultActionCapabilityLoaded] = useState(false);
    const [vaultActionBatches, setVaultActionBatches] = useState<VaultActionBatch[]>([]);

    const [triggeredAbort, setTriggeredAbort] = useState(false);
    const [interruptMessage, setInterruptMessage] = useState<string>("");
    const bufferRef = useRef("");
    const idleTimerRef = useRef<NodeJS.Timeout | null>(null);
    const sentRequestRef = useRef<PendingChatRequest | null>(null);

    const {
        data: authenticatedData,
        error: authenticationError,
        isLoading: authenticationLoading,
    } = useAuthenticatedData();
    const isMobileWidth = useIsMobileWidth();
    const [isChatSideBarOpen, setIsChatSideBarOpen] = useState(false);
    const [socketUrl, setSocketUrl] = useState<string | null>(null);
    // track whether we've already shown a toast for the current disconnect cycle to avoid duplicates
    const disconnectToastShownRef = useRef(false);
    // Track whether the websocket is closing due to an intentional action (page refresh/navigation or idle timeout)
    const intentionalCloseRef = useRef(false);

    const disconnectFromServer = useCallback(() => {
        if (idleTimerRef.current) {
            clearTimeout(idleTimerRef.current);
        }
        // Mark as intentional so onClose does not show transient network error banner
        intentionalCloseRef.current = true;
        setSocketUrl(null);
        console.log("WebSocket disconnected due to inactivity.");
    }, []);

    const resetIdleTimer = useCallback(() => {
        const idleTimeout = 10 * 60 * 1000; // 10 minutes
        if (idleTimerRef.current) {
            clearTimeout(idleTimerRef.current);
        }
        idleTimerRef.current = setTimeout(disconnectFromServer, idleTimeout);
    }, [disconnectFromServer]);

    const { toast } = useToast();
    const { sendMessage, lastMessage } = useWebSocket(socketUrl, {
        share: true,
        shouldReconnect: (closeEvent) => true,
        reconnectAttempts: 10,
        // reconnect using exponential backoff with jitter
        reconnectInterval: (attemptNumber) => {
            const baseDelay = 1000 * Math.pow(2, attemptNumber);
            const jitter = Math.random() * 1000; // Add jitter up to 1s
            return Math.min(baseDelay + jitter, 20000); // Cap backoff at 20s
        },
        onOpen: () => {
            console.log("WebSocket connection established.");
            resetIdleTimer();
            // Reset disconnect toast guard so future disconnects can notify again
            disconnectToastShownRef.current = false;
            // Reset intentional close flag after a successful open
            intentionalCloseRef.current = false;
        },
        onClose: (event) => {
            console.log("WebSocket connection closed.");
            if (idleTimerRef.current) {
                clearTimeout(idleTimerRef.current);
            }
            // Suppress notice if:
            //  - Intentional close (page refresh/navigation or idle management)
            //  - Normal closure (1000) or Going Away (1001 - typical on page reload)
            //  - No query to process
            if (
                !intentionalCloseRef.current &&
                event?.code !== 1000 &&
                event?.code !== 1001 &&
                queryToProcess
            ) {
                if (!disconnectToastShownRef.current) {
                    toast({
                        title: "Network issue",
                        description:
                            "Connection lost. Please check your network and try again when ready.",
                        variant: "destructive",
                        duration: 6000,
                    });
                    disconnectToastShownRef.current = true;
                }
            }
            // Mark any in-progress streamed message as completed so UI updates (stop spinner, show send icon)
            setMessages((prev) => {
                if (!prev || prev.length === 0) return prev;
                const newMessages = [...prev];
                const last = newMessages[newMessages.length - 1];
                if (last && !last.completed) {
                    last.completed = true;
                }
                return newMessages;
            });
            // Reset processing state so ChatInputArea send button reappears
            setProcessQuerySignal(false);
            setQueryToProcess("");
            setPendingRequest(null);
            sentRequestRef.current = null;
        },
        onError: (event) => {
            console.error("WebSocket error", event);
            // Perform same cleanup as onClose to avoid stuck UI
            setMessages((prev) => {
                if (!prev || prev.length === 0) return prev;
                const newMessages = [...prev];
                const last = newMessages[newMessages.length - 1];
                if (last && !last.completed) {
                    last.completed = true;
                }
                return newMessages;
            });
            setProcessQuerySignal(false);
            setQueryToProcess("");
            setPendingRequest(null);
            sentRequestRef.current = null;
            if (!intentionalCloseRef.current && !disconnectToastShownRef.current) {
                toast({
                    title: "Network error",
                    description:
                        "Connection lost. Please check your network and try again when ready.",
                    variant: "destructive",
                    duration: 5000,
                });
                disconnectToastShownRef.current = true;
            }
        },
    });

    const queueQueryToProcess = useCallback(
        (query: string, attachments?: QueuedQueryAttachments) => {
            setQueryToProcess(query);
            setPendingRequest(
                query
                    ? {
                          query,
                          images: attachments?.images ?? images,
                          uploadedFiles: attachments?.uploadedFiles ?? uploadedFiles,
                      }
                    : null,
            );
        },
        [images, uploadedFiles],
    );

    const handleVaultActionBatchChange = useCallback((batch: VaultActionBatch) => {
        setVaultActionBatches((current) => upsertVaultActionBatch(current, batch));
        setMessages((current) =>
            current.map((message) =>
                message.vaultActionBatch?.id === batch.id
                    ? { ...message, vaultActionBatch: batch }
                    : message,
            ),
        );
    }, []);

    // Handle page unload / refresh: mark intentional so we don't show a toast
    useEffect(() => {
        const handleBeforeUnload = () => {
            intentionalCloseRef.current = true;
        };
        window.addEventListener("beforeunload", handleBeforeUnload);
        return () => window.removeEventListener("beforeunload", handleBeforeUnload);
    }, []);

    useEffect(() => {
        if (lastMessage !== null) {
            resetIdleTimer();
            // Check if this is a control message (JSON) rather than a streaming event
            try {
                const controlMessage = JSON.parse(lastMessage.data);
                if (controlMessage.type === "interrupt_acknowledged") {
                    console.log("Interrupt acknowledged by server");
                    setProcessQuerySignal(false);
                    return;
                } else if (controlMessage.type === "interrupt_message_acknowledged") {
                    console.log("Interrupt message acknowledged by server");
                    setProcessQuerySignal(false);
                    return;
                } else if (controlMessage.error) {
                    console.error("WebSocket error:", controlMessage.error);
                    setMessages((prevMessages) => {
                        if (!prevMessages.length) return prevMessages;
                        const newMessages = [...prevMessages];
                        const currentMessage = { ...newMessages[newMessages.length - 1] };
                        currentMessage.rawResponse =
                            controlMessage.error || "OfferAgent failed to generate a response.";
                        currentMessage.completed = true;
                        newMessages[newMessages.length - 1] = currentMessage;
                        return newMessages;
                    });
                    toast({
                        title: "OfferAgent could not respond",
                        description: String(controlMessage.error),
                        variant: "destructive",
                        duration: 6000,
                    });
                    setProcessQuerySignal(false);
                    setPendingRequest(null);
                    sentRequestRef.current = null;
                    return;
                }
            } catch {
                // Not a JSON control message, process as streaming event
            }

            const eventDelimiter = "␃🔚␗";
            bufferRef.current += lastMessage.data;

            let newEventIndex;
            while ((newEventIndex = bufferRef.current.indexOf(eventDelimiter)) !== -1) {
                const eventChunk = bufferRef.current.slice(0, newEventIndex);
                bufferRef.current = bufferRef.current.slice(newEventIndex + eventDelimiter.length);
                if (eventChunk) {
                    setMessages((prevMessages) => {
                        const newMessages = [...prevMessages];
                        const currentMessage = newMessages[newMessages.length - 1];
                        if (!currentMessage || currentMessage.completed) {
                            return prevMessages;
                        }

                        const { context, onlineContext } = processMessageChunk(
                            eventChunk,
                            currentMessage,
                            currentMessage.context || [],
                            currentMessage.onlineContext || {},
                        );

                        // Update the current message with the new reference data
                        currentMessage.context = context;
                        currentMessage.onlineContext = onlineContext;

                        if (currentMessage.completed) {
                            setQueryToProcess("");
                            setPendingRequest(null);
                            sentRequestRef.current = null;
                            setProcessQuerySignal(false);
                            setImages([]);
                            if (conversationId) generateNewTitle(conversationId, setTitle);
                        }

                        return newMessages;
                    });
                }
            }
        }
    }, [lastMessage, setMessages, conversationId, resetIdleTimer, toast]);

    useEffect(() => {
        welcomeConsole();
    }, []);

    useEffect(() => {
        let cancelled = false;
        if (authenticationLoading) return;
        if (authenticationError || !authenticatedData) {
            setVaultActionCapability(null);
            setVaultActionCapabilityLoaded(true);
            return;
        }

        setVaultActionCapabilityLoaded(false);
        fetchVaultActionCapability()
            .then((capability) => {
                if (!cancelled) setVaultActionCapability(capability);
            })
            .catch((error) => {
                console.error("Failed to load Web VaultAction capability", error);
                if (!cancelled) setVaultActionCapability(null);
            })
            .finally(() => {
                if (!cancelled) setVaultActionCapabilityLoaded(true);
            });

        return () => {
            cancelled = true;
        };
    }, [authenticatedData, authenticationError, authenticationLoading]);

    useEffect(() => {
        let cancelled = false;
        setVaultActionBatches([]);
        if (!authenticatedData || !conversationId) return;

        listVaultActionBatches(conversationId)
            .then((batches) => {
                if (!cancelled) setVaultActionBatches(batches);
            })
            .catch((error) => {
                console.error("Failed to recover Web VaultAction batches", error);
            });

        return () => {
            cancelled = true;
        };
    }, [authenticatedData, conversationId]);

    const handleTriggeredAbort = (value: boolean, newMessage?: string) => {
        if (value) {
            setInterruptMessage(newMessage || "");
        }
        setTriggeredAbort(value);
    };

    useEffect(() => {
        if (triggeredAbort) {
            sendMessage(
                JSON.stringify({
                    type: "interrupt",
                    query: interruptMessage,
                }),
            );
            console.log("Sent interrupt message via WebSocket:", interruptMessage);

            // Mark the last message as completed
            setMessages((prevMessages) => {
                const newMessages = [...prevMessages];
                const currentMessage = newMessages[newMessages.length - 1];
                if (currentMessage) currentMessage.completed = true;
                return newMessages;
            });

            // Set the interrupt message as the new query being processed
            queueQueryToProcess(interruptMessage, { images: [], uploadedFiles: undefined });
            setTriggeredAbort(false); // Always set to false after processing
            setInterruptMessage("");
        }
    }, [triggeredAbort, sendMessage, interruptMessage, queueQueryToProcess]);

    useEffect(() => {
        if (pendingRequest) {
            const newStreamMessage: StreamMessage = {
                rawResponse: "",
                trainOfThought: [],
                context: [],
                onlineContext: {},
                completed: false,
                timestamp: new Date().toISOString(),
                rawQuery: pendingRequest.query,
                images: pendingRequest.images,
                queryFiles: pendingRequest.uploadedFiles,
            };
            setMessages((prevMessages) => [...prevMessages, newStreamMessage]);
            setProcessQuerySignal(true);
        }
    }, [pendingRequest]);

    useEffect(() => {
        if (processQuerySignal) {
            if (!pendingRequest || !conversationId) {
                setProcessQuerySignal(false);
                return;
            }

            if (sentRequestRef.current === pendingRequest) {
                return;
            }
            sentRequestRef.current = pendingRequest;

            localStorage.removeItem("message");

            // Re-establish WebSocket connection if disconnected
            resetIdleTimer();
            if (!socketUrl) {
                const protocol = window.location.protocol === "https:" ? "wss:" : "ws:";
                const wsUrl = `${protocol}//${window.location.host}/api/chat/ws?client=web`;
                setSocketUrl(wsUrl);
            }

            const chatAPIBody = {
                q: pendingRequest.query,
                conversation_id: conversationId,
                stream: true,
                client_capabilities: {
                    vaultActions: vaultActionCapability?.enabled === true,
                },
                ...(pendingRequest.images.length > 0 && { images: pendingRequest.images }),
                ...(pendingRequest.uploadedFiles && { files: pendingRequest.uploadedFiles }),
            };

            sendMessage(JSON.stringify(chatAPIBody));
        }
    }, [
        processQuerySignal,
        pendingRequest,
        conversationId,
        resetIdleTimer,
        socketUrl,
        sendMessage,
        vaultActionCapability?.enabled,
    ]);

    useEffect(() => {
        if (!conversationId) return;

        const protocol = window.location.protocol === "https:" ? "wss:" : "ws:";
        const wsUrl = `${protocol}//${window.location.host}/api/chat/ws?client=web`;
        setSocketUrl(wsUrl);

        return () => {
            if (idleTimerRef.current) {
                clearTimeout(idleTimerRef.current);
            }
        };
    }, [conversationId]);

    const handleConversationIdChange = (newConversationId: string) => {
        setConversationID(newConversationId);
    };

    const handleRetryMessage = async (query: string, turnId?: string) => {
        if (!query) {
            console.warn("No query provided for retry");
            return false;
        }

        // If we have a turnId, delete the old turn first
        if (turnId) {
            try {
                const response = await fetch("/api/chat/conversation/message", {
                    method: "DELETE",
                    headers: {
                        "Content-Type": "application/json",
                    },
                    body: JSON.stringify({
                        conversation_id: conversationId,
                        turn_id: turnId,
                    }),
                });
                if (!response.ok) {
                    throw new Error(
                        (await response.text()) || "Failed to delete message for retry",
                    );
                }
                setMessages((prevMessages) => prevMessages.filter((msg) => msg.turnId !== turnId));
            } catch (error) {
                console.error("Failed to delete message for retry:", error);
                return false;
            }
        }

        // Re-send the original query
        queueQueryToProcess(query);
        return true;
    };

    if (authenticationLoading || !vaultActionCapabilityLoaded) return <Loading />;

    return (
        <SidebarProvider>
            <div className="print-hidden">
                <AppSidebar conversationId={conversationId || ""} />
            </div>
            <SidebarInset>
                <header className="flex h-16 shrink-0 items-center gap-2 border-b px-4 print-hidden">
                    <SidebarTrigger className="-ml-1" />
                    <Separator orientation="vertical" className="mr-2 h-4" />
                    {conversationId && (
                        <div
                            className={`${styles.chatTitleWrapper} text-nowrap text-ellipsis overflow-hidden max-w-screen-md grid items-top font-bold mx-2 md:mr-8 col-auto h-fit`}
                        >
                            {isMobileWidth ? (
                                <Link className="p-0 no-underline" href="/">
                                    <KhojLogoType className="h-auto w-32 max-w-full" />
                                </Link>
                            ) : (
                                title && (
                                    <>
                                        <h2
                                            className={`text-lg text-ellipsis whitespace-nowrap overflow-x-hidden mr-4`}
                                        >
                                            {title}
                                        </h2>
                                        <ChatSessionActionMenu
                                            conversationId={conversationId}
                                            setTitle={setTitle}
                                            sizing={"md"}
                                        />
                                    </>
                                )
                            )}
                        </div>
                    )}
                    <div className="flex justify-end items-start gap-2 text-sm ml-auto">
                        <Button
                            variant="ghost"
                            size="icon"
                            className="h-12 w-12 data-[state=open]:bg-accent print-hidden"
                            onClick={() => setIsChatSideBarOpen(!isChatSideBarOpen)}
                        >
                            <Joystick className="w-6 h-6" />
                        </Button>
                    </div>
                </header>
                <div className={`${styles.main} ${styles.chatLayout}`}>
                    <title>
                        {`${defaultTitle}${!!title && title !== defaultTitle ? `: ${title}` : ""}`}
                    </title>
                    <div className={styles.chatBox}>
                        <div className={styles.chatBoxBody}>
                            <Suspense fallback={<Loading />}>
                                <ChatBodyData
                                    isLoggedIn={authenticatedData ? true : false}
                                    streamedMessages={messages}
                                    setStreamedMessages={setMessages}
                                    setTitle={setTitle}
                                    setQueryToProcess={queueQueryToProcess}
                                    setUploadedFiles={setUploadedFiles}
                                    isMobileWidth={isMobileWidth}
                                    onConversationIdChange={handleConversationIdChange}
                                    setImages={setImages}
                                    setTriggeredAbort={handleTriggeredAbort}
                                    isChatSideBarOpen={isChatSideBarOpen}
                                    setIsChatSideBarOpen={setIsChatSideBarOpen}
                                    isParentProcessing={processQuerySignal}
                                    onRetryMessage={handleRetryMessage}
                                    vaultActionBatches={vaultActionBatches}
                                    vaultActionCapability={vaultActionCapability}
                                    onVaultActionBatchChange={handleVaultActionBatchChange}
                                />
                            </Suspense>
                        </div>
                    </div>
                </div>
            </SidebarInset>
        </SidebarProvider>
    );
}
