# OfferAgent for Obsidian

这是 OfferAgent Windows 本地 Runtime 的桌面端 Obsidian 客户端。插件负责安装和连接签名的
Host/Worker、提供 Obsidian Context/Client Tools，并渲染同一 Worker 的 Session、Run、工具、
审批与诊断事件。

插件不连接 OfferAgent/Khoj Server，不上传或同步 Vault 内容，也不实现第二套 Agent Loop。
Client Tool 对 Vault 写入仅提供活动编辑器、预览证明和同 Pipe 的提交后只读观察；磁盘
create/append/patch/rename/trash 全部由 Worker 的单一原子事务协调器执行。Worker 在插件确认
编辑器仍关闭且磁盘 after-hash 精确匹配前保留回滚能力，断连或状态漂移会回滚。受影响文件仍
打开或未保存时会明确冲突并要求先保存、关闭后重新审批，插件不会用编辑器 debounce 冒充持久化成功。
生产 IPC 使用当前 Windows SID 专属 Named Pipe；“打开本地 Web”只访问同一 Worker 绑定的
随机 Loopback 端口。模型 Provider URL 仅属于用户选择的推理边界，本地模型可完全断网运行。

个人本机开发安装默认选择 `deepseek-v4-flash`。DeepSeek 只由 Worker 访问固定官方 Chat Completions
端点；插件不包含端点 URL，也不直接发模型请求。设置页的 Provider 密码框只做一次性提交，经认证
Named Pipe 写入 Workspace 绑定的 Windows DPAPI SecretStore；插件配置只保存 opaque handle，不保存
API key 明文。

## 停止语义

插件热重载、禁用或 Obsidian 窗口关闭时，`onunload` 只断开当前客户端，不会把断线误当成
显式停机；其他 Vault 和已经开始的 Run 可继续由同一 Host/Worker 管理。需要真正停止时，请在
命令面板执行“一键停止本机所有 OfferAgent Runtime”。该命令会先明确确认，再通过当前 Windows
SID 专属的认证控制管道拒绝新连接、取消所有 Vault 的活动 Run，并清空 Worker、Shell 与
Subagent 进程树。它不会删除 Vault 笔记或本地 Runtime 数据。

## 开发

在 `src/interface/obsidian` 中执行：

```powershell
corepack yarn install --frozen-lockfile
corepack yarn test
corepack yarn build
```

构建会生成 `main.js`。正式测试产物应部署到目标 Vault 的插件目录；不要长期手改已编译文件，
也不要读取、复制或打印旧 `data.json` 中的凭据。插件加载旧设置时只迁移允许的本地字段，并以
封闭 schema 重写数据，旧 Server URL、API key 与同步字段不会进入新配置。

`npm run protocol:generate` 会从 Harness 的权威 JSON Schema 同时生成协议身份以及完整的
TypeScript DTO、Command/Event/ReverseRequest 映射。`HarnessClient` 和 `EventReducer` 直接使用
这份生成映射；不要在插件中另写同名 wire DTO。生成器会复验 Schema 字节哈希，测试会比较完整
method/event 目录，Schema 漂移必须先重新生成并通过 TypeScript 编译。
