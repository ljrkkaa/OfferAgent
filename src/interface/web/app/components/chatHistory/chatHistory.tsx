"use client";

import styles from "./chatHistory.module.css";
import { useRef, useEffect, useState, useCallback } from "react";
import { motion, AnimatePresence } from "framer-motion";

import ChatMessage, {
    ChatHistoryData,
    StreamMessage,
    TrainOfThought,
    TrainOfThoughtObject,
} from "../chatMessage/chatMessage";

import { ScrollArea } from "@/components/ui/scroll-area";

import { InlineLoading } from "../loading/loading";

import { Lightbulb, ArrowDown, CaretDown, CaretUp } from "@phosphor-icons/react";

import AgentProfileCard from "../profileCard/profileCard";
import { getIconFromIconName } from "@/app/common/iconUtils";
import { AgentData } from "@/app/common/agent";
import React from "react";
import { useIsMobileWidth } from "@/app/common/utils";
import { Button } from "@/components/ui/button";
import { KhojLogo } from "../logo/khojLogo";
import {
    listVaultActionBatches,
    vaultActionBatchForTurn,
    type VaultActionBatch,
    type VaultActionCapability,
} from "@/app/common/vaultActions";
import VaultActionReview from "../chatMessage/vaultActionReview";

interface ChatResponse {
    status: string;
    response: ChatHistoryData;
}

function isChatHistoryData(data: unknown): data is ChatHistoryData {
    return (
        typeof data === "object" &&
        data !== null &&
        Array.isArray((data as ChatHistoryData).chat) &&
        ((data as ChatHistoryData).agent === null ||
            typeof (data as ChatHistoryData).agent === "object") &&
        typeof (data as ChatHistoryData).conversation_id === "string" &&
        ((data as ChatHistoryData).slug === null ||
            typeof (data as ChatHistoryData).slug === "string") &&
        typeof (data as ChatHistoryData).is_owner === "boolean"
    );
}

interface ChatHistoryProps {
    conversationId: string;
    setTitle: (title: string) => void;
    pendingMessage?: string;
    incomingMessages?: StreamMessage[];
    setIncomingMessages?: (incomingMessages: StreamMessage[]) => void;
    setAgent: (agent: AgentData) => void;
    customClassName?: string;
    setIsChatSideBarOpen?: (isOpen: boolean) => void;
    setIsOwner?: (isOwner: boolean) => void;
    onRetryMessage?: (query: string, turnId?: string) => Promise<boolean> | boolean | void;
    vaultActionBatches: VaultActionBatch[];
    vaultActionCapability: VaultActionCapability | null;
    onVaultActionBatchChange: (batch: VaultActionBatch) => void;
}

interface TrainOfThoughtComponentProps {
    trainOfThought: string[] | TrainOfThoughtObject[];
    lastMessage: boolean;
    agentColor: string;
    keyId: string;
    completed?: boolean;
}

function TrainOfThoughtComponent(props: TrainOfThoughtComponentProps) {
    const [collapsed, setCollapsed] = useState(props.completed);
    const trainOfThoughtEntries: TrainOfThoughtObject[] = (props.trainOfThought || []).map(
        (entry) => (typeof entry === "string" ? { type: "text", data: entry } : entry),
    );

    const variants = {
        open: {
            height: "auto",
            opacity: 1,
            transition: { duration: 0.3, ease: "easeOut" },
        },
        closed: {
            height: 0,
            opacity: 0,
            transition: { duration: 0.3, ease: "easeIn" },
        },
    } as const;

    useEffect(() => {
        if (props.completed) {
            setCollapsed(true);
        }
    }, [props.completed]);

    return (
        <div
            className={`${!collapsed ? styles.trainOfThought + " border" : ""} rounded-lg`}
            key={props.keyId}
        >
            {!props.completed && <InlineLoading className="float-right" />}
            {props.completed &&
                (collapsed ? (
                    <Button
                        className="w-fit text-left justify-start content-start text-xs"
                        onClick={() => setCollapsed(false)}
                        variant="ghost"
                        size="sm"
                    >
                        Thought Process <CaretDown size={16} className="ml-1" />
                    </Button>
                ) : (
                    <Button
                        className="w-fit text-left justify-start content-start text-xs p-0 h-fit"
                        onClick={() => setCollapsed(true)}
                        variant="ghost"
                        size="sm"
                    >
                        Close <CaretUp size={16} className="ml-1" />
                    </Button>
                ))}
            <AnimatePresence initial={false}>
                {!collapsed && (
                    <motion.div initial="closed" animate="open" exit="closed" variants={variants}>
                        {trainOfThoughtEntries.map((entry, index) => (
                            <TrainOfThought
                                key={`train-text-${index}-${entry.data.length}`}
                                message={entry.data}
                                primary={
                                    index === trainOfThoughtEntries.length - 1 &&
                                    props.lastMessage &&
                                    !props.completed
                                }
                                agentColor={props.agentColor}
                            />
                        ))}
                    </motion.div>
                )}
            </AnimatePresence>
        </div>
    );
}

