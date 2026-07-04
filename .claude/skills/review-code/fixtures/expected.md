# Expected outcomes (source of truth for regression)

| fixture | path | expected findings |
|---|---|---|
| real-bugs/sql_injection.py | fixtures/real-bugs/sql_injection.py | security / major+ (SQL injection) |
| traps/parameterized_query.py | fixtures/traps/parameterized_query.py | (none — parameterized is safe) |
| clean/correct.py | fixtures/clean/correct.py | (none — Approve) |

See `.claude/skills/review-code/SKILL.md` Step 3 (verifier pass).
