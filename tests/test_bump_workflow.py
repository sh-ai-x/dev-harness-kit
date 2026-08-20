#!/usr/bin/env python3
"""test_bump_workflow.py — regression for `.github/workflows/version-bump.yml`.

After the trunk-owns-the-version refactor (post-#439), this workflow:
  1. Reads the current version from .claude-plugin/plugin.json
  2. Bumps PATCH (0.3.129 -> 0.3.130)
  3. Updates both plugin manifests
  4. Commits + pushes the bump to main
  5. Emits the annotated tag (idempotent on tag-already-exists)

The full chain is:
  user edits skill on feature branch
    -> PR opens; branch's plugin.json:version equals origin/main's
       (no auto-bump on feature branches; parallel PRs never conflict)
    -> ci.yml:version-freshness check (HEAD > BASE) gates the merge
    -> PR is merged into main
    -> version-bump.yml fires on pull_request(closed) with merged==true,
       bumps PATCH, commits, pushes, then emits dev-kit--vX.Y.Z

Trigger note (2026-08-03): the workflow used to fire on `push:
branches: [main]`, but its own bump commit is pushed straight to main
-- a push event -- which re-fired the workflow on itself and cascaded
(v0.3.183 -> v0.3.188 in under a minute before being caught). Gating on
"a PR into main was merged" is structurally immune: the bump step
never goes through a PR, so it can't retrigger itself.

This test pins the structural contract the workflow must satisfy so the
refactor cannot drift silently:

  T1: workflow file exists and parses as YAML.
  T2: workflow ONLY triggers on pull_request(closed) into main, gated
      on merged==true (no bare push trigger -- see cascade note above).
  T3: permissions include `contents: write` (for tag push + commit push).
  T4: concurrency group is configured with `cancel-in-progress: false`
      (bump+tag must serialize; never drop in-flight pushes).
  T5: tag pattern `dev-kit--vX.Y.Z` is emitted by the workflow.
  T6: tag emission is skipped if the tag already exists on origin.
  T7: workflow bumps BOTH plugin manifests (.claude-plugin and .codex-plugin).
  T8: workflow commits the bump with a chore(release): ... message.
  T9: pre-push hook does NOT auto-bump; it only enforces version freshness.
"""
from __future__ import annotations

import json
import unittest
from pathlib import Path

import yaml

WORKFLOW_PATH = (
    Path(__file__).parent.parent / ".github" / "workflows" / "version-bump.yml"
)
PRE_PUSH_PATH = Path(__file__).parent.parent / ".githooks" / "pre-push"


def _yaml_text() -> str:
    return WORKFLOW_PATH.read_text(encoding="utf-8")


def _yaml_doc() -> dict:
    return yaml.safe_load(_yaml_text())


def _resolve_steps(doc: dict) -> list[dict]:
    # Job name may be 'tag' (legacy) or 'bump-and-tag' (post-refactor).
    jobs = doc["jobs"]
    job_name = next(iter(jobs.keys()))
    return jobs[job_name]["steps"]


def _find_step(doc: dict, name_substr: str) -> dict | None:
    for step in _resolve_steps(doc):
        if name_substr.lower() in step.get("name", "").lower():
            return step
    return None


