import {
    LocalDevelopmentRuntimeInstaller,
    LocalDevelopmentRuntimeInstallerOptions,
} from "./local_development_installer";

interface LocalDevelopmentSelectionOptions extends LocalDevelopmentRuntimeInstallerOptions {
    readonly vaultRoot: string;
    readonly releasePublicKeys: Readonly<Record<string, string>>;
    readonly ownerId?: string;
    readonly legacyOwnerId?: string | null;
    readonly approvePrivilegeExpansion?: (request: unknown) => Promise<boolean>;
}

/** Compile-time local selection; release bundles never resolve this source. */
export function createRuntimeInstaller(
    options: LocalDevelopmentSelectionOptions,
): LocalDevelopmentRuntimeInstaller {
    return new LocalDevelopmentRuntimeInstaller(options);
}
