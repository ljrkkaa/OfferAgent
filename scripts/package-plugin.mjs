import { copyFile, mkdir } from "node:fs/promises";
import path from "node:path";
import { fileURLToPath } from "node:url";

const repositoryRoot = path.resolve(
  path.dirname(fileURLToPath(import.meta.url)),
  "..",
);
const pluginRoot = path.join(repositoryRoot, "packages", "plugin");
const distribution = path.join(pluginRoot, "dist");

await mkdir(distribution, { recursive: true });
await Promise.all([
  copyFile(path.join(pluginRoot, "manifest.json"), path.join(distribution, "manifest.json")),
  copyFile(path.join(pluginRoot, "styles.css"), path.join(distribution, "styles.css")),
  copyFile(
    path.join(repositoryRoot, "packages", "runtime", "dist", "cli.js"),
    path.join(distribution, "runtime.js"),
  ),
]);