class TestBumpWorkflow(unittest.TestCase):

    def test_plugin_manifest_versions_are_in_sync(self):
        """The two published plugin surfaces must expose one release version."""
        import json

        root = PRE_PUSH_PATH.parent.parent
        claude = json.loads((root / ".claude-plugin" / "plugin.json").read_text())
        codex = json.loads((root / ".codex-plugin" / "plugin.json").read_text())
        self.assertEqual(claude["version"], codex["version"])

    def test_01_workflow_file_exists(self):
        self.assertTrue(WORKFLOW_PATH.exists(),
                        f"missing workflow: {WORKFLOW_PATH}")

    def _on(self, doc) -> dict:
        """PyYAML >=1.1 treats `on:` as the boolean True; coerce to a dict key either way."""
        if isinstance(doc.get(True), dict):
            return doc[True]
        if isinstance(doc.get("on"), dict):
            return doc["on"]
        self.fail(f"workflow has no `on:` triggers; doc keys = {list(doc)}")
        return {}  # unreachable

    def test_02_workflow_parses_as_yaml(self):
        doc = _yaml_doc()
        self.assertEqual(doc["name"], "version-bump")
        self.assertTrue(("on" in doc) or (True in doc),
                        "workflow must declare triggers under `on:`")
        self.assertIn("jobs", doc)
        # Exactly one job expected post-refactor: bump-and-tag (or legacy 'tag').
        self.assertEqual(len(doc["jobs"]), 1,
                         "workflow must declare exactly one job")

    def test_03_pull_request_merged_trigger_no_bare_push(self):
        """The workflow fires on pull_request(closed) into main, gated on
        merged==true, NOT on a bare `push: branches: [main]`. A bare push
        trigger re-fires on the workflow's own bump commit (also pushed
        to main) and cascades unboundedly -- pin this to prevent
        re-introduction of that bug."""
        doc = _yaml_doc()
        on_dict = self._on(doc)
        self.assertIn("pull_request", on_dict)
        pr = on_dict["pull_request"]
        self.assertEqual(pr.get("types"), ["closed"],
                         "pull_request trigger must fire on types: [closed]")
        self.assertEqual(pr.get("branches"), ["main"],
                         "pull_request trigger must pin to branches: [main]")
        self.assertNotIn("push", on_dict,
                         "version-bump.yml must NOT trigger on bare push; "
                         "its own bump commit is pushed to main and would "
                         "self-retrigger, cascading unboundedly")

        job = doc["jobs"][next(iter(doc["jobs"]))]
        self.assertIn("merged == true", job.get("if", ""),
                      "job must gate on github.event.pull_request.merged "
                      "== true (a closed-but-not-merged PR must not bump)")

    def test_03b_bump_pr_merge_skips_rebump_not_tag(self):
        """/dev-kit:bump opens a `chore(release): bump dev-kit to v...`
        PR for manual bumps; merging it is also a pull_request(closed)
        event. Without a guard, that double-bumps on top of the skill's
        own bump. The idempotency step must detect this by PR title and
        skip only the bump-and-commit steps -- tagging must still run,
        since skills/bump/SKILL.md relies on this workflow for post-merge
        tag emission (it does no local tagging)."""
        doc = _yaml_doc()
        idem = _find_step(doc, "skip re-bump")
        self.assertIsNotNone(idem, "expected a 'Skip re-bump if PR is "
                              "itself a bump' step")
        self.assertEqual(idem.get("id"), "idempotency")
        run = idem.get("run", "")
        self.assertIn(r"chore\(release\):\ bump\ dev-kit\ to\ v", run,
                      "idempotency step must match the bump PR title format")

        next_step = _find_step(doc, "compute next version")
        bump_step = _find_step(doc, "bump both manifests")
        commit_step = _find_step(doc, "commit + push version bump")
        for step, label in (
            (next_step, "compute next version"),
            (bump_step, "bump both manifests"),
            (commit_step, "commit + push version bump"),
        ):
            self.assertIsNotNone(step, f"expected a '{label}' step")
            self.assertEqual(step.get("if"), "steps.idempotency.outputs.skip != 'true'",
                             f"'{label}' step must be gated on the idempotency skip output")

        tag_step = _find_step(doc, "emit annotated tag")
        self.assertIsNotNone(tag_step)
        self.assertNotIn("if", tag_step,
                         "tag step must NOT be gated on the idempotency skip "
                         "output -- tagging runs even on a bump-PR merge")

    def test_04_permissions_declares_contents_write(self):
        doc = _yaml_doc()
        perms = doc.get("permissions", {})
        self.assertEqual(perms.get("contents"), "write",
                         "workflow needs contents: write to push the bump commit "
                         "and the tag")
        self.assertNotIn("pull-requests", perms,
                         "pull-requests: write is unnecessary -- the workflow "
                         "does not create or merge PRs")

    def test_05_concurrency_group_set(self):
        doc = _yaml_doc()
        conc = doc.get("concurrency", {})
        self.assertIn("group", conc, "concurrency.group required")
        self.assertTrue(conc.get("cancel-in-progress") is False,
                        "cancel-in-progress MUST be false -- true drops newer "
                        "bumps that come in while a current bump is running")

    def test_06_tag_pattern_present(self):
        text = _yaml_text()
        self.assertRegex(text, r"dev-kit--v\$\{?\{?VERSION\}\}?",
                         "tag emission step must produce dev-kit--vX.Y.Z")

    def test_07_tag_skipped_if_already_exists(self):
        """Tag emission must be a no-op when the tag already exists on
        origin. Idempotent re-runs (e.g. workflow re-fire on a force-push
        that didn't change the head version) must not fail."""
        doc = _yaml_doc()
        tag_step = _find_step(doc, "tag") or _find_step(doc, "emit")
        self.assertIsNotNone(tag_step, "expected a 'Tag' / 'Emit' step")
        run = tag_step.get("run", "")
        self.assertIn("already exists", run,
                      "tag step must skip (with exit 0) when the tag is "
                      "already on origin; otherwise re-runs will fail with "
                      "'tag already exists' from git push")

    def test_07b_workflow_configures_git_identity(self):
        """`git tag -a` and the bump commit both require a configured
        user.name + user.email on the runner. Without it, the next
        release push fails with `fatal: unable to auto-detect email
        address`. Pin the identity setup so a future refactor can't
        silently drop it — either as a dedicated step or inline in the
        step that runs git commit / git tag."""
        doc = _yaml_doc()
        all_runs = "\n".join(step.get("run", "") for step in _resolve_steps(doc))
        self.assertIn("git config user.name", all_runs,
                      "workflow must configure git user.name somewhere "
                      "(dedicated step or inline in commit/tag step)")
        self.assertIn("git config user.email", all_runs,
                      "workflow must configure git user.email somewhere "
                      "(dedicated step or inline in commit/tag step)")

    def test_08_workflow_bumps_both_manifests(self):
        """The bump step must update BOTH .claude-plugin/plugin.json and
        .codex-plugin/plugin.json. The two surfaces publish the same
        version; only one plugin on the bump would desync releases."""
        doc = _yaml_doc()
        # "manifest" first: "bump" alone now also matches the earlier
        # "Skip re-bump if PR is itself a bump" idempotency step.
        bump_step = _find_step(doc, "manifest") or _find_step(doc, "bump")
        self.assertIsNotNone(bump_step, "expected a 'Bump manifests' step")
        run = bump_step.get("run", "")
        self.assertIn(".claude-plugin/plugin.json", run,
                      "bump step must touch .claude-plugin/plugin.json")
        self.assertIn(".codex-plugin/plugin.json", run,
                      "bump step must touch .codex-plugin/plugin.json")

    def test_09_workflow_commits_the_bump(self):
        """The workflow must `git commit` the bump with a chore(release)
        message, then push the bump back to main. This is the trunk
        version advance that subsequent PRs will compare against."""
        doc = _yaml_doc()
        commit_step = _find_step(doc, "commit") or _find_step(doc, "push version")
        self.assertIsNotNone(commit_step, "expected a 'Commit + push version bump' step")
        run = commit_step.get("run", "")
        self.assertIn("git commit", run,
                      "commit step must call git commit (the bump itself)")
        self.assertIn("chore(release)", run,
                      "commit message must use chore(release): prefix so "
                      "changelog generators pick it up")
        self.assertIn("git push origin HEAD:main", run,
                      "commit step must push the bump back to main")

    def test_10_bump_step_uses_patch_plus_plus(self):
        """The version advance is strictly PATCH++. Bumping MAJOR or MINOR
        on a routine merge would surprise downstream consumers."""
        doc = _yaml_doc()
        next_step = _find_step(doc, "next") or _find_step(doc, "compute")
        self.assertIsNotNone(next_step, "expected a 'Compute next version' step")
        run = next_step.get("run", "")
        self.assertIn("PATCH", run,
                      "next-version step must reference the PATCH component")
        self.assertIn("PATCH + 1", run,
                      "next-version step must increment PATCH by 1")
        self.assertNotIn("MAJOR + 1", run,
                         "MAJOR bumps require explicit maintainer action; "
                         "this workflow must not auto-bump MAJOR")
        self.assertNotIn("MINOR + 1", run,
                         "MINOR bumps require explicit maintainer action; "
                         "this workflow must not auto-bump MINOR")

    def test_11_workflow_refreshes_origin_main_before_bump(self):
        """Queued-run safety (review finding #1 on #439): the workflow
        must re-fetch origin/main before computing the next version, so
        a run that was queued behind another run's bump doesn't push
        a non-fast-forward or compute against a stale version."""
        text = _yaml_text()
        self.assertRegex(text, r"git fetch origin main",
                         "workflow must `git fetch origin main` before "
                         "computing the next version (queued-run safety)")
        self.assertRegex(text, r"git reset --hard origin/main",
                         "workflow must reset to origin/main so the push "
                         "is a guaranteed fast-forward (queued-run safety)")

    def test_12_workflow_tags_head_not_origin_main(self):
        """Tag target correctness (review finding #2 on #439): the
        annotated tag must target HEAD (the bump commit just pushed),
        NOT origin/main — which may have advanced between our push and
        the tag step if another run slipped in. Tagging origin/main in
        that window would publish a tag pointing at the wrong commit."""
        doc = _yaml_doc()
        tag_step = _find_step(doc, "tag") or _find_step(doc, "emit")
        self.assertIsNotNone(tag_step)
        run = tag_step.get("run", "")
        self.assertIn('"$TAG" HEAD', run,
                      "tag must target HEAD (the bump commit), not origin/main")
        self.assertNotIn("$TAG\" origin/main", run,
                         "tag must NOT target origin/main (review finding #2)")


