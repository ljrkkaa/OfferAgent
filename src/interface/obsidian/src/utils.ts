import { Notice, Vault, Modal, TFile, setIcon, Editor, WorkspaceLeaf } from 'obsidian';
import { KhojSetting } from 'src/settings'
import { OfferAgentServer } from './api';
import { KhojSearchModal } from './search_modal';

function fileExtensionToMimeType(extension: string): string {
    switch (extension) {
        case 'pdf':
            return 'application/pdf';
        case 'md':
        case 'markdown':
            return 'text/markdown';
        default:
            return 'text/plain';
    }
}

function filenameToMimeType(filename: TFile): string {
    switch (filename.extension) {
        case 'pdf':
            return 'application/pdf';
        case 'md':
        case 'markdown':
            return 'text/markdown';
        default:
            console.warn(`Unknown file type: ${filename.extension}. Defaulting to text/plain.`);
            return 'text/plain';
    }
}

export const fileTypeToExtension = {
    'pdf': ['pdf'],
    'markdown': ['md', 'markdown'],
};
export const supportedBinaryFileTypes = fileTypeToExtension.pdf;
export const supportedFileTypes = fileTypeToExtension.markdown.concat(supportedBinaryFileTypes);

export function getFilesToSync(vault: Vault, setting: KhojSetting): TFile[] {
    const files = vault.getFiles()
        // Filter supported file types for syncing
        .filter(file => supportedFileTypes.includes(file.extension))
        // Filter user configured file types for syncing
        .filter(file => {
            if (fileTypeToExtension.markdown.includes(file.extension)) return setting.syncFileType.markdown;
            if (fileTypeToExtension.pdf.includes(file.extension)) return setting.syncFileType.pdf;
            return false;
        })
        // Filter in included folders
        .filter(file => {
            // If no folders are specified, sync all files
            if (setting.syncFolders.length === 0) return true;
            // Otherwise, check if the file is in one of the specified folders
            return setting.syncFolders.some(folder =>
                file.path.startsWith(folder + '/') || file.path === folder
            );
        })
        // Filter out excluded folders
        .filter(file => {
            // If no folders are excluded, include all files
            if (setting.excludeFolders.length === 0) return true;
            // Exclude files in any of the excluded folders
            return !setting.excludeFolders.some(folder =>
                file.path.startsWith(folder + '/') || file.path === folder
            );
        })
        // Sort files by type: markdown > pdf
        .sort((a, b) => {
            const typeOrder: (keyof typeof fileTypeToExtension)[] = ['markdown', 'pdf'];
            const aType = typeOrder.findIndex(type => fileTypeToExtension[type].includes(a.extension));
            const bType = typeOrder.findIndex(type => fileTypeToExtension[type].includes(b.extension));
            return aType - bType;
        });

    return files;
}

