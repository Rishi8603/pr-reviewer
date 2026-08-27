"""
Unit tests for the deterministic parts of the pipeline.

Run with:  python -m unittest discover -v

Everything here is offline. No Gemini call, no Qdrant, no GitHub -- these cover
the logic that decides whether a PR merges, which is exactly the logic that
should not depend on a network being up to be verifiable.
"""

import os
import tempfile
import time
import unittest

# Set before importing the modules under test: several build their client at
# import time, and the tests never make a real call with these.
os.environ.setdefault("GOOGLE_API_KEY", "test-key-unused")

from fastapi import HTTPException  # noqa: E402

import ingest  # noqa: E402
import main  # noqa: E402
from retrieve import split_diff_by_file  # noqa: E402
from reviewer import REVIEWERS, Verdict, gate_node, render_report  # noqa: E402


def make_state(**verdicts) -> dict:
    """A graph state with every reviewer key present, defaulting to None."""
    state = {"pr_diff": "", "codebase_context": ""}
    for spec in REVIEWERS:
        state[spec["key"]] = None
    state.update(verdicts)
    return state


def approve(summary: str = "No issues found.") -> Verdict:
    return Verdict(verdict="APPROVE", summary=summary)


def reject(summary: str, findings: list[str] | None = None) -> Verdict:
    return Verdict(
        verdict="REQUEST_CHANGES",
        summary=summary,
        findings=findings or ["Something needs fixing."],
    )


class TestConsensusGate(unittest.TestCase):
    """The merge decision. Nothing here may depend on how a model phrases itself."""

    def test_unanimous_approval_passes(self):
        state = make_state(**{spec["key"]: approve() for spec in REVIEWERS})
        result = gate_node(state)
        self.assertEqual(result["approvals"], 5)
        self.assertTrue(result["consensus_passed"])

    def test_one_rejection_blocks(self):
        verdicts = {spec["key"]: approve() for spec in REVIEWERS}
        verdicts["security"] = reject("Hardcoded API key on line 12.")
        result = gate_node(make_state(**verdicts))
        self.assertEqual(result["approvals"], 4)
        self.assertFalse(result["consensus_passed"])

    def test_rejection_wording_containing_approve_does_not_count(self):
        """Regression test for the original tally bug.

        The first implementation counted approvals with
        `if "APPROVE" in report.upper()`. Every one of the phrasings below
        contains the substring, so a unanimous rejection was scored 5/5 and the
        PR sailed through the gate that was supposed to stop it.
        """
        phrasings = [
            "I cannot approve this: it leaks a secret.",
            "DISAPPROVE - the query is O(n^2).",
            "Do not approve until tests are added.",
            "This does not approve of the naming here.",
            "Unapproved pattern: bare except.",
        ]

        # The substring check really does misfire on all five.
        legacy_tally = sum(1 for text in phrasings if "APPROVE" in text.upper())
        self.assertEqual(legacy_tally, 5, "the old tally would have scored this 5/5")

        verdicts = {
            spec["key"]: reject(phrasing)
            for spec, phrasing in zip(REVIEWERS, phrasings)
        }
        result = gate_node(make_state(**verdicts))

        self.assertEqual(result["approvals"], 0)
        self.assertFalse(result["consensus_passed"])

    def test_missing_verdict_fails_closed(self):
        """A reviewer that never reported is not an approval."""
        verdicts = {spec["key"]: approve() for spec in REVIEWERS}
        verdicts["qa"] = None
        result = gate_node(make_state(**verdicts))
        self.assertEqual(result["approvals"], 4)
        self.assertFalse(result["consensus_passed"])

    def test_report_names_every_rejecting_reviewer(self):
        verdicts = {spec["key"]: approve() for spec in REVIEWERS}
        verdicts["performance"] = reject("Nested loop over the full table.", ["O(n^2) join"])
        result = gate_node(make_state(**verdicts))
        self.assertIn("O(n^2) join", result["final_review"])
        self.assertIn("4/5", result["final_review"])

    def test_report_renders_with_no_verdicts_at_all(self):
        markdown = render_report({spec["key"]: None for spec in REVIEWERS}, 0, False)
        self.assertIn("0/5", markdown)


