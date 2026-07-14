# OfferAgent 个人本机版本完成证据

> 适用范围：`docs/architecture/personal-local-scope.md` 定义的项目所有者本人、当前 Windows x64
> 电脑上的 Obsidian 本地开发安装。本文不把未签名开发产物描述成公开发行包。

## 当前结论

截至 2026-07-14，DeepSeek V4 Chat Completions 已作为个人本机默认 `ModelGateway` 完成实现、回归、
冻结、原子安装和真实 Worker 配置迁移。真实 Pipe 健康探针已确认当前配置为
`deepseek-v4-flash / medium`，并在没有 DeepSeek SecretHandle 时正确返回 `auth_required`。剩余唯一
模型验收是：由用户本人把一枚轮换后的真实 DeepSeek 凭据通过插件写入 Windows SecretStore，再完成
真实健康检查和一次只读模型对话。凭据验收结束前不以 Fake Model 代替，也不读取或迁移旧插件
`data.json` 中的任何秘密。

## 七项范围证据

| 范围 | 结果 | 证据 |
| --- | --- | --- |
| 1. 真实插件可用 | 通过 | 当前源码构建的 `2.0.0-beta.28` 已原子安装到真实插件目录；Obsidian 真实面板可打开，实际“运行诊断”显示 Runtime/index ready；插件 manifest 为 `isDesktopOnly: true`。 |
| 2. 本机 Runtime 可用 | 通过 | Obsidian 冷启动后只有一个 Host 和一个 Worker，Worker 的父进程是 Host；本地 Web 明确显示与插件相同 Worker PID、Runtime 指纹和 SQLite 身份；网络只监听 `127.0.0.1` 随机端口。 |
| 3. 至少一个真实模型路径 | **Adapter/部署通过；待用户写入轮换后的凭据并做真实对话** | 默认与当前真实安装均为 `deepseek-v4-flash / medium / Chat Completions`。真实 frozen Worker 的 Pipe 健康探针返回 `auth_required / credential_unavailable`，证明配置迁移生效且无凭据时 fail closed。设置页只经认证 Named Pipe `secrets/put` 写入 Workspace 绑定的 DPAPI SecretStore；插件不含 DeepSeek URL、不直连模型、不保存明文 key。 |
| 4. 真实工作区读取 | 待按现行工具链复验 | 验收只允许 Glob/Grep/Read 的按需文件读取；旧索引命中数据不再作为证据。 |
| 5. 安全写闭环 | 通过 | 真实 `ProductionWorkerApplication` + Win32 Named Pipe + 临时 Vault E2E 覆盖单文件 create/append/replace/patch、Diff Artifact、durable approval、`expectedHash`、CAS、SQLite Journal、commit-observe 和最终 compose；rename/trash/multi-op 在个人模型入口 fail closed。恢复矩阵覆盖取消、ACK 丢失、幂等和硬崩溃阶段。 |
| 6. 本机可靠性 | 通过 | 插件真实“一键停止”确认后约 6 秒内 Host/Worker/process-host 全部退出且 Obsidian 保持运行；完整退出 Obsidian 后无残留；冷启动产生新的单例进程树并恢复原 SQLite Session。冻结自检连续四次完全一致且自检沙箱残留为 0。 |
| 7. 可重复构建与回归 | 通过 | Python 全量、插件、TypeScript、协议、架构、依赖、Semgrep、Web assets、冻结归档审计均通过；个人安装/更新命令见 `docs/personal-local-plugin-install.md`。 |

## 自动化门禁

- Python 全量：`1652 passed, 8 skipped`，用时 174.27 秒。8 个 skip 均为当前 Windows 账户
  无符号链接权限或 Windows 文件共享语义限制；没有失败或 xfail。
- DeepSeek Provider 定向：13 项通过；覆盖固定端点、SecretHandle、thinking/JSON/SSE、原始思维链
  丢弃、usage、终止符、HTTP 错误和 fail-closed；模型健康 SecretStore 补充测试通过。
- MyPy `452 source files`、0 issues；Ruff 检查通过，格式检查 479 个文件通过。
- 插件：171 passed、1 个 Windows symlink 条件 skip；TypeScript `--noEmit`、生产构建和本机开发构建通过。
- 协议：生成物契约 2/2；Schema hash
  `sha256:6dfc46d51d69b962f8effa16a13bc8bddbff43a60dfe701b32ce3cb5f583d08e`。
- 架构/依赖：repository closure、forbidden dependencies、architecture check 全通过；Import Linter
  336 个文件、2,916 条依赖、11/11 contracts kept。
- Semgrep：305 个目标、4 条规则、0 findings。
- Web assets check、`uv build`、`git diff --check` 均通过；候选包和真实安装目录的 frozen 自检均
  为 10/10 健康。

## 最终个人候选包

