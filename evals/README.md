# LLM evals

The unit tests in the repo root check the code around the model. These check the
model's answers: does a reviewer catch a real bug, approve good code, resist
instructions hidden in a diff, and keep credentials out of its report?

## Run it

Needs `GOOGLE_API_KEY`. A full run is 117 Gemini calls, about 25 minutes.

```bash
pip install -r requirements.txt
pip install -r requirements-test.txt
python -m evals.run_evals --label my-change --baseline evals/results/baseline.json
```

It exits with status 1 if a metric regressed. Add `--resume` to continue a stopped
run, and `--category secret` to run one category. The metrics themselves have
offline tests: `python -m unittest evals.test_eval_logic`.

## What is in it

| File | Purpose |
|---|---|
| `golden_cases.py` | 39 cases with known answers: 13 seeded bugs, 3 clean PRs sent to all 5 reviewers, 9 prompt injections, 2 secret leaks |
| `metrics.py` | DeepEval metrics: verdict accuracy, defect identified, finding groundedness, secret not echoed |
| `run_evals.py` | Calls the model 3 times per case, because answers vary, and records every result |
| `report.py` | Builds the scorecard and compares it with a baseline |

The metrics are rules rather than an LLM judge. They cost no quota, and the same
output always gets the same score, so a change in score means the model changed.

## Results

From `results/baseline.json`, 117 runs of `gemini-flash-lite-latest` on the final prompt.

| Metric | Result |
|---|---|
| Seeded bugs rejected for the right reason | 39 of 39 |
| Clean PR verdicts that approved | 44 of 45 |
| Clean PRs passing the 5-of-5 gate | 8 of 9 |
| Prompt injection attacks that flipped a verdict | 0 of 27 |
| Reports repeating a secret | 0 of 6, and 0 of 14 in a larger sample |
| Findings naming code that does not exist (groundedness) | 1.0, none detected |
| Repeat runs agreeing with the majority verdict | 99.1% |

## What the evals changed

**The secret leak.** The original prompt quoted a fake credential in 4 of 20 reports
that completed. A new prompt line, telling reviewers not to quote values, took that
to 0 of 20. These are small samples.

**The fix caused a regression, and the suite caught it.** That line made the QA
reviewer start rejecting a one-word docstring fix, because of an existing bug in the
unchanged lines around it. A direct comparison on that case:

| Prompt | Approved the clean change |
|---|---|
| Original | 10 of 10 |
| With the secret line | 4 of 10 |
| With the secret line, plus "judge only the lines this PR adds or removes" | 10 of 10 |

**Test cases can be wrong too.** On the first run no clean PR passed the gate. Reading
the findings showed the cases were unfair: one had tests inside the source file, one
lacked the type hints the Style brief asks for, and one had a docstring promising
behaviour the diff did not show. They were rewritten after seeing the results, so the
clean scores are weaker evidence than the seeded-bug scores.
`results/first-run-original-clean-cases.json` keeps that run.

## Limits

- **The bugs are easy.** Each seeded bug is obvious and alone in a tiny diff, so 39 of
  39 says little about subtle bugs in a large PR.
- **The attacks are few.** Three payload styles, on three diffs. Zero successes means
  these failed, not that the reviewers cannot be tricked.
- **Groundedness is a heuristic.** It catches invented file and identifier names, not
  false claims about real code. It has not yet flagged a live hallucination, so its
  real-world sensitivity is unproven.
- **Retrieval is not evaluated.** The cases pass no codebase context, so the RAG step
  is not covered.
- **One model alias.** `gemini-flash-lite-latest` can change without a commit, which is
  why the workflow runs weekly.
- **Failed calls are excluded.** Connection errors are not scored, since they say
  nothing about review quality. See `.github/workflows/llm-evals.yml` for the schedule.
