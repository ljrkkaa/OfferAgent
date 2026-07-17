---
status: accepted
---

# Retain attachments for the Conversation lifetime

OfferAgent 将用户粘贴、拖入或选择的图片作为 Conversation Attachment 保存在 Vault 外，并保留到 Conversation 被删除，使历史消息能够继续显示图片，后续 Agent Run 也能再次引用。归档不删除附件；达到单会话或全局容量上限时，产品必须提示用户清理而不能静默淘汰。此决策取代 ADR-0017 的运行结束即删除规则，但继续遵守附件不进入 Vault、不成为知识库素材以及删除 Conversation 时彻底清理的边界。