export async function updateContentIndex(
    vault: Vault,
    setting: KhojSetting,
    server: OfferAgentServer,
    lastSync: Map<TFile, number>,
    regenerate: boolean = false,
    userTriggered: boolean = false,
    onProgress?: (progress: { processed: number, total: number }) => void
): Promise<Map<TFile, number>> {
    // Get all markdown, pdf files in the vault
    console.log(`OfferAgent: Updating OfferAgent content index...`);
    const files = getFilesToSync(vault, setting);
    console.log(`OfferAgent: Found ${files.length} eligible files in vault`);

    let countOfFilesToIndex = 0;
    let countOfFilesToDelete = 0;
    lastSync = lastSync.size > 0 ? lastSync : new Map<TFile, number>();

    // Count files that need indexing (modified since last sync or regenerating)
    const filesToSync = regenerate
        ? files
        : files.filter(file => file.stat.mtime >= (lastSync.get(file) ?? 0));

    // Show notice with file counts when user triggers sync
    if (userTriggered) {
        new Notice(`🔄 Syncing ${filesToSync.length} of ${files.length} files to OfferAgent...`);
    }
    console.log(`OfferAgent: ${filesToSync.length} files to sync (${files.length} total eligible)`);

    // Add all files to index as multipart form data, batched by size, item count
    const MAX_BATCH_SIZE = 10 * 1024 * 1024; // 10MB max batch size
    const MAX_BATCH_ITEMS = 50; // Max 50 items per batch
    let fileData: { blob: Blob, path: string }[][] = [];
    let currentBatch: { blob: Blob, path: string }[] = [];
    let currentBatchSize = 0;

    for (const file of files) {
        // Only push files that have been modified since last sync if not regenerating
        if (!regenerate && file.stat.mtime < (lastSync.get(file) ?? 0)) {
            continue;
        }

        countOfFilesToIndex++;
        const encoding = supportedBinaryFileTypes.includes(file.extension) ? "binary" : "utf8";
        const mimeType = fileExtensionToMimeType(file.extension) + (encoding === "utf8" ? "; charset=UTF-8" : "");
        const fileContent = encoding == 'binary' ? await vault.readBinary(file) : await vault.read(file);
        const fileItem = { blob: new Blob([fileContent], { type: mimeType }), path: file.path };

        const fileSize = (typeof fileContent === 'string') ? new Blob([fileContent]).size : fileContent.byteLength;
        if ((currentBatchSize + fileSize > MAX_BATCH_SIZE || currentBatch.length >= MAX_BATCH_ITEMS) && currentBatch.length > 0) {
            fileData.push(currentBatch);
            currentBatch = [];
            currentBatchSize = 0;
        }

        currentBatch.push(fileItem);
        currentBatchSize += fileSize;
    }

    // Add files to delete (previously synced but no longer in vault) to final batch
    let filesToDelete: TFile[] = [];
    for (const lastSyncedFile of lastSync.keys()) {
        if (!files.includes(lastSyncedFile)) {
            countOfFilesToDelete++;
            const fileObj = new Blob([""], { type: filenameToMimeType(lastSyncedFile) });
            currentBatch.push({ blob: fileObj, path: lastSyncedFile.path });
            filesToDelete.push(lastSyncedFile);
        }
    }

    // Add final batch if not empty
    if (currentBatch.length > 0) {
        fileData.push(currentBatch);
    }

    // Delete all files of enabled content types first if regenerating
    let error_message: string | null = null;
    if (regenerate) {
        // Mark content types to delete based on user sync file type settings
        const contentTypesToDelete: string[] = [];
        if (setting.syncFileType.markdown) contentTypesToDelete.push('markdown');
        if (setting.syncFileType.pdf) contentTypesToDelete.push('pdf');

        try {
            for (const contentType of contentTypesToDelete) {
                await server.deleteContentByType(contentType);
            }
        } catch (err) {
            console.error('OfferAgent: Error deleting content types:', err);
            error_message = "❗️Failed to clear existing content index";
            fileData = [];
        }
    }

    // Upload files in batches
    let responses: string[] = [];
    let processedFiles = 0;
    const totalFiles = fileData.reduce((sum, batch) => sum + batch.length, 0);

    // Report initial progress with total count before uploading
    if (onProgress) {
        onProgress({ processed: 0, total: totalFiles });
    }

    for (const batch of fileData) {
        try {
            const resultText = await server.uploadContentBatch(batch);
            responses.push(resultText);
            processedFiles += batch.length;
            if (onProgress) {
                onProgress({ processed: processedFiles, total: totalFiles });
            }
        } catch (err: any) {
            console.error('OfferAgent: Failed to upload batch:', err);
            if (err.message?.includes('429')) {
                error_message = `❗️Requests were throttled. Try again later.`;
            } else {
                error_message = `Failed to sync content with OfferAgent server. Error: ${err.message ?? String(err)}`;
            }
            break;
        }
    }

    const indexedPaths = new Set(responses.flatMap(response => response.split(",").filter(path => path.length > 0)));

    // Update last sync time for each successfully indexed file
    files
        .filter(file => indexedPaths.has(file.path))
        .reduce((newSync, file) => {
            newSync.set(file, new Date().getTime());
            return newSync;
        }, lastSync);

    // Remove files that were deleted from last sync
    filesToDelete
        .filter(file => indexedPaths.has(file.path))
        .forEach(file => lastSync.delete(file));

    if (error_message) {
        new Notice(error_message);
    } else {
        const summary = `Updated ${countOfFilesToIndex}, deleted ${countOfFilesToDelete} files`;
        if (userTriggered) new Notice(`✅ ${summary}`);
        console.log(`✅ Refreshed OfferAgent content index. ${summary}.`);
    }

    return lastSync;
}

