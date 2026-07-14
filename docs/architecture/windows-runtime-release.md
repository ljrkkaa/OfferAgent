# Windows Runtime 离线发行与验证

正式发行由 `packages/offeragent-harness/scripts/build_windows_release.py` 统一生成。插件完整包、离线 ZIP 和 Setup payload 共用同一份 canonical manifest、Ed25519 detached signature 与逐文件 SHA-256，不允许维护第二套 Runtime。

## 信任链

1. 仓库只保存 `release-2026` 公钥，TypeScript 与 Python keyring 必须逐字节一致。
2. 构建时从外部 PEM 读取 Ed25519 私钥，并验证其公钥与仓库 keyring 一致；私钥不会写入源码、产物、日志或命令行。
3. 所有 `.exe` 先由 SignTool 完成 Authenticode，再计算 manifest hash。
4. 插件先按 Node `os.arch()` 选择 `windows-x64` 或 `windows-arm64`，再验证 canonical manifest、Ed25519、平台、兼容范围、bootstrap 闭包 hash，以及清单架构对应的 PE machine（x64=`0x8664`、arm64=`0xAA64`），最后才启动签名 bootstrap。
5. bootstrap 使用同一验证器复验 manifest、每个文件和 Authenticode，安全解压到同盘 staging，自检、SQLite 备份/迁移后原子切换 `current.json`。

自检不是单纯加载 PE：它会为随机 nonce 建立当前 SID 专属的隔离状态根和合成 Vault，使用与插件相同的
CRT `fd3` discovery / `fd4` Vault 输入契约调用当前候选中的签名 Host。Host 必须经过正式 attach、
Workspace registry、Worker 签名复验、原子 Job 归属和 DPAPI discovery 路径，再由真实当前 SID Named Pipe
完成 `initialize -> runtime/status -> shutdown`，最后通过认证的 `stop-all` 回执证明 Host、Worker、Job
进程树和监听器均已清空。直启 Worker 只保留为单体诊断，不计入激活通过条件。自检还验证签名 Process
Host、SQLite WAL、只读 Vault、随机 Loopback bind、Named Pipe challenge 和 Job Object 退出；整个探测不读取
用户 Vault，也不发起模型或网络请求。

bootstrap 是 PyInstaller onedir 闭包的一部分。它依赖的 DLL、PYD 和资源全部列入 `bootstrap.dependencies`；插件拒绝未签名的额外文件，避免只校验 EXE 却从旁加载被替换 DLL。

Runtime 的 SBOM 不是从 `Requires-Dist` 推测出来的包清单。构建器必须读取 Host、Worker、Self-test、
Process Host 四个 onedir 目标各自的 `Analysis/COLLECT/EXE/PKG/PYZ` TOC，并用 `COLLECT` 与合并后的
实际文件树做一一闭包校验；缺 TOC、重复目标、额外文件、未知 DLL/PYD/data 或来源冲突都会终止发行。
`Analysis/PYZ/PKG/EXE` 中的 bootloader、CPython 模块、runtime hook 和嵌入模块也必须记录为来源材料。

构建产物内的 `provenance/frozen-payload.v1.json` 使用 canonical JSON，记录每个运行文件在
PyInstaller/static copy 捕获点的长度与 SHA-256、最终长度与 SHA-256、唯一归属组件、来源 digest、
四个目标和五类 TOC digest；机器绝对路径不会进入产物。DLL、PYD 和 data 必须与捕获字节完全一致；
EXE 只允许由构建器记录的 `authenticode-sign-v1` 转换改变，并绑定签名前/签名后 SHA-256、目标 PE
machine 和成功的 Authenticode 验证。独立 audit 按签名 manifest 的 executable 集合要求转换记录精确
覆盖，拒绝漏报、额外转换、签名前 hash 不匹配、签名后 hash 不匹配和架构不匹配。
`sbom/runtime.spdx.json` 是 SPDX 2.3 文件级文档，包含每个文件的 SHA-1/SHA-256、每个包的
PackageVerificationCode，以及精确的 `DESCRIBES`、`CONTAINS`、`DEPENDS_ON` 关系。无法从发行证据
确定的许可证一律写 `NOASSERTION`，不得根据包名猜测。由于内容哈希不能自引用，provenance 明确排除
自身、SPDX 与 manifest/signature；SPDX 只排除自身，而签名 manifest 逐文件覆盖 provenance 与 SPDX。
独立 audit 只根据签名 manifest、解压文件和两份 attestation 重新核对，不能用 audit 环境的
`Requires-Dist` 元数据替代冻结载荷证据。

## 构建门禁

构建支持原生 Windows x64 和原生 Windows arm64；`--architecture` 必须与 `IsWow64Process2` 返回的本机原生 machine 一致，不允许在 x64 runner 上把产物标成 arm64，也不允许反向伪装。两种架构均要求：

- PyInstaller onedir，禁止 onefile 和 UPX；
- 外部 Ed25519 私钥；
- SignTool、代码签名证书和 RFC 3161 timestamp URL；
- 完整生产 Worker composition 明确声明 release-ready；
- 完整 `COMMAND_REGISTRY` handler factory；
- Web assets、协议 schema 和架构检查保持最新；
- 发行树不含 Django、PostgreSQL、Khoj Server、Node Runtime 或外部 Python 依赖。

示例（证书 thumbprint 和私钥路径只来自受保护的 CI secret）：

