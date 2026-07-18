# Obsidian 条件 Vault 变更能力

## 结论

OfferAgent 当前锁定的 `obsidian` 类型包是 1.12.3；`yarn.lock` 与 Obsidian 官方 API 仓库的
[`d5b94f5`（Update to v1.12.3）](https://github.com/obsidianmd/obsidian-api/commit/d5b94f56e3a909396ae05941e67ddb51a167180d)
一致。以下结论只依赖这份公开契约和 Obsidian 官方开发文档，不假定闭源桌面应用内部存在未公开的锁或事务。

- `Vault.process()` 公开保证的是**一篇文本文件**在读取当前内容到写回新内容之间不发生变化。它提供了在同步回调中比较
  expected content hash 的正确原语。OfferAgent 在不匹配时返回刚观察到的原内容并报告 conflict，不依赖未公开的回调异常
  中止语义；实现可能发生一次同内容写回或元数据变化，因此不能额外宣称“零写入”。
- `Vault.create()`、`Vault.modify()`、`Vault.delete()` 和 `Vault.rename()` 都没有 expected hash、expected version、
  compare token 或 transaction 参数。
- 公开 API 没有 compare-and-delete、compare-and-rename 或多文件事务。先读/比较再调用 `delete()` 或 `rename()`
  存在 TOCTOU；OfferAgent 不能把这种序列描述成条件删除或原子多文件提交。
- `mtime:size` 可以用作发现漂移的快照版本，但公开契约没有保证它能与 `process()` 的内容读写在同一个原子比较中提交；
  因而它不是独立的强版本 CAS，也不能排除 ABA。
- Git Checkpoint、durable journal、串行执行和补偿回滚可以构成**可恢复的顺序批次**，但不能让多个 Vault 文件对其他插件、
  Obsidian Sync 或外部编辑器具有瞬时的全有或全无可见性。

## 一手来源

仓库的 [`yarn.lock`](../../src/interface/obsidian/yarn.lock#L165-L168) 固定 `obsidian` 1.12.3。对应的官方声明中：

- [`DataWriteOptions`](https://github.com/obsidianmd/obsidian-api/blob/d5b94f56e3a909396ae05941e67ddb51a167180d/obsidian.d.ts#L2057-L2074)
  只有 `ctime` 与 `mtime`，没有前置条件或事务字段。
- [`Vault.create/delete/rename/modify/process`](https://github.com/obsidianmd/obsidian-api/blob/d5b94f56e3a909396ae05941e67ddb51a167180d/obsidian.d.ts#L6413-L6545)
  都各自返回一个 Promise；只有 `process()` 的说明使用 “Atomically read, modify, and save the contents of a note”。
- 较底层的
  [`DataAdapter.process/remove/rename`](https://github.com/obsidianmd/obsidian-api/blob/d5b94f56e3a909396ae05941e67ddb51a167180d/obsidian.d.ts#L1969-L2054)
  也仍是逐路径方法，没有批次或条件删除接口，所以绕到 Adapter 不会获得公开的多文件事务保证。

Obsidian 官方 [Vault 开发文档](https://docs.obsidian.md/Plugins/Vault) 进一步明确：

- 基于当前内容修改时应使用 `Vault.process()`；它保证文件不会在读取当前内容和写回更新之间发生变化。
- `process()` 的变换回调必须同步。异步计算应先读取，最后进入 `process()` 并在回调里再次比较当前数据；若不同，应该询问用户或重试。
- `delete()` 是不留痕删除，`trash()` 才保留用户反悔的可能；文档没有为二者声明内容或版本前置条件。

官方 API 仓库自身说明它发布的是
[Obsidian API 类型定义](https://github.com/obsidianmd/obsidian-api/tree/d5b94f56e3a909396ae05941e67ddb51a167180d)。
因此“声明未提供的事务也许在内部实现了”不能作为产品保证。

## 各操作能承诺什么

| 操作 | 公开保证 | OfferAgent 可安全承诺 | 不能承诺 |
| --- | --- | --- | --- |
| `process(file, fn)` | 单文件内容读—改—写原子区间；同步回调 | 回调内按 current content hash 做条件修改；不匹配则无写入 | 独立 `mtime:size` CAS；跨文件原子性 |
| `create(path, data)` | 创建一篇新文件；无 expected 参数；1.12.3 的 plaintext `create()` 未明文规定同名冲突行为 | 成功返回后核验结果；任何冲突或异常都失败关闭 | 把同名冲突必定抛错当作公开保证；在异常/ACK 丢失后仅凭“同内容文件存在”证明本次调用拥有该文件 |
| `modify(file, data)` | 覆盖一篇现有文件 | 只用于无需前置条件的普通操作；条件修改应改用 `process()` | CAS、无丢失更新 |
| `delete(file, force?)` | 删除给定文件或目录 | 无条件删除（仅在产品明确允许时） | compare-and-delete；先校验后删除仍无竞态 |
| `rename(file, newPath)` | 移动或改名一个文件/目录 | 无条件单路径 rename | source/destination CAS；多文件事务；链接更新与 rename 的整体原子性 |

这里的“不能承诺”是指**不能依据 Obsidian 公开 Vault API 承诺**，不是声称所有文件系统或未来 Obsidian 版本在理论上都无法提供该能力。

## 对 Vault Change Batch 的约束

### 正向应用

1. 修改现有文本文件时，在 `Vault.process()` 回调内比较 expected identity，再返回 after content。回调不匹配时返回
   current content 并报告 conflict，从而即使实现执行同内容写回也不会用旧内容覆盖用户编辑；不得依赖未公开的异常中止语义，
   也不得先 `cachedRead()` 校验后再 `modify()`。
2. Interview Submission 不包含 delete 或 rename。这既符合其新增/更新知识的领域语义，也避开公开 API 无法提供的条件删除与条件重命名。
3. 创建操作要把“Promise 异常且随后观察到相同内容”判为 unknown outcome，因为相同内容可能由外部参与者创建；不得猜测性删除。
4. 每个目标写入前仍可重验来源和目标，durable journal 必须在第一项写入前进入 applying。发生冲突后只对能够以条件变更证明所有权的目标补偿。

### 回滚、恢复和 Guarded Undo

`read(afterHash) -> restore(before)` 不是 guarded restore：外部编辑可以发生在两步之间。恢复接口必须接收 expected current identity，并使用与正向应用相同的条件变更原语：

- `after content -> before content`：可用 `Vault.process()` 做 content-CAS。
- `after content -> absent`（撤销一次 create）：需要 compare-and-delete；当前 Obsidian Vault API 不支持。出现这种补偿需求时保留文件、锁存后续写入并报告 manual review，不能调用 `delete()` 冒险覆盖并发编辑。
- `absent -> before content`（恢复一次 delete）：需要可靠的 create-if-absent 与归属判断；Interview Submission 已禁止 delete，其他批次若保留 delete，必须把异常后的归属不明视为 unknown outcome。

串行 fence 只排除同一 Obsidian 进程内 OfferAgent 自己的并发 Tool 执行，不能排除用户编辑、其他插件、Sync 或外部进程；它不能替代上述条件变更。

### 产品用语

可以承诺：用户对一个 Vault Change Batch 只能整体确认或拒绝；执行有 checkpoint、journal、漂移检查、受条件约束的补偿和明确的
manual-review 状态。

不能仅凭当前公开 API 承诺：多个文件在任意观察者眼中瞬时原子切换，或失败后必然能自动删除已创建文件。若产品继续使用
“atomic batch”，必须把它明确定义为用户决策与 durable recovery 的逻辑原子性，而非底层多文件事务。

## 必要验证

- 在 `process()` 回调开始前、回调内和 Promise resolve 前注入外部修改，证明 identity 不匹配时保留回调观察到的完整内容并返回
  conflict；无法确定结果时进入 unknown。目标 Obsidian 版本的实测仍应记录是否产生同内容写回或元数据事件，但安全性不依赖其为零。
- 在 guarded restore 的校验与写回之间注入修改；恢复必须冲突，不能覆盖外部内容。
- 对 create 模拟 ACK 丢失以及外部创建同内容文件；不得把文件删除为“回滚”。
- Interview Submission 含 delete/rename 时在预览前拒绝。
- 第二个目标失败时，只有能以 expected after hash 条件恢复的既有文件被自动补偿；新建文件需要条件删除时进入 manual review。
- 进程崩溃后从 durable journal 恢复，任何 unexpected identity 都锁存写入并给出精确路径，不能把顺序补偿结果报告为已证明的多文件事务。
