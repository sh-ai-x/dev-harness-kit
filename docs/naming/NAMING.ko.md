# NAMING — dev-harness-kit 네이밍 규칙 (ADR-0010 SSOT)

> 단일 진실 공급원: 이 파일 + `tests/test_naming.py` 회귀 테스트.

**언어:** [English](NAMING.md) · 한국어

## 스킬 디렉터리 / 파일

- **형식**: `<category>-<verb-or-noun>.md` (kebab-case, 영어)
- **디렉터리**: `skills/<skill-name>/SKILL.md` (한 단계 — Claude Code
  플러그인 스캔 규칙; 카테고리는 프런트매터에 유지)
- **프런트매터 `name:`** = 디렉터리 마지막 세그먼트
- **프런트매터 `category:`** ∈ {`bootstrap`, `plan`, `design`, `build`,
  `review`, `security`, `audit`, `shortcuts`, `ship`, `config`, `eval`,
  `status`}

### 카테고리별 네이밍 패턴

| 카테고리 | 패턴 | 예시 |
|---|---|---|
| `bootstrap` | `<category>-<instrument>` | `bootstrap` (sanity / codebase-map / hook-matrix는 인라인 서브스테이지), `ci-setup` (슬래시 간결성을 위해 프런트매터에서는 `bootstrap` 카테고리에 속하지만 `/dev-kit:bootstrap-ci-setup`이 아니라 `/dev-kit:ci-setup`으로 참조됨) |
| `plan` | (없음 — `plan`은 독립형) | — |
| `build` | `build-<discipline>` | `build-debug` |
| `review` | `review-<subject>` | (없음 — `review`는 독립형) |
| `security` | `security-<subject>` | (없음 — `security`는 독립형) |
| `audit` | `audit-<subject>` | `audit` (slop / secret / outdated는 인라인 모드) |
| `shortcuts` | `shortcut-<name>` | `codex-cache-update`, `log`, `llm-refresh` (탈출구) |
| `ship` | (스킬 없음, 게이트만 존재) | — |

## 슬래시 명령

- **접두사**: `/dev-kit:`
- **0-인자**: 모든 메인 명령은 인자를 받지 않는다.
- **형식**: `/dev-kit:<stage>` (단축: `/dev-kit:<shortcut>`)

## Markdown 문서 / 핸드오프

- `docs/<topic>/{STAGES,NAMING,COST-ANALYSIS,PRE-IMPL-CHECK}.md`
  (PascalCase 또는 kebab-case 단수, 주제별 하위 디렉터리로 그룹화)
- ADR: `docs/adr/ADR-NNNN-kebab-slug.md` (0으로 패딩)
- 핸드오프: `hand-off/<from>→<to>.md` (유니코드 화살표 →; 디버그 재시도는
  ↔ 사용)
- 루프 로그: `.dev-kit/loop-log.json` (단수)
- 예시: `examples/sample-<descriptor>.md`

## 코드 (Python)

- 파일: `snake_case.py`
- 함수: `snake_case()`
- 클래스: `PascalCase`
- 상수: `UPPER_SNAKE_CASE`
- private: `_leading_underscore()`

## Bash

- 파일: `kebab-case.sh` (동작 접미사)
- 함수: `snake_case()`
- 환경 변수: `UPPER_SNAKE`
- 지역 변수: `lower_snake`

## JSON

- 파일: `kebab-case.json` (`marketplace.json`, `.active-hooks.json`)
- 키: `snake_case`

## 훅 스크립트

- `hooks/<verb>-<noun>.sh` (예: `tdd-guard.sh`, `slop-detector.sh`)
- Shebang: `#!/usr/bin/env bash`

## 회귀 검증

`tests/test_naming.py` — SKILL.md 프런트매터의 `name`이 디렉터리
이름과 일치. `category:`가 허용된 집합에 포함. 그 외 규칙도 검증.
