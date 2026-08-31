# 토큰 효율 + 리서치

**언어:** [English](token-efficiency.md) · 한국어

두 스킬이 같은 명제를 공유한다: **모든 주장은 출처가 붙거나 제거된다**.
하나는 **비용**(`/dev-kit:token-analyzer`)에, 다른 하나는 **인용**
(`/dev-kit:research`)에 그 규칙을 시행한다. 둘 다 모델이 말로 빠져나갈 수
없는 읽기 전용 데이터 계층을 출하하며 — 그 대시보드는 `/dev-kit:log`
트랜스크립트(비용용) 또는 `lib/research_engine.py` 팬아웃(인용용)의
결정론적 재생이다.

이 문서는 둘 모두를 다룬다. 60초짜리 버전은 README에 있다; 이 문서는
긴 버전이다.

---

## `/dev-kit:token-analyzer`

**카테고리:** `audit` · **알파:** `analysis` · **호출:** `/dev-kit:token-analyzer [--repo NAME] [--days N]` (사람이 호출)

`token-analyzer`는 `/dev-kit:log`가 캡처한 JSONL 세션 트랜스크립트를
저장소별, 최근 N일 토큰 효율 대시보드로 바꾼다: 4차원 세션 점수, 6개
안티패턴 경고, USD 절감액 추정.

이것이 `/dev-kit:log`의 `--html` 플래그가 아니라 자체 스킬인 이유는
`/dev-kit:log`는 트랜스크립트 캡처(on/off/status/setup)만 토글하는 반면
이 스킬은 그 트랜스크립트를 *소비*하기 때문이다 — 캡처와 분석은
별개의 슬래시 명령을 받을 자격이 있는 별개의 파이프라인 단계다.

### 작동 방식

1. `logs/claude-code/<branch>/` 및/또는 `logs/codex/<branch>/` 아래에
   트랜스크립트가 있는지 확인(재귀 워크; 최상위 레거시 평면 파일도
   픽업되어 브랜치 `main` 아래에 분류). 어느 위치에도 트랜스크립트가
   없으면 스킬은 실행 대신 `/dev-kit:log setup` + `/dev-kit:log on`을
   사용자에게 알려준다.
2. 캡처된 세션에서 가장 흔한 `cwd` basename으로 저장소 이름을 감지하거나
   `--repo <name>`을 받아 오버라이드; 사용자가 `--repo`를 전달하지 않았다면
   스킬은 실행 전에 파생된 이름을 사용자에게 확인한다.
3. `tools/token_efficiency_analyzer.py --repo <name> --days 30`을 호출하고
   `[ok] sessions=N files_scanned=M total_cost=$... estimated_savings=$...`
   요약 줄을 캡처.
4. 사용자에게 요약과 출력 HTML 경로를 **상대** 경로, `./` 접두사(절대
   `/Users/...` 경로가 아닌)로 출력 — 사용자가 다른 머신, 워크트리, 또는
   심볼릭 링크된 마운트에 있을 수 있으므로.

스킬 자체는 읽기 전용(`disallowed-tools: Write Edit`)이다; Python CLI가
직접 파일을 쓰며, 이는 나머지 dev-kit audit/inspect 스킬이 스킬 본문을
순수하게 유지하고 드라이버가 I/O를 소유하게 하는 방식과 같다. 출력 예시:

```
[ok] sessions=14  files_scanned=14  total_cost=$1.23  estimated_savings=$0.01  stale_cost=$0.00  transcripts=14
Open: ./docs/observability/dashboard-dev-harness-kit-30d.html
```

### 미리보기

![Token efficiency dashboard — dev-harness-kit, 최근 30일](../screenshots/token-dashboard-dev-harness-kit-30d.png)

*스크린샷은 최신 대시보드 HTML에서 `tools/render_dashboard.py`(Playwright
+ Chrome, 1440 × 2×)에 의해 재생성된다. 어떤 `tools/token_efficiency_analyzer.py`
변경 후 새로 고친다.*

### 플래그

