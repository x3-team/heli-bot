"""python -m evals --agent golden|noop|baseline|module:func [--only ids] [--out dir] [--gate 0.7]"""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

from evals.agents import resolve_agent
from evals.runner import load_scenarios, run_scenario, to_json, to_markdown


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m evals")
    parser.add_argument("--agent", default="golden")
    parser.add_argument("--only", help="id сценариев или категории через запятую")
    parser.add_argument("--out", type=Path, help="каталог для report.json и report.md")
    parser.add_argument("--gate", type=float, default=0.0, help="минимальная доля успеха, иначе код 1")
    args = parser.parse_args(argv)

    factory = resolve_agent(args.agent)
    scenarios = load_scenarios(only=args.only)
    results = []
    for scenario in scenarios:
        result = run_scenario(scenario, factory(scenario))
        mark = "ok  " if result.success else "FAIL"
        print(f"{mark} {result.id:<14} шагов={result.steps:<2} ₽={result.cost_rub:<7.3f} "
              f"stop={result.stopped} {'; '.join(result.failures + result.violations)}", flush=True)
        results.append(result)

    meta = {"agent": args.agent, "alias": os.environ.get("HELI_EVAL_ALIAS", "heli-fast"),
            "at": datetime.now(timezone.utc).isoformat(timespec="seconds"), "commit": os.environ.get("GITHUB_SHA", "")}
    report = to_json(results, meta)
    markdown = to_markdown(results, meta)
    if args.out:
        args.out.mkdir(parents=True, exist_ok=True)
        (args.out / "report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        (args.out / "report.md").write_text(markdown, encoding="utf-8")
    summary = report["summary"]
    print(f"\nуспех {summary['passed']}/{summary['total']} ({summary['success_rate']:.0%}), "
          f"шагов в среднем {summary['avg_steps']}, ₽ {summary['cost_rub_total']:.2f}, "
          f"остановки {summary['stops']}, нарушений {summary['safety_violations']}")
    return 1 if summary["success_rate"] < args.gate else 0


if __name__ == "__main__":
    sys.exit(main())
