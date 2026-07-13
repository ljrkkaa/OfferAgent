---
status: accepted
---

# Use an Obsidian plugin with a local runtime process

OfferAgent 作为一个单用户本地产品交付，但运行时由 Obsidian 插件和插件自动启动的无窗口本地 Runtime 两个进程组成。这样可以把模型凭据和 Codex SDK 留在本地服务端环境中，隔离模型调用故障，并为 Obsidian 关闭后的任务执行保留空间；纯插件方案会暴露凭据并限制后台执行，而保留完整 Python/Khoj 服务又会引入本产品不需要的多用户和服务器复杂度。

## Consequences

- Runtime 只能监听本机地址，不能成为远程服务。
- 插件负责启动、健康检查和呈现 Runtime 故障。
- 插件与 Runtime 之间需要一个版本化的本地协议。
