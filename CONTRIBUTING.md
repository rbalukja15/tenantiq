# Contributing

This repo is built in the open, issue by issue, so the history is legible.

## Workflow
1. Pick an issue and move it to **In Progress** on the project board.
2. Branch from `main`: `feat/<issue#>-slug`, `docs/<issue#>-slug`, `fix/<issue#>-slug`.
3. Implement with tests. Run `make lint` and `make test` before committing.
4. Open a PR whose body contains `Closes #<n>`. Fill in the PR template.
5. Ensure CI is green, self-review, then **squash merge**.

## Commit messages
[Conventional Commits](https://www.conventionalcommits.org/):
`feat(rag): enforce citation schema in LLM output`

## Decisions
Any non-trivial decision gets an ADR in `docs/adr/` (Context / Decision / Consequences).

## Definition of done
Code + tests + docs updated + a dated entry in `docs/devlog.md` + the issue auto-closed by the PR.
