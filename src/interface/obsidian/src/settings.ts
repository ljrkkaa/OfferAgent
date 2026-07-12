import { App, Notice, PluginSettingTab, Setting, TFile, SuggestModal } from 'obsidian';
import Khoj from 'src/main';
import { ModelOption, UserInfo } from './api';
import { getBackendStatusMessage, updateContentIndex } from './utils';

interface SyncFileTypes {
    markdown: boolean;
    pdf: boolean;
}

export interface KhojSetting {
    resultsCount: number;
    khojUrl: string;
    khojApiKey: string;
    connectedToBackend: boolean;
    autoConfigure: boolean;
    lastSync: Map<TFile, number>;
    syncFileType: SyncFileTypes;
    userInfo: UserInfo | null;
    syncFolders: string[];
    excludeFolders: string[];
    syncInterval: number;
    fileAccessMode: 'none' | 'read' | 'write';
    selectedChatModelId: string | null; // Mirrors server's selected_chat_model_config
    availableChatModels: ModelOption[];
}

export const DEFAULT_SETTINGS: KhojSetting = {
    resultsCount: 15,
    khojUrl: 'http://127.0.0.1:42110',
    khojApiKey: '',
    connectedToBackend: false,
    autoConfigure: true,
    lastSync: new Map(),
    syncFileType: {
        markdown: true,
        pdf: true,
    },
    userInfo: null,
    syncFolders: [],
    excludeFolders: [],
    syncInterval: 60,
    fileAccessMode: 'read',
    selectedChatModelId: null, // Will be populated from server
    availableChatModels: [],
}

export class KhojSettingTab extends PluginSettingTab {
    plugin: Khoj;
    private chatModelSetting: Setting | null = null;

    constructor(app: App, plugin: Khoj) {
        super(app, plugin);
        this.plugin = plugin;
    }

