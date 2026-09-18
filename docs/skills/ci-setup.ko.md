> [← 스킬 인덱스](README.ko.md) · [프로젝트 README](../../README.ko.md)

# `ci-setup`

**카테고리:** `bootstrap` · **알파:** `enforcement` · **호출:** `/dev-kit:ci-setup` (사람이 호출)

`ci-setup`은 dev-kit의 재사용 가능한 CI 워크플로 템플릿 — GitHub Actions
워크플로, pre-push 훅, 스크립트, 규칙 파일을 다루는 15개 예상 경로 — 을
대상 프로젝트에 설치한다. 멱등이며, `.dev-kit/ci-config.json`의 존재에
의해 순수하게 게이트되고(버전 비교 없음), 그 마커 파일은 `/dev-kit:build`가
시작하기 전 확인하는 하드 전제조건이다.

## 사용 시점

- 사용자가 `/dev-kit:bootstrap` 후 `/dev-kit:ci-setup`을 입력.
- 사용자가 새 저장소에서 같은 CI 형태(branch-policy + validate + test +
  auto-fix)를 원함.
- 사용자가 `/dev-kit:build`를 위해 저장소를 준비 중 (ci-setup이 전제조건).
- 사용자가 템플릿을 새로 고치기 위해 재실행 (`--force` 플래그).

## 작동 방식

`lib/ci_setup.py`를 통한 3-단계 오케스트레이션:

**Phase 1 — Detect** (결정론적, LLM 호출 없음): 인자 파싱 (`--target DIR`은
`$PWD`로 기본; `--force`는 덮어씀; `--setup-secrets`는 repo 시크릿을
묻고 설정; `--skip-verify`는 Phase 3를 건너뜀); `python3 ≥ 3.10` 확인;
존재 단락을 `install_ci_config()`에 위임, 마커와 모든 예상 경로가 이미
존재하고 `force=False`이면 no-op 리포트를 반환; `.git/` 프로브(없으면
경고)와 `.github/` 프로브(없으면 생성); 라운드트립 JSON 파스 + 비어
있지 않은 dict 검사를 통해 `.dev-kit/ci-config.json` 작성 후 하드 검증,
손상을 조용히 삼키지 않고 에러로 보고.

**Phase 1.5 — Pre-flight probe** (`gh`가 없을 때 silent): `gh auth status`,
`gh repo view`, `gh secret list --json name`에 대한 읽기 전용 검사로
`DEV_KIT_GITHUB_TOKEN`, `MINIMAX_API_KEY`, `ANTHROPIC_API_KEY` 각각에
대해 OK/WARN/INFO/SKIP을 반환. 실패한 프로브는 설치를 절대 차단하지 않음.

**Phase 2 — Install**: 15개 `EXPECTED_PATHS` 각각에 대해 존재하고
`force=False`이면 건너뛰고, `force=True`이면 덮어쓰고, `shutil.copy2`로
복사(git diff 안정성을 위해 mtime 보존); 셸 스크립트, pre-push 훅,
`validate.py`에 `chmod 0o755`; `.dev-kit/ci-config.json` 마커를 원자적으로 작성.

**Phase 1.7 — Lint pass** (non-fatal, 항상 실행, no-op 멱등 재설치에서도):
`lint_installed_workflows()`가 이전에 설치된 워크플로의 알려진-오래된 패턴
(예: `pull_request` 모드에서 누락된 판정에 하드 실패하던 pre-0.1.3
게이트가 있는 `review.yml`)을 경고(에러 아님)로 표시; 사용자가 `--force`로
재실행하여 finding에 대응.

**Phase 3 — Verify** (`--skip-verify`가 아니면): 설치된 모든 `.sh`와
pre-push 훅에 `bash -n`; 설치된 모든 `.py`에 `ast.parse`; `python3 scripts/validate.py`
(`"OK: CI installation valid"` 예상); `bash scripts/ci-local.sh` (exit 0
예상); `act`가 없으면 WARN하는(실패하지 않는) `act -l` 검사.

