# 훅 레퍼런스 — 시행 계층

**언어:** [English](HOOK-REFERENCE.md) · 한국어

이 플러그인의 하중 받는 표면은 **결정론적 시행**이며 프롬프트 산문이
아니다. `CLAUDE.md`의 Iron Law L7("스킬의 알파는 모델이 자기 자신을
부과할 수 없는 부분에 산다")에 따라, 아래 훅은 도구 호출이 실행되기
전에 단락시키며 — 직접 차단하거나 마스킹하기 때문에 모델이 그것을
건너뛰고 싶어할 때도 그대로 유지되고, 순수 추론 기반 스킬이 더 똑똑한
미래 모델에 '흡수'될 수 있는 방식으로는 흡수될 수 없다.

스킬(`/dev-kit:*`)은 이 훅들과 빌드 상태 머신(`phases/<name>/index.json`)
위편의 래퍼에 불과하다. 이 페이지에서 한 가지만 기억한다면: **규칙을
실제로 시행하는 것은 훅이며, 스킬은 그것을 운전하기 좋게 만들 뿐이다.**

*어디서 훅 커버리지가 여전히 빈약한지*(알려진 갭, 런타임별 배선 차이)에
대한 동반 감사는 [`hook-coverage-gaps.md`](hook-coverage-gaps.ko.md)를
본다.

---

## 시행 훅, 무엇을 지키는가 기준

| 훅 | 하는 일 | 단계 |
|---|---|---|
| `tdd-guard` | 실패하는 테스트 없이 `lib/` 편집을 차단 | Build |
| `bash-guard` | 파괴적 `git` / `rm` / 셸 이스케이프를 거부 | Build |
| `secret-scan` | 도구 입력에서 자격 증명 패턴을 마스킹 | All |
| `slop-detector` | (KO+EN) 어구 + 구조 뱅크에 걸친 AI-전형 패턴을 잡는다 | Build + Review + Security |
| `loop-detect` | 세션별 fingerprint로 동일한 Bash 호출이 3회 연속 일어나면 경고 | All |
| `worktree-guard` | 메인 체크아웃에서의 Edit/Write를 하드 차단; 거절 시 `git worktree list --porcelain`을 통해 라이브 워크트리 목록을 출력 | All |
| `git-guard` | 브랜치 전략을 시행: `main` 커밋/푸시, force-push, `gh pr merge`를 차단; 피처 브랜치로 `git push`할 때 `plugin.json` 슬롯을 검증 (슬롯 검사는 단위 테스트 가능한 진실표 보존을 위해 `hooks/lib/slot-check.sh`로 추출됨 — 아래 *공유 헬퍼* 참고) | All |
| `worktree-auto-cut` | 작업별 워크트리 + 브랜치를 생성 | All |
| `stop-verify` | 세션 종료 전에 종료 코드 / 테스트 횟수 인용 + 5-항목 의도 체크리스트 (`lib/pre_completion_checklist.py`) | Plan + Design + Build + Review + Security + Ship |
| `review-yml-isolation` | `review.yml` PR을 `review.yml`만으로 강제 | All |

## 이벤트별 훅 인벤토리

같은 훅을 그것을 발화시키는 Claude Code / Codex 이벤트 인덱스 —
*왜* 훅이 작동했거나 작동하지 않았는지 디버깅할 때 유용:

| 훅 | 이벤트 | 목적 | 모드 |
|---|---|---|---|
| `tdd-guard.sh` | PreToolUse (Write\|Edit\|MultiEdit) | TDD 테스트 우선 시행 | advisory / `--strict` |
| `bash-guard.sh` | PreToolUse (Bash) | 파괴적 명령 차단 | advisory / `--strict` |
| `git-guard.sh` | PreToolUse (Bash) | 브랜치 전략 시행 | hard-block |
| `worktree-guard.sh` | PreToolUse (Write\|Edit\|MultiEdit) | 메인 체크아웃 편집 차단 | hard-block |
| [`linear-autosync.sh`](linear-autosync.ko.md) | PreToolUse (Write\|Edit\|MultiEdit) | `tools/linear_sync.py`를 통해 모든 Edit를 사용자 Linear 워크스페이스에 자동 동기화 (dev-kit 프로젝트 디렉터리가 아니면 silent-bail) | advisory (silent exit 0) |
| `review-yml-isolation.sh` | PreToolUse (Bash) | `review.yml` 변경을 자체 커밋/PR로 강제 | hard-block |
| `worktree-auto-cut.sh` | UserPromptSubmit | 메인에서 새-작업 프롬프트에 대해 워크트리를 자동 생성 | advisory (fails open) |
| `session-start-check.sh` | SessionStart | 워크트리 규칙을 알려준다 | advisory |
| `log-on-session-start.sh` | SessionStart | 매 세션마다 로그 훅을 (idempotent하게) 자동 설치 | advisory |
| `provider-divergence-check.sh` | SessionStart | `.env:CI_REVIEW_PROVIDER`가 off-list거나, 발산하거나, 누락되었을 때 알린다 | advisory |
| `secret-scan.sh` | PostToolUse (Write\|Edit) | 편집에서 자격 증명을 감지 | hard-block |
| `slop-detector.sh` | PostToolUse (Write\|Edit) | AI 슬롭 차단 (어구 + 구조 + 점수, KO+EN) | advisory (opt-in strict) |
| `worktree-log-auto-install.sh` | PostToolUse (Bash) | 새로 추가된 워크트리에 로그 훅을 설치 | advisory |
| `loop-detect.sh` | PostToolUse (Bash) | 반복되는 동일한 Bash 호출 후 재시도 전에 경고 | advisory (fails open) |
| `acp-tier-assert.sh` | PreToolUse (`*`) | 첫 도구 호출에 ACP 에이전트 tier-assertion 라인 (M/T/L) 강제 | hard-block |
| `stop-verify.sh` | Stop | 세션 종료 시 회귀 테스트 + 사전 완료 의도 체크리스트 실행 | hard-block |
| `sub-agent-handoff.sh` | PostToolUse (Agent) | 서브에이전트 응답에 STATUS / EVIDENCE / NEXT-ACTION 조각이 실려 있는지 검증; advisory; jq 누락 시 fail-closed | advisory (jq 누락 시 fail-closed) |

**"Mode" 열 읽기:** `hard-block`는 도구 호출이 그대로 거부됨을 뜻한다 —
훅을 제거하는 것 외에는 우회가 없다. `advisory`는 훅이 경고하며
(`tdd-guard`/`bash-guard`의 경우 `--strict`로 에스컬레이션해 하드 블록
가능). `fails open`는 훅 자체의 내부 오류가 작업을 블록하지 않고 그
호출에 대한 검사를 건너뛴다.

### 공유 헬퍼 (`hooks/lib/`)

이들은 그 자체로 훅이 아니다 — 위의 훅이 `source`해 로직을 PreToolUse
셸 스크립트 안에 인라인하는 대신 단위 테스트 가능하게 유지하도록 한다.
각 헬퍼는 자체 `tests/test_<helper>.py` 회귀 커버리지를 가진다.

| 헬퍼 | source 하는 곳 | 목적 |
|---|---|---|
| `payload-parse.sh` | 대부분의 PreToolUse 훅 | `read_stdin_json`, `require_jq` |
| `secret-patterns.sh` | `secret-scan.sh` | Bash ERE 자격 증명 뱅크 (`lib/analysis_core/runner.py::_SECRET_PATTERNS`와 SSOT) |
| `worktree-detect.sh` | `worktree-guard.sh`, `git-guard.sh` | `worktree_detect` (`--git-dir == --git-common-dir` 판별자의 단일 진실 공급원) |
| `hook-preamble.sh` | 6개 훅 (`tests/test_hook_preamble.py` 참고) | 공통 preamble: `set -euo pipefail`, `LC_ALL=C.UTF-8`, `$0` 상대 경로 설정 |
| `locale-utf8.sh` | preamble를 사용하는 훅 | 일회성 `LC_ALL=C.UTF-8` / `LANG=C.UTF-8` 설정 |
| `slot-check.sh` | `git-guard.sh` | `plugin.json` 버전-슬롯 검사를 위한 `slot_should_deny <claude> <codex> <expected>` 진실표 (2026-08-03 추가, inspect finding #2) |
| `stage-gate.sh` | `stop-verify.sh` | `hook_stage_active` + `pre_completion_checklist_active` 단계 활성화 헬퍼 (후자는 stop-verify의 단계 + 오버라이드 규칙을 따라 같은 게이트에서 의도 체크리스트가 발화하도록) |
| `loop-detect.sh` | `hooks/loop-detect.sh` | 세션별 Bash fingerprint를 추가하고 구성된 임계값에서 연속 매치를 감지 |

---

## 더 보기

- [훅 커버리지 갭](hook-coverage-gaps.ko.md) — 이 매트릭스의 알려진 갭과 런타임별 배선 차이 (Claude Code vs. Codex).
- [`linear-autosync.sh`](linear-autosync.ko.md) — 매 Edit Linear 자동 동기화 훅 (PROJECT_DIR 가드, ENV fast-path, non-blocking 계약).
- [`rules/git-workflow.md`](../../rules/git-workflow.md) — `worktree-guard`와 `git-guard`가 시행하는 워크트리 + 브랜치 규칙.
- [`docs/architecture/RUNTIME-PORTABILITY.ko.md`](../architecture/RUNTIME-PORTABILITY.ko.md) — 같은 훅이 Claude Code와 Codex 둘 다에서 실행되는 방식.
- 메인 [`README.ko.md`](../../README.ko.md) — 짧은 버전, "작동 원리" 아래.

## 타임아웃 정책

UserPromptSubmit 훅(특히 `tdd-scope-judge.sh`와 `worktree-auto-cut.sh`)은
`hooks.json`에 명시적 `timeout: 120`을 가진다. 30초 기본값은 이 훅들에
불충분하다:

- `worktree-auto-cut.sh`는 `git fetch origin main` + `git worktree add`를
  실행하며, 둘 다 느린 origin이나 큰 HEAD에서 30초를 초과할 수 있다.
- `tdd-scope-judge.sh`는 path-규칙 미스 폴백으로 LLM judge
  (`lib.tdd_scope_judge`)를 실행한다. state 파일 root는
  `${DEV_KIT_TDD_ROOT:-$(git rev-parse --show-toplevel)}`로 결정되며,
  이는 `tdd-guard.sh`가 `.tdd-scope.json`을 읽을 때 쓰는 것과 동일한
  폴백이다 — `DEV_KIT_TDD_ROOT`가 git toplevel 밖을 가리켜도 두 훅이
  같은 state 경로에 합의하도록 한다.

두 훅 모두 advisory이므로(스크립트 레벨 계약에 따라 실패 시 exit 0)
타임아웃은 정확성을 깨는 대신 알림을 조용히 버린다 — 그러나 사용자가
제안을 잃는다. 120초는 전형적인 경우(<10s)보다 충분히 위이며
600초 기본 훅 천장보다 충분히 아래다. 다른 훅 그룹(PreToolUse,
SessionStart, PostToolUse, Stop)은 30초 기본값을 상속; 현재 무거운
경로를 실행하는 것이 없어 기본값이 적절하다.
