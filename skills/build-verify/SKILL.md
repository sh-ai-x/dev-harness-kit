---
name: build-verify
category: build
description: verification-before-completion. 인용된 exit code + test count + build log 없이 "done" ❌ (MUST-L3, hook stop-verify).
when_to_use: |
  - User types "됐어" / "끝" / "통과" (declaration phrases)
allowed-tools: Read Bash
disallowed-tools: Write Edit WebFetch
model: haiku
---

# build-verify — Evidence-Before-Done

## Iron Law (예외 없음)
**No "done" without running the actual command and quoting evidence.**

## 검증 우선순위 (강→약)

```
1. exit code (test runner / linter / build)
2. test count (passed/failed/skipped)
3. lint count
4. runtime log (last 30 lines)
5. file path + line number 인용
```

## 규칙

- "should work" / "probably fine" / "됐어" ❌
- "통과" 발언 시 자동 stop-verify hook stderr 경고:
  > [verify-gate] You said it works but cited no output/exit/test evidence.
  > [verify-gate] Run the verify command and quote the output.
- 2-layer (MUST-9): (a) hook 자동 검사 + (b) skill 조언
- 회귀 fixture (`fixtures/real-bugs/`) 자동 검증 권장

## Hook 자동 정렬

Build / Review / Security / Ship 단계에서 `stop-verify.sh` 자동 활성. 사용자가 "완료" / "끝" 발언 시 hook이 stop event 받고 verify 명령 실행 + 결과 stderr 출력.

## 사용 예

```
User: "TDD 끝났어"
Hook: [stop-verify] running pytest...
       passed 12, failed 0, error 0
       OK — quoted 12/12.
       Iron Law L3 verified.
```

OR (실패):

```
User: "됐어"
Hook: [verify-gate] You said it works but cited no output/exit/test evidence.
       Run the verify command and quote the output.
```

## Hand-off

검증 통과 → `state_codec.transition_stage(root, "review")` 자동. 실패 → build-engine으로 회귀 (re-iterate).