**Phase 4 — Post-install checklist**: 성공 시 그리고 옵트인 시 출력,
`OWNER/REPO`가 `git remote get-url origin`에서 자동 채워짐. `--setup-secrets`
(Phase 4b)와 함께 스킬은 `CI_REVIEW_PROVIDER`에서 프로바이더를 읽고(env
→ `.env` → `.env.example` → 기본 `minimax`), `required_secrets_for_provider()`를
통해 필수 시크릿을 열거하며, `AskUserQuestion`으로 각각을 묻고 `set_repo_secrets()`를
호출해 `gh secret set` 실행 — 시크릿 설정이 실패해도 설치는 성공(경고로
표시).

## 사용법

```bash
/dev-kit:ci-setup [--force] [--setup-secrets] [--target DIR] [--skip-verify] [--provider NAME]
```

| 플래그 | 효과 |
|---|---|
| *(0-인자)* | `$PWD`에 대한 멱등 설치/no-op. |
| `--force` | `EXPECTED_PATHS` 안의 기존 파일만 덮어씀. |
| `--setup-secrets` | `gh secret set`를 통해 필수 repo 시크릿을 인터랙티브하게 설정. |
| `--target DIR` | `$PWD`가 아닌 다른 디렉터리에 설치 (숨겨진 플래그). |
| `--skip-verify` | Phase 3 검증을 건너뜀 (숨겨진 플래그). |
| `--provider NAME` | CI 리뷰 프로바이더를 오버라이드 (숨겨진 플래그). |

실패 exit 코드: `1` = 인자 에러, `2` = 마커 존재 + `--force` 없음, `3` = 복사
실패, `4` = 검증 실패.

## 출력

`.dev-kit/ci-config.json` — `/dev-kit:build`가 시작하기 전 요구하는 마커/계약.
더해 15개 설치된 경로:

| 경로 | 목적 |
|---|---|
| `.github/workflows/ci.yml` | 브랜치-정책 경고 + 테스트 + 검증 잡 |
| `.github/workflows/auto-fix-pr.yml` | `changes_requested` 리뷰에 대한 자동-수정 루프 (5-iter 캡) |
| `.github/workflows/review.yml` | `/dev-kit:review`(3-차원) + `/dev-kit:security`(10-차원) PR 팬아웃 + 심각도 게이트 |
| `.githooks/pre-push` | main을 대상으로 하는 직접 `git` `push`를 블록하는 클라이언트-사이드 훅 |
| `scripts/validate.py` | 설치 + 마커 + bash 문법 확인 |
| `scripts/test.sh` | Pytest 래퍼 (`tests/` 없으면 우아하게 건너뜀) |
| `scripts/branch-policy.sh` | CI 스크립트 컨텍스트를 위한 pre-push 훅의 미러 |
| `scripts/ci-local.sh` | 로컬-러너 진입점 |
| `hooks/worktree-guard.sh` | 메인 체크아웃에 대한 PreToolUse Write/Edit 블록 |
| `hooks/session-start.sh` | 단일 SessionStart 등록점; lifecycle child 훅을 분배 |
| `hooks/lib/worktree-detect.sh` | 공유 `--git-dir`/`--git-common-dir` 판별자 |
| `hooks/hooks.json` | canonical hook registry와 SessionStart dispatcher를 올바른 이벤트 matcher에 배선 |
| `.claude/rules/git-workflow.md` | 브랜치 / 워크트리 / PR 규약 |
| `tests/test_worktree_guard.py` | 4개 규칙 훅 + hooks.json 배선에 대한 회귀 테스트 |

## 관련

- [bootstrap](bootstrap.ko.md) — 일반적으로 이 스킬 전에 실행.
- [bootstrap](bootstrap.ko.md) — `bootstrap` + 이 스킬을 한 호출로 구성.
- `/dev-kit:build` — 이 스킬이 쓰는 `.dev-kit/ci-config.json` 마커 없이는
  시작을 거부.
- `docs/quality/ci-setup.ko.md` — 전체 사용법 문서.

---
*출처: [`skills/ci-setup/SKILL.md`](../../skills/ci-setup/SKILL.md)*
