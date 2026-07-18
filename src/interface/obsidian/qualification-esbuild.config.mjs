import esbuild from "esbuild";
import process from "process";
import path from "path";
import builtins from "builtin-modules";

const arguments_ = process.argv.slice(2);
const outputArguments = arguments_.filter((value) => value.startsWith("--outfile="));
if (outputArguments.length !== 1 || arguments_.length !== 1) {
    throw new Error("qualification build requires one explicit outfile");
}
const requestedOutput = outputArguments[0].slice("--outfile=".length);
if (!requestedOutput || requestedOutput.includes("\0")) throw new Error("invalid qualification outfile");

await esbuild.build({
    entryPoints: ["src/qualification_driver.ts"],
    bundle: true,
    external: [...builtins, "node:*"],
    format: "cjs",
    platform: "node",
    target: "node16",
    logLevel: "info",
    sourcemap: false,
    treeShaking: true,
    outfile: path.resolve(requestedOutput),
});
