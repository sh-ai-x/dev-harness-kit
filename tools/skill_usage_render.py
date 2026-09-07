"""skill_usage_render.py -- presentation + downstream side-effects.

Three concerns that used to live in ``tools/skill_usage.py``:

1. Table + JSON formatting (``format_table`` / ``format_json``).
2. Per-cwd roll-up filtering (``filter_by_cwd_prefix``).
3. Pipe-into-``dump_usage.py`` for the prune-propose AskUserQuestion
   loop (``_run_propose_delete``).

Re-imported by ``tools/skill_usage.py`` so the public test surface
(``skill_usage.format_table`` / ``skill_usage.format_json`` /
``skill_usage.filter_by_cwd_prefix``) stays importable.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path


def format_table(skills: dict[str, dict],
                 *, top: int | None = None) -> str:
    """Render the aggregate as a fixed-width text table.

    Sorted by ``turns`` descending, ties broken by ``invocations`` desc,
    then by skill name (stable order). Skill name is truncated at 40
    chars -- actual names are ``<plugin>:<skill>`` (typically <30 chars).
    ``last_seen`` is truncated to the minute precision to keep rows
    scannable.
    """
    rows = sorted(skills.items(),
                  key=lambda kv: (-kv[1]["turns"], -kv[1]["invocations"],
                                  kv[0]))
    if top is not None:
        rows = rows[:top]

    name_w = max([8] + [min(40, len(k)) for k, _ in rows])
    headers = (f"{'SKILL':<{name_w}}  {'TURNS':>6}  {'INVOCATIONS':>11}  "
               f"{'LAST_SEEN':<22}")
    sep = "-" * len(headers)
    lines = [headers, sep]
    for name, rec in rows:
        shown = name if len(name) <= name_w else name[: name_w - 1] + "~"
        last = rec.get("last_seen") or "?"
        last_short = last[:19].replace("T", " ") if last != "?" else "?"
        lines.append(f"{shown:<{name_w}}  {rec['turns']:>6}  "
                     f"{rec['invocations']:>11}  {last_short:<22}")
    return "\n".join(lines)


def format_json(skills: dict[str, dict]) -> str:
    """Emit the aggregate as JSON (sorted by turns desc for stable diffs)."""
    ordered = dict(sorted(skills.items(),
                          key=lambda kv: (-kv[1]["turns"],
                                          -kv[1]["invocations"],
                                          kv[0])))
    return json.dumps(ordered, indent=2, sort_keys=False)


def filter_by_cwd_prefix(skills: dict[str, dict], cwd_prefix: str,
                         *, _cwd_matches=None) -> dict[str, dict]:
    """Return a fresh aggregate restricted to skills whose ``cwds`` map
    has at least one entry starting with ``cwd_prefix``.

    The returned dict rolls each surviving cwd's per-skill counts back
    into the top-level counters so callers can render top-N without
    touching the per-cwd breakdown. ``last_seen`` is also rolled up
    as the max across the matching cwds.

    Skills without a ``cwds`` map (i.e. ``include_per_cwd=False``) are
    dropped -- the caller should rerun aggregation with
    ``include_per_cwd=True`` when per-cwd filtering is needed.

    ``_cwd_matches`` is injected so this module stays free of
    ``tools/skill_usage``-internal helpers. ``tools/skill_usage.py``
    binds it at re-export time.
    """
    if _cwd_matches is None:
        raise TypeError(
            "filter_by_cwd_prefix requires _cwd_matches to be injected; "
            "import via tools/skill_usage.py, not directly"
        )
    out: dict[str, dict] = {}
    if not cwd_prefix:
        return out
    for name, rec in skills.items():
        cwds = rec.get("cwds")
        if not cwds:
            continue
        merged = {"turns": 0, "invocations": 0, "last_seen": None}
        for cwd, bucket in cwds.items():
            if not _cwd_matches(cwd, cwd_prefix):
                continue
            merged["turns"] += bucket.get("turns", 0)
            merged["invocations"] += bucket.get("invocations", 0)
            ls = bucket.get("last_seen")
            if ls and (merged["last_seen"] is None or ls > merged["last_seen"]):
                merged["last_seen"] = ls
        if merged["turns"] or merged["invocations"]:
            out[name] = merged
    return out


def _discover_catalog_skills(repo_root: Path) -> list[str]:
    """Return every user-invocable skill's ``<plugin>:<name>`` id from the
    on-disk ``skills/*/SKILL.md`` catalog.

    ``aggregate_skill_usage`` only ever sees a skill that appears in a log
    line, so a skill that has *never once* been invoked has no row at all
    and is invisible to the 0/0 filter in :func:`_run_propose_delete`. This
    walks the catalog directly so those skills get a zero-default row too.

    Model-use sub-skills (``user-invocable: false``, e.g. ``build-tdd``)
    are excluded: they are dispatched implicitly by a sub-agent rather
    than via an explicit ``Skill`` tool_use, so a 0/0 telemetry reading
    for them reflects a dispatch gap, not disuse -- surfacing them here
    would risk proposing deletion of load-bearing build machinery.
    """
    prefix = "dev-kit"
    plugin_json = repo_root / ".claude-plugin" / "plugin.json"
    if plugin_json.is_file():
        try:
            data = json.loads(plugin_json.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            data = None
        if isinstance(data, dict) and data.get("name"):
            prefix = data["name"]

    skills_dir = repo_root / "skills"
    if not skills_dir.is_dir():
        return []

    names: list[str] = []
    for skill_md in sorted(skills_dir.glob("*/SKILL.md")):
        try:
            text = skill_md.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        if not text.startswith("---"):
            continue
        end = text.find("\n---", 3)
        front = text[3:end] if end != -1 else text[3:]

        name = None
        invocable = True
        for line in front.splitlines():
            line = line.strip()
            if line.startswith("name:"):
                name = line.split(":", 1)[1].strip().strip("\"'")
            elif line.startswith("user-invocable:"):
                invocable = line.split(":", 1)[1].strip().lower() != "false"
        if name and invocable:
            names.append(f"{prefix}:{name}")
    return names


