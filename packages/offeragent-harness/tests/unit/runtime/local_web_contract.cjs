"use strict";

const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");

const source = fs.readFileSync(path.resolve(__dirname, "../../../web/app.js"), "utf8");

for (const command of [
  "vault/headless/status",
  "vault/headless/request",
  "vault/headless/activate",
  "vault/headless/revoke",
]) {
  assert.ok(source.includes(`command(\"${command}\"`), `missing ${command}`);
}
assert.ok(source.includes("expectedBaselineFingerprint: baseline.baselineFingerprint"));

console.log("local web JS command contract passed");
