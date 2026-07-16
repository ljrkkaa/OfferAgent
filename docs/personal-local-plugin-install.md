# OfferAgent 个人本机开发安装

这条路径只用于项目所有者当前的 Windows x64 电脑。它生成未签名的 PyInstaller onedir Runtime，
不是公开发行包，也不会绕过正式发行验证器：

- 正式 `build_windows_release.py` 仍强制 Ed25519、Authenticode、SignTool 和 Inno Setup。
- 正式插件在编译期只包含 `EmbeddedRuntimeInstaller`；本机构建在编译期改用独立的
  `LocalDevelopmentRuntimeInstaller`，正式 bundle 中不存在开发 installer 标记。
- 本机 manifest 必须是 canonical JSON，并明确包含 `developmentOnly: true`。它固定完整文件集、
  文件长度、SHA-256、x64 PE 架构、协议版本、Schema hash、Git commit、源码树 hash 和构建身份。
- 构建脚本把该 manifest 的期望 SHA-256 编译进本机插件 bundle；插件启动前、执行自检前后以及每次
  实际启动 Host 前都会按该锚点校验完整 Runtime。Host、Worker 和 process-host 各自在冻结进程中
  再校验自身、Worker、Process catalog、内置进程和 Skill 资产。未签名不等于不校验。
- PyInstaller 只接收显式 Runtime 模块白名单和最小可信 DLL 搜索路径；构建会同时审计 provenance
  TOC 与最终 EXE 内的 PYZ 模块名，拒绝 testing/fakes、pytest、迁移 CLI 和旧服务器依赖。
- Host/Worker 仍使用唯一 production composition、唯一 Agent Loop 和同一 Tool Kernel；开发入口只
  注入固定哈希 trust，不维护第二套 Agent Runtime。

这里的哈希锚点用于发现构建损坏、局部替换和混合版本，不提供正式代码签名的发布者身份保证。如果同一
Windows SID 下的恶意进程能同时替换 `main.js`、嵌入的哈希和整套 Runtime，它仍可替换整个本机插件。
个人本机路径不把这种场景描述为不可替换；需要抵御它时必须使用正式 Authenticode/Ed25519 发行链。

## 一次性准备

在 `E:\Projects\offeragent\repo\packages\offeragent-harness` 中执行：

```powershell
uv sync --extra dev --frozen
```

Node 依赖已经由仓库的插件工作区管理；若 `node_modules` 尚不存在，先在
`E:\Projects\offeragent\repo\src\interface\obsidian` 执行 `npm install`。

## 只构建，不安装

输出目录必须不存在，构建脚本不会覆盖已有产物：

```powershell
uv run python scripts/build_local_windows_plugin.py `
  --output E:\Projects\offeragent\artifacts\offeragent-obsidian-plugin
```

产物入口为：

```text
offeragent-obsidian-plugin\
  main.js
  manifest.json
  styles.css
  local-development-build.json
  runtime\windows-x64\local-development\
    development-runtime-manifest.json
    offeragent-host.exe
    offeragent-worker.exe
    offeragent-process-host.exe
    offeragent-self-test.exe
    ...完整 onedir 依赖和内置资产
```

## 一条命令安装或更新

先在 OfferAgent 插件中执行“停止 Runtime”，再完全退出 Obsidian。更新脚本会拒绝在
`Obsidian.exe`、`offeragent-host.exe` 或 `offeragent-worker.exe` 仍运行时继续：

```powershell
uv run python scripts/update_local_windows_plugin.py --vault-root 'E:\面试胜利！'
```

脚本在临时目录从源码重新构建并复验，然后原子切换
`<Vault>\.obsidian\plugins\offeragent-obsidian-plugin`。现有 `data.json` 只作为不透明文件移动到新目录；
安装器不会打开、解码、打印或复制其中的旧 `khojApiKey`。任一步失败都会把旧插件和原 `data.json`
回滚到原位置。如果 Windows 在回滚移动本身发生故障，安装器会保留承载唯一 `data.json` 的
backup/staging/failed 恢复目录、在错误中给出该本地路径，并拒绝递归清理该目录；它不会为了“清理干净”
而删除最后一份设置文件。

如果已有一个经过验证的构建产物，也可以只执行安装：

```powershell
uv run python scripts/install_local_windows_plugin.py `
  --artifact E:\Projects\offeragent\artifacts\offeragent-obsidian-plugin `
  --vault-root 'E:\面试胜利！'
```

## 更新后的检查

1. 启动 Obsidian 并启用 OfferAgent。
2. 状态应经过“定位 → 校验 → Runtime 自检 → 启动 Host → 连接 Worker → ready”。
   Runtime 第一次从新目录启动时会触发 Windows 对未签名开发文件的冷扫描，自检最多可能等待约 5 分钟；
   后续启动通常明显更快。校验内容不会因此减少或跳过。
3. 打开聊天，确认同一 Vault 的 Pipe 连接、索引状态和模型配置可见。
4. 默认模型为 `deepseek-v4-flash`。首次使用时在设置页输入一枚有效的 DeepSeek API key，点击
   `安全保存`；明文只经认证 Named Pipe 写入 Windows DPAPI SecretStore。随后点击 `应用并检查`，
   健康状态必须为 healthy。不要把 key 写入 `data.json`、Vault、环境变量或命令行。
5. 新 Workspace 默认未信任，界面会明确显示实际有效权限为只读。需要日常写入时，先在设置中
   显式确认“信任当前 Workspace”，再使用“标准”模式；写操作仍进入 Diff 与逐次审批，信任不等于 Bypass。
6. 首次验收先做真实 Vault 只读检索；写事务继续只在临时 Vault 验收，除非另有明确授权。
7. 更新前后若源码或协议有任何变化，必须从一个不存在的干净输出目录重新构建；旧 smoke artifact
   不能冒充最终候选。

## 与公开发行的边界

本机产物没有 Authenticode/SmartScreen 信誉，不能上传社区商店、不能给第三方使用，也不能改名伪装成
正式离线包。准备公开分发时，回到 `docs/architecture/windows-runtime-release.md` 的签名构建、独立审计、
安装器和干净 VM 门槛。