    display(): void {
        const { containerEl } = this;
        containerEl.empty();
        this.chatModelSetting = null; // Reset when display is called

        // Add notice whether able to connect to khoj backend or not
        let backendStatusMessage = getBackendStatusMessage(
            this.plugin.settings.connectedToBackend,
            this.plugin.settings.userInfo?.email,
            this.plugin.settings.khojUrl,
        );

        const connectHeaderEl = containerEl.createEl('h3', { title: backendStatusMessage });
        const connectHeaderContentEl = connectHeaderEl.createSpan({ cls: 'khoj-connect-settings-header' });
        const connectTitleEl = connectHeaderContentEl.createSpan({ text: 'Connect OfferAgent' });
        const backendStatusEl = connectTitleEl.createSpan({ text: this.connectStatusIcon(), cls: 'khoj-connect-settings-header-status' });
        if (this.plugin.settings.userInfo && this.plugin.settings.connectedToBackend) {
            if (this.plugin.settings.userInfo.photo) {
                const profilePicEl = connectHeaderContentEl.createEl('img', {
                    attr: { src: this.plugin.settings.userInfo.photo },
                    cls: 'khoj-profile'
                });
                profilePicEl.addEventListener('click', () => { new Notice(backendStatusMessage); });
            } else if (this.plugin.settings.userInfo.email) {
                const initial = this.plugin.settings.userInfo.email[0].toUpperCase();
                const profilePicEl = connectHeaderContentEl.createDiv({
                    text: initial,
                    cls: 'khoj-profile khoj-profile-initial'
                });
                profilePicEl.addEventListener('click', () => { new Notice(backendStatusMessage); });
            }
        }
        if (this.plugin.settings.userInfo && this.plugin.settings.userInfo.email) {
            connectHeaderEl.title = this.plugin.settings.userInfo?.email === 'default@example.com'
                ? "Signed in"
                : `Signed in as ${this.plugin.settings.userInfo.email}`;
        }

        // Add khoj settings configurable from the plugin settings tab
        const apiKeySetting = new Setting(containerEl)
            .setName('OfferAgent API Key')
            .addText(text => text
                .setValue(`${this.plugin.settings.khojApiKey}`)
                .onChange(async (value) => {
                    this.plugin.settings.khojApiKey = value.trim();
                    backendStatusMessage = await this.refreshConnectionState();

                    if (!this.plugin.settings.connectedToBackend) {
                        this.plugin.settings.availableChatModels = [];
                        this.plugin.settings.selectedChatModelId = null;
                    }
                    backendStatusEl.setText(this.connectStatusIcon())
                    connectHeaderEl.title = backendStatusMessage;
                    await this.refreshModelsAndServerPreference();
                }));

        apiKeySetting.setDesc('Use the server KHOJ_API_KEY value, or leave empty in anonymous local mode.');

        new Setting(containerEl)
            .setName('OfferAgent URL')
            .setDesc('The URL of the OfferAgent backend.')
            .addText(text => text
                .setValue(`${this.plugin.settings.khojUrl}`)
                .onChange(async (value) => {
                    this.plugin.settings.khojUrl = value.trim().replace(/\/$/, '');
                    backendStatusMessage = await this.refreshConnectionState();

                    if (!this.plugin.settings.connectedToBackend) {
                        this.plugin.settings.availableChatModels = [];
                        this.plugin.settings.selectedChatModelId = null;
                    }
                    backendStatusEl.setText(this.connectStatusIcon())
                    connectHeaderEl.title = backendStatusMessage;
                    await this.refreshModelsAndServerPreference();
                }));

        // Interact section
        containerEl.createEl('h3', { text: 'Interact' });

        // Chat Model Dropdown
        this.renderChatModelDropdown();

        // Initial fetch of models and server preference if connected
        if (this.plugin.settings.connectedToBackend) {
            // Defer slightly to ensure UI is ready and avoid race conditions
            setTimeout(async () => {
                await this.refreshModelsAndServerPreference();
            }, 1000);
        }

        new Setting(containerEl)
            .setName('Results Count')
            .setDesc('The number of results to show in search and use for chat.')
            .addSlider(slider => slider
                .setLimits(1, 30, 1)
                .setValue(this.plugin.settings.resultsCount)
                .setDynamicTooltip()
                .onChange(async (value) => {
                    this.plugin.settings.resultsCount = value;
                    await this.plugin.saveSettings();
                }));

        // Add new "Sync" heading
        containerEl.createEl('h3', { text: 'Sync' });

        new Setting(containerEl)
            .setName('Auto Sync')
            .setDesc('Automatically index your vault with OfferAgent.')
            .addToggle(toggle => toggle
                .setValue(this.plugin.settings.autoConfigure)
                .onChange(async (value) => {
                    this.plugin.settings.autoConfigure = value;
                    await this.plugin.saveSettings();
                }));

        // Add setting to sync markdown notes
        new Setting(containerEl)
            .setName('Sync Notes')
            .setDesc('Index Markdown files in your vault with OfferAgent.')
            .addToggle(toggle => toggle
                .setValue(this.plugin.settings.syncFileType.markdown)
                .onChange(async (value) => {
                    this.plugin.settings.syncFileType.markdown = value;
                    await this.plugin.saveSettings();
                }));

        // Add setting to sync PDFs
        new Setting(containerEl)
            .setName('Sync PDFs')
            .setDesc('Index PDF files in your vault with OfferAgent.')
            .addToggle(toggle => toggle
                .setValue(this.plugin.settings.syncFileType.pdf)
                .onChange(async (value) => {
                    this.plugin.settings.syncFileType.pdf = value;
                    await this.plugin.saveSettings();
                }));

        // Add setting for sync interval
        const syncIntervalValues = [1, 5, 10, 20, 30, 45, 60, 120, 1440];
        new Setting(containerEl)
            .setName('Sync Interval')
            .setDesc('Minutes between automatic synchronizations')
            .addDropdown(dropdown => dropdown
                .addOptions(Object.fromEntries(
                    syncIntervalValues.map(value => [
                        value.toString(),
                        value === 1 ? '1 minute' :
                            value === 1440 ? '24 hours' :
                                `${value} minutes`
                    ])
                ))
                .setValue(this.plugin.settings.syncInterval.toString())
                .onChange(async (value) => {
                    this.plugin.settings.syncInterval = parseInt(value);
                    await this.plugin.saveSettings();
                    // Restart the timer with the new interval
                    this.plugin.restartSyncTimer();
                }));

        // Add setting to manage include folders
        const includeFoldersContainer = containerEl.createDiv('include-folders-container');
        new Setting(includeFoldersContainer)
            .setName('Include Folders')
            .setDesc('Folders to sync (leave empty to sync entire vault)')
            .addButton(button => button
                .setButtonText('Add Folder')
                .onClick(() => {
                    const modal = new FolderSuggestModal(this.app, async (folder: string) => {
                        if (!this.plugin.settings.syncFolders.includes(folder)) {
                            this.plugin.settings.syncFolders.push(folder);
                            await this.plugin.saveSettings();
                            this.updateIncludeFolderList(includeFolderListEl);
                        }
                    });
                    modal.open();
                }));

        // Create a list to display selected include folders
        const includeFolderListEl = includeFoldersContainer.createDiv('folder-list');
        this.updateIncludeFolderList(includeFolderListEl);

        // Add setting to manage exclude folders
        const excludeFoldersContainer = containerEl.createDiv('exclude-folders-container');
        new Setting(excludeFoldersContainer)
            .setName('Exclude Folders')
            .setDesc('Folders to exclude from sync (takes precedence over includes)')
            .addButton(button => button
                .setButtonText('Add Folder')
                .onClick(() => {
                    const modal = new FolderSuggestModal(this.app, async (folder: string) => {
                        // Don't allow excluding root folder
                        if (folder === '') {
                            new Notice('Cannot exclude the root folder');
                            return;
                        }
                        if (!this.plugin.settings.excludeFolders.includes(folder)) {
                            this.plugin.settings.excludeFolders.push(folder);
                            await this.plugin.saveSettings();
                            this.updateExcludeFolderList(excludeFolderListEl);
                        }
                    });
                    modal.open();
                }));

        // Create a list to display selected exclude folders
        const excludeFolderListEl = excludeFoldersContainer.createDiv('folder-list');
        this.updateExcludeFolderList(excludeFolderListEl);

        let indexVaultSetting = new Setting(containerEl);
        indexVaultSetting
            .setName('Force Sync')
            .setDesc('Manually force OfferAgent to re-index your Obsidian Vault.')
            .addButton(button => button
                .setButtonText('Update')
                .setCta()
                .onClick(async () => {
                    // Disable button while updating index
                    button.setButtonText('Updating 🌑');
                    button.removeCta();
                    indexVaultSetting = indexVaultSetting.setDisabled(true);

                    // Show indicator for indexing in progress (animated text)
                    const progress_indicator = window.setInterval(() => {
                        if (button.buttonEl.innerText === 'Updating 🌑') {
                            button.setButtonText('Updating 🌘');
                        } else if (button.buttonEl.innerText === 'Updating 🌘') {
                            button.setButtonText('Updating 🌗');
                        } else if (button.buttonEl.innerText === 'Updating 🌗') {
                            button.setButtonText('Updating 🌖');
                        } else if (button.buttonEl.innerText === 'Updating 🌖') {
                            button.setButtonText('Updating 🌕');
                        } else if (button.buttonEl.innerText === 'Updating 🌕') {
                            button.setButtonText('Updating 🌔');
                        } else if (button.buttonEl.innerText === 'Updating 🌔') {
                            button.setButtonText('Updating 🌓');
                        } else if (button.buttonEl.innerText === 'Updating 🌓') {
                            button.setButtonText('Updating 🌒');
                        } else if (button.buttonEl.innerText === 'Updating 🌒') {
                            button.setButtonText('Updating 🌑');
                        }
                    }, 300);
                    this.plugin.registerInterval(progress_indicator);

                    // Obtain sync progress elements by id (created below)
                    const syncProgressEl = document.getElementById('khoj-sync-progress') as HTMLProgressElement | null;
                    const syncProgressText = document.getElementById('khoj-sync-progress-text') as HTMLElement | null;

                    if (syncProgressEl && syncProgressText) {
                        syncProgressEl.style.display = '';
                        syncProgressText.style.display = '';
                        syncProgressText.textContent = 'Preparing files...';
                        syncProgressEl.value = 0;
                        syncProgressEl.max = 1;
                    }

                    const onProgress = (progress: { processed: number, total: number }) => {
                        const el = document.getElementById('khoj-sync-progress') as HTMLProgressElement | null;
                        const txt = document.getElementById('khoj-sync-progress-text') as HTMLElement | null;
                        if (!el || !txt) return;
                        el.max = Math.max(progress.total, 1);
                        el.value = Math.min(progress.processed, el.max);
                        txt.textContent = `Syncing... ${progress.processed} / ${progress.total} files`;
                    };

                    try {
                        this.plugin.settings.lastSync = await updateContentIndex(
                            this.app.vault,
                            this.plugin.settings,
                            this.plugin.server,
                            this.plugin.settings.lastSync,
                            true,
                            true,
                            onProgress,
                        );
                    } finally {
                        // Cleanup: hide sync progress UI
                        const el = document.getElementById('khoj-sync-progress') as HTMLProgressElement | null;
                        const txt = document.getElementById('khoj-sync-progress-text') as HTMLElement | null;
                        if (el) el.style.display = 'none';
                        if (txt) txt.style.display = 'none';
                        // Reset button state
                        window.clearInterval(progress_indicator);
                        button.setButtonText('Update');
                        button.setCta();
                        indexVaultSetting = indexVaultSetting.setDisabled(false);
                    }
                })
            );
        // Create progress bar for Force Sync operation (hidden by default)
        const syncProgressEl = document.createElement('progress');
        syncProgressEl.id = 'khoj-sync-progress';
        syncProgressEl.value = 0;
        syncProgressEl.max = 1;
        syncProgressEl.style.width = '100%';
        syncProgressEl.style.display = 'none';
        const syncProgressText = document.createElement('span');
        syncProgressText.id = 'khoj-sync-progress-text';
        syncProgressText.textContent = '';
        syncProgressText.style.display = 'none';
        indexVaultSetting.descEl.appendChild(syncProgressEl);
        indexVaultSetting.descEl.appendChild(syncProgressText);
    }

