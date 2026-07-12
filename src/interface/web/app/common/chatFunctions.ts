import type { ChatOptions } from "../components/chatInputArea/chatInputArea";
import type { Context, OnlineContext, StreamMessage } from "../components/chatMessage/chatMessage";
import { attachVaultActionBatch } from "./vaultActions";

export interface RawReferenceData {
    context?: Context[];
    onlineContext?: OnlineContext;
}

export interface MessageMetadata {
    conversationId: string;
    turnId: string;
}

interface MessageChunk {
    type: string;
    data: unknown;
}

const STREAM_EVENT_TYPES = new Set([
    "start_llm_response",
    "end_llm_response",
    "end_response",
    "status",
    "thought",
    "references",
    "vault_actions",
    "metadata",
    "usage",
    "message",
]);

function isRecord(value: unknown): value is Record<string, unknown> {
    return typeof value === "object" && value !== null;
}

export function convertMessageChunkToJson(chunk: string): MessageChunk {
    let event: unknown;
    try {
        event = JSON.parse(chunk);
    } catch {
        throw new Error("Invalid OfferAgent stream event");
    }
    if (
        !isRecord(event) ||
        Object.keys(event).length !== 2 ||
        typeof event.type !== "string" ||
        !STREAM_EVENT_TYPES.has(event.type) ||
        !("data" in event)
    ) {
        throw new Error("Invalid OfferAgent stream event");
    }
    const textEvents = new Set([
        "start_llm_response",
        "end_llm_response",
        "end_response",
        "status",
        "thought",
        "message",
    ]);
    if (textEvents.has(event.type) && typeof event.data !== "string") {
        throw new Error(`Invalid ${event.type} stream event`);
    }
    if (!textEvents.has(event.type) && !isRecord(event.data)) {
        throw new Error(`Invalid ${event.type} stream event`);
    }
    return { type: event.type, data: event.data };
}

export function processMessageChunk(
    rawChunk: string,
    currentMessage: StreamMessage,
    context: Context[] = [],
    onlineContext: OnlineContext = {},
): { context: Context[]; onlineContext: OnlineContext } {
    const chunk = convertMessageChunkToJson(rawChunk);

    if (!currentMessage || !chunk || !chunk.type) return { context, onlineContext };

    console.log(`chunk type: ${chunk.type}`);

    if (chunk.type === "status") {
        console.log(`status: ${chunk.data}`);
        const statusMessage = chunk.data as string;
        currentMessage.trainOfThought.push(statusMessage);
    } else if (chunk.type === "thought") {
        const thoughtChunk = chunk.data as string;
        const lastThoughtIndex = currentMessage.trainOfThought.length - 1;
        const previousThought =
            lastThoughtIndex >= 0 ? currentMessage.trainOfThought[lastThoughtIndex] : "";
        // If the last train of thought started with "Thinking: " append the new thought chunk to it
        if (previousThought.startsWith("**Thinking:** ")) {
            currentMessage.trainOfThought[lastThoughtIndex] += thoughtChunk;
        } else {
            currentMessage.trainOfThought.push(`**Thinking:** ${thoughtChunk}`);
        }
    } else if (chunk.type === "references") {
        const references = chunk.data as RawReferenceData;

        if (references.context) context = references.context;
        if (references.onlineContext) onlineContext = references.onlineContext;
        return { context, onlineContext };
    } else if (chunk.type === "metadata") {
        const messageMetadata = chunk.data as MessageMetadata;
        currentMessage.turnId = messageMetadata.turnId;
    } else if (chunk.type === "vault_actions") {
        attachVaultActionBatch(currentMessage, chunk.data);
    } else if (chunk.type === "message") {
        const chunkData = chunk.data;
        if (typeof chunkData !== "string") throw new Error("Invalid message stream event");
        currentMessage.rawResponse += chunkData;
    } else if (chunk.type === "start_llm_response") {
        console.log(`Started streaming: ${new Date()}`);
    } else if (chunk.type === "end_llm_response") {
        console.log(`Completed streaming: ${new Date()}`);
    } else if (chunk.type === "end_response") {
        // Append any references after all the data has been streamed
        if (onlineContext) currentMessage.onlineContext = onlineContext;
        if (context) currentMessage.context = context;

        // Mark current message streaming as completed
        currentMessage.completed = true;
    }
    return { context, onlineContext };
}