export async function createNote(name: string, newLeaf = false): Promise<void> {
    try {
        let pathPrefix: string
        switch (this.app.vault.getConfig('newFileLocation')) {
            case 'current':
                pathPrefix = (this.app.workspace.getActiveFile()?.parent.path ?? '') + '/'
                break
            case 'folder':
                pathPrefix = this.app.vault.getConfig('newFileFolderPath') + '/'
                break
            default: // 'root'
                pathPrefix = ''
                break
        }
        await this.app.workspace.openLinkText(`${pathPrefix}${name}.md`, '', newLeaf)
    } catch (e) {
        console.error('OfferAgent: Could not create note.\n' + (e as any).message);
        throw e
    }
}

export async function createNoteAndCloseModal(query: string, modal: Modal, opt?: { newLeaf: boolean }): Promise<void> {
    try {
        await createNote(query, opt?.newLeaf);
    }
    catch (e) {
        new Notice((e as Error).message)
        return
    }
    modal.close();
}

export function getBackendStatusMessage(
    connectedToServer: boolean,
    userEmail: string | undefined,
    serverUrl: string,
): string {
    if (!connectedToServer)
        return `Could not connect to OfferAgent at ${serverUrl}. Check the URL, port forwarding, and API key.`;
    else if (!userEmail)
        return `Connected to OfferAgent. Configure the client with the server KHOJ_API_KEY value.`;
    else if (userEmail === 'default@example.com')
        // Logged in as default user in anonymous mode
        return `Welcome back to OfferAgent`;
    else
        return `Welcome back to OfferAgent, ${userEmail}`;
}

