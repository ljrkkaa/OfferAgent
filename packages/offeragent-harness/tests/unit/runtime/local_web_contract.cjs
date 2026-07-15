"use strict";

const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");

const source = fs.readFileSync(path.resolve(__dirname, "../../../web/app.js"), "utf8");

assert.doesNotMatch(source, /vault\/headless|headlessVaultWrite|clientTools|reverseRequests/u);
assert.doesNotMatch(source, /memory\/(?:settings|configure|list|get|review|edit|delete|export)/u);

console.log("local web single Worker tool authority contract passed");
