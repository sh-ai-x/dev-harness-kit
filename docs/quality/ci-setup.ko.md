# `/dev-kit:ci-setup` — Dev-Kit CI 템플릿 설치

**언어:** [English](ci-setup.md) · 한국어

`/dev-kit:ci-setup` 스킬은 이미 `/dev-kit:bootstrap`으로 부트스트랩된
모든 프로젝트에 dev-kit의 재사용 가능한 CI 워크플로 템플릿, Git
훅, 로컬 러너 스크립트를 설치한다. 브랜치 정책 가드, 3-잡
validate/test/auto-fix, 심각도 게이트 리뷰라는 같은 CI 형태를
플릿의 모든 저장소에 명령 하나로 복제할 수 있도록 존재한다.

## 설치 후 체크리스트

`/dev-kit:ci-setup`이 `.dev-kit/ci-config.json`을 쓴 후, 다음을 순서대로
수행한다:

1. **GitHub 시크릿과 프로바이더 변수를 추가한다.** 리뷰 + 보안
   워크플로는 LLM 자격 증명 *그리고* 프로바이더 선택자가 필요하다.
   로컬 터미널에서:
   ```bash
   gh secret   set DEV_KIT_GITHUB_TOKEN --repo <owner>/<repo> --app actions   # sh-ai-x/dev-harness-kit로 범위 지정된 PAT
   gh secret   set MINIMAX_API_KEY       --repo <owner>/<repo>                # 또는 ANTHROPIC_API_KEY / DEEPSEEK_API_KEY
   gh variable set CI_REVIEW_PROVIDER    --repo <owner>/<repo> --body minimax
   ```
   `DEV_KIT_GITHUB_TOKEN`은 `sh-ai-x/dev-harness-kit`가 비공개일 때만
   필요하다. 프로바이더 시크릿 + `CI_REVIEW_PROVIDER` 짝 맞추기는 아래
   [GitHub 변수 — 프로바이더 선택](#github-변수--프로바이더-선택) 절에서
   다룬다.
2. **Ruff를 설치하고 Git 훅을 활성화**해서 스테이징된 Python 파일이
   린트되고 `main`으로의 직접 푸시가 클라이언트 측에서 차단되게
   한다:
   ```bash
   brew install ruff                              # macOS
   apt install ruff                               # Debian/Ubuntu
   git config core.hooksPath .githooks
   ```
3. **`.github/workflows/*`를 건드리지 않는 피처 PR을 먼저 연다** —
   이것이 리뷰 + 보안에 대한 스모크 테스트다.
4. **`review.yml`을 추가하는 첫 PR**은 그것이 기본 브랜치에 착륙하기
   전까지는 심각도 게이트로 액션을 검증받을 수 없다. 그 부트스트랩
   PR을 먼저 머지한다; 이후 모든 PR에서 게이트가 동작한다.

이 체크리스트는 `print_checklist=True` kwarg와 함께 호출되면
`lib/ci_setup.py:POST_INSTALL_CHECKLIST`를 통해 스킬이 자동으로
출력한다; Phase 4의 단계별 설치기는 설치가 성공한 후 이를 출력한다.

## GitHub 변수 — 프로바이더 선택

리뷰 + 보안 워크플로는 `vars.CI_REVIEW_PROVIDER`를 읽어 호출할 LLM
프로바이더를 선택한다. 변수 값은 반드시 대응하는 `*_API_KEY` 시크릿과
짝이 맞아야 한다 — 불일치하면 `review.yml`이 `Error: provider secret
missing`으로 1을 반환하며 실패한다.

`/dev-kit:ci-setup` 직후 설정한다:

```bash
gh variable set CI_REVIEW_PROVIDER --repo <owner>/<repo> --body minimax    # 또는 anthropic | deepseek
```

| `CI_REVIEW_PROVIDER` | 워크플로가 읽는 시크릿 | 선택 시점 |
|---|---|---|
| `minimax` (킷 기본) | `${{ secrets.MINIMAX_API_KEY }}` | 킷 개발과 플릿 롤아웃의 기본값 |
| `anthropic` | `${{ secrets.ANTHROPIC_API_KEY }}` | 리뷰어가 Claude(Opus / Sonnet)여야 할 때 |
| `deepseek` | `${{ secrets.DEEPSEEK_API_KEY }}` | 큰 diff의 저비용 리뷰 |

허용 목록(`minimax`, `anthropic`, `deepseek`)은
`review.yml -> workflow_dispatch.inputs.provider.options`와
`bin/set-provider.sh`에서 강제한다. 그 외 값은
`Error: unsupported provider`로 워크플로를 실패시킨다.

두 값 모두 검증:

```bash
gh variable list --repo <owner>/<repo> | grep CI_REVIEW_PROVIDER
gh secret   list --repo <owner>/<repo> | grep -E '(MINIMAX|ANTHROPIC|DEEPSEEK)_API_KEY'
```

짝이 맞는 로컬 선택자는 `.env:CI_REVIEW_PROVIDER`다
(`bin/set-provider.sh <provider>`로 관리). 로컬 측은 `.gitignore` 처리되어
사용자별이며, GitHub 변수는 저장소별이다. `provider-divergence-check.sh`
공유 `hooks/session-start.sh` dispatcher가 호출하는 SessionStart child 훅이
두 값이 어긋날 때 알린다.

> `/dev-kit:ci-setup`의 `--setup-secrets` 플래그는 `CI_REVIEW_PROVIDER`를
> 읽고, `required_secrets_for_provider()`로 필요한 시크릿을 열거한 뒤,
> `gh secret set`을 호출하기 전에 각각 입력받는다. 시크릿 설정이
> 실패해도 설치 자체는 성공한다(경고, 오류 아님).

## 언제 사용하는가

`/dev-kit:bootstrap` 이후, `/dev-kit:build` 이전에 프로젝트당 한 번
`/dev-kit:ci-setup`을 실행한다. 스킬은 멱등하므로 재실행해도 안전하다
(dev-kit이 CI 형태를 업그레이드한 후 템플릿을 새로고침하려면
`--force`를 사용).

## 무엇이 설치되는가

스킬은 CI 템플릿과 canonical hook 소스 트리를 대상 프로젝트로 복사한다.
`hooks/hooks.json`과 `hooks/**/*.sh`는 plugin 소스에서 자동 도출되므로
`templates/ci/`에 hook 코드를 중복 작성하지 않는다. 설치되는 파일:

| 경로 | 목적 |
|---|---|
| `.github/workflows/ci.yml` | 브랜치 정책 경고 + `pytest` 테스트 + `validate.py` 검증기 잡 |
| `.github/workflows/auto-fix-pr.yml` | `changes_requested` 리뷰에 대한 자동 수정 루프(5회 반복 상한, 라벨 카운터, 금지 경로 가드) |
| `.github/workflows/review.yml` | `/dev-kit:review`(3차원) + `/dev-kit:security`(10차원) PR 팬아웃 + 심각도 게이트. **셀프 어웨어 설치 스텝**: 체크아웃이 자체 설치인지 일반 소비자 설치인지 런타임에 감지 |
| `.githooks/pre-push` | `main`에 대한 `git push`를 클라이언트 측에서 차단; `git config core.hooksPath .githooks`로 활성화. dev-kit 소스 저장소는 형제 `.githooks/pre-commit`에 Ruff 린트 게이트도 유지하지만, 소비자에게는 복사되지 않는다. |
| `scripts/validate.py` | dev-kit 자신의 `ci.yml`의 5단계 validate 잡에서 추출됨; 설치 + 마커 + bash 문법을 확인 |
| `scripts/test.sh` | `pytest` 래퍼(`tests/` 디렉터리가 없으면 우아하게 건너뜀) |
| `scripts/branch-policy.sh` | CI 스크립트 컨텍스트를 위한 `pre-push` 미러 |
| `scripts/ci-local.sh` | 로컬 러너 진입점: `validate.py` + `test.sh` + 선택적 `act -l` |
| **`hooks/**/*.sh`** | 공유 helper를 포함한 canonical hook 전체 구현 |
| **`hooks/hooks.json`** | 모든 hook 소스와 함께 복사되는 canonical 등록 manifest |
| **`rules/git-workflow.md`** | 정식 워크트리 규칙; Claude Code가 찾을 수 있도록 `.claude/rules/git-workflow.md`에 설치 |
| **`tests/test_worktree_guard.py`** | 워크트리 규칙을 커버하는 회귀 테스트(차단/허용/실행 권한 비트 등) |

설치 후 마커 파일 `.dev-kit/ci-config.json`이 프로젝트 루트에
작성된다. 이 마커는 `/dev-kit:build`와의 **계약**이다 — 없으면 build가
시작을 거부한다.

## 검증하는 방법

Claude Code와 Codex의 라이프사이클 훅 정의는 `hooks/hooks.json`에서
공유된다. 로컬 상태 리포트는 다음으로 실행한다:

```bash
python3 bin/dev-kit-hooks-status.py
```

Codex에서는 설치 후 또는 훅 정의가 바뀔 때마다 `/hooks`로 플러그인
훅을 검토하고 신뢰한다. Git pre-commit과 pre-push 훅은 두 클라이언트
모두와 별개다. 호스트에 Ruff를 설치한 다음 훅 디렉터리를
활성화한다:

```bash
brew install ruff                              # macOS
apt install ruff                               # Debian/Ubuntu
git config core.hooksPath .githooks
```

```bash
bash scripts/ci-local.sh
```

이것은 `ci.yml`에서 GitHub Actions가 실행하는 것과 같은 점검
집합이지만, `nektos/act`나 푸시 권한이 필요 없다. 예상 출력:

```
=== validate ===
validate.py — repo_root=/path/to/repo
  - installation complete OK (CI 파일 8개 + hook 26개)
  - ci-config marker OK
  - bash syntax OK (셸 파일 30개 정상)
  - test runner OK (bash -n clean)
OK: CI installation valid

=== test ===
... (pytest 출력, 또는 tests/가 없으면 "skip")
```

선택 사항: `nektos/act`가 설치되어 있으면 `act -l`이 발견된 워크플로를
나열한다; 없으면 스크립트가 경고하고 우아하게 폴백한다.

## build로의 핸드오프

스킬은 마커로 `.dev-kit/ci-config.json`을 쓴다. 이 마커가 존재하지
않으면 `/dev-kit:build`는 버전 비교 없이 시작을 거부한다. 다음 게이트
메시지가 보이면:

```
Pre-flight gate: refuse to start if `.dev-kit/ci-config.json` is absent.
Run `/dev-kit:ci-setup` first.
```

…`/dev-kit:ci-setup`을 실행한다(마커가 오래됐다면 `--force`로
재실행).

## FAQ

### 왜 첫 PR의 심각도 게이트에 `::warning::review verdict missing`이 뜨는가?

이 게이트는 `pull_request`와 `workflow_dispatch` 모드 **둘 다**에서
누락된 판정을 허용한다 — 빈 R이나 S는 이제 하드 실패가 아니라
`::warning::` + 기본값 Approve를 낸다. 이는 의도된 것이다: 머지를
막는 것은 (`REVIEW_REQUIRED` / `CHANGES_REQUESTED` on the PR) 사람의
게이트이지, 단일 에이전트 판정 누락이 아니다. 실제 리뷰 피드백
(`Changes Requested` / `Blocked`)은 여전히 exit 1로 PR을 막는다.
`::warning::`은 AI 판정이 비어 있었음(액션 스킵, 속도 제한, 일시적
에러)을 알려주는 정보성 메시지이며, 조사할 수 있게 해준다.

`.github/workflows/review.yml`을 **추가하는** 바로 그 첫 PR은
액션이 여전히 `main`에 대해 그 새 워크플로 파일을 검증할 수 없다
(워크플로 검증 게이트). 그 부트스트랩 PR을 먼저 머지한다; 이후
PR들은 정상적으로 흘러간다.

### 왜 스킬이 `DEV_KIT_GITHUB_TOKEN is required for consumer-install`이라고 불평하는가?

그 시크릿은 업스트림 소스인 `sh-ai-x/dev-harness-kit`가 비공개일
때만 필요하다. 포크/미러가 공개라면 `DEV_KIT_GITHUB_TOKEN`을 비어
있지 않은 임의의 값(예: `gh token`)으로 설정한다 — 설치 스텝이
`git clone https://github.com/...`를 통한 공개 클론으로
단락(short-circuit)된다.



**Q: 기존 `.github/workflows/ci.yml`을 덮어쓰는가?**
A: 아니오 — `--force` 없이 재실행하면 멱등이며 기존 파일을
건너뛴다. dev-kit의 템플릿이 진화한 후 새로고침하려면 `--force`를
사용한다.

**Q: `nektos/act`가 필요한가?**
A: 아니오. `scripts/ci-local.sh`는 어떤 POSIX 호스트에서든 로컬로
같은 검증기를 실행한다. `act`는 선택 사항이다 — 전체 GitHub Actions
동등성(예: Docker 기반 매트릭스 테스트)을 원하면
<https://nektos.act.dev>에서 설치한다.

**Q: 어떻게 제거하는가?**
A: `.dev-kit/ci-config.json`을 삭제한 다음, 설치된 파일들을
`git rm`한다(대상 저장소가 새로 만들어졌고 아직 버전 관리하에 있지
않다면 `rm -rf`도 가능). CI 템플릿은 의도적으로 깊게 통합되어 있지
않다 — 당신이 소유하는 일반 파일이다.

**Q: CI가 `Install dev-kit plugin`에서 `DEV_KIT_GITHUB_TOKEN secret is
required`로 실패한다. 어떻게 하는가?**
A: dev-harness-kit 소스 저장소(`sh-ai-x/dev-harness-kit`)가
비공개다. `review.yml`의 소비자 설치 분기는
`git clone https://x-access-token:${DEV_KIT_GITHUB_TOKEN}@github.com/sh-ai-x/dev-harness-kit.git`를
통해 클론한다. CI에서 이것이 동작하게 하려면:

  1. <https://github.com/settings/tokens?type=beta>에서 다음으로
     **세분화된 개인 액세스 토큰**을 생성한다:
     - **Resource owner:** `sh-ai-x` (또는 dev-harness-kit이 사는 곳)
     - **Repository access:** `sh-ai-x/dev-harness-kit`만
     - **Permissions → Repository permissions:** `Contents: Read-only`
  2. 이 소비자 저장소에서 **Settings → Secrets and variables →
     Actions → New repository secret**으로 이동한다:
     - **Name:** `DEV_KIT_GITHUB_TOKEN`
     - **Value:** 1단계의 세분화된 PAT를 붙여넣는다

  설치 스텝은 `review`와 `security` 잡 둘 다에서 그 시크릿을
  `${{ secrets.DEV_KIT_GITHUB_TOKEN }}`으로 노출한다. 이것이 없으면
  소비자 설치 분기는 일반적인 git 인증 실패 대신 명확한 `::error::`
  메시지와 함께 빨리 실패한다(exit 1).

  나중에 dev-harness-kit이 공개로 전환되면 시크릿을 제거해도 되고
  `git clone`은 자격 증명 없이 동작한다. 설치 스텝을 새로고침하고
  싶다면 `/dev-kit:ci-setup --force`를 재실행한다.

**Q: 왜 마커 파일에 버전이 있는가?**
A: dev-kit 업그레이드로 CI 형태가 바뀐 후 `/dev-kit:build`가 오래된
템플릿에서 실행을 거부할 수 있게 하기 위해서다. dev-kit을
업그레이드한 후 새 검증기 로직을 받으려면
`/dev-kit:ci-setup --force`를 재실행한다.

**Q: 변경 사항을 잃지 않고 파일을 커스터마이징할 수 있는가?**
A: 가능하다 — `/dev-kit:ci-setup --force`가 `EXPECTED_PATHS` 파일을
다시 쓸 때는 템플릿 그대로 그대로 쓴다. 커스터마이징은 그 집합
**밖**에 둔다(예: `.github/workflows/`의 추가 워크플로 파일,
`pre-push` 외의 추가 Git 훅). `EXPECTED_PATHS` 밖의 파일은 절대
건드리지 않는다.