export function modifyFileFilterForConversation(
    conversationId: string | null,
    filenames: string[],
    setAddedFiles: (files: string[]) => void,
    mode: "add" | "remove",
) {
    if (!conversationId) {
        console.error("No conversation ID provided");
        return;
    }

    const method = mode === "add" ? "POST" : "DELETE";

    const body = {
        conversation_id: conversationId,
        filenames: filenames,
    };
    const addUrl = `/api/chat/conversation/file-filters/bulk`;

    fetch(addUrl, {
        method: method,
        headers: {
            "Content-Type": "application/json",
        },
        body: JSON.stringify(body),
    })
        .then((res) => {
            if (!res.ok)
                throw new Error(`Failed to call API at ${addUrl} with error ${res.statusText}`);
            return res.json();
        })
        .then((data) => {
            if (!Array.isArray(data) || data.some((file) => typeof file !== "string")) {
                throw new Error("Invalid file filter response");
            }
            setAddedFiles(data);
        })
        .catch((err) => {
            console.error(err);
            return;
        });
}

export async function createNewConversation() {
    try {
        const response = await fetch("/api/chat/sessions?client=web", {
            method: "POST",
        });
        if (!response.ok)
            throw new Error(`Failed to fetch chat sessions with status: ${response.status}`);
        const data = await response.json();
        const conversationID = data.conversation_id;
        if (typeof conversationID !== "string")
            throw new Error("Conversation ID not found in response");
        return conversationID;
    } catch (error) {
        console.error("Error creating new conversation:", error);
        throw error;
    }
}

export function buildChatUrl(conversationId: string, query?: string) {
    const params = new URLSearchParams({ conversationId });
    if (query) params.set("q", query);
    return `/chat?${params.toString()}`;
}

export async function fetchChatOptions(): Promise<ChatOptions> {
    const response = await fetch("/api/chat/options");
    if (!response.ok) {
        throw new Error(`Failed to fetch chat options: ${response.status}`);
    }
    return response.json();
}

export async function packageFilesForUpload(files: FileList): Promise<FormData> {
    const formData = new FormData();

    const fileReadPromises = Array.from(files).map((file) => {
        return new Promise<void>((resolve, reject) => {
            let reader = new FileReader();
            reader.onload = function (event) {
                if (event.target === null) {
                    reject();
                    return;
                }

                let fileContents = event.target.result;
                let fileType = file.type;
                let fileName = file.name;
                if (fileType === "") {
                    let fileExtension = fileName.split(".").pop();
                    if (fileExtension === "md") {
                        fileType = "text/markdown";
                    } else if (
                        fileExtension === "txt" ||
                        fileExtension === "tsx" ||
                        fileExtension === "ipynb"
                    ) {
                        fileType = "text/plain";
                    } else if (fileExtension === "html") {
                        fileType = "text/html";
                    } else if (fileExtension === "pdf") {
                        fileType = "application/pdf";
                    } else {
                        // Skip this file if its type is not supported
                        console.warn(
                            `File type ${fileType} not supported. Skipping file: ${fileName}`,
                        );
                        resolve();
                        return;
                    }
                }

                if (fileContents === null) {
                    console.warn(`Could not read file content. Skipping file: ${fileName}`);
                    reject();
                    return;
                }

                let fileObj = new Blob([fileContents], { type: fileType });
                formData.append("files", fileObj, file.name);
                resolve();
            };
            reader.onerror = reject;
            reader.readAsArrayBuffer(file);
        });
    });

    await Promise.all(fileReadPromises);
    return formData;
}

