#!/usr/bin/env python3
"""eval_gate.py — CI gate wrapper around lib.eval_runner.

Wires `run_eval()` + `run_golden_diff()` into a non-zero exit code
on regression so GitHub Actions can use the script as a hard gate.

Why a wrapper exists:

- `lib.eval_runner` main() prints JSON summary but always exits 0.
- The golden-diff regression verdict lives in `reg["summary"]["ok"]`
  (False when there are critical markers or removed cases).
- Without a non-zero exit, CI cannot block PRs on eval regression.

Stdlib only. Invoked by `.github/workflows/eval.yml`.

Exit codes:
    0 — eval OK (no critical markers, all golden cases still scored)
    1 — eval regression (critical markers OR removed golden cases)
    2 — eval infrastructure failure (import error, eval-runner crash)

Usage::

    python3 tools/eval_gate.py             # all dims, dry-run
    python3 tools/eval_gate.py --no-dry    # call real LLM judge (requires .env)
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

# Make `import eval_runner` AND `from lib.eval import ...` both work.
# - `eval_runner` lives at `lib/eval_runner.py` — need `lib/` on sys.path.
# - `from lib.eval import ...` requires `lib/` to be a package, which means
#   the *parent* of `lib/` must also be on sys.path.
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "lib"))

import eval_runner  # noqa: E402  -- sys.path tweak above is required
import llm_judge  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description="Run eval as a CI gate")
    parser.add_argument("--no-dry", action="store_true",
                        help="call real LLM judge (default: dry-run)")
    parser.add_argument("--dim", choices=eval_runner.SUPPORTED_DIMS,
                        help="restrict to one dim (default: all)")
    parser.add_argument("--case", help="restrict to a single case_id")
    parser.add_argument("--project-root", default=".",
                        help="project root (default: cwd)")
    parser.add_argument("--skip-write-report", action="store_true",
                        help="do not write .dev-kit/regression-report.md")
    args = parser.parse_args()

    root = Path(args.project_root).resolve()

    try:
        report = eval_runner.run_eval(
            root,
            dry_run=not args.no_dry,
            dim=args.dim,
            case=args.case,
        )
        cfg = llm_judge.load_config(root)
        reg = eval_runner.run_golden_diff(root, report, config=cfg)
    except Exception as exc:
        print(json.dumps({"infra_failure": True, "error": str(exc)}, indent=2),
              file=sys.stderr)
        return 2

    if not args.skip_write_report:
        try:
            eval_runner.write_regression_report(root, reg)
        except Exception as exc:
            print(f"warn: write_regression_report failed: {exc}", file=sys.stderr)

    print(json.dumps(reg["summary"], indent=2))

    if reg["summary"]["ok"]:
        return 0
    print(f"eval regression: critical={reg['summary']['critical']} "
          f"removed={reg['summary']['removed_cases']}", file=sys.stderr)
    return 1


if __name__ == "__main__":
    sys.exit(main())
