---
status: accepted
---

# Use an isolated read-only Research Browser

OfferAgent 使用带独立 Profile 的 Research Browser 访问牛客、小红书等需要动态页面或登录态的研究来源，而不读取用户日常 Chrome 的 Cookie 或标签页。用户在隔离浏览器中手动登录，Agent 只在用户启动的 Agent Run 内根据研究目标自主跨站搜索、跳转、翻页和读取，不设置逐域名授权名单；首版禁止发帖、评论、点赞、收藏、关注、私信和其他写入式网页操作。这个隔离以额外登录和浏览器运行成本换取可审计的副作用边界，避免把面经研究扩张成对用户个人浏览器的通用控制。
