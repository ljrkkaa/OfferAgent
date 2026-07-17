---
status: accepted
---

# Capability-gate Codex hosted web search

OfferAgent 在模型请求中允许同时声明本地 Function Tool 和 Provider 托管工具。首版尝试向 Codex Responses 兼容后端声明 `{ "type": "web_search" }`，由 Provider 执行互联网搜索并返回 `web_search_call`、来源和 `url_citation`；Vault 工具仍由 OfferAgent Runtime 与 Obsidian 插件执行。

当前 `CodexSubscriptionProvider` 使用 `chatgpt.com/backend-api/codex`，该订阅后端没有公开兼容性保证，因此 Hosted Web Search 必须作为经过运行时探测的 Provider Capability，而不是无条件可用的 Agent 工具。

## Behavior

1. Provider 在首次需要 Web Search 时执行最小能力探测，并缓存当前后端与模型的探测结果。
2. 支持时向统一 Agent 循环暴露 Hosted Web Search，并解析搜索调用、来源和可点击引用。
3. 后端返回不支持的工具或参数错误时，自动重试一次不带 Hosted Web Search 的请求，将能力标记为不可用，并保留用户提供 URL 的 `web_read`。
4. 设置页展示 `available`、`unavailable` 或 `unknown`，允许用户重新探测。
5. 不为首版引入 Serper、Exa、Firecrawl、Google Custom Search 或 SearXNG 密钥。

## Consequences

- 使用 Hosted Web Search 不会引入 Codex Agent；Agent 的状态机、工具循环和 Vault 权限仍由 OfferAgent 自己实现。
- Provider 工具定义必须使用可区分联合类型，不能再把所有工具都转换成普通 Function Tool。
- 聊天界面必须把 Web Search 返回的引用显示为可点击来源。
- 探测失败和功能降级不能导致整个 Agent Run 失败。