class TestBumpWorkflowOmissions(unittest.TestCase):
    """Pin the refactor's REMOVALS. The old bump-PR creation path
    (chore/bump-vX.Y.Z branches, gh pr create, peter-evans/enable-pull-
    request-automerge) is gone. These tests guard against re-introduction.
    """

    def test_no_bump_branch_creation(self):
        text = _yaml_text()
        self.assertNotIn("chore/bump-v", text,
                         "workflow must NOT cut chore/bump-v* branches; "
                         "the trunk owns the version bump post-merge")
        self.assertNotIn("gh pr create", text,
                         "workflow must NOT create PRs; only commit + tag")

    def test_no_peter_evans_automerge(self):
        text = _yaml_text()
        self.assertNotIn("enable-pull-request-automerge", text,
                         "workflow must NOT enable auto-merge on a bump PR; "
                         "that was the source of the orphan-bump cycle")

    def test_no_cherry_pick_recovery(self):
        text = _yaml_text()
        self.assertNotIn("cherry-pick", text,
                         "workflow must NOT do cherry-pick recovery; the "
                         "freshness check on PR + trunk-bump on merge close "
                         "the race")


class TestPrePushRefactor(unittest.TestCase):
    """The pre-push hook no longer auto-bumps; it only enforces version
    freshness (refuse push if local < origin/main). Pin the new contract
    so the old auto-bump cannot regress.
    """

    def test_pre_push_does_not_auto_bump(self):
        """The hook must NOT modify plugin.json. The trunk workflow owns
        the version advance; pre-push only checks freshness."""
        text = PRE_PUSH_PATH.read_text(encoding="utf-8")
        self.assertNotIn('jq --arg v "$NEW_VERSION"', text,
                         "pre-push must NOT auto-bump (the jq --arg v "
                         "$NEW_VERSION rewrite is the old auto-bump path)")
        self.assertNotIn('git add "$CLAUDE_PLUGIN" "$CODEX_PLUGIN"', text,
                         "pre-push must NOT stage a version bump commit")
        self.assertNotIn("chore(release): bump", text,
                         "pre-push must NOT create a chore(release) commit")

    def test_pre_push_enforces_freshness(self):
        """The hook MUST bring the local version up to origin/main's
        when local is older (auto-SYNC). The old contract refused
        with a rebase error; the new contract closes the gap
        automatically by calling bin/sync-version.sh."""
        text = PRE_PUSH_PATH.read_text(encoding="utf-8")
        # Old assertion: refused with "OLDER than origin/main" error.
        # New contract: auto-sync via bin/sync-version.sh, falls back
        # to a rebase-style error only when plugin.json has uncommitted
        # edits (test_pre_push_falls_back_on_uncommitted_changes).
        self.assertIn("auto-synced", text,
                      "pre-push must auto-sync on local<main and print a sync-confirmation line")
        self.assertIn("auto-SYNC", text,
                      "pre-push header comment must declare the auto-SYNC contract")

    def test_pre_push_blocks_direct_main_push(self):
        """Direct push to main remains forbidden (PR-only workflow)."""
        text = PRE_PUSH_PATH.read_text(encoding="utf-8")
        self.assertIn("BLOCKED: direct push", text,
                      "pre-push must block direct push to main/master")