    private connectStatusIcon() {
        if (this.plugin.settings.connectedToBackend && this.plugin.settings.userInfo?.email)
            return '🟢';
        else if (this.plugin.settings.connectedToBackend)
            return '🟡'
        else
            return '🔴';
    }

    private async refreshConnectionState(): Promise<string> {
        this.plugin.server.configure(this.plugin.settings.khojUrl, this.plugin.settings.khojApiKey);
        const connection = await this.plugin.server.probe();
        this.plugin.settings.connectedToBackend = connection.connected;
        this.plugin.settings.userInfo = connection.user;
        await this.plugin.saveSettings();
        return getBackendStatusMessage(
            connection.connected,
            connection.user?.email,
            this.plugin.settings.khojUrl,
        );
    }

    private async refreshModelsAndServerPreference() {
        let serverSelectedModelId: string | null = null;
        if (this.plugin.settings.connectedToBackend) {
            try {
                const [availableModels, serverConfig] = await Promise.all([
                    this.plugin.server.getChatModels(),
                    this.plugin.server.getUserSettings(),
                ]);

                this.plugin.settings.availableChatModels = availableModels;

                if (serverConfig.selected_chat_model_config !== undefined) {
                    const serverModelIdStr = serverConfig.selected_chat_model_config.toString();
                    if (this.plugin.settings.availableChatModels.some(m => m.id === serverModelIdStr)) {
                        serverSelectedModelId = serverModelIdStr;
                    } else {
                        console.warn(`OfferAgent: Server model ${serverModelIdStr} is not available. Using default.`);
                    }
                }
                this.plugin.settings.selectedChatModelId = serverSelectedModelId;
            } catch (error) {
                console.error("OfferAgent: Failed to load model settings", error);
                this.plugin.settings.availableChatModels = [];
                this.plugin.settings.selectedChatModelId = null;
                this.plugin.settings.connectedToBackend = false;
            }
        } else {
            this.plugin.settings.availableChatModels = [];
            this.plugin.settings.selectedChatModelId = null; // Clear selection if disconnected
        }
        await this.plugin.saveSettings(); // Save the potentially updated selectedChatModelId
        this.renderChatModelDropdown(); // Re-render the dropdown with new data
    }

