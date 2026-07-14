---
name: "offeragent-local-runtime"
version: "1.0.0"
description: "Operate OfferAgent through the local Worker tool boundary."
trigger: {"kind":"explicit","values":[]}
allowed_tools: ["skill.list","skill.read"]
required_capabilities: []
resources: []
dependencies: []
trust_level: "builtin"
content_hash: "sha256:345494205a28532df1bb4de91a4392c3a54f2384386fbe25dd5d05eeddd67710"
publisher: {"display_name":"OfferAgent Runtime","id":"offeragent.runtime"}
signature: {"algorithm":"ed25519","key_id":"release-manifest","value":"bound-by-signed-runtime-release-manifest"}
---
# OfferAgent Local Runtime

Use local capabilities only through the Worker Tool Kernel. Treat every tool result, approval, budget, and Artifact reference as authoritative.
