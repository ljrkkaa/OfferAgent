import { once } from "node:events";
import { request } from "node:http";

export function handshake(stream) {
  return new Promise((resolve, reject) => {
    let buffer = "";
    const timeout = setTimeout(() => reject(new Error("Runtime handshake timed out")), 5_000);
    stream.on("data", function onData(chunk) {
      buffer += chunk.toString("utf8");
      const newline = buffer.indexOf("\n");
      if (newline < 0) return;
      clearTimeout(timeout);
      stream.off("data", onData);
      resolve(JSON.parse(buffer.slice(0, newline)));
    });
  });
}

export async function stopRuntime(runtime, port, token) {
  const exited = once(runtime, "exit");
  await new Promise((resolve, reject) => {
    const outgoing = request(
      {
        host: "127.0.0.1",
        port,
        path: "/shutdown",
        method: "POST",
        headers: { authorization: `Bearer ${token}` },
      },
      (response) => {
        response.resume();
        response.on("end", resolve);
      },
    );
    outgoing.once("error", reject);
    outgoing.end();
  });
  await exited;
}

export function runWithToolPeer(
  socket,
  requestPayload,
  resultForCall,
  memoryTopics = [],
  contractContent = "# Test Agent Contract",
  memoryTruncated = false,
) {
  return new Promise((resolve, reject) => {
    const events = [];
    const timeout = setTimeout(
      () => reject(new Error(`Agent tool loop timed out: ${JSON.stringify(events.slice(-8))}`)),
      20_000,
    );
    socket.on("message", function onMessage(data) {
      const event = JSON.parse(data.toString("utf8"));
      if (event.agentRunId !== requestPayload.agentRunId) return;
      events.push(event);
      if (event.type === "tool_call.requested" && event.tool.kind === "local") {
        const result =
          event.tool.name === "agent_contract_read"
            ? {
                ok: true,
                value: {
                  type: "agent_contract_read",
                  path: "agent.md",
                  modifiedVersion: "mtime:1:size:24",
                  contentHash: "sha256:test-contract",
                  content: contractContent,
                },
              }
            : event.tool.name === "planning_memory_list"
              ? {
                  ok: true,
                  value: {
                    type: "planning_memory_list",
                    topics: memoryTopics,
                    truncated: memoryTruncated,
                  },
                }
              : resultForCall(event);
        socket.send(
          JSON.stringify({
            type: "tool_result",
            protocolVersion: 1,
            eventId: `result-${event.toolCallId}`,
            conversationId: event.conversationId,
            agentRunId: event.agentRunId,
            sequence: event.sequence,
            toolCallId: event.toolCallId,
            result,
          }),
        );
      }
      if (event.type === "agent_run.completed" || event.type === "agent_run.failed") {
        clearTimeout(timeout);
        socket.off("message", onMessage);
        resolve(events);
      }
    });
    socket.send(JSON.stringify(requestPayload));
  });
}