export async function populateHeaderPane(
    headerEl: Element,
    setting: KhojSetting,
    viewType: string,
    server: OfferAgentServer,
): Promise<void> {
    try {
        const connection = await server.probe();
        setting.connectedToBackend = connection.connected;
        setting.userInfo = connection.user;
    } catch (error) {
        console.error("Could not connect to OfferAgent");
    }

    // Add OfferAgent title to header element
    const titlePaneEl = headerEl.createDiv();
    titlePaneEl.className = 'khoj-header-title-pane';
    const titleEl = titlePaneEl.createDiv();
    titleEl.className = 'khoj-logo';
    titleEl.textContent = "OfferAgent";

    // Populate the header element with the navigation pane
    // Create the nav element
    const nav = titlePaneEl.createEl('nav');
    nav.className = 'khoj-nav';

    // Create the title pane element
    titlePaneEl.appendChild(titleEl);
    titlePaneEl.appendChild(nav);

    // Create the chat link
    const chatLink = nav.createEl('a');
    chatLink.id = 'chat-nav';
    chatLink.className = 'khoj-nav chat-nav';
    chatLink.dataset.view = KhojView.CHAT;

    // Create the chat icon
    const chatIcon = chatLink.createEl('span');
    chatIcon.className = 'khoj-nav-icon khoj-nav-icon-chat';
    setIcon(chatIcon, 'khoj-chat');

    // Create the chat text
    const chatText = chatLink.createEl('span');
    chatText.className = 'khoj-nav-item-text';
    chatText.textContent = 'Chat';

    // Append the chat icon and text to the chat link
    chatLink.appendChild(chatIcon);
    chatLink.appendChild(chatText);

    // Create the search link
    const searchLink = nav.createEl('a');
    searchLink.id = 'search-nav';
    searchLink.className = 'khoj-nav search-nav';

    // Create the search icon
    const searchIcon = searchLink.createEl('span');
    searchIcon.className = 'khoj-nav-icon khoj-nav-icon-search';
    setIcon(searchIcon, 'khoj-search');

    // Create the search text
    const searchText = searchLink.createEl('span');
    searchText.className = 'khoj-nav-item-text';
    searchText.textContent = 'Search';

    // Append the search icon and text to the search link
    searchLink.appendChild(searchIcon);
    searchLink.appendChild(searchText);

    // Create the similar link
    const similarLink = nav.createEl('a');
    similarLink.id = 'similar-nav';
    similarLink.className = 'khoj-nav similar-nav';
    similarLink.dataset.view = KhojView.SIMILAR;

    // Create the similar icon
    const similarIcon = similarLink.createEl('span');
    similarIcon.id = 'similar-nav-icon';
    similarIcon.className = 'khoj-nav-icon khoj-nav-icon-similar';
    setIcon(similarIcon, 'webhook');

    // Create the similar text
    const similarText = similarLink.createEl('span');
    similarText.className = 'khoj-nav-item-text';
    similarText.textContent = 'Similar';

    // Append the similar icon and text to the similar link
    similarLink.appendChild(similarIcon);
    similarLink.appendChild(similarText);

    // Helper to get the current Khoj leaf if active
    const getCurrentKhojLeaf = (): WorkspaceLeaf | undefined => {
        const activeLeaf = this.app.workspace.activeLeaf;
        if (activeLeaf && activeLeaf.view &&
            (activeLeaf.view.getViewType() === KhojView.CHAT || activeLeaf.view.getViewType() === KhojView.SIMILAR)) {
            return activeLeaf;
        }
        return undefined;
    };

    // Add event listeners to the navigation links
    // Chat link event listener
    chatLink.addEventListener('click', () => {
        // Get the activateView method from the plugin instance
        const khojPlugin = this.app.plugins.plugins.offeragent;
        khojPlugin?.activateView(KhojView.CHAT, getCurrentKhojLeaf());
    });

    // Search link event listener
    searchLink.addEventListener('click', () => {
        // Open the search modal
        new KhojSearchModal(this.app, setting, server).open();
    });

    // Similar link event listener
    similarLink.addEventListener('click', () => {
        // Get the activateView method from the plugin instance
        const khojPlugin = this.app.plugins.plugins.offeragent;
        khojPlugin?.activateView(KhojView.SIMILAR, getCurrentKhojLeaf());
    });

    // Append the nav items to the nav element
    nav.appendChild(chatLink);
    nav.appendChild(searchLink);
    nav.appendChild(similarLink);

    // Append the title and new chat container to the header element
    headerEl.appendChild(titlePaneEl);

    if (viewType === KhojView.CHAT) {
        // Create subtitle pane for New Chat button
        const newChatEl = headerEl.createDiv("khoj-header-right-container");

        // Add New Chat button
        const newChatButton = newChatEl.createEl('button');
        newChatButton.className = 'khoj-header-new-chat-button';
        newChatButton.title = 'Start New Chat (Ctrl+Alt+N)';
        setIcon(newChatButton, 'plus-circle');
        newChatButton.textContent = 'New Chat';

        // Add event listener to the New Chat button
        newChatButton.addEventListener('click', () => {
            const khojPlugin = this.app.plugins.plugins.offeragent;
            if (khojPlugin) {
                // First activate the chat view
                khojPlugin.activateView(KhojView.CHAT).then(() => {
                    // Then create a new conversation
                    setTimeout(() => {
                        // Access the chat view directly from the leaf after activation
                        const leaves = this.app.workspace.getLeavesOfType(KhojView.CHAT);
                        if (leaves.length > 0) {
                            const chatView = leaves[0].view;
                            if (chatView && typeof chatView.createNewConversation === 'function') {
                                chatView.createNewConversation();
                            }
                        }
                    }, 100);
                });
            }
        });

        // Append the new chat container to the header element
        headerEl.appendChild(newChatEl);
    }

    // Update active state based on current view
    const updateActiveState = () => {
        const activeLeaf = this.app.workspace.activeLeaf;
        if (!activeLeaf) return;

        const viewType = activeLeaf.view?.getViewType();

        // Remove active class from all links
        chatLink.classList.remove('khoj-nav-selected');
        similarLink.classList.remove('khoj-nav-selected');

        // Add active class to the current view link
        if (viewType === KhojView.CHAT) {
            chatLink.classList.add('khoj-nav-selected');
        } else if (viewType === KhojView.SIMILAR) {
            similarLink.classList.add('khoj-nav-selected');
        }
    };

    // Initial update
    updateActiveState();

    // Register event for workspace changes
    this.app.workspace.on('active-leaf-change', updateActiveState);
}

