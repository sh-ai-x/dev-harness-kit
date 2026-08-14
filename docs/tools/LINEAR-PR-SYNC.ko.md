# Linear PR 상태 동기화

**언어:** [English](LINEAR-PR-SYNC.md) · 한국어

`tools/linear_pr_sync.py`는 `.github/workflows/linear-pr-sync.yml`에 의해
호출되어 풀 리퀘스트 브랜치에 연관된 Linear 이슈를 PR 라이프사이클에 맞춰
정렬한다.

## 상태 매핑

| GitHub pull-request 이벤트 | Linear 상태 |
|---|---|
| `opened` (draft=false) | In Progress |
| `opened` (draft=true) | no-op (draft는 `ready_for_review`에서 동기화) |
| `ready_for_review`, `reopened`, `synchronize`, `edited` | In Review |
| `closed` with `merged=true` | Done |
| `closed` without merge | Canceled |

이슈는 `<!-- scope:<branch>::` 설명 마커 **접두사**로 상관된다 — 이는 이
스크립트가 직접 이슈를 생성할 때 쓰는 정확한 `<!-- scope:<branch>::auto-sync -->`
마커와, 클라이언트 측 세션 훅(`tools/linear_sync.py`)이 쓰는
`<!-- scope:<branch>::<prompt 단어들> -->` 마커를 모두 매칭시켜, 브랜치의 PR
라이프사이클 전이가 로컬 훅이 이미 만든 이슈에 반영되고 중복 이슈를 만들지
않도록 한다. 쿼리는 `LINEAR_PROJECT_NAME`(기본 `dev-harness-kit`)으로
범위화되어 페이지네이션. 워크플로-상태 ID는 프로젝트의 팀 내에서만 해결.

이벤트 동기화 단계는 non-blocking이므로 외부 Linear 장애가 PR을 블록할 수
없다. 워크플로의 `smoke` 명령은 엄격하며 프로젝트 또는 어떤 필수 워크플로
상태도 해결할 수 없을 때 non-zero를 반환.
