export const PROTOCOL_VERSION = 1 as const;

export interface RuntimeHandshake {
  instanceId: string;
  pid: number;
  port: number;
  protocolVersion: typeof PROTOCOL_VERSION;
}

export interface RuntimeHealth {
  instanceId: string;
  protocolVersion: typeof PROTOCOL_VERSION;
  status: "healthy";
}

export interface RuntimeShutdown {
  status: "shutting_down";
}

export interface RuntimeError {
  code: "not_found" | "unauthorized";
  message: string;
}
