import assert from "node:assert/strict";
import { readFile, stat } from "node:fs/promises";
import { fileURLToPath } from "node:url";
import path from "node:path";
import test from "node:test";

const repositoryRoot = path.resolve(
  path.dirname(fileURLToPath(import.meta.url)),
  "..",
);
const pluginPackage = path.join(repositoryRoot, "packages", "plugin", "dist");

test("the production build emits a loadable desktop plugin with its Runtime", async () => {
  const expectedFiles = ["main.js", "manifest.json", "styles.css", "runtime.js"];
  for (const file of expectedFiles) {
    const details = await stat(path.join(pluginPackage, file));
    assert.ok(details.isFile());
    assert.ok(details.size > 0, `${file} should not be empty`);
  }

  const manifest = JSON.parse(
    await readFile(path.join(pluginPackage, "manifest.json"), "utf8"),
  );
  assert.deepEqual(manifest, {
    id: "offeragent",
    name: "OfferAgent",
    version: "0.1.0",
    minAppVersion: "1.6.0",
    description: "A local, evidence-based interview study agent.",
    author: "OfferAgent",
    main: "main.js",
    isDesktopOnly: true,
  });
});
