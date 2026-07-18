---
status: accepted
---

# Use catalog-gated Codex vision without local OCR

OfferAgent 使用 Codex Responses 的原生 `input_image` 理解 Run Attachment，图片资格只取自当前账户绑定的实时模型目录，并把真实附件仅放入原始 USER 消息；不发送合成探测图，也不维护第二套视觉缓存。首版不捆绑本地 OCR，因为原生视觉同时理解文字、版面与上下文，而 OCR 会引入中文语言包、原生依赖和第二套降级语义；目录未声明图片输入时在本地拒绝，并要求用户选择支持图片的模型或提供文本。[OpenAI 图片输入文档](https://developers.openai.com/api/docs/guides/images-vision)同时确认了 Responses 的 USER 图片示例、支持格式、批次上限与 `high`/`original` 细节语义。
