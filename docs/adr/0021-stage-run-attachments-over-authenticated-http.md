---
status: superseded
---

# Stage Run Attachments over authenticated local HTTP

Superseded by ADR 0025.

Obsidian 插件通过 Runtime 的本机认证 HTTP 端点上传 Run Attachment 二进制，Runtime 校验文件签名、类型和大小后写入临时附件区并返回不透明 Attachment ID、Hash 与元数据；随后 WebSocket 上的 Agent Run 命令只引用这些 ID。这个端点窄化修正 ADR 0007：Conversation、Agent Run、工具、流式输出和恢复事件仍由可重放 WebSocket 承载，但不可重放的大体积二进制不进入 JSON 事件历史。上传失败或 Run 未成功启动时必须立即清理，Attachment ID 只能由所属 Conversation 和 Agent Run 使用。
