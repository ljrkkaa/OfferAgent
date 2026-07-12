import { App, MarkdownView, TFile, TFolder } from 'obsidian';

export type VaultAction =
    | { op: 'create_file'; path: string; content: string; mode: 'create_only' }
    | { op: 'append_file'; path: string; content: string; heading?: string; mode: 'append' }
    | { op: 'replace_text'; path: string; find: string; replace: string; mode: 'replace'; reason?: string };

export interface VaultActionResult {
    action: VaultAction;
    success: boolean;
    status: 'applied' | 'rolled_back' | 'not_applied' | 'manual_review_required';
    path: string;
    error?: string;
}

function isRecord(value: unknown): value is Record<string, unknown> {
    return typeof value === 'object' && value !== null;
}

function hasExactKeys(value: Record<string, unknown>, required: string[], optional: string[] = []): boolean {
    const allowed = new Set([...required, ...optional]);
    return required.every(key => Object.prototype.hasOwnProperty.call(value, key))
        && Object.keys(value).every(key => allowed.has(key));
}

function isVaultAction(value: unknown): value is VaultAction {
    if (!isRecord(value) || typeof value.op !== 'string' || typeof value.path !== 'string') return false;
    if (value.op === 'create_file') {
        return hasExactKeys(value, ['op', 'path', 'content', 'mode'])
            && typeof value.content === 'string'
            && value.mode === 'create_only';
    }
    if (value.op === 'append_file') {
        return hasExactKeys(value, ['op', 'path', 'content', 'mode'], ['heading'])
            && typeof value.content === 'string'
            && value.mode === 'append'
            && (value.heading === undefined || typeof value.heading === 'string');
    }
    return value.op === 'replace_text'
        && hasExactKeys(value, ['op', 'path', 'find', 'replace', 'mode'], ['reason'])
        && typeof value.find === 'string'
        && value.find.length > 0
        && typeof value.replace === 'string'
        && value.mode === 'replace'
        && (value.reason === undefined || typeof value.reason === 'string');
}

export function parseVaultActions(value: unknown): VaultAction[] | null {
    if (!isRecord(value)
        || !hasExactKeys(value, ['actions'])
        || !Array.isArray(value.actions)
        || !value.actions.every(isVaultAction)) return null;
    return value.actions;
}

export function vaultActionReview(action: VaultAction): { summary: string; details: string } {
    const summary = `${action.op}: ${action.path}`;
    if (action.op === 'create_file') return { summary, details: `Content:\n${action.content}` };
    if (action.op === 'append_file') {
        const heading = action.heading ? `Heading: ${action.heading}\n\n` : '';
        return { summary, details: `${heading}Content:\n${action.content}` };
    }
    const reason = action.reason ? `Reason: ${action.reason}\n\n` : '';
    return {
        summary,
        details: `${reason}Find:\n${action.find}\n\nReplace with:\n${action.replace}`,
    };
}

interface PlannedFile {
    path: string;
    original: string | null;
    content: string;
    exists: boolean;
}

function errorMessage(error: unknown): string {
    return error instanceof Error ? error.message : String(error);
}

export class FileInteractions {
    private readonly CONTEXT_FILES_LIMIT = 3;

    constructor(private app: App) {}

    private getRecentActiveMarkdownFiles(limit: number): TFile[] {
        const seen = new Set<string>();
        return this.app.workspace.getLeavesOfType('markdown')
            .sort((a, b) => (b as any).activeTime - (a as any).activeTime)
            .map(leaf => (leaf.view as MarkdownView)?.file)
            .filter((file): file is TFile => {
                if (!file || seen.has(file.path)) return false;
                seen.add(file.path);
                return true;
            })
            .slice(0, limit);
    }

    public async getOpenFilesContent(fileAccessMode: 'none' | 'read' | 'write'): Promise<string> {
        if (fileAccessMode === 'none') return '';

        const files = this.getRecentActiveMarkdownFiles(this.CONTEXT_FILES_LIMIT);
        if (files.length === 0) return '';

        const sections: string[] = [];
        for (const file of files) {
            try {
                sections.push(`<OPEN_FILE>\n# file: ${file.path}\n\n${await this.app.vault.read(file)}\n</OPEN_FILE>`);
            } catch (error) {
                console.error(`Error reading file ${file.path}:`, error);
            }
        }
        if (sections.length === 0) return '';
        return `\n\n<SYSTEM>\nFor context, the user is currently working on these files. Vault changes must use the structured vault tools.\n<WORKING_FILE_SET>\n${sections.join('\n\n')}\n</WORKING_FILE_SET>\n</SYSTEM>`;
    }

    private getSafeVaultActionPath(filePath: string): string | null {
        const target = filePath.trim().replace(/\\/g, '/');
        if (!target || target.startsWith('/') || target.endsWith('/')) return null;
        if (!target.endsWith('.md') && !target.endsWith('.txt')) return null;
        if (target.split('/').some(part => !part || part === '.' || part === '..')) return null;
        return target;
    }

