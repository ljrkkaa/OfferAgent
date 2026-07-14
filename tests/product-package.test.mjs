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

test("the packaged Sidebar stays theme-neutral, keyboard-visible, and narrow-safe", async () => {
  const styles = await readFile(path.join(pluginPackage, "styles.css"), "utf8");

  assert.match(styles, /background:\s*var\(--background-primary\)/);
  assert.match(styles, /background:\s*var\(--background-secondary/);
  assert.match(styles, /:focus-visible/);
  assert.match(styles, /overflow-wrap:\s*anywhere/);
  assert.match(styles, /@media\s*\(max-width:\s*360px\)/);
  assert.match(styles, /offeragent-sidebar__message--user[^}]*max-width:\s*82%/s);
  assert.match(styles, /offeragent-sidebar__message--assistant[^}]*width:\s*100%/s);
  assert.doesNotMatch(styles, /#[0-9a-f]{3,8}\b|rgba?\(|hsla?\(/i);
});
