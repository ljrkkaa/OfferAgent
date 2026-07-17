# Use stop and revise instead of pausing Agent Runs

OfferAgent 不提供冻结并继续同一个 Agent Run 的用户暂停语义。用户停止运行后，已经显示的部分回答作为 durable cancelled Run Event 保留，但 Conversation Context 装配器只接纳 completed Turn 与 completed root Run，因此这段内容不会进入未来模型上下文。原提示词可以回填、修改，并作为新的 Turn/Run 发送；回填不得静默覆盖非空草稿。这样既保留 ChatGPT/Codex 式的快速纠正体验，也避免把未完成的流式文本误当作 Run Checkpoint 或可恢复状态。
