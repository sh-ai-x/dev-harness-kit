# STAGES — dev-harness-kit 단계별 하네스 스펙

**언어:** [English](STAGES.md) · 한국어

> 참조: ADR-0011, ADR-0020. 7개 단계(B / B.5 / 1 / 2 / 3 / 5a / 5b / 6 / 7) ×
> must/must-not/AC 통합.

## Stage B — Bootstrap (`/dev-kit:bootstrap`)

- **목표**: 새 프로젝트에 최초 진입, 0 → 30분 생산성. 최소 설정: 새
  저장소에 정확히 3개 파일 작성.
- **Must**: (a) 읽기 전용 sanity 감사(stdout에 출력; `--persist-audit`는
  `.dev-kit/sanity-report.md`를 쓴다) (b) CLAUDE.md는 슬림 포인터
  문서로 작성되고 코드베이스 맵은 `docs/CODEBASE-MAP.md`로 지연 로딩
  (`--full-claude-md` 시에만) (c) `.dev-kit/.active-hooks.json` SSOT 초기화
  (d) CLAUDE.md는 `iron-laws/index.md`, `guidelines/index.md`,
  `hooks/index.md`, `rules/index.md`로 상세 내용을 지연 로딩 (e) Codex 및
  다른 CLI를 위한 AGENTS.md 공유 지침 인덱스
- **Must-Not**: 파일 수정(sanity는 읽기 전용). 락파일 수정. 추측.
  핸드오프 파일 영구 저장(CLAUDE.md의 `.dev-kit/hand-off/` 포인터로 충분).
- **AC**: 새 저장소에서: CLAUDE.md (슬림 포인터), AGENTS.md,
  `.dev-kit/.active-hooks.json`, `iron-laws/index.md`, `guidelines/index.md`,
  `hooks/index.md`, `rules/index.md` (`rules/` 존재 시) 모두 존재.
  `.dev-kit/` 디렉터리 자동 생성.
- **활성 스킬**: `bootstrap`(sanity + codebase-map + hook-matrix가 인라인
  서브스테이지), `write_project_md`
- **활성 훅**: `secret-scan`=읽기 전용. 나머지 OFF.
- **핸드오프 출력**: CLAUDE.md의 `.dev-kit/hand-off/` 포인터(bootstrap에서
  별도 핸드오프 파일 없음)

## Stage B.5 — CI Setup (`/dev-kit:ci-setup`)

- **목표**: dev-kit의 CI 형태(워크플로 + pre-push 훅 + 로컬 러너)를
  대상 저장소에 복제. 원-커맨드 CI 동등성.
- **Must**: (a) `.dev-kit/ci-config.json` 마커를 통한 멱등 설치.
  (b) `.githooks/pre-push` + 3개 GitHub Actions 워크플로 미러링.
  (c) dev-kit 자신의 `ci.yml`의 5단계 validate 잡에서 추출된
  `validate.py`. (d) 새로고침용 `--force` 플래그; 그 외에는 덮어쓰기
  거부.
- **Must-Not**: dev-kit 자신의 저장소를 수정. 마커 삭제. 대상 저장소의
  사용자 생성 파일 삭제.
- **AC**: 설치 후 예상되는 15개 파일 모두 존재(워크플로 3개 + 스크립트
  4개 + 워크트리 규칙 파일 5개 + pre-push + .claude/rules/git-workflow.md
  + 테스트). `python3 scripts/validate.py`가 exit 0. `.dev-kit/ci-config.json`의
  스키마가 올바름.
- **활성 스킬**: `ci-setup`(0-인자 오케스트레이터; 숨겨진 `--force`,
  `--target DIR`)
- **활성 훅**: Bootstrap과 동일(`secret-scan`=읽기 전용)
- **핸드오프 출력**: 마커 파일을 통해 `build`를 게이트

## Stage 1 — Plan+Design (`/dev-kit:plan`)

