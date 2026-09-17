> [← 스킬 인덱스](README.ko.md) · [프로젝트 README](../../README.ko.md)

# `build`

**카테고리:** `build` · **알파:** `state` · **호출:** `/dev-kit:build` (사람이 호출)

`build`는 하네스의 실행 엔진이다: 이전 `/dev-kit:plan`이 생성한 단계
파일을 가져와 그것들을 단계당 하나의 격리된 서브에이전트로 실제 실행,
어떤 단일 광범위 세션도 전체 단계의 상태를 한 번에 들고 있지 않아도
되게 한다. 단계 안전 실행은 실제 프로세스 격리(단계별 git 워크트리),
하드 동시성 정책, 영속된 진짜 서브프로세스 증거를 요구하며, 어느 것도
평범한 프롬프트가 자체적으로 보장할 수 없기 때문에 자체 스킬로 산다.

## 사용 시점

- 사용자가 `/dev-kit:build`를 입력.
- 계획과 디자인이 이미 완료 — `PRD.md`와 `phases/<name>/` 존재.
- `/dev-kit:ci-setup`이 이미 `.dev-kit/ci-config.json`을 작성 — 이것은
  제안이 아닌 하드 요구사항.

## 작동 방식

`build`는 `.dev-kit/ci-config.json`이 없으면 시작을 거부, 먼저
`/dev-kit:ci-setup`(또는 오래된 템플릿을 새로 고치려면 `--force`)을 실행하라고
사용자에게 알린다. 버전 비교 게이트 없음 — 마커 파일의 존재만이 중요.

사전 비행 게이트가 통과하면, `lib/execute.py:main`이 단계 인덱스를 읽고,
적격(재개 가능, 비-블록) 단계를 필터링하고, `lib.dispatch_classifier.classify(...)`를
호출해 병렬 vs 순차를 결정하며, 결정을 첫 빌드 줄로 기록(`dispatch:
<mode> — <reason>`). 사용자-대면 토글 없음 — 하네스는 단계 메타데이터에서
디스패치를 추론하며, 사용자가 방출된 줄을 감사.

**Classifier 우선순위 순서** (첫 매치 승리):
1. 임의 쌍(`depends_on` / `consumes`) 간의 **의존성 엣지** → 순차.
2. **모호한 범위**(서문 또는 AC에 TODO/FIXME/TBD/maybe/perhaps/either) → 순차.
3. 두 단계 간 **겹치는 쓰기** → 순차.
4. **N ≥ 4 적격 단계** 그리고 깨끗한 워크트리 격리 → 병렬.
5. **그 외** → 순차.

이전 `--parallel N` / `--allow-parallel-build` 플래그는 v0.3.214에서
제거; argparse가 이제 거부. 순차가 기본; 병렬은 단계가 진짜로 안전할
때만 발화.

`build`는 `/dev-kit:plan`이 방출한 `worktree: "<branch-base>"` 필드를
포함해야 하는 `phases/<name>/index.json`을 읽는다(예: `plan/plugin-harness-v3-0-mvp`).
그로부터 단계별 브랜치(`<branch-base>-step<N>`)와 워크트리 경로(`<root>/.worktrees/<phase>-step<N>`)를
도출; 필드가 없으면 의도된 계약이 아닌 방어-인-심층 조치로 `feat/<phase>`로
폴백.

`status`가 `SKIPPABLE_STATUSES`(`completed`, `unimplemented`)에 있는 단계는
건너뜀. 어떤 단계가든 `status == "blocked"`이면 러너가 exit 코드 2로
빠짐 — 암묵적 재개 없음. `--skip-blocked` 오버라이드는 러너가 블록된
단계를 지나 계속 진행, `pending | error | in_progress`만 실행; 이 방식으로
건너뛴 단계는 실행 후 `.dev-kit/hand-off/build→review.md`에 나열.

각 재개 가능 단계에 대해 `build`는:

1. `git worktree add -B <branch> <wt> origin/main` 실행 (MUST-38 — 단계별
   워크트리 격리).
2. 서문으로 `step<N>.md`를 읽고 인수-기준 가드 + "3-사이클 자가-수정
   최대" 지시를 추가 (MUST-37).
3. `update_step_status`로 단계를 `in_progress`로 표시, `started_at`을
   스탬프.
4. `subprocess.run(["claude", "-p", "--workdir", str(wt), full_prompt], capture_output=True, text=True)` 호출
   — 정확히 단계당 한 서브에이전트 (MUST-36).
5. 실제 `exit_code`, `stdout`, `stderr`, `duration_seconds`로 `phases/<name>/step<N>-output.json`을
   기록 — 스텁된 `0.01` 또는 "stub completed" 값이 아님. 이 쓰기는
   오케스트레이터의 루트 체크아웃이 아닌 단계별 워크트리(`wt`)를 대상:
   아래의 chore 커밋은 `cwd=wt`로 `git add -A`를 실행, 따라서 `root/phases/...`
   아래에 쓰인 파일은 거기서 스테이지되지 않음.
6. 0이 아닌 exit: 단계를 `error`로 표시, `error_message`를 스태시, 커밋
   없이 non-zero 반환.
7. 성공: 단계별 브랜치에서 두 커밋 — `feat({phase}): step {N}[ — <name>]`
   그 다음 `chore({phase}): step {N} output` — 그리고 `--push`가 설정되어
   있으면 단계별 브랜치를 `origin`에 푸시.

상태 상태 머신은 `lib/execute.py`(`VALID_STATUSES`, `SKIPPABLE_STATUSES`,
`RESUMABLE_STATUSES`, 그리고 `--skip-blocked` 오버라이드)에 단일 진실
공급원으로: `/dev-kit:plan`이 `pending`을 방출하고, `build`가 단계를
`in_progress` / `completed` / `error` / `blocked`을 통해 구동.

