# NAMING — dev-harness-kit 명명 규약 (ADR-0010 SSOT)

> Single source of truth: 이 문서 + `tests/test_naming.py` 회귀 검증.

## Skill 디렉토리 / 파일

- **포맷**: `<category>-<verb-or-noun>.md` (kebab-case, 영문)
- **디렉토리**: `skills/<skill-name>/SKILL.md` (한 단계 — Claude Code 플러그인 스캔 규약; category는 frontmatter에 유지)
- **Frontmatter `name:`** = directory 마지막 segment
- **Frontmatter `category:`** ∈ {`bootstrap`, `plan`, `design`, `build`, `review`, `security`, `audit`, `shortcuts`, `ship`, `config`, `eval`, `onboard`, `repair`, `status`}

### 카테고리별 명명 패턴

| Category | Pattern | 예시 |
|---|---|---|
| `bootstrap` | `<category>-<instrument>` | `bootstrap-sanity`, `bootstrap-codebase-map`, `bootstrap-active-hooks` |
| `plan` | `plan-<actor>` | `plan-ralph` |
| `design` | `design-<instrument>` | (deprecated — merged into plan) |
| `build` | `build-<discipline>` | `build-engine`, `build-tdd`, `build-debug`, `build-verify`, `build-simplify`, `build-methodology` |
| `review` | `review-<subject>` | (none — `review` is standalone) |
| `security` | `security-<subject>` | (none — `security` is standalone) |
| `audit` | `audit-<subject>` | `audit-slop`, `audit-secret` |
| `shortcuts` | `shortcut-<name>` | `shortcut-tdd-fast`, `shortcut-quick-fix` |
| `ship` | (no skill, gate only) | — |

## Slash Command

- **Prefix**: `/dev-kit:`
- **0-arg**: 모든 메인 명령. 인자 없음.
- **Format**: `/dev-kit:<stage>` (단축키: `/dev-kit:<shortcut>`)

## Markdown 문서 / 핸드오프

- `docs/{STAGES,NAMING,COST-ANALYSIS,PRIVATE-REPO-SETUP,INTEGRATION,AX,PRE-IMPL-CHECK,METHODOLOGY}.md` (PascalCase or kebab-case singular)
- ADR: `docs/adr/ADR-NNNN-kebab-slug.md` (zero-padded)
- Hand-off: `hand-off/<from>→<to>.md` (Unicode arrow →, debug retry는 ↔)
- Loop log: `.dev-kit/loop-log.json` (singular)
- 예시: `examples/sample-<descriptor>.md`

## 코드 (Python)

- 파일: `snake_case.py`
- 함수: `snake_case()`
- 클래스: `PascalCase`
- 상수: `UPPER_SNAKE_CASE`
- 비공개: `_leading_underscore()`

## Bash

- 파일: `kebab-case.sh` (action suffix)
- 함수: `snake_case()`
- 환경 변수: `UPPER_SNAKE`
- 로컬 변수: `lower_snake`

## JSON

- 파일: `kebab-case.json` (`marketplace.json`, `.active-hooks.json`)
- Key: `snake_case`

## Hook 스크립트

- `hooks/<verb>-<noun>.sh` (e.g., `tdd-guard.sh`, `slop-detector.sh`)
- 헤더: `#!/usr/bin/env bash`

## 회귀 검증

`tests/test_naming.py` — SKILL.md `name` = directory name. category ∈ 9종. 등.
