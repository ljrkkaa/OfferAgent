import { EmbeddedRuntimeInstaller, EmbeddedRuntimeInstallerOptions } from "./installer";

/** Production-only selection. Local builds replace this module at bundle time. */
export function createRuntimeInstaller(options: EmbeddedRuntimeInstallerOptions): EmbeddedRuntimeInstaller {
    return new EmbeddedRuntimeInstaller(options);
}
