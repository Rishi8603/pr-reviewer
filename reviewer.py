"""
The review graph: five specialised reviewers fan out, one gate fans them in.

Two things changed from the first version, both about trusting the gate:

1. Reviewers return a constrained `Verdict` object instead of free prose that
   was then string-matched. The old tally was `if "APPROVE" in report.upper()`,
   which counted "I cannot approve this" and "DISAPPROVE" as approvals -- the
   component advertised as the deterministic safeguard could silently pass a PR
   every reviewer had rejected.

2. The final Markdown is rendered in Python rather than by a sixth LLM call.
   Once the tally is decided there is nothing left to reason about, and letting
   a model narrate the outcome meant it could write encouraging prose over a
   3/5 rejection. Removing it also drops one round-trip from a latency budget
   the developer is sitting and waiting through.
"""

import os
from typing import Literal, TypedDict

from dotenv import load_dotenv
from langchain_google_genai import ChatGoogleGenerativeAI
from langgraph.graph import END, START, StateGraph
from pydantic import BaseModel, Field

load_dotenv()

LLM_MODEL = os.getenv("REVIEW_MODEL", "gemini-2.5-flash")

# Low but not zero. Zero makes the model brittle on prompts it finds ambiguous;
# this is high enough to stay fluent and low enough that two runs of the same
# diff reach the same verdict.
TEMPERATURE = 0.1

REQUEST_TIMEOUT_SECONDS = 90


class Verdict(BaseModel):
    """One reviewer's decision, shaped so the tally never has to interpret text."""

    verdict: Literal["APPROVE", "REQUEST_CHANGES"]
    summary: str = Field(description="One or two sentences explaining the decision.")
    findings: list[str] = Field(
        default_factory=list,
        description="Specific, actionable issues. Empty when the verdict is APPROVE.",
    )


class AgentState(TypedDict):
    """Shared state for the graph.

    Each reviewer writes exactly one key, which is why the five can run in the
    same superstep with no reducer: there is never a write conflict to resolve.
    """

    pr_diff: str
    codebase_context: str
    security: Verdict | None
    performance: Verdict | None
    style: Verdict | None
    qa: Verdict | None
    pm: Verdict | None
    approvals: int
    total_reviewers: int
    consensus_passed: bool
    final_review: str


llm = ChatGoogleGenerativeAI(
    model=LLM_MODEL,
    temperature=TEMPERATURE,
    timeout=REQUEST_TIMEOUT_SECONDS,
    max_retries=2,
)

# with_structured_output binds a response schema, so the model is constrained at
# decode time instead of being asked politely to comply in the prompt.
structured_llm = llm.with_structured_output(Verdict)


# =====================================================================
# THE FIVE REVIEWERS
# One spec per reviewer rather than five near-identical functions: adding a
# sixth reviewer is one list entry plus one graph edge, and the shared runner
# means the fail-closed behaviour cannot be forgotten in one of them.
# =====================================================================
REVIEWERS = [
    {
        "key": "security",
        "label": "Security",
        "emoji": "🛡️",
        "brief": (
            "You are a strict security auditor. Look for hardcoded credentials, "
            "injection risks (SQL, command, path traversal), missing authentication "
            "or authorisation checks, unsafe deserialisation, and secrets in logs."
        ),
    },
    {
        "key": "performance",
        "label": "Performance",
        "emoji": "🚀",
        "brief": (
            "You are a performance engineer. Look for algorithmic complexity "
            "regressions, N+1 queries, work inside loops that could be hoisted, "
            "unbounded memory growth, and blocking I/O on a hot path."
        ),
    },
    {
        "key": "style",
        "label": "Style",
        "emoji": "💅",
        "brief": (
            "You are a senior reviewer enforcing readability. Look for unclear "
            "naming, dead code, missing type hints, and inconsistency with the "
            "conventions visible in the retrieved codebase context. Ignore anything "
            "an autoformatter would fix on its own."
        ),
    },
    {
        "key": "qa",
        "label": "QA / Tests",
        "emoji": "🧪",
        "brief": (
            "You are a QA lead. Check whether new behaviour arrived with tests, and "
            "look for unhandled edge cases: empty collections, None, zero, negative "
            "numbers, boundary indices, and unhandled exceptions."
        ),
    },
    {
        "key": "pm",
        "label": "Product",
        "emoji": "👔",
        "brief": (
            "You are a pragmatic product owner. Check whether the change is scoped "
            "to one coherent piece of work, and flag unrelated refactoring or "
            "speculative abstraction that is not needed yet."
        ),
    },
]

RUBRIC = """
{brief}

Review the pull request diff below.

Return REQUEST_CHANGES only for a concrete problem you can point at in this
diff, with a specific finding for each one. Do not invent issues to look
thorough, and do not comment on matters outside your speciality -- another
reviewer covers those. If you find nothing in your area, return APPROVE.

=== PR DIFF ===
{diff}

=== EXISTING CODEBASE CONTEXT (retrieved by similarity to the changed files) ===
{context}
""".strip()