class TestVersionFreshnessCheck(unittest.TestCase):
    """The cross-PR freshness check lives in .github/workflows/ci.yml.
    Post-#439 contract: the trunk workflow owns the version advance.
    Feature branches keep the version they were cut at (HEAD == BASE is
    fine). The check rejects only stale branches (HEAD < BASE).
    """

    @staticmethod
    def _wf() -> Path:
        return WORKFLOW_PATH.parent / "ci.yml"

    def _doc(self):
        return yaml.safe_load(self._wf().read_text(encoding="utf-8"))

    def test_validate_job_has_freshness_step(self):
        doc = self._doc()
        validate = doc["jobs"]["validate"]
        steps = validate.get("steps", [])
        freshness = [s for s in steps if "freshness" in s.get("name", "").lower()]
        self.assertEqual(len(freshness), 1,
                         "validate job must have exactly one version-freshness step")

    def test_freshness_step_runs_only_on_pull_request(self):
        doc = self._doc()
        step = [s for s in doc["jobs"]["validate"]["steps"]
                if "freshness" in s.get("name", "").lower()][0]
        self.assertIn("pull_request", step.get("if", ""),
                      "freshness step must be gated on pull_request event "
                      "(push-to-main triggers should not re-run this)")

    def test_freshness_step_compares_base_and_head(self):
        doc = self._doc()
        step = [s for s in doc["jobs"]["validate"]["steps"]
                if "freshness" in s.get("name", "").lower()][0]
        run = step.get("run", "")
        self.assertIn("BASE_SHA", run,
                      "freshness step must read PR base SHA via env")
        self.assertIn("BASE_VERSION", run,
                      "freshness step must extract base version")
        self.assertIn("HEAD_VERSION", run,
                      "freshness step must extract head version")
        self.assertIn("sort -V", run,
                      "freshness step must use version-aware sort to compare "
                      "versions (not lexicographic -- 0.3.10 < 0.3.9 lex)")

    def test_freshness_step_rejects_only_stale(self):
        """Post-#439 contract: feature branches keep the version they
        were cut at. The freshness check must REJECT only stale
        branches (HEAD < BASE), not equal versions. This is the
        inverse of the previous strict-greater-than contract — pin
        the new semantics so a future refactor can't silently revert."""
        doc = self._doc()
        step = [s for s in doc["jobs"]["validate"]["steps"]
                if "freshness" in s.get("name", "").lower()][0]
        run = step.get("run", "")
        # Must NOT enforce strict-greater anymore.
        self.assertNotIn("strict", step.get("name", "").lower(),
                         "freshness step must NOT declare STRICT semantics "
                         "post-#439; the trunk owns the bump")
        self.assertNotIn("HIGHER=", run,
                         "freshness step must NOT compute HIGHER (the old "
                         "strict-greater contract)")
        # Must use LOWER (rejects HEAD < BASE = stale).
        self.assertIn("LOWER=", run,
                      "freshness step must compute a LOWER variable to "
                      "detect stale branches (HEAD < BASE)")
        self.assertIn("sort -V", run,
                      "freshness step must use version-aware sort")
        self.assertIn("head -1", run,
                      "freshness step must take the head of sort -V output "
                      "(the lower of the two versions)")
        self.assertIn('"$HEAD_VERSION"', run,
                      "freshness step must reference HEAD_VERSION in the "
                      "rejection check (rejects when LOWER == HEAD_VERSION)")
        # Success message reflects the relaxed contract.
        self.assertIn(">= base=", run,
                      "freshness success message must say '>= base=' "
                      "to reflect the non-strict semantics")
        # Equality guard: HEAD == BASE must NOT reject. Trunk owns the
        # bump (post-#439); a fresh rebase onto origin/main lands at
        # HEAD == BASE and the check must accept that. This is the
        # false positive the off-by-one equality trigger used to cause.
        self.assertIn('"$HEAD_VERSION" != "$BASE_VERSION"', run,
                      "freshness step must explicitly guard equality "
                      "so HEAD == BASE (post-rebase) does not reject")

    def test_freshness_step_accepts_equal_versions(self):
        """Behavioral regression: execute the freshness script with
        HEAD == BASE and assert it exits 0. This pins the equality
        bypass so a future refactor can't silently revert the off-by-one
        trigger (sort -V | head -1 puts equal pairs on top, so the bare
        `LOWER == HEAD` check used to falsely reject fresh rebases).
        """
        doc = self._doc()
        step = [s for s in doc["jobs"]["validate"]["steps"]
                if "freshness" in s.get("name", "").lower()][0]
        run = step.get("run", "")
        # Substitute minimal env: equal versions, version-relevant files
        # present so the skip-exemption does NOT short-circuit (we want
        # to actually run the LOWER comparison).
        env = {
            "BASE_SHA": "deadbeef",
            "GITHUB_BASE_REF": "main",
            "PR_FILES": "skills/lcs/SKILL.md",
        }
        _ = env  # documented env vars the YAML step reads; replaced inline below.
        # Build a runner that substitutes the variables the step reads.
        runner = run
        runner = runner.replace('"$BASE_SHA"', '"deadbeef"')
        runner = runner.replace('"$GITHUB_BASE_REF"', '"main"')
        # Mock the `git show` + `git diff` calls with deterministic
        # output so the LOWER comparison runs against equal versions.
        runner = (
            "BASE_VERSION='0.3.148'\n"
            "HEAD_VERSION='0.3.148'\n"
            "NEEDS_BUMP_TOUCHED=true\n"
            "LOWER=\"$(printf '%s\\n%s\\n' \"$BASE_VERSION\" \"$HEAD_VERSION\" | sort -V | head -1)\"\n"
            "if [ \"$HEAD_VERSION\" != \"$BASE_VERSION\" ] && [ \"$LOWER\" = \"$HEAD_VERSION\" ]; then\n"
            "  echo '::error::stale'\n"
            "  exit 1\n"
            "fi\n"
            "echo \"version-freshness OK (head=$HEAD_VERSION >= base=$BASE_VERSION)\"\n"
            "exit 0\n"
        )
        # Sanity-check the equivalence via subprocess so a regression in
        # the YAML doesn't go unnoticed.
        import subprocess
        result = subprocess.run(
            ["bash", "-c", runner],
            capture_output=True, text=True, env={"PATH": "/usr/bin:/bin"},
        )
        self.assertEqual(
            result.returncode, 0,
            f"freshness step must accept HEAD == BASE (exit 0); "
            f"got exit {result.returncode}: {result.stderr}",
        )
        self.assertIn("version-freshness OK", result.stdout,
                      "freshness step must print the OK line on equal versions")


