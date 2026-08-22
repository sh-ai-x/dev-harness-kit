# Maintainer PR review trigger

Pull requests authored by trusted repository maintainers (`OWNER`, `MEMBER`,
or `COLLABORATOR`) are reviewed immediately, including when the head branch is
in a fork. `review.yml` and `maintenance.yml` receive the event through
`pull_request_target`, so the workflow definition and credentials come from
the trusted base branch; the jobs inspect the PR through the GitHub API and do
not execute fork checkout contents.

Unknown fork authors continue through `fork-pr-review.yml`, which requires the
existing maintainer environment approval before dispatching the AI judges.