export function generateNewTitle(conversationId: string, setTitle: (title: string) => void) {
    fetch(`/api/chat/title?conversation_id=${encodeURIComponent(conversationId)}`, {
        method: "POST",
    })
        .then((res) => {
            if (!res.ok) throw new Error(`Failed to call API with error ${res.statusText}`);
            return res.json();
        })
        .then((data) => {
            setTitle(data.title);
        })
        .catch((err) => {
            console.error(err);
            return;
        });
}

export function uploadDataForIndexing(
    files: FileList,
    setWarning: (warning: string) => void,
    setUploading: (uploading: boolean) => void,
    setError: (error: string) => void,
    setUploadedFiles?: (files: string[]) => void,
    conversationId?: string | null,
) {
    const allowedExtensions = ["text/markdown", "text/plain", "text/html", "application/pdf"];
    const allowedFileEndings = ["md", "txt", "html", "pdf"];
    const badFiles: string[] = [];
    const goodFiles: File[] = [];

    const uploadedFiles: string[] = [];

    for (let file of files) {
        const fileEnding = file.name.split(".").pop();
        if (!file || !file.name || !fileEnding) {
            if (file) {
                badFiles.push(file.name);
            }
        } else if (
            !allowedExtensions.includes(file.type) &&
            !allowedFileEndings.includes(fileEnding.toLowerCase())
        ) {
            badFiles.push(file.name);
        } else {
            goodFiles.push(file);
        }
    }

    if (goodFiles.length === 0) {
        setWarning("No supported files found");
        return;
    }

    if (badFiles.length > 0) {
        setWarning("The following files are not supported yet:\n" + badFiles.join("\n"));
    }

    const formData = new FormData();
    const indexedFiles: string[] = [];

    // Create an array of Promises for file reading
    const fileReadPromises = Array.from(goodFiles).map((file) => {
        return new Promise<void>((resolve, reject) => {
            let reader = new FileReader();
            reader.onload = function (event) {
                if (event.target === null) {
                    reject();
                    return;
                }

                let fileContents = event.target.result;
                let fileType = file.type;
                let fileName = file.name;
                if (fileType === "") {
                    let fileExtension = fileName.split(".").pop();
                    if (fileExtension === "md") {
                        fileType = "text/markdown";
                    } else if (fileExtension === "txt") {
                        fileType = "text/plain";
                    } else if (fileExtension === "html") {
                        fileType = "text/html";
                    } else if (fileExtension === "pdf") {
                        fileType = "application/pdf";
                    } else {
                        // Skip this file if its type is not supported
                        resolve();
                        return;
                    }
                }

                if (fileContents === null) {
                    reject();
                    return;
                }

                let fileObj = new Blob([fileContents], { type: fileType });
                formData.append("files", fileObj, file.name);
                indexedFiles.push(file.name);
                resolve();
            };
            reader.onerror = reject;
            reader.readAsArrayBuffer(file);
        });
    });

    setUploading(true);

    // Wait for all files to be read before making the fetch request
    Promise.all(fileReadPromises)
        .then(() => {
            if (indexedFiles.length === 0) throw new Error("No supported files found");
            return fetch("/api/content?client=web", {
                method: "PATCH",
                body: formData,
            });
        })
        .then((response) => {
            if (!response.ok) throw new Error(`Failed to upload files: ${response.status}`);
            for (let fileName of indexedFiles) {
                uploadedFiles.push(fileName);
                if (conversationId && setUploadedFiles) {
                    modifyFileFilterForConversation(
                        conversationId,
                        [fileName],
                        setUploadedFiles,
                        "add",
                    );
                }
            }
            if (setUploadedFiles) setUploadedFiles(uploadedFiles);
        })
        .catch((error) => {
            console.log(error);
            setError(`Error uploading file: ${error}`);
        })
        .finally(() => {
            setUploading(false);
        });
}
