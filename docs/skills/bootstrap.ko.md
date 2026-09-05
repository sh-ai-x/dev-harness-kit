> [← 스킬 인덱스](README.ko.md) · [프로젝트 README](../../README.ko.md)

# `bootstrap`

**카테고리:** `bootstrap` · **알파:** `state` · **호출:** `/dev-kit:bootstrap` (사람이 호출)

`bootstrap`은 새 dev-harness-kit 프로젝트의 정식 원샷 설정이다. 무조건적인
bootstrap 파이프라인(sanity, codebase-map, hook-matrix, write-claude-md)을
실행한 다음 운영자에게 CI 템플릿 설치 여부를 묻는다(기본은 N; 자동 수락은
`--yes`, 거절은 `--skip-ci`). 운영자가 Y로 답하면 디스크의 최종 상태는
레거시 `/dev-kit:bootstrap-full` 슬래시와 일치 — 세 개의 SSOT 파일 + 15개
CI 워크플로 템플릿 + pre-push 훅 + `.dev-kit/ci-config.json` 마커.

## 사용 시점

- 사용자가 새 프로젝트에서 처음으로 `/dev-kit:bootstrap`을 실행.
- 사용자가 `CLAUDE.md` / `active-hooks.json`을 새로 고치길 원함.

## 작동 방식

Bootstrap은 무조건적 파이프라인을 그 다음 옵션으로 ci-setup을 7단계
오케스트레이션(4 자동 단계, 1 프롬프트, 1 ci-setup, 1 종료)에서 실행:

1. **Sanity** (결정론적, LLM 없음) — 7-검사 감사: 매니페스트 존재
   (`package.json`/`pyproject.toml`), `.git/` 건강, `docs/` 템플릿
   플레이스홀더, 금지-어구 스캔(slop-detector SSOT 정규식), 시크릿
   스캔(자격 증명 패턴 — 이것이 유일한 **CRITICAL FAIL** 검사, 나머지는
   모두 WARN), 훅-우회 감지(`DEV_KIT_HOOK_OFF=*` env), 방법론 lockfile
   일관성 검사(`lib/methodology.json`). 결과는 PASS(모두 통과), WARN(1-3
   경고, 통과 허용), 또는 FAIL(4+ 경고 또는 1+ 중요 — Plan 진입 차단).
   출력은 stdout으로만; 파일(`.dev-kit/sanity-report.md`)은 `--persist-audit`와
   함께일 때만 작성.
2. **Codebase map** (결정론적, LLM 없음) — CLAUDE.md는 슬림 포인터;
   codebase 맵은 `docs/CODEBASE-MAP.md`를 통해 지연 로드(`--full-claude-md`와
   함께일 때만 작성). 전체 맵(Tree via `os.walk` depth 4, Manifest, Deps
   top-10, Conventions)은 `lib/write_project_md.py:render_codebase_map_doc`가
   렌더링. CLAUDE.md의 참조 블록은 항상 이 파일을 가리킨다.
3. **Hook matrix init** — `.dev-kit/.active-hooks.json`을 단계별(bootstrap/
   plan/design/build/review/security/ship) 어떤 훅(`tdd-guard`, `bash-guard`,
   `secret-scan`, `slop-detector`, `stop-verify`)이 활성인지의 단일 진실
   공급원으로 작성. `hooks/hooks.json`은 matrix reader만 등록; 모든 활성
   결정이 JSON에 산다.
4. **write-claude-md** — `lib/write_project_md.py`가 `CLAUDE.md`와
   `AGENTS.md`(CLIs가 AGENTS.md를 읽는 경우를 위한 CLAUDE.md로의 1줄
   포인터)를 §1-§5 섹션으로 원자적으로 작성.
5. **ci-setup prompt** — 무조건적인 bootstrap 세트가 착륙한 후 스킬은
   `Also install CI templates (ci-setup)? [y/N]`을 묻는다. 기본은 N.
   프롬프트를 건너뛰려면 `--yes`(Y 가정), 건너뛰고 불가-기능 리스트를
   출력하려면 `--skip-ci`.