| 플래그 | 기본값 | 목적 |
|---|---|---|
| `--repo <name>` | (cwd에서 자동 감지되지 않으면 필수) | `Path(cwd).name` 매칭 |
| `--days <n>` | `30` | 룩백 윈도우 |
| `--logs-dir <path>` | `./logs` | `claude-code/` + `codex/` 서브디렉터리의 루트 (재귀 워크) |
| `--branch <name>` | _(모두)_ | 단일 브랜치로 필터(`gitBranch`에 대한 대소문자 무시 서브스트링); 빈 값 = 필터 없음 |
| `--out <path>` | `docs/observability/dashboard-<repo>-<days>d.html` | 출력 HTML 경로(사이드카는 `<out-stem>.assets/`에 떨어짐) |
| `--transcripts` / `--no-transcripts` | `--transcripts` (켜짐) | 세션별 풀-트랜스크립트 사이드카 페이지를 작성하고 Transcript Index에서 연결; `--no-transcripts` = 인덱스 전용, 비활성 Open 셀 |
| `--cost-gate-tokens <int>` | `200000` | 세션별 `input + cache_read` 게이트; 이 값을 초과하는 세션은 stderr WARN 트리거 |
| `--cost-gate-usd <float>` | `5.00` | 세션별 USD 게이트; 이 값을 초과하는 세션은 stderr WARN 트리거 |
| `--pricing-override <path>` | _(없음)_ | PRICING dict를 오버라이드하는 JSON 파일 (`{tier: {in, out, cache_write_5m, cache_write_1h, cache_read}}`) |
| `--json` | _(끔)_ | 기계가 읽을 수 있는 JSON 요약을 stdout으로 출력, HTML 쓰기 건너뜀; `cost_gate=bad` 시 exit code 3 |

### 출력 섹션

HTML 대시보드는 자기 완결이다(인라인 `<style>`만, `<script>` 없음,
외부 자원 없음, 다크 모드 인식). 섹션:
`tools/token_efficiency_analyzer.py:render_dashboard`가 렌더링:

- **Cost Gate 배너** — 초록 `ok` / 호박색 `warn` / 빨강 `bad`, 위반 세션
  ID와 사유 포함.
- **Overview** — 4개 메트릭 타일: 활성 세션, 총 비용, 평균 점수(글자
  등급 배지 포함), 평균 캐시 적중률.
