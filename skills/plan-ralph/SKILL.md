---
name: plan-ralph
category: plan
description: PM/기획 전 과정을 하나의 Ralph 루프로. 아이디어 → PRD.md까지 단일 stage (Plan+Design 통합, MUST-50). 6 gates (frame → evidence → diff → non-goals → socratic → prd-writer) + Seed convergence (5 cycles, in-loop). PRD.md 외 다른 산출물 절대 ❌ (코드·빌드·배포).
when_to_use: |
  - User types `/dev-kit:plan` with idea
  - User wants PRD regenerated
  - Resume from .pm-prd-fast/decision-log.md (HOLD 후 재개)
allowed-tools: Read Write Glob AskUserQuestion
disallowed-tools: Bash Edit NotebookEdit WebFetch
model: opus
disable-model-invocation: false
safety:
  safety_valve: 8
  convergence: composite (rubric ≥ 75 + final_similarity ≥ 0.85 + DoD 5 conditions)
  narrowed_delta: bool
  dedup_metric: identical-answer-cycle=2
  user_interrupt: true
---

# plan-ralph — Integrated PM (Plan+Design merged, MUST-50)

## Core Goal
**오직 플랜과 기획만 만든다.** 실제 구현·빌드·배포 절대 ❌. 사용자 goal + AC + non-goals 받아 → 6 gates + Seed convergence 단일 Ralph 루프 → `PRD.md` + `phases/<name>/step<N>.md` 자동 산출.

## 입력 / 출력

- **입력**: 사용자 1-line idea + AC (1~5개) + non-goals (1~3개)
- **출력**: `PRD.md` + `.pm-prd-fast/*.md` + `phases/<name>/{index.json, step<N>.md}` + `.dev-kit/hand-off/plan→build.md`
- **공통 누적**: `.pm-prd-fast/decision-log.md` + `.dev-kit/loop-log.json`

## 6 통합 단계 (1 Ralph 루프)

```
[1/8] frame-problem      ← idea + customer + situation + cause + cost
       ↓
[2/8] evidence-gate     ← rubric ≥ 75 OR 3+ independent sources
       ↓
[3/8] diff-profit-gate  ← 3 alternatives + customer-language differentiation + positive unit margin
       ↓
[4/8] non-goals         ← 3+ non-goals with rationale + breach-response
       ↓
[5/8] socratic-deepen   ← Cut Line 5-question check (≥3 pass)
       ↓
[6/8] Phase 분해         ← phases/<name>/index.json 자동 (MUST-50 absorption)
       ↓
[7/8] Seed convergence   ← interview-harness 통합: similarity ≥ 0.85
       ↓
[8/8] prd-writer        ← PRD.md 6-section DoD 5 conditions
```

## 규칙 (예외 없음)

- 5필드 loop 선언 (MUST-15): safety_valve=8, convergence composite, narrowed_delta, dedup_metric, user_interrupt
- PRD.md 외 산출물 ❌ (코드 / package.json / Dockerfile / test 코드)
- 사용자 "코드 짜줘" 요청에도 PRD 완성 전엔 코드 작성 ❌
- HOLD 발생 시 `/dev-kit:plan` 재호출로 재개
- loop-log.json 매 cycle narrowing append (MUST-16)

## Hook 정렬

Plan/Design 단계:
- `slop-detector=OFF` (기획 문서는 LLM-typical 표현 정당)
- `stop-verify=ON`
- 그 외 OFF

## Hand-off

PRD.md 완성 시:
- `state_codec.transition_stage(root, "build")`
- `state_codec.append_hand_off(root, "plan", "build", "...")` 자동
- `.dev-kit/hand-off/plan→build.md` 작성
- `/dev-kit:build` 호출 대기
