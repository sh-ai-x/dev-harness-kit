# Expected outcomes (source of truth for /dev-kit:review regression)

| fixture | category | expected verdict |
|---|---|---|
| real-bugs/sql_injection.py | security | major+ (or higher) |
| traps/parameterized_query.py | (none — safe) | (no findings) |
| clean/addition.py | (none — clean) | Approve |

Drive with: `bash fixtures/check.sh {real-bugs|traps|clean}`
Then run `/dev-kit:review fixtures/<category>` and compare.