```powershell
uv run python scripts/build_windows_release.py `
  --architecture x64 `
  --runtime-version 2.0.0 `
  --plugin-minimum-version 2.0.0 `
  --plugin-maximum-version 2.9.9 `
  --build-commit <40位commit> `
  --key-id release-2026 `
  --ed25519-private-key <受保护PEM路径> `
  --sign-tool <Windows SDK SignTool.exe> `
  --certificate-sha1 <证书thumbprint> `
  --timestamp-url <RFC3161 URL> `
  --iscc <Inno Setup ISCC.exe> `
  --source-date-epoch <UTC epoch> `
  --output <空输出目录>
```

命令在输出根下生成架构专属的 `windows-x64/` 或 `windows-arm64/`，其中 Runtime ZIP、离线插件 ZIP 和 Setup 文件名都携带架构。Inno x64 模板使用 `x64os`，明确排除可模拟 x64 的 ARM64 Windows；ARM64 模板使用 `arm64`。生成后必须在同架构原生机器上运行：

```powershell
uv run python scripts/audit_windows_release.py `
  --architecture x64 `
  --payload <输出根>\windows-x64\plugin-runtime\windows-x64 `
  --plugin-version 2.0.0
```

随后进入同架构干净 Windows VM 的断网安装、升级、回滚、保留数据卸载和彻底清除 E2E。构建脚本没有 unsigned production 开关；缺证书、私钥或完整 Worker composition 时必须失败，不能产出降级包。

`.github/workflows/windows-local-runtime.yml` 分别提供手动的 `native-x64-signed-release` 与
`native-arm64-signed-release` 门禁，只会调度带对应 `X64`/`ARM64`、`Windows`、
`offeragent-release` 标签的自托管原生 runner，并在读取签名材料前再次用系统 API 证明本机架构。
没有真实 runner 的跳过或排队状态不代表该架构已完成发行验证；只有签名构建、独立 audit 和后续
同架构干净 VM E2E 都成功后，才能把该架构标记为已验证。

## IPC 与数据边界

- Host attach 的 Vault root 仅通过继承 fd4 传入，fd3 只返回一次 canonical discovery JSON；stdout、argv、环境和日志不包含 Vault root。
- 插件与并发 attach 进程复用当前 SID Host；转发使用当前 SID DACL、DPAPI material 和 challenge-response Named Pipe。
- `current.json`、失败断路器、引用计数和迁移快照位于 `%LOCALAPPDATA%\OfferAgent\runtime`。
- SQLite migration 前使用 SQLite Backup API；失败恢复数据库与旧指针。手动回滚同样切换匹配的数据库 generation。
- 默认卸载保留 workspace state 和配置；彻底清除要求精确二次确认，且永不删除 Vault 笔记。

## 多 Vault 安装与卸载账本

固定 Inno `AppId` 只拥有全局安装器，不代表任一 Vault 插件副本的所有权。签名 bootstrap 在
`%LOCALAPPDATA%\OfferAgent\installer\vault-installations.json` 维护当前 SID DACL 保护、canonical JSON、
SHA-256 完整性校验和原子替换的安装账本。每条记录绑定随机 `installationId`、canonical root identity、
插件目录文件系统 identity、版本历史及可选 Runtime owner。Setup 刚复制但插件尚未启动时记录为 pending；
首次真实 bootstrap 通过 fd3 收到 Vault root，并以 Vault 内 portable workspace identity 原子绑定 owner。
Vault root 不进入 argv、环境、日志或诊断。旧版 HKCU `VaultRoot` 只通过受保护固定 request 文件迁移；
无法证明 portable identity 时失败关闭，不猜测 owner。

卸载器必须显式选择“最近一次由 Setup/插件 bootstrap 明确绑定的 installationId”或“所有 Vault”。
Apps & Features 不被称为某个“当前 Vault”；单 Vault 模式还要求用户逐字输入受保护指针中的 ID，无法
确认时应取消或选择全部。账本绝不把移除后的指针自动改成字典序剩余项。单 Vault 模式只允许保留数据，签名
coordinator 先把该记录的固定 `.obsidian\plugins\offeragent-obsidian-plugin` 原子隔离为账本绑定的
tombstone，再用 `OPEN_REPARSE_POINT`、拒绝并发 write/delete sharing 的 Windows handle 验证全树 identity、
final path、root containment、类型、hardlink 和 reparse 属性，并通过同一 DELETE handle 自底向上删除。
它绝不扫描其他 Vault，也不沿 junction/symlink 删除；root 已移动且未重新绑定时只释放已证明的 Runtime
owner，不搜索新路径。单 Vault成功后 `InitializeUninstall=False`，全局 Inno payload 保持不变。

所有 Vault 模式通过幂等 operation ID、完整性 journal 和完成 receipt 串行恢复。默认逐 owner 释放引用并
保留 Session/索引/设置；彻底清除只允许在其他引用均释放后，由最后一个已绑定 owner 执行精确确认的全局
purge。coordinator ACK 后 Inno 才继续删除全局 `{app}`，并且 `[UninstallDelete]` 只删除三个固定已知账本
文件，再对 `requests`/`installer` 使用 `dirifempty`；未知 sibling 永不递归删除。ACK 丢失或进程中断时，
HKCU 只持久化非敏感 operation/scope/installation ID 以恢复同一操作，不持久化 Vault root。
如果所有记录均为 pending，就没有可证明的 Runtime owner：Inno 会在持久化 operation ID 之前让 bootstrap
只读预检并拒绝全局 purge，提示用户改选 preserve-data；不会构造假的 owner 或留下无法恢复的 purge 操作。