    private appendContent(existing: string, content: string, heading?: string): string {
        const normalizedContent = content.endsWith('\n') ? content : `${content}\n`;
        if (!heading) {
            const separator = existing.endsWith('\n') || existing.length === 0 ? '' : '\n';
            return `${existing}${separator}${normalizedContent}`;
        }

        const lines = existing.split('\n');
        const escapedHeading = heading.replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
        const headingPattern = new RegExp(`^(#{1,6})\\s+${escapedHeading}\\s*$`);
        const headingIndex = lines.findIndex(line => headingPattern.test(line));
        if (headingIndex === -1) {
            const separator = existing.endsWith('\n') || existing.length === 0 ? '' : '\n';
            return `${existing}${separator}\n## ${heading}\n\n${normalizedContent}`;
        }

        const headingLevel = lines[headingIndex].match(/^#+/)?.[0].length ?? 1;
        let insertIndex = lines.length;
        for (let index = headingIndex + 1; index < lines.length; index++) {
            const match = lines[index].match(/^(#{1,6})\s+/);
            if (match && match[1].length <= headingLevel) {
                insertIndex = index;
                break;
            }
        }

        const before = lines.slice(0, insertIndex);
        if (before.length > 0 && before[before.length - 1].trim() !== '') before.push('');
        return [...before, ...normalizedContent.trimEnd().split('\n'), ...lines.slice(insertIndex)]
            .join('\n')
            .replace(/\n?$/, '\n');
    }

    private async planVaultActions(actions: VaultAction[]): Promise<{ files: PlannedFile[]; paths: string[] }> {
        const files = new Map<string, PlannedFile>();
        const paths: string[] = [];

        for (const action of actions) {
            const safePath = this.getSafeVaultActionPath(action.path);
            if (!safePath) throw new Error(`Unsafe vault action path: ${action.path}`);
            paths.push(safePath);

            let planned = files.get(safePath);
            if (!planned) {
                const existing = this.app.vault.getAbstractFileByPath(safePath);
                if (existing && !(existing instanceof TFile)) {
                    throw new Error(`Vault action target is not a file: ${safePath}`);
                }
                const original = existing instanceof TFile ? await this.app.vault.read(existing) : null;
                planned = { path: safePath, original, content: original ?? '', exists: original !== null };
                files.set(safePath, planned);
            }

            if (action.op === 'create_file') {
                if (planned.exists) throw new Error(`File already exists: ${safePath}`);
                planned.content = action.content;
                planned.exists = true;
            } else if (action.op === 'append_file') {
                if (!planned.exists) throw new Error(`File does not exist: ${safePath}`);
                planned.content = this.appendContent(planned.content, action.content, action.heading);
            } else {
                if (!planned.exists) throw new Error(`File does not exist: ${safePath}`);
                if (!action.find) throw new Error('replace_text requires a non-empty find value');
                const matches = planned.content.split(action.find).length - 1;
                if (matches !== 1) {
                    throw new Error(`replace_text expected exactly one match in ${safePath}, found ${matches}`);
                }
                planned.content = planned.content.replace(action.find, action.replace);
            }
        }

        return { files: [...files.values()], paths };
    }

    public async applyVaultActions(actions: VaultAction[]): Promise<VaultActionResult[]> {
        if (actions.length === 0) return [];

        let plan: { files: PlannedFile[]; paths: string[] };
        try {
            plan = await this.planVaultActions(actions);
        } catch (error) {
            const message = errorMessage(error);
            return actions.map(action => ({
                action,
                success: false,
                status: 'not_applied',
                path: action.path,
                error: message,
            }));
        }

        const modified: PlannedFile[] = [];
        const createdFiles: PlannedFile[] = [];
        const createdFolders: string[] = [];
        try {
            for (const file of plan.files) {
                const current = this.app.vault.getAbstractFileByPath(file.path);
                if (file.original === null) {
                    if (current) throw new Error(`File appeared before apply: ${file.path}`);
                    createdFolders.push(...await this.ensureParentFolders(file.path));
                    await this.app.vault.create(file.path, file.content);
                    createdFiles.push(file);
                } else {
                    if (!(current instanceof TFile)) throw new Error(`File disappeared before apply: ${file.path}`);
                    await this.app.vault.process(current, content => {
                        if (content !== file.original) throw new Error(`File changed before apply: ${file.path}`);
                        return file.content;
                    });
                    modified.push(file);
                }
            }
        } catch (error) {
            const rollbackErrors = await this.rollback(modified);
            const preservedPaths = [
                ...createdFiles.map(file => file.path),
                ...createdFolders,
            ];
            if (preservedPaths.length > 0) {
                rollbackErrors.push(`manual review required: preserved created paths: ${preservedPaths.join(', ')}`);
            }
            const status = rollbackErrors.length > 0
                ? 'manual_review_required'
                : modified.length > 0
                    ? 'rolled_back'
                    : 'not_applied';
            const message = [errorMessage(error), ...rollbackErrors].join('; ');
            return actions.map(action => ({ action, success: false, status, path: action.path, error: message }));
        }

        return actions.map((action, index) => ({
            action,
            success: true,
            status: 'applied',
            path: plan.paths[index],
        }));
    }

    private async ensureParentFolders(filePath: string): Promise<string[]> {
        const created: string[] = [];
        let currentPath = '';
        for (const folder of filePath.split('/').slice(0, -1)) {
            currentPath = currentPath ? `${currentPath}/${folder}` : folder;
            const existing = this.app.vault.getAbstractFileByPath(currentPath);
            if (!existing) {
                await this.app.vault.createFolder(currentPath);
                created.push(currentPath);
            } else if (!(existing instanceof TFolder)) {
                throw new Error(`Cannot create folder "${currentPath}" because a file already exists there`);
            }
        }
        return created;
    }

    private async rollback(modified: PlannedFile[]): Promise<string[]> {
        const errors: string[] = [];
        for (const file of [...modified].reverse()) {
            try {
                const current = this.app.vault.getAbstractFileByPath(file.path);
                if (!(current instanceof TFile) || file.original === null) {
                    throw new Error(`rollback conflict: file disappeared: ${file.path}`);
                }
                await this.app.vault.process(current, content => {
                    if (content !== file.content) {
                        throw new Error(`rollback conflict: file changed after apply: ${file.path}`);
                    }
                    return file.original as string;
                });
            } catch (error) {
                errors.push(errorMessage(error));
            }
        }
        return errors;
    }
}
