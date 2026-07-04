---
name: build-tdd
category: build
description: Red-Green-Refactor 사이클. methodology=tdd 일 때 (default). failed test 없이는 production code ❌. hook tdd-guard 활성.
when_to_use: |
  - User types "X 만들어줘" / "X 추가해줘" / "X 구현해줘"
  - methodology=tdd (default) sub-agent
allowed-tools: Read Write Edit Bash
disallowed-tools: WebFetch Agent
model: opus
---

# build-tdd — Red-Green-Refactor

## Iron Law
```
실패 테스트 없이 프로덕션 코드 작성 금지.
테스트 실패를 직접 확인하지 않으면 알 수 없다.
```

## Cycle

```
RED      → 실패하는 테스트 작성
           ↓ 실행해서 RED 직접 확인
GREEN    → 통과시키는 최소 구현 작성
           ↓ 모든 테스트 GREEN 확인
REFACTOR → 동작 변경 없이 정리
           ↓ 테스트 여전히 GREEN 확인
           ↓ 다음 사이클
```

## Hook 자동 정렬

`tdd-guard.sh` (PreToolUse Write|Edit|MultiEdit) 가 lib/, src/lib, src/utils, app/api/services/domain/ 경로 차단. 테스트 파일 co-located OR tests/ 디렉토리에 먼저 존재해야 통과.

## 규칙 (예외 없음)

- 테스트 전에 코드 작성했으면 → **삭제하고 처음부터.**
- "참고용" / "단순한 코드니까" → 합리화. **모두 거부.**
- RED 확인 필수 (테스트 실행해서 의도한 대로 실패하는지)
- 한 번에 한 사이클. cycle N+1 시작 = 이전 cycle GREEN 확인 후만.

## Red Flags — 즉시 멈출 것

| 생각 | 현실 |
|---|---|
| "이번 한 번만 건너뛰자" | 합리화. 멈출 것. |
| "테스트는 나중에 추가" | 사후 테스트는 TDD 아님 |
| "이건 너무 단순" | 단순한 것도 깨진다 |
| "리팩토링은 테스트 없이도 OK" | 리팩토링도 TDD 적용 |

## 예외 (사용자 명시 승인 후)

- 일회용 프로토타입 / throwaway script
- 자동 생성 코드 (migrations, generated clients)
- 설정 파일 / 타입 정의 / 정적 자산

## 작업 순서

1. 요구사항 한 문장
2. 테스트 파일 먼저 작성 — 실패 케이스 1개 이상
3. RED 확인 (실행)
4. GREEN 작성 — 통과할 최소한만
5. REFACTOR — 중복 제거, 네이밍 개선
6. 다음 사이클
