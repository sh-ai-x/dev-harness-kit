# Workflow scenarios — when the flow doesn't go straight through

**Language:** English

The normal path is a straight line: `bootstrap → plan → build → review → ship`.
Real work rarely stays on that line. You close your laptop mid-build. A plan
turns out to be wrong once you start coding. You want to skip a step you don't
need today.

This page walks through those cases one at a time, with a concrete example for
each. Everything here uses commands and files that exist in the plugin today — no
invented flags.

If you just want the short version, the README's
["When the flow doesn't go straight through"](../../README.md#when-the-flow-doesnt-go-straight-through)
section has a 2-4 sentence pointer for each case.

---

## Quick reference

| Situation | What to do | Where the detail is |
|---|---|---|
| Build stopped partway (you closed the terminal, hit an error, or paused) | Re-run `/dev-kit:build` — it resumes from the first unfinished step | [Case 1](#case-1-a-build-that-stopped-partway) |
| You're back on a different day or a different terminal and lost your place | `/dev-kit:status` plus `phases/<name>/index.json` shows the current worktree state | [Case 2](#case-2-coming-back-from-a-different-terminal-or-day) |
| You want to go straight to Build without a full Plan | Scope Plan tightly, or hand-seed a one-step phase file — there is no bypass flag | [Case 4](#case-4-skipping-straight-to-build-without-a-full-plan) |

---

## Background: how Build tracks its place

Build doesn't hold your progress in memory. It writes it to disk, so it survives
a closed terminal, a crash, or a week away.

When `/dev-kit:plan` runs, it creates a folder `phases/<name>/` containing:

- `index.json` — the list of steps and the **status** of each one.
- `step1.md`, `step2.md`, … — one file per step (what to read, what to do, the
  acceptance criteria, and what *not* to do).

Every step in `index.json` moves through this lifecycle:

```
unimplemented  →  pending  →  in_progress  →  completed
```

Plus two states for runtime trouble: `error` (a step failed) and `blocked` (a
step was paused on purpose).

`/dev-kit:build` always starts at **the first step that isn't `completed`.** That
one fact is what makes every "I got interrupted" case below just work.

---

## Case 1: a build that stopped partway

**Example.** You ran `/dev-kit:plan` for a feature with four steps. You started
`/dev-kit:build`. Step 1 and step 2 finished. Partway through step 3 you closed
your laptop for the night.

The next morning, in the same worktree, you run:

```bash
/dev-kit:build
```

Here's what happens. Build reads `phases/<name>/index.json`, sees step 1 and step
2 are `completed`, and picks up at step 3 — the first step that isn't done. Steps
1 and 2 are not repeated. You do not re-plan, and you do not pass any "resume"
flag; re-running the same command *is* the resume.

This is true no matter why the build stopped — a normal pause, an error on a
step, or the process being killed. Build looks at status, not at how it ended.

**If a step is marked `error`.** A step that failed mid-run is left as `error` in
`index.json`. Re-running `/dev-kit:build` re-attempts it. If it keeps failing for
the same reason, the problem is usually the step itself or the code it depends
on — not the resume mechanism. Read the step file (`phases/<name>/step<N>.md`)
and the step's output (`phases/<name>/step<N>-output.json`) to see what the
acceptance check actually reported.

**How to check where you are** without running anything:

```bash
cat phases/<name>/index.json      # look at each step's "status"
/dev-kit:status                   # a rendered view of loop progress
```

---

## Case 2: coming back from a different terminal or day

You paused a build yesterday and open a fresh terminal today. Start in the
worktree where the change lives, then inspect the durable phase state:

```bash
/dev-kit:status
cat phases/<name>/index.json
```

`/dev-kit:build` resumes from the first step whose status is not `completed`.
If you need a different conversation, use the normal session-history and resume
features provided by the runtime; the repository does not maintain a second
session index.

---

## Case 3: skipping straight to Build without a full Plan

A common wish: "this is tiny, I don't want a whole PRD, let me just build."

**Be clear on what exists.** There is **no one-command bypass flag** that jumps
straight to Build today. Two older shortcut commands that used to do something
like this (`tdd-fast` and `quick-fix`) were **removed** in commit `62d2aa9`
(PR #456). If you see leftover copies of those command files in a local checkout,
they're stale cache artifacts, not supported commands — don't rely on them.

`/dev-kit:build` needs `phases/<name>/index.json` plus per-step files to know what
to build. So your honest options are:

**Option A — scope Plan tightly (recommended).** `/dev-kit:plan` does not have to
produce a large PRD. For a small task, give it a narrow prompt and let it emit a
short plan with one or two steps. This is the normal, supported path and it's
fast for small work. You still get the step tracking that makes Case 1 (resume)
work.

**Option B — hand-seed a minimal phase file.** If you genuinely want to skip the
planning conversation, you can create a minimal `phases/<name>/index.json` with a
single step and its `step1.md` yourself, then run `/dev-kit:build`. This is
manual and unsupported hand-work — the schema has to be valid for build to read
it — so only reach for it if you already know the phase-file format. Option A is
easier and safer for almost everyone.

**Also note:** `/dev-kit:build` refuses to run until `/dev-kit:ci-setup` has been
run in the repo (it checks for `.dev-kit/ci-config.json`). If you're on a brand-new
repo, run `/dev-kit:bootstrap (with ci-setup prompt)` first — that does bootstrap and ci-setup in
one shot.

There is no supported way to have Build write production code with no step file
at all. The step file is what tells the build what "done" means; without it there
is nothing to verify against, which is the whole point of the harness.

---

## See also

- [`docs/stages/STAGES.md`](../stages/STAGES.md) — the full per-stage spec (what
  each of bootstrap / plan / build / review / security / ship must do).
- [`docs/skills/build.md`](../skills/build.md) — the Build skill in detail.
- [`docs/skills/log.md`](../skills/log.md) — optional local telemetry for
  token and skill-usage analysis.
- Main [`README.md`](../../README.md) — install, quickstart, and the short
  version of these scenarios.
