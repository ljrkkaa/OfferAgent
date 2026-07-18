const assert = require("node:assert/strict");
const path = require("node:path");
const test = require("node:test");
const { buildSync } = require("esbuild");

function loadModule() {
    const output = buildSync({
        entryPoints: [path.join(__dirname, "../src/runtime/interview_source_identity.ts")],
        bundle: true,
        format: "cjs",
        platform: "node",
        target: "node16",
        write: false,
    }).outputFiles[0].text;
    const compiled = { exports: {} };
    new Function("require", "module", "exports", output)(require, compiled, compiled.exports);
    return compiled.exports;
}

const imageA = `sha256:${"a".repeat(64)}`;
const imageB = `sha256:${"b".repeat(64)}`;

test("raw Interview source identity removes tracking and credentials before stable ordering", () => {
    const { normalizeInterviewSourceIdentity } = loadModule();

    assert.deepEqual(normalizeInterviewSourceIdentity({
        sourceUrls: [
            "HTTPS://Example.COM/post?z=last&utm_source=feed&access_token=secret&a=first#comments",
            "https://docs.example.com/guide?b=2&a=3&a=1&X-Amz-Signature=secret&expires=123",
        ],
        orderedImageContentHashes: [imageA, imageB],
    }), {
        canonicalUrls: [
            "https://example.com/post?a=first&z=last",
            "https://docs.example.com/guide?a=1&a=3&b=2",
        ],
        orderedImageContentHashes: [imageA, imageB],
        sourceFingerprint: "sha256:0cd65e3ab3208489c6bfaa9d2ce2cb125ae8fbef34c9a965c1efe3f7d2302c37",
    });
});

test("raw Interview source identity strips common secret, password, OAuth and JWT parameters", () => {
    const { normalizeInterviewSourceIdentity } = loadModule();

    assert.deepEqual(normalizeInterviewSourceIdentity({
        sourceUrls: [
            "https://example.com/interview?secret=top-secret&password=hunter2&code=oauth-code" +
                "&jwt=header.payload.signature&client_secret=client-value&topic=backend",
        ],
    }), {
        canonicalUrls: ["https://example.com/interview?topic=backend"],
        orderedImageContentHashes: [],
        sourceFingerprint: null,
    });
});

test("canonical Interview source identity accepts only the exact normalized receipt", () => {
    const { validateCanonicalInterviewSourceIdentity } = loadModule();
    const identity = {
        canonicalUrls: ["https://example.com/interview?a=first&z=last"],
        orderedImageContentHashes: [imageA, imageB],
        sourceFingerprint: "sha256:0cd65e3ab3208489c6bfaa9d2ce2cb125ae8fbef34c9a965c1efe3f7d2302c37",
    };

    assert.deepEqual(validateCanonicalInterviewSourceIdentity(identity), identity);
    assert.equal(validateCanonicalInterviewSourceIdentity({
        ...identity,
        canonicalUrls: ["https://EXAMPLE.com/interview?z=last&utm_source=feed&a=first#comments"],
    }), null);
});

test("Interview source identity rejects private IPv4 and IPv6 source ranges", () => {
    const { normalizeInterviewSourceIdentity } = loadModule();
    const nonPublicUrls = [
        "http://127.0.0.1/interview",
        "http://10.0.0.7/interview",
        "http://100.64.0.1/interview",
        "http://169.254.1.2/interview",
        "http://172.16.0.1/interview",
        "http://192.168.0.1/interview",
        "http://198.18.0.1/interview",
        "http://203.0.113.7/interview",
        "http://[::1]/interview",
        "http://[::ffff:127.0.0.1]/interview",
        "http://[::ffff:0a00:0007]/interview",
        "http://[fc00::1]/interview",
        "http://[fe80::1]/interview",
        "http://[ff00::1]/interview",
        "http://[2001:db8::1]/interview",
        "http://[100::1]/interview",
        "http://[3fff::1]/interview",
    ];

    for (const sourceUrl of nonPublicUrls) {
        assert.equal(normalizeInterviewSourceIdentity({ sourceUrls: [sourceUrl] }), null, sourceUrl);
    }
});

test("Interview image identity changes when the same pages are reordered", () => {
    const { normalizeInterviewSourceIdentity } = loadModule();

    assert.equal(
        normalizeInterviewSourceIdentity({ orderedImageContentHashes: [imageA, imageB] }).sourceFingerprint,
        "sha256:0cd65e3ab3208489c6bfaa9d2ce2cb125ae8fbef34c9a965c1efe3f7d2302c37",
    );
    assert.equal(
        normalizeInterviewSourceIdentity({ orderedImageContentHashes: [imageB, imageA] }).sourceFingerprint,
        "sha256:91248f80db44bc7684df6335570b2143d476a4ee87edb7baa2c896383e283aba",
    );
});

test("canonical Interview source validation rejects a fingerprint from another page order", () => {
    const { validateCanonicalInterviewSourceIdentity } = loadModule();

    assert.equal(validateCanonicalInterviewSourceIdentity({
        canonicalUrls: [],
        orderedImageContentHashes: [imageB, imageA],
        sourceFingerprint: "sha256:0cd65e3ab3208489c6bfaa9d2ce2cb125ae8fbef34c9a965c1efe3f7d2302c37",
    }), null);
});
