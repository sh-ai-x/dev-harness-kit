---
name: build-methodology
category: build
description: methodology router. TDD/SDD/DDD/BDD/FDD custom 선택 (MUST-48). lib/methodology/<name>.py 자동 dispatch.
when_to_use: |
  - User runs /dev-kit:config "methodology" question
  - Auto-invoked by build-engine per step
allowed-tools: Read Write Bash
disallowed-tools: WebFetch Agent
model: haiku
---

# build-methodology — Verification Artifact Router

## Iron Law
**L1 = "no prod code without verification artifact"** (artifact type varies by methodology).

## Adapter 인터페이스 (`lib/methodology/abc.py`)

```python
class Methodology(ABC):
    name: str  # tdd | sdd | ddd | bdd | fdd | custom
    @abstractmethod
    def pre_check(self, worktree: Path, step: Dict) -> Dict: ...
    @abstractmethod
    def verification_command(self, worktree: Path, step: Dict) -> List[str]: ...
    @abstractmethod
    def cycle_steps(self) -> List[str]: ...
    @abstractmethod
    def report_status(self, worktree: Path, step: Dict) -> Dict: ...
```

## 5 Adapter (default 가능)

| Methodology | Artifact | Verification | Cycle | Hook |
|---|---|---|---|---|
| **TDD** (default) | failing unit test | `pytest` / `vitest` | Red→Green→Refactor | `tdd-guard` |
| **SDD** | OpenAPI / proto spec | contract test (Pact) | Spec→Impl→Contract | `spec-guard` |
| **DDD** | Aggregate / Domain | domain test (ubiquitous lang) | Model→Test→Refine | `domain-guard` |
| **BDD** | Gherkin feature | step def + scenario | Given/When/Then | `bdd-guard` |
| **FDD** | Feature spec | feature flag / smoke | Plan→Design→Build | `feature-guard` |

## Selector

`/dev-kit:config` 의 multiSelect "methodology" answer → `lib/methodology.json` 자동 기록.

## Hook 정렬

`.dev-kit/.active-hooks.json`의 stage-cell `tdd-guard` value:
- `true` (모든 hook ON)
- `false` (전체 OFF, 사용자가 opt-out)
- methodology adapter 별도 register 시 자동 매핑

## 신규 방법론 추가

`lib/methodology/<name>.py` 1 파일 + `lib/test_methodology.py` 회귀 1개로 끝 (YAGNI). ADR 필요 + migration 없음.