- **목표**: 아이디어 → PRD.md + `phases/<name>/{index.json, step<N>.md}`
- **Must**: 하나의 Ralph 루프, safety_valve=8(MUST-15)에서 5개 게이트
  (frame → validate → non-goals → decompose → emit). `validate` 게이트는
  기존의 evidence / diff-profit / socratic 게이트를 하나의 복합
  수렴 테스트로 융합한다: `evidence_count >= 3` AND
  `value_score = LTV × reachable_users / cost >= 3.0` AND
  `ambiguity_score <= 3`. Phase index.json은
  `lib/execute.py:register_step()`을 통해 작성되므로 모든 스텝이
  명시적 `status`(`unimplemented` → `pending` → `in_progress` →
  `completed`, 런타임용 `error` / `blocked` 추가)를 갖는다.
- **Must-Not**: 코드 작성, 빌드, 배포. PRD.md + phases/ +
  .dev-kit/hand-off/ 외의 산출물 작성. 런타임 전용 상태(`in_progress`,
  `completed`, `error`, `blocked`) 설정 — 그것은 harness-runner의
  몫이다. 이전의 5-질문 grill-me 실행(모호성 루프로 대체됨).
- **AC**: PRD.md 6-섹션 DoD 통과. `phases/<name>/step<N>.md` 4개
  필드(must-read / instruction / AC / Don't). `phases/<name>/index.json`
  스키마 유효. `value_score >= 3.0` AND `ambiguity_score <= 3` 또는
  `loop-log.json`이 `status: held` + 사용자 확인을 보여줌.
  `loop-log.json`에 사이클마다 narrowing이 추가됨.
- **활성 스킬**: `plan`(자체 완결)
- **활성 훅**: `stop-verify`=ON. `slop-detector`=OFF(계획 문서는 허용).
  나머지 OFF.
- **핸드오프 출력**: `plan→build.md`

## Stage 2 — Valuate (`/dev-kit:valuate`)

- **목표**: Stage 1의 계획이 만들 가치가 있는지 결정. `proceed` /
  `revise` / `hold` / `kill` 중 하나를 반환한다. 판정을
  `.dev-kit/valuations/<plan-id>.json`에 저장한다.
- **Must**: (a) `lib/llm_judge.py:call_judge(axes=DIM_AXES["plan_value"])`를
  통해 6개 루브릭 축(problem_fit / roi_estimate /
  existing_solution_edge / team_capability / risk_vs_reward /
  measurability) 채점. (b) `lib/valuation_engine.py:decide(plan,
  rubric_scores)`를 실행해 판정 산출. (c) 절대 리스크 하한 규칙 지키기:
  어느 축이든 < 2.0이면 다른 모든 축과 무관하게 `kill`. (d) 판정
  봉투(`decision` / `rationale` / `blocking_findings`)를
  `.dev-kit/valuations/<plan-id>.json`에 저장.
- **Must-Not**: LLM이 판정을 직접 내리게 허용. 엔진이 유일한
  권한자다 — 판정은 엔진이 하고 judge는 점수만 낸다. 게이트가 그럴
  때 `kill` / `hold` / `revise`를 임의로 내리기.
- **AC**: `.dev-kit/valuations/<plan-id>.json`이 정식 봉투와 함께
  존재. `python3 -m lib.valuation_engine --plan PRD.md --dry-run`이
  유효한 봉투와 함께 exit 0.
- **활성 스킬**: `valuate`(`alpha: enforcement` — 엔진은 결정론적)
- **활성 훅**: `stop-verify`=ON. 나머지 OFF.
- **핸드오프 출력**: `.dev-kit/valuations/<plan-id>.json`(build 단계가
  이 파일을 사전 점검 판정으로 읽는다; build-stage 자동 게이트는
  #463에서 제거되었다. PR #589부터 `valuate`는 모델 호출 전용이며
  사용자 메뉴에는 노출되지 않는다 — `/dev-kit:plan` 등 계획 단계가
  루브릭을 호출하고, build는 판정과 무관하게 계속 진행된다).

## Stage 3 — Build (`/dev-kit:build`)

- **목표**: `phases/<name>/step<N>.md`당 코드 완성 + 회귀 GREEN.
- **Must**: (a) `phases/<name>/step<N>.md`를 정확히 따르기. (b) AC
  명령을 실행하고 출력을 인용. (c) 버그 → 재현 → 근본 원인 →
  회귀 테스트 → 최소 수정(`build-debug`를 통한 4단계 디버그).
  (d) 2-커밋 프로토콜(feat + chore). 참고: `.dev-kit/valuations/<plan-id>.json`을
  읽고 비-PROCEED 판정을 거부하던 Phase 4 자동 게이트가 여기 있었지만
  #463까지였다; 그 게이트는 이제는 제거된 URI 서브스트레이트에
  묶여 있었으므로 서브스트레이트와 함께 사라졌다. PR #589부터
  `valuate`는 모델 호출 전용이며 판정 봉투는 순수 권고로 동작한다 —
  build는 판정과 무관하게 진행된다.
- **Must-Not**: AC를 추측("동작할 것", "아마 괜찮을 것"). `output.json`
  삭제. 여러 변경을 배치로 묶기.
- **AC**: 모든 스텝이 `status=completed`. `pytest`가 exit 0 + 개수
  인용. 2-커밋 프로토콜 준수.
- **활성 스킬**: `build-tdd`, `build-debug`, `build-verify`,
  `build-refactor`(스텝별 하네스 러너 + 방법론 선택기는
  `lib/execute.py` + `lib/methodology/`에 있다; prune의 3단계 스윕은
  `prune`에 인라인됨)
- **활성 훅**: `tdd-guard`, `bash-guard`, `secret-scan`,
  `slop-detector`, `stop-verify` — 모두 ON
- **서브에이전트**: Phase 3(계획됨). 현재는 순차 실행만.
- **핸드오프 출력**: `build→review.md`

## Stage 5a — Review (`/dev-kit:review`)

- **목표**: diff에서 정확성 + 보안 + 아키텍처 결함을 찾고 PR 스타일
  판정을 낸다.
- **Must**: 모든 발견사항은 `failure_scenario` + `confidence`를
  가진다. **단일 메시지 3차원 팬아웃**. 별도 검증 패스.
- **Must-Not**: 검증 패스를 건너뛰기. 검증되지 않은 critical을 보고.
- **AC**: PR 요약에 `**Verdict:**` + 정렬된 인라인 발견사항. 심각도별
  개수.
- **활성 스킬**: `review`
- **활성 훅**: `slop-detector`, `secret-scan`, `stop-verify` = ON.
  리뷰/보안 판정 게이트는 `.github/workflows/review.yml`(CI)을 통해
  실행.
- **핸드오프 출력**: `review→ship.md`

## Stage 5b — Security (`/dev-kit:security`)

- **목표**: OWASP Top 10 2025(A01~A10) 감사.
- **Must**: 카테고리별 분석 표. 단일 메시지 10차원 팬아웃. 검증기
  CONFIRMED ≥ 5.
- **Must-Not**: A0X ID 건너뛰기. 검증되지 않은 critical.
- **AC**: 카테고리별 표. 심각도별 판정.
- **활성 스킬**: `security`
- **활성 훅**: Review와 동일.
- **핸드오프 출력**: `security→ship.md`(Review와 독립)

## Stage 6 — Ship (`/dev-kit:ship`)

- **목표**: 릴리스 준비된 태그 발행.
- **Must**: Review 판정=Approve + Verify AC 통과 + Pre-push
  main-block 통과.
- **Must-Not**: main에 직접 푸시. `--no-verify` 남용.
- **AC**: git tag + CHANGELOG 항목 + 릴리스 전 스모크 테스트.
- **활성 스킬**: (없음, 수동 게이트만)
- **활성 훅**: `stop-verify`=ON.

## Stage 7 — Maintenance Gate (`.github/workflows/maintenance.yml`)

- **목표**: clean-code + 과설계 + 가치에 대한 PR 전용 시행. pre-push의
  intent 점검과 짝을 이룬다. bump-PR을 제외한 모든 PR에서 실행.
- **Must**: (a) `maintenance_judge` 잡이 `claude-code-action`을 통해
  `/dev-kit:maintenance --diff <PR>`을 호출한다;
  `eval/prompts/judge-maintenance.md`의 judge 프롬프트가 정식
  20항목 루브릭(CC-1..8 + OE-1..8 + VM-1..4)을 적용하고 세 개의
  복합 0-10 축(`code_sanity_score`, `docs_coverage_score`,
  `scope_discipline_score`)을 낸다. (b) `gate` 잡이
  `lib/maintenance_gate.py:extract_verdict`를 통해 판정을
  추출한다(review.yml의 패턴을 미러링). (c) `gate`는 마지막 단계로
  docs-updated 서브게이트(`lib/maintenance_gate.py:docs_updated_ok`)를
  실행한다. (d) 통합 판정 도출: `code_sanity_score < 5` → Blocked;
  `5..7.99` → Changes Requested; `≥ 8` → Approve; docs-updated
  실패가 있는 Approve는 Changes Requested로 강등. (e) bump-PR
  건너뛰기는 review.yml의 필터
  (`startsWith(title, 'chore(release): bump dev-kit to v')`)를
  미러링.
- **Must-Not**: PR을 자동 승인. `--no-verify` 등가물 우회(존재하지
  않음; 게이트는 절대 자동 승인하지 않는다). PR이 아닌 표면에
  전용 maintenance 워크플로를 사용.
- **AC**: `.github/workflows/maintenance.yml`에 워크플로 존재.
  `python3 -m lib.maintenance_gate --extract-verdict-from-stdin`이
  exit 0이고 `Approve|Changes Requested|Blocked|""`를 출력. `tests/test_maintenance_gate.py`가
  GREEN(판정 추출, docs-updated 점검, combine_verdict, CLI
  서브프로세스를 커버하는 ≥ 19개 테스트). Eval 골든 케이스
  `eval/golden/maintenance-{01,02,03}*.json`이 존재하고 기대된
  판정 밴드로 해석됨. `lib/llm_judge.py:DIM_AXES["maintenance"]`에
  3개 축이 등록됨. `docs/quality/maintenance-gate.md`가 임계값 +
  우회 정책을 문서화.
- **활성 스킬**: `maintenance`(워크플로 동반 에이전트-스킬, 옵트인
  호출 패턴)
- **활성 훅**: (인프로세스 훅 없음 — 순수 CI 게이트)
- **핸드오프 출력**: (터미널 — 이 게이트는 review/security와 함께
  머지 전 마지막 신호다)

## 횡단 — Inspect (`/dev-kit:inspect`)

- **목표**: (PR별, diff별이 아닌) 전체 코드베이스 건강 감사, 6개
  차원(`dead`, `dup`, `smell`, `overeng`, `cleancode`, `slop`)을
  병렬로. 마크다운 리포트 하나를 `.dev-kit/inspect-report.md`에
  생성.
- **Must**: 단일 메시지 6차원 팬아웃. 생존자에 대한 검증 패스.
  리포트에서 HIGH/MED/LOW 버킷팅. 발견사항마다 `failure_scenario`
  필수.
- **Must-Not**: 소스 파일 수정(읽기 전용 불변식). PR 코멘트 게시.
  스킬 밖에서 리포트 편집.
- **AC**: `.dev-kit/inspect-report.md`에 리포트 존재. 판정은
  `Critical | Major drift | Minor drift | Healthy` 중 하나. 차원별
  요약 표 존재.
- **활성 스킬**: `inspect`

## 횡단 — Eval (`/dev-kit:evaluate`)

- **목표**: 에이전트 행동 평가(등록된 루브릭에 대해 트랜스크립트 재생).
- **Must**: 기존 review/security/plan, harness-quality, os-quality,
  maintenance, D1–D7 계약을 유지하고 workflow evidence로
  `harness_effectiveness` 5개 영역(예방, first-pass, recovery, learning,
  measurement integrity)을 함께 평가한다.
- **Must-Not**: 평가 중 성공 evidence를 만들거나, transcript만으로 누락된
  workflow event를 추정하거나, 별도 effectiveness option/skill을 요구하지 않는다.
- **Evidence source**: workflow 경계의 TraceLog event,
  `.dev-kit/repair/events.jsonl`, 기존 eval transcript/report, legacy fallback
  artifact를 명시적으로 구분한다.
- **AC**: 기존 평가와 5개 effectiveness component가 별도로 표시된다.
  evidence가 없으면 `INSUFFICIENT_EVIDENCE`이며 기존 verdict는 하위 호환된다.
- **활성 스킬**: 통합 report를 제공하는 `evaluate`만 사용한다.
