---
status: accepted
---

# Use a single minimal Agent Sidebar

OfferAgent 首版只提供一个 Obsidian 右侧 Agent Sidebar，并参考 Claudian 的侧栏聊天体验与 Obsidian 原生视觉语言。侧栏由精简标题区、消息流、默认折叠的工具活动和底部输入框组成；运行中断、变更确认、冲突与撤销只在相关事件发生时以内联提示或模态框出现，不设置常驻任务中心、仪表盘或独立运行管理页。

会话切换、新建会话和设置入口收在标题区与紧凑标签栏中。标签只保存当前打开的 Session、草稿和选择位置；消息、Run 状态与终态仍由 Python Worker 的 Session/Event replay 重建，插件不得把标签变成第二套 Conversation 或 Run 状态。设置页只直接展示 Runtime/Provider 状态、模型、Vault Permission Mode 等常用项；Hosted Web Search 探测、Git Checkpoint 保留和诊断日志归入折叠的高级设置。首版不实现消息内联编辑、计划模式开关、MCP 管理或多 Provider 管理；Claudian 仅作为视觉与交互参考，不决定 OfferAgent 的 Agent、权限或进程架构。
