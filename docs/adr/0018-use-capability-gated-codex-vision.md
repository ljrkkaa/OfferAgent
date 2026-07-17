---
status: accepted
---

# Use capability-gated Codex vision without local OCR

OfferAgent 使用 Codex Responses 兼容输入中的原生 `input_image` 理解 Run Attachment，并按当前订阅后端与模型探测 Vision Capability；本机 `codex-cli 0.135.0` 已通过同一 Codex 订阅链路实测 `gpt-5.4` 能准确读取面经截图。首版不捆绑本地 OCR，因为原生视觉同时理解文字、版面与上下文，而 OCR 会引入中文语言包、原生依赖和第二套降级语义；能力不可用时应明确要求用户切换支持视觉的模型或提供文本。
