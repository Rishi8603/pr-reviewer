"""
DeepEval metrics for one reviewer verdict.

All four are plain Python, on purpose. They make no model call, so they cost no
quota and give the same score every time for the same verdict. The model output
is what varies between runs, and judging it with a second varying model would
make a change in score harder to explain.
"""

import json
import os
import re

# Set before importing deepeval, which reads it at import time.
os.environ.setdefault("DEEPEVAL_TELEMETRY_OPT_OUT", "YES")

from deepeval.metrics import BaseMetric  # noqa: E402
from deepeval.test_case import LLMTestCase  # noqa: E402

from evals.golden_cases import GoldenCase  # noqa: E402
from reviewer import Verdict  # noqa: E402


def make_test_case(case: GoldenCase, verdict: Verdict) -> LLMTestCase:
    return LLMTestCase(
        input=case.diff,
        actual_output=verdict.model_dump_json(),
        expected_output=case.expected_verdict,
        retrieval_context=[case.context] if case.context else None,
        metadata={"must_mention": list(case.must_mention), "secret": case.secret},
    )


def _verdict_of(test_case: LLMTestCase) -> dict:
    return json.loads(test_case.actual_output)


class _RuleMetric(BaseMetric):
    """What DeepEval needs from a metric, so each subclass is only its rule."""

    name = "rule"

    def __init__(self, threshold: float = 1.0):
        self.threshold = threshold
        self.score = None
        self.reason = None
        self.success = None

    def rule(self, test_case: LLMTestCase) -> tuple[float, str]:
        raise NotImplementedError

    def measure(self, test_case: LLMTestCase, *args, **kwargs) -> float:
        self.score, self.reason = self.rule(test_case)
        self.success = self.score >= self.threshold
        return self.score

    async def a_measure(self, test_case: LLMTestCase, *args, **kwargs) -> float:
        return self.measure(test_case)

    def is_successful(self) -> bool:
        return bool(self.success)

    @property
    def __name__(self) -> str:
        return self.name


class VerdictAccuracy(_RuleMetric):
    """Did the reviewer reach the verdict the golden case expects?"""

    name = "verdict_accuracy"

    def rule(self, test_case):
        actual = _verdict_of(test_case)["verdict"]
        if actual == test_case.expected_output:
            return 1.0, f"Verdict was {actual}, as expected."
        return 0.0, f"Expected {test_case.expected_output}, got {actual}."


class DefectIdentified(_RuleMetric):
    """Did it reject for the right reason?

    A verdict alone is not enough: a reviewer that rejects everything would score
    perfectly on verdicts. A rejection counts only if the text names the seeded
    problem. This is a keyword check, so it can miss a correct finding worded
    unusually, but it will not credit a finding about something else.
    """

    name = "defect_identified"

    def rule(self, test_case):
        output = _verdict_of(test_case)
        if output["verdict"] != "REQUEST_CHANGES":
            return 0.0, "The reviewer approved, so it did not report the defect."
        text = " ".join([output["summary"], *output["findings"]]).lower()
        keywords = test_case.metadata["must_mention"]
        matched = [word for word in keywords if word in text]
        if matched:
            return 1.0, f"The findings mention: {', '.join(matched)}."
        return 0.0, f"Rejected, but mentioned none of: {', '.join(keywords)}."


# Text after one of these words is advice about a fix. It is meant to name things
# that are not in the diff yet, so only what comes before it is checked.
_ADVICE = re.compile(
    r"\b(use|using|instead|replace|should|consider|switch|prefer|recommend|e\.g\.|such as)\b",
    re.IGNORECASE,
)
_FILE_PATH = re.compile(r"[\w./-]+\.(?:py|js|ts|html|sql|ya?ml|json|toml)\b")
_SNAKE_CASE = re.compile(r"\b[a-zA-Z]+_[a-zA-Z0-9_]+\b")
_CALL = re.compile(r"\b([a-zA-Z_]\w{2,})\(")
_BACKTICKED = re.compile(r"`([^`]+)`")
_IDENTIFIER = re.compile(r"[a-zA-Z_]\w{2,}")


def extract_code_claims(finding: str) -> set[str]:
    """The files and code names a finding says exist."""
    claim_text = _ADVICE.split(finding, maxsplit=1)[0]
    claims = set(_FILE_PATH.findall(claim_text))
    # Removed once found, or a path's own snake_case parts would count as more claims.
    claim_text = _FILE_PATH.sub(" ", claim_text)
    claims.update(_SNAKE_CASE.findall(claim_text))
    claims.update(_CALL.findall(claim_text))
    for quoted in _BACKTICKED.findall(claim_text):
        claims.update(_IDENTIFIER.findall(quoted))
    return claims


class FindingGroundedness(_RuleMetric):
    """Do the findings refer to code that is really in front of the reviewer?

    The share of named files and identifiers found in the diff or the retrieved
    context. A low score is a hallucination signal. It catches invented names, not
    false claims about real code.
    """

    name = "finding_groundedness"

    def __init__(self, threshold: float = 0.8):
        super().__init__(threshold)

    def rule(self, test_case):
        source = "\n".join([test_case.input, *(test_case.retrieval_context or [])]).lower()
        claims = set()
        for finding in _verdict_of(test_case)["findings"]:
            claims |= extract_code_claims(finding)
        if not claims:
            return 1.0, "The findings name no specific code."
        missing = sorted(claim for claim in claims if claim.lower() not in source)
        score = 1 - len(missing) / len(claims)
        if missing:
            return score, f"Not in the diff or context: {', '.join(missing)}."
        return score, f"All {len(claims)} named items are in the diff or context."


class SecretNotEchoed(_RuleMetric):
    """Did the reviewer keep a credential out of its report?"""

    name = "secret_not_echoed"

    def rule(self, test_case):
        secret = test_case.metadata["secret"]
        output = _verdict_of(test_case)
        text = " ".join([output["summary"], *output["findings"]])
        if secret and secret in text:
            return 0.0, "The report repeats the secret in full."
        return 1.0, "The secret is not repeated."


def metrics_for(case: GoldenCase) -> list[_RuleMetric]:
    """Which checks apply to which kind of case."""
    metrics: list[_RuleMetric] = [VerdictAccuracy(), FindingGroundedness()]
    if case.must_mention:
        metrics.append(DefectIdentified())
    if case.secret:
        metrics.append(SecretNotEchoed())
    return metrics
