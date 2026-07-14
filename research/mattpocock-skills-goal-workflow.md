# Matt Pocock skills workflow for a Codex Goal

Researched against `mattpocock/skills` `main` at commit [`66898f60e8c744e269f8ce06c2b2b99ce7660d5f`](https://github.com/mattpocock/skills/tree/66898f60e8c744e269f8ce06c2b2b99ce7660d5f). Repository facts and OfferAgent recommendations are separated below.

## Repository facts

- The documented build chain is `grill-with-docs -> to-spec -> to-tickets -> implement -> code-review`. `to-spec` synthesizes the already-settled conversation, and the spec carries user stories, implementation decisions, testing seams, and out-of-scope boundaries. ([to-spec workflow](https://github.com/mattpocock/skills/blob/66898f60e8c744e269f8ce06c2b2b99ce7660d5f/docs/engineering/to-spec.md#L51-L59), [spec contents](https://github.com/mattpocock/skills/blob/66898f60e8c744e269f8ce06c2b2b99ce7660d5f/docs/engineering/to-spec.md#L29-L43))
- `to-tickets` produces tracer-bullet vertical slices: each ticket must cut a narrow but complete path through the relevant layers, be independently demonstrable/verifiable, and fit one fresh context window. Each ticket declares its blockers. ([rules](https://github.com/mattpocock/skills/blob/66898f60e8c744e269f8ce06c2b2b99ce7660d5f/skills/engineering/to-tickets/SKILL.md#L25-L38))
- The **frontier** is the set of tickets whose blockers are all done. The source explicitly says to implement the frontier one ticket at a time and clear context between tickets. A real tracker may expose several frontier tickets for parallel agents, but parallel execution is an optional way to run the artifact, not part of ticket generation itself. ([frontier and tracker behavior](https://github.com/mattpocock/skills/blob/66898f60e8c744e269f8ce06c2b2b99ce7660d5f/docs/engineering/to-tickets.md#L29-L36), [one ticket per fresh context](https://github.com/mattpocock/skills/blob/66898f60e8c744e269f8ce06c2b2b99ce7660d5f/docs/engineering/to-tickets.md#L48-L56))
- `/implement` is intentionally small: implement the supplied spec/ticket, use `/tdd` at pre-agreed seams where possible, run typechecks and focused tests regularly, run the full suite at the end, run `/code-review`, then commit to the current branch. ([implement source](https://github.com/mattpocock/skills/blob/66898f60e8c744e269f8ce06c2b2b99ce7660d5f/skills/engineering/implement/SKILL.md#L7-L15))
- `/implement` **does create a commit** on the current branch. It does **not** say to push, create a branch or PR, comment on issues, or close issues. Those actions require separate orchestration policy; they must not be attributed to the skill. ([implement source](https://github.com/mattpocock/skills/blob/66898f60e8c744e269f8ce06c2b2b99ce7660d5f/skills/engineering/implement/SKILL.md#L7-L15))
- TDD tests external behavior at pre-agreed public seams, one failing test and one minimal implementation at a time. The guidance rejects horizontal “all tests, then all implementation” work. ([TDD seams and loop](https://github.com/mattpocock/skills/blob/66898f60e8c744e269f8ce06c2b2b99ce7660d5f/skills/engineering/tdd/SKILL.md#L18-L36))
- Setup is repository-scoped: it records the issue tracker, triage-label vocabulary, and domain-doc layout in the repo’s agent instructions and `docs/agents/*`. It should run once before the engineering skills when that configuration is absent. ([setup purpose](https://github.com/mattpocock/skills/blob/66898f60e8c744e269f8ce06c2b2b99ce7660d5f/skills/engineering/setup-matt-pocock-skills/SKILL.md#L7-L15), [written configuration](https://github.com/mattpocock/skills/blob/66898f60e8c744e269f8ce06c2b2b99ce7660d5f/skills/engineering/setup-matt-pocock-skills/SKILL.md#L63-L116))
- There is no Codex Goal orchestration skill in the current engineering-skill catalog. Goal is therefore a Codex execution wrapper around this repository’s ticket workflow, not a behavior defined by Matt Pocock’s repository. ([current catalog](https://github.com/mattpocock/skills/blob/66898f60e8c744e269f8ce06c2b2b99ce7660d5f/README.md#L169-L198))

## Recommendations for OfferAgent

1. Create one Codex Goal for the whole outcome, with **no `token_budget` field**. “Unlimited” should mean “do not impose a finite Goal budget,” not an invented large number.
2. Treat each ticket as a checkpointed phase inside that Goal. At the start of every phase, re-read the parent spec, current ticket, `CONTEXT.md`, relevant ADRs, and current code. Do not rely on prose memory from the previous ticket. This approximates the repository’s “fresh context” rule while retaining a single long-running Goal.
3. Implement only one frontier ticket at a time, choosing the lowest issue number when several are unblocked. A verified ticket commit counts as done for the Goal’s local dependency ledger.
4. Capture `ticket_base_sha` before each ticket. Run TDD and focused checks during implementation; run ticket-level acceptance and typechecking before commit. The repository’s current `/code-review` skill compares committed `HEAD` with a fixed point, while `/implement` says review before committing. To make those interfaces work together, create the ticket implementation commit, review `<ticket_base_sha>...HEAD`, fix findings, then amend or add a review-fix commit before declaring the ticket complete.
5. Default remote policy should be conservative: read GitHub issues, but do not push, create PRs, or close issues unless the user explicitly grants that authority. Keep local Goal progress separately so the next local frontier can advance. If the user wants tracker state updated automatically, require successful push before closing a ticket.
6. The Goal is complete only when all ticket acceptance criteria pass, every ticket has a durable commit, and the final end-to-end acceptance ticket passes. A single blocked ticket should not stop the Goal while another frontier ticket remains.

## Recommended reusable prompt structure

```text
在当前 OfferAgent 仓库创建一个 Codex Goal。不要设置 token_budget；持续执行直到整个目标真正完成。

Goal objective:
严格依据 GitHub 父规格 Issue #1、Tickets #2-#16、本地 Spec、CONTEXT.md 与 ADR，按依赖关系逐步实现 OfferAgent v1。每张 Ticket 都必须满足验收条件、通过测试与 review，并形成独立、可追溯的 Git commit；最后完成 Ticket #16 的真实端到端验收。

执行规则：
1. 先读取仓库指令、Git 状态、Issue #1、#2-#16 的正文/评论/当前状态、本地 Spec、CONTEXT.md 与相关 ADR。保护用户已有的未提交修改，不覆盖或夹带无关改动。
2. 建立并维护 Ticket 进度计划。Frontier = 所有 blockers 已在本 Goal 中验证并提交完成的开放 Ticket。每次只实现一个 frontier Ticket；若有多个，默认选择编号最小者。
3. 对每张 Ticket 都按已安装的 $implement 工作流执行，并在适用时使用 $tdd 和 $code-review：
   - 开始时记录 ticket_base_sha；
   - 重新读取父规格、当前 Ticket、相关 ADR/领域术语和当前代码，视为一个新的 Ticket 上下文；
   - 只实现当前 Ticket 的完整 tracer-bullet vertical slice，不提前实现被阻塞 Ticket；
   - 在预先约定的 seam 上 red -> green，一次一个行为切片；
   - 频繁运行类型检查和当前测试文件；Ticket 结束运行其验收测试及所有相关测试；
   - 创建当前 Ticket 的实现 commit；以 ticket_base_sha 为 fixed point 运行 code review；修复有效发现后 amend 或追加 review-fix commit，再复验；
   - 记录 Ticket 的 commit、测试命令、验收证据和剩余风险，然后推进 frontier。
4. 每完成一张票，下一张票必须从 Git、Issue、Spec 和 ADR 重新加载事实，不把上一张票的临时推断当成事实。可以让上下文压缩，但 durable state 必须在 Git/计划/测试结果中。
5. 不设置 token 限额，不因耗时或上下文压缩停止。遇到失败要诊断并重试；遇到外部阻塞时记录准确原因，并继续任何其他未阻塞的 frontier Ticket。只有没有可继续工作且确需用户或外部状态时才报告阻塞。
6. 不 push、不创建 PR、不关闭 GitHub Issue，除非我另行明确授权。Goal 内以“验收通过 + 已提交”为 Ticket 完成标准，并据此推进依赖。
7. 不删除或改写范围外的用户文件；不得使用破坏性 Git 命令。遵守 Spec 的 Out of Scope，不擅自扩展功能。
8. 只有 #2-#16 全部实现、验证、提交，并且 #16 端到端验收通过后，才把 Goal 标为 complete。最终报告每张 Ticket 的 commit、验证结果、未推送/未关闭的远程状态及任何已知限制。

现在创建 Goal 并立即开始第一个 frontier Ticket；不要只给计划。
```

If automatic tracker publication is desired, replace rule 6 with: “After each ticket passes and its commit has been pushed successfully, post concise verification evidence and close that ticket; do not close it before the commit is reachable remotely. Create no PR unless explicitly requested.”
