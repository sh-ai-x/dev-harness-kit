# /dev-kit:review-local

GH-Actions 리뷰 워크플로우의 로컬 equivalent. `claude` CLI로 `/dev-kit:review` + `/dev-kit:security` + `/dev-kit:maintenance`를 실행하고 동일한 verdict 추출 + combined gate + L3-evidence를 적용.

## When to use

- PR에서 GH-Actions minutes가 소진된 경우
- 푸시 전에 로컬에서 review verdict를 반복 확인하고 싶은 경우
- provider 전환 (`bin/set-provider.sh`) 테스트 후 푸시하기 전

## When NOT to use

- org 레벨 MCP나 private reviewer bot에 의존하는 PR — local `claude -p`는 MCP 서버 접근 불가
- `gh pr merge` 필요 시 — 병합은 human action이며 이 스크립트 범위 밖

## Usage

```bash
# Dry-run (LLM 호출 없이 env + planned commands 미리보기)
bin/review-local.sh --pr 123 --dry-run

# 전체 리뷰 + clean verdict 시 auto-approve
bin/review-local.sh --pr 123 --auto-approve

# 특정 provider 강제 지정
bin/review-local.sh --pr 123 --provider anthropic --auto-approve

# /dev-kit:review만 실행 (security + maintenance 스킵)
bin/review-local.sh --pr 123 --review-only
```

## Arguments

| Argument | Description |
|:---------|:------------|
| `--pr N` | PR 번호 (필수) |
| `--provider` | LLM provider: `minimax`, `anthropic`, `deepseek` (기본: `.env:CI_REVIEW_PROVIDER`) |
| `--auto-approve` | verdict가 Approve이고 L3-evidence gate 통과 시 `gh pr review --approve` |
| `--review-only` | review만 실행 |
| `--security-only` | security만 실행 |
| `--maintenance-only` | maintenance만 실행 |
| `--dry-run` | LLM 호출 없이 planned commands만 출력 |

## Related

- `bin/review-local.sh` — implementation
- `skills/review/` — review skill invoked via `claude -p`
- `skills/security/` — security skill
- `lib/maintenance_gate.py` — verdict extraction + combined gate helper
- `.github/workflows/review.yml` — GH-Actions equivalent
- `docs/local-ci.md` — full local-CI playbook
