# Maintenance Gate — 과도한 엔지니어링 + 클린 코드 + 가치 게이트

**언어:** [English](maintenance-gate.md) · 한국어

Maintenance gate는 pre-push "의도" 검사의 CI 대응물이다. 모든 풀 리퀘스트에
실행되며 PR diff를 `eval/prompts/judge-code-sanity.md`의 20-체크박스
코드-새니티 루브릭에 대해 판정한다. 전체 루브릭은 세 버킷을 가진다:

- **클린 코드 (CC-1..8)** — 모호한 이름, 과대 함수, 죽은 코드, 매직
  상수, 복사-붙여넣기, 삼킨 에러, 타입 비안전, 오래된 코멘트.
- **과도한 엔지니어링 (OE-1..8)** — 단일 구현자 추상 베이스 클래스,
  투기적 매개변수 / YAGNI 플래그, 조기 최적화, 과도한 레이어링, 단일
  구현을 위한 factory/strategy/DI, 깊은 상속, 파일당 클래스 스프롤.
- **가치 / 의미 (VM-1..4)** — 명시된 목적, 노이즈 없음, 범위 규율,
  "diff가 자기 줄을 벌었다."

PR 전용 `push_intent` 판정은 `.githooks/pre-push`에서 각 푸시 전 로컬로
VM-1..4 축을 실행한다. Maintenance gate는 통합 PR diff에 대한 전체 CC
+ OE + VM 검사 — 더 느리지만 포괄적.

## 아키텍처

| 표면 | 파일 | 역할 |
|---|---|---|
| 워크플로 | `.github/workflows/maintenance.yml` | CI 진입점. 두 잡: `maintenance_judge`(`/dev-kit:maintenance --diff <PR>`를 `claude-code-action`을 통해 실행)와 `gate`(판정 추출 + docs-updated 서브-게이트 실행). |
| Judge 프롬프트 | `eval/prompts/judge-maintenance.md` | 20-체크박스 루브릭을 래핑하고 모델에게 `code_sanity_score`, `docs_coverage_score`, `scope_discipline_score`(각 0-10)와 `reason`, `items_flagged[]`를 요청. |
| Gate 로직 | `lib/maintenance_gate.py` | 순수 함수 판정 추출 + docs-updated 검사 + `combine_verdict` 도출. `tests/test_maintenance_gate.py`에서 단위 테스트됨. |
| Pre-push 형제 | `.githooks/pre-push` (`DEV_KIT_PUSH_INTENT=1`로 옵트인) | 푸시 전 tip 커밋에 대한 `lib/push_intent_judge.py` 실행. 4개 value/meaning 축(VM-1..4)만. |
| Pre-push 형제 CLI | `lib/push_intent_judge.py` | `dim="push_intent"`로 `lib/llm_judge.call_judge` 위의 얇은 래퍼. |
| 회귀 픽스처 | `eval/golden/maintenance-*.json` | Maintenance judge를 위한 세 개의 골든 베이스라인. |

## 판정 도출

Judge 프롬프트는 세 개의 0-10 합성 축을 요청한다. Gate는 `code_sanity_score`를
CI 판정에 매핑한다:

| `code_sanity_score` | CI 판정 |
|---|---|
| ≥ 8.0 | **Approve** |
| 5.0 – 7.99 | **Changes Requested** |
| < 5.0 | **Blocked** |

Gate는 그 다음 **docs-updated 서브-게이트**를 실행:

- **Pass** (a) PR이 프로덕션 경로를 건드리지 않거나, (b) PR이 `docs/`
  파일(자동 관리되는 `docs/stages/STAGES.md`와 `docs/repo/REPOSITORY-MAP.md`
  제외)도 건드리거나, (c) PR 본문에 인용된 기존 참조와 함께 `docs-not-required:`
  마커를 포함.
- **Fail** 그 외 — 게이트는 `Approve`를 `Changes Requested`로 강등하여
  사람 리뷰어가 알아차리게.

Judge의 `Blocked`는 docs 검사를 단락: judge가 중요한 과도한 엔지니어링이나
클린 코드 위반을 표시하면 완벽히 문서화된 PR도 게이트에 실패.

## 임계값 (조정 가능)

`lib/maintenance_gate.py`의 `combine_verdict` 함수가 판정 맵의 단일
진실 공급원. 임계값을 올리거나 내리려면 `maintenance.yml`의 judge
프롬프트 매핑에서 밴드의 `>=` 컷오프를 편집하고 `tests/test_maintenance_gate.py`의
매칭 단위 테스트를 업데이트.