- 路径：`E:\Projects\offeragent\artifacts\offeragent-obsidian-plugin-deepseek-r1`
- Git HEAD：`90ba97d5a63f8b15d352449fd1d799c0eb5b9330`
- 源码树：`sha256:fcfc6a47bc17998d81625467f5dee589c0d3d9eb0b359f3a15dd340726549131`
- Runtime manifest：`sha256:99a2bd7e0159ddcf00881c7334ef0b70f25fe505cea690739ee41fdb67142dea`
- Runtime content：`sha256:c447dde9597df0fa5ec2e235421cd17c7e199032462fd2f22644391fb6f89ac5`
- 精确树：209 个常规文件、87,172,304 字节；Runtime manifest 的 204 个成员逐文件复验通过。
- 四个入口 EXE 均为 PE `0x8664` x64；PyInstaller 内嵌模块审计没有 testing/fake/pytest、
  Khoj、Django、PostgreSQL 或迁移 CLI。
- DeepSeek r1 候选包隔离自检和真实安装目录自检均为 10/10 健康；自检结束后没有进程残留。
- `main.js` 中 `deepseek-v4-flash` 出现 2 次；`gpt-5.6-luna` 只保留 1 次作为精确旧默认迁移标记；
  `api.deepseek.com` 出现 0 次，端点只存在于 Worker Provider Adapter。

安装器对候选包和安装后目录分别复验。除 `data.json` 外的 209 个文件逐字节一致；本次安装对
`data.json` 只比较长度、创建时间、修改时间和 Windows 文件属性，安装前后完全一致，内容从未
打开、解码、打印或复制。

## 当前 DeepSeek 推理边界

个人本机默认使用 DeepSeek V4 的固定官方 Chat Completions 推理路径：

1. 插件只保存 `deepseek / chat-completions / deepseek-v4-flash / medium` 等非敏感选择；固定端点
   `https://api.deepseek.com/chat/completions` 只存在于 Python Worker Adapter。
2. API key 经认证 Named Pipe 写入当前 Workspace 的 Windows DPAPI SecretStore。模型请求时通过
   `SecretResolver.consume()` 临时消费，绑定 `kind=model-provider`、`providerId=deepseek` 和
   Workspace scope；明文不进入 Vault、SQLite、配置、命令行或日志。
3. DeepSeek 不接收 Harness 工具定义。任何返回的 `tool_calls` 都是未请求的远程工具调用并 fail
   closed；Agent Loop、Memory、Shell、审批和 Subagent 仍只在本地 Worker。
4. V4 没有独立的 medium 档；Adapter 把 `minimal/low/medium/high` 规范为 Provider 的 `high`，把
   `max` 规范为 `max`。health probe 的 effort 为 `None`，因此关闭 thinking，避免 4-token probe
   被推理预算吞掉。
5. DeepSeek JSON Output 只保证 JSON object。Adapter 把 canonical Schema 放入受信 system 指令，
   收流后仍在本机用原始 Draft 2020-12 Schema 严格校验；原始 `reasoning_content` 在 Provider
   边界丢弃，只有 reasoning token 数进入 usage。

已完成的 Codex/OpenAI/Responses Provider 继续保留为可选路径，不删除。本机 Codex Subscription
实验适配也保留，但不再是个人默认值，更不会把 Codex CLI/App Server 作为第二套 Agent Runtime。

## 真实运行证据

1. Obsidian 加载安装包后，插件实际诊断显示 `ready`、协议 1.0、上述 Schema hash、索引 ready。
2. 本地 Web 只能用插件 Pipe 签发的一次性 fragment 启动；直接访问根地址会 fail closed。
3. 一次性令牌交换后 fragment 被清除，页面显示同一 Worker PID；Obsidian 在线时，Web 明确只把
   Vault 写入交给唯一 Named Pipe Client Tool。
4. Web 创建的空 Session 已持久化到 SQLite；关闭页面、从新的插件启动链接重开以及完整
   Obsidian/Host/Worker 冷启动后，均恢复同一“0 轮”Session。
5. 真实 Vault 只读验证前后没有内容或 mtime 变化；本轮没有写入任何已有笔记。

## 最后一项验收

用户本人需要在当前 Obsidian 的 OfferAgent 设置中：

1. 保持 Provider 为 DeepSeek、模型为 `deepseek-v4-flash`、推理强度为 `medium`；
2. 在密码框输入一枚轮换后的 DeepSeek API key 并点击 `安全保存`；
3. 点击 `应用并检查`，确认健康；
4. 完成一次只读工作区问题，验证流式回答与文件引用/wikilink/backlink；
5. 重开 Session 验证消息恢复，再执行一次显式停止并确认进程树归零。

完成以上步骤后，把本文第 3 项和“当前结论”更新为通过，才可宣告个人本机版本完成。

## 外部系统与秘密

- 本轮 DeepSeek 变更未连接或修改旧服务器；没有执行任何服务器命令，也没有服务器状态变化。
- 未读取、复制或打印真实插件 `data.json` 的 `khojApiKey` 或其他内容。
- 用户曾在对话中粘贴 Provider key；实现、测试、命令、日志、配置、SQLite 和提交均未复述或使用
  该值。因为该值已进入对话记录，真实验收要求先在 DeepSeek 控制台轮换，再通过插件安全保存。
- 未修改或删除真实 Vault 的既有 `notes/` 内容。
