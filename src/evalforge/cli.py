"""Command line.

    evalforge run  --dataset data.jsonl --scorers numeric_preservation,repetition --out ./runs/a
    evalforge gate --candidate runs/a/run.json --baseline runs/b/run.json

The gate exits 0 / 1 / 2 — pass, regression, inconclusive — so a CI pipeline can branch on
"we don't know" separately from "it broke".
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from . import runner
from .gate import CategoryRecall, GatePolicy, RunSummary, evaluate, power_report
from .spec import ExecutionPolicy, RunSpec, ScorerRef, sha256_of


def _spec(rows: list[dict], scorer_names: list[str], model_id: str, judge_rate: float) -> RunSpec:
    return RunSpec(
        dataset_hash=runner.dataset_hash(rows),
        scorers=tuple(
            ScorerRef(name=n, version="1.0.0", config={}) for n in sorted(scorer_names)
        ),
        model_id=model_id,
        model_params_hash=sha256_of({"temperature": 0})[:16],
        judge_sample_rate=judge_rate,
    )


def cmd_run(args: argparse.Namespace) -> int:
    rows = runner.load_jsonl(Path(args.dataset))
    spec = _spec(rows, args.scorers.split(","), args.model, args.judge_sample_rate)
    policy = ExecutionPolicy(
        max_cost_inr=args.max_cost, min_coverage=args.min_coverage,
        max_attempts=args.max_attempts,
    )
    outcome = runner.run(rows, spec, policy)
    runner.write_report(outcome, Path(args.out))
    print(json.dumps(
        {
            "run_id": outcome.run_id,
            "digest": outcome.digest,
            "coverage": outcome.metrics["coverage"],
            "spent_inr": outcome.budget["spent"],
            "overrun_inr": outcome.budget["overrun"],
            "errors": len(outcome.errors),
        },
        indent=2,
    ))
    return 0


def _summary(path: str) -> RunSummary:
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    m = data["metrics"]
    recall: dict[str, CategoryRecall] = {}
    for cat, counts in (m.get("recall_counts") or {}).items():
        recall[cat] = CategoryRecall(int(counts[0]), int(counts[1]))
    return RunSummary(
        run_id=data["run_id"],
        coverage=float(m.get("coverage", 0.0)),
        recall=recall,
        false_alarm_rate=float(m.get("false_alarm_rate", 0.0)),
        overrun_inr=float((data.get("budget") or {}).get("overrun", 0.0)),
    )


def cmd_gate(args: argparse.Namespace) -> int:
    result = evaluate(
        _summary(args.candidate),
        _summary(args.baseline),
        GatePolicy(min_coverage=args.min_coverage, tolerance=args.recall_tolerance),
    )
    print(json.dumps(result.to_dict(), indent=2))
    return result.exit_code


def cmd_power(args: argparse.Namespace) -> int:
    print(json.dumps(power_report(args.baseline, args.n), indent=2))
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="evalforge", description=__doc__)
    sub = p.add_subparsers(dest="command", required=True)

    r = sub.add_parser("run", help="score a dataset")
    r.add_argument("--dataset", required=True)
    r.add_argument("--scorers", default="empty_output,numeric_preservation,repetition,length_ratio")
    r.add_argument("--model", default="none")
    r.add_argument("--max-cost", type=float, default=0.0)
    r.add_argument("--min-coverage", type=float, default=0.0)
    r.add_argument("--max-attempts", type=int, default=3)
    r.add_argument("--judge-sample-rate", type=float, default=0.0)
    r.add_argument("--out", default="./runs/latest")
    r.set_defaults(func=cmd_run)

    g = sub.add_parser("gate", help="compare a run against a baseline")
    g.add_argument("--candidate", required=True)
    g.add_argument("--baseline", required=True)
    g.add_argument("--min-coverage", type=float, default=0.98)
    g.add_argument("--recall-tolerance", type=float, default=0.02)
    g.set_defaults(func=cmd_gate)

    pw = sub.add_parser("power", help="what regression can this sample size actually detect?")
    pw.add_argument("--n", type=int, required=True, help="items per arm")
    pw.add_argument("--baseline", type=float, required=True, help="baseline recall, e.g. 0.90")
    pw.set_defaults(func=cmd_power)
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return int(args.func(args))


if __name__ == "__main__":
    sys.exit(main())
