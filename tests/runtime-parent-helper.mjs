import { spawn } from "node:child_process";

const [runtimeEntry, token] = process.argv.slice(2);
const runtime = spawn(
  process.execPath,
  [runtimeEntry, "--port", "0", "--token", token, "--parent-pid", `${process.pid}`],
  { stdio: ["ignore", "pipe", "ignore"], windowsHide: true },
);

let stdout = "";
runtime.stdout.on("data", (chunk) => {
  stdout += chunk.toString("utf8");
  const newline = stdout.indexOf("\n");
  if (newline === -1) return;
  process.stdout.write(stdout.slice(0, newline + 1), () => process.exit(0));
});