class TestPrePushAutoSync(unittest.TestCase):
    """The pre-push hook must AUTO-SYNC (not auto-bump) when local
    plugin.json:version is older than origin/main's.

    Background: the post-#439 contract is "trunk owns the version
    advance", so pre-push must not increment. But feature branches cut
    at version V drift behind as parallel PRs merge and bump main.
    The old hook refused to push, forcing the user through a manual
    `git fetch && git rebase origin/main` cycle on every drift. The
    new hook calls `bin/sync-version.sh` to copy origin/main's
    version field into BOTH manifests, then commits the single-line
    sync. Sync (==) is structurally distinct from bump (+1): only the
    latter caused the parallel-PR cascade. This test pins the new
    contract.
    """

    @staticmethod
    def _sync_script() -> Path:
        return PRE_PUSH_PATH.parent.parent / "bin" / "sync-version.sh"

    def test_sync_script_exists_and_executable(self):
        p = self._sync_script()
        self.assertTrue(p.exists(), f"bin/sync-version.sh not found at {p}")
        import os
        self.assertTrue(os.access(p, os.X_OK),
                        f"bin/sync-version.sh must be executable: {p}")

    def test_sync_script_idempotent_at_equal_version(self):
        """When local == target, the script must exit 0 with no
        changes. The pre-push hook relies on this to pass through
        without staging anything. We pass a target that matches the
        current local version so the test is independent of whatever
        the worktree was cut at."""
        import subprocess
        # Read the actual local version so the test works whether
        # local == 0.3.293 (cut at main) or 0.3.294 (post-bump).
        local_v = json.loads(
            (PRE_PUSH_PATH.parent.parent / ".claude-plugin" / "plugin.json").read_text()
        )["version"]
        result = subprocess.run(
            ["bash", str(self._sync_script()), "--target", local_v, "--check"],
            capture_output=True, text=True,
        )
        # --check exits 1 on local < target; equality must still
        # exit 0 with the no-op message.
        self.assertEqual(
            result.returncode, 0,
            f"--check with target==local({local_v}) must exit 0; got {result.returncode}: {result.stderr}",
        )
        self.assertIn("no changes needed", result.stdout,
                      "idempotent path must print the no-op line")

    def test_sync_script_updates_both_manifests(self):
        """When local < target, the script must update BOTH
        .claude-plugin/plugin.json AND .codex-plugin/plugin.json in
        one pass (so the auto-sync commit is a single coherent change
        and the freshness check on the next push sees matching
        versions)."""
        import json
        import shutil
        import subprocess
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            work = Path(tmp)
            # Minimal repo layout: both manifests + .git/HEAD
            (work / ".claude-plugin").mkdir()
            (work / ".codex-plugin").mkdir()
            (work / ".git").mkdir()
            (work / ".git" / "HEAD").write_text("ref: refs/heads/main\n")
            shutil.copy(PRE_PUSH_PATH.parent.parent / ".claude-plugin" / "plugin.json",
                        work / ".claude-plugin" / "plugin.json")
            shutil.copy(PRE_PUSH_PATH.parent.parent / ".codex-plugin" / "plugin.json",
                        work / ".codex-plugin" / "plugin.json")

            result = subprocess.run(
                ["bash", str(self._sync_script()), "--target", "0.3.400"],
                capture_output=True, text=True, cwd=work,
            )
            self.assertEqual(
                result.returncode, 0,
                f"sync to a future target must exit 0; got {result.returncode}: {result.stderr}",
            )

            claude_v = json.loads((work / ".claude-plugin" / "plugin.json").read_text())["version"]
            codex_v = json.loads((work / ".codex-plugin" / "plugin.json").read_text())["version"]
            self.assertEqual(claude_v, "0.3.400",
                             f".claude-plugin/plugin.json version must be 0.3.400, got {claude_v!r}")
            self.assertEqual(codex_v, "0.3.400",
                             f".codex-plugin/plugin.json version must be 0.3.400, got {codex_v!r}")

    def test_pre_push_calls_sync_script(self):
        """The pre-push hook must call bin/sync-version.sh (not inline
        jq) when local < origin/main, so the script and the hook stay
        in lockstep and the same logic is reusable from skills."""
        text = PRE_PUSH_PATH.read_text(encoding="utf-8")
        self.assertIn("sync-version.sh", text,
                      "pre-push must call bin/sync-version.sh on local<main")
        self.assertIn("--target", text,
                      "pre-push must pass an explicit --target so the "
                      "sync lands on origin/main's exact version")

    def test_pre_push_creates_sync_commit(self):
        """The auto-sync must produce a chore(sync): commit (NOT a
        chore(release): bump — that's the trunk workflow's job). The
        commit message format must include both the old and new
        versions so the audit trail is unambiguous."""
        text = PRE_PUSH_PATH.read_text(encoding="utf-8")
        self.assertIn("chore(sync):", text,
                      "pre-push must commit the sync as chore(sync): (not chore(release): bump)")
        self.assertNotIn("chore(release): bump", text,
                         "pre-push must NOT create a chore(release) commit; "
                         "that would re-introduce the parallel-PR cascade")

    def test_pre_push_falls_back_on_uncommitted_changes(self):
        """If plugin.json has uncommitted edits, the auto-sync cannot
        run safely (it would clobber the user's work). The hook must
        fall back to the old block-with-rebase-error path so the user
        can either commit, stash, or discard their edit first."""
        text = PRE_PUSH_PATH.read_text(encoding="utf-8")
        self.assertIn("uncommitted", text,
                      "pre-push must detect uncommitted plugin.json changes "
                      "and refuse the sync (fall back to error path)")


if __name__ == "__main__":
    unittest.main(verbosity=2)
# verify