def _run_reviewer(spec: dict, state: AgentState) -> Verdict:
    # Log lines stay ASCII. The emoji belong in the Markdown report, which goes
    # to GitHub as UTF-8 over HTTP; stdout on Windows is cp1252 by default and
    # printing an emoji there raises UnicodeEncodeError mid-review.
    print(f"[{spec['label']}] reviewer analysing...")
    prompt = RUBRIC.format(
        brief=spec["brief"],
        diff=state["pr_diff"],
        context=state.get("codebase_context") or "(no context retrieved)",
    )
    try:
        verdict = structured_llm.invoke(prompt)
        if verdict is None:
            raise ValueError("model returned no parseable verdict")
        return verdict
    except Exception as exc:
        # Fail closed. A reviewer we could not hear from is not an approval --
        # treating an API timeout as consent is exactly how a gate stops being a
        # gate. The merge blocks and the report says why.
        print(f"{spec['label']} reviewer failed: {type(exc).__name__}: {exc}")
        return Verdict(
            verdict="REQUEST_CHANGES",
            summary=f"The {spec['label']} reviewer could not complete ({type(exc).__name__}).",
            findings=[f"Review incomplete: {type(exc).__name__}. Re-run before merging."],
        )


def make_reviewer_node(spec: dict):
    def node(state: AgentState) -> dict:
        return {spec["key"]: _run_reviewer(spec, state)}

    node.__name__ = f"{spec['key']}_node"
    return node


# =====================================================================
# THE CONSENSUS GATE
# Pure Python, no model call. This is the only place the merge decision is made.
# =====================================================================
def render_report(verdicts: dict[str, Verdict | None], approvals: int, passed: bool) -> str:
    total = len(REVIEWERS)
    if passed:
        headline = f"### ✅ Approved — {approvals}/{total} consensus reached"
    else:
        headline = f"### ❌ Changes requested — {approvals}/{total} reviewers approved"

    lines = ["## 🤖 Autonomous PR Review Swarm", "", headline, "", "| Reviewer | Verdict |", "| --- | --- |"]
    for spec in REVIEWERS:
        verdict = verdicts.get(spec["key"])
        approved = verdict is not None and verdict.verdict == "APPROVE"
        mark = "✅ Approved" if approved else "❌ Changes requested"
        lines.append(f"| {spec['emoji']} {spec['label']} | {mark} |")

    for spec in REVIEWERS:
        verdict = verdicts.get(spec["key"])
        if verdict is None or verdict.verdict == "APPROVE":
            continue
        lines += ["", f"### {spec['emoji']} {spec['label']}", "", verdict.summary]
        for finding in verdict.findings:
            lines.append(f"- {finding}")

    if passed:
        approved_summaries = [
            f"- **{spec['emoji']} {spec['label']}** — {verdicts[spec['key']].summary}"
            for spec in REVIEWERS
            if verdicts.get(spec["key"]) is not None
        ]
        lines += ["", "<details><summary>Reviewer notes</summary>", ""] + approved_summaries + ["", "</details>"]

    lines += [
        "",
        "---",
        f"*Merge requires unanimous approval ({total}/{total}). The tally is computed in Python "
        "from constrained reviewer verdicts, not parsed from model prose.*",
    ]
    return "\n".join(lines)


def gate_node(state: AgentState) -> dict:
    print("Consensus gate tallying verdicts...")

    verdicts = {spec["key"]: state.get(spec["key"]) for spec in REVIEWERS}

    # An exact comparison against a Literal field. There is no substring in this
    # decision, so no phrasing a reviewer chooses can flip it.
    approvals = sum(
        1 for verdict in verdicts.values()
        if verdict is not None and verdict.verdict == "APPROVE"
    )
    passed = approvals == len(REVIEWERS)

    status = "APPROVED" if passed else "CHANGES REQUESTED"
    print(f"Consensus: {approvals}/{len(REVIEWERS)} -> {status}")

    return {
        "approvals": approvals,
        "total_reviewers": len(REVIEWERS),
        "consensus_passed": passed,
        "final_review": render_report(verdicts, approvals, passed),
    }


# =====================================================================
# THE GRAPH
# =====================================================================
workflow = StateGraph(AgentState)

for spec in REVIEWERS:
    workflow.add_node(spec["key"], make_reviewer_node(spec))
workflow.add_node("gate", gate_node)

# FAN-OUT: every reviewer is an edge from START, which puts all five in the same
# superstep. LangGraph dispatches sync node callables to a threadpool, and the
# work inside each is a network call to Gemini that releases the GIL while it
# waits -- so the wall clock is the slowest reviewer, not the sum of five.
for spec in REVIEWERS:
    workflow.add_edge(START, spec["key"])

# FAN-IN: the list form makes the gate wait for all five before it runs.
workflow.add_edge([spec["key"] for spec in REVIEWERS], "gate")
workflow.add_edge("gate", END)

pr_reviewer_graph = workflow.compile()


def initial_state(pr_diff: str, codebase_context: str) -> AgentState:
    """Build a complete starting state, so no node reads a missing key."""
    state: AgentState = {
        "pr_diff": pr_diff,
        "codebase_context": codebase_context,
        "approvals": 0,
        "total_reviewers": len(REVIEWERS),
        "consensus_passed": False,
        "final_review": "",
    }
    for spec in REVIEWERS:
        state[spec["key"]] = None
    return state
