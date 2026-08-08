# OfferAgent Harness

`offeragent-harness` 是 OfferAgent 的 Windows x64 本地、Provider-neutral Agent Core 与 Runtime。它不是
Khoj Server 的便携打包，也不启动另一个模型厂商 Agent Runtime。

## 所有权

- 每个 Obsidian 插件实例直接启动一个 `offeragent-worker.exe`；Worker 内只有一个
  `HarnessService` 和一个 canonical Agent Loop。
- 同一交互式 Windows 会话和用户、同一 canonical Vault root 的 Worker 由命名互斥锁排他；锁早于 SQLite 打开和
  恢复，且一直持有到事件循环完全退出。
- 插件唯一 IPC 是继承 stdin/stdout 上的 framed JSON-RPC。没有常驻 Host、discovery、Named Pipe
  或后台 Worker。
- 工具调度、权限、审批、Session/Event、SQLite、Vault、Memory、Shell、Hooks 和 Subagent 均归
  Worker 所有；插件不拥有第二套状态机。
- 模型只能通过 `ModelGateway` 参与规划和生成，不能访问文件、执行进程或管理业务状态。
- Shell/Hook 等进程工具经短生命周期 `offeragent-process-host.exe` 执行，并受固定 hash/catalog、
  Job Object、AppContainer 和可选离线 Authenticode 约束。个人本地版的内置文档解析器是显式例外：
  它固定以当前 Windows 用户权限运行，不提供隔离模式配置开关。
- 生产依赖不得包含 Khoj、Django、PostgreSQL、LangChain 或远程 Conversation/Workspace Store。

当前包只支持个人 Windows x64 本地构建，不包含正式签名发布、Setup、自动更新通道或 ARM64 路径。

## 开发门禁

```powershell
uv sync --extra dev --locked --python 3.12
uv run pytest -q
uv run ruff check src tests scripts
uv run ruff format --check src tests scripts
uv run mypy src tests
uv run lint-imports --config .importlinter --no-cache
uv run python -m offeragent_harness.protocol.schemas check
uv run python scripts/audit_repository_closure.py
uv run python scripts/check_documentation.py
uv run python scripts/check_architecture.py
uv run python scripts/check_forbidden_dependencies.py
uv build
```

结果以当前命令的退出状态为准；README 不保存测试数量或临时 schema hash。

## 执行和文件工具

所有 Agent 请求都从 direct stdio 进入应用命令边界，再统一经过
能力策略、审批、审计、UoW 和恢复。`runtime.duplex_json_rpc` 只负责 framing、取消和背压，不拥有
Agent、Tool、Vault 或 Storage 实现。

读取工作区前须遵守其 `CLAUDE.md`；需要使用 `AGENTS.md` 时由指令文件显式导入。Worker 使用 Glob
定位文件、Grep（固定的 `rg.exe`）搜索内容、Read 读取确认范围。知识入库会维护版本化的语义 PageIndex 和
带引用 LLM Wiki，但在线 Agent 仍通过 Skill 组织 `grep/read` 从 Wiki 导航到原始 evidence page；
当前知识检索不使用 Embedding、向量库或网络页面检索。

生产 closure audit 验证旧服务器路径和入口不存在、生产代码不导入测试 fake、依赖图符合
`.importlinter`，且冻结 bundle 不含已删除的常驻协调 Host、发行或更新模块。

## PDF 和图片附件

Obsidian 对话支持在同一条自然语言指令中附加 PDF、PNG、JPEG 或 WebP。附件可通过文件选择、拖放或
粘贴导入；插件先按文件魔数确认类型、计算 SHA-256，再把外部文件复制到 Vault 的
`OfferAgent/Attachments/<sha256>.<ext>`。已有的内容寻址文件必须重新校验为同一内容，插件不会覆盖
哈希不一致的文件。每个草稿/Turn 最多 5 个附件，每个文件最多 64 MiB，PDF 最多 256 页；一轮提供给
模型的文档证据上下文总计最多 768 KiB。超出限制、加密或不支持的 PDF、无可提取文字、源文件发生
变化以及解析输出不完整时均失败关闭，不会静默截断后继续回答。

附件不是直接发送给多模态模型。Worker 校验 Vault 路径、内容哈希和实际媒体类型后，将只读副本放入
进程暂存区，并通过固定 hash、固定参数的 `document-extract` profile 以当前 Windows 用户权限运行
PyMuPDF 与 RapidOCR。该个人本地策略没有配置开关，也不使用 AppContainer；操作系统不会额外阻断
解析进程的本机文件或网络权限，但解析实现不包含联网下载或远程 OCR 路径。固定进程身份、Job Object、
暂存目录、超时和完整输出上限仍然生效。PDF 优先使用内嵌文字，只有 `strip()` 后为空的页面才渲染并
OCR；图片直接 OCR。生产 RapidOCR/ONNX Runtime 配置固定要求 NVIDIA CUDA provider，并绑定 GPU 0；
CUDA、cuDNN 或 provider 不可用时解析器失败关闭，不会静默回退到 CPU 或 DirectML。PyMuPDF 的内嵌文字
提取和页面栅格化仍在 CPU 上执行，只有 RapidOCR 的检测、方向分类和识别模型在 GPU 上推理。DeepSeek
Provider 仍是纯文本 Chat Completions 路径，只接收提取后
的、标记为不可信来源数据的文字证据。

内容寻址的 Vault 文件及其 SHA-256 引用是权威 source；提取文字和页级 provenance 是 Run 所有的
derived Artifacts，可从 source 重新生成。用户可直接用自然语言要求总结、提取问题、查找某项信息，
或整理后写入 Vault。Runtime 不按文件名、内容关键字或“入库”等词选择解析/写入路径：显式
`DocumentContentBlock` 决定是否解析，后续写入仍只能经现有 Vault transaction、权限策略、Diff、
审批和原子提交闭环完成。

## 个人构建

完整插件只能由：

```powershell
uv run python scripts/build_local_windows_plugin.py `
  --output ..\..\..\artifacts\offeragent-obsidian-plugin `
  --ripgrep-executable C:\path\to\rg.exe
```

更新目标 Vault 只能由：

```powershell
uv run python scripts/update_local_windows_plugin.py `
  --vault-root 'C:\path\to\your-vault' `
  --ripgrep-executable C:\path\to\rg.exe
```

项目概览、安全边界和完整开发命令见仓库根目录 `README.md`。