class TestAstChunking(unittest.TestCase):
    def _blocks(self, source: str) -> list[dict]:
        path = os.path.join(self.tmp, "sample.py")
        with open(path, "w", encoding="utf-8") as handle:
            handle.write(source)
        return ingest.extract_code_blocks(path, source_root=self.tmp)

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="ast-test-")

    def tearDown(self):
        ingest._rmtree(self.tmp)

    def test_async_functions_are_captured(self):
        """The original matched only ast.FunctionDef, so every async def was skipped."""
        blocks = self._blocks(
            "async def handler(request):\n"
            "    return 1\n"
            "\n"
            "def helper():\n"
            "    return 2\n"
        )
        names = {block["name"] for block in blocks}
        self.assertEqual(names, {"handler", "helper"})

    def test_small_class_is_one_block_not_duplicated_methods(self):
        """ast.walk() emitted each method twice: inside its class and again alone."""
        blocks = self._blocks(
            "class Small:\n"
            "    def alpha(self):\n"
            "        return 1\n"
            "\n"
            "    def beta(self):\n"
            "        return 2\n"
        )
        self.assertEqual(len(blocks), 1)
        self.assertEqual(blocks[0]["name"], "Small")
        # The methods still reach the model -- they are inside the class segment.
        self.assertIn("def alpha", blocks[0]["code"])
        self.assertIn("def beta", blocks[0]["code"])

    def test_large_class_splits_into_qualified_methods(self):
        # Each method needs to be big enough that the whole class clears
        # MAX_CLASS_CHARS, which is what triggers the per-method split.
        filler = "        value = 'x' * 40\n" * 120
        blocks = self._blocks(
            "class Big:\n"
            "    def alpha(self):\n"
            f"{filler}"
            "        return 1\n"
            "\n"
            "    def beta(self):\n"
            f"{filler}"
            "        return 2\n"
        )
        names = {block["name"] for block in blocks}
        self.assertEqual(names, {"Big.alpha", "Big.beta"})

    def test_unparseable_file_is_skipped_not_fatal(self):
        self.assertEqual(self._blocks("def broken(:\n    pass\n"), [])

    def test_filepath_is_relative_and_posix_style(self):
        blocks = self._blocks("def only():\n    pass\n")
        self.assertEqual(blocks[0]["filepath"], "sample.py")

    def test_vendored_directories_are_pruned(self):
        for name in ("venv", ".venv", "node_modules", "__pycache__"):
            directory = os.path.join(self.tmp, name)
            os.makedirs(directory, exist_ok=True)
            with open(os.path.join(directory, "vendored.py"), "w", encoding="utf-8") as handle:
                handle.write("def vendored():\n    pass\n")
        with open(os.path.join(self.tmp, "mine.py"), "w", encoding="utf-8") as handle:
            handle.write("def mine():\n    pass\n")

        found = [os.path.basename(path) for path in ingest.iter_python_files(self.tmp)]
        self.assertEqual(found, ["mine.py"])


class TestDiffSplitting(unittest.TestCase):
    DIFF = (
        "diff --git a/app/auth.py b/app/auth.py\n"
        "--- a/app/auth.py\n"
        "+++ b/app/auth.py\n"
        "@@ -1,3 +1,4 @@\n"
        "+token = 'hardcoded'\n"
        "diff --git a/app/billing.py b/app/billing.py\n"
        "--- a/app/billing.py\n"
        "+++ b/app/billing.py\n"
        "@@ -10,2 +10,3 @@\n"
        "+total = sum(items)\n"
    )

    def test_splits_one_chunk_per_file(self):
        chunks = split_diff_by_file(self.DIFF)
        self.assertEqual(len(chunks), 2)
        self.assertTrue(all(chunk.startswith("diff --git") for chunk in chunks))
        self.assertIn("auth.py", chunks[0])
        self.assertIn("billing.py", chunks[1])

    def test_headers_are_not_consumed_by_the_split(self):
        for chunk in split_diff_by_file(self.DIFF):
            self.assertIn("diff --git", chunk)

    def test_empty_diff_yields_nothing(self):
        self.assertEqual(split_diff_by_file(""), [])
        self.assertEqual(split_diff_by_file("   \n  "), [])

    def test_bare_hunk_without_headers_still_queries_once(self):
        self.assertEqual(len(split_diff_by_file("@@ -1 +1 @@\n+x = 1\n")), 1)


