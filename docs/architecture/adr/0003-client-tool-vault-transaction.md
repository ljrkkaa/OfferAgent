# ADR-0003：Vault 写入是 Loop 内 Client Tool 事务

状态：Accepted

## 决策

所有写入统一表示为 `vault.transaction`，依次通过 Schema、Policy、preflight、Diff、Approval、`expectedHash` 重验、真实执行、Journal 和结果回填。模型面对的定义和持久化执行器始终位于 Worker；Obsidian 通过双向 Named Pipe 提供逐路径 live-editor before/after hash、打开/未保存状态和预览证明。只有该证明与 Worker 的物理路径、内容 hash 和事务计划完全一致，且所有受影响编辑器均已保存并关闭时，Worker 才使用同一套 handle-based CAS 执行。`trash` 的 canonical proof 同时包含源路径的“存在→缺失”和 Worker 内部归档路径的“缺失→原内容”；内部路径由 `runId + toolCallId +` 模型侧 `argsHash + operationIndex + source` 确定性生成，私有 IPC `transactionId` 不参与路径身份，插件与 Worker 任一侧计算漂移都会精确冲突而不是忽略隐藏路径。

插件不得直接 create/append/patch/rename/trash。`client/tool/invoke` 对新的 Vault 变更固定 fail closed，仅保留旧 invocation journal 的只读结果核对；这避免把 `Editor.setValue()` 的两秒 debounce、Obsidian API 的路径 TOCTOU 或插件崩溃误报为已持久化成功。编辑器仍打开或有未保存内容时返回显式 conflict，用户保存并关闭后可基于新 hash 重新预检和审批，不猜测、不覆盖。

最终 live preview 与本地 CAS 之间必须持有绑定到原 `clientConnectionId` 和原 Reverse Channel generation 的提交租约。断连 retirement 会先从新路由中原子撤销该连接，再等待已经取得的租约完成；旧 Run 即使遇到同 ID 的测试性重连，也不能切换到新 channel。这里不得用 `contains()` 后再提交的竞态式检查。

registry lease 本身不等于物理 Pipe 活性，也不能冻结 Obsidian UI，因此 ClientBound 提交采用第二道明确屏障：Worker 完成 CAS 和同句柄 `verify_committed` 后暂不清理 backup/rollback handles，必须通过同一 leased Pipe 调用 `client/tool/commit-observe`。插件重新确认每个物理路径仍无打开/未保存编辑器并返回真实磁盘 after-hash；读取期间 TFile 或 editor identity 改变也直接冲突。只有该观察与 Worker 计划完全一致才 cleanup 并形成成功结果；Pipe 断开、编辑器重开、hash 漂移或协议异常都会进入原有 rollback，回滚无法证明时返回 unknown。观察完成后再打开文件会读取已经确认的新磁盘内容，不再持有旧 buffer。

## 后果

`prepared` 不解除写完成义务。Worker CAS 的 claim/publish/rollback 使用 no-replace 和同句柄结果确认；ACK 丢失不得重放 append。插件只返回真实只读证明，成功 ToolResult 必须同时来自 Worker 的真实 after hash和同连接 post-commit observation。代价是当前 Obsidian API 无法提供条件式、崩溃持久的 live-buffer commit，因此打开的目标文件必须先保存并关闭；这是安全边界，不使用启发式自动合并。
