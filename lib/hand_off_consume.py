"""hand_off_consume.py — Deterministic SSOT for the plan-skill hand-off consume gate.

Single source of truth for the routing decision issue #898 called out.
Before this module existed, the plan skill's "interview consume gate"
read ``.dev-kit/hand-off/<step>.md`` frontmatter generically and could
pick up a ``sot-harness-*.md`` file (which carries ``status: locked``)
as if it were an interview handoff — failing closed with "interview
held" when the real state was a successful SOT lock.

Public surface
--------------

Discovery (filename-glob discriminator):

- ``discover_interview_handoff(root)`` — find the single
  ``.dev-kit/hand-off/interview-*.md``. Raises ``ValueError`` if more
  than one match; returns ``None`` when missing.
- ``discover_sot_handoff(root)`` — find the single
  ``.dev-kit/hand-off/sot-harness-*.md``. Same semantics.

Validation (typed ``handoff_kind`` discriminator):

- ``parse_yaml_frontmatter(text)`` — tiny YAML-subset parser.
  Returns ``dict`` or ``None`` (no frontmatter). Raises
  ``ValueError`` on malformed input.
- ``validate_interview_handoff(path)`` — verifies the file carries
  ``handoff_kind: interview`` plus a known 5-field status
  (``ok | best-effort | user-acknowledged | held``). Returns
  ``(ok, status, reason)``.
- ``validate_sot_handoff(path)`` — verifies the file carries
  ``handoff_kind: sot`` plus ``status: locked | held``. Returns
  ``(ok, status, reason)``.

Routing:

- ``routing_decision(root, from_sot_arg, *, skip_interview=False)`` —
  the deterministic plan-skill routing table. Returns a ``dict``
  shaped:

  .. code-block:: python

     {
         "path": "interview" | "from_sot" | "skip" | "error",
         "status": str,         # the handoff's status, or "skipped" / "missing" / "misrouted" / "held"
         "handoff_path": Path | None,
         "reason": str,         # human-readable, surfaced verbatim by the plan skill
         "error": bool,         # True iff path == "error"
     }

Why this lives in ``lib/`` and not ``skills/plan/SKILL.md``
----------------------------------------------------------

The plan skill's consume gate is pure LLM instruction; the LLM is
free to read any ``*.md`` and decide what to do. That is exactly
the bug issue #898 reports: the LLM read a SOT file because the
plan skill did not constrain the filename pattern. Putting the
discriminator + validation behind a Python helper pins the
contract in code, makes the four regression scenarios testable,
and lets the plan skill delegate to the helper via a short
"Read the helper, then Read the discovered file" recipe instead
of re-implementing the routing logic in prose.

CLI: not provided. The plan skill drives the consume gate by
calling ``routing_decision`` via the ``Skill`` tool's allowed-tools
surface (``Read Write Glob AskUserQuestion Skill``); today this
helper is invoked through the ``Skill`` mechanism indirectly via
``python -m lib.hand_off_consume`` once a ``main()`` entry is
warranted.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Optional, Tuple

# ----- filename-shape discriminators (the first line of defence) -----

INTERVIEW_HANDOFF_GLOB = "interview-*.md"
SOT_HANDOFF_GLOB = "sot-harness-*.md"

HANDOFF_DIRNAME = "hand-off"
HANDOFF_DIR_PARENTS = (".dev-kit",)  # root / ".dev-kit" / "hand-off"

# ----- typed handoff_kind constants (the second line of defence) -----

INTERVIEW_HANDOFF_KIND = "interview"
SOT_HANDOFF_KIND = "sot"

# Interview status set — matches lib/interview_engine.py:STATUSES plus
# the 5-field frontmatter the plan skill already reads.
INTERVIEW_STATUSES: Tuple[str, ...] = (
    "ok",
    "best-effort",
    "user-acknowledged",
    "held",
)

# SOT status set — matches lib/sot_harness_engine.py's writer-side
# constants (issue #898).
SOT_STATUSES: Tuple[str, ...] = (
    "locked",
    "held",
)

# Routing decision "path" values.
PATH_INTERVIEW = "interview"
PATH_FROM_SOT = "from_sot"
PATH_SKIP = "skip"
PATH_ERROR = "error"


# --------------------------------------------------------------------------- #
# YAML-subset frontmatter parser
# --------------------------------------------------------------------------- #


_FRONTMATTER_RE = re.compile(r"\A---\s*\n(.*?)\n---\s*\n", re.DOTALL)


def parse_yaml_frontmatter(text: str) -> Optional[dict]:
    """Extract a flat string→string frontmatter dict.

    Accepts the leading ``---\\n...\\n---\\n`` fence only. Values are
    stripped of surrounding quotes. Returns ``None`` when no
    frontmatter is present (including the "opened but never closed"
    case — treat malformed input as "no frontmatter" so the caller
    can decide between fail-closed and a soft skip).

    A deliberately tiny subset: keys are flat strings, values are
    strings (no nested types, no list support). The plan/interview/SOT
    contracts only use flat string fields; a real YAML library would
    pull in PyYAML for nothing useful here.
    """
    if not text.startswith("---"):
        return None
    m = _FRONTMATTER_RE.match(text)
    if m is None:
        return None
    block = m.group(1)
    out: dict = {}
    for line in block.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        if ":" not in stripped:
            continue
        key, _, value = stripped.partition(":")
        value = value.strip()
        # Strip a single layer of matching surrounding quotes.
        if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
            value = value[1:-1]
        out[key.strip()] = value
    return out


# --------------------------------------------------------------------------- #
# Hand-off directory + discovery
# --------------------------------------------------------------------------- #


def _hand_off_dir(root: Path) -> Path:
    """Return ``<root>/.dev-kit/hand-off``. Does not require existence."""
    return root.joinpath(*HANDOFF_DIR_PARENTS, HANDOFF_DIRNAME)


def _discover_unique(dirpath: Path, glob: str, *, label: str) -> Optional[Path]:
    """Find the unique match for ``glob`` in ``dirpath``.

    Returns ``None`` when the dir is missing or no file matches.
    Raises ``ValueError`` when more than one match exists — the
    caller (plan skill) must refuse to choose between two
    unverified handoffs.
    """
    if not dirpath.is_dir():
        return None
    matches = sorted(dirpath.glob(glob))
    if not matches:
        return None
    if len(matches) > 1:
        names = ", ".join(m.name for m in matches)
        raise ValueError(
            f"multiple {label} handoffs in {dirpath}: {names}. "
            "Resolve the duplicate before re-invoking /dev-kit:plan."
        )
    return matches[0]


def discover_interview_handoff(root: Path) -> Optional[Path]:
    """Return the unique ``interview-*.md`` in ``.dev-kit/hand-off/`` or ``None``."""
    return _discover_unique(_hand_off_dir(root), INTERVIEW_HANDOFF_GLOB, label="interview")


def discover_sot_handoff(root: Path) -> Optional[Path]:
    """Return the unique ``sot-harness-*.md`` in ``.dev-kit/hand-off/`` or ``None``."""
    return _discover_unique(_hand_off_dir(root), SOT_HANDOFF_GLOB, label="sot-harness")


# --------------------------------------------------------------------------- #
# Validation
# --------------------------------------------------------------------------- #


def _read_frontmatter(path: Path) -> Tuple[Optional[dict], Optional[str]]:
    """Read + parse ``path``'s frontmatter.

    Returns ``(fm, reason)``. ``fm`` is ``None`` when the file is
    missing or has no frontmatter; ``reason`` is non-empty when the
    caller should refuse (missing file / malformed frontmatter).
    """
    if not path.exists():
        return None, f"handoff file not found: {path}"
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        return None, f"handoff file unreadable ({exc}): {path}"
    try:
        fm = parse_yaml_frontmatter(text)
    except ValueError as exc:
        return None, f"malformed frontmatter in {path}: {exc}"
    if fm is None:
        return None, f"no YAML frontmatter in {path}"
    return fm, None


def validate_interview_handoff(path: Path) -> Tuple[bool, str, str]:
    """Validate an interview handoff's typed frontmatter.

    Returns ``(ok, status, reason)``:

    - ``ok`` is ``True`` iff ``handoff_kind == "interview"`` and the
      status is one of ``ok | best-effort | user-acknowledged`` (the
      three proceed-states the plan skill accepts).
    - ``status`` is the raw frontmatter ``status`` value, even when
      invalid (so the plan skill can show the user what was found).
    - ``reason`` is empty on success, otherwise a short human-readable
      string the plan skill surfaces verbatim.
    """
    fm, reason = _read_frontmatter(path)
    if fm is None:
        return False, "missing", reason
    kind = fm.get("handoff_kind", "")
    status = fm.get("status", "")
    if kind != INTERVIEW_HANDOFF_KIND:
        return (
            False,
            status,
            (
                f"{path.name} has handoff_kind={kind!r}; expected "
                f"handoff_kind={INTERVIEW_HANDOFF_KIND!r}. This file is not "
                "an interview handoff; the plan skill must not consume it "
                "through the interview gate. If this is a SOT handoff, "
                "re-invoke with `--from-sot <path>`."
            ),
        )
    if status not in INTERVIEW_STATUSES:
        return (
            False,
            status,
            (
                f"{path.name} status={status!r} is not a recognized "
                "interview status. Expected one of "
                f"{', '.join(INTERVIEW_STATUSES)}."
            ),
        )
    if status == "held":
        return False, status, (
            "interview contract is held (per Phase 6, issue #385). "
            "Re-run /dev-kit:interview to unblock, then re-invoke "
            "/dev-kit:plan."
        )
    return True, status, ""


def validate_sot_handoff(path: Path) -> Tuple[bool, str, str]:
    """Validate a SOT handoff's typed frontmatter.

    Returns ``(ok, status, reason)`` with the same shape as
    ``validate_interview_handoff``. ``ok`` is ``True`` iff
    ``handoff_kind == "sot"`` and ``status == "locked"``.
    """
    fm, reason = _read_frontmatter(path)
    if fm is None:
        return False, "missing", reason
    kind = fm.get("handoff_kind", "")
    status = fm.get("status", "")
    if kind != SOT_HANDOFF_KIND:
        return (
            False,
            status,
            (
                f"{path.name} has handoff_kind={kind!r}; expected "
                f"handoff_kind={SOT_HANDOFF_KIND!r}. This file is not a SOT "
                "handoff; the plan skill must not consume it through "
                "--from-sot. If this is an interview handoff, re-invoke "
                "/dev-kit:plan without --from-sot."
            ),
        )
    if status not in SOT_STATUSES:
        return (
            False,
            status,
            (
                f"{path.name} status={status!r} is not a recognized SOT "
                f"status. Expected one of {', '.join(SOT_STATUSES)}."
            ),
        )
    if status == "held":
        return False, status, (
            "SOT harness is held (one or more rounds failed validation, "
            "see lib/sot_harness_engine.py:validate). Re-run "
            "/dev-kit:sot-harness-writer to complete the missing rounds."
        )
    return True, status, ""


# --------------------------------------------------------------------------- #
# Routing decision — the SSOT consumed by the plan skill
# --------------------------------------------------------------------------- #


def routing_decision(
    root: Path,
    from_sot_arg: Optional[str],
    *,
    skip_interview: bool = False,
) -> dict:
    """Compute the plan-skill consume-gate routing.

    Precedence (top wins):

    1. ``skip_interview`` → ``path="skip"`` (backward compat with the
       existing ``--skip-interview`` flag).
    2. ``from_sot_arg`` → validate the path via
       ``validate_sot_handoff``. On ``ok`` return ``path="from_sot"``;
       on validation failure return ``path="error"`` with an actionable
       ``reason``.
    3. ``discover_interview_handoff(root)`` → validate via
       ``validate_interview_handoff``. On ``ok`` return
       ``path="interview"``; on failure return ``path="error"``.
    4. ``discover_sot_handoff(root)`` is present but no ``--from-sot``
       was given → ``path="error"`` with reason pointing the user at
       ``--from-sot``. **Not** "interview held" — this is the
       discriminator issue #898 mandates.
    5. Nothing on disk → ``path="error"`` with
       ``status="missing"``, pointing the user at ``/dev-kit:interview``.

    Return shape (always the same keys):

    .. code-block:: python

       {
           "path": "interview" | "from_sot" | "skip" | "error",
           "status": str,
           "handoff_path": Optional[Path],
           "reason": str,
           "error": bool,
       }
    """
    if skip_interview:
        return {
            "path": PATH_SKIP,
            "status": "skipped",
            "handoff_path": None,
            "reason": "",
            "error": False,
        }

    if from_sot_arg:
        sot_path = Path(from_sot_arg)
        if not sot_path.exists():
            return {
                "path": PATH_ERROR,
                "status": "misrouted",
                "handoff_path": sot_path,
                "reason": (
                    f"--from-sot points at {sot_path} but the file was "
                    "not found. Verify the path and re-invoke."
                ),
                "error": True,
            }
        ok, status, reason = validate_sot_handoff(sot_path)
        if ok:
            return {
                "path": PATH_FROM_SOT,
                "status": status,
                "handoff_path": sot_path,
                "reason": "",
                "error": False,
            }
        # Validator caught a structural problem (wrong handoff_kind,
        # wrong status, malformed frontmatter). Use the validator's
        # status verbatim; only fall back to "misrouted" when the
        # validator has nothing to say.
        return {
            "path": PATH_ERROR,
            "status": status or "misrouted",
            "handoff_path": sot_path,
            "reason": reason,
            "error": True,
        }

    # Generic plan path: scan ONLY interview-*.md (the SOT handoff is
    # not a valid interview handoff — see issue #898).
    try:
        interview_path = discover_interview_handoff(root)
    except ValueError as exc:
        return {
            "path": PATH_ERROR,
            "status": "duplicate",
            "handoff_path": None,
            "reason": str(exc),
            "error": True,
        }
    if interview_path is not None:
        ok, status, reason = validate_interview_handoff(interview_path)
        if ok:
            return {
                "path": PATH_INTERVIEW,
                "status": status,
                "handoff_path": interview_path,
                "reason": "",
                "error": False,
            }
        return {
            "path": PATH_ERROR,
            "status": status,
            "handoff_path": interview_path,
            "reason": reason,
            "error": True,
        }

    # No interview handoff — check whether a SOT handoff exists, so the
    # user gets an actionable hint instead of "interview held".
    try:
        sot_path = discover_sot_handoff(root)
    except ValueError as exc:
        return {
            "path": PATH_ERROR,
            "status": "duplicate",
            "handoff_path": None,
            "reason": str(exc),
            "error": True,
        }
    if sot_path is not None:
        ok, status, reason = validate_sot_handoff(sot_path)
        if ok:
            # Issue #898 — the SOT handoff is locked but the operator did
            # NOT pass --from-sot. Surface as a routing error, not as an
            # "interview held" event. The plan skill's previous behaviour
            # was to misclassify this and refuse to plan.
            return {
                "path": PATH_ERROR,
                "status": "misrouted",
                "handoff_path": sot_path,
                "reason": (
                    f"found SOT handoff at {sot_path} but the plan skill "
                    "was invoked without --from-sot. Re-invoke with "
                    f"`/dev-kit:plan --from-sot {sot_path}` to consume it, "
                    "or run /dev-kit:interview first to produce an "
                    "interview handoff for the generic plan path."
                ),
                "error": True,
            }
        return {
            "path": PATH_ERROR,
            "status": status or "misrouted",
            "handoff_path": sot_path,
            "reason": reason,
            "error": True,
        }

    return {
        "path": PATH_ERROR,
        "status": "missing",
        "handoff_path": None,
        "reason": (
            "no interview handoff at .dev-kit/hand-off/interview-*.md. "
            "Run /dev-kit:interview <plan-file> to produce one, then "
            "re-invoke /dev-kit:plan. (Pass --skip-interview to bypass "
            "this gate for backward compat.)"
        ),
        "error": True,
    }


__all__ = [
    "INTERVIEW_HANDOFF_GLOB",
    "SOT_HANDOFF_GLOB",
    "INTERVIEW_HANDOFF_KIND",
    "SOT_HANDOFF_KIND",
    "INTERVIEW_STATUSES",
    "SOT_STATUSES",
    "PATH_INTERVIEW",
    "PATH_FROM_SOT",
    "PATH_SKIP",
    "PATH_ERROR",
    "parse_yaml_frontmatter",
    "discover_interview_handoff",
    "discover_sot_handoff",
    "validate_interview_handoff",
    "validate_sot_handoff",
    "routing_decision",
]
