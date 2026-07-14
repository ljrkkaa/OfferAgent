---
status: accepted
---

# Store Run Attachments temporarily outside the Vault

OfferAgent 将用户粘贴、拖入或选择的图片临时保存在 `%LOCALAPPDATA%\OfferAgent\attachments\`，使 Interrupted Run 能在 Obsidian 或 Runtime 重启后继续使用同一输入。附件不复制进 Vault、不成为知识库原始素材，并在 Agent Run 完成或取消、Conversation 删除时立即清理；异常遗留文件由短期 TTL 清理。纯内存附件虽然更少落盘，但会破坏已接受的显式恢复语义。