## 우회

이 게이트에 대한 `--no-verify` 등가물은 없다. Maintenance에 실패하는 PR을
강제 머지하려면 운영자가 다음 중 하나를 해야:

1. 후속 커밋에서 게이트의 finding을 수정(게이트 재실행).
2. 관리자가 GitHub UI에서 브랜치 보호를 우회해 admin 권한으로 머지.
   게이트는 절대 자동 승인하지 않는다.

## 작동 예시

잘 명명된 함수, 명확한 docstring, 추상 베이스 클래스 없는 20줄의
`lib/awesome.py`와 `docs/awesome.md`를 추가하는 PR:

1. `maintenance_judge`가 `/dev-kit:maintenance --diff 42`를 실행.
   - Judge 프롬프트가 `code_sanity_score=9.0`, `docs_coverage_score=10.0`,
     `scope_discipline_score=10.0`, `items_flagged=[]` 반환.
   - Judge가 `**Verdict:** Approve`를 방출.
2. `gate`이 `Approve`를 추출.
3. `gate`이 docs-updated 서브-게이트를 실행:
   `docs_updated_ok(["lib/awesome.py", "docs/awesome.md"], "")` →
   `(True, "docs updated: docs/awesome.md")`.
4. `gate`이 `combine_verdict(Approve, True, "...")`를 실행 →
   `{"verdict": "Approve", "docs_ok": True, ...}`.
5. Gate가 0으로 종료.

같은 PR이지만 `lib/awesome.py`만 (docs 업데이트 없음):

1. `maintenance_judge`가 `code_sanity_score=9.0` 반환 →
   `**Verdict:** Approve`.
2. `gate`이 `Approve`를 추출.
3. `gate`이 docs-updated 서브-게이트를 실행:
   `docs_updated_ok(["lib/awesome.py"], "")` →
   `(False, "PR changed lib/awesome.py but no doc under docs/ ...")` .
4. `gate`이 `combine_verdict(Approve, False, "...")`를 실행 →
   `{"verdict": "Changes Requested", "docs_ok": False, ...}`.
5. Gate가 1로 종료하며 `::error::reason: ...`.

## 로컬 개발

GitHub Actions 없이 로컬에서 게이트의 로직을 실행:

```bash
# 코멘트 본문에서 판정 추출
echo "**Verdict:** Approve" | python3 -m lib.maintenance_gate \
  --extract-verdict-from-stdin

# docs-updated 검사 실행
python3 -m lib.maintenance_gate --docs-check \
  --changed-files lib/foo.py --changed-files docs/foo.md \
  --pr-body ""

# judge 판정 + docs 검사 결합
python3 -m lib.maintenance_gate \
  --judge-verdict Approve \
  --docs-ok \
  --docs-reason "ok"
```

## 포크 PR

`maintenance_judge`(및 `review.yml`의 `review`/`security`)는 PR의 head
repo가 포크일 때 `pull_request` 트리거에서 스스로 스킵한다 — GitHub는
포크에서 온 `pull_request` 실행에 대해 워크플로우가 선언한
`permissions:`와 무관하게 `GITHUB_TOKEN`을 read-only로 제한하고 OIDC
토큰 발급을 거부하므로, judge를 그대로 실행하면 OIDC 토큰 요청
단계에서 실패한다. `gate`도 해당 PR에 대해서는 "판정 누락 시 Approve로
기본값 처리"하는 폴백을 스킵해, judge가 한 번도 실행되지 않은 채로
Approve로 기본 처리된 게이트를 통과하는 일이 없도록 한다.

`.github/workflows/fork-pr-review.yml`은 이제 옵트인 수동 폴백이다.
`pull_request_target`(신뢰된 `main`에서 워크플로우 파일을 읽으므로
write 권한을 부여해도 안전) 트리거로, 포크 PR에 대해서만 실행되며,
**운영자가 `vars.AUTO_REVIEW_FORKS=false`(문자열 리터럴)로 명시적으로
설정한 경우에만 동작한다**. 작업의 `if:` 블록이 이 변수로 게이트되어
있어, 기본 동작(변수 미설정 / 빈값 / `true`)에서는 이 워크플로우가
완전히 스킵되고 아래의 자동-리뷰 경로로 라우팅된다.

