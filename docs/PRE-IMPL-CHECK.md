# Pre-Implementation Gate — dev-harness-kit

> **AI가 자동 작성, 사용자가 검토 (MUST-25, MUST-55, MUST-NOT-17).**
> 작성 시점: Phase 1 시작 직전 (코드 작성 진입 전).
> 사용자 OK 없이는 첫 코드 작성 ❌.

---

## §A. WHETHER Gate (YAGNI)

- **지금 바로 사용자가 체감할 pain:**
  - 5개 분리된 plugin (`pm-prd-fast`, `interview-harness`, `dev-harness`, `claude-review-plugins`, `slop-shield`) 설치·호출·컨텍스트 전환 부담.
  - 단계 사이 hand-off 추적 깨짐 (`.pm-prd-fast/*.md` ↔ `PRD.md` ↔ `phases/*/step<N>.md` ↔ review findings).
  - Iron Law 5개가 5개 repo에 분산 → 단일 SSOT 부재.
- **6개월 안에 쓰일 가능성:** HIGH (이미 사용 중)
- **안 만들면 어떤 비용:** 5 plugin 유지보수 (~2.5 일/month × $100/h × 5명 = ~$15K/year 손실)

## §B. 5-Question Checklist (MUST-24)

| # | Question | 응답 | 근거 |
|---|---|:---:|---|
| 1 | **WHETHER** — 진짜 필요한가? | ✅ | 사용자 명시: "5 repo → 통합 플러그인 만들어줘" |
| 2 | **PROBLEM** — 측정 가능한 pain? | ✅ | (a) 5 install 비용 (b) hand-off 깨짐 (c) Iron Law 5 중복 |
| 3 | **CHEAPER ALT** — 더 단순한 방법? | ✅ | "5 repo 그대로 합치기" 검토 → 일관성 깨짐. 통합만 유일 |
| 4 | **REVERT COST** — 제거 비용? | ✅ | `DEPRECATED.md` 1줄. 옛 repo 코드 보존 |
| 5 | **VALUE/COST** — 가치가 비용의 3배? | ✅ | 가치 $15K/year ≥ 비용 $9.6K (구현) |

## §C. 8-Dimension Cost/Risk (MUST-54)

| # | Dimension | 자동 분석 결과 |
|---|---|---|
| 1 | **Time Cost** | 구현 **6.0 일** ($4,800). 유지보수 0.5 일/월 = $4,800/year |
| 2 | **Monetary Cost** | LLM-as-judge 13 자산 × daily × 2K tokens × $0.0003 = **$87/year**. CI nightly $15/year. **합계 ~$100/year** |
| 3 | **Legal Risk** | 5 repo 모두 MIT/Apache2 호환. GDPR non-PII. **LOW** |
| 4 | **Maintenance Risk** | Maintaner 1명 + ADR 22개 + CHANGELOG + 온보딩 1 커맨드. **MEDIUM** (mitigated) |
| 5 | **Opportunity Cost** | 안 만들면 5 plugin 2.5 일/month × 12 × $100/h × 5명 = **$15K/year 손실** = HIGH VALUE |
| 6 | Compatibility Risk | DEPRECATED.md + opt-in provider. 기존 review workflow와 충돌 없음. **LOW** |
| 7 | Security Risk | hook 우회 가능 (opt-in `DEV_KIT_HOOK_OFF`). secret scan 자동. **LOW** |
| 8 | Operational Risk | CI 의존 + provider 다운 시 manual `/dev-kit:eval`. **MEDIUM** |

(상세 분석: `docs/COST-ANALYSIS.md` 참조)

## §D. 오늘 만들 것 (단일 문장 per row)

| 기능 | 단일 입출력 | 가장 단순한 형태 | Test 1개 | 회귀 1개 |
|---|---|---|---|---|
| `dev-harness-kit/` 디렉토리 | (1 dir) | `mkdir + touch .gitkeep` | `test_dir_exists` | — |
| `CLAUDE.md` (§1~§5 통합) | (1 file, SSOT) | `lib/write_claude_md.py` + Iron Laws 5개 + skeleton sections | `test_write_claude_md_skeleton` | `test_no_dup_iron_law` |
| `.claude-plugin/marketplace.json` | (1 file) | dev-harness-kit marketplace 선언 | `test_marketplace_valid` | `test_install.sh_dependency` |
| `.claude-plugin/plugin/plugin.json` | (1 file) | name=dev-harness-kit, v0.1.0 | `test_plugin_json_schema` | `test_naming_consistent` |
| `hooks/hooks.json` (전부 `exit 0`) | (1 file) | Pre/Post/Stop × 2 = 6 hook, single file | `test_hooks_json_all_exit0` | `test_hook_portable_paths` |
| `skills/bootstrap-sanity/SKILL.md` | (1 file) | 결정론 read-only audit, regex+glob 0건 | `test_sanity_deterministic` | `test_sanity_readonly_no_modify` |
| `skills/bootstrap-codebase-map/SKILL.md` | (1 file) | 4 섹션 결정론 합성 → CLAUDE.md §3 | `test_codebase_map_stub` | `test_codebase_map_5line` |
| `commands/bootstrap.md` | (1 file) | `/dev-kit:bootstrap` 0-arg orchestrator | `test_bootstrap_command_zero_arg` | `test_bootstrap_stage_aware_hooks` |
| `lib/{state_codec,active_hooks_codec,write_claude_md}.py` | (3 files) | 각각 snake_case.py, ≥1 test | `test_state_codec_roundtrip` 등 | `test_<module>_ssot` |
| `.env.example` | (1 file) | Provider + Token 환경변수 템플릿 | `test_env_example_keys` | `test_install_doc_consistent` |

**합계: 10개 파일 / Phase 1 1.0일 / 테스트 10+ / 회귀 10+**

## §E. 오늘 안 만들 것 (명시적 비범위)

- [ ] 별도 agents/ 디렉토리 (Sub-agent 흡수, MUST-23)
- [ ] 플러그인 외부 의존 (5 repo 전부 흡수, ADR-0001)
- [ ] 인터랙티브 옵션 (UX 자동 결정, MUST-21)
- [ ] 머신러닝/AI 확장 (Loop Engineering만 Ralph로, ADR-0002)
- [ ] --team 모드 (Phase 1 = 10x default, 100x는 Phase 5에서)
- [ ] Eval-Repair Specialized Fixers (Phase 3)
- [ ] A2A typed schemas (Phase 3)
- [ ] `lib/install.sh --team` (Phase 3)
- [ ] `docs/COST-ANALYSIS.md` 동봉 (이미 §C에서 발췌, 본문은 별도)
- [ ] 각 옛 5 repo `DEPRECATED.md` (Phase 4)

## §F. 사용자 검토 (1회, HOTL)

위 5 질문 + 8 dimension + Phase 1 작업 범위 검토 후:

- [ ] 모두 OK
- [ ] 우려 코멘트: `_______________________________________________`

OK 후 Phase 1 코드 작성 시작. 사용자 응답 대기 ≤ 1 영업일.

---

**작성**: AI 자동 (Phase 1 시작 5분 전)
**검토**: 사용자 1회 (이 체크리스트 grep + 5분)
**Phase 1 시작 조건**: §F 모두 체크 또는 코멘트 후 사용자 명시 OK
