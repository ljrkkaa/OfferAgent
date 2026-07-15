# Use stop and revise instead of pausing Agent Runs

OfferAgent 不提供冻结并继续同一个 Agent Run 的用户暂停语义。用户停止运行后，已经显示的部分回答保留为不进入后续 Conversation Context 的 Stopped Run 内容；原提示词可以回填、修改，并作为新的 Agent Run 重新发送。这样既保留 ChatGPT/Codex 式的快速纠正体验，也避免把未完成的流式文本误当作 Run Checkpoint 或可恢复状态。