### 기본 자동-리뷰 경로 (pull_request_target 마이그레이션 이후의 포크 PR)

`maintenance.yml`과 `review.yml`은 `pull_request` / `workflow_dispatch`
추가로 `pull_request_target`도 트리거한다. 각 워크플로우의 LLM-judge
작업(`maintenance_judge`, `review`, `security`)은
`vars.AUTO_REVIEW_FORKS != 'false'` 게이트 뒤에서 포크 PR에 대해
자동으로 실행된다. 포크 신뢰 모델은 다음으로 유지된다:

1. 워크플로우 파일은 신뢰된 `main` 브랜치에서 읽음(포크가 아님).
2. `actions/checkout`은 안전한 기본 `ref:` 사용 — `pull_request_target`
   에서 기본 ref는 GitHub가 만든 머지 커밋(base + head)이며 PR head
   SHA가 아니다. 향후 누군가가 `ref: ${{ github.event.pull_request.head.sha }}`
   을 추가하면 신뢰 모델이 무너지며, `tests/test_fork_pr_auto_review.py`
   가 그 부재를 핀한다.
3. LLM judge는 `gh pr diff`(GitHub API)로 diff를 가져오므로 러너의
   파일시스템 내용이 보이는 PR 내용 이상으로 judge에 영향을 줄 수
   없다. dev-kit 플러그인은 머지 커밋에서 심볼릭 링크되며 포크 트리가
   아니다.

결과: 외부 기여자(`mybotagent`)의 PR #682, #687처럼 더 이상
Actions 탭에서 메인테이너 클릭이 필요하지 않다 — AI judge 코멘트가
모든 `synchronize` / `opened` / `reopened` 이벤트에서 자동으로 PR에
남는다.

### 수동 폴백 경로 (`vars.AUTO_REVIEW_FORKS=false`인 경우만)

운영자가 자동 리뷰를 명시적으로 옵트아웃한 경우(예: CI-분 단위 사용량을
사람 승인 뒤에 게이트하기 위해) `fork-pr-review.yml`이 포크 PR에 대해
`pull_request_target`에서 실행되고 `fork-pr-review` GitHub
Environment 뒤에 위치하며, 승인 시 `maintenance.yml` + `review.yml`을
`workflow_dispatch`로 디스패치한다 — 포크-원본 이벤트가 아니므로 그
실행은 정상 권한으로 도달한다. 모드 전환은 한 줄짜리 명령이다:

```bash
# 수동 게이트 재활성화(포크 PR이 승인 대기열에 머무름):
gh variable set AUTO_REVIEW_FORKS --body false
# 기본 자동-리뷰 경로 복원:
gh variable delete AUTO_REVIEW_FORKS
```

## 관련

- `eval/prompts/judge-code-sanity.md` — 20-체크박스 루브릭(maintenance
  judge 프롬프트가 참조하는 결정론적 SSOT).
- `eval/prompts/judge-maintenance.md` — judge의 사용자 프롬프트.
- `lib/maintenance_gate.py` — 게이트 로직(판정 추출 + docs-updated
  검사 + `combine_verdict`).
- `tests/test_maintenance_gate.py` — 단위 테스트(각 판정 + 각 docs-검사
  분기 + CLI 패리티를 다루는 19개 테스트).
- `eval/golden/maintenance-*.json` — maintenance judge를 위한 세 개의
  회귀 픽스처(가치 정렬, 과도한 엔지니어링, 범위 드리프트).
- `.github/workflows/review.yml` — 형제 보안/정확성 게이트. 두 게이트가
  판정-추출 패턴을 공유해 운영자에게 PR 코멘트가 동일하게 보임. 포크
  PR 자동-리뷰 경로를 위해 `pull_request_target`도 트리거함 — 위
  "포크 PR" 참고.
- `.github/workflows/fork-pr-review.yml` — 이 워크플로우(+ `review.yml`)
  를 포크 PR에 대해 디스패치하는 옵트인 수동 폴백. 오직
  `vars.AUTO_REVIEW_FORKS=false`일 때만 동작. 위 "포크 PR" 참고.
- `tests/test_fork_pr_auto_review.py` — 자동-리뷰 계약을 핀함
  (pull_request_target 트리거, AUTO_REVIEW_FORKS 게이트, 안전한
  checkout 기본값, 포크 코드 실행 없음).
- `docs/stages/STAGES.md` §7 — 파이프라인 단계 설명.
