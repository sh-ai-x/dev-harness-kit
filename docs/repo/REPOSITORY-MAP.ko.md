# 저장소 맵

**언어:** [English](REPOSITORY-MAP.md) · 한국어

이 문서는 `dev-harness-kit`의 컨셉 수준 레이아웃이다. 현재 파일 인벤토리는
`ls`(또는 `tree -L 2`)를 실행한다. 플러그인 매니페스트(`/dev-kit:plan`,
`/dev-kit:ci-setup`)와 워크트리 규칙(`rules/git-workflow.md`)이 정식
진실 공급원이다; 이 맵은 보조 네비게이션이며 두 번째 설정 소스가 아니다.

```
dev-harness-kit/
├── .claude-plugin/   # marketplace.json + plugin.json (Claude Code 매니페스트)
├── .codex-plugin/    # plugin.json + 번들 훅 (Codex 매니페스트)
├── skills/           # 사용자 호출 / 내부 스킬별 SKILL.md 1개
│   └── README.md     # 모든 스킬의 정식 사람이 읽을 수 있는 인덱스
├── commands/         # 슬래시 명령 래퍼 (skill-usage, review-local, …)
├── hooks/            # 훅 스크립트 + lib/ + hooks.json + references/slop/
├── lib/              # Python 엔진 (state, execute, ci_setup, eval, cost_gate, …)
├── bin/              # devkit-refresh.sh + set-provider.sh + dev-kit-* 상태 스크립트
├── tools/            # save_log.py + analyzer + cost-gate + session/skill 텔레메트리
├── templates/ci/     # 소비자 저장소에 출하되는 CI 템플릿
├── fixtures/         # analyzer를 위한 합성 세션 로그 픽스처
├── tests/            # pytest 스위트
├── eval/             # cases/ + transcripts/ + prompts/ + golden/
│   └── golden/       # 회귀 베이스라인 (12개 차원 케이스 + 3개 유지보수 케이스)
├── docs/
│   └── proposals/    # 디자인 제안 -- umbrella별 <main>/<sub>.{yaml,html}
│                     # 자동 부착된 "← Index" 백내비 포함
├── rules/            # 공유 정식 규칙 (git-workflow, session-hygiene, …)
└── CLAUDE.md         # SSOT (`/dev-kit:bootstrap`에 의해 자동 생성)
```

저장소 문서 전체는 [README.ko.md](../../README.ko.md)에서 시작한다.
선택 가능한 생성된 프로젝트 맵은 `/dev-kit:bootstrap --full-claude-md`로
`docs/CODEBASE-MAP.md`에 작성된다.
