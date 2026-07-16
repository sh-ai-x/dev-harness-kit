# dev-harness-kit

> AI 네이티브 통합 하네스 플러그인 — 타입드 서브에이전트 위임, Eval-Repair 루프,
> Human-on-the-Loop 감독으로 Plan → Build → Review → Ship 전 과정을 커버합니다.

[![License](https://img.shields.io/badge/license-MIT-blue)](LICENSE)

**언어:** [English](README.md) · 한국어

---

## 목차

- [개요](#개요)
- [설치](#설치)
- [플러그인 최신 유지](#플러그인-최신-유지)
- [소비자 최초 설정](#소비자-최초-설정)
- [명령어 레퍼런스](#명령어-레퍼런스)
- [핵심 개념](#핵심-개념)
  - [Worktree 규칙](#worktree-규칙)
  - [대상별 스킬 구분](#대상별-스킬-구분)
- [도구](#도구)
  - [Loghooks](#loghooks-dev-kitlog)
  - [토큰 효율 분석기](#토큰-효율-분석기)
  - [Cost gate](#cost-gate)
- [소비자 CI 설치](#소비자-ci-설치)
- [Codex CLI 호환](#codex-cli-호환)
- [에이전트 행동 Eval](#에이전트-행동-eval)
- [저장소 구조](#저장소-구조)
- [설계 원칙](#설계-원칙)
- [기여](#기여)

---

## 개요

`dev-harness-kit`는 단일 Claude Code / Codex 플러그인(`dev-kit`)으로 배포되며 전체
개발 루프를 커버합니다. 주요 기능:

- **Plan + Design 단일 명령** — `/dev-kit:plan`이 5-게이트 루프
  (`frame → validate → non-goals → decompose → emit`)로 `PRD.md` +
  `phases/<name>/{index.json, step<N>.md}`를 자동 생성합니다. 고정 질문지 대신
  정량화된 모호도·가치 점수로 구동됩니다.
- **스텝별 서브에이전트 Build** — `/dev-kit:build`가 각 스텝을 TDD + 자동 수정
  루프가 내장된 서브에이전트에 위임합니다.
- **병렬 Review / Security** — `/dev-kit:review`(정확성 + 보안 + 아키텍처)와
  `/dev-kit:security`(OWASP A01–A10)가 서브에이전트로 팬아웃되고, 검증 패스가
  거짓 양성을 걸러냅니다.
- **에이전트 행동 Eval** — `/dev-kit:eval`이 기록된 트랜스크립트를 재생하고
  차원별 루브릭 + 코드 위생 체크리스트로 판정합니다.
- **Eval-Repair 루프** — 자동 점검 → 전문 수정기 → 최종 Human Review.
- **Human-on-the-Loop** — 하네스가 자동 진행하고, 최종 승인은 사용자가 합니다.
- **Worktree 강제** — 훅이 메인 체크아웃에서의 편집을 차단하고, 모든 새 작업을
  전용 worktree + 브랜치로 유도합니다.
- **소비자 설치** — `/dev-kit:ci-setup`이 본 저장소와 다운스트림 소비자 저장소
  양쪽에서 동작하는 self-aware CI 워크플로 세트를 배포합니다.
- **비용 가시성** — 옵트인 세션 loghooks가 공급하는 토큰 효율 대시보드와 실시간
  cost gate.

---

## 설치

Claude Code CLI가 필요합니다. `claude plugin …` 명령 실행 전
[Node 호환성](#node-호환성)을 먼저 확인하세요.

```bash
# 마켓플레이스 설치 (권장)
claude plugin marketplace add sh-ai-x/dev-harness-kit
claude plugin install dev-kit

# …또는 로컬 체크아웃에서
git clone https://github.com/sh-ai-x/dev-harness-kit
claude plugin marketplace add ./dev-harness-kit
claude plugin install dev-kit

# 매 세션 시작 시
/reload-plugins
```

설치는 `.claude-plugin/plugin.json`의 `version` 필드를 고정하며, 로드되는 사본은
버전명 캐시 디렉터리(`~/.claude/plugins/cache/dev-kit/dev-kit/<version>/`)에
위치합니다. 마켓플레이스 소스는 `main` 브랜치를 추적하므로
(`marketplace.json` → `source.ref: main`) 머지마다 새 버전이 제공됩니다 —
[플러그인 최신 유지](#플러그인-최신-유지) 참조.

### 라이브 소스 개발 (기여자 권장)

마켓플레이스 설치는 배포된 한 버전을 고정합니다. 이 저장소를 개발할 때는 재설치
없이 편집이 즉시 반영되도록 로컬 체크아웃을 가리키세요:

```bash
claude --plugin-dir /path/to/dev-harness-kit
```

`~/.zshrc`(또는 `~/.bashrc`)에 셸 별칭을 두면 편리합니다:

```bash
alias claude-dev='claude --plugin-dir /path/to/dev-harness-kit'

claude-dev   # 프로젝트 디렉터리에서: 로컬 편집을 로드, 재빌드 불필요
claude       # 마켓플레이스 고정 설치로 폴백
```

둘 다 사용 가능할 때 해당 세션에서는 로컬 `--plugin-dir` 사본이 우선합니다.

> **`~/.claude/skills/dev-kit`를 저장소로 심링크하지 마세요.** 마켓플레이스 설치와
> 동일한 `name`을 가진 skills-dir 플러그인이 충돌하며, 로더가 두 번째 사본을
> 거부합니다. 무플래그 라이브 소스 설치는 위 별칭을 사용하세요.

### Node 호환성

번들된 Claude Code CLI는 **Node ≥ 25**에서 크래시합니다
(`TypeError: Cannot read properties of undefined (reading 'prototype')`,
`cli.js:384`). 모든 `claude plugin …` 명령은 **Node 22**에서 실행하세요:

```bash
nvm install 22 && nvm use 22
```

`--plugin-dir` 플래그는 영향받지 않습니다 — 실패하는 CLI 경로를 완전히
우회합니다.

---

## 플러그인 최신 유지

마켓플레이스 설치는 `~/.claude/plugins/cache/dev-kit/dev-kit/<version>/`의 캐시
사본을 로드합니다. PR이 `main`에 머지되면 새로고침 전까지 캐시는 stale 상태입니다.

**새로고침이 필요한 경우:**

- PR이 `main`에 머지되어 현재 세션에서 새 동작을 원할 때.
- `/dev-kit:*` 출력이 최신 소스와 맞지 않을 때.
- 소비자 저장소의 `/dev-kit:ci-setup`이 파일 누락을 보고할 때(예:
  `scripts/branch-policy.sh: No such file or directory`) — 캐시가 stale.

### Claude Code

`dev-kit` 마켓플레이스 엔트리는 `main`을 가리키므로, 머지마다 마켓플레이스
카탈로그가 핀 버전을 자동 범프합니다. 가장 깔끔한 경로는:

```bash
# 권장: 마켓플레이스에서 최신 핀 버전을 가져옵니다.
# 모든 셸에서 동작하며, Claude Code 세션 내부에서도 업데이터 경로가
# 위의 CLI 버그를 우회합니다(아래 "Node 호환성" 참조).
claude plugin update dev-kit
```

이마저 실패하면(주로 Claude Code 세션 내부에서 번들 CLI가 Node `TypeError`를
던지는 경우), 메인테넌스 스크립트가 `git pull` + `rsync`로 같은 작업을
수행합니다:

```bash
# 최후 수단: 마켓플레이스 클론을 pull하고 버전 캐시로 rsync.
bin/devkit-refresh.sh
bin/devkit-refresh.sh --dry-run    # 변경 사항 미리 보기
```

스크립트도 사용할 수 없으면 직접 캐시를 새로고침할 수 있습니다:

```bash
cd ~/.claude/plugins/marketplaces/dev-kit && git pull origin main --ff-only
rsync -a --delete --exclude=.git \
  ~/.claude/plugins/marketplaces/dev-kit/ \
  ~/.claude/plugins/cache/dev-kit/dev-kit/<version>/
```

> **`devkit-refresh.sh`가 존재하는 이유:** `claude plugin install --force`와
> `claude plugin update`는 Claude Code 세션 *내부*에서 호출되면 위의 Node
> `TypeError`를 던지는 동일한 CLI 경로를 탑니다. 이 스크립트는 평범한
> `git pull` + `rsync`로 같은 작업을 수행하므로 모든 환경에서 동작합니다. 캐시
> 버전은 `plugin.json`에서 읽고(필드가 없으면 마켓플레이스 클론의 short SHA로
> 폴백), 배포된 훅/템플릿 스크립트의 실행 비트를 보존합니다.

### Codex

```bash
bash skills/codex-cache-update/scripts/update.sh
bash skills/codex-cache-update/scripts/update.sh --dry-run   # 확인만
```

Codex 마켓플레이스 체크아웃을 업그레이드하고 일치하는 버전 캐시를 동기화합니다 —
마켓플레이스 명령이 이미 최신이라고 보고하는 경우에도 마찬가지입니다. 이후
마켓플레이스 경로, 매니페스트 버전, 캐시 경로, 마지막 `cache synchronized` 줄을
출력합니다. 비기본 설치 경로는 오버라이드하세요:

```bash
CODEX_MARKETPLACE_DIR="$HOME/.codex/.tmp/marketplaces/dev-kit" \
CODEX_CACHE_ROOT="$HOME/.codex/plugins/cache/dev-kit/dev-kit" \
bash skills/codex-cache-update/scripts/update.sh
```

새로고침 후에는 클라이언트를 재시작하거나 지원되는 곳에서 `/reload-plugins`를
실행하세요.

---

## 소비자 최초 설정

대부분의 사용자는 소비자입니다. "새 저장소가 있다" 시나리오의 엔드투엔드 흐름:

```bash
# 1. 생성 + 클론
gh repo create myorg/myrepo --private --clone && cd myrepo

# 2. 플러그인 설치
claude plugin marketplace add sh-ai-x/dev-harness-kit
claude plugin install dev-kit
# (라이브 소스: claude --plugin-dir /path/to/dev-harness-kit)

# 3. 원샷 설정: CLAUDE.md + AGENTS.md + active-hooks.json + CI 템플릿
/dev-kit:bootstrap-full
#    = /dev-kit:bootstrap 다음 /dev-kit:ci-setup --force.
#    한쪽만 원하면 개별 실행하세요.

# 4. 최초 커밋 + 푸시
git add -A && git commit -m "chore: bootstrap dev-kit"
git push -u origin main
```

**최초 설치에는 `--force`를 사용하세요.** 새 저장소에서는 기본 설치와 결과가
동일하지만(어느 쪽이든 모든 파일이 복사됨), `--force`는 이전 시도의 부분 설치와
stale 플러그인 캐시에 대해 견고합니다. 이후 업스트림 템플릿 변경을 가져오려면
`--force`로 재실행하세요 — 새로고침 vs 최초 설치 의미는
[소비자 CI 설치](#소비자-ci-설치) 참조.

일반적인 다음 단계: `/dev-kit:plan`으로 PRD와 phases를 생성합니다.

---

## 명령어 레퍼런스

`/dev-kit:<skill>`로 호출합니다. 아래 목록은 사용자용 진입점을 워크플로 단계별로
묶은 것입니다. `SKILL.md`에 `user-invocable: true`인 스킬만 슬래시 자동완성에
나타납니다. 현재 정확한 목록은 해당 frontmatter를 확인하거나 자동완성을
사용하세요 — [대상별 스킬 구분](#대상별-스킬-구분) 참조.

**설정**

| 명령 | 용도 |
|---|---|
| `/dev-kit:bootstrap` | 최초 진입 — `CLAUDE.md` 생성 |
| `/dev-kit:bootstrap-full` | 원샷 bootstrap + ci-setup (신규 프로젝트 기본) |
| `/dev-kit:ci-setup` | CI 템플릿 설치 (워크플로 + 훅 + 스크립트 + worktree 파일) |
| `/dev-kit:ci-doctor` | CI 준비 상태 읽기 전용 PASS/FAIL 감사 |
| `/dev-kit:log setup\|on\|off\|status` | 프로젝트별 세션 loghooks 토글 |
| `/dev-kit:config` | 스킬 / MCP / 훅 / 방법론 선택기 |

**Plan → Build**

| 명령 | 용도 |
|---|---|
| `/dev-kit:plan` | PRD + phases (Plan + Design 통합) |
| `/dev-kit:build` | 스텝별 서브에이전트 실행 |
| `/dev-kit:adapt` | 빌드 중 계획/스펙 수정 |
| `/dev-kit:feat-add` | TDD로 기능 추가 |
| `/dev-kit:feat-fix` | 지정된 단일 기능의 재현 우선 수정 |
| `/dev-kit:feat-revise` | TDD로 기능 개정 |
| `/dev-kit:feat-remove` | 기능 제거 (콜그래프 스윕 + 삭제 리포트) |

**Review → Ship**

| 명령 | 용도 |
|---|---|
| `/dev-kit:review` | 3차원 리뷰 (정확성 + 보안 + 아키텍처) |
| `/dev-kit:security` | OWASP A01–A10 감사 |
| `/dev-kit:audit` | 배치 slop + 시크릿 감사 |
| `/dev-kit:inspect` | 8차원 코드 건강 감사 (읽기 전용) |
| `/dev-kit:refactor` | 3단계 리팩터: inspect → cleanup → review |
| `/dev-kit:prune` | 3단계 삭제 스윕: inspect → delete → review |
| `/dev-kit:babysit-pr` | PR 베이비시터 루프 (CI 폴링, 수정, 반복) |
| `/dev-kit:ship` | 릴리스 태그 |
| `/dev-kit:bump [major\|minor\|patch]` | 명시적 버전 범프 + 푸시 |

**Eval / 비용 / 리포팅**

| 명령 | 용도 |
|---|---|
| `/dev-kit:eval` | 에이전트 행동 eval (review/security/plan + 코드 위생) |
| `/dev-kit:repair approve\|reject\|defer <asset>` | Eval-Repair Human Review |
| `/dev-kit:report` | eval + inspect 리포트 HTML 뷰어 |
| `/dev-kit:token-analyzer` | 세션 로그 기반 토큰 효율 대시보드 |
| `/dev-kit:cost-gate` | 실시간 cost gate (지출 + 임계값 + 커밋 푸터) |

**문서 / 단축**

| 명령 | 용도 |
|---|---|
| `/dev-kit:docs-maintenance` | stale 문서 감사, README 새로고침, volatile 사실 제거 |
| `/dev-kit:tdd-fast` | Bootstrap + Plan 생략 → 곧바로 Build (핫픽스) |
| `/dev-kit:shortcut-quick-fix` | 온디맨드 verify + debug |

---

## 핵심 개념

### Worktree 규칙

정본 규칙은 `rules/git-workflow.md`입니다. Claude Code는 `.claude/rules` 호환
심링크로, Codex는 `AGENTS.md`를 통해 같은 파일을 발견합니다. 요구사항은
하드합니다:

> **모든 작업 = 새 worktree + 클라이언트 핸드오프 + 새 브랜치.** Claude Code는
> worktree에서 새 세션을 열고, Codex는 거기서 서브에이전트를 스폰합니다. 이전
> 작업 브랜치나 메인 체크아웃에서는 편집 금지.

세 개의 훅이 강제합니다:

- `worktree-guard.sh` — 메인 체크아웃의 모든 Edit/Write 하드 차단.
- `task-detector.sh` — 새 작업 프롬프트("implement X" 등)에 조기 경고.
- `session-start-check.sh` — 세션 시작 시 부드러운 알림.

정본 worktree 경로는 저장소 루트의 클라이언트 중립 `.worktrees/<slug>/`이므로,
Claude Code와 Codex가 한 브랜치에 대해 같은 체크아웃을 엽니다. 레거시
`.claude/worktrees/` · `.codex/worktrees/` 체크아웃은 로그 분석용으로 여전히
발견되지만, 새 자동 컷은 `.worktrees/`를 사용합니다. 이 worktree 규칙 파일들은
`templates/ci/`를 통해 소비자 저장소에도 배포됩니다.

### 대상별 스킬 구분

각 `SKILL.md`는 `user-invocable` frontmatter 플래그를 가집니다:

- **`user-invocable: true`**(또는 미설정) — `/dev-kit:` 자동완성에 노출. *사용자*가
  입력합니다.
- **`user-invocable: false`** — 숨김. 부모 스킬 실행 시 *Claude*가 하위 단계로
  자동 호출합니다.

스킬명이 자동완성되지 않으면 내부 하위 스킬입니다 — 대신 사용자용 부모를
입력하세요(예: `/dev-kit:build-refactor`가 아니라 `/dev-kit:refactor`). 멘탈
모델: 사용자용 스킬은 동사(*무엇*), 내부 스킬은 기계장치(*어떻게*). 이 README는
변하는 스킬 목록을 복제하지 않습니다 — 실제 목록은 `skills/` frontmatter나
자동완성을 확인하세요.

---

## 도구

### Loghooks (`/dev-kit:log`)

독립 저장소 [`loghooks`](https://github.com/sh-ai-x/loghooks)(Claude Code
`Stop` + `SessionEnd`, Codex 대응)를 프로젝트별 원커맨드 온/오프 토글로
감쌉니다.

```bash
/dev-kit:log setup   # tools/save_log.py 복사 + logs/{claude-code,codex}/ 스캐폴드
/dev-kit:log on      # .claude/settings.json + .codex/hooks.json에 훅 병합
/dev-kit:log status  # managed=N captured=N
/dev-kit:log off     # 센티넬 태그 항목만 제거; 스캐폴드는 유지
```

설치되는 모든 항목은 `_loghooks_managed=true`를 지니며, `off`는 그것만 제거하므로
기존 사용자 훅은 보존됩니다. 캡처된 트랜스크립트는
`logs/<tool>/<branch>/<sid>.jsonl`(`gitBranch`별 그룹)에 저장되고 gitignore
됩니다. [`logs/README.md`](logs/README.md)와 `skills/log/SKILL.md` 참조.

### 토큰 효율 분석기

표준 라이브러리만 사용하는 Python CLI(`tools/token_efficiency_analyzer.py`)로,
loghooks가 캡처한 `logs/{claude-code,codex}/**/*.jsonl` 트랜스크립트를 소비해
자체 완결형 HTML 대시보드 하나를 출력합니다 — 의존성·JavaScript·네트워크 없음.
사용자용 진입점은 `/dev-kit:token-analyzer` 스킬이며, CLI는 CI용으로 직접 호출도
가능합니다:

```bash
python3 tools/token_efficiency_analyzer.py --repo "my-project" --days 30
open token-dashboard-my-project-30d.html
```

대시보드는 최근 N일간 저장소별로 세 가지 질문에 답합니다:

1. **지출은 어디로 가는가?** 저장소별·도구별(`Read` 과다 플래그)·세션별 비용
   비중.
2. **각 세션의 효율은?** 4개 차원의 0–100 점수.
3. **무엇을 고쳐야 하는가?** 6개 안티패턴 경고 + USD 절감 추정.

**주요 플래그**

| 플래그 | 기본값 | 용도 |
|---|---|---|
| `--repo <name>` | (필수) | 각 세션의 `Path(cwd).name`(저장소 디렉터리 basename)과 매칭 |
| `--days <n>` | `30` | 조회 기간; 오래된 세션은 제외 |
| `--logs-dir <path>` | `./logs` | `claude-code/`·`codex/` 하위 디렉터리 루트 |
| `--out <path>` | `token-dashboard-<repo>-<days>d.html` | 출력 HTML 경로 |

**점수 차원**(100점 가중)

| 차원 | 가중치 | 페널티 대상 |
|---|---:|---|
| Cache Utilization | 0.35 | 전체 프롬프트를 재프라이밍하는 프리픽스 불일치 |
| Output Density | 0.25 | 많이 읽고 적게 산출 |
| Read Redundancy | 0.20 | 같은 파일 재독(카토그래피 부재) |
| Tool Economy | 0.20 | 적은 산출에 많은 도구 호출 |

**경고 트리거**

| 코드 | 조건 |
|---|---|
| `CACHE_HIT_LOW` | `cache_hit_ratio < 50%` |
| `READ_HEAVY` | `Read`가 전체 도구 비용의 40% 이상 |
| `HEAVY_CONTEXT` | 한 세션에서 `total_input > 500K` 토큰 |
| `MODEL_OVERSPEC` | Opus + density 점수 < 20 |
| `WRITE_NOT_REUSED` | `cache_write > 50K` 그리고 `cache_read < 2 × cache_write` |
| `REPEATED_USER_MSG` | 동일 사용자 메시지 텍스트가 2회 이상 |

모델별 가격은 스크립트에 내장되어 있으며(`opus`/`sonnet`/`haiku`, 모델 id
부분 문자열 매칭, 미인식 id는 Sonnet으로 폴백), 계약 요율은 파일 상단의
`PRICING`을 오버라이드하세요. 절감액은 보수적 회수 모델입니다 — 캐시 미스
페널티 + 중복 읽기 낭비만 계산하며 전체 청구액이 아닙니다. 도구별 비용 열은
추정 휴리스틱(`n_calls × 2K 토큰 × input 가격`)입니다 — Anthropic 청구는
도구별 지출을 분해하지 않기 때문입니다.

**스킬이 아니라 도구인 이유:** 루프에 LLM 호출 없이 로컬 파일을 변환하므로,
스킬로 감싸면 불필요한 모델 왕복이 강제됩니다. 입력을 *생성*하는 loghooks는
스킬로 남고, 출력을 *소비*하는 분석기는 스크립트입니다.

합성 픽스처로 검증:

```bash
python3 fixtures/make_fixture.py
python3 tools/token_efficiency_analyzer.py --repo "fixture-repo" --days 30 \
  --logs-dir fixtures/logs --out fixtures/out/dashboard.html
```

### Cost gate

사후 토큰 대시보드와 구별되는 **읽기 전용** 비용 레이어입니다. cost-gate는 실행
중인 원장을 온디맨드로 출력하고 PR 애그리게이터가 필요로 하는 트레일러 블록을
방출합니다. 분석기는 과거 세션을 재생합니다. 상태는
`<cwd>/.dev-kit/.cost-gate/state.json`에 있습니다. **게이트는 관측만 하며 —
도구 호출을 절대 차단하지 않습니다.**

| 레이어 | 트리거 | 기본 임계값 | 동작 |
|---|---|---:|---|
| 세션 경고 | `/dev-kit:cost-gate` | `$5.00` | 한 화면 `ok`/`warn` 상태; 거부 없음 |
| PR 플래그 | PR opened/synchronize/reopened | `$20.00` | `cost-flag` 라벨 + 단일 코멘트 upsert |

`DEV_KIT_COST_WARN_USD`와 `DEV_KIT_PR_COST_FLAG_USD`로 오버라이드하세요. PR
애그리게이터(`.github/workflows/cost-flag.yml`)가 읽는 커밋 트레일러 방출:

```bash
git commit -m "feat: thing" -m "$(python3 tools/cost_gate_status.py --footer)"
```

`lib/cost_gate.py`와 `tools/cost_gate_status.py`는 가격 테이블·상태 파일·
트랜스크립트 스캐너를 분석기와 완전히 독립적으로 유지합니다 — 회귀 테스트가
교차 import 없음을 단언합니다.

---

## 소비자 CI 설치

`/dev-kit:ci-setup`은 dev-kit을 *다른* 저장소에서 동작하게 하는 것입니다. 다음을
복사합니다:

- GitHub Actions 워크플로 (ci, auto-fix-pr, review)
- 스크립트 (validate, test, branch-policy, ci-local)
- pre-push 훅
- worktree 규칙 파일 (훅, lib, 규칙, 테스트)

배포되는 `review.yml`은 **self-aware**합니다: 체크아웃이 dev-kit 플러그인 자체인지
(self-install) 평범한 소비자 저장소인지(public 소스에서 클론) 감지하므로, 하나의
워크플로 파일이 양쪽에서 동작합니다.

**CI 리뷰 프로바이더 전환:** `bin/set-provider.sh <provider>`를 실행하세요. git
추적되는 `.github/ci-review-provider.txt`를 편집하고 diff를 출력한 뒤, 커밋 +
푸시는 직접 합니다. `.env`는 **참조하지 않습니다**(GitHub 호스팅 러너는 어차피
읽을 수 없음). 각 프로바이더는 대응하는 저장소 시크릿(`MINIMAX_API_KEY`,
`ANTHROPIC_API_KEY`, `DEEPSEEK_API_KEY`)을 `gh secret set`으로 먼저 푸시해야
합니다. `.github/workflows/review.yml`을 직접 편집하는 PR은
`claude-code-action`의 안티탬퍼링 가드에 의해 스킵됩니다 — 정상이며, 해당 PR이
머지되면 해소됩니다.

### `--force`: 사용할 때와 하지 말 때

`ci-setup`은 **기본적으로 idempotent**합니다 — 마커 `.dev-kit/ci-config.json`이
설치 시각 + 콘텐츠 해시를 기록하므로, 일치하는 재실행은 no-op입니다. `--force`는
예상 파일을 무조건 덮어씁니다.

**`--force` 사용:** 최초 설치, 새로 추가/수정된 템플릿을 가져올 때, 또는
stale/부분 설치가 의심될 때(마커는 있으나 파일이 누락·drift). **`--force` 지양:**
업스트림 변경이 없는 클린 재실행, 또는 설치 파일을 직접 편집한 경우(로컬
커스터마이징을 덮어씀 — diff를 먼저 검토).

```bash
bin/devkit-refresh.sh                         # 1. 캐시 새로고침 → 최신 템플릿
cd /path/to/consumer-repo
/dev-kit:ci-setup --force                      # 2. 설치
git diff .github/ scripts/ .githooks/ hooks/ .claude/ tests/   # 3. diff 검토
/dev-kit:ci-doctor                             # 4. 준비 상태 검증 (PASS까지 반복)
git add -A && git commit -m "chore(ci): refresh dev-kit templates"   # 5. 커밋
```

---

## Codex CLI 호환

Codex CLI의 플러그인 포맷([openai/plugins](https://github.com/openai/plugins))은
`"skills"` 필드가 skills 디렉터리를, `"hooks"` 필드가 번들된
`.codex-plugin/hooks/hooks.json`을 가리키는 `.codex-plugin/plugin.json`
매니페스트입니다. 번들 사본은 정본 `hooks/hooks.json`을 미러링하며(Codex는 플러그인
훅 파일이 플러그인 루트 안에 있어야 함), 회귀 테스트가 두 이벤트 인벤토리를
동기화합니다. Codex 명령은 `${PLUGIN_ROOT}`를, Claude Code는
`${CLAUDE_PLUGIN_ROOT}`를 사용하며 Claude Code는 `hooks/hooks.json`을 직접
계속 로드합니다.

플러그인 활성화 후 Codex에서 `/hooks`로 훅을 검토·신뢰하세요 — 새롭거나 변경된
비관리 훅은 신뢰 전까지 스킵됩니다. 로컬 상태 확인:

```bash
python3 bin/dev-kit-hooks-status.py          # 사람이 읽는 형식
python3 bin/dev-kit-hooks-status.py --json    # 기계가 읽는 형식
```

리포트는 Claude Code 등록, Codex 등록 + 신뢰, `.dev-kit/.active-hooks.json`
매트릭스, Git의 별도 pre-push 훅을 구분합니다. Git 강제는 명시적으로 옵트인한
뒤에만 활성화됩니다:

```bash
git config core.hooksPath .githooks
```

### 훅 인벤토리

| 훅 | 이벤트 | 용도 | 모드 |
|---|---|---|---|
| `tdd-guard.sh` | PreToolUse (Write\|Edit\|MultiEdit) | TDD 테스트 우선 강제 | advisory / `--strict` |
| `bash-guard.sh` | PreToolUse (Bash) | 파괴적 명령 차단 | advisory / `--strict` |
| `git-guard.sh` | PreToolUse (Bash) | 브랜치 전략 강제 | hard-block |
| `worktree-guard.sh` | PreToolUse (Write\|Edit\|MultiEdit) | 메인 체크아웃 편집 차단 | hard-block |
| `task-detector.sh` | UserPromptSubmit | 새 작업을 worktree로 유도 | advisory |
| `session-start-check.sh` | SessionStart | 세션 시작 시 worktree 규칙 알림 | advisory |
| `secret-scan.sh` | PostToolUse (Write\|Edit) | 편집 내 자격증명 탐지 | hard-block |
| `slop-detector.sh` | PostToolUse (Write\|Edit) | AI slop 차단 (구문 + 구조 + 스코어링, KO+EN) | advisory (옵트인 strict) |
| `stop-verify.sh` | Stop | 세션 종료 시 회귀 테스트 실행 | hard-block |

---

## 에이전트 행동 Eval

`/dev-kit:eval`은 dev-kit 스킬 실행 시 **에이전트가 올바른 입력에 올바른 출력을
내는지**를 측정합니다. 단위는 *케이스 픽스처 + 기록된 트랜스크립트 → 차원별
루브릭 판정*입니다. v1은 재생 전용: 기록된 트랜스크립트가 없는 케이스는
`SKIPPED`(회귀가 아닌 설정 갭)입니다.

**세 개의 차원**(각 축 0–10):

| 차원 | 축 | 측정 대상 |
|---|---|---|
| `review` | 판정 일관성 · 심각도 보정 · precision · recall · 코드 위생 | 리뷰 판정 + 발견 품질 |
| `security` | OWASP 분류 · 심각도 정확도 · precision | A01–A10 매핑 + 거짓 양성률 |
| `plan` | 스펙 명료성 · 스텝 원자성 · AC 실행성 · 의존성 정렬 | 원자적·실행가능·빌드가능 계획 |

케이스별 축 평균 → 판정: **OK** ≥ 8.0 · **DRIFT_WARNING** 5.0–7.9 · **ROT**
< 5.0 · **SKIPPED**(트랜스크립트 없음). `review` 차원은 `ADR-0022`에 고정된
20체크박스 코드 위생 루브릭(클린 코드 + 오버엔지니어링 + 가치/의미)을
내장합니다.

```bash
# 전체 eval → .dev-kit/eval-report.md
python lib/eval_runner.py --project-root . [--dry-run]
python lib/eval_runner.py --project-root . --dim plan
python lib/eval_runner.py --project-root . --case review-04-factory-one-impl
```

`--dry-run`은 LLM 호출을 건너뜁니다(각 케이스를 7.0/DRIFT_WARNING으로 목킹) —
API 키 없이 CI에서 유용합니다. 케이스 추가에는 코드 변경이 필요 없습니다:
`eval/cases/<dim>/`에 케이스 JSON, `eval/transcripts/<dim>/`에 트랜스크립트를
드롭한 뒤 재실행하세요. 전체 근거는
`docs/adr/ADR-0022-eval-agent-behavior.md` 참조.

---

## 저장소 구조

```
dev-harness-kit/
├── .claude-plugin/   # marketplace.json + plugin.json (Claude Code 매니페스트)
├── .codex-plugin/    # plugin.json + 번들 훅 (Codex 매니페스트)
├── skills/           # 평면: skills/<skill-name>/SKILL.md
├── hooks/            # 훅 스크립트 + lib/ + hooks.json
├── lib/              # Python 엔진 (state, execute, ci_setup, eval, cost_gate, …)
├── bin/              # devkit-refresh.sh + dev-kit-hooks-status.py + dev-kit-report.py
├── tools/            # save_log.py + token_efficiency_analyzer.py + cost_gate_status.py
├── templates/ci/     # 소비자 저장소로 배포되는 CI 템플릿
├── tests/            # pytest 스위트
├── eval/             # cases/ + transcripts/ + prompts/ + golden/
├── docs/             # STAGES, NAMING, COST-ANALYSIS, adr/, …
├── rules/            # 공유 정본 규칙 (git-workflow, session-hygiene, …)
└── CLAUDE.md         # SSOT (/dev-kit:bootstrap이 자동 생성)
```

---

## 설계 원칙

- **NO-DUP** — Iron Law는 한 곳(`CLAUDE.md §1`)에, 훅 + 스킬로 강제.
- **NO-BOTTLENECK** — 0-arg UX, 지연 로딩 `CLAUDE.md`, 병렬 서브에이전트.
- **NO-MEANINGLESS-LOOP** — 명시적 루프 시맨틱 + 자동 STOP + 사용자 인터럽트.
- **Human-on-the-Loop** — 자동 진행 + 사용자 감독 + 1회 인터럽트.
- **방법론 확장** — TDD / SDD / DDD / BDD / FDD 선택 가능.
- **A2A typed** — 서브에이전트 ↔ 메인 통신을 JSON-Schema SSOT로.
- **Plugin-only** — 플러그인 매니페스트가 단일 진실 원천.
- **Worktree-per-task** — 훅으로 강제, `rules/git-workflow.md`에 문서화.
- **Consumer-install** — 하나의 self-aware 워크플로 세트가 본 저장소와 소비자
  저장소 양쪽에서 동작.

전체 ADR 시리즈는 `docs/adr/` 참조.

---

## 기여

pre-impl 게이트(`docs/PRE-IMPL-CHECK.md`)와 8차원 비용 점검
(`docs/COST-ANALYSIS.md`)을 통과한 뒤:

```bash
python3 -m pytest tests/ -q
claude plugin validate .claude-plugin/plugin.json
```

참고 문서: [`docs/STAGES.md`](docs/STAGES.md),
[`docs/NAMING.md`](docs/NAMING.md), [`CHANGELOG.md`](CHANGELOG.md).

## 라이선스

MIT