def _seed_from_catalog(skills: dict[str, dict],
                       catalog_names: list[str]) -> dict[str, dict]:
    """Seed ``skills`` with one row per catalog name, merging prefixed and
    bare-name telemetry rows.

    Telemetry captures skills under their bare name (e.g. ``worktree-prune``)
    but the catalog lists them under the prefixed form (``dev-kit:worktree-prune``).
    A skill with bare-name usage would otherwise show as 0/0 because the
    prefix-aware candidate filter would never see it. Merge both rows so a
    skill with any dispatch in any name form is recognised as used.

    Returns a fresh dict; ``skills`` is not mutated.
    """
    seeded = dict(skills)
    for name in catalog_names:
        bare = name.split(":", 1)[-1] if ":" in name else name
        merged = {"turns": 0, "invocations": 0, "last_seen": None}
        for src in (skills.get(name), skills.get(bare)):
            if not src:
                continue
            merged["turns"] += src.get("turns", 0)
            merged["invocations"] += src.get("invocations", 0)
            ls = src.get("last_seen")
            if ls and (merged["last_seen"] is None or ls > merged["last_seen"]):
                merged["last_seen"] = ls
        seeded[name] = merged
    return seeded


def _run_propose_delete(skills: dict[str, dict],
                        window: int | None,
                        *,
                        dry_run: bool,
                        here: Path | None = None) -> int:
    """Pipe the 0/0-in-window subset to ``dump_usage.py``.

    The subset is the deterministic gate: skills whose aggregated
    ``turns`` AND ``invocations`` are both 0 within the window. The
    telemetry dict is first seeded with every user-invocable skill from
    the on-disk catalog (see :func:`_discover_catalog_skills`) so a skill
    that has *never* been invoked -- not just one that went quiet -- is
    still a candidate.

    ``dry_run=True`` echoes ``--dry-run`` to dump_usage.py so the
    chat-rendered table is printed without the AskUserQuestion loop.
    Returns dump_usage.py's exit code (0 on a clean loop).

    ``here`` is the tools/ dir; injected so this module stays
    free of __file__-relative paths and is test-friendly.
    """
    here = here or Path(__file__).resolve().parent
    repo_root = here.parent

    seeded = _seed_from_catalog(skills, _discover_catalog_skills(repo_root))

    candidates = sorted(
        name for name, rec in seeded.items()
        if rec.get("turns", 0) == 0 and rec.get("invocations", 0) == 0
    )
    dump_script = repo_root / "skills" / "prune-propose" / "scripts" / "dump_usage.py"
    if not dump_script.is_file():
        print(f"[skill-usage] dump script missing: {dump_script}",
              file=sys.stderr)
        return 2

    import subprocess
    cmd = [sys.executable, str(dump_script),
           "--window-days", str(window if window is not None else 0)]
    if dry_run:
        cmd.append("--dry-run")
    payload = "\n".join(candidates) + ("\n" if candidates else "")
    r = subprocess.run(cmd, input=payload, text=True,
                       capture_output=True, timeout=300)
    if r.stdout:
        sys.stdout.write(r.stdout)
    if r.stderr:
        sys.stderr.write(r.stderr)
    return r.returncode