    private renderChatModelDropdown() {
        if (!this.chatModelSetting) {
            this.chatModelSetting = new Setting(this.containerEl)
                .setName('Chat Model');
        } else {
            // Clear previous description and controls to prepare for re-rendering
            this.chatModelSetting.descEl.empty();
            this.chatModelSetting.controlEl.empty();
        }
        // Use this.chatModelSetting directly for modifications
        const modelSetting = this.chatModelSetting;

        if (!this.plugin.settings.connectedToBackend) {
            modelSetting.setDesc('Connect to OfferAgent to load and set chat model options.');
            modelSetting.addText(text => text.setValue("Not connected").setDisabled(true));
            return;
        }

        if (this.plugin.settings.availableChatModels.length === 0 && this.plugin.settings.connectedToBackend) {
            modelSetting.setDesc('Fetching models or no models available. Check OfferAgent connection or try refreshing.');
            modelSetting.addButton(button => button
                .setButtonText('Refresh Models')
                .onClick(async () => {
                    button.setButtonText('Refreshing...').setDisabled(true);
                    await this.refreshModelsAndServerPreference();
                    // Re-rendering happens inside refreshModelsAndServerPreference
                }));
            return;
        }

        modelSetting.setDesc('The default AI model used for chat.');
        modelSetting.addDropdown(dropdown => {
            dropdown.addOption('', 'Default'); // Placeholder when cannot retrieve chat model options from server.
            this.plugin.settings.availableChatModels.forEach(model => {
                dropdown.addOption(model.id, model.name);
            });
            dropdown
                .setValue(this.plugin.settings.selectedChatModelId || '')
                .onChange(async (value) => {
                    try {
                        await this.plugin.server.updateChatModel(value);
                        this.plugin.settings.selectedChatModelId = value;
                        await this.plugin.saveSettings();
                    } catch (error) {
                        console.error("OfferAgent: Failed to update chat model", error);
                        new Notice("Failed to update chat model on the OfferAgent server.");
                        dropdown.setValue(this.plugin.settings.selectedChatModelId || '');
                    }
                });
        });
    }

