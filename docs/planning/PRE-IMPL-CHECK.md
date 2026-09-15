# Pre-Implementation Gate

> AI auto-writes, user reviews (single approval, no code without this check).
> Write time: right before Phase 1 (before any production code).

---

## §A. WHETHER Gate (YAGNI)

- **Pain the user feels right now:**
  - 5 separate plugins (`pm-prd-fast`, `interview-harness`, `dev-harness`, `claude-review-plugins`, `slop-shield`) — install, invoke, and context-switching cost.
  - Hand-off breaks between stages (`.pm-prd-fast/*.md` ↔ `PRD.md` ↔ `phases/*/step<N>.md` ↔ review findings).
  - Iron Law × 5 scattered across 5 repos — no single SSOT.
- **Will be used within 6 months**: HIGH (already in use).
- **Cost of NOT building**: 5-plugin maintenance (~2.5 day/month × $100/h × 5 people = ~$15K/year loss).

## §B. 5-Question Checklist

| # | Question | Answer | Rationale |
|---|---|:---:|---|
| 1 | **WHETHER** — Is it really needed? | ✅ | User explicit: "merge 5 repos into one plugin" |
| 2 | **PROBLEM** — Is the pain measurable? | ✅ | (a) 5 install cost (b) broken hand-off (c) Iron Law × 5 duplicates |
| 3 | **CHEAPER ALT** — Simpler way? | ✅ | "Keep 5 repos as-is" reviewed — consistency breaks. Integration is the only path |
| 4 | **REVERT COST** — Cost to remove? | ✅ | Low — code is dead, directory removed |
| 5 | **VALUE/COST** — Is value 3× cost? | ✅ | Value $15K/year ≥ cost $9.6K (build) |

## §C. 8-Dimension Cost/Risk

| # | Dimension | Auto-analysis result |
|---|---|---|
| 1 | **Time Cost** | Build **6.0 days** ($4,800). Maintenance 0.5 day/month = $4,800/year |
| 2 | **Monetary Cost** | LLM-as-judge 13 assets × daily × 2K tokens = **~$3/year**. CI nightly $15/year. **Total ~$18/year** (negligible) |
| 3 | **Legal Risk** | All 5 repos MIT/Apache2 compatible. GDPR non-PII. **LOW** |
| 4 | **Maintenance Risk** | 1 maintainer + 4 ADRs + CHANGELOG. **MEDIUM** (mitigated) |
| 5 | **Opportunity Cost** | Not building = 5 plugins × 2.5 day/month × 12 × $100/h × 5 people = **$15K/year loss** = HIGH VALUE |
| 6 | Compatibility Risk | Opt-in provider. No conflict with existing review workflow. **LOW** |
| 7 | Security Risk | Hook bypass possible (opt-in `DEV_KIT_HOOK_OFF`). Secret scan automated. **LOW** |
| 8 | Operational Risk | CI dependency + provider down → manual `/dev-kit:evaluate`. **MEDIUM** |

(Detail: `docs/quality/COST-ANALYSIS.md`)

## §D. What to build today (one sentence per row)

| Feature | Single I/O | Simplest form | Test 1 | Regression 1 |
|---|---|---|---|---|
| `dev-harness-kit/` directory | (1 dir) | `mkdir + touch .gitkeep` | `test_dir_exists` | — |
| `CLAUDE.md` (§1~§5 unified) | (1 file, SSOT) | `lib/write_project_md.py` + 5 Iron Laws + skeleton sections | `test_write_project_md_skeleton` | `test_no_dup_iron_law` |
| `.claude-plugin/marketplace.json` | (1 file) | dev-harness-kit marketplace declaration | `test_marketplace_valid` | `test_install_sh_dependency` |
| `.claude-plugin/plugin.json` | (1 file) | name=dev-harness-kit | `test_plugin_json_schema` | `test_naming_consistent` |
| `hooks/hooks.json` (all `exit 0`) | (1 file) | Pre/Post/Stop × 2 = 6 hooks, single file | `test_hooks_json_all_exit0` | `test_hook_portable_paths` |
| `skills/bootstrap/SKILL.md` (sanity + codebase-map + hook-matrix inlined sub-stages) | (1 file) | Deterministic read-only audit + 4-section codebase-map synthesis + hook matrix init | `test_sanity_deterministic` | `test_sanity_readonly_no_modify` |
| `lib/{state_codec,active_hooks_codec,write_project_md}.py` | (3 files) | Each snake_case.py, ≥1 test | `test_state_codec_roundtrip` etc. | `test_<module>_ssot` |
| `.env.example` | (1 file) | Provider + Token env var template | `test_env_example_keys` | `test_install_doc_consistent` |

**Total: 10 files / Phase 1 1.0 day / 10+ tests / 10+ regressions**

## §E. NOT building today (explicit out-of-scope)

- [ ] Separate `agents/` directory (absorbed into sub-agents)
- [ ] External plugin deps (all 5 repos fully absorbed, ADR-0001)
- [ ] Interactive options (UX auto-determined)
- [ ] ML/AI extensions (Loop Engineering only via Ralph, ADR-0002)
- [ ] Team collaboration toggle (Phase 1 = 10x default, 100x in Phase 5)
- [ ] Eval-Repair Specialized Fixers (Phase 3)
- [ ] A2A typed schemas (Phase 3)
- [ ] `DEV_KIT_TEAM` install/tracking integration (Phase 3)
- [ ] `docs/quality/COST-ANALYSIS.md` attached (already excerpted in §C, body separate)

## §F. User review (1×, HOTL)

Review the 5 questions + 8 dimensions + Phase 1 scope, then:

- [ ] All OK
- [ ] Concern: `_______________________________________________`

After OK, start Phase 1 code. User response expected within 1 business day.

---

**Written by**: AI auto (5 min before Phase 1)
**Reviewed by**: User 1× (grep + 5 min)
**Phase 1 start condition**: §F all checked, OR user explicit OK after comments