export default function ChatHistory(props: ChatHistoryProps) {
    const {
        conversationId,
        incomingMessages,
        setAgent,
        setIsChatSideBarOpen,
        setIsOwner,
        setTitle,
    } = props;
    const [data, setData] = useState<ChatHistoryData | null>(null);
    const [currentPage, setCurrentPage] = useState(0);
    const [hasMoreMessages, setHasMoreMessages] = useState(true);
    const [currentTurnId, setCurrentTurnId] = useState<string | null>(null);
    const sentinelRef = useRef<HTMLDivElement | null>(null);
    const scrollAreaRef = useRef<HTMLDivElement | null>(null);
    const scrollableContentWrapperRef = useRef<HTMLDivElement | null>(null);
    const latestUserMessageRef = useRef<HTMLDivElement | null>(null);
    const latestFetchedMessageRef = useRef<HTMLDivElement | null>(null);

    const [incompleteIncomingMessageIndex, setIncompleteIncomingMessageIndex] = useState<
        number | null
    >(null);
    const [fetchingData, setFetchingData] = useState(false);
    const [historyError, setHistoryError] = useState<string | null>(null);
    const [isNearBottom, setIsNearBottom] = useState(true);
    const isMobileWidth = useIsMobileWidth();
    const scrollAreaSelector = "[data-radix-scroll-area-viewport]";
    const fetchMessageCount = 10;
    const hasStartingMessage = Boolean(
        props.pendingMessage || incomingMessages?.some((message) => !message.completed),
    );

    const scrollToBottom = useCallback(
        (instant: boolean = false) => {
            const scrollAreaEl =
                scrollAreaRef.current?.querySelector<HTMLElement>(scrollAreaSelector);
            requestAnimationFrame(() => {
                scrollAreaEl?.scrollTo({
                    top: scrollAreaEl.scrollHeight,
                    behavior: instant ? "auto" : "smooth",
                });
            });
            // Optimistically set, the scroll listener will verify
            if (
                instant ||
                (scrollAreaEl &&
                    scrollAreaEl.scrollHeight -
                        (scrollAreaEl.scrollTop + scrollAreaEl.clientHeight) <
                        5)
            ) {
                setIsNearBottom(true);
            }
        },
        [scrollAreaSelector],
    );

    const adjustScrollPosition = useCallback(() => {
        const scrollAreaEl = scrollAreaRef.current?.querySelector<HTMLElement>(scrollAreaSelector);
        requestAnimationFrame(() => {
            // Snap scroll position to the latest fetched message ref
            latestFetchedMessageRef.current?.scrollIntoView({ behavior: "auto", block: "start" });
            // Now scroll up smoothly to render user scroll action
            scrollAreaEl?.scrollBy({ behavior: "smooth", top: -150 });
        });
    }, [scrollAreaSelector]);

    const fetchMoreMessages = useCallback(
        (currentPage: number) => {
            if (!hasMoreMessages || fetchingData) return;
            const nextPage = currentPage + 1;
            const maxMessagesToFetch = nextPage * fetchMessageCount;
            let conversationFetchURL = "";

            if (!conversationId) {
                return;
            }
            conversationFetchURL = `/api/chat/history?client=web&conversation_id=${encodeURIComponent(conversationId)}&n=${maxMessagesToFetch}`;

            fetch(conversationFetchURL)
                .then((response) => {
                    if (!response.ok) {
                        throw new Error(
                            `Failed to fetch chat history with status ${response.status}`,
                        );
                    }
                    return response.json();
                })
                .then((chatData: ChatResponse) => {
                    setHistoryError(null);
                    if (chatData.status !== "ok" || !isChatHistoryData(chatData.response)) {
                        throw new Error("Invalid chat history response");
                    }
                    setTitle(chatData.response.slug || "New Conversation");
                    setIsOwner && setIsOwner(chatData?.response?.is_owner);
                    if (
                        chatData &&
                        chatData.response &&
                        chatData.response.chat &&
                        chatData.response.chat.length > 0
                    ) {
                        setCurrentPage(
                            Math.ceil(chatData.response.chat.length / fetchMessageCount),
                        );
                        if (chatData.response.chat.length === data?.chat.length) {
                            setHasMoreMessages(false);
                            setFetchingData(false);
                            return;
                        }
                        if (chatData.response.agent) {
                            setAgent(chatData.response.agent);
                        }
                        setData(chatData.response);
                        setFetchingData(false);
                        if (currentPage === 0) {
                            scrollToBottom(true);
                        } else {
                            adjustScrollPosition();
                        }
                    } else {
                        const chatMetadata = {
                            chat: [],
                            agent: chatData.response.agent,
                            conversation_id: chatData.response.conversation_id,
                            slug: chatData.response.slug,
                            is_owner: chatData.response.is_owner,
                        };
                        if (chatData.response.agent) {
                            setAgent(chatData.response.agent);
                        }
                        setData(chatMetadata);
                        if (setIsChatSideBarOpen && !hasStartingMessage) {
                            setIsChatSideBarOpen(true);
                        }

                        setHasMoreMessages(false);
                        setFetchingData(false);
                    }
                })
                .catch((err) => {
                    console.error(err);
                    setHistoryError("Unable to load this conversation.");
                    setHasMoreMessages(false);
                    setFetchingData(false);
                });
        },
        [
            adjustScrollPosition,
            conversationId,
            data?.chat.length,
            fetchMessageCount,
            fetchingData,
            hasMoreMessages,
            hasStartingMessage,
            scrollToBottom,
            setAgent,
            setIsChatSideBarOpen,
            setIsOwner,
            setTitle,
        ],
    );

    useEffect(() => {
        const scrollAreaEl = scrollAreaRef.current?.querySelector<HTMLElement>(scrollAreaSelector);
        if (!scrollAreaEl) return;

        const detectIsNearBottom = () => {
            const { scrollTop, scrollHeight, clientHeight } = scrollAreaEl;
            const bottomThreshold = 50; // pixels from bottom
            const distanceFromBottom = scrollHeight - (scrollTop + clientHeight);
            const isNearBottom = distanceFromBottom <= bottomThreshold;
            setIsNearBottom(isNearBottom);
        };

        scrollAreaEl.addEventListener("scroll", detectIsNearBottom);
        detectIsNearBottom(); // Initial check
        return () => scrollAreaEl.removeEventListener("scroll", detectIsNearBottom);
    }, [scrollAreaRef]);

    // Auto scroll while incoming message is streamed
    useEffect(() => {
        if (incomingMessages && incomingMessages.length > 0 && isNearBottom) {
            scrollToBottom(true);
        }
    }, [incomingMessages, isNearBottom, scrollToBottom]);

    // ResizeObserver to handle content height changes (e.g., images loading)
    useEffect(() => {
        const contentWrapper = scrollableContentWrapperRef.current;
        const scrollViewport =
            scrollAreaRef.current?.querySelector<HTMLElement>(scrollAreaSelector);

        if (!contentWrapper || !scrollViewport) return;

        const observer = new ResizeObserver(() => {
            // Check current scroll position to decide if auto-scroll is warranted
            const { scrollTop, scrollHeight, clientHeight } = scrollViewport;
            const bottomThreshold = 50;
            const currentlyNearBottom =
                scrollHeight - (scrollTop + clientHeight) <= bottomThreshold;

            if (currentlyNearBottom) {
                // Only auto-scroll if there are incoming messages being processed
                if (incomingMessages && incomingMessages.length > 0) {
                    const lastMessage = incomingMessages[incomingMessages.length - 1];
                    // If the last message is not completed, or it just completed (indicated by incompleteIncomingMessageIndex still being set)
                    if (
                        !lastMessage.completed ||
                        (lastMessage.completed && incompleteIncomingMessageIndex !== null)
                    ) {
                        scrollToBottom(true); // Use instant scroll
                    }
                }
            }
        });

        observer.observe(contentWrapper);
        return () => observer.disconnect();
    }, [incomingMessages, incompleteIncomingMessageIndex, scrollAreaRef, scrollToBottom]); // Dependencies

    // Scroll to most recent user message after the first page of chat messages is loaded.
    useEffect(() => {
        if (data && data.chat && data.chat.length > 0 && currentPage < 2) {
            requestAnimationFrame(() => {
                latestUserMessageRef.current?.scrollIntoView({ behavior: "auto", block: "start" });
            });
        }
    }, [data, currentPage]);

    useEffect(() => {
        if (!hasMoreMessages || fetchingData) return;

        // TODO: A future optimization would be to add a time to delay to re-enabling the intersection observer.
        const observer = new IntersectionObserver(
            (entries) => {
                if (entries[0].isIntersecting && hasMoreMessages) {
                    setFetchingData(true);
                    fetchMoreMessages(currentPage);
                }
            },
            { threshold: 1.0 },
        );

        if (sentinelRef.current) {
            observer.observe(sentinelRef.current);
        }

        return () => observer.disconnect();
    }, [hasMoreMessages, currentPage, fetchingData, fetchMoreMessages]);

    useEffect(() => {
        setHasMoreMessages(true);
        setFetchingData(false);
        setCurrentPage(0);
        setData(null);
        setHistoryError(null);
    }, [props.conversationId]);

    useEffect(() => {
        if (incomingMessages) {
            const lastMessage = incomingMessages[incomingMessages.length - 1];
            if (lastMessage && !lastMessage.completed) {
                setIncompleteIncomingMessageIndex(incomingMessages.length - 1);
                setTitle(lastMessage.rawQuery);
                // Store the turnId when we get it
                if (lastMessage.turnId) {
                    setCurrentTurnId(lastMessage.turnId);
                }
            }
        }
    }, [incomingMessages, setTitle]);

    function constructAgentName() {
        return "OfferAgent";
    }

    function constructAgentPersona() {
        if (!data || !data.agent) {
            return "Your local knowledge agent.";
        }

        if (!data.agent?.persona) {
            return "Your local knowledge agent.";
        }

        return data.agent?.persona;
    }

    const handleDeleteMessage = (turnId?: string) => {
        if (!turnId) return;

        setData((prevData) => {
            if (!prevData || !turnId) return prevData;
            return {
                ...prevData,
                chat: prevData.chat.filter((msg) => msg.turnId !== turnId),
            };
        });

        // Update incoming messages if they exist
        if (props.incomingMessages && props.setIncomingMessages) {
            props.setIncomingMessages(
                props.incomingMessages.filter((msg) => msg.turnId !== turnId),
            );
        }

        listVaultActionBatches(props.conversationId)
            .then((batches) => batches.forEach(props.onVaultActionBatchChange))
            .catch((error) =>
                console.error(
                    "Failed to refresh VaultAction batches after message deletion",
                    error,
                ),
            );
    };

    const handleRetryMessage = async (query: string, turnId?: string) => {
        if (!query) return false;

        const retryStarted = await props.onRetryMessage?.(query, turnId);
        if (retryStarted !== false && turnId) {
            handleDeleteMessage(turnId);
        }
        return retryStarted !== false;
    };

    if (!props.conversationId) {
        return null;
    }

    const assistantTurnIds = new Set([
        ...(data?.chat
            .filter((message) => message.by === "khoj")
            .map((message) => message.turnId)
            .filter((turnId): turnId is string => Boolean(turnId)) ?? []),
        ...(props.incomingMessages
            ?.map((message) => message.turnId)
            .filter((turnId): turnId is string => Boolean(turnId)) ?? []),
    ]);
    const unmatchedVaultActionBatches = props.vaultActionBatches.filter(
        (batch) =>
            ["pending", "applying", "conflict", "failed", "manual_review_required"].includes(
                batch.status,
            ) && !assistantTurnIds.has(batch.turn_id),
    );

    return (
        <ScrollArea
            className={`
            h-[calc(100svh-theme(spacing.44))]
            sm:h-[calc(100svh-theme(spacing.44))]
            md:h-[calc(100svh-theme(spacing.44))]
            lg:h-[calc(100svh-theme(spacing.44))]
        `}
            ref={scrollAreaRef}
        >
            <div ref={scrollableContentWrapperRef}>
                {/* Print-only header with conversation info */}
                <div className="print-only-header">
                    <div className="print-header-content">
                        <div className="print-header-left">
                            <KhojLogo className="print-logo" />
                        </div>
                        <div className="print-header-right">
                            <h1>{data?.slug || "Conversation with OfferAgent"}</h1>
                            <div className="conversation-meta">
                                <p>
                                    <strong>Agent:</strong> {constructAgentName()}
                                </p>
                            </div>
                        </div>
                    </div>
                    <hr />
                </div>

                <div className={`${styles.chatHistory} ${props.customClassName}`}>
                    <div ref={sentinelRef} style={{ height: "1px" }}>
                        {fetchingData && <InlineLoading className="opacity-50" />}
                    </div>
                    {historyError && (
                        <div className="mx-4 my-3 rounded-md border border-rose-200 bg-rose-50 p-3 text-sm text-rose-700 dark:border-rose-900 dark:bg-rose-950 dark:text-rose-200">
                            {historyError}
                        </div>
                    )}
                    {data &&
                        data.chat &&
                        data.chat.map((chatMessage, index) => (
                            <React.Fragment key={`chatMessage-${index}`}>
                                {chatMessage.trainOfThought && chatMessage.by === "khoj" && (
                                    <TrainOfThoughtComponent
                                        trainOfThought={chatMessage.trainOfThought}
                                        lastMessage={false}
                                        agentColor={data?.agent?.color || "orange"}
                                        key={`${index}trainOfThought`}
                                        keyId={`${index}trainOfThought`}
                                        completed={true}
                                    />
                                )}
                                <ChatMessage
                                    key={`${index}fullHistory`}
                                    ref={
                                        // attach ref to the second last message to handle scroll on page load
                                        index === data.chat.length - 2
                                            ? latestUserMessageRef
                                            : // attach ref to the newest fetched message to handle scroll on fetch
                                              // note: stabilize index selection against last page having less messages than fetchMessageCount
                                              index ===
                                                data.chat.length -
                                                    (currentPage - 1) * fetchMessageCount
                                              ? latestFetchedMessageRef
                                              : null
                                    }
                                    isMobileWidth={isMobileWidth}
                                    chatMessage={{
                                        ...chatMessage,
                                        vaultActionBatch:
                                            chatMessage.by === "khoj"
                                                ? vaultActionBatchForTurn(
                                                      props.vaultActionBatches,
                                                      chatMessage.turnId,
                                                  )
                                                : undefined,
                                    }}
                                    customClassName="fullHistory"
                                    borderLeftColor={`${data?.agent?.color}-500`}
                                    isLastMessage={index === data.chat.length - 1}
                                    onDeleteMessage={handleDeleteMessage}
                                    onRetryMessage={handleRetryMessage}
                                    conversationId={props.conversationId}
                                    vaultActionCapability={props.vaultActionCapability}
                                    onVaultActionBatchChange={props.onVaultActionBatchChange}
                                />
                            </React.Fragment>
                        ))}
                    {props.incomingMessages &&
                        props.incomingMessages.map((message, index) => {
                            const messageTurnId = message.turnId ?? currentTurnId ?? undefined;
                            return (
                                <React.Fragment key={`incomingMessage${index}`}>
                                    <ChatMessage
                                        key={`${index}outgoing`}
                                        isMobileWidth={isMobileWidth}
                                        chatMessage={{
                                            message: message.rawQuery,
                                            context: [],
                                            onlineContext: {},
                                            created: message.timestamp,
                                            by: "you",
                                            automationId: "",
                                            images: message.images,
                                            conversationId: props.conversationId,
                                            turnId: messageTurnId,
                                            queryFiles: message.queryFiles,
                                        }}
                                        customClassName="fullHistory"
                                        borderLeftColor={`${data?.agent?.color}-500`}
                                        onDeleteMessage={handleDeleteMessage}
                                        onRetryMessage={handleRetryMessage}
                                        conversationId={props.conversationId}
                                        turnId={messageTurnId}
                                    />
                                    {message.trainOfThought &&
                                        message.trainOfThought.length > 0 && (
                                            <TrainOfThoughtComponent
                                                trainOfThought={message.trainOfThought}
                                                lastMessage={
                                                    index === incompleteIncomingMessageIndex
                                                }
                                                agentColor={data?.agent?.color || "orange"}
                                                key={`${index}trainOfThought-${message.trainOfThought.length}-${message.trainOfThought.map((t) => t.length).join("-")}`}
                                                keyId={`${index}trainOfThought`}
                                                completed={message.completed}
                                            />
                                        )}
                                    <ChatMessage
                                        key={`${index}incoming`}
                                        isMobileWidth={isMobileWidth}
                                        chatMessage={{
                                            message: message.rawResponse,
                                            context: message.context,
                                            onlineContext: message.onlineContext,
                                            created: message.timestamp,
                                            by: "khoj",
                                            automationId: "",
                                            rawQuery: message.rawQuery,
                                            intent: {
                                                type: message.intentType || "",
                                                query: message.rawQuery,
                                                "memory-type": "",
                                                "inferred-queries": message.inferredQueries || [],
                                            },
                                            conversationId: props.conversationId,
                                            turnId: messageTurnId,
                                            vaultActionBatch:
                                                message.vaultActionBatch ??
                                                vaultActionBatchForTurn(
                                                    props.vaultActionBatches,
                                                    messageTurnId,
                                                ),
                                        }}
                                        conversationId={props.conversationId}
                                        turnId={messageTurnId}
                                        onDeleteMessage={handleDeleteMessage}
                                        onRetryMessage={handleRetryMessage}
                                        customClassName="fullHistory"
                                        borderLeftColor={`${data?.agent?.color}-500`}
                                        isLastMessage={index === props.incomingMessages!.length - 1}
                                        vaultActionCapability={props.vaultActionCapability}
                                        onVaultActionBatchChange={props.onVaultActionBatchChange}
                                    />
                                </React.Fragment>
                            );
                        })}
                    {unmatchedVaultActionBatches.length > 0 && (
                        <section className="mx-2 my-4" aria-label="恢复的文件修改批次">
                            <p className="mb-2 text-sm font-medium text-amber-700 dark:text-amber-300">
                                以下文件修改没有可见的助手消息，仍需单独处理：
                            </p>
                            {unmatchedVaultActionBatches.map((batch) => (
                                <VaultActionReview
                                    key={batch.id}
                                    batch={batch}
                                    capability={props.vaultActionCapability}
                                    onBatchChange={props.onVaultActionBatchChange}
                                />
                            ))}
                        </section>
                    )}
                    {props.pendingMessage && (
                        <ChatMessage
                            key={`pendingMessage-${props.pendingMessage.length}`}
                            isMobileWidth={isMobileWidth}
                            chatMessage={{
                                message: props.pendingMessage,
                                context: [],
                                onlineContext: {},
                                created: new Date().getTime().toString(),
                                by: "you",
                                automationId: "",
                                conversationId: props.conversationId,
                                turnId: undefined,
                            }}
                            conversationId={props.conversationId}
                            onDeleteMessage={handleDeleteMessage}
                            onRetryMessage={handleRetryMessage}
                            customClassName="fullHistory"
                            borderLeftColor={`${data?.agent?.color ?? "orange"}-500`}
                            isLastMessage={true}
                        />
                    )}
                    {data && (
                        <div className={`${styles.agentIndicator} pb-4`}>
                            <div className="relative group mx-2 cursor-pointer">
                                <AgentProfileCard
                                    name={constructAgentName()}
                                    avatar={
                                        getIconFromIconName(
                                            data.agent?.icon ?? "Lightbulb",
                                            data.agent?.color ?? "orange",
                                        ) || <Lightbulb />
                                    }
                                    description={constructAgentPersona()}
                                />
                            </div>
                        </div>
                    )}
                </div>
                <div className={`${props.customClassName} fixed bottom-[20%] z-10`}>
                    {!isNearBottom && (
                        <button
                            title="Scroll to bottom"
                            className="absolute bottom-0 right-0 bg-white dark:bg-[hsl(var(--background))] text-neutral-500 dark:text-white p-2 rounded-full shadow-xl"
                            onClick={() => {
                                scrollToBottom();
                            }}
                        >
                            <ArrowDown size={24} />
                        </button>
                    )}
                </div>
            </div>
        </ScrollArea>
    );
}