    // Helper method to update the include folder list display
    private updateIncludeFolderList(containerEl: HTMLElement) {
        this.updateFolderList(
            containerEl,
            this.plugin.settings.syncFolders,
            'Including entire vault',
            async (folder) => {
                this.plugin.settings.syncFolders = this.plugin.settings.syncFolders.filter(f => f !== folder);
                await this.plugin.saveSettings();
                this.updateIncludeFolderList(containerEl);
            }
        );
    }

    // Helper method to update the exclude folder list display
    private updateExcludeFolderList(containerEl: HTMLElement) {
        this.updateFolderList(
            containerEl,
            this.plugin.settings.excludeFolders,
            'No folders excluded',
            async (folder) => {
                this.plugin.settings.excludeFolders = this.plugin.settings.excludeFolders.filter(f => f !== folder);
                await this.plugin.saveSettings();
                this.updateExcludeFolderList(containerEl);
            }
        );
    }

    // Shared helper to render a folder list with remove buttons
    private updateFolderList(
        containerEl: HTMLElement,
        folders: string[],
        emptyText: string,
        onRemove: (folder: string) => void
    ) {
        containerEl.empty();
        if (folders.length === 0) {
            containerEl.createEl('div', {
                text: emptyText,
                cls: 'folder-list-empty'
            });
            return;
        }

        const list = containerEl.createEl('ul', { cls: 'folder-list' });
        folders.forEach(folder => {
            const item = list.createEl('li', { cls: 'folder-list-item' });
            item.createSpan({ text: folder });

            const removeButton = item.createEl('button', {
                cls: 'folder-list-remove',
                text: '×'
            });
            removeButton.addEventListener('click', () => onRemove(folder));
        });
    }
}

// Modal with folder suggestions
class FolderSuggestModal extends SuggestModal<string> {
    constructor(app: App, private onChoose: (folder: string) => void) {
        super(app);
    }

    getSuggestions(query: string): string[] {
        const folders = this.getAllFolders();
        if (!query) return folders;

        return folders.filter(folder =>
            folder.toLowerCase().includes(query.toLowerCase())
        );
    }

    renderSuggestion(folder: string, el: HTMLElement) {
        el.createSpan({
            text: folder || '/',
            cls: 'folder-suggest-item'
        });
    }

    onChooseSuggestion(folder: string, _: MouseEvent | KeyboardEvent) {
        this.onChoose(folder);
    }

    private getAllFolders(): string[] {
        const folders = new Set<string>();
        folders.add(''); // Root folder

        // Get all files and extract folder paths
        this.app.vault.getAllLoadedFiles().forEach(file => {
            const folderPath = file.parent?.path;
            if (folderPath) {
                folders.add(folderPath);

                // Also add all parent folders
                let parent = folderPath;
                while (parent.includes('/')) {
                    parent = parent.substring(0, parent.lastIndexOf('/'));
                    folders.add(parent);
                }
            }
        });

        return Array.from(folders).sort();
    }
}
