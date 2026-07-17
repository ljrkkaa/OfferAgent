# Issue tracker: GitHub

Issues and PRDs for this repo live in `ljrkkaa/OfferAgent` on GitHub. Use the `gh` CLI for all operations.

## Conventions

- **Create an issue**: `gh issue create --title "..." --body "..."`.
- **Read an issue**: `gh issue view <number> --comments`, also fetching labels.
- **List issues**: `gh issue list --state open --json number,title,body,labels,comments` with appropriate filters.
- **Comment on an issue**: `gh issue comment <number> --body "..."`.
- **Apply or remove labels**: `gh issue edit <number> --add-label "..."` / `--remove-label "..."`.
- **Close an issue**: `gh issue close <number> --comment "..."`.

Infer the repository from `git remote -v`; `gh` does this automatically inside this clone.

## Pull requests as a request surface

**PRs as a request surface: no.**

GitHub shares one number space across issues and PRs. Resolve an ambiguous `#N` with `gh pr view N` and fall back to `gh issue view N`.

## Skill operations

- When a skill says "publish to the issue tracker", create a GitHub issue.
- When a skill says "fetch the relevant ticket", run `gh issue view <number> --comments`.
