# OfferAgent 个人本机插件交付范围

## 目标

当前只交付给项目所有者本人，在一台 Windows x64 电脑上通过 Obsidian 插件使用。它是本地开发
安装，不是面向公众的安装包，也不以“已满足公开发行级 Definition of Done”对外宣称。

完整架构继续保留，已经完成的 Host/Worker、Agent Core、Web、Memory、Skills、Hooks、Shell、
Subagent、升级/卸载等代码不删除。后续只修复会影响个人日常使用、数据正确性或安全边界的
问题，不再为尚无实际需求的发行矩阵持续扩张实现。

## 当前必须完成

1. **真实插件可用**：源码构建后的插件部署到 `E:\面试胜利！\.obsidian\plugins\offeragent-obsidian-plugin`，
   保留现有 `data.json`，可在 Obsidian 中加载、打开聊天、查看状态和停止 Runtime。
2. **本机 Runtime 可用**：当前 Windows x64 上只有一个 Host；该 Vault 只有一个 Worker、一个
   `HarnessService`、一份 SQLite 和一个活动 Run 权威。插件只通过当前 SID 专属 Named Pipe 连接。
3. **至少一个模型路径可用**：用户配置的一个 `ModelGateway` Provider 能完成真实对话；模型只推理，
   不取得文件、Shell、Session、Memory、审批或 Subagent 所有权。凭据由本机 Secret Store
   管理，不读取或迁移旧 `khojApiKey`。
4. **真实工作区读取可用**：以 `E:\面试胜利！` 做只读文件搜索、读取、引用/wikilink/backlink 验收；项目源码、
   构建物和测试报告不得进入工具可见范围。
5. **安全写闭环可用**：个人运行路径只向模型开放单文件 create/append/replace/patch，并在临时
   Vault 中覆盖 Diff、审批、`expectedHash`、原子提交、冲突、取消、ACK 丢失、硬崩溃恢复和幂等
   恢复。多文件批次、rename 和 trash 的已有实现与测试保留，但在具备 durable batch journal 前
   fail closed，不进入个人运行路径。真实知识库默认不做自动写入验收；需要真实写 smoke 时，由
   用户另行明确授权并只写专属测试目录。
6. **本机可靠性可用**：插件重载、断连、Worker 重启和停止不会重复写、伪报成功或遗留本轮启动的
   子进程树；影响数据正确性的 P0/P1 必须清零。
7. **可重复构建与回归**：Python 全量测试、插件测试/构建、协议生成、架构/依赖检查通过，并提供一条
   可重复的个人本机安装/更新命令。

## 保留但不再扩张

以下能力已经实现，因此继续保留并维持现有回归；除非发现会破坏上述主路径的问题，不再以功能
完善或 UI 抛光作为当前阻塞项：

- 本地 Loopback Web UI 与插件的高级界面对齐。
- Memory、Skills、Hooks、Shell 和 Subagent 的更多配置体验。
- 多 Vault 并行、复杂 Run tree、旧服务器会话/Memory 导入器的更多边角场景。
- 更新、回滚、卸载 ledger 和诊断的公开产品级体验。

## 当前不做，公开分发前再做

- Authenticode 证书、SmartScreen/Defender 信誉和公开发布签名仪式。
- Windows arm64 原生构建与 x64/arm64 双架构矩阵。
- Inno Setup 安装器、社区商店/自动更新通道和面向第三方的离线完整包。
- 无 Python/Node 的干净 Windows 10/11 VM 安装、升级、回滚、断电恢复和完整卸载矩阵。
- 长时间压力、性能 P95、rename storm、大规模多 Vault/多 Windows 用户测试。
- 网络抓包、第三方许可/SBOM 发布审计、灰度发布和旧服务器迁移演练。
- 本地 Web UI 与 Obsidian 每一项交互的像素级/全场景一致性。
- 多文件原子批次、rename/trash 的 durable batch journal 与崩溃恢复。

这些项目不是从仓库删除，而是移出个人版本的完成判定。将来准备分享给其他用户时，恢复
`task.md` 第 7 阶段、15.4—15.6 和第 16 节的完整发行门槛。

## 个人版本完成判定

同时满足以下条件即可结束当前项目，而不等待公开发行工作：

- 当前机器上的 Obsidian 能从真实插件目录启动并连接本地 x64 Host/Worker。
- 真实 Vault 只读文件搜索与引用验证通过；临时 Vault 单文件写事务和硬崩溃故障注入通过。
- 一个真实模型 Provider 可聊天；Session 重开可恢复；显式停止后本轮进程树退出。
- Python、插件、协议和架构回归全绿，且没有影响个人主路径或数据安全的已知 P0/P1。
- 文档明确标识为“个人本机开发安装”，未签名产物不冒充正式发行包。

具体的隔离设计、构建命令、原子安装/回滚和 `data.json` 保护规则见
[`docs/personal-local-plugin-install.md`](../personal-local-plugin-install.md)。
