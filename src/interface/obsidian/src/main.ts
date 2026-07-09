import { Plugin, TAbstractFile, WorkspaceLeaf } from 'obsidian';
import { KhojSetting, KhojSettingTab, DEFAULT_SETTINGS } from 'src/settings'
import { KhojSearchModal } from 'src/search_modal'
import { KhojChatView } from 'src/chat_view'
import { KhojSimilarView } from 'src/similar_view'
import { updateContentIndex, canConnectToBackend, KhojView, fileTypeToExtension } from 'src/utils';
import { KhojPaneView } from 'src/pane_view';


export default class Khoj extends Plugin {
    settings: KhojSetting;
    indexingTimer: NodeJS.Timeout;
    syncDebounceTimer: NodeJS.Timeout | null = null;
    syncInProgress = false;
    syncRequested = false;

    async onload() {
        await this.loadSettings();

        // Add search command. It can be triggered from anywhere
        this.addCommand({
            id: 'search',
            name: 'Search',
            hotkeys: [{ modifiers: ["Ctrl", "Alt"], key: "S" }],
            callback: () => { new KhojSearchModal(this.app, this.settings).open(); }
        });

        // Add similar notes command. It can only be triggered from the editor
        this.addCommand({
            id: 'similar',
            name: 'Find similar notes',
            hotkeys: [{ modifiers: ["Ctrl", "Alt"], key: "F" }],
            editorCallback: () => { this.activateView(KhojView.SIMILAR); }
        });

        // Add chat command. It can be triggered from anywhere
        this.addCommand({
            id: 'chat',
            name: 'Chat',
            callback: () => { this.activateView(KhojView.CHAT); }
        });

        // Add similar documents view command
        this.addCommand({
            id: 'similar-view',
            name: 'Open Similar Documents View',
            callback: () => { this.activateView(KhojView.SIMILAR); }
        });

        // Add new chat command with hotkey
        this.addCommand({
            id: 'new-chat',
            name: 'New Chat',
            hotkeys: [{ modifiers: ["Ctrl", "Alt"], key: "N" }],
            callback: async () => {
                // First, activate the chat view
                await this.activateView(KhojView.CHAT);

                // Wait a short moment for the view to activate
                setTimeout(() => {
                    // Try to get the active chat view
                    const chatView = this.app.workspace.getActiveViewOfType(KhojChatView);
                    if (chatView) {
                        chatView.createNewConversation();
                    }
                }, 100);
            }
        });

        // Add conversation history command with hotkey
        this.addCommand({
            id: 'conversation-history',
            name: 'Show Conversation History',
            hotkeys: [{ modifiers: ["Ctrl", "Alt"], key: "O" }],
            callback: () => {
                this.activateView(KhojView.CHAT).then(() => {
                    const chatView = this.app.workspace.getActiveViewOfType(KhojChatView);
                    if (chatView) {
                        chatView.toggleChatSessions();
                    }
                });
            }
        });

        // Add sync command to manually sync new changes
        this.addCommand({
            id: 'sync',
            name: 'Sync new changes',
            callback: async () => this.syncNow("manual", true)
        });

        // Add edit confirmation commands
        this.addCommand({
            id: 'apply-edits',
            name: 'Apply pending edits',
            hotkeys: [{ modifiers: ["Ctrl", "Shift"], key: "Enter" }],
            callback: () => {
                const chatView = this.app.workspace.getActiveViewOfType(KhojChatView);
                if (chatView) {
                    chatView.applyPendingEdits();
                }
            }
        });

        this.addCommand({
            id: 'cancel-edits',
            name: 'Cancel pending edits',
            hotkeys: [{ modifiers: ["Ctrl", "Shift"], key: "Backspace" }],
            callback: () => {
                const chatView = this.app.workspace.getActiveViewOfType(KhojChatView);
                if (chatView) {
                    chatView.cancelPendingEdits();
                }
            }
        });

        // Register views
        this.registerView(KhojView.CHAT, (leaf) => new KhojChatView(leaf, this));
        this.registerView(KhojView.SIMILAR, (leaf) => new KhojSimilarView(leaf, this));

        // Create an icon in the left ribbon.
        this.addRibbonIcon('message-circle', 'OfferAgent', (_: MouseEvent) => {
            this.activateView(KhojView.CHAT);
        });

        // Add a settings tab so the user can configure khoj
        this.addSettingTab(new KhojSettingTab(this.app, this));

        // Start the sync timer
        this.registerVaultSyncEvents();
        this.startSyncTimer();
        this.syncNow("startup");
    }

    // Method to start the sync timer
    private startSyncTimer() {
        // Clean up the old timer if it exists
        if (this.indexingTimer) {
            clearInterval(this.indexingTimer);
        }

        // Start a new timer with the configured interval
        this.indexingTimer = setInterval(() => this.syncNow("scheduled"), this.settings.syncInterval * 60 * 1000);
    }