export enum KhojView {
    CHAT = "khoj-chat-view",
    SIMILAR = "khoj-similar-view",
}

function copyParentText(event: MouseEvent, message: string, originalButton: string) {
    const button = event.currentTarget as HTMLElement;
    if (!button || !button?.parentNode?.textContent) return;
    if (!!button.firstChild) button.removeChild(button.firstChild as HTMLImageElement);
    const textContent = message ?? button.parentNode.textContent.trim();
    navigator.clipboard.writeText(textContent).then(() => {
        setIcon((button as HTMLElement), 'copy-check');
        setTimeout(() => {
            setIcon((button as HTMLElement), originalButton);
        }, 1000);
    }).catch((error) => {
        console.error("Error copying text to clipboard:", error);
        const originalButtonText = button.innerHTML;
        setIcon((button as HTMLElement), 'x-circle');
        setTimeout(() => {
            button.innerHTML = originalButtonText;
            setIcon((button as HTMLElement), originalButton);
        }, 2000);
    });

    return textContent;
}

export function createCopyParentText(message: string, originalButton: string = 'copy-plus') {
    return function (event: MouseEvent) {
        return copyParentText(event, message, originalButton);
    }
}

export function pasteTextAtCursor(text: string | undefined) {
    // Get the current active file's editor
    const editor: Editor = this.app.workspace.getActiveFileView()?.editor
    if (!editor || !text) return;
    const cursor = editor.getCursor();
    // If there is a selection, replace it with the text
    if (editor?.getSelection()) {
        editor.replaceSelection(text);
        // If there is no selection, insert the text at the cursor position
    } else if (cursor) {
        editor.replaceRange(text, cursor);
    }
}

export function getFileFromPath(sourceFiles: TFile[], chosenFile: string): TFile | undefined {
    // Find the vault file matching file of chosen file, entry
    let fileMatch = sourceFiles
        // Sort by descending length of path
        // This finds longest path match when multiple files have same name
        .sort((a, b) => b.path.length - a.path.length)
        // The first match is the best file match across OS
        // e.g. Khoj server on Linux, Obsidian vault on Android
        .find(file => chosenFile.replace(/\\/g, "/").endsWith(file.path))
    return fileMatch;
}

export function getLinkToEntry(sourceFiles: TFile[], chosenFile: string, chosenEntry: string): string | undefined {
    // Find the vault file matching file of chosen file, entry
    let fileMatch = getFileFromPath(sourceFiles, chosenFile);

    // Return link to vault file at heading of chosen search result
    if (fileMatch) {
        let resultHeading = fileMatch.extension !== 'pdf' ? chosenEntry.split('\n', 1)[0] : '';
        let linkToEntry = resultHeading.startsWith('#') ? `${fileMatch.path}${resultHeading}` : fileMatch.path;
        console.log(`Link: ${linkToEntry}, File: ${fileMatch.path}, Heading: ${resultHeading}`);
        return linkToEntry;
    }
}
