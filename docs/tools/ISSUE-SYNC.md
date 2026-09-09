# GitHub-issue sync gate

`.github/workflows/issue-sync.yml` calls `tools/issue_sync.py parse` to enforce the one-time PR ↔ GitHub-issue sync contract.

## State mapping

| PR body has … | Gate result |
| --- | --- |
| No GitHub-issue reference | Skipped — `::notice`, exit 0 |
| One or more `#N` / `owner/repo#N` references | Each ref must be `open` at gate time; any `closed` ref → hard fail |
| Ref points at a missing/deleted issue (404) | Hard fail (treated as stale link) |

The parser recognizes GitHub's standard close-keywords (`close`/`closes`/`closed`, `fix`/`fixes`/`fixed`, `resolve`/`resolves`/`resolved`) plus reference-only keywords (`ref`/`refs`/`reference`/`references`, `see`, `part of`, `related to`, `tracking`). Bare `#N` mentions with no keyword are also counted — a contributor who writes `Tracking #123` is still linking the PR to that issue.

## Design

- **Blocking, unlike `linear-pr-sync.yml`.** Linear state-sync is best-effort (`continue-on-error: true`); a closed GitHub-issue reference is a stale-link contract violation that should surface in CI rather than at merge time, so this gate hard-fails on the first closed ref.
- **Sparse checkout** — `tools/issue_sync.py` only. Mirrors `linear-pr-sync.yml`'s checkout shape so the two one-time-check workflows share the same runner-time surface.
- **Attacker-influenced fields in `env:`** — `PR_TITLE` / `PR_BODY` are passed via the step's environment, not spliced into `run:` shell source, so a malicious PR body containing `'` / `;` / `$()` cannot break out of an unquoted shell variable.
- **Strips fenced code blocks + HTML comments** before scanning — PR templates and design docs that embed `#NN` examples do not falsely trip the gate.
- **`\b` word boundary** on the ref group keeps `foo#bar` slugs from triggering a numeric-issue match.
- **Keyword inside the regex span** (named group `kw`) — a look-back approach loses keywords at offset 0; capturing the keyword in the match span keeps the parser's behavior consistent at the start of the body/title.

## Running locally

```bash
python3 tools/issue_sync.py parse --body "Closes #42" --json
# → [{"ref": "#42", "owner": null, "repo": null, "number": 42, "keyword": "closes", "source": "body"}]
```

Tests live at `tests/test_issue_sync.py` and cover keyword normalization, cross-repo references, code-fence / inline-code / HTML-comment stripping, word-boundary edges, the `--quiet` shell-gating mode, and the CLI subprocess shape (matches the CI sparse-checkout path).