    private registerVaultSyncEvents() {
        const schedule = (file: TAbstractFile) => {
            if (this.shouldSyncPath(file.path)) this.scheduleSync("vault change");
        };

        this.registerEvent(this.app.vault.on("create", schedule));
        this.registerEvent(this.app.vault.on("modify", schedule));
        this.registerEvent(this.app.vault.on("delete", schedule));
        this.registerEvent(this.app.vault.on("rename", (file, oldPath) => {
            if (this.shouldSyncPath(file.path) || this.shouldSyncPath(oldPath)) this.scheduleSync("vault rename");
        }));
    }

    private shouldSyncPath(path: string): boolean {
        const extension = path.split(".").pop()?.toLowerCase() ?? "";
        const syncable =
            (this.settings.syncFileType.markdown && fileTypeToExtension.markdown.includes(extension)) ||
            (this.settings.syncFileType.pdf && fileTypeToExtension.pdf.includes(extension));
        if (!syncable) return false;
        if (this.settings.syncFolders.length > 0 && !this.settings.syncFolders.some(folder => path.startsWith(folder + "/") || path === folder)) return false;
        return !this.settings.excludeFolders.some(folder => path.startsWith(folder + "/") || path === folder);
    }

    private scheduleSync(reason: string) {
        if (!this.settings.autoConfigure) return;
        if (this.syncDebounceTimer) clearTimeout(this.syncDebounceTimer);
        this.syncDebounceTimer = setTimeout(() => {
            this.syncDebounceTimer = null;
            this.syncNow(reason);
        }, 5000);
    }

    private async syncNow(reason: string, userTriggered: boolean = false) {
        if (!this.settings.autoConfigure) return;
        if (this.syncInProgress) {
            this.syncRequested = true;
            return;
        }

        this.syncInProgress = true;
        try {
            do {
                this.syncRequested = false;
                this.settings.lastSync = await updateContentIndex(
                    this.app.vault,
                    this.settings,
                    this.settings.lastSync,
                    false,
                    userTriggered
                );
                await this.saveSettings();
            } while (this.syncRequested && this.settings.autoConfigure);
        } catch (error) {
            console.error(`OfferAgent: ${reason} sync failed`, error);
        } finally {
            this.syncInProgress = false;
        }
    }

    // Public method to restart the timer (called from settings)
    public restartSyncTimer() {
        this.startSyncTimer();
    }

    async loadSettings() {
        // Load khoj obsidian plugin settings
        this.settings = Object.assign({}, DEFAULT_SETTINGS, await this.loadData());

        // Check if can connect to khoj server
        ({ connectedToBackend: this.settings.connectedToBackend } =
            await canConnectToBackend(this.settings.khojUrl, this.settings.khojApiKey, true));
    }

    async saveSettings() {
        await this.saveData(this.settings);
    }

    async onunload() {
        // Remove scheduled job to update index at regular cadence
        if (this.indexingTimer)
            clearInterval(this.indexingTimer);
        if (this.syncDebounceTimer)
            clearTimeout(this.syncDebounceTimer);

        this.unload();
    }

    async activateView(viewType: KhojView, existingLeaf?: WorkspaceLeaf) {
        const { workspace } = this.app;
        let leafToUse: WorkspaceLeaf | null = null;

        // Check if an existingLeaf is provided and is suitable for a view type switch
        if (existingLeaf && existingLeaf.view &&
            (existingLeaf.view.getViewType() === KhojView.CHAT || existingLeaf.view.getViewType() === KhojView.SIMILAR) &&
            existingLeaf.view.getViewType() !== viewType) {
            // The existing leaf is a Khoj pane and we want to switch its type
            leafToUse = existingLeaf;
            await leafToUse.setViewState({ type: viewType, active: true });
        } else {
            // Standard logic: find an existing leaf of the target type, or create a new one
            const leaves = workspace.getLeavesOfType(viewType);
            if (leaves.length > 0) {
                leafToUse = leaves[0];
            } else {
                // If we are not switching an existing Khoj leaf,
                // and no leaf of the target type exists, create a new one.
                // Use the provided existingLeaf if it's not a Khoj pane we're trying to switch,
                // otherwise, get a new right leaf.
                leafToUse = (existingLeaf && !(existingLeaf.view instanceof KhojPaneView)) ? existingLeaf : workspace.getRightLeaf(false);
                if (leafToUse) {
                    await leafToUse.setViewState({ type: viewType, active: true });
                } else {
                    console.error("OfferAgent: Could not get a leaf to activate view.");
                    return;
                }
            }
        }

        if (leafToUse) {
            workspace.revealLeaf(leafToUse); // Ensure the leaf is visible

            // Specific actions after revealing/switching
            if (viewType === KhojView.CHAT) {
                // Ensure the view instance is correct after potential setViewState
                const chatView = leafToUse.view as KhojChatView;
                if (chatView instanceof KhojChatView) { // Double check instance type
                    // Use a more robust way to get the input, or ensure it's always present after onOpen
                    const chatInput = chatView.containerEl.querySelector<HTMLTextAreaElement>(".khoj-chat-input");
                    chatInput?.focus();
                }
            }
        }
    }
}