6. **ci-setup** (Y에 한해) — `lib/ci_setup.py:install_ci_config(force=True)`에
   위임(Phase 1.5 사전 비행 프로브 + 15 EXPECTED_PATHS + `.dev-kit/ci-config.json`
   마커 + Phase 1.7 lint + Phase 4 사후-설치 체크리스트). `--skip-verify`와
   함께면 Phase 3 verify가 건너뜀. `--force` 없이는 재실행이 no-op.
7. **Exit** — 정식 plan -> build 루프를 시작하려면 `/dev-kit:build <first-feature>`,
   사후-설치 드리프트 검증을 하려면 `/dev-kit:ci-doctor`로의 포인터.

숨겨진 플래그(보이는 옵션 프롬프트 없음 — MUST-NOT-13): `--skip-sanity`,
`--skip-map`, `--slim|--full`, `--team`, `--strict`, `--persist-audit`,
`--skip-ci`(ci-setup 건너뛰기, `n` 응답과 등가), `--yes`(프롬프트 건너뛰기,
`Y` 기본), `--force`(ci-setup 중 기존 CI 템플릿 덮어쓰기), `--skip-verify`
(ci-setup Phase 3 verify 건너뛰기). `--strict`와 함께면 모든 훅이 `exit 0`
대신 `exit 2`로 기본.

## 사용법

```bash
/dev-kit:bootstrap [--skip-sanity] [--skip-map] [--slim|--full] [--team] [--strict] [--persist-audit]
```

| 플래그 | 효과 |
|---|---|
| *(0-인자)* | 전체 파이프라인을 실행하고 ci-setup(기본 N) → git-defaults(기본 Y)를 묻는다. 레거시 `/dev-kit:bootstrap-full` 종단 상태를 원하면 `--yes`, ci-setup을 건너뛰고 불가-기능 리스트를 출력하려면 `--skip-ci`. |
| `--skip-sanity` | sanity 서브-단계를 건너뜀. |
| `--skip-map` | codebase-map 서브-단계를 건너뜀. |
| `--slim` / `--full` | CLAUDE.md 상세도 모드 제어. |
| `--full-claude-md` | 지연-로딩 인덱스 대신 전체 4-섹션 codebase 맵을 `docs/CODEBASE-MAP.md`에 작성. |
| `--team` | 팀-모드 변형 (숨겨진 플래그). |
| `--strict` | 모든 훅이 기본 `exit 0` 대신 `exit 2`. |
| `--persist-audit` | `.dev-kit/sanity-report.md`도 작성. |
| `--skip-ci` | ci-setup을 건너뜀. 불가-기능 리스트를 출력. |
| `--yes` | ci-setup 프롬프트를 건너뜀; Y 가정. |
| `--force` | ci-setup 중 기존 CI 템플릿을 덮어씀. |
| `--skip-verify` | ci-setup Phase 3 verify를 건너뜀. |

## 출력

무조건적인 bootstrap 세트에서 새 저장소에 대한 세 파일: `CLAUDE.md`,
`AGENTS.md`, `.dev-kit/.active-hooks.json`. `--persist-audit`와 함께면
`.dev-kit/sanity-report.md` 추가. `--full-claude-md`와 함께면 `docs/CODEBASE-MAP.md`
추가. ci-setup 프롬프트에 Y 또는 `--yes`면 추가: `.github/workflows/`에
15개 CI 워크플로 템플릿 + `.dev-kit/ci-config.json` 마커 + pre-push 훅.

## 관련

- [ci-setup](ci-setup.ko.md) — 독립 절반 (`--skip-ci` 흐름에서 사용; 또한
  `/dev-kit:ci-setup --force`로 직접 도달 가능).
- `/dev-kit:plan` — 옵트인 아이디어 → PRD.md 합성; 기본 다음 단계가 아님.

---
*출처: [`skills/bootstrap/SKILL.md`](../../skills/bootstrap/SKILL.md)*
