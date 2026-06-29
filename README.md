# Khoj 面试版个人知识库 Agent

这是一个基于 Khoj 精简改造的个人知识库 Agent 项目，当前目标是跑通第一阶段 demo 主链路：

`Obsidian 同步 -> 本地文档索引 -> 搜索/RAG -> Chat 问答 -> 工具调用 -> 记忆 -> 评估`

项目保留 Khoj 原有的核心 Agent 能力，但去掉或隐藏了商业化、多客户端和部分外部服务功能，让仓库更适合展示个人知识库、RAG、Agent 工具编排和本地优先的产品思路。

## 当前已有功能

### 1. Obsidian 知识库同步

当前保留 Obsidian 插件和同步链路，可用于把本地 Obsidian 项目库中的内容同步到服务端。

- 支持通过 Obsidian 客户端上传内容。
- 默认主线面向 Markdown、PDF 和纯文本资料。
- 服务端负责内容解析、切分、索引和后续检索。

### 2. 本地文档索引

项目目前聚焦个人知识库最小闭环，支持以下内容类型：

- Markdown
- PDF
- Plaintext / HTML-like plaintext

相关处理逻辑位于 `src/khoj/processor/content`，搜索和过滤能力位于 `src/khoj/search_type`、`src/khoj/search_filter`。

### 3. Web/API Chat

项目保留 Web 和 API 聊天入口，可通过统一的后端 Agent 链路处理用户问题。

- API 聊天入口：`/api/chat`
- Web 路由和页面仍保留在 `src/interface/web`
- 支持会话历史、模型配置、文件上下文和流式回答等基础能力

### 4. 搜索与 RAG 引用

当前主线能力是基于本地知识库进行检索增强生成。

- 支持从本地索引中检索相关文档片段。
- 回答可携带知识库引用，便于追踪答案来源。
- 支持文本搜索、过滤条件和 Agent 上下文组合。

这部分是面试展示的核心：用户提问后，系统先检索本地资料，再把相关上下文交给模型生成回答。

### 5. Agent 配置与工具能力

项目保留 Agent 配置、工具权限和工具调用相关逻辑。

当前可作为高级能力保留的工具包括：

- Online search / webpage 工具
- Code tool
- MCP tool
- Operator/browser/computer 相关能力
- Deep Research 多轮研究链路

这些能力适合在 RAG 主流程稳定后作为进阶演示。默认演示路径仍建议先走本地知识库问答。

### 6. Memory 长期记忆

项目保留用户记忆能力，可用于记录用户偏好、长期事实和可复用上下文。

- 支持查看、修改和删除记忆。
- 支持 Agent/用户维度的上下文隔离。
- 可作为个人助理能力的一部分展示。

### 7. Deep Research

项目保留 `/research` 研究链路，用于展示更复杂的 Agent 规划和工具执行能力。

Deep Research 可以多轮选择工具、收集资料、记录中间步骤，并在最后汇总结果。它适合展示“模型不只是聊天，而是能规划任务并调用工具”。

### 8. 测试与评估

项目保留 pytest 测试和 eval 相关入口。

- 单元/集成测试位于 `tests`
- 测试数据位于 `tests/data`
- 评估脚本位于 `tests/evals`

后续可以补充一组本地知识库 gold set，用于衡量 RAG 回答质量、引用准确性和工具选择效果。

## 本地运行

初始化开发环境：

```bash
bash scripts/dev_setup.sh
```

启动本地服务：

```bash
bash scripts/run_local.sh
```

默认本地服务会读取 `.env`，使用嵌入式数据库配置，并监听 `127.0.0.1:42110`。可通过环境变量覆盖主机、端口、模型和 API 配置。

运行测试：

```bash
uv run pytest
```

运行单个测试文件：

```bash
uv run pytest tests/test_local_kb.py
```

## 当前暂不支持或已隐藏的功能

为了让项目更聚焦，当前分支已经删除、隐藏或暂不作为主线展示以下能力：

- Android、Desktop、Emacs 多客户端
- Stripe 订阅计费
- Twilio、WhatsApp、短信/手机号登录
- S3 上传路径
- 外发 telemetry 服务
- 语音转文字、文字转语音
- creative image generation 入口
- Notion/GitHub 内容源 UI 和 API
- DOCX、图片 OCR、Org-mode 的第一阶段外露入口
- QQ bot 实际接入

注意：部分数据库模型、历史 migration、底层 processor 或旧测试可能仍保留，用于兼容和后续清理；它们不代表当前第一阶段 demo 的主线功能。

## 项目状态

当前仓库是一个面试/演示导向的精简分支，重点展示：

1. 本地优先的个人知识库同步与索引。
2. 基于本地资料的搜索和 RAG 引用回答。
3. Agent 工具选择、长期记忆和 Deep Research 等进阶能力。
4. 可测试、可评估、可继续扩展到 QQ bot 或其他入口的架构。

完整端到端效果仍依赖真实 LLM、embedding 和本地知识库配置。建议演示顺序为：先导入 Obsidian/Markdown/PDF 资料，再展示搜索和 Chat/RAG，最后展示 Memory、Deep Research 或 MCP/Operator 等高级能力。
