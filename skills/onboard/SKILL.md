---
name: onboard
category: onboard
description: 0-arg 신규 팀원 onboarding (MUST-47). 30분 productive. CLAUDE.md + .dev-kit + eval baseline 자동.
when_to_use: |
  - User types /dev-kit:onboard <github_username>
allowed-tools: Read Write Bash Glob Grep
model: opus
disable-model-invocation: false
---

# /dev-kit:onboard — 신규 팀원 30분 productive

## 자동 액션

1. CLAUDE.md 갱신 (§0 "team member: <name>")
2. codebase-map §3 갱신
3. first task 자동 위임 (`build-tdd`)
4. PR 자동 (hand-off 첨부)
5. eval baseline 캡처 (golden 13개에 사용자 signature 추가)

## 출력

- `.dev-kit/onboarding-<username>.md` (진행 가이드)
- 첫 PR 자동 생성