---
name: audit-slop
category: audit
description: slop-detector SSOT 일괄 감사. KO+EN banned phrase scan. HIGH/MEDIUM/LOW buckets report.
when_to_use: |
  - User types /dev-kit:audit (cross-cutting)
  - Bulk audit before release
allowed-tools: Read Grep Bash
disallowed-tools: Write Edit
model: haiku
---

# audit-slop — Read-Only Slop Audit

## Iron Law
**Read-only invariant.** 파일 수정 ❌. grep 결과만 report.

## SSOT

`hooks/slop-detector.sh`의 `SLOP=` regex (single source of truth). 17 EN + KO 동등 phrase.

## 출력

```markdown
## /dev-kit:audit slop — {path} — {N} files / {M} total matches

### HIGH (≥5 matches)
- README.md 8 (delve into×3, robust×2, cutting-edge×3, ...)

### MEDIUM (2-4)
- docs/STAGES.md 3

### LOW (1)
- skill/t.skill
```

## 규칙

- Skip globs: `.git/`, `node_modules/`, `dist/`, `__pycache__/`, lockfiles
- Max 20 files in report
- Per-phrase count + path (token efficiency)
- Read-only — 절대 write ❌

## Hook

slop-detector.sh는 PostToolUse 자동 활성 (slop-detector=ON stage).

## 회귀 fixture

- `examples/sample-with-slop.md` → HIGH ≥ 1 report
- `examples/sample-clean.md` → 0 finding