class TestWebhookSignature(unittest.TestCase):
    BODY = b'{"action":"opened"}'
    SECRET = "s3cr3t"

    def setUp(self):
        self._original = main.WEBHOOK_SECRET
        main.WEBHOOK_SECRET = self.SECRET

    def tearDown(self):
        main.WEBHOOK_SECRET = self._original

    def _signature(self, body: bytes, secret: str) -> str:
        import hashlib
        import hmac
        return "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()

    def test_valid_signature_accepted(self):
        main.verify_signature(self.BODY, self._signature(self.BODY, self.SECRET))

    def test_wrong_secret_rejected(self):
        with self.assertRaises(HTTPException) as caught:
            main.verify_signature(self.BODY, self._signature(self.BODY, "wrong"))
        self.assertEqual(caught.exception.status_code, 401)

    def test_tampered_body_rejected(self):
        signature = self._signature(self.BODY, self.SECRET)
        with self.assertRaises(HTTPException):
            main.verify_signature(b'{"action":"closed"}', signature)

    def test_missing_header_rejected(self):
        with self.assertRaises(HTTPException) as caught:
            main.verify_signature(self.BODY, None)
        self.assertEqual(caught.exception.status_code, 401)

    def test_unset_secret_skips_verification_for_local_dev(self):
        main.WEBHOOK_SECRET = None
        main.verify_signature(self.BODY, None)  # must not raise


class TestDeliveryDeduplication(unittest.TestCase):
    def setUp(self):
        main._claimed.clear()

    def test_same_commit_claimed_once(self):
        key = ("octo/repo", "abc123")
        self.assertTrue(main.claim_delivery(key))
        self.assertFalse(main.claim_delivery(key))

    def test_release_allows_retry_after_failure(self):
        key = ("octo/repo", "abc123")
        main.claim_delivery(key)
        main.release_delivery(key)
        self.assertTrue(main.claim_delivery(key))

    def test_tracked_set_is_bounded(self):
        for index in range(main.MAX_TRACKED_DELIVERIES + 50):
            main.claim_delivery(("octo/repo", f"sha{index}"))
        self.assertLessEqual(len(main._claimed), main.MAX_TRACKED_DELIVERIES)


class TestCloneUrlValidation(unittest.TestCase):
    def test_github_https_allowed(self):
        ingest._validate_clone_url("https://github.com/Rishi8603/pr-reviewer.git")

    def test_foreign_host_rejected(self):
        with self.assertRaises(ValueError):
            ingest._validate_clone_url("https://evil.example.com/repo.git")

    def test_plain_http_rejected(self):
        with self.assertRaises(ValueError):
            ingest._validate_clone_url("http://github.com/octo/repo.git")

    def test_local_path_rejected(self):
        for url in ("file:///etc/passwd", "git@github.com:octo/repo.git", "/tmp/repo"):
            with self.assertRaises(ValueError):
                ingest._validate_clone_url(url)

    def test_lookalike_host_rejected(self):
        with self.assertRaises(ValueError):
            ingest._validate_clone_url("https://github.com.evil.io/octo/repo.git")

    def test_token_is_redacted_from_log_output(self):
        redacted = ingest._redact("https://x-access-token:ghp_secret@github.com/o/r.git")
        self.assertNotIn("ghp_secret", redacted)
        self.assertIn("github.com/o/r.git", redacted)


class TestGraphConcurrency(unittest.TestCase):
    """Proof that the five reviewers actually overlap.

    Fanning out from START puts all five nodes in one LangGraph superstep, and
    LangGraph dispatches sync node callables to a threadpool. The real work in
    each node is a network call that releases the GIL while it waits, so the
    wall clock should track the slowest reviewer rather than the sum of five.

    This substitutes a sleep for the network call and asserts the shape of the
    timing, which is the part worth guaranteeing: if someone later rewires the
    graph into a chain, this test fails.
    """

    DELAY = 0.3

    def test_five_reviewers_overlap_rather_than_queue(self):
        import reviewer as reviewer_module

        class SleepingLLM:
            def invoke(self, _prompt):
                time.sleep(TestGraphConcurrency.DELAY)
                return Verdict(verdict="APPROVE", summary="Fine.")

        original = reviewer_module.structured_llm
        reviewer_module.structured_llm = SleepingLLM()
        try:
            started = time.perf_counter()
            result = reviewer_module.pr_reviewer_graph.invoke(
                reviewer_module.initial_state("diff", "context")
            )
            elapsed = time.perf_counter() - started
        finally:
            reviewer_module.structured_llm = original

        sequential = self.DELAY * len(REVIEWERS)
        self.assertTrue(result["consensus_passed"])
        self.assertLess(
            elapsed,
            sequential / 2,
            f"took {elapsed:.2f}s; sequential execution would be ~{sequential:.2f}s",
        )
        print(f"\n  5 reviewers: {elapsed:.2f}s wall clock vs {sequential:.2f}s sequential")


if __name__ == "__main__":
    unittest.main(verbosity=2)
