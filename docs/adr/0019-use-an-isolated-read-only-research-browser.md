---
status: accepted
---

# Use an isolated read-only Research Browser

OfferAgent 使用带独立 Profile 的 Research Browser 访问牛客、小红书等需要动态页面或登录态的研究来源，而不读取用户日常 Chrome 的 Cookie 或标签页。用户在隔离浏览器中手动登录，Agent 只在用户启动的 Agent Run 内根据研究目标自主跨站搜索、跳转、翻页和读取，不设置逐域名授权名单；首版禁止发帖、评论、点赞、收藏、关注、私信和其他写入式网页操作。这个隔离以额外登录和浏览器运行成本换取可审计的副作用边界，避免把面经研究扩张成对用户个人浏览器的通用控制。

实现使用独立持久化 Electron partition，仅向 Agent 暴露公开 URL 打开、渲染内容读取、链接枚举后跟随、
下一页/滚动和返回。所有连接通过本机代理固定到已验证的公网 DNS 结果，拒绝本机、私网和重绑定目标；
新窗口、站点权限和下载均被拒绝。页面正文标记为不可信数据，读取结果产生可点击的 Web Source Reference，
WebSocket 与 WebTransport 类双向资源被请求类型白名单拒绝，WebRTC 的非代理 UDP 路径被关闭；
页面不能提供 Agent 指令或触发发布、表单、上传及社交操作。
