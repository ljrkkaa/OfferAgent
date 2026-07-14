import type { ExtensionRequestClient } from "../src/local/extension_settings";
import type { ProtocolRequestClient } from "../src/runtime/harness_client";

declare const protocol: ProtocolRequestClient;
declare const extensions: ExtensionRequestClient;

// Positive controls prove the fixture is checking the public typed surface.
void protocol.request("session/get", { sessionId: "sess_type_contract", includeTurns: true });
void extensions.request("process/registrations/list", {});

// There is deliberately no permissive public string/JsonObject overload.
// @ts-expect-error unknown methods must fail at compile time
void protocol.request("session/geet", {});
// @ts-expect-error required params cannot disappear through a generic overload
void protocol.request("session/get", {});
// @ts-expect-error params from another command cannot be substituted
void protocol.request("session/get", { runId: "run_wrong_shape" });
// @ts-expect-error extension process confirmation is bound to its full challenge
void extensions.request("process/registrations/confirm", { challengeId: "challenge_only" });
