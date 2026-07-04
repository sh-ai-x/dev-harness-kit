---
name: plan
category: plan
description: 0-arg command. Plan+Design 통합 (MUST-50). 6 gates (frame → evidence → diff → non-goals → socratic → prd-writer) + Seed convergence + Phase 분해. PRD.md + phases/<name>/step<N>.md 자동 산출. plan-ralph SKILL dispatch.
when_to_use: |
  - User types /dev-kit:plan with new idea
  - User wants PRD regenerated from existing .pm-prd-fast/decision-log.md
allowed-tools: Read Write Glob AskUserQuestion
disallowed-tools: Bash Edit NotebookEdit WebFetch
model: opus
disable-model-invocation: false
---

# /dev-kit:plan — Plan+Design Integrated Stage

## Iron Law
**PRD.md 외 산출물 절대 ❌.** 사용자가 "코드 짜줘" 강요 시에도 PRD 완성 전엔 코드 작성 ❌.

## 동작

1. Plan+Design 1 stage 통합 (MUST-50): pm-prd-fast 6 gates + interview-harness Seed convergence + harness-runner Phase 분해 모두 1 Ralph loop에서.
2. `plan-ralph` SKILL dispatch.
3. safety_valve=8 (MUST-15) + loop-log.json narrowing append (MUST-16).
4. 동일답변 2회 hard STOP + narrowed=false ≥ 2회 STOP (MUST-NOT-9, 10).
5. PRD.md 완성 시 hand-off `plan→build.md` 자동 생성 + state.json stage=build 전이.

## 입력 / 출력

- 입력: 사용자 1-line idea + AC 1~5 + non-goals 1~3
- 출력: `PRD.md` + `.pm-prd-fast/*.md` + `phases/<name>/{index.json, step<N>.md}` + `.dev-kit/hand-off/plan→build.md`
- 누적: `.pm-prd-fast/decision-log.md` + `.dev-kit/loop-log.json`

## Hook 정렬 (Stage A)

| Hook | Mode |
|---|---|
| stop-verify | ON |
| 그 외 | OFF |

slop-detector OFF: 기획 문서는 LLM-typical 표현 정당 (MUST-13).

## 다음 단계

`/dev-kit:build` — PRD.md + phases/<name>/step<N>.md 기반 step-by-step 실행 (sub-agent 위임, MUST-36).