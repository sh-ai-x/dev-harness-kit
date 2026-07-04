# STAGES — dev-harness-kit 단계별 하네스 명세

> ADR-0011 + ADR-0020 참조. 6 stages × must/must-not/AC 통합.

## Stage B — Bootstrap (`/dev-kit:bootstrap`)
- **Goal**: 신규 프로젝트 첫 진입 0 → 30분 productive.
- **Must**: (a) sanity read-only PASS (b) codebase-map 자동 합성 (c) active-hooks.json SSOT 초기화 (d) CLAUDE.md §1~§5 통합 기록
- **Must-Not**: 파일 수정 ❌ (sanity + map 모두 read-only). lockfile 변경 ❌. 추정 ❌.
- **AC**: sanity-report.md PASS / WARN ≤ 3. CLAUDE.md 5 섹션 STUB (--slim) 또는 FULL. .dev-kit/ 디렉토리 자동 생성.
- **Active Skills**: bootstrap-sanity, bootstrap-codebase-map, bootstrap-active-hooks, write_claude_md
- **Active Hooks**: secret-scan=read-only. 그 외 OFF.
- **Hand-off out**: bootstrap→plan.md (사용자 OK 다음)

## Stage 1 — Plan+Design (`/dev-kit:plan`)
- **Goal**: idea → PRD.md + phases/<name>/step<N>.md
- **Must**: 6 gates (frame → evidence → diff → non-goals → socratic → prd-writer) + Seed convergence + Phase 분해. **단일 Ralph loop, safety_valve=8** (MUST-50, MUST-15).
- **Must-Not**: 코드·빌드·배포 작성. PRD.md 외 산출물 작성. /dev-kit:plan 외 sub-step 호출 ❌. 동일답변 ≥ 2회.
- **AC**: PRD.md 5 DoD 통과. phases/<name>/step files 5필드. final_similarity ≥ 0.85. loop-log.json narrowing 매 cycle append.
- **Active Skills**: plan-ralph (pm-prd-fast + interview-harness 통합), build-harness-engine
- **Active Hooks**: stop-verify=ON. slop-detector=OFF (기획 문서 정당). 그 외 OFF.
- **Hand-off out**: plan→build.md

## Stage 3 — Build (`/dev-kit:build`)
- **Goal**: phases/<name>/step<N>.md 단계별 코드 완성 + 회귀 GREEN.
- **Must**: (a) `phases/<name>/step<N>.md` 그대로 수행. (b) AC 명령 실제 실행 + 출력 인용. (c) 버그 시 재현 → 원인 → 회귀 테스트 → 최소 fix (4-phase debug, build-debug). (d) 2-commit protocol (feat + chore).
- **Must-Not**: AC 추측 ❌ ("should work", "probably fine"). output.json 삭제 ❌. 여러 변경 한꺼번에.
- **AC**: 모든 step status=completed. pytest exit code 0 + count 인용. 2-commit protocol 준수.
- **Active Skills**: build-engine, build-tdd, build-debug, build-verify, build-simplify, build-methodology
- **Active Hooks**: tdd-guard, bash-guard, secret-scan, slop-detector, stop-verify = 모두 ON
- **Sub-agent**: sub_agent_runner (MUST-36~38). AC 위임 + 자가 수정 루프.
- **Hand-off out**: build→review.md

## Stage 5a — Review (`/dev-kit:review`)
- **Goal**: 변경 코드의 correctness + security + architecture 결함 + PR-style verdict.
- **Must**: 각 finding에 `failure_scenario` + `confidence`. **단일 메시지 3-dim fan-out**. verifier pass 별개.
- **Must-Not**: verifier pass 생략 ❌. 미증거 critical 보고 ❌.
- **AC**: PR summary **Verdict:** + 정렬된 inline findings. severity별 카운트.
- **Active Skills**: review
- **Active Hooks**: slop-detector, secret-scan, stop-verify = ON. review-pre-commit (git) + dev-kit-review.yml (CI).
- **Hand-off out**: review→ship.md

## Stage 5b — Security (`/dev-kit:security`)
- **Goal**: OWASP Top 10 2025 (A01~A10) audit.
- **Must**: per-category breakdown table. 단일 메시지 10차원 fan-out. verifier CONFIRMED ≥ 5.
- **Must-Not**: A0X ID 누락. 미증거 critical.
- **AC**: per-category table. severity별 verdict.
- **Active Skills**: security
- **Active Hooks**: Review와 동일.
- **Hand-off out**: security→ship.md (Review와 독립)

## Stage 6 — Ship (`/dev-kit:ship`)
- **Goal**: release-ready tag 발행.
- **Must**: Review verdict=Approve + Verify AC 통과 + Pre-push main-block 통과.
- **Must-Not**: main 직접 push. --no-verify 남용.
- **AC**: git tag + CHANGELOG entry + pre-release smoke.
- **Active Skills**: (no skill, manual gate only)
- **Active Hooks**: stop-verify=ON.

## Cross-cutting — Audit (`/dev-kit:audit`)
- **Goal**: slop + secret 일괄 감사.
- **Must**: HIGH/MEDIUM/LOW buckets 출력. banned-phrase regex SSOT.
- **Must-Not**: 파일 수정 (read-only).
- **AC**: HIGH ≥ 5 = warning. 발견 0 = 0 finding.
- **Active Skills**: audit-slop, audit-secret

## Cross-cutting — Eval (`/dev-kit:eval`)
- **Goal**: 자산 신선도 평가 (CLAUDE.md / skill / hook / Iron Law).
- **Must**: 4축 점수 (semantic_drift / completeness / correctness / consistency). 2-judge cross-check.
- **AC**: ≥ 8 OK. < 5 ROT → CI fail.
- **Active Skills**: audit-eval, audit-a2a (Phase 3)

## Cross-cutting — Repair (`/dev-kit:repair`)
- **Goal**: Eval-Repair 8단계 loop. 마지막 단계 = 사용자 1회 approve.
- **Must**: 7단계 자동. 8단계 Human Review 만 동기 STOP.
- **Must-Not**: auto commit diff ❌ (MUST-NOT-31). review design 빌드 자체 변경 ❌.
- **AC**: human approve|reject|defer 만 commit.
- **Active Skills**: 9개 Specialized Fixer per category.