빌드 단계 동안 훅 매트릭스: `methodology=tdd`일 때 `tdd-guard` ON, `bash-guard`
ON, `secret-scan` ON (PostToolUse), `slop-detector` ON, `stop-verify` ON.

## 사용법

```bash
/dev-kit:build [--skip-blocked] [--push]
```

| 플래그 | 효과 |
|---|---|
| `--skip-blocked` | `blocked` 단계를 지나 계속, `pending \| error \| in_progress`만 실행; 건너뛴 단계는 핸드오프 파일에 기록. |
| `--push` | 성공한 단계 후 단계별 브랜치를 `origin`에 푸시. |

디스패치 모드는 배치당 자동 분류("작동 방식" 참고). `--parallel` 플래그
없음; 분류자가 결정.

## 출력

- 단계당 `phases/<name>/step<N>-output.json`: `{exit_code, stdout, stderr, duration_seconds, timestamp}`,
  모두 실제 서브프로세스 출력.
- `.dev-kit/hand-off/build→review.md`, 자동 기록.
- 성공한 단계당 단계별 브랜치에서 2-커밋 프로토콜: `feat({phase}): step {N} — {name}`와 `chore({phase}): step {N} output`.

테스트 증거: `tests/test_execute.py`의 50 테스트가 러너 동작을 커버(스킵-가능-상태
건너뛰기, blocked가 exit 2 반환, pending 단계가 워크트리를 생성하고 서문
+ 인수-기준 가드와 `claude` 호출, 2-커밋 프로토콜, 실패 시 커밋 없음,
`--push`에 게이트된 푸시, 자동-분류 계약을 위한 새 `TestMainDispatchDecision`
클래스, 그리고 `update_step_status`의 10개 상태-머신 테스트(in-progress
idempotency, duration rounding, reset semantics)). 더해 `tests/test_dispatch_classifier.py`의
27 테스트가 5개의 분류자 규칙, 우선순위 순서, idempotency, reason format,
그리고 `?`-마커 false-positive 회귀를 모두 커버.

## 장기 실행 세션 템플릿

빌드 단계가 한 Claude Code 세션을 넘어 확장될 것으로 예상될 때 — 전형적
신호: 단계 수 >= 5, 또는 사용자가 명시적으로 "이건 여러 날에 걸친 노력"
이라 말할 때 — `build`는 첫 단계 시작 전에 4-파일 템플릿 번들을
`templates/`에서 단계별 워크트리의 `<worktree>/templates/`로 복사(멱등 —
`cp -u`가 오래된 파일만 새로 고침). 번들은 콜드-스타트 수정: 그것 없이는
모든 새 세션이 30-60분을 "마지막 세션이 무엇을 했는가?" 재발견에 씀.

| 템플릿 | 목적 |
|---|---|
| `templates/init.sh` | 부트스트랩: env 검증, 피처 리스트 읽기, 다음 실패 피처 선택, 베이스라인 테스트 실행. 멱등 — 세션 열 때마다 재실행. |
| `templates/feature_list.json` | `{id, description, status, depends_on, test_path}`의 JSON 배열. "남은 것은 무엇인가"의 단일 진실 공급원. |
| `templates/progress.log.md` | 추가 전용 세션-별 로그 (Goal / Work done / Tests status / Blockers / Next session should / Commits). |
| `templates/session_handoff.md` | 콜드-컨텍스트에서 재개 체크리스트; 어떤 코드 변경보다 먼저 세션 열 때 FIRST로 읽기. |

운영 계약: 각 단계의 서문(`step<N>.md`)은 커밋 전에 `progress.log.md`에
추가하고 세션 열 때 `init.sh`를 재실행하는 한 줄 알림을 포함해야 함.
`codex exec`로 구동된 단계도 같은 계약을 지킴 — 러너는 에이전트를
스폰하기 전 워크트리로 템플릿을 복사해 템플릿이 에이전트의 작업 트리의
일부가 됨.

실패 모드: 단계 시작 시 `init.sh`가 exit 3 (`"no failing feature remaining"`)을
내면 빌드가 사실상 완료 — 다른 단계를 강제하기보다 `/dev-kit:review`로
빠짐.

템플릿 동작은 `tests/test_long_running_templates.py`로 검증(구조 + 임시
디렉터리에서 dry-run / missing-list / all-passing exit 코드를 다루는
동작 실행).

## 관련

- [build-debug](build-debug.md) — 단계의 서브에이전트가 체계적인 디버깅을
  필요로 할 때 호출.
- `tdd-guard`와 `stop-verify` — 테스트 우선 편집과 완료 증거를 결정적으로
  시행하는 훅.
- `/dev-kit:review` 그리고 `/dev-kit:security`, 그 다음 `/dev-kit:ship` —
  `build` 완료 후 다음 단계.
- `lib/execute.py` — 이 스킬이 래핑하는 하네스-러너 엔진.
- `lib/dispatch_classifier.py` — 배치당 병렬 vs 순차를 결정하는 순수 Python
  분류자(5-규칙 우선순위 순서, 순차 기본; 레거시 `--parallel` 플래그를
  대체).
- `tests/test_execute.py` — 위에서 참조된 50 테스트.
- `tests/test_dispatch_classifier.py` — 5개 규칙, 우선순위 순서, idempotency,
  reason format을 모두 다루는 27 분류자 테스트.

---
*출처: [`skills/build/SKILL.md`](../../skills/build/SKILL.md)*
