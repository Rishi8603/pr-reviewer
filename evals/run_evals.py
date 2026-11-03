"""
Runs the golden dataset against the real model and scores the results.

    python -m evals.run_evals --label my-change --baseline evals/results/after-fix.json

Needs GOOGLE_API_KEY. Results are written after every call, so a run stopped by
the daily quota can carry on later with --resume instead of paying for it twice.
Exits with status 1 if a metric regressed against the baseline.
"""

import argparse
import json
import sys
import time
from pathlib import Path

from evals.golden_cases import ALL_CASES, GoldenCase
from evals.metrics import make_test_case, metrics_for
from evals.report import build_scorecard, find_regressions, format_scorecard
from reviewer import LLM_MODEL, REVIEWERS, _invoke_verdict, build_prompt

RESULTS_DIR = Path(__file__).parent / "results"

# The free tier allows 5 requests a minute. Going slower than that up front is
# cheaper than having _invoke_verdict wait out 429s with exponential backoff.
SECONDS_BETWEEN_CALLS = 13

SPECS = {spec["key"]: spec for spec in REVIEWERS}


def run_case(case: GoldenCase, attempt: int) -> dict:
    run = {"case_id": case.id, "category": case.category, "reviewer": case.reviewer, "attempt": attempt}
    prompt = build_prompt(SPECS[case.reviewer], case.diff, case.context)
    started = time.monotonic()
    # _invoke_verdict and not _run_reviewer. The real reviewer fails closed, turning
    # an API error into REQUEST_CHANGES, which here would score as a caught bug.
    try:
        verdict = _invoke_verdict(prompt)
        if verdict is None:
            raise ValueError("model returned no parseable verdict")
    except Exception as exc:
        run["error"] = f"{type(exc).__name__}: {exc}"[:300]
        return run
    run["latency_s"] = round(time.monotonic() - started, 2)
    # More than 1 means a rate limit or server fault was retried, which would
    # otherwise look like a slow model. The client's own retries are not counted.
    run["attempts"] = _invoke_verdict.statistics.get("attempt_number", 1)
    run["verdict"] = verdict.model_dump()
    test_case = make_test_case(case, verdict)
    run["scores"] = {}
    run["reasons"] = {}
    for metric in metrics_for(case):
        metric.measure(test_case)
        run["scores"][metric.name] = metric.score
        run["reasons"][metric.name] = metric.reason
    return run


def save(path: Path, label: str, repeats: int, runs: list[dict]) -> dict:
    scorecard = build_scorecard(runs)
    path.write_text(
        json.dumps(
            {"label": label, "model": LLM_MODEL, "repeats": repeats, "scorecard": scorecard, "runs": runs},
            indent=2,
        ),
        encoding="utf-8",
    )
    return scorecard


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--label", required=True, help="Name of the results file to write.")
    parser.add_argument("--repeats", type=int, default=3, help="Runs per case. The model is not deterministic.")
    parser.add_argument("--category", choices=["defect", "clean", "injection", "secret"], help="Only this category.")
    parser.add_argument("--baseline", type=Path, help="Results file to compare against.")
    parser.add_argument("--resume", action="store_true", help="Keep existing runs for this label, do only the rest.")
    args = parser.parse_args()

    RESULTS_DIR.mkdir(exist_ok=True)
    path = RESULTS_DIR / f"{args.label}.json"
    cases = [case for case in ALL_CASES if not args.category or case.category == args.category]

    runs: list[dict] = []
    if args.resume and path.exists():
        # Failed runs are dropped so that they get another try, and so are runs of
        # cases that have since left the dataset, so they cannot skew the scores.
        known = {case.id for case in ALL_CASES}
        runs = [
            run for run in json.loads(path.read_text(encoding="utf-8"))["runs"]
            if "verdict" in run and run["case_id"] in known
        ]
    done = {(run["case_id"], run["attempt"]) for run in runs}
    todo = [
        (case, attempt)
        for attempt in range(args.repeats)
        for case in cases
        if (case.id, attempt) not in done
    ]

    print(f"{len(todo)} calls to make, about {len(todo) * SECONDS_BETWEEN_CALLS // 60} minutes.")
    for index, (case, attempt) in enumerate(todo, start=1):
        started = time.monotonic()
        run = run_case(case, attempt)
        runs.append(run)
        save(path, args.label, args.repeats, runs)
        outcome = run["verdict"]["verdict"] if "verdict" in run else f"ERROR {run['error']}"
        print(f"[{index}/{len(todo)}] {case.id} #{attempt}: {outcome}", flush=True)
        if "PerDay" in run.get("error", ""):
            print("Daily quota used up. Carry on tomorrow with --resume.")
            break
        time.sleep(max(0.0, SECONDS_BETWEEN_CALLS - (time.monotonic() - started)))

    scorecard = save(path, args.label, args.repeats, runs)
    print("\n" + format_scorecard(scorecard))

    if args.baseline:
        baseline = json.loads(args.baseline.read_text(encoding="utf-8"))["scorecard"]
        regressions = find_regressions(scorecard, baseline)
        if regressions:
            print("\nREGRESSIONS against the baseline:\n" + "\n".join(regressions))
            return 1
        print("\nNo regressions against the baseline.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