- **Cost & Token Distribution** — 저장소별 비용(공유 막대) + 도구별
  비용(공유 막대; `Read`가 #1이면 호박색 배너).
- **Cost by Branch** — 윈도우 내 모든 브랜치에 걸친 브랜치별 공유 막대,
  `gitBranch` 와이어 필드에서 소싱되며 레거시 평면 파일을 위한 경로
  폴백; `--branch <name>`가 나머지 리포트를 집중.
- **Cost by Worktree(State 컬럼 포함)** — 같은 형태에 `.worktrees/*/`
  아래 모든 워크트리 디렉터리에 대한 `State` 컬럼(`live` / `merged` /
  `gone` / `main`) 추가. 워크트리가 `merged` 또는 `gone`인 모든
  Sessions 행에는 호박색 `stale` 칩이 접두사로 붙음.
- **Stale Cost 타일** — 모든 `merged` / `gone` 세션의 달러 가치, 총액의
  비율 포함.
- **Cost by Model & Cache TTL Mix** — 모델별 비용 표 + 4-막대 Cache TTL
  Mix(`cache_read` / `write 5m` / `write 1h` / `pure miss`).
- **Sessions** — 세션별 행: 브랜치, 모델, 시작 시간, input/output/tools/
  cache-hit/cost, 점수 알약 + 글자 등급, 경고 칩.
- **ROI Actions (추정 절감액 기준 순위)** — `estimated_save_usd` 기준
  내림차순으로 정렬된 중복 제거된 경고.
- **Actionable Insights & Estimated Savings** — USD 콜아웃이 cache-miss /
  dup-read / model-downgrade 하위-회수로 분할.
- **Recommended Optimizations** — 경고 코드별 do/don't 리스트, 발화된
  코드에 대한 초록 체크.

### 점수 루브릭 (4차원, 0–100 가중치)

| 차원 | 가중치 | 공식 | 처벌 대상 |
|---|---:|---|---|
| Cache Utilization | 0.40 | 단계: `0..0.50` → `0..50` (1:1), `0.50..0.85` → `50..100`, `≥0.85` → `100` | prefix 불일치 |
| Output Density | 0.20 | `min(100, output / total_input * 400)` | 읽기 전용 세션 |
| Read Redundancy | 0.20 | `max(0, 100 - (max_repeat_reads - 1) * 12.5)` | 카르토그래피 실패 |
| Tool Economy | 0.20 | `max(0, 100 - tools_per_1k_out * 2)` | 도구 스래싱 |

합계 = `0.40*cache + 0.20*density + 0.20*redundancy + 0.20*economy`. 글자
등급 밴드: `A: ≥90`, `B: ≥80`, `C: ≥70`, `D: ≥60`, `F: <60` — Overview
타일과 모든 세션별 행에서 컬러 배지로 렌더링.

### 가격 모델 (USD per 1M 토큰, 티어별)

| 티어 | in | out | cache_write_5m | cache_write_1h | cache_read |
|---|---:|---:|---:|---:|---:|
| opus        | 15.0000 | 75.0000 | 18.7500 | 30.0000 | 1.5000 |
| sonnet      |  3.0000 | 15.0000 |  3.7500 |  6.0000 | 0.3000 |
| haiku       |  0.8000 |  4.0000 |  1.0000 |  1.6000 | 0.0800 |
| gpt-5-codex |  1.2500 | 10.0000 |  1.2500 |  1.2500 | 0.6250 |
| gpt-5       |  1.2500 | 10.0000 |  1.2500 |  1.2500 | 0.6250 |
| gpt-4.1     |  2.5000 | 10.0000 |  2.5000 |  2.5000 | 1.2500 |
| gpt-4o      |  2.5000 | 10.0000 |  2.5000 |  2.5000 | 1.2500 |
| o3          | 10.0000 | 40.0000 | 10.0000 | 10.0000 | 5.0000 |
| o4-mini     |  1.1000 |  4.4000 |  1.1000 |  1.1000 | 0.5500 |

Anthropic 5m TTL write = 1.25× base input; 1h TTL write = 2.0× base input.
OpenAI는 단일 캐시 입력 할인(약 base input 50%)과 TTL 분할이 없어
두 cache-write 컬럼 모두 base input과 같다. 어떤 티어든
`--pricing-override <path>.json`으로 오버라이드 가능. 알 수 없는 모델
ID는 sonnet 가격으로 폴백하며 stderr WARN 줄을 출력.

### 경고 트리거 (6개 안티패턴)

각 트리거는 프롬프트의 정확한 이모지-접두사 메시지를 가지며, 대시보드에
verbatim으로 렌더링된다. 각 `Warning`은 `estimated_save_usd`, `priority`
(1–4), `reclaim_axis`(`cache_miss` | `dup_read` | `model_downgrade` |
`""`)를 가져 ROI 액션이 달러 가치로 순위 매겨질 수 있게 한다.

| 코드 | 조건 | 수정 | 회수 축 |
|---|---|---|---|
| `CACHE_HIT_LOW` | `total_input > 50K AND cache_hit < 50%` | volatile 데이터를 프롬프트 꼬리로 이동; 세션 중간에 모델을 바꾸지 말 것 | `cache_miss` |
| `READ_HEAVY` | `Read`가 도구 비용의 ≥ 40% | 큰 파일을 한 번 고정; 카르토그래피 구축 | `dup_read` |
| `HEAVY_CONTEXT` | 한 세션에서 `total_input > 500K` | 서브에이전트에 위임; `/compact` 실행 | `""` |
| `MODEL_OVERSPEC` | Opus + density 점수 < 20 | Sonnet / Haiku로 다운그레이드 | `model_downgrade` |
| `WRITE_NOT_REUSED` | `cache_write > 50K` AND `cache_read < 2*cache_write` | 재읽기 가능한 데이터만 프롬프트 앞에 둘 것 | `cache_miss` |
| `REPEATED_USER_MSG` | 어떤 사용자 메시지 텍스트든 ≥ 2회 출현 | 완료된 서브태스크를 컨텍스트에서 제거 | `cache_miss` |

### 추정 절감액 (USD) — 세 회수 축

보수적인 회수 모델: 캐시 미스 + 중복 읽기 + 모델 다운그레이드 패널티만이
회수되며, 전체 청구액은 아니다. 목표 = 85% 캐시 적중(Anthropic 권장
최소) + 중복 읽기 0 + density<20인 Opus 세션이 Sonnet으로 스왑.

- **Cache-miss 델타** (`cache_miss_reclaim`): 토큰을 청구 가능한 input에서
  세션이 85%에 도달할 때까지 `cache_read`로 이동; 절감액 = `shifted *
  (input_price - cache_read_price)`.
- **Duplicate-read 델타** (`dup_read_reclaim`): 한 번 이상 읽힌 파일당
  `2K tokens * (n - 1)`, base input 가격 기준.
- **Model-downgrade 델타** (`model_downgrade_reclaim`): density<20인 Opus
  세션에 대해 같은 토큰 볼륨으로 Sonnet 가격 하에서 비용을 재계산하고
  차이를 취함.

도구별 비용은 `n_calls * 2K_tokens * input_price`에서 추정됨 — 휴리스틱이지
청구 API 호출이 아니다.

### Iron Law

답변에서 요약 줄을 패러프레이즈가 아닌 인용: CLI는 성공 시
`[ok] sessions=N files_scanned=M total_cost=$X.XX estimated_savings=$Y.YY stale_cost=$Z.ZZ`을
출력한다; verbatim으로 복사하여 사용자가 HTML을 열지 않고도 숫자를
감사할 수 있게 한다. 그 줄 없이 "done"이나 "passed"를 주장하지 않는다.

Stdout vs stderr 계약: `[ok]` 요약 줄은 stdout으로 간다. Cost Gate WARN
줄, 알 수 없는 모델 WARN 줄, 워크트리 분류 WARN 줄은 stderr로 —
stdout을 파싱하는 소비자는 절대로 WARN 줄을 그 안에서 보지 않아야 한다.
Exit code 3는 `--json` 하에서만 `cost_gate=bad`를 의미한다; HTML 모드는
로그 디렉터리가 비어 있지 않으면 항상 0으로 종료(exit 2).

### 관련

- `tools/token_efficiency_analyzer.py` — CLI 드라이버 (stdlib 전용).
- `fixtures/make_fixture.py` — 경고 트리거당 1개, 6개 합성 JSONL 픽스처.
- `tests/test_token_efficiency_analyzer.py` — 점수 곡선, 글자 등급,
  경고별 $ 귀속, Cost Gate, 알 수 없는 모델 경고, 가격 오버라이드,
  그리고 엔드투엔드 HTML + JSON 출력을 다루는 13개 단위 테스트.
- [`cost-gate`](../skills/cost-gate.md) — 이 사후 다중 세션 대시보드의
  라이브 단일 세션 대응물.

---

## `/dev-kit:research`

**카테고리:** `design` · **알파:** `enforcement` · **호출:** `/dev-kit:research <claim>` (사람이 호출)

`research`는 출처가 필요한 어떤 주장에든 Phase 0 → Phase 3 인용 시행
게이트를 실행한다. `cache → direct → multi-source → human-in-the-loop`을
거쳐 에스컬레이션한 다음 `verify()`와 `enforce_citations()`가 no-go
게이트다. 모든 주장은 출처를 인용하거나 제거된다. 출처:
[`skills/research/SKILL.md`](../../skills/research/SKILL.md).

### 사용 시점

- 사용자가 `/dev-kit:research <claim>`을 입력.
- `plan` 또는 `review` 단계가 사실을 주장하기 전 인용된 증거가 필요.
- 운영자가 초안에 대해 결정론적 "모든 주장은 출처를 인용" 패스를 원함.
- 코드 리뷰가 인용되지 않은 주장을 드러내 그것을 인용하거나 제거해야
  할 때.

### 단계 (에스컬레이션 체인)

`escalate(query, max_phase=N)`은 4개 결정론적 단계를 거닌다:

- **Phase 0** — `.dev-kit/research_cache.jsonl` (< 30일) 에 대한 캐시
  적중.
- **Phase 1** — 첫 후보 URL에 대한 직접 HTTP GET + OGP / JSON-LD 추출.
- **Phase 2** — N개 후보 URL에 대한 팬아웃, URL 기준 중복 제거.
- **Phase 3** — 사람 핸드오프. 구조화된 `NEEDS_HUMAN` 페이로드를 반환.
  결코 결과를 날조하지 않음.

`max_phase` 플래그는 **3**(사람 핸드오프 캡, `MAX_PHASE_CAP`)으로 기본;
더 높은 값을 전달하면 no-op(엔진이 사람 핸드오프 단계에서 캡).
캐시 전용 실행을 강제하려면 `--max-phase 0`; Phase 1만으로 제한하려면
`--max-phase 1`; Phase 2 다중 소스 팬아웃을 허용하려면 `--max-phase 2`.

### 검증 게이트

`verify(claim, sources)`:

- 소스당 `url` + `fetched_at` + `source_type` 요구.
- 모든 URL에 HEAD 체크; 깨진 URL은 갭이 됨.
- `>= 3` 소스가 동의할 때 자신감 부스트.

결과 산문에 대한 `enforce_citations(text)`:

- `[src:URL;ts:DATE;type:primary]` 블록이 있는 문장은 통과.
- 다른 문장은 `[UNCITED]` 접두사가 붙어 리뷰어가 고칠 수 있게.

### 호출

```bash
# 인용 게이트와 함께 완전한 Phase 0–3 에스컬레이션.
/dev-kit:research "Why does X fail in CI?" --max-phase 3

# 캐시 전용 실행 강제 (네트워크 없음):
/dev-kit:research "Why does X fail in CI?" --max-phase 0

# 초안 산문 파일에 인용 시행 강제:
python3 -c "from lib.research_engine import enforce_citations; print(enforce_citations(open('draft.md').read()))"
```

`safety_valve: 4`, `convergence: enforce_citations returns 0 uncited sentences`,
`dedup_metric: same-query-escalate=2`, `user_interrupt: true`.

### Eval 훅

스킬은 두 새로운 `DIM_AXES` 튜플(각 5개 축, `review` 형태를 미러링)로
판정된다:

| 축 | 무엇을 점수 매기는가 |
|---|---|
| `research_source` | 권위 / 최신성 / 1차 vs 2차 / url 유효성 / 인용 완성도 |
| `research_claim` | 인용-필수 / n-소스 합의 / 1차 출처 존재 / 타임스탬프 존재 / 루브릭 매칭 |

프롬프트: `eval/prompts/judge-research-source.md` + `judge-research-claim.md`.
라이브 eval은 자동 트리거되지 않음 — 케이스 픽스처가 생기면
`/dev-kit:evaluate --dim research_source` 또는 `--dim research_claim`로 배선.

### Iron Laws

- **L1**: `verify()`가 내보내는 모든 주장은 `url` + `fetched_at` +
  `source_type`을 포함해야 한다. 갭 리스트가 이 계약을 깬다.
- **L4**: 소스 레코드에 `TODO` 플레이스홀더 없음 — 빈 `title`은 괜찮지만
  누락된 `fetched_at`은 "나중에 채울 것"이 아니라 갭이다.
- **L5**: 검색 엔진 메뉴가 아니라 하나의 결정론적 흐름(Phase 0 → 3).

### 실패 모드

- Phase 1의 네트워크 실패 → `max_phase >= 2`이고 후보 URL이 최소 2개라면
  Phase 2로 에스컬레이트; 그 외 Phase 3 `NEEDS_HUMAN`.
- 후보 URL 비어 있고 `max_phase >= 2` → Phase 3 (URL을 발명하지 않음).
- 0개 소스로 `verify()` → `verified=False`, `gaps=[<reason>]`.
- HEAD 요청 실패 → URL이 `citations`에서 제외되고 `gaps`에 나열.

### 핸드오프

- plan-모드 주장: 인용 시행 산문과 함께 `/dev-kit:plan`에 핸드오프.
- 리뷰 finding: `enforce_citations()` 후 `/dev-kit:review`에 핸드오프.
- 릴리스-차단 검증: `verify()`가 `confidence >= 0.7`로 `verified=True`를
  반환하면 `/dev-kit:ship`에 핸드오프.

### 관련

- `lib/research_engine.py` — escalate / verify / enforce_citations.
- `lib/llm_judge.py` — `research_source` + `research_claim` 축.
- [`plan`](../skills/plan.ko.md) — 인용 시행 산문 핸드오프 대상.
- [`review`](../skills/review.md) — 인용 시행 후 리뷰 패스.
